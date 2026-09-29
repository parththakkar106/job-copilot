#!/usr/bin/env python3
"""Start the job pipeline: the apply bridge + the web console. One command, one file.

    python start.py                 # bridge (:8765) + console at http://127.0.0.1:8000
    python start.py --dev           # same, but Vite on :5173 with frontend hot-reload
    python start.py --no-bridge     # console only (no Telegram / extension control)
    python start.py --inline        # stream all output into THIS terminal instead
    python start.py --stop          # stop the running services
    python start.py --status        # show what is running

DEFAULT (prod) mode runs ONE web server: FastAPI serves both the API and the prebuilt UI
in web/frontend/dist on :8000. dist/ is committed, so this mode needs no Node. If Node is
installed and a source file is newer than dist/ (or you pass --build), it rebuilds first.

--dev mode instead runs uvicorn --reload on :8000 and the Vite dev server on :5173
(proxying /api to :8000). Open :5173 in that mode. Slower to start, but edits to the
frontend appear instantly - use it when working ON the console, not when using it.

Every start first stops any copy of these services that is already running: the owners
of their ports, processes with the same command line, and launchers from earlier runs.
A second bridge cannot bind :8765, so without this step it exits while the old bridge
keeps serving old code.

On Windows the services run hidden, with no console windows, and this launcher exits
once they are listening. Their output goes to `<logs>/bridge.log` and
`<logs>/console.log`, and the previous run's log is kept as `*.prev.log`. --inline streams
everything into one terminal instead, prefixed by service, and is the default off Windows.

The bridge is OPTIONAL: it needs TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID (from the
environment or bridge/.env). Without them it is skipped with a note - the console still
works, but the run controls (Collect / Send approved / Submit) need it, since submitting
is only ever done by the extension.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))
from core import paths  # noqa: E402

LOGS = paths.LOGS
WEB = REPO_ROOT / "web"
FRONTEND = WEB / "frontend"
DIST = FRONTEND / "dist"
BRIDGE = REPO_ROOT / "bridge" / "bridge.py"
BRIDGE_ENV = REPO_ROOT / "bridge" / ".env"

IS_WIN = os.name == "nt"
NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BREAKAWAY = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
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
    """Install deps on first run; build the SPA when prod mode needs a fresh dist.

    dist/ is committed, so a user without Node runs the prebuilt UI. Node is needed only
    for --dev, --build, or to rebuild after editing web/frontend/src.
    """
    building = need_build and (force_build or _dist_is_stale())
    if need_build and not building:
        return True
    if not shutil.which("npm"):
        # A fresh clone gives every file a new mtime, so dist/ can look stale when it is not.
        if need_build and not force_build and (DIST / "index.html").exists():
            print("[start] Node not found, using the prebuilt console UI")
            return True
        print("[start] npm not found. Install Node 18+ to use --dev or --build.",
              file=sys.stderr)
        return False
    if not (FRONTEND / "node_modules").exists():
        if not _npm("install", "first run, installing frontend deps"):
            return False
    if building:
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
        svc.append({"tag": "api", "log": "console", "port": 8000, "core": True,
                    "cwd": REPO_ROOT, "args": api + ["--reload"], "shell": None,
                    "url": "http://127.0.0.1:8000", "open": False})
        svc.append({"tag": "ui", "log": "frontend", "port": 5173, "core": True,
                    "cwd": FRONTEND, "args": None, "shell": "npm run dev",
                    "url": "http://127.0.0.1:5173", "open": True})
    else:
        svc.append({"tag": "web", "log": "console", "port": 8000, "core": True,
                    "cwd": REPO_ROOT, "args": api, "shell": None,
                    "url": "http://127.0.0.1:8000", "open": True})
    if want_bridge and _bridge_creds_present():
        svc.append({"tag": "brg", "log": "bridge", "port": 8765, "core": False,
                    "cwd": REPO_ROOT, "args": [PY, str(BRIDGE)], "shell": None,
                    "url": "ws://127.0.0.1:8765", "open": False})
    return svc


# ------------------------------------------------------- stop what already runs ---

# Every port start.py can serve on. The stop step clears all of them regardless of mode,
# so switching between --dev and prod does not leave the other mode's server behind.
PORTS = (8000, 5173, 8765)


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _win_processes() -> tuple[list[dict], dict[int, int]]:
    """Every process with its command line, and the owner PID of each of PORTS.

    One PowerShell call answers both, so a start pays the ~1s PowerShell startup once."""
    ports = ",".join(str(p) for p in PORTS)
    script = (
        "$p = @(Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CommandLine);"
        f"$l = @(Get-NetTCPConnection -State Listen -LocalPort {ports} -ErrorAction SilentlyContinue |"
        " Select-Object LocalPort,OwningProcess);"
        "@{procs=$p; listen=$l} | ConvertTo-Json -Depth 3 -Compress"
    )
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                         capture_output=True, text=True, creationflags=NO_WINDOW).stdout
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError:
        return [], {}
    procs = data.get("procs") or []
    listen = data.get("listen") or []
    if isinstance(listen, dict):
        listen = [listen]
    return procs, {int(c["LocalPort"]): int(c["OwningProcess"]) for c in listen}


def _is_ours(proc: dict) -> str | None:
    """Name the service a process belongs to, or None if it is not one of ours.

    Matching is on this repo's own command lines, so an unrelated python.exe or node.exe is
    never touched."""
    cmd = (proc.get("CommandLine") or "").replace("/", "\\").casefold()
    name = (proc.get("Name") or "").casefold()
    root = str(REPO_ROOT).casefold()
    if str(BRIDGE).casefold() in cmd:
        return "bridge"
    if "web.app.main:app" in cmd and "--port 8000" in cmd:
        return "console"
    if name.startswith("python") and "start.py" in cmd and root in cmd:
        return "launcher"
    if name == "node.exe" and root in cmd:
        return "frontend"
    # The .cmd wrappers the old window-per-service launcher wrote to %TEMP%\jobpipe-*.
    if name == "cmd.exe" and "\\jobpipe-" in cmd:
        return "old window"
    return None


def _running() -> list[tuple[int, str]]:
    """(pid, what) for every process start.py should treat as a running copy.

    A port owner counts only when it or one of its ancestors is ours. Another project
    serving on :8000 is reported and left alone, and the start then fails on the busy port."""
    if not IS_WIN:
        return []
    procs, owners = _win_processes()
    by_pid = {int(p.get("ProcessId") or 0): p for p in procs}
    me = {os.getpid(), os.getppid()}

    def ours_or_ancestor(pid: int) -> str | None:
        seen = set()
        while pid in by_pid and pid not in seen:
            seen.add(pid)
            what = _is_ours(by_pid[pid])
            if what:
                return what
            pid = int(by_pid[pid].get("ParentProcessId") or 0)
        return None

    found: dict[int, str] = {}
    for port, pid in owners.items():
        if pid in me:
            continue
        if ours_or_ancestor(pid):
            found[pid] = f"listening on :{port}"
        else:
            proc_name = (by_pid.get(pid) or {}).get("Name") or "?"
            print(f"[start] :{port} is held by pid {pid} ({proc_name}), which is not part of "
                  "this project. Leaving it running.", file=sys.stderr)
    for pid, p in by_pid.items():
        what = _is_ours(p)
        if what and pid not in me:
            found.setdefault(pid, what)
    return sorted(found.items())


def _stop_running(quiet: bool = False) -> bool:
    """Kill every running copy, then wait for the ports to free. True when they are free."""
    running = _running()
    for pid, what in running:
        if not quiet:
            print(f"[start] stopping pid {pid} ({what})")
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not IS_WIN:
        return True
    deadline = time.time() + 15
    while time.time() < deadline:
        busy = [p for p in PORTS if _port_open(p)]
        if not busy:
            return True
        time.sleep(0.5)
    print(f"[start] ports still in use after the stop: {busy}", file=sys.stderr)
    return False


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
    if mode == "hidden":
        print(f"  Running hidden. Logs: {LOGS}")
        print("  Stop with: python start.py --stop")
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


# ------------------------------------------------------- hidden launcher (Win) ---

def _open_log(name: str):
    """Open `<logs>/<name>.log` for a fresh run, keeping the last run's as `.prev.log`."""
    LOGS.mkdir(parents=True, exist_ok=True)
    log, prev = LOGS / f"{name}.log", LOGS / f"{name}.prev.log"
    try:
        if log.exists():
            os.replace(log, prev)
    except OSError:
        pass          # a log another process still holds open is appended to instead
    return open(log, "a", encoding="utf-8", errors="replace")


def _spawn_hidden(s: dict) -> subprocess.Popen:
    """Start one service with no window, detached from this launcher.

    CREATE_NO_WINDOW gives the service a hidden console of its own, which child console
    programs (npm, node) inherit instead of opening visible windows. Breakaway lets the
    service outlive a Task Scheduler job that ends when run_daily.ps1 exits. Not every
    job allows breakaway, so a refused breakaway retries without it."""
    log = _open_log(s["log"])
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}    # otherwise the log stays empty
    kwargs = dict(cwd=str(s["cwd"]), shell=bool(s["shell"]), env=env,
                  stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    try:
        return subprocess.Popen(s["shell"] or s["args"],
                                creationflags=NO_WINDOW | NEW_GROUP | BREAKAWAY, **kwargs)
    except OSError:
        return subprocess.Popen(s["shell"] or s["args"],
                                creationflags=NO_WINDOW | NEW_GROUP, **kwargs)
    finally:
        log.close()       # the child holds its own handle


def _run_hidden(svc: list[dict], dev: bool, want_bridge: bool) -> int:
    procs = [(s, _spawn_hidden(s)) for s in svc]
    deadline = time.time() + 45
    pending = {s["tag"] for s in svc}
    while pending and time.time() < deadline:
        for s, p in procs:
            if s["tag"] not in pending:
                continue
            if _port_open(s["port"]):
                pending.discard(s["tag"])
            elif p.poll() is not None:
                print(f"[start] {s['tag']} exited ({p.returncode}) during startup. "
                      f"See {LOGS / (s['log'] + '.log')}", file=sys.stderr)
                pending.discard(s["tag"])
        time.sleep(0.5)
    down = [s for s, _ in procs if not _port_open(s["port"])]
    _banner(svc, "hidden", dev, want_bridge)
    for s in down:
        print(f"[start] {s['tag']} is NOT listening on :{s['port']}. "
              f"See {LOGS / (s['log'] + '.log')}", file=sys.stderr)
    return 1 if any(s["core"] or s["tag"] == "brg" for s in down) else 0


def _status() -> int:
    for port, name in ((8765, "bridge"), (8000, "console"), (5173, "frontend (dev)")):
        print(f"  {name:<15} :{port}  {'up' if _port_open(port) else 'down'}")
    for pid, what in _running():
        print(f"  pid {pid:<8} {what}")
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
        if a not in ("--dev", "--no-bridge", "--inline", "--build", "--stop", "--status",
                     "-h", "--help"):
            print(f"unknown option {a}\n\n{__doc__}", file=sys.stderr)
            return 2
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return 0
    if "--status" in argv:
        return _status()
    if "--stop" in argv:
        return 0 if _stop_running() else 1

    dev = "--dev" in argv
    want_bridge = "--no-bridge" not in argv
    inline = "--inline" in argv or not IS_WIN

    # prod serves the built SPA from FastAPI, so it needs a fresh dist/; dev serves from
    # Vite and needs only node_modules.
    if not _ensure_frontend(need_build=not dev, force_build="--build" in argv):
        return 1

    # Stop after the build, so the old console keeps serving while npm runs.
    if not _stop_running():
        return 1

    svc = _services(dev, want_bridge)
    return _run_inline(svc, dev, want_bridge) if inline \
        else _run_hidden(svc, dev, want_bridge)


if __name__ == "__main__":
    sys.exit(main())
