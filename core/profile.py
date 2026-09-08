#!/usr/bin/env python3
"""
profile / taxonomy resolver — the data layer the Apply engine calls at fill-time.

  python profile.py classify --field "Are you legally authorized to work in the US?"
  python profile.py classify --field "Desired salary" --company Ramp
  python profile.py get --key identity.email
  python profile.py resume --field "Quantitative Researcher" [--jd jd.txt]
  python profile.py checkfill          # list unfilled FILL_ME values
  python profile.py --selftest

classify() is the core: given a form field's visible text, returns which bucket it
falls in and — for deterministic fields — the resolved, render-ready value. Buckets and
patterns live in taxonomy.yaml (config-driven); values live in profile.json. The apply
engine uses:  deterministic -> fill silently; draftable -> hand answer_key to the drafter;
always_ask -> surface to the user; None -> escalate to Tier 1 Claude field-mapping.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path
import yaml

# Living under core/ also stops this module shadowing the stdlib `profile`.
from core import paths
from server.store import get_store

# taxonomy.yaml stays a file. It is tuning, not identity: keyword rules that ship with the
# code as working defaults, which is what lets a fresh clone run before anything is
# imported. Only the personal half moves into the store.
TAXONOMY = paths.TAXONOMY
FILL_ME = "FILL_ME"


def load_profile() -> dict:
    return get_store().get_profile()


def load_taxonomy() -> dict:
    return yaml.safe_load(TAXONOMY.read_text(encoding="utf-8"))


def dig(profile: dict, dotted: str):
    """Resolve a dotted value_key like 'identity.address.city' against profile.json.

    A numeric segment indexes a list ('work_history.0.employer'). Before work_history
    existed the profile was all dicts, so a numeric segment silently resolved to None —
    which reads in taxonomy.yaml exactly like a correctly-spelled key for a field the
    user has not filled in yet. Keep the two cases distinguishable.
    """
    cur = profile
    for part in dotted.split("."):
        if isinstance(cur, list):
            if not part.lstrip("-").isdigit():
                return None
            i = int(part)
            if not (-len(cur) <= i < len(cur)):
                return None
            cur = cur[i]
            continue
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def render(value) -> str:
    """Render a resolved value the way a text field expects it."""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return "" if value is None else str(value)


def _buckets_for(tax: dict, company: str | None) -> dict:
    """Merge per-company override patterns (prepended) onto the base buckets."""
    buckets = {k: list(v) for k, v in tax["buckets"].items()}
    overrides = tax.get("overrides") or {}
    if company and company in overrides:
        for bucket, extra in overrides[company].items():
            buckets.setdefault(bucket, [])
            buckets[bucket] = list(extra) + buckets[bucket]
    return buckets


def resolve_alternates(profile: dict, rule: dict, value_key: str) -> list[str]:
    """Ordered fallback answers for a deterministic rule, rendered ready to match.

    Two sources, in this order:
      1. the rule's own `alternates:` — a DIFFERENT answer for the same question, used
         when one question text fronts two widget kinds ("Phone Number" = a text box on
         most boards, an SMS-consent Yes/No on Ashby). Each entry is a dotted profile
         path if it resolves, else the literal string.
      2. profile.json `answer_alternates[value_key]` — the SAME answer in other words
         (tenant vocabularies), or a ranked list of equally-true answers.

    Only the caller knows whether the first choice fit, so nothing is filtered here
    beyond dropping blanks and unset placeholders. Duplicates are collapsed keeping
    first position, since order is the whole meaning of the list.
    """
    out: list[str] = []
    for entry in rule.get("alternates") or []:
        val = dig(profile, entry) if isinstance(entry, str) else None
        # A path that resolves wins; anything else is the literal answer itself. Human
        # sentences ("I've already graduated") never resolve, so this stays unambiguous.
        out.append(render(val) if val is not None else str(entry))
    # NOT dig("answer_alternates.<value_key>"): the keys there contain literal dots
    # ("eeo.veteran_status" is ONE key), so the block must be fetched and indexed whole —
    # same access the Workday driver already uses.
    out.extend(str(a) for a in ((dig(profile, "answer_alternates") or {}).get(value_key) or []))
    seen, uniq = set(), []
    for a in out:
        if not a or a == FILL_ME or a in seen:
            continue
        seen.add(a)
        uniq.append(a)
    return uniq


def classify(field_text: str, company: str | None = None,
             profile: dict | None = None, tax: dict | None = None) -> dict:
    """Return {bucket, value_key?, answer_key?, value?, rendered?, filled?, alternates?}.

    bucket is one of deterministic/draftable/always_ask, or None if no rule matched
    (caller escalates to Tier 1). For deterministic, value/rendered are resolved and
    filled=False flags an unset FILL_ME placeholder the user still owes.

    `alternates` is the ordered list of OTHER answers to try when `rendered` matches none
    of the control's options — see resolve_alternates().
    """
    profile = profile if profile is not None else load_profile()
    tax = tax if tax is not None else load_taxonomy()
    text = (field_text or "").lower()
    buckets = _buckets_for(tax, company)

    for bucket in tax["priority"]:
        for rule in buckets.get(bucket, []):
            if re.search(rule["pattern"], text, re.IGNORECASE):
                out = {"bucket": bucket, "matched": rule["pattern"]}
                if bucket == "deterministic":
                    vk = rule["value_key"]
                    val = dig(profile, vk)
                    out.update(value_key=vk, value=val, rendered=render(val),
                               filled=(val is not None and val != FILL_ME),
                               alternates=resolve_alternates(profile, rule, vk))
                elif bucket == "draftable":
                    out["answer_key"] = rule["answer_key"]
                return out
    return {"bucket": None}


def resume_filename(track: dict, profile: dict | None = None) -> str:
    """The filename a resume is attached under, built from your name rather than the PDF's.

    The source filename is whatever the file happened to be called on disk, and after
    `python -m core.resumes sync-local` that is the Finals-folder name. Deriving the
    outgoing name from it meant an employer saw a different string per track, and any
    name that did not already read `..._<tag>_vN.pdf` went out verbatim. Build the name
    from `identity` instead, so every track sends `First_Last[_x].pdf`.

    The suffix is the track tag's FIRST LETTER, and only when the profile defines more
    than one track. One letter because the Workday adapter reads the attached track back
    off the filename (`attachedNamePattern`), which needs the letters to stay distinct.

    If the identity is unset, fall back to the source filename: an application with an
    oddly named CV is better than one carrying `FILL_ME.pdf`.
    """
    profile = profile if profile is not None else load_profile()
    ident = profile.get("identity") or {}

    def _clean(v) -> str:
        v = "" if v is None or v == FILL_ME else str(v).strip()
        return re.sub(r"[^A-Za-z0-9]+", "_", v).strip("_")

    first, last = _clean(ident.get("first_name")), _clean(ident.get("last_name"))
    stem = "_".join(p for p in (first, last) if p)
    if not stem:
        stem = _clean(ident.get("full_name"))
    if not stem:
        src = track.get("current_file")
        # An unfilled track carries the placeholder rather than omitting the key, so
        # without this check the file goes out called FILL_ME.pdf.
        return src if src and src != FILL_ME else f"{track.get('tag') or 'resume'}.pdf"

    tracks = profile.get("resume_tracks") or []
    tag = str(track.get("tag") or "")
    if len(tracks) > 1 and tag:
        stem = f"{stem}_{tag[0].lower()}"
    ext = Path(track.get("local_path") or track.get("current_file") or "x.pdf").suffix or ".pdf"
    return f"{stem}{ext}"


def pick_resume(field_text: str, jd_text: str = "", profile: dict | None = None) -> dict:
    """Static best-of-4 pick by keyword overlap (role title weighted 2x over JD body)."""
    profile = profile if profile is not None else load_profile()
    title = (field_text or "").lower()
    jd = (jd_text or "").lower()
    best, best_key = None, (-1, -1)
    for track in profile["resume_tracks"]:
        score = sum(2 for k in track["keywords"] if k in title)
        score += sum(1 for k in track["keywords"] if k in jd)
        # tiebreak by specificity: total chars of matched phrases (title 2x)
        spec = sum(2 * len(k) for k in track["keywords"] if k in title)
        spec += sum(len(k) for k in track["keywords"] if k in jd)
        if (score, spec) > best_key:
            best, best_key = track, (score, spec)
    best_score = best_key[0]
    if best_score <= 0:  # nothing matched -> fall back to the configured default
        tag = profile.get("default_resume_tag")
        best = next((t for t in profile["resume_tracks"] if t["tag"] == tag), best)
        best_score = 0
    return {"tag": best["tag"], "local_path": best["local_path"],
            "current_file": best["current_file"], "score": best_score,
            "send_as": resume_filename(best, profile)}


def resolve_stream(profile: dict, tag: str) -> dict:
    """Collapse every `by_stream` block in the profile down to one track's wording.

    The four resumes describe the SAME two jobs in four different vocabularies — the
    quant PDF sells a MILP solver, the AI one sells a ReAct agent, and they are the same
    work. So a form filled from a stream-blind profile contradicts the PDF stapled to it,
    which is exactly the kind of mismatch a recruiter reads as embellishment.

    The fix is to make the stream a decision taken ONCE, at the top of an application —
    `pick_resume` already takes it to choose the PDF — and then resolve the profile
    through that same tag before anything downstream looks at it. Everything after this
    point (classify, dig, the extension's fill) sees a plain profile with plain string
    values and needs no knowledge that streams exist.

    A `by_stream` block holds only the keys that actually differ; anything absent falls
    through to the sibling default, so employer/title/dates stay written once. Those are
    facts and must NOT vary by stream — only emphasis may.
    """
    if isinstance(profile, dict):
        out = {}
        overrides = profile.get("by_stream") or {}
        chosen = overrides.get(tag) or {}
        for k, v in profile.items():
            if k == "by_stream":
                continue                       # consumed, never leaks into the result
            out[k] = resolve_stream(v, tag)
        for k, v in chosen.items():
            out[k] = _merge(out.get(k), resolve_stream(v, tag))
        return out
    if isinstance(profile, list):
        return [resolve_stream(v, tag) for v in profile]
    return profile


def _merge(base, override):
    """Overlay `override` on `base`, recursing into dicts.

    A dict override MERGES rather than replaces. Overriding one key of a block must not
    delete the rest of it — `by_stream.quant.identity.first_name` shadows exactly that
    name and leaves the address, links and everything else standing. Plain replacement
    looks correct on a one-key fixture and quietly empties the block in production.

    Lists replace wholesale: there is no honest way to merge two ordered sequences (index
    alignment is a guess), so a stream that wants a different list writes the whole list.
    """
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for k, v in override.items():
            out[k] = _merge(out.get(k), v)
        return out
    return override


def stream_tags(profile: dict) -> list[str]:
    """The stream tags the profile actually defines (the resume track tags)."""
    return [t["tag"] for t in profile.get("resume_tracks", [])]


def find_fillme(profile: dict, prefix: str = "") -> list[str]:
    out = []
    if isinstance(profile, dict):
        for k, v in profile.items():
            out += find_fillme(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(profile, list):
        for i, v in enumerate(profile):
            out += find_fillme(v, f"{prefix}[{i}]")
    elif profile == FILL_ME:
        out.append(prefix)
    return out


# ---------------------------------------------------------------- CLI

def cmd_classify(a):
    r = classify(a.field, a.company)
    print(json.dumps(r, indent=2))
    if r.get("bucket") == "deterministic" and not r.get("filled"):
        print(f"  ! value_key '{r['value_key']}' is unset ({FILL_ME}) — run checkfill", file=sys.stderr)


def cmd_get(a):
    val = dig(load_profile(), a.key)
    if val is None:
        sys.exit(f"no key '{a.key}'")
    print(render(val))


def cmd_resume(a):
    jd = Path(a.jd).read_text(encoding="utf-8") if a.jd else ""
    print(json.dumps(pick_resume(a.field, jd), indent=2))


# Values that are ASSERTIONS ABOUT YOU rather than contact details: they ship with a
# plausible default, so FILL_ME never flags them, but submitting someone else's answer to
# any of these is a materially false statement on a real job application. checkfill is
# documented as "what you still owe", so it has to name them even though they are set.
MUST_REVIEW = [
    "work_authorization.authorized_us",
    "work_authorization.require_sponsorship",
    "work_authorization.us_citizen",
    "work_authorization.relocation",
    "attestations.age_18_or_older",
    "attestations.background_check_consent",
    "attestations.security_clearance",
    "attestations.non_compete",
    "attestations.criminal_history",
    "attestations.previously_applied_or_interviewed",
    "affiliations.previously_employed",
    "affiliations.government_affiliation",
    "affiliations.external_business",
    "affiliations.related_to_employee",
    # Both ship preset in profile.example.json, so nothing else would ever surface them.
    "identity.based_in_us",
    "attestations.sms_consent",
]


def cmd_checkfill(a):
    prof = load_profile()
    holes = find_fillme(prof)
    if holes:
        print(f"{len(holes)} unfilled value(s):")
        for h in holes:
            print(f"  {h}")
    else:
        print("profile complete — no FILL_ME placeholders")

    # A defaulted answer looks identical to a considered one, which is exactly the problem.
    print("\nSet, but assertions about YOU — confirm each is actually true before applying:")
    for key in MUST_REVIEW:
        val = dig(prof, key)
        if val is not None:
            print(f"  {key} = {render(val)}")


def selftest():
    profile, tax = load_profile(), load_taxonomy()
    cases = [
        ("First Name", None, "deterministic", "identity.first_name"),
        ("Email Address", None, "deterministic", "identity.email"),
        ("Are you legally authorized to work in the United States?", None, "deterministic", "work_authorization.authorized_us"),
        ("Will you now or in the future require visa sponsorship?", None, "deterministic", "work_authorization.require_sponsorship"),
        ("Are you willing to relocate?", None, "deterministic", "work_authorization.relocation"),
        ("LinkedIn Profile", None, "deterministic", "links.linkedin"),
        ("Why do you want to work at our company?", None, "draftable", None),
        ("Tell us about yourself", None, "draftable", None),
        ("Desired salary", None, "always_ask", None),
        ("What is your current compensation?", None, "always_ask", None),
        ("How many years of experience do you have?", None, "always_ask", None),
        ("Gender", None, "deterministic", "eeo.gender"),
        ("Race/Ethnicity", None, "deterministic", "eeo.race_ethnicity"),
        ("Veteran Status", None, "deterministic", "eeo.veteran_status"),
        ("Disability Status", None, "deterministic", "eeo.disability_status"),
        ("Have you ever worked for this company before?", None, "deterministic", "affiliations.previously_employed"),
        ("Have you previously been employed by us?", None, "deterministic", "affiliations.previously_employed"),
        ("Are you a former employee?", None, "deterministic", "affiliations.previously_employed"),
        ("Are you affiliated with any government official or office?", None, "deterministic", "affiliations.government_affiliation"),
        ("Do you have any external business interests?", None, "deterministic", "affiliations.external_business"),
        ("Do you have a relative employed here?", None, "deterministic", "affiliations.related_to_employee"),
        # citizenship trio — order-sensitive
        ("Are you a US citizen?", None, "deterministic", "work_authorization.us_citizen"),
        ("Are you a US citizen or permanent resident?", None, "deterministic", "work_authorization.authorized_us"),
        ("What is your visa status?", None, "deterministic", "work_authorization.status"),
        # employment / education
        ("Current Employer", None, "deterministic", "employment.current_employer"),
        ("Current Job Title", None, "deterministic", "employment.current_title"),
        ("What university did you attend?", None, "deterministic", "education.school"),
        ("Highest degree earned", None, "deterministic", "education.degree"),
        ("Field of study", None, "deterministic", "education.major"),
        ("GPA", None, "deterministic", "education.gpa"),
        ("Expected graduation date", None, "deterministic", "education.graduation_date"),
        # preferences
        ("How did you hear about us?", None, "deterministic", "preferences.referral_source"),
        ("Are you open to remote or hybrid work?", None, "deterministic", "preferences.work_arrangement"),
        ("Preferred work location", None, "deterministic", "preferences.locations"),
        ("City", None, "deterministic", "identity.address.city"),
        # attestations
        ("Are you at least 18 years of age?", None, "deterministic", "attestations.age_18_or_older"),
        ("Do you consent to a background check?", None, "deterministic", "attestations.background_check_consent"),
        ("Do you hold an active security clearance?", None, "deterministic", "attestations.security_clearance"),
        ("Are you bound by a non-compete agreement?", None, "deterministic", "attestations.non_compete"),
        ("Do you require any accommodations for the interview?", None, "deterministic", "attestations.interview_accommodations_needed"),
        ("Have you ever been convicted of a felony?", None, "deterministic", "attestations.criminal_history"),
        ("What is your favorite programming language?", None, None, None),
        # --- 2026-07-12 audit fixes: clashes (wrong rule won) and misses ---
        # sponsorship polarity: inverted/compound phrasings answer Yes, standard stays No
        ("Are you able to work without visa sponsorship?", None, "deterministic", "work_authorization.authorized_us"),
        ("Can you work in the US without sponsorship?", None, "deterministic", "work_authorization.authorized_us"),
        ("Are you legally authorized to work in the United States and will not require sponsorship in the future?", None, "deterministic", "work_authorization.authorized_us"),
        ("Will you require sponsorship for employment visa status?", None, "deterministic", "work_authorization.require_sponsorship"),
        # citizenship: country/nationality = the citizenship country, interrogative/bare
        # noun = the status string
        ("Country of citizenship", None, "deterministic", "work_authorization.citizenship_country"),
        ("Nationality", None, "deterministic", "work_authorization.citizenship_country"),
        ("What is your citizenship?", None, "deterministic", "work_authorization.status"),
        # \bstate\b anchoring: the verb "state" and shadowed later rules
        ("Please state your reasons for applying for this position", None, None, None),
        ("Are you affiliated with any state-owned enterprise?", None, "deterministic", "affiliations.government_affiliation"),
        ("Are you affiliated with any government institution?", None, "deterministic", "affiliations.government_affiliation"),
        ("State", None, "deterministic", "identity.address.state_full"),
        ("Which U.S. State or Canadian Province do you reside in?", None, "deterministic", "identity.address.state_full"),
        # EEO: standalone Hispanic/Latino Y/N fills instead of escalating
        ("Are you Hispanic or Latino?", None, "deterministic", "eeo.hispanic_latino"),
        # substring traps: cell/zip word-bounded, referral rule hoisted above them
        ("Miscellaneous", None, None, None),
        ("How did you hear about Zipline?", None, "deterministic", "preferences.referral_source"),
        # adverb between "you" and the verb must not break the referral rule (Affirm form)
        ("How did you first learn about Affirm as an employer?", None, "deterministic", "preferences.referral_source"),
        ("Desired hourly rate", None, "always_ask", None),
        # "Preferred name" is the given name (Parth), not a location preference
        ("Preferred Name", None, "deterministic", "identity.first_name"),
        ("Preferred work location", None, "deterministic", "preferences.locations"),
        # work-history fields and essay phrasings must NOT autofill profile values
        ("List your previous employers", None, None, None),
        ("Why do you want to leave your current employer?", None, None, None),
        ("What do you enjoy most about your current role?", None, None, None),
        ("What has been your major professional achievement?", None, None, None),
        ("Have you ever been disciplined by a regulatory authority?", None, None, None),
        ("What is your major?", None, "deterministic", "education.major"),
        # decided answers + location routing + graduation gap
        ("Do you require relocation assistance?", None, "deterministic", "preferences.relocation_assistance_needed"),
        ("Preferred City", None, "deterministic", "preferences.locations"),
        ("City, State", None, "deterministic", "identity.location_display"),
        ("When did you graduate?", None, "deterministic", "education.graduation_date"),
        # --- the "what excites you" family (added 2026-07-31) -------------------------
        # Every earlier draftable rule keys on "why" + a want-verb, so these all fell
        # through to bucket None and needed a manual "Draft it" tap on every board. The
        # KEY matters as much as the bucket: answer_bank.yaml has a why_role row, and the
        # bank beats Claude, so landing on why_role would serve one canned essay to every
        # company. These three keys are deliberately not in the bank.
        ("What excites you about this role?", None, "draftable", "role_excitement"),
        ("What interests you about this position?", None, "draftable", "role_excitement"),
        ("Why are you excited about this opportunity?", None, "draftable", "role_excitement"),
        # asks both; the role is the real question
        ("Tell us a bit about why you're applying to work here. What excited you about this role?",
         None, "draftable", "role_excitement"),
        ("What excites you about joining us?", None, "draftable", "company_excitement"),
        ("What drew you to Zipline?", None, "draftable", "company_excitement"),
        ("Our purpose is economic empowerment. Briefly tell us what that purpose means to you.",
         None, "draftable", "values_mission"),
        # the older "why" phrasings must still win their own keys
        ("Why do you want to work here?", None, "draftable", "why_company"),
        ("Why this role?", None, "draftable", "why_role"),
        # an excitement verb with no role/company noun is not this family
        ("What excites you about the compensation package?", None, None, None),
        # --- OBSERVED PHRASINGS block (added 2026-08-12 from logs/fill_reports.jsonl) ---
        # Every one of these was surfaced as a question on a live board while the answer
        # sat in profile.json. Append a case here whenever the rule book grows.
        ("What is your most recent company?", None, "deterministic", "employment.current_employer"),
        ("What is your most recent job title?", None, "deterministic", "employment.current_title"),
        ("What country do you currently reside in?", None, "deterministic", "identity.address.country"),
        ("What country are you based in?", None, "deterministic", "identity.address.country"),
        ("Do you live in the US or plan to live in the US by the time you start working?",
         None, "deterministic", "identity.based_in_us"),
        ("Are you based in a US equivalent timezone?", None, "deterministic", "identity.based_in_us"),
        ("What is your military status?", None, "deterministic", "eeo.veteran_status"),
        ("Have you ever worked for Robinhood as an employee, intern or contractor?",
         None, "deterministic", "affiliations.previously_employed"),
        ("How did you discover us?", None, "deterministic", "preferences.referral_source"),
        ("Which locations are you open to working from? (Select all that apply)",
         None, "deterministic", "preferences.locations"),
        ("What is your preferred office location?", None, "deterministic", "preferences.locations"),
        ("Do you consent to receiving text messages?", None, "deterministic", "attestations.sms_consent"),
        # THE REGRESSION THESE MUST NOT CAUSE: the employer/title rules end-anchor away a
        # trailing "?" precisely so question-sentences aren't answered with a value. The
        # new interrogative rules keep that property — Babel Street's competitor question
        # is a live Yes/No that must never be filled with an employer name.
        ("If yes, is Babel Street considered a competitor to your most recent employer?", None, None, None),
        ("Describe your most recent role and responsibilities", None, None, None),
        # An essay ABOUT the current job must reach the drafter, not be answered with the
        # employer's name. It lands in draftable (checked after deterministic), which is
        # proof the new interrogative rule declined it rather than that no rule saw it.
        ("Why do you want to leave your most recent company?", None, "draftable", "why_company"),
    ]
    fails = 0
    for field, company, want_bucket, want_key in cases:
        r = classify(field, company, profile, tax)
        # deterministic rules carry value_key, draftable ones answer_key — a case asserts
        # whichever its bucket uses, and None still means "don't care".
        got_key = r.get("value_key") or r.get("answer_key")
        ok = r.get("bucket") == want_bucket and (want_key is None or got_key == want_key)
        if not ok:
            fails += 1
            print(f"FAIL: {field!r} -> {r.get('bucket')}/{got_key} "
                  f"(wanted {want_bucket}/{want_key})")
    # boolean rendering
    if render(True) != "Yes" or render(False) != "No":
        fails += 1; print("FAIL: boolean render")

    # --- alternates: the "if the first answer doesn't fit, try the next" chain ---------
    # Each case below is a question whose FIRST answer was flagged on a live board because
    # it matched none of the control's options. The fill engine walks this list in order.
    def alts(field):
        return classify(field, None, profile, tax).get("alternates") or []

    # rule-level: a different profile key entirely (the widget, not the wording, differs).
    # Asserted against the profile's OWN value, never a literal — that keeps a personal
    # answer out of this shareable file and makes the check hold for any user's profile.
    if render(dig(profile, "eeo.pronouns")) not in alts("What gender pronouns do you prefer?"):
        fails += 1; print("FAIL: pronouns not offered as an alternate to gender")
    if render(dig(profile, "attestations.sms_consent")) not in alts("Phone Number"):
        fails += 1; print("FAIL: sms_consent not offered as an alternate to phone")
    if "Yes" not in alts("Are you willing to work from the office(s) listed on the job description?"):
        fails += 1; print("FAIL: in-office acknowledgment not offered for a Yes/No office question")
    # ORDER IS THE MEANING of the list: the profile's own value is never in it (it is
    # tried first by the caller), and rule alternates precede answer_alternates.
    g = classify("Gender", None, profile, tax)
    if g.get("rendered") in g.get("alternates", []):
        fails += 1; print("FAIL: the primary value must not repeat inside alternates")
    # profile-level: same answer, other wordings — and a literal that is not a profile path
    if "I've already graduated" not in alts("Expected graduation date"):
        fails += 1; print("FAIL: graduation-date literal alternate missing")
    loc = alts("What is your preferred office location?")
    if loc[:1] != ["San Francisco"] or "New York" not in loc:
        fails += 1; print(f"FAIL: ranked location alternates wrong -> {loc[:3]}")
    # a rule with no alternates anywhere resolves to an empty list, never None
    if classify("First Name", None, profile, tax).get("alternates") != []:
        fails += 1; print("FAIL: a rule with no alternates should yield []")
    # an unset/blank entry is dropped rather than offered as an answer
    fake_profile = {"a": {"b": FILL_ME}, "answer_alternates": {"a.b": ["", FILL_ME, "Real"]}}
    if resolve_alternates(fake_profile, {"alternates": ["a.b"]}, "a.b") != ["Real"]:
        fails += 1; print("FAIL: blank/FILL_ME alternates must be dropped")
    # override: force a company's "why us" to always_ask
    tax2 = json.loads(json.dumps(tax))
    tax2["overrides"] = {"Jane Street": {"always_ask": [{"pattern": "why .*(work|join)"}]}}
    r = classify("Why do you want to work at us?", "Jane Street", profile, tax2)
    if r.get("bucket") != "always_ask":
        fails += 1; print(f"FAIL: override -> {r.get('bucket')} (wanted always_ask)")
    # resume pick
    if pick_resume("Quantitative Researcher")["tag"] != "quant":
        fails += 1; print("FAIL: resume pick quant")
    if pick_resume("Machine Learning Engineer")["tag"] != "ai":
        fails += 1; print("FAIL: resume pick ai")
    if pick_resume("Barista")["tag"] != profile["default_resume_tag"]:
        fails += 1; print("FAIL: resume default fallback")

    # Outgoing filename: built from identity, never from the PDF's own name.
    named = dict(profile, identity=dict(profile.get("identity") or {},
                                        first_name="Ada", last_name="Lovelace"))
    quant = next(t for t in named["resume_tracks"] if t["tag"] == "quant")
    if resume_filename(dict(quant, current_file="whatever_v9.pdf"), named) != "Ada_Lovelace_q.pdf":
        fails += 1; print("FAIL: resume filename ignores the source name")
    one_track = dict(named, resume_tracks=[quant])
    if resume_filename(quant, one_track) != "Ada_Lovelace.pdf":
        fails += 1; print("FAIL: single track carries no suffix")
    unnamed = dict(named, identity={"first_name": FILL_ME, "last_name": FILL_ME})
    if resume_filename(dict(quant, current_file="src.pdf"), unnamed) != "src.pdf":
        fails += 1; print("FAIL: unset identity falls back to the source name")
    if len({resume_filename(t, named) for t in named["resume_tracks"]}) != len(named["resume_tracks"]):
        fails += 1; print("FAIL: two tracks share one outgoing filename")

    holes = find_fillme(profile)
    print(f"\n{len(cases)+5} checks, {fails} failure(s). "
          f"profile FILL_ME placeholders: {len(holes)}")
    return 1 if fails else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("classify"); p.add_argument("--field", required=True); p.add_argument("--company")
    p = sub.add_parser("get");      p.add_argument("--key", required=True)
    p = sub.add_parser("resume");   p.add_argument("--field", required=True); p.add_argument("--jd")
    p = sub.add_parser("checkfill")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if not a.cmd:
        ap.error("need a subcommand or --selftest")
    {"classify": cmd_classify, "get": cmd_get, "resume": cmd_resume, "checkfill": cmd_checkfill}[a.cmd](a)
