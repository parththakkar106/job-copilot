"""The console's login: one shared password, a signed session cookie, and failed-try limits.

The page has one job on the public internet: keep strangers away from a console that holds
personal data and can submit real applications. Every path sits behind `gate`, including
the built UI, so nothing renders for a visitor without a session.

The password's scrypt hash and the cookie signing key live in AUTH_PATH, which
`scripts/set_console_password.py` writes. Without that file the console serves loopback
requests only, and tunnel traffic gets a 503, so a tunnel started before the password
was set exposes nothing.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import secrets
import time
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

AUTH_PATH = Path(__file__).resolve().parents[2] / "bridge" / ".console_auth.json"
COOKIE = "console_session"
SESSION_SECS = 7 * 86400
MIN_PASSWORD = 12
LOGIN_PATHS = ("/login",)
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "testserver")

# A per-address limit slows one guesser, and the global limit covers a guesser that rotates
# addresses. A long password makes the global limit a backstop, at the cost that a sustained
# attack also locks out real logins for one window.
# shortcut: counters live in memory and reset on restart. Persist them if restarts matter.
WINDOW_SECS = 15 * 60
MAX_FAILS_PER_ADDRESS = 5
MAX_FAILS_GLOBAL = 30
_fails_by_address: dict[str, deque] = {}
_fails_global: deque = deque()


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 15, r=8, p=1,
                          maxmem=64 * 1024 * 1024, dklen=32)


def write_password(password: str, path: Path | None = None) -> None:
    """Store a new hash and a new signing key, which also ends every existing session."""
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"use at least {MIN_PASSWORD} characters")
    salt = secrets.token_bytes(16)
    (path or AUTH_PATH).write_text(json.dumps({
        "salt": salt.hex(), "hash": _hash(password, salt).hex(),
        "signing_key": secrets.token_hex(32),
    }), encoding="utf-8")


def _load() -> dict | None:
    try:
        return json.loads(AUTH_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _password_ok(password: str, stored: dict) -> bool:
    return hmac.compare_digest(_hash(password, bytes.fromhex(stored["salt"])),
                               bytes.fromhex(stored["hash"]))


def _sign(expires: str, stored: dict) -> str:
    return hmac.new(bytes.fromhex(stored["signing_key"]), expires.encode(),
                    hashlib.sha256).hexdigest()


def _new_token(stored: dict) -> str:
    expires = str(int(time.time()) + SESSION_SECS)
    return f"{expires}.{_sign(expires, stored)}"


def _token_ok(token: str, stored: dict) -> bool:
    expires, _, signature = (token or "").partition(".")
    return (expires.isdigit() and int(expires) > time.time()
            and hmac.compare_digest(signature.encode(), _sign(expires, stored).encode()))


def _address(request: Request) -> str:
    # The console binds to loopback, so a request reaches it only from this machine or
    # through cloudflared. Cloudflare overwrites this header, and a client cannot forge it.
    return request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else "unknown")


def _recent(fails: deque) -> deque:
    while fails and fails[0] < time.time() - WINDOW_SECS:
        fails.popleft()
    return fails


def _locked(address: str) -> bool:
    return (len(_recent(_fails_global)) >= MAX_FAILS_GLOBAL
            or len(_recent(_fails_by_address.get(address, deque()))) >= MAX_FAILS_PER_ADDRESS)


def _record_failure(address: str) -> None:
    now = time.time()
    _fails_global.append(now)
    _fails_by_address.setdefault(address, deque()).append(now)


def reset_limits() -> None:
    """Clear the failed-try counters. Tests call this between cases."""
    _fails_by_address.clear()
    _fails_global.clear()


def _is_tunnel_or_remote(request: Request) -> bool:
    host = urlsplit(f"//{request.headers.get('host', '')}").hostname or ""
    return "cf-connecting-ip" in request.headers or host not in LOOPBACK_HOSTS


def _cross_site(request: Request) -> bool:
    """True for a write that another site started.

    SameSite=Lax treats sibling subdomains as the same site, so it does not stop a page on
    another *.parth.party host. The Origin check does."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return False
    origin = request.headers.get("origin")
    return bool(origin) and urlsplit(origin).netloc != request.headers.get("host")


def _login_page(error: str = "", status: int = 200) -> HTMLResponse:
    note = f'<p role="alert">{html.escape(error)}</p>' if error else ""
    return HTMLResponse(status_code=status, headers={"Cache-Control": "no-store"}, content=f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Pipeline</title>
<style>
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 16px;
    box-sizing: border-box; background: #0b0d13; color: #e8eaf0;
    font: 15px/1.5 "Segoe UI", system-ui, sans-serif; }}
  form {{ width: 100%; max-width: 340px; display: grid; gap: 12px; }}
  h1 {{ margin: 0; font-size: 21px; }}
  label {{ color: #8b93a7; font-size: 13px; }}
  input {{ width: 100%; box-sizing: border-box; margin-top: 6px; padding: 10px 12px;
    color: inherit; background: #151923; border: 1px solid #282e3c; border-radius: 8px;
    font-size: 16px; }}
  button {{ padding: 10px; color: #fff; background: #7c6cff; border: 0; border-radius: 8px;
    font-size: 15px; cursor: pointer; }}
  p {{ margin: 0; color: #ff6b6b; font-size: 13px; }}
</style></head>
<body><form method="post" action="/login">
  <h1>Job Pipeline</h1>{note}
  <label>Password
    <input type="password" name="password" autocomplete="current-password" autofocus required>
  </label>
  <button type="submit">Log in</button>
</form></body></html>""")


async def login_form(request: Request) -> Response:
    return _login_page()


async def login_submit(request: Request) -> Response:
    stored = _load()
    if stored is None:
        return _login_page("No password is set. Run scripts/set_console_password.py.", 503)
    address = _address(request)
    if _locked(address):
        return _login_page("Too many tries. Wait 15 minutes.", 429)
    raw = (await request.body())[:4096].decode("utf-8", "replace")
    password = (parse_qs(raw).get("password") or [""])[0]
    # scrypt takes tens of milliseconds of CPU, so it stays off the event loop.
    if not await run_in_threadpool(_password_ok, password, stored):
        _record_failure(address)
        return _login_page("Wrong password.", 401)
    _fails_by_address.pop(address, None)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(COOKIE, _new_token(stored), max_age=SESSION_SECS, httponly=True,
                        samesite="lax", secure="cf-connecting-ip" in request.headers)
    return response


async def logout(request: Request) -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE)
    return response


async def gate(request: Request, call_next) -> Response:
    """Middleware: every request passes here before any route, static file, or API docs."""
    if _cross_site(request):
        return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
    stored = _load()
    if stored is None:
        if _is_tunnel_or_remote(request):
            return JSONResponse({"detail": "console password is not set"}, status_code=503)
        return await call_next(request)
    if request.url.path not in LOGIN_PATHS and not _token_ok(
            request.cookies.get(COOKIE, ""), stored):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "log in first"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    response = await call_next(request)
    # Cloudflare caches static file types by default, and a cached asset would reach
    # visitors without a session.
    response.headers["Cache-Control"] = "private, no-cache"
    return response
