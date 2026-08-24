# Outreach — find people, draft the note, Parth sends it (implementation plan)

> **SUPERSEDED IN DIRECTION 2026-08-18 — see `referral_flow_plan.md`.** Parth flipped the
> flow to people -> jobs: he finds the contact himself and pastes the profile in. Phase 1
> below (`core/contacts.py`) is unchanged and still correct, but **the new path does not
> call it**. Phases 2-4 here are replaced by the phasing in `referral_flow_plan.md`. The
> constraints, provider table, title filter, relevance levers and PII sections below all
> still hold and are referenced from there rather than duplicated.

Status: **PHASE 1 BUILT AND MEASURED LIVE 2026-08-04** (`core/contacts.py`, 91 checks). Phases 2-4 (drafting,
Telegram, console) are still plan only. Companion to `dashboard_spec.md` (the pile model)
and the apply flow in the root `CLAUDE.md` — this reuses both wholesale.

## What shipped (phase 1)

`core/contacts.py` (new, `python -m core.contacts --selftest`), `core/paths.py`
(+`OUTREACH`), `scripts/pii_guard.py` (+ a third-party-profile shape rule and
`derive_contact_patterns()`), `scripts/test_pii_guard.py` (+9 checks, 118 total),
`test_home_layout.py` (+`OUTREACH`, 56 total). Nothing is wired into the bridge or the
console yet — this is a library plus a CLI, on purpose, so the search quality can be
measured before anything depends on it.

Deviations from the plan below:

- **`Contact.__post_init__` derives `country` from the profile URL.** The plan treated it as
  a parsed field, but a Contact built anywhere else then scored as location-unknown, which
  silently disables the whole location weighting instead of failing loudly. It caught itself
  in the selftest.
- **`default_affinities()` reads the school and employer from `profile.json` at runtime.**
  The first draft hardcoded them and the PII guard flagged it — they are the user's real
  details, in what is meant to be a shareable repo. This is also what makes the module work
  for a stranger with their own profile.
- **The ladder counts an EXCLUDED contact as a hit.** If the only person a query finds is
  already in the outreach store, the query still worked; falling further down the ladder
  would only re-find them at the cost of another API call.
- **`example-` is reserved in profile URLs**, exactly as `555` and `example.com` already are
  in this project's guard. The selftest needs realistic profile URLs to parse, and the guard
  needs to flag real ones — the prefix is what lets both be true. **The first draft of the
  selftest used real people lifted from live search results; that is precisely the leak this
  feature creates, and it went in before the guard knew how to catch it.**

**Measured live against Tavily on 10 real queue companies** — see the measured section
below for what that changed. Phase-1 exit criterion met; phase 2 is unblocked.

The pipeline currently ends at "submitted". For the dream tier it does not even do that:
`core/scraper.py:662` writes *"the auto-apply loop skips these — apply by hand (referral
first)"* into the digest and then abandons 304 roles at exactly the point where a referral
is the thing that matters. This closes that gap.

Goal: for a role Parth cares about, surface **2-3 named people worth contacting** and a
**drafted LinkedIn message** for each. He sends it himself. Nothing is ever sent
automatically — same rule as submit.

## Two constraints that shaped everything below

**1. LinkedIn has no usable API.** Self-serve products are your own profile (OAuth),
share/post, company-page management, and the ads Marketing API. Profile lookup, people
search, and employee lists are not available at any price without partner approval, which
is granted to ATS/CRM vendors, not job seekers. Verified 2026-08-04. Treat as permanent.

**2. Invite limits are real, but Parth is on Premium** (his decision, 2026-08-04), so the
~5-personalised-invites-per-month cap that binds free accounts does **not** apply. What still
applies: **300 characters** per invite note, ~100-200 invites/week, and an acceptance rate
under ~30% triggering restrictions. The last one is the only one likely to bite, and it is a
quality problem, not a volume problem — see "Relevance" below.

### Channel design

Every invite carries a note, so there is no budget counter and no "connect silently, DM
later" fallback to build. Two drafted variants per contact:

- **`invite_note`** — <=300 chars, sent with the connection request. The primary path.
- **`dm`** — longer, no cap. Sent after they accept, or as an InMail (Premium) to someone
  worth reaching without waiting.

Outreach is still a state machine, because acceptance takes days:

```
found -> invited -> accepted -> messaged -> replied / no_reply
```

`accepted` is the only transition Parth reports by hand (we cannot see his LinkedIn). A
Telegram nudge asks about pending invites every few days rather than making him remember.

## Discovery: a search index, never LinkedIn itself

Public LinkedIn profiles are indexed by the major search engines, so every query runs
against a general **web-search API** over public profile pages. Parth's LinkedIn account is
never automated, never scraped, never logged into by code. This is the same reasoning that
made the scraper API-only: the account is worth more than the feature.

### The provider is pluggable, and that is not over-engineering

This market has broken three times in eighteen months, so betting the design on one vendor
is how it stops working:

| provider | status (verified 2026-08-04) |
|---|---|
| **Google Custom Search JSON API** | **CLOSED TO NEW CUSTOMERS.** Existing accounts cut off **2027-01-01**. The first draft of this plan specified it; it was already unavailable. |
| Bing Web Search | Retired outright 2025-08-11. No Microsoft drop-in. |
| Brave | Free tier withdrawn 2026-02. ~$0.003-0.005/query, card required, **no spend cap**. ~$5-9/mo at this volume. |
| **Tavily** | ~1000 credits/month free. **Current default, verified working 2026-08-04.** Live run cost ~4.2 queries/role, so ~240 roles/month inside the free tier. |
| Exa | ~1000 credits free, similar shape. |

`CONTACTS_SEARCH_PROVIDER` picks one (`tavily`/`brave`/`exa`/`google`); each adapter
normalises to `{title, link, snippet}`, which is all `parse_result` needs. Adding the next
one when this table rots again is a ten-line function plus one selftest case.

**Re-verify availability before adopting any of these.** The whole reason this section
exists is that a confidently-stated free tier turned out not to be purchasable.

Three **discovery** queries per role, plus scoring boosts (see below):

| signal | query shape | measured result |
|---|---|---|
| `team` | `site:linkedin.com/in intitle:<Company> "<team or role kw>" (Manager OR Lead OR Head)` | **Best performer.** See §Measured. |
| `recruiter` | `site:linkedin.com/in intitle:<Company> ("technical recruiter" OR "talent acquisition")` | **Works.** Lowest reply rate, but the only person who can move an application. |
| `alumni_school` | `site:linkedin.com/in "<Company>" "<your school>"` | **Works only with the title filter** — raw results are ~80% ex-employees. |

Parse name + headline + profile URL out of the result title/snippet. At ~20 new roles/day
and 3 queries per role that is ~60 queries/day, which a free tier covers; the quota only
binds if Parth backfills history, which should be rate-limited and resumable.

### THE TITLE FILTER (the load-bearing rule)

Search engines render a profile as `Name - Headline | LinkedIn`, and **the headline is the person's
CURRENT employer.** Measured directly: an alum who has left the target company renders as
`<Name> - <their new employer> | LinkedIn`, with "ex-<target>" appearing only in the snippet
body.

So: **require the company name in the result TITLE, not anywhere on the page.** This one
post-filter separates current employees from ex-employees and from randoms who merely
mentioned the company in a post. It is why the `team` and `recruiter` queries scored well
(`intitle:` forced it) and why the raw alumni query did not.

Implement it as a **post-filter on results**, not only as an `intitle:` operator — operators
are honoured inconsistently across engines, and the post-filter is deterministic.

### The shared-employer term is a BOOST, not a query

Measured: `site:linkedin.com/in "<Company>" "<former employer>"` is mostly junk. A large,
frequently-discussed former employer is named so often in posts and commentary that the
query matches people *talking about* it rather than people who worked there — one genuine
hit across two searches.

Inverted, it works: find people via the three discovery queries, then **check whether the
former-employer term (or the school term) appears in their snippet, and boost the score.**
Same for any other shared-background term. This is the overlap ranking from below, and it
turns the worst-performing signal into a free ranking input. Parth asked for the
former-employer signal specifically (2026-08-04); this is how it earns its place.

**Both terms are read from `profile.json` at runtime** (`contacts.default_affinities()`),
never written into the code or this document. They are the user's real school and employer,
so hardcoding them would put personal data in the shareable repo — the guard flagged exactly
that in this file's and `core/contacts.py`'s first drafts.

**Be honest about `team`:** you cannot determine who owns a specific req from outside. This
finds *plausible* people on the right team by title adjacency, not the confirmed hiring
manager, and both the UI and the drafted note must respect that — a note opening "I saw
you're hiring for this role" sent to someone uninvolved is worse than no message.

## Relevance — the part that decides whether this is useful

A shortlist of three good people beats a list of thirty. Five levers, in order of payoff:

**1. Mine the JD for the team, not just the title.** Postings routinely name the team
("you'll join Payments Risk") or the manager's title ("reporting to the Director of
Quantitative Research"). Those strings make `team` queries specific enough to find actual
teammates instead of anyone with a similar job title. This is the single biggest lever, and
it is why the lazy JD re-fetch below is worth the request.

**2. Query in a ladder, stop at the first hit.** Exactly the pattern `searchCandidates()`
already uses for the school dropdown: exact team name -> team + seniority -> role keywords
-> company only. Specific queries match better but often return nothing; generic ones always
return something but it is mush. Ladder down until rows appear.

**3. Score on overlap; let one person match several signals.** Do NOT sort each person into
one bucket. Test every candidate against every signal and rank by how many they hit —
someone who is both a former colleague *and* on the hiring team is a categorically better
contact than either signal alone, and a bucket-per-person model cannot express that person
at all. Then title similarity to the role and seniority as tiebreaks.

**4. Exclusion list.** Staffing-agency recruiters who name the company without working
there, sales/BD titles, and headlines containing "ex-" or "former". Cheap, kills most junk.

**4b. Location, weighted per signal — never a hard filter.** LinkedIn puts a **country
subdomain on profile URLs** (`in.linkedin.com/in/...`, `ca.linkedin.com/in/...`, plain
`www.` otherwise), so country arrives free with every result, no extra query. Observed in
the 2026-08-04 run. City appears only in snippet text ("Greater Seattle Area") and
inconsistently — treat country as dependable, city as a bonus. The subdomain reflects
profile locale rather than strict residence, so it is good-not-certain evidence.

Weight it by signal, because relevance differs sharply:

| signal | weight | why |
|---|---|---|
| `recruiter` | **high** (0.45) | Recruiters are region-scoped by job function. The measured recruiter set spanned Canada, Chicago, Seattle and the Bay Area; a Canada-based recruiter is unlikely to own a US req. |
| `team` | medium (0.20) | An EM in NYC hiring for a remote role is entirely right. Prefer a match, never require one. |
| `alumni_*` | **small tie-breaker only** (0.10) | Referrals travel inside a company regardless of geography. An alum in the Bangalore office can still refer a US req; dropping `in.linkedin.com` here discards some of the best contacts. |

**The alumni penalty is capped by an invariant, not chosen by feel** (Parth's call
2026-08-04: an alum is worth more wherever they sit). At 0.10 a distant alum scores 0.90 and
still outranks an ordinary same-country team contact (0.80) and every recruiter. Past ~0.20
it would invert that, which is the opposite of the intent. Three selftest assertions pin the
ordering so retuning either constant cannot silently reorder the signals.

**One deliberate exception:** a *senior* same-country team contact wins the seniority nudge
(0.80 + 0.15 = 0.95) and edges out a distant alum at 0.90. That is intended — a Head or
Director on the hiring team is plausibly the person who owns the req, and that beats a
referral from someone three time zones from the work.

Compare against the job's own `location` + `location_hint`, which every queue row already
carries — no new data needed on the job side.

**5. Parth is the last filter.** With Premium he can open any profile himself. The pipeline
does not need to be right — it needs to hand him three plausible people and a link, and he
decides in five seconds. Optimise for a good shortlist, not perfect precision.

### The known weakness: index staleness

Snippets go stale, so "at Robinhood" can mean "left in 2024". **Measured 2026-08-04: on the
raw alumni query this is roughly 4 in 5 results, not the 1 in 5 originally estimated here.**
An ex-employee cannot refer you, so these are not degraded hits — they are worthless ones.
The title filter above is the cure and is not optional. What survives it is a much smaller,
much better list. Parth eyeballs the profile before sending, so a residual stale hit costs
five seconds. **Do not over-engineer beyond the title filter.**

Keep the top 2-3 after ranking. Never surface ten.

## Measured 2026-08-04 (hand-run, before any code)

Run against `Robinhood — Senior Software Engineer, Data Lake`, a real queue row. **Caveat:
run through a general web-search tool, NOT the provider this will use** — a different
index that honoured operators loosely (it matched "Robin Elliott" against "Robinhood"). The
failure *shapes* below will carry; the exact yield must be re-measured against the chosen provider in phase 1.

- **`team` — strongest, and better than expected.** Returned `Srikanth Devidi - Head of Data
  and Experimentation Platform at Robinhood`, two Data Science Managers, a VP Engineering,
  and an Engineering Manager. A plausible hiring-manager org **from the job title alone**,
  before any JD mining. That raises confidence in lever 1 rather than replacing it.
- **`recruiter` — works.** Six named Robinhood recruiters, several with specialisms
  ("Technical Recruiting at Robinhood", "Recruiting at Robinhood - We're Hiring!").
- **`alumni_school` — real people, but ~80% "Ex-SWE @ Robinhood".** Drove the title filter.
- **`alumni_employer` — failed as a query.** Matched commentary about both companies. Drove
  the demotion to a scoring boost.

**Verdict: proceed.** Two of three discovery queries are good out of the box and the third
has a known cure.

## Measured LIVE against Tavily, 2026-08-04 — 10 real queue companies

30 contacts across 10 roles, **42 queries** (~4.2/role, so ~240 roles/month inside a
1000-credit free tier). Roughly **7 of 10 companies returned a shortlist worth acting on**;
the other three are one identifiable failure class, below.

Four things the live run changed, none of which were predictable from the SERP experiment:

- **Tavily returns a scraped profile CARD, not a search snippet.** `title` is only the
  person's name; the body is `# Name / <employer> / <location, ISO country> / ## About /
  ## Education`. That is strictly better than Google's title string — the employer is a
  field, the location is city-level, education is stated — so `parse_profile_card()` reads
  it and `split_title()` became the fallback for SERP-shaped providers.
- **`site:` and `intitle:` are Google-only.** Tavily matches them as literal text, so they
  were pure noise. `strip_operators()` removes them and the domain restriction is expressed
  natively via `include_domains`.
- **The employer test had to become EXACT.** A substring test wrecks single-word company
  names — "Writer" matched a freelance content writer, "Harvey" a surname, "Sierra" a role
  at Cisco. `employer_is()` requires equality (plus a short legal-suffix allowance).
- **Signals must be VERIFIED from the card, not inferred from the query that found them.**
  A semantic engine matches loosely, so the alumni query returns plenty of people who merely
  work there. An `alumni_*` signal now requires the school/employer to be visible on the
  profile; unbacked hits fall back to the weakest signal rather than being discarded, since
  they are still current employees of the target.

### The one open failure class: company names that are ordinary words

**Writer, Sierra, Elastic** all contaminated. "Writer" returned three literal writers,
"Elastic" matched people listing Elasticsearch as a skill, "Sierra" matched a different
Sierra. The exact-match fix removed the worst of it but cannot fix a case where the person's
employer field genuinely *is* the word.

Not yet solved. Options, cheapest first: (a) detect single-token company names that are
common English words and skip the alumni query for them, since team/recruiter queries carry
the company as context and contaminate less; (b) require a second corroborating mention;
(c) accept it and mark those results low-confidence in the UI, since Parth eyeballs the
profile before sending anyway. **(a) plus (c) is the recommendation.**

### Also worth knowing

Most alumni hits are **India-based** (`in`), which is expected and correct — location is
deliberately not penalised for alumni because a referral crosses offices. But it means the
alumni signal skews toward people who cannot speak to a US team's hiring, while still
outranking a US-based team contact. Watch whether that ordering is right in practice; the
weights in `SIGNAL_WEIGHTS` are the dial.

The best single result of the run: for `Discord — Senior SWE, Application Security`, the top
two were **Head of Corporate Security @ Discord** and **Product Security at Discord**. That
is the `team` signal doing exactly what it was designed for, from the job title alone.

### Second source: the posting itself

Some postings name a recruiter or carry a contact address. **The scraper already fetches
this and discards it**: `core/scraper.py:496` requests Greenhouse with `content=true`, and
`parse_greenhouse` (`:156`) keeps seven fields, none of them the description. Lever
(`?mode=json`) and Ashby (`descriptionHtml`) are the same.

Do **not** add a description field to `Job`. 213 roles of JD HTML in a git-committed
`queue.json` is megabytes of nightly churn for data that is used on a handful of roles.
Instead **re-fetch the single posting lazily**, only when contacts are generated for it
(~20/day). Cheap, no storage, no schema change.

## Data model

Contacts ride on the pending record like answers and drafts do, so the collect/approve
machinery works unchanged:

```jsonc
"contacts": [{
  "id": "fnv1a(profile_url)",       // stable across re-runs, unlike a sequence
  "name": "...", "headline": "...", "profile_url": "...",
  "signals": ["alumni_employer", "team"],   // a LIST — overlap is the ranking signal
  "country": "us",                          // from the profile URL subdomain; "" if unknown
  "score": 0.82,
  "invite_note": "...", "dm": "...",  // drafted, never sent
  "state": "found|invited|accepted|messaged|replied|no_reply|skipped",
  "history": [{"state": "invited", "ts": "..."}]
}]
```

Plus **`outreach.json` in `JOBCOPILOT_HOME`** — an append log keyed by contact id, mirroring
`applied.json`. It has to be separate because a pending record gets archived on submit
(`pending.archive()`), and follow-up outlives the application by weeks. This is also what
answers "have I already messaged this person?" when the same recruiter covers three roles —
**dedupe by profile URL across jobs, or you will invite the same person twice.**

## Drafting

A `drafter.draft_outreach(contact, job, ctx)` sibling to `draft_answers`. Reuses everything:
the voice rules already encoded in `build_prompt` (human, short, one subtle typo, no
dashes), the batching trick (all contacts for one role in one Claude call), `sanitize()`,
the `DRAFTS_ENABLED` kill switch, and persistence so a re-read is free.

New prompt requirements: hard 300-char ceiling on `invite_note` (validate after generation,
regenerate once if over), the specific role title, and one concrete hook from the contact's
headline. **The matched signals go into the prompt** — a note to an ex-colleague colleague
should open on that, not on the role, and a `team` note must not assert the person is
hiring. The bank should get outreach rows eventually, but start with model-only — there is
no corpus of Parth-approved outreach yet.

## Surfaces

- **Telegram** — a card per role reusing the existing approve loop: *"Ramp · 3 found · 👍
  copy note / ✏️ edit / ⏭ skip"*. Tapping gives the profile URL and the text to paste.
- **Web console** — a fourth page, `Outreach.jsx`. Table by state, the monthly note budget,
  and the follow-up queue. Reuses `MoveMenu.jsx` for state transitions.
- **No extension work in v1.** A LinkedIn-side floaty is deliberately out of scope: it is
  the only piece that touches LinkedIn directly, and copy-paste solves the same problem at
  zero account risk.

## PII — this is the part to get right first

Contacts are **other people's** personal data: names, headlines, profile URLs. Consequences:

- `outreach.json` and the contact blocks live in **`job-pipeline` (private)**, never in
  `job-copilot`. `core/paths.py` owns the location; `test_home_layout.py` gets a case.
- **`scripts/pii_guard.py` must learn the shape** before any of this is written, or the
  pre-commit hook will happily let a real contact name into a test fixture. The guard has
  caught its own author three times — assume it will need to catch this too.
- Test fixtures use invented people. Follow `test_pii_guard.py`'s convention.
- Nothing here goes near `answer_bank.yaml`, which is Parth's own words and is shared.

## Phasing

1. **Discovery + storage — DONE 2026-08-04.** `core/contacts.py`, the signals, the query
   ladder, the title filter, overlap scoring, dedupe by profile URL, `outreach.json`, paths
   + PII guard + tests. Measured live; see above. Exit criterion met.

2. **Drafting.** `draft_outreach`, both variants, the 300-char validator. **Plus two gaps
   carried over from phase 1** (Parth's call 2026-08-04 — deferred here rather than patched
   in place):
   - **Wire `fetch_description()` into `find_contacts()`.** It is written and tested but
     nothing calls it: `description` is a parameter the caller must supply, and the live
     measurement passed none. So **every measured result came from role keywords alone and
     the JD/team-name lever — the one called biggest in Relevance §1 — has never actually
     run.** That means real headroom AND that `team_hints()` is untested against live
     postings. Fetch lazily inside `find_contacts` when the caller supplies nothing.
   - **A title-similarity term in `score()`.** Relevance §3 lists it as a tiebreak and it
     was never implemented — `score()` has signal weight, overlap, seniority and location
     and nothing else. Consequence: among three team contacts at one company, a Head of Data
     and a Head of Marketing score IDENTICALLY. Cheap fix: token overlap between the job
     title (minus `_STOP`) and the contact's headline.

3. **Telegram surface + state machine.** The accept-nudge loop.
4. **Console page.** Follow-up queue, state transitions.

5. **LATER OPTION — replace the regex extraction with AI** (Parth's request 2026-08-04).
   `team_hints()` is four hand-written regexes and is deliberately conservative, so it fires
   only on the phrasings it was taught ("join the X team", "reporting to the Director of
   Y"). A model reading the posting would get the team name, the manager's title, the tech
   stack and the seniority band out of any phrasing, and could say "no team named" instead
   of silently matching nothing.

   **Why this is close to free, and where it has to go:** extraction must happen BEFORE the
   search (its output builds the query), while drafting happens AFTER (it needs the
   contact), so the two cannot share one call. But `bridge/_predraft_record` ALREADY makes
   one batched Claude call per job at collect time — extraction rides along in that call for
   roughly zero marginal cost, since the fixed system-prompt reload is what dominates. Stash
   the result on the pending record next to the drafts and `find_contacts` reads it.

   Keep the regex as the fallback: it costs nothing, needs no bridge, and phase 1 must keep
   working with drafting disabled or the CLI offline. **Do not start here** — measure what
   the regex actually yields on live JDs in phase 2 first, or there is no baseline to judge
   the model against.

## Open questions for Parth

- **Trigger point.** Dream tier wants outreach *before* applying (referral first); pipeline
  tier wants it *after* submit (follow-up within 48h). Confirm that split.
- **Volume ceiling.** How many roles per week actually deserve outreach? Premium removes the
  note cap, but the ~30% acceptance-rate floor means indiscriminate inviting is still the
  one way to get restricted.

Settled: **Premium** (2026-08-04, Parth's call — no note budget to model). **the former-employer term
as a second alumni signal** (same date, same call).
