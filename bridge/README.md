# Apply control plane — Telegram bridge

`bridge.py` is the human-in-the-loop for the Apply engine. The extension fills every
field it can deterministically (Tier 0, no Claude credits); anything that needs Parth —
salary and other `always_ask` values, low-confidence field mappings (Tier 1), and
answer-bank drafts (Tier 2) — is asked over Telegram by this bridge and the answer is
sent straight back into the form. The submit button is always Parth's own tap.

It runs locally on Parth's laptop, only while he's applying. **Secrets live in the
environment, never in a file.**

```
extension (background.js)  <-- ws://127.0.0.1:8765 -->  bridge.py  <-- HTTPS -->  Telegram
```

## One-time setup

### 1. Create the bot
1. In Telegram, message **@BotFather**.
2. Send `/newbot`, pick a name and a username ending in `bot`.
3. BotFather replies with a **token** like `1234567890:AAE...`. That's `TELEGRAM_BOT_TOKEN`.

### 2. Get your chat id
1. Send any message (e.g. `hi`) to your new bot in Telegram.
2. Open this URL in a browser (paste your token in):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Find `"chat":{"id":123456789,...}`. That number is `TELEGRAM_CHAT_ID`.

### 3. Install deps
```powershell
python -m pip install -r bridge/requirements.txt
```

## Running

Easiest: put the two values in `bridge/.env` (gitignored — never committed) and just run:
```
TELEGRAM_BOT_TOKEN=123:AAE...
TELEGRAM_CHAT_ID=123456789
```
```powershell
python bridge/bridge.py
```
`bridge.py` auto-loads `bridge/.env`. A real exported env var still wins over the file,
so you can override per-shell if you want:
```powershell
$env:TELEGRAM_BOT_TOKEN = "123:AAE..."
$env:TELEGRAM_CHAT_ID   = "123456789"
python bridge/bridge.py
```

On start it prints the bot username and sends you a "🟢 Apply bridge is up" message.
Leave it running in a terminal while you apply. `Ctrl+C` to stop.

> The token/chat-id belong in `bridge/.env` or your shell env only — both are gitignored
> / out of the repo. Never commit them.

## Testing the pipe (no form needed)

1. Start the bridge (above).
2. Load the unpacked extension (`chrome://extensions` → Developer mode → Load unpacked →
   `extension/`), open its popup, click **Test control plane**.
3. Two questions arrive on Telegram. Reply to the first with any text; on the second,
   either reply or tap **👍 Use draft**.
4. The popup shows your answers echoed back. That's the full round-trip working.

## Protocol (JSON over the WebSocket)

```
extension -> bridge:  {type:"ping"}
                      {type:"ask", reqId, items:[{id, label, why?, draft?}]}
bridge -> extension:  {type:"pong"}
                      {type:"hb"}                     # heartbeat, keeps MV3 worker alive
                      {type:"answers", reqId, answers:{id: value}}
                      {type:"error", error}
```

Items are asked one at a time; the next Telegram message (or a 👍 on a drafted item) is
that item's answer. A `draft` lets you accept with one tap or reply to edit.

## Built and shipping

All three of the slices this section used to list as pending are done and tested:
`fill.js` escalation calls `ASK_USER` and applies the returned answers; the end-of-form
review and submit gate work; and Tier 1/2 drafting runs in the bridge. Drafting shells out
to the **Claude Code CLI** (`bridge/drafter.py`), so it needs a `claude` binary on PATH and
does NOT use `ANTHROPIC_API_KEY`. Without that binary the bridge simply asks you cold.
