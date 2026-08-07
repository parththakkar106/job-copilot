"""Checks for scripts/pii_guard.py.

The profile below is entirely invented. That is not just hygiene: a test that hardcoded the
author's real details would put them in the repo the guard exists to keep them out of, and
the guard would then flag its own test file.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pii_guard as G                                          # noqa: E402

FAKE = {
    "identity": {
        "first_name": "Riley", "last_name": "Quintero",
        "email": "riley.quintero@fastmail.test",
        # 555 on purpose: reserved fiction, so the shape rules ignore it and this file does
        # not trip the very check it is testing. The DERIVED pattern is a literal, so it
        # still matches, which is what these cases exercise.
        "phone": "(555) 867-5309",
        "address": {"line1": "88 Wexford Row", "city": "Springfield", "state": "IL",
                    "state_full": "Illinois", "zip": "62704", "country": "United States"},
        "location_display": "Springfield, Illinois, United States",
    },
    "links": {"linkedin": "https://linkedin.com/in/riley-quintero-42",
              "github": "FILL_ME", "portfolio": "", "twitter": ""},
    "work_authorization": {"citizenship_country": "Uruguay", "status": "Green Card"},
    "employment": {"current_employer": "Initech", "current_title": "Analyst"},
    "education": {"school": "Miskatonic Institute of Technology", "degree": "B.E."},
    "eeo": {"gender": "Woman", "sexual_orientation": "Heterosexual",
            "veteran_status": "FILL_ME", "transgender": "No", "demographic_consent": True},
}


def hits(patterns, line: str) -> list[str]:
    return [lbl for _, lbl in G.scan([("f.py", 1, line)], patterns)] or []


def main() -> int:
    checks = 0
    P = G.derive_patterns(FAKE)

    def flags(line: str) -> bool:
        return bool(G.scan([("f.py", 1, line)], P))

    # --- the phone, however it was punctuated -------------------------------------------
    for form in ("(555) 867-5309", "555-867-5309", "555.867.5309", "5558675309",
                 "+1 555 867 5309"):
        assert flags(f"const PHONE = '{form}';"), form
        checks += 1
    # A partial scrub is the dangerous case: area code + exchange with the last four
    # digits changed still identifies the number, and a literal search would miss it.
    assert flags("// fixture: 555-867-0000"), "partial phone should still flag"
    checks += 1

    # --- the rest of the derived set ----------------------------------------------------
    for line, why in (
        ("email: riley.quintero@fastmail.test", "email"),
        ("// he lives in Springfield so the picker defaults there", "city"),
        ("zip = '62704'", "postal code"),
        ("state_full: Illinois", "state"),
        ('{"state": "IL"}', "quoted state abbreviation"),
        ("# 88 Wexford Row is the address the form expects", "street"),
        ("employer = 'Initech'", "employer"),
        ("school: Miskatonic Institute of Technology", "school in full"),
        ("// the Miskatonic typeahead returns nothing", "distinctive school word"),
        ("citizenship: Uruguay", "citizenship beside its question"),
        ("status = 'Green Card'", "work authorization stands alone"),
        ("linkedin.com/in/riley-quintero-42", "profile link"),
        ("gender: Woman", "EEO answer beside its question"),
        ("sexual orientation -> Heterosexual", "underscored key matches spaced text"),
        ("Heterosexual is the sexual_orientation default", "answer before question"),
    ):
        assert flags(line), why
        checks += 1

    # --- what must NOT flag -------------------------------------------------------------
    for line, why in (
        ("# Author: Riley Quintero", "the author's name is deliberately allowed"),
        ("school lists use an Institute of Technology suffix", "generic words only"),
        ("call 555-123-4567 for the demo", "555 is reserved fiction"),
        ("contact user@example.com", "example.com is a documentation domain"),
        ("job-bot@users.noreply.github.com", "the Action's commit identity"),
        ("const FNV_PRIME = 16777619;", "a bare constant is not a phone number"),
        ("veteran_status: FILL_ME", "unset fields contribute no pattern"),
        ("github link is FILL_ME", "unset link contributes no pattern"),
        ("transgender: No", "a two-letter EEO answer is too common to hunt"),
        ("if (state === current) {", "a bare identifier is not a state abbreviation"),
        # The false positives a real audit run produced. Every one of these is ordinary
        # prose that happens to contain an EEO answer or a country name, and flagging them
        # is how a check earns itself a permanent --no-verify.
        ("# the parsed value goes straight into the prompt", "prose containing an answer"),
        ("// walk the list straight through, no sorting", "prose containing an answer"),
        ("# 03:00 UTC = 08:30 IST, and Uruguay has no DST", "a country in a timezone note"),
        ("  - Uruguay        # excluded by the US-only filter", "a country in a filter"),
    ):
        assert not flags(line), why
        checks += 1
    # Known and accepted: a quoted two-letter state flags wherever it appears, including
    # `state === 'IL'`. Kept deliberately â€” the quoting is what makes it a value rather
    # than a coincidence, and the alternative is missing the real one in a fixture.
    assert flags("if (state === 'IL') {"), "quoted state abbreviation flags by design"
    checks += 1

    # --- a line listing mutually exclusive answers is a mapping, not a disclosure -------
    for line, why in (
        ('["Gender", "Woman", "Man"],', "a canonicalisation fixture"),
        ('// "Woman" matches an ATS that only offers "Female"', "a comment explaining one"),
        ("pronouns: He/Him, She/Her, They/Them", "an option list"),
        ('ok(diff([{v:"Heterosexual"}],[{v:"Straight"}]) === 0)', "an equivalence pair"),
        ("gender: Woman | Man | Non-binary", "a menu"),
    ):
        assert not flags(line), why
        checks += 1
    # ...but the suppression must not swallow a real answer sitting on its own.
    for line, why in (
        ("gender: Woman", "a lone answer is still a disclosure"),
        ("sexual orientation: Heterosexual", "a lone answer is still a disclosure"),
        # A single paired pronoun is an answer; the bare words are not enough to suppress.
        ("they say the gender is Woman", "bare 'they' must not count as an alternative"),
    ):
        assert flags(line), why
        checks += 1
    assert G.lists_alternatives("Male vs Female"), "two alternatives detected"
    checks += 1
    assert not G.lists_alternatives("value: Female"), "one alternative is not a list"
    checks += 1
    assert not G.lists_alternatives("the female candidate"), \
        "'male' must not match inside 'female'"
    checks += 1
    # Suppression is scoped to EEO: a phone number on an option-list line still flags.
    assert flags("Male or Female, call 555-867-5309"), \
        "suppression must not extend past the EEO checks"
    checks += 1

    # --- the checks a DERIVED set cannot produce ----------------------------------------
    # These come from the retired publish-time scanner, which hunted the disclosure rather
    # than this author's copy of it. The distinguishing case: an EEO answer that is NOT the
    # one in the profile — someone else's, or one since changed — has no literal to derive
    # from, so only the shape check can see it.
    for line, why in (
        ("gender: Man", "an EEO answer that is not the profile's own"),
        ("gender identity: Non-binary", "an answer absent from the profile"),
        ("sexual orientation: Gay", "an orientation absent from the profile"),
    ):
        assert G.scan([("f.py", 1, line)], G.SHAPE_PATTERNS), why
        checks += 1
    # ...and it must still yield to the option-list rule, or every canon() mapping table in
    # the extension becomes a permanent finding.
    for line, why in (
        ('["Gender", "Man", "Woman"],', "a mapping fixture is not a disclosure"),
        ("pronouns: He/Him, She/Her", "an option list is not a disclosure"),
    ):
        assert not G.scan([("f.py", 1, line)], G.SHAPE_PATTERNS), why
        checks += 1

    # An application outcome is a company name beside a DATE or a live record. The boundary
    # is deliberate: an ATS behaviour note naming a company is the most useful content in
    # the repo and must survive, so widening this to any outcome verb is a bad trade.
    for line, why in (
        ("// verified live on Northwind 2026-07-18", "a dated live record"),
        ("# skipped a live Northwind record during a check", "a named live record"),
        ("// the real Northwind run never confirmed", "a named run outcome"),
    ):
        assert G.scan([("f.py", 1, line)], G.SHAPE_PATTERNS), why
        checks += 1
    for line, why in (
        ("// verified live on Northwind: the first click is swallowed", "behaviour, undated"),
        ('assert len(t) < 4096, "would be rejected by Telegram"', "an API limit, not an ATS"),
        ("// Northwind embeds the form in an iframe", "a plain technical note"),
    ):
        assert not G.scan([("f.py", 1, line)], G.SHAPE_PATTERNS), why
        checks += 1

    # The credential itself is hunted; the level and the major are not, being shared by
    # millions. Not hunting the credential let a comment naming one reach the repo.
    assert flags("// e.g. the free-text credential 'B.E.'"), "the degree is hunted"
    checks += 1
    for line, why in (
        ("degree_level: Bachelors", "a level identifies nobody"),
        ("major: Computer Science", "a major identifies nobody"),
    ):
        assert not flags(line), why
        checks += 1

    # --- the same rule for the home state: a lookup table is not a disclosure -----------
    # This is why core/scraper.py can carry all 50 states without blocking every future
    # edit to the file. Threshold is 3, so a two-state sentence is still a disclosure.
    for line, why in (
        ('"alaska", "arizona", "georgia", "illinois", "texas"', "50-state name table"),
        ('("AL", "AK", "CA", "IL", "NY", "TX")', "50-state abbreviation table"),
    ):
        assert not flags(line), f"{why} must not flag"
        checks += 1
    for line, why in (
        ("home_state = 'Illinois'", "a single state name is still the answer"),
        ('state: "IL"', "a single quoted abbreviation is still the answer"),
        ("moved from Illinois to Texas", "two states is a sentence, not a table"),
    ):
        assert flags(line), f"{why}: {line}"
        checks += 1
    # ...and suppression is scoped to the state check, like the EEO one.
    assert flags('"AL", "AK", "CA", "IL", "NY" â€” call 555-867-5309'), \
        "suppression must not extend past the state check"
    checks += 1

    # --- an unfilled template must produce nothing personal -----------------------------
    blank = {"identity": {"email": "FILL_ME", "phone": "FILL_ME",
                          "address": {k: "FILL_ME" for k in
                                      ("line1", "city", "state", "state_full", "zip")}},
             "links": {"linkedin": "FILL_ME"}, "employment": {"current_employer": "FILL_ME"},
             "education": {"school": "FILL_ME"},
             "eeo": {"gender": "FILL_ME"}}
    assert G.derive_patterns(blank) == [], "FILL_ME must never become a pattern"
    checks += 1
    assert not G.scan([("f.py", 1, "anything at all FILL_ME")],
                      G.derive_patterns(blank)), "empty pattern set matches nothing"
    checks += 1

    # --- shape checks stand alone, with no profile --------------------------------------
    # Assembled at runtime, never written out whole. The shape rules exist to catch a real
    # number belonging to somebody who is not you, so testing them needs a string that is
    # not reserved fiction â€” and a file that contains one would fail its own check.
    stranger_phone = "312-" + "224-9087"
    stranger_email = "dana.whitfield" + "@" + "northwind.co"
    assert G.scan([("f.py", 1, f"reach me at {stranger_phone}")], G.SHAPE_PATTERNS), \
        "a stranger's phone in a comment should flag even with no profile"
    checks += 1
    assert not G.scan([("f.py", 1, "id 555-123-4567")], G.SHAPE_PATTERNS), "555 exempt"
    checks += 1
    assert G.scan([("f.py", 1, f"cc: {stranger_email}")], G.SHAPE_PATTERNS), \
        "a third party's address should flag even with no profile"
    checks += 1
    for dummy in ('{"value": "a@b.com"}', "user@example.com", "x@y.org",
                  "bot@users.noreply.github.com", "seed@fixtures.test",
                  "svc@thing.invalid"):
        assert not G.scan([("f.py", 1, dummy)], G.SHAPE_PATTERNS), dummy
        checks += 1

    # --- allowlist ----------------------------------------------------------------------
    for p in ("data/profile.json", "data/companies.yaml", "data/answer_bank.yaml",
              "data/pii_extra.txt", "CLAUDE.md",
              # this file: its job is to hold the shapes the guard hunts, and its
              # fixtures are invented, so it is the one exemption that is not private
              "scripts/test_pii_guard.py",
              "docs/code_audit_2026-07-18.md", "docs/taxonomy_audit_2026-07-12.md"):
        assert G.ALLOW.match(p), p
        checks += 1
    for p in ("extension/fill.js", "data/taxonomy.yaml", "docs/notes.md",
              "docs/autonomous_run.md", "scripts/run_daily.ps1",
              "data/profile.example.json",
              # the guard holds none of what it hunts, and is scanned to keep it so
              "scripts/pii_guard.py"):
        assert not G.ALLOW.match(p), f"{p} must be scanned"
        checks += 1

    # --- one finding per line, so a noisy line does not print ten times ------------------
    line = "riley.quintero@fastmail.test lives in Springfield IL 62704"
    assert len(G.scan([("f.py", 7, line)], P)) == 1, "one finding per line"
    checks += 1
    assert G.scan([("f.py", 7, line)], P)[0].startswith("f.py:7  "), "path:line prefix"
    checks += 1

    # --- diff parsing: added lines only, with their NEW line numbers ---------------------
    diff = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -3,0 +4,2 @@\n"
        "+first added line\n"
        "+second added line\n"
        "@@ -20 +30 @@\n"
        "-removed line\n"
        "+replacement line\n"
    )
    got = G._parse_diff(diff)
    assert got == [("x.py", 4, "first added line"), ("x.py", 5, "second added line"),
                   ("x.py", 30, "replacement line")], got
    checks += 1
    assert all("removed" not in t for _, _, t in got), "deletions are not scanned"
    checks += 1
    assert G._parse_diff("+++ b/data/profile.json\n@@ -0,0 +1 @@\n+secret\n") == [], \
        "allowlisted paths are dropped during parsing"
    checks += 1

    # --- extra-pattern file: comments, labels, and a bad regex --------------------------
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "pii_extra.txt").write_text(
            "# a comment\n"
            "\n"
            r"\b409\b[^\n]{0,30}compan" + "\ttarget-list size\n"
            "unlabelled-pattern\n"
            "[unclosed\n",
            encoding="utf-8")
        orig = G.paths.DATA
        try:
            G.paths.DATA = d
            extra = G.load_extra()
        finally:
            G.paths.DATA = orig
    labels = [lbl for _, lbl in extra]
    assert len(extra) == 2, extra                       # the bad regex is skipped
    checks += 1
    assert "target-list size" in labels, labels
    checks += 1
    assert "private detail" in labels, "an unlabelled pattern gets a default label"
    checks += 1
    assert G.scan([("d.md", 1, "we track 409 companies")], extra), "extra pattern fires"
    checks += 1

    # --- target-list statistics, derived from the real companies file -------------------
    # The file itself is withheld, but its size and tiering leak in prose, and no pattern
    # built from profile.json can know those numbers. Reading them at runtime keeps the
    # guard free of hardcoded facts and works on a stranger's own list.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "companies.yaml").write_text(
            "companies:\n" + "".join(
                f"  - {{slug: c{i}, tier: {'dream' if i < 4 else 'pipeline'}}}\n"
                for i in range(11)),
            encoding="utf-8")
        orig = G.paths.COMPANIES_WRITE
        try:
            G.paths.COMPANIES_WRITE = d / "companies.yaml"
            stats = G.derive_company_stats()
        finally:
            G.paths.COMPANIES_WRITE = orig
    assert stats, "a real companies file yields patterns"
    checks += 1
    # 11 rows: 4 dream, 7 pipeline.
    assert G.scan([("d.md", 1, "we track 11 companies across three ATSs")], stats), \
        "the total flags beside a word that makes it a claim about the list"
    checks += 1
    assert G.scan([("d.md", 1, "4 of them are dream targets")], stats), "a tier count flags"
    checks += 1
    assert G.scan([("d.md", 1, "7 in the pipeline tier")], stats), "the other tier flags"
    checks += 1
    # A bare number is every small constant in the codebase; only the claim is hunted.
    assert not G.scan([("d.md", 1, "retry up to 11 times before giving up")], stats), \
        "a bare number must not flag"
    checks += 1
    # No companies file at all contributes nothing rather than blocking a commit.
    orig = G.paths.COMPANIES_WRITE
    try:
        G.paths.COMPANIES_WRITE = Path(td) / "gone.yaml"
        assert G.derive_company_stats() == [], "a missing companies file is not fatal"
    finally:
        G.paths.COMPANIES_WRITE = orig
    checks += 1

    # --- outreach contacts: SOMEBODY ELSE's data ----------------------------------------
    # The only category this repo handles that does not belong to its author, and the only
    # one nobody would think to check. Two halves: a shape rule for the profile URL, and a
    # derived rule for the name (a name has no shape, and unlike everything else here these
    # names are not in profile.json to be derived from).
    shape = list(G.SHAPE_PATTERNS)
    assert G.scan([("f.py", 1, 'url = "https://www.linkedin.com/in/dana-whitfield-4c1"')],
                  shape), "a third-party profile URL must flag"
    checks += 1
    assert G.scan([("f.py", 1, "# via ca.linkedin.com/in/priya-n-8a2aa882")], shape), \
        "the country subdomains are the same profile"
    checks += 1
    # `example-` is reserved for invented fixtures, exactly as 555 and example.com are.
    # core/contacts.py's selftest needs realistic profile URLs to test its parser against,
    # and mangling them until they stop looking real would stop them testing anything.
    assert not G.scan([("f.py", 1, '"link": "https://www.linkedin.com/in/example-dana"')],
                      shape), "the reserved fixture prefix must not flag"
    checks += 1
    # Prose explaining the URL format is not a person: a real slug is multi-part.
    assert not G.scan([("f.py", 1, "profiles live under linkedin.com/in/<slug>")], shape), \
        "a bare format example must not flag"
    checks += 1

    with tempfile.TemporaryDirectory() as td2:
        op = Path(td2) / "outreach.json"
        orig_out = G.paths.OUTREACH
        try:
            G.paths.OUTREACH = op
            assert G.derive_contact_patterns() == [], "no outreach file is not fatal"
            checks += 1
            op.write_text(json.dumps([
                {"name": "Dagny Halloran",
                 "profile_url": "https://www.linkedin.com/in/dagny-halloran-77"},
                {"name": "Bo", "profile_url": ""},          # one short part: too generic
            ]), encoding="utf-8")
            C = G.derive_contact_patterns()
            assert G.scan([("f.py", 1, "# verified against Dagny Halloran's profile")], C), \
                "a real contact's name must flag"
            checks += 1
            assert G.scan([("f.py", 1, "slug = 'dagny-halloran-77'")], C), \
                "the slug alone must flag"
            checks += 1
            assert not G.scan([("f.py", 1, "let bo = 1;")], C), \
                "a one-part name is too generic to hunt"
            checks += 1
            op.write_text("{ not json", encoding="utf-8")
            assert G.derive_contact_patterns() == [], "a corrupt outreach file is not fatal"
            checks += 1
        finally:
            G.paths.OUTREACH = orig_out

    print(f"pii_guard: {checks}/{checks} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
