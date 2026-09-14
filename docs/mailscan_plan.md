# Mail scan — derive application outcomes from the inbox (implementation plan)

Status: **PLAN ONLY.** Nothing is built. The rules below come from a dry run over one
month of real mail, described in "What the sample showed".

This feature answers a question the pipeline cannot: what happened *after* you applied.
`applied.json` records that you sent an application. It never records the reply. The mail
scan reads your inbox, classifies each message as a confirmation, a rejection, or an
interview step, and writes those events to `outcomes.json`.

Companion to `dashboard_spec.md`, which owns the pile model. This document does not change
that model. See "The independence rule" for why.

## The independence rule

**The mail scan never moves a pile.** It reads `applied.json` and writes `outcomes.json`.
It issues no pile transition, and it edits no pending record.

The reason is that the pipeline and the inbox describe different things. A pile records
your decision about a role: you queued it, you approved it, you were not interested. An
outcome records the employer's decision about you. Today `MOVES.submitted` offers
`{ target: 'blocked', label: 'Mark rejected' }`, so `blocked` already carries both
meanings. If the scan could write piles, one misclassified message would archive a live
application. Keeping the two stores separate means a wrong classification costs you a
wrong row on a dashboard, never a lost application.

`mailcode.py` constrains itself the same way, for a related reason.

## The four statuses

| Status | Where it comes from |
| --- | --- |
| `applied` | A confirmation message, or an `applied.json` row. |
| `rejected` | A rejection message. |
| `interview` | An interview invitation, a scheduling request, or an assessment invitation. |
| `ghosted` | Nothing arrived for 20 days. Derived, never stored. |

`ghosted` is not an event, because no message announces it. The scan computes it at read
time from the last event and today's date. If you store it, a rejection that arrives on
day 40 cannot correct the row, and the dashboard shows a stale status forever. The 20-day
threshold lives in `prefs.json` so you can change it without editing code.

Terminal events win over non-terminal ones. Among non-terminal events, the most recent
wins. `ghosted` applies only when no event follows the confirmation.

## What the sample showed

The dry run covered one month of mail, plus a five-month sweep for interview traffic.
These findings set the rules.

### The subject line cannot classify a message

This is the finding that shapes everything else. Vendors reuse one subject template for
opposite outcomes. `Thank you for applying to <company>` arrives for a confirmation and,
weeks later, for the rejection of that same application. Lever and Ashby phrase rejections
as `Thank you for your interest in <company>`, which reads like a confirmation.

Therefore: **the body decides the status, and the subject only supplies the company and
the role.** A rule keyed on the subject misclassifies roughly half of all rejections.

### Security-code mail is the largest false positive

About one message in five in the sample is a Greenhouse security code, subject
`Security code for your application to <company>`. It matches every reasonable
"application" pattern and carries no status. `mailcode.py` already owns this traffic.
Exclude it before classifying.

### Marketing and transactional mail split by sender domain

The same vendor sends both, from different domains. Transactional mail comes from
`us.greenhouse-mail.io`. Marketing comes from `us.greenhouse-jobs.com`. Job-board nudges
come from their own addresses and match application phrasing. Exclude by domain, which is
cheaper and more stable than excluding by wording.

### Interview traffic is rare, and humans send it

Across five months the sample holds one company's interview sequence and three assessment
invitations. Everything else is a confirmation or a rejection.

Interview mail arrives from a named person, whose address is a first and last name at the company domain, not
from a `no-reply@` address. Treat a human sender as a strong signal that the message
belongs to a live conversation. Automated mail almost never does.

### Your own replies sit in the same threads

An interview thread interleaves the recruiter's messages with your replies. Gmail labels
yours `SENT`. Drop any message you sent before classifying, or the scan reads your own
words as an employer's decision.

### One thread can hold several roles

A single thread carries confirmations for two different roles at one company. Another
carries five requisition numbers. A thread is therefore the wrong unit for counting.

**Dedup on `(company, role)`, never on the thread.** Where a vendor supplies a requisition
number, prefer it: it identifies the application exactly, and it survives a role title
that the employer words differently in different messages.

## Classification rules

Apply the gates in order. The first four reject a message outright. Only a message that
survives all four reaches the classifier.

1. **Drop anything you sent.** The Gmail label `SENT` is definitive.
2. **Drop security codes.** The subject begins `Security code for your application`.
3. **Drop marketing domains.** Maintain an explicit deny-list of vendor marketing hosts
   and job-board nudge senders. A deny-list is correct here, where an allow-list is not:
   new employers appear constantly, but the set of vendors sending marketing is small and
   changes slowly.
4. **Require an application-shaped body.** The message mentions an application, a
   candidacy, a role, or a requisition. This gate is narrow on purpose. `mailcode.py`
   records that one loose term turned an equivalent gate into a pass-through.

Then classify on the body, most specific first:

- **Rejected.** The body decides against you: it declines to move forward, it selects
  other candidates, it regrets to inform you, or it reports the position filled or
  closed. Rejection wording is the most templated of the three and matches reliably.
- **Interview.** The body invites you to interview, requests your availability, proposes
  a time, or invites you to an assessment. Weight a human sender heavily here.
- **Applied.** The body confirms receipt: it has received your application, it will
  review it, or it will be in touch.

If no branch matches, record the message as `unclassified` rather than guessing. An
unclassified count that grows is how you learn the rules need work.

## Joining a message to an application

Best effort, and honest about failure. Resolve in this order.

1. **A requisition number**, if the vendor supplies one. Exact.
2. **Company and role**, matched against `applied.json` within a plausible date window.
   Exact enough to attach to a single application.
3. **Company alone.** Attach the outcome to the company, not to a job.

Where step 3 applies and you hold several applications at that company, do not guess which
one. Attribute the outcome to the company and show it that way. Picking the nearest by date
produces a per-job row that is confidently wrong, which is worse than an honest aggregate.

Extract the company from the subject, or from the body when the subject names only a
vendor. Do not read it from the sender domain: for ATS mail that domain names the vendor,
not the employer.

## Where the data lives

`outcomes.json` sits under `JOBCOPILOT_HOME`, beside `applied.json`. It records where you
applied and what came back, so it is a record of a real job search and belongs with the
rest of your data rather than in the checkout. Add `OUTCOMES` to `core/paths.py` and add
`"OUTCOMES": True` to `EXPECTED` in `test_home_layout.py`.

Store events, not statuses. Each event names the application it belongs to, the status, the
date, the message id, and how the join resolved. Current status is always computed from the
events, which is what lets `ghosted` correct itself and what lets a rules change re-derive
history without a re-fetch.

Record the provenance of every application: the pipeline, the mail scan, or both. The two
sources disagree on totals by design, because you apply to roles outside the pipeline and
because not every application produces mail. Provenance is what lets each statistic name
its own denominator, so the dashboard can be a best estimate without being incoherent.

## Phasing

**P1. Classify offline.** Build the rules and the event store. Drive them from fixtures,
the way `test_mailcode.py` drives `FakeReader`. No inbox access, no writes to
`JOBCOPILOT_HOME`. Exit criterion: the rules reproduce the outcomes you already confirmed
by hand in the old dashboard's `data.json`, reported as a confusion matrix.

**P2. Scan for real.** Add a reader over a bounded window, reusing the `MailReader`
interface from `mailcode.py`. Cache by message id so a re-run is cheap. Write
`outcomes.json`. Report unclassified counts.

**P3. Show it.** Add a route to `web/app/routes.py` and a page under
`web/frontend/src/pages/`.

**P4. Schedule it.** Wire into `scripts/run_daily.ps1`, which already runs daily under
Windows Task Scheduler.

Do not start P4 until the unclassified rate is low enough that you trust the numbers.

## Credentials

The production scanner needs `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, and
`GMAIL_REFRESH_TOKEN`, which `python bridge/gmail_auth.py` produces. None are set today.
The `gmail.readonly` scope is correct for this feature: the scan only ever reads.

`make_reader()` also accepts `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` over IMAP. Prefer
OAuth. An App Password cannot be scoped, and this feature reads far more mail than
`mailcode.py` does.

## Fixtures and the PII guard

Test fixtures reproduce vendor templates, not your correspondence. Use fabricated employers
and `example-` prefixed addresses, which is the convention `core/contacts.py` already
follows. Vendor boilerplate is not personal data. The employers you applied to are, which
is why `companies.yaml` is gitignored, and the same reasoning covers a fixture file.

Run `scripts/pii_guard.py` over anything this feature adds.
