#!/usr/bin/env python3
"""Start the job pipeline: the apply bridge + the web console. One command, one file.

    python start.py                 # bridge (:8765) + console at http://127.0.0.1:8000
    python start.py --dev           # same, but Vite on :5173 with frontend hot-reload
    python start.py --no-bridge     # console only (no Telegram / extension control)
    python start.py --inline        # stream all output into THIS terminal instead

DEFAULT (prod) mode runs ONE web server: the SPA is built once into web/frontend/dist and
FastAPI serves both the API and the UI on :8000. No Node process is left running. The
build is skipped when dist/ is newer than everything in web/frontend/src, so a normal
start costs nothing; touch a source file (or pass --build) and it rebuilds first.

--dev mode instead runs uvicorn --reload on :8000 and the Vite dev server on :5173
(proxying /api to :8000). Open :5173 in that mode. Slower to start, but edits to the
frontend appear instantly - use it when working ON the console, not when using it.

On Windows each service gets its OWN console window (titled, and kept open on a crash so
the error stays readable). This launcher window then just parks - Ctrl+C here (or closing
it) stops all of them. --inline streams everything into one terminal instead, prefixed by
service, which is the default off Windows.

The bridge is OPTIONAL: it needs TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID (from the
environment or bridge/.env). Without them it is skipped with a note - the console still
works, but the run controls (Collect / Send approved / Submit) need it, since submitting
is only ever done by the extension.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
WEB = REPO_ROOT / "web"
FRONTEND = WEB / "frontend"
DIST = FRONTEND / "dist"
BRIDGE = REPO_ROOT / "bridge" / "bridge.py"
BRIDGE_ENV = REPO_ROOT / "bridge" / ".env"

IS_WIN = os.name == "nt"
NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
PY = sys.executable


# ---------------------------------------------------------------- frontend build ---

def _newest_mtime(root: Path, *extra: Path) -> float:
    """Newest mtime under `root` plus any explicit files. Used to decide if dist is stale."""
    times = [p.stat().st_mtime for p in root.rglob("*") if p.is_file()]
    times += [p.stat().st_mtime for p in extra if p.exists()]
    return max(times, default=0.0)


def _dist_is_stale() -> bool:
    """dist/ is stale if it's missing, or if any frontend source is newer than it.
    package.json/vite.config.js count as sources: changing deps or the build config
    changes the output too."""
    index = DIST / "index.html"
    if not index.exists():
        return True
    src_time = _newest_mtime(
        FRONTEND / "src",
        FRONTEND / "index.html",
        FRONTEND / "package.json",
        FRONTEND / "vite.config.js",
    )
    return src_time > index.stat().st_mtime


def _npm(args: str, why: str) -> bool:
    print(f"[start] {why}: npm {args} ...")
    rc = subprocess.run(f"npm {args}", cwd=str(FRONTEND), shell=True).returncode
    if rc != 0:
        print(f"[start] npm {args} failed (exit {rc})", file=sys.stderr)
        return False
    return True


def _ensure_frontend(need_build: bool, force_build: bool) -> bool:
    """Install deps on first run; build the SPA when prod mode needs a fresh dist."""
    if not (FRONTEND / "node_modules").exists():
        if not _npm("install", "first run, installing frontend deps"):
            return False
    if need_build and (force_build or _dist_is_stale()):
        why = "rebuilding the console UI" if DIST.exists() else "building the console UI"
        if not _npm("run build", why):
            return False
    return True


# --------------------------------------------------------------------- services ---

def _bridge_creds_present() -> bool:
    """The bridge sys.exit()s without both Telegram vars. They can come from the
    environment or from bridge/.env (which the bridge loads itself). Presence of the file
    is enough - we don't parse it; the bridge reports if it's incomplete."""
    if os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() and \
       os.environ.get("TELEGRAM_CHAT_ID", "").strip():
        return True
    return BRIDGE_ENV.exists()


def _services(dev: bool, want_bridge: bool) -> list[dict]:
    """The processes to run. `core` services tear the rest down when they exit; the
    bridge doesn't - losing it costs the run controls, not the console."""
    api = [PY, "-m", "uvicorn", "web.app.main:app", "--host", "127.0.0.1", "--port", "8000"]
    svc: list[dict] = []
    if dev:
        svc.append({"tag": "api", "title": "Job Pipeline - API (:8000)", "core": True,
                    "cwd": REPO_ROOT, "args": api + ["--reload"], "shell": None,
                    "url": "http://127.0.0.1:8000", "open": False})
        svc.append({"tag": "ui", "title": "Job Pipeline - Frontend (:5173)", "core": True,
                    "cwd": FRONTEND, "args": None, "shell": "npm run dev",
                    "url": "http://127.0.0.1:5173", "open": True})
    else:
        svc.append({"tag": "web", "title": "Job Pipeline - Console (:8000)", "core": True,
                    "cwd": REPO_ROOT, "args": api, "shell": None,
                    "url": "http://127.0.0.1:8000", "open": True})
    if want_bridge and _bridge_creds_present():
        svc.append({"tag": "brg", "title": "Job Pipeline - Bridge (:8765)", "core": False,
                    "cwd": REPO_ROOT, "args": [PY, str(BRIDGE)], "shell": None,
                    "url": "ws://127.0.0.1:8765", "open": False})
    return svc


def _banner(svc: list[dict], mode: str, dev: bool, want_bridge: bool) -> None:
    line = "=" * 64
    print(f"\n{line}\n  Job Pipeline  ({'dev' if dev else 'prod'}, {mode})\n{line}")
    for s in svc:
        print(f"  {s['tag']:<4} {s['url']:<28}{'  <- open this' if s['open'] else ''}")
    if not any(s["tag"] == "brg" for s in svc):
        why = "--no-bridge" if not want_bridge else \
              "set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID (env or bridge/.env)"
        print(f"  brg  skipped - {why}")
    print(line)
    if mode == "windows":
        print(f"  {len(svc)} window(s) opened. Keep THIS window - Ctrl+C here stops them all.")
    else:
        print("  Ctrl+C stops everything.")
    print(f"{line}\n")


def _kill(proc) -> None:
    if proc.poll() is not None:
        return
    try:
        if IS_WIN:
            # npm/uvicorn/cmd spawn children; /T kills the whole tree.
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            proc.terminate()
    except Exception:
        pass


# ------------------------------------------------- separate-console launcher (Win) ---

def _write_launch_script(s: dict, scripts: Path) -> Path:
    """One .cmd per service: set the window title, run it, and pause on exit so a crash's
    error text stays on screen instead of the window vanishing."""
    if s["shell"]:
        command = s["shell"]
        if s["shell"].startswith("npm"):
            command = "call " + s["shell"]   # npm is npm.cmd; call returns control to us
    else:
        command = subprocess.list2cmdline(s["args"])
    script = scripts / f"{s['tag']}.cmd"
    script.write_text(
        "@echo off\r\n"
        f"title {s['title']}\r\n"
        f'cd /d "{s["cwd"]}"\r\n'
        f"{command}\r\n"
        "echo.\r\n"
        f"echo [start] {s['tag']} exited with code %errorlevel%. "
        "Window kept open so you can read any error - close it when done.\r\n"
        "pause >nul\r\n",
        encoding="ascii", errors="replace",
    )
    return script


def _run_windows(svc: list[dict], dev: bool, want_bridge: bool) -> int:
    scripts = Path(tempfile.mkdtemp(prefix="jobpipe-"))
    procs = []
    try:
        for s in svc:
            script = _write_launch_script(s, scripts)
            procs.append(subprocess.Popen(["cmd", "/c", str(script)],
                                          creationflags=NEW_CONSOLE))
        _banner(svc, "windows", dev, want_bridge)
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
    finally:
        for p in procs:
            _kill(p)
        shutil.rmtree(scripts, ignore_errors=True)
        print("\n[start] stopped all windows.")
    return 0


# ------------------------------------------- inline launcher (POSIX, or --inline) ---

def _stream(proc, tag: str) -> None:
    """Prefix each child's output so the services' lines are told apart."""
    for line in proc.stdout:
        sys.stdout.write(f"[{tag:>3}] {line}")
        sys.stdout.flush()


def _run_inline(svc: list[dict], dev: bool, want_bridge: bool) -> int:
    core, optional = [], []
    for s in svc:
        p = subprocess.Popen(
            s["shell"] or s["args"], cwd=str(s["cwd"]), shell=bool(s["shell"]),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            creationflags=NEW_GROUP,
        )
        (core if s["core"] else optional).append((s["tag"], p))
    for tag, p in core + optional:
        threading.Thread(target=_stream, args=(p, tag), daemon=True).start()
    _banner(svc, "inline", dev, want_bridge)
    try:
        while True:
            for tag, p in core:
                if p.poll() is not None:
                    print(f"\n[start] {tag} exited ({p.returncode}) - stopping the rest.")
                    raise KeyboardInterrupt
            for tag, p in list(optional):       # bridge exit is not fatal
                if p.poll() is not None:
                    print(f"\n[start] {tag} exited ({p.returncode}) - continuing without "
                          "it; run controls (Collect / Send / Submit) are unavailable.")
                    optional.remove((tag, p))
            time.sleep(0.4)
    except KeyboardInterrupt:
        pass
    finally:
        for _, p in core + optional:
            _kill(p)
        print("[start] stopped.")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    for a in argv:
        if a not in ("--dev", "--no-bridge", "--inline", "--build", "-h", "--help"):
            print(f"unknown option {a}\n\n{__doc__}", file=sys.stderr)
            return 2
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return 0

    dev = "--dev" in argv
    want_bridge = "--no-bridge" not in argv
    inline = "--inline" in argv or not IS_WIN

    # prod serves the built SPA from FastAPI, so it needs a fresh dist/; dev serves from
    # Vite and needs only node_modules.
    if not _ensure_frontend(need_build=not dev, force_build="--build" in argv):
        return 1

    svc = _services(dev, want_bridge)
    return _run_inline(svc, dev, want_bridge) if inline \
        else _run_windows(svc, dev, want_bridge)


if __name__ == "__main__":
    sys.exit(main())
