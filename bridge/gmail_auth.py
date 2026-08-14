#!/usr/bin/env python
"""gmail_auth.py — one-time consent to get a READ-ONLY Gmail refresh token.

Run once:  python bridge/gmail_auth.py

Prints a GMAIL_REFRESH_TOKEN line to add to bridge/.env. The bridge then reads verification
codes with it (see mailcode.GmailReader); nothing else in the pipeline touches your mail.

WHY OAUTH AND NOT AN APP PASSWORD: an App Password cannot be scoped — it would grant this
process full IMAP access, including delete, to read an 8-character code. The scope
requested here, `gmail.readonly`, cannot modify anything, and it can be revoked on its own
at https://myaccount.google.com/permissions without disturbing any other access.

Uses the loopback flow (Google's recommended flow for a desktop app): a local one-shot HTTP
server catches the redirect, so no code is ever copied by hand. Nothing leaves this machine
except the token exchange with Google, and the refresh token is printed for you to place —
this script deliberately does NOT write your .env for you.
"""
import http.server
import json
import os
import secrets
import socket
import sys
import threading
import urllib.parse
import webbrowser

import requests

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Catcher(http.server.BaseHTTPRequestHandler):
    """Catches the single redirect Google sends back, then stops."""
    result: dict = {}

    def do_GET(self):
        _Catcher.result = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
        ok = "code" in _Catcher.result
        body = (b"<h2>Done.</h2><p>You can close this tab and go back to the terminal.</p>"
                if ok else b"<h2>No code came back.</h2><p>Check the terminal.</p>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass  # the default handler prints the query string, which carries the auth code


def main() -> int:
    client_id = os.environ.get("GMAIL_CLIENT_ID", "").strip() or input("OAuth client ID: ").strip()
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET", "").strip() or input("OAuth client secret: ").strip()
    if not client_id or not client_secret:
        print("Both the client ID and secret are required — see the setup steps in this "
              "file's docstring.", file=sys.stderr)
        return 1

    port = _free_port()
    redirect_uri = f"http://127.0.0.1:{port}"
    state = secrets.token_urlsafe(16)
    url = f"{AUTH_URL}?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": SCOPE, "state": state,
        # access_type=offline is what makes Google return a REFRESH token at all, and
        # prompt=consent forces a fresh one even if this client was authorised before —
        # without it a re-run returns only an access token and the script looks broken.
        "access_type": "offline", "prompt": "consent"})

    server = http.server.HTTPServer(("127.0.0.1", port), _Catcher)
    threading.Thread(target=server.handle_request, daemon=True).start()

    print("\nOpening your browser to approve READ-ONLY Gmail access.")
    print("If it doesn't open, paste this into it:\n")
    print(url + "\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    print("Waiting for the redirect…")
    server.socket.settimeout(300)
    for _ in range(300):
        if _Catcher.result:
            break
        threading.Event().wait(1)
    got = _Catcher.result
    if not got.get("code"):
        print(f"No authorisation code came back ({got.get('error') or 'timed out'}).", file=sys.stderr)
        return 1
    if got.get("state") != state:
        # The state check is what stops another page on this machine feeding us its code.
        print("State mismatch — ignoring this response.", file=sys.stderr)
        return 1

    r = requests.post(TOKEN_URL, timeout=30, data={
        "code": got["code"], "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code"})
    if not r.ok:
        print(f"Token exchange failed ({r.status_code}). Check the client ID/secret and "
              f"that the redirect URI type is 'Desktop app'.", file=sys.stderr)
        return 1
    payload = r.json()
    refresh = payload.get("refresh_token")
    if not refresh:
        print("Google returned no refresh token. Revoke this app at "
              "https://myaccount.google.com/permissions and run this again.", file=sys.stderr)
        return 1

    scopes = payload.get("scope", "")
    if "gmail.readonly" not in scopes:
        print(f"Unexpected scope granted: {scopes}", file=sys.stderr)
        return 1

    print("\nGranted scope:", scopes, "(read-only — this token cannot modify your mail)\n")
    print("Add these three lines to bridge/.env (which is gitignored):\n")
    print(f"GMAIL_CLIENT_ID={client_id}")
    print(f"GMAIL_CLIENT_SECRET={client_secret}")
    print(f"GMAIL_REFRESH_TOKEN={refresh}")
    print("\nThen restart the bridge. Revoke any time at "
          "https://myaccount.google.com/permissions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
