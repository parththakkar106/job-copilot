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

## Drafting (Tier 2)

Essay answers are drafted by shelling out to the **Claude Code CLI** (`drafter.py`), so it
needs a `claude` binary on `PATH` and does **not** use `ANTHROPIC_API_KEY`. Without that
binary the bridge just asks you cold — nothing breaks.

Drafting is a persisted on/off flag rather than a startup setting, so the web console and
Telegram (`/draft`) can toggle it and the running bridge picks the change up on its next
read. The answer bank always wins over a generated draft, since a saved answer is wording
you already approved.
