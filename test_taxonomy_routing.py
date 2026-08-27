#!/usr/bin/env python3
"""A label must reach the rule that reads the QUESTION, not one that reads a word in it.

Rules inside a bucket are tested in file order and the first match wins, so a generic
substring rule placed above a specific one silently answers questions it does not
understand. Live on Abnormal Security: "Are you currently an employee or contractor at
Abnormal? (if Yes, please add your Abnormal email in the email section of the application
page)" is a Yes/No dropdown, but the parenthetical carries the word "email", the generic
`e-?mail` rule sat above every affiliations rule, and the fill offered Parth's address to a
list containing only "Yes" and "No".

Ordering is not visible from any single rule, so this file pins the pairs that collide.
Each case is a real label with the value_key it must resolve to.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from core import profile as P  # noqa: E402

ok = fail = 0


def check(name, cond):
    global ok, fail
    print(("PASS " if cond else "FAIL ") + name)
    ok, fail = ok + (1 if cond else 0), fail + (0 if cond else 1)


prof = P.load_profile()
tax = P.load_taxonomy()

# (label, expected value_key or None for "no rule should match")
CASES = [
    # THE REGRESSION: the question is about employment, the parenthetical mentions email.
    ("Are you currently an employee or contractor at Abnormal?\n"
     "(if Yes, please add your Abnormal email in the email section of the application page)",
     "affiliations.current_employee"),
    ("Are you a current employee of Stripe?", "affiliations.current_employee"),
    ("Are you currently employed by Ramp?", "affiliations.current_employee"),
    ("Are you an employee of the company?", "affiliations.current_employee"),
    ("Are you a contractor for us?", "affiliations.current_employee"),
    ("Are you presently an employee at Databricks?", "affiliations.current_employee"),
    ("Are you an intern at this company?", "affiliations.current_employee"),
    # ...while a plain email field still reaches identity.email.
    ("Email", "identity.email"),
    ("Email*", "identity.email"),
    ("Preferred Email Address", "identity.email"),
    # "Are you currently employed?" asks about his job somewhere else. Its answer is not No,
    # so the rule above must not claim it: it escalates instead.
    ("Are you currently employed?", None),
    ("What is your current employer?", None),
    # The questions that open with the same words and ask something else. The
    # current_employee rule sits above the affiliations block to clear the email rule, so
    # these prove it still hands each of them back to its own rule. Every key involved is
    # false today, so a wrong match would give the right answer by luck; the point is that
    # flipping one key later changes only the questions it names.
    ("Are you an employee referral?", None),
    ("Are you an employee referral or applying directly?", None),
    ("Were you referred by an employee?", None),
    ("Are you related to a current employee?", "affiliations.related_to_employee"),
    ("Are you currently an employee of a government agency?",
     "affiliations.government_affiliation"),
    ("Are you currently an employee or official of a state-owned entity?",
     "affiliations.government_affiliation"),
    ("Are you employed by a customer, partner, or competitor of ours?",
     "affiliations.external_business"),
    # Prior employment stays its own question.
    ("Have you previously worked at Abnormal", "affiliations.previously_employed"),
    ("Have you ever worked for us as a contractor?", "affiliations.previously_employed"),
    # Neighbors that merely open with "Are you", kept so a future widening of the rule
    # cannot quietly swallow them.
    ("Are you legally authorized to work in the US?", "work_authorization.authorized_us"),
    ("Are you willing to relocate?", "work_authorization.relocation"),
    ("Are you 18 years of age or older?", "attestations.age_18_or_older"),
    ("Are you currently a student?", None),
    # Greenhouse asks the Hispanic/Latino gate before it draws the race dropdown, and the
    # generic race rule matches the word "hispanic" too. The gate must win.
    ("Are you Hispanic/Latino?", "eeo.hispanic_latino"),
    ("Please identify your race", "eeo.race_ethnicity"),
]

for label, expected in CASES:
    got = P.classify(label, None, prof, tax).get("value_key")
    check(f"{label.splitlines()[0][:56]!r} -> {expected}", got == expected)

print(f"\ntaxonomy routing: {ok}/{ok + fail} checks passed")
sys.exit(1 if fail else 0)
