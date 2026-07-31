"""Checks for scripts/pii_guard.py.

The profile below is entirely invented. That is not just hygiene: a test that hardcoded the
author's real details would put them in the repo the guard exists to keep them out of, and
the guard would then flag its own test file.
"""
from __future__ import annotations

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
    # `state === 'IL'`. Kept deliberately — the quoting is what makes it a value rather
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
    assert flags('"AL", "AK", "CA", "IL", "NY" — call 555-867-5309'), \
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
    # not reserved fiction — and a file that contains one would fail its own check.
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
              "data/pii_extra.txt", "CLAUDE.md", "scripts/publish.py",
              "docs/code_audit_2026-07-18.md", "docs/taxonomy_audit_2026-07-12.md"):
        assert G.ALLOW.match(p), p
        checks += 1
    for p in ("extension/fill.js", "data/taxonomy.yaml", "docs/notes.md",
              "docs/autonomous_run.md", "scripts/run_daily.ps1",
              "data/profile.example.json",
              # the guard holds none of what it hunts, and is scanned to keep it so
              "scripts/pii_guard.py", "scripts/test_pii_guard.py"):
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
            r"\b114\b[^\n]{0,30}compan" + "\ttarget-list size\n"
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
    assert G.scan([("d.md", 1, "we track 114 companies")], extra), "extra pattern fires"
    checks += 1

    print(f"pii_guard: {checks}/{checks} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
