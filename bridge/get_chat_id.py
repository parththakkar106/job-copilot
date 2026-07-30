#!/usr/bin/env python3
"""Print your Telegram chat id. Message the bot first, then run this with
TELEGRAM_BOT_TOKEN set in the env. Prints the chat id to paste into TELEGRAM_CHAT_ID."""
import os
import sys

import requests

_envpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_envpath):
    with open(_envpath, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
if not token:
    sys.exit("Set TELEGRAM_BOT_TOKEN first, then send your bot a message in Telegram.")

r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20)
r.raise_for_status()
updates = r.json().get("result", [])
chats = {}
for u in updates:
    msg = u.get("message") or u.get("edited_message") or {}
    chat = msg.get("chat")
    if chat:
        chats[chat["id"]] = chat.get("username") or chat.get("first_name") or ""

if not chats:
    sys.exit("No messages found. Send your bot any message in Telegram, then re-run.")

for cid, who in chats.items():
    print(f"TELEGRAM_CHAT_ID = {cid}   ({who})")
