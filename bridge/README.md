# Apply control plane — Telegram bridge

`bridge.py` is the human-in-the-loop for the Apply engine. The extension fills every field
it can deterministically (Tier 0, no model calls); anything that needs a decision — salary
and other `always_ask` values, low-confidence field mappings (Tier 1), and drafted essays
(Tier 2) — is asked over Telegram and the answer goes straight back into the form.

**Submitting is always your own tap.** The bridge never submits anything.

```
extension (background.js)  <-- ws://127.0.0.1:8765 -->  bridge.py  <-- HTTPS -->  Telegram
```

It runs locally, only while you are applying. It holds real answers to real application
questions, so it binds `127.0.0.1` and has no auth: do not expose the port.

## One-time setup

**All of it is optional.** The extension is the thing that fills forms, and in panel mode it
fills one with the bridge switched off entirely — you answer the leftover questions in the
panel. What the bridge adds is remote control: steps 1-3 put those questions on your phone
and unlock queue mode, and step 4 lets a submit read the verification code some boards mail
you. Set up none of it, some of it, or all of it.

### 1. Create the bot

1. In Telegram, message **@BotFather**.
2. Send `/newbot`, pick a name and a username ending in `bot`.
3. BotFather replies with a **token** like `1234567890:AAE...`. That is `TELEGRAM_BOT_TOKEN`.

### 2. Get your chat id

Message your new bot once (any text — it will not reply yet), then:

```powershell
python -m pip install -r bridge/requirements.txt
$env:TELEGRAM_BOT_TOKEN = "1234567890:AAE..."
python bridge/get_chat_id.py
```

It prints the chat id to use as `TELEGRAM_CHAT_ID`. (If you would rather do it by hand,
that script just reads `https://api.telegram.org/bot<TOKEN>/getUpdates` and pulls
`.message.chat.id` out of the response.)

### 3. Store the two values

Put them in **`bridge/.env`**, which is gitignored:

```
TELEGRAM_BOT_TOKEN=1234567890:AAE...
TELEGRAM_CHAT_ID=123456789
```

Exported environment variables work too, and **a real environment variable beats the
file** — `_load_dotenv` uses `setdefault`, so `.env` only fills in what the environment
has not already set.

> That precedence is the one thing to remember when you **rotate the token**. Updating
> `.env` alone does nothing if the old token is also set as a persistent environment
> variable: the stale one keeps winning and the bridge fails authentication on boot. Change
> both, and start from a fresh terminal — an already-open shell still holds the old value.

Either way the secrets stay out of the repo. Never commit them.

### 4. Verification codes (optional)

Nothing depends on this one, including the rest of the bridge.
Some Greenhouse boards mail an 8-character code on submit ("to confirm you're a human") and
hold the application behind it. Give the bridge a read credential for that mailbox and it
hands the code back to the extension, which types it into the gate. Skip this and nothing
breaks: the application is parked with a reason and you paste the code yourself.

**A fetch only ever follows a human action** — your Submit click, the panel's 🔐 Code
button, or your ✅ in Telegram. Nothing polls, and the unattended queue never triggers one.
`mailcode.py` only looks at mail that arrived after that click, and only at messages that
read as verification, so a bank or 2FA code in the same inbox is not a candidate.

Read the mailbox your applications go to — the address in `profile.json` `identity.email`.
It is not necessarily the Google account you sign in with, and a mismatch fails as
`[AUTHENTICATIONFAILED] Invalid credentials`, which reads like a bad password.

Two shapes; set **either** set of variables in `bridge/.env`. If both are present the OAuth
one is used, so a leftover App Password never silently outranks the stronger credential.

**A. App Password** — fastest, one value. Needs 2-Step Verification on the account; then
[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) → create one:

```
GMAIL_ADDRESS=<the address in profile.json identity.email>
GMAIL_APP_PASSWORD=abcd efgh ijkl mnop
```

Google shows it space-grouped; either form works, the spaces are stripped for you (IMAP
rejects them). Understand the trade: **an App Password cannot be scoped** — that credential
grants full mailbox access, including delete. `ImapReader` opens the mailbox with `EXAMINE`
rather than `SELECT`, so the server refuses writes and mail is not even marked read, but
that is read-only by *our code*, not by the credential.

**B. Read-only OAuth token** — more clicks once, and the grant itself cannot modify mail.
In [console.cloud.google.com](https://console.cloud.google.com): new project → enable the
**Gmail API** → OAuth consent screen (External; add your own address as a test user) →
Credentials → Create OAuth client ID → **Desktop app**. Then:

```powershell
python bridge/gmail_auth.py
```

It opens the consent screen, catches the redirect on a loopback port, checks the granted
scope is `gmail.readonly`, and prints three lines to paste into `bridge/.env`:

```
GMAIL_CLIENT_ID=...apps.googleusercontent.com
GMAIL_CLIENT_SECRET=...
GMAIL_REFRESH_TOKEN=...
```

Revoke any time at [myaccount.google.com/permissions](https://myaccount.google.com/permissions),
without disturbing anything else.

> **While the consent screen is in "Testing", Google expires refresh tokens after 7 days.**
> Publish the app (Publishing status → Publish) to stop re-authorising weekly. The
> "unverified app" warning is expected — verification is only about *other* people using
> your client, and you are the only user.

Restart the bridge after editing `.env`, and remember the precedence rule above: a real
environment variable still beats the file.

## Running

Normally you do not start the bridge directly — the launcher at the repo root starts it
alongside the web console, as one process tree:

```powershell
python start.py
```

To run just the bridge:

```powershell
python bridge/bridge.py
```

On start it prints the bot username and sends a "🟢 Apply bridge is up" message. Leave it
running while you apply; `Ctrl+C` stops it.

Two things worth knowing:

- **Check liveness via the port, not the log.** stdout is buffered, so a backgrounded
  bridge's log file can sit empty while it is perfectly healthy. `netstat -ano` for a
  LISTENING socket on 8765 is the reliable signal.
- **Outbound calls to Telegram pick their IP family by measurement, not by faith.** On a
  box where one family is dead (a VPN routing only IPv4 is the everyday case), requests
  would otherwise hang on a route that cannot connect. The bridge probes on first use and
  re-probes after a connection failure, so a VPN dropping mid-run recovers on its own. Pin
  it with `TELEGRAM_IP_FAMILY=4|6|auto` only if you have measured that you need to — an
  unset variable is the self-correcting setting.
- **A new WebSocket connection supersedes the old one** (it closes the previous client and
  drains the inbox). Do not connect a test client while a real run is in progress — it will
  end that run.

## Testing the pipe (no form needed)

1. Start the bridge.
2. Load the unpacked extension (`chrome://extensions` → Developer mode → Load unpacked →
   `extension/`), open its popup, click **Test control plane**.
3. Two questions arrive on Telegram. Reply to the first with any text; on the second,
   either reply or tap **👍 Use draft**.
4. The popup echoes your answers back. That is the full round trip.

## Protocol (JSON over the WebSocket)

The interactive loop:

```
extension -> bridge:  {type:"ping"}
                      {type:"ask", reqId, items:[{id, label, why?, draft?}]}
                      {type:"review", reqId, summary:[{label,value}], warnings?}
bridge -> extension:  {type:"pong"}
                      {type:"hb"}                     # heartbeat, keeps the MV3 worker alive
                      {type:"answers", reqId, answers:{id: value}}
                      {type:"error", error}
```

Items are asked one at a time; the next Telegram message (or a 👍 on a drafted item) is
that item's answer. A `draft` lets you accept with one tap or reply to edit.

The batch flow adds `collect` (phase 1 parks a job's open questions on disk), `approved`
(phase 3 pulls back the approved pile), `applied`, `drafts` and `notify`. The full set,
with the reply shape for each, is documented in the module docstring at the top of
`bridge.py` — that is the source of truth, since it sits next to the dispatch table.

## Answering on the phone

Questions arrive one job at a time, led by a short card — company and role, location and
pay, the link, when it was found, and how many questions are coming — so you know what you
are answering before the first one lands. The card is sent once per job, not above every
question.

Every question also carries **✋ Skip this job**, for a role you decide against halfway
through. It *defers* rather than discards: the record and the answers you already typed
stay on disk, and the job drops out of the queue until you pick it up again. (The ✋ on the
final summary still deletes the record, so tomorrow's scrape re-serves the role — by then
there is a full set of answers and nothing half-done to lose.)

**`/jobs`** lists the whole batch — company, role, when it was found, `2/4` answered, and
its state — eight to a page, and tapping a row pins it as the next thing `/next` serves.
The pin is a `focus_at` stamp on the record itself, not a variable in the bridge, so
picking a job on the phone and answering it an hour later after a restart still lands on
the same job. Approved jobs are listed but not tappable: they are already on their way to
an employer, and reopening one under a flush in flight is the race the pending store
exists to prevent.

## Getting the resume itself (`/myresume`)

**`/myresume`** lists the four tracks with the date each was last cached; tap one and the
bridge uploads that PDF to the chat. **`/myresume ai`** skips the tap. It sends the file
from `resumes/` — the same bytes the extension attaches — under the same name an employer
sees, so what lands on your phone is what went out on the application.

The cache is refreshed on the laptop with `python -m core.resumes sync-local`. If a track
has no cached file, `/myresume` says so and names that command rather than sending
something stale.

### The name on the file

Every resume goes out as `First_Last_<letter>.pdf`, built from `identity` in your profile:
`Parth_Thakkar_q.pdf` for quant, `_d` for DS, `_t` for tech, `_a` for AI. The suffix is
dropped when the profile defines a single track.

The source PDF's own name is never used. It was, and after `python -m core.resumes
sync-local` that name is whatever the file is called in the Finals folder, so each track
went out under a different string and any name not already shaped `..._<tag>_vN.pdf` went
out verbatim. `core/profile.py resume_filename` and its port in `extension/attach.js` are
now the one source of that name, checked against each other by `test_stream_parity.py`.

## Drafting (Tier 2)

Essay answers are drafted by shelling out to the **Claude Code CLI** (`drafter.py`), so it
needs a `claude` binary on `PATH` and does **not** use `ANTHROPIC_API_KEY`. Without that
binary the bridge just asks you cold — nothing breaks.

Drafting is a persisted on/off flag rather than a startup setting, so the web console and
Telegram (`/draft`) can toggle it and the running bridge picks the change up on its next
read. The answer bank always wins over a generated draft, since a saved answer is wording
you already approved.
