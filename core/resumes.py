#!/usr/bin/env python3
"""
resume cache manager — Drive is the source of truth; resumes/ is the local byte cache
the Apply extension reads at attach-time (DataTransfer injection needs real bytes).

Because the standalone script can't authenticate to Drive, the actual download is done by
Claude (which holds the Google Drive connection): Claude calls download_file_content for
the latest `<title_prefix>vN.pdf` per track, then feeds the base64 dump here:

  python resumes.py install --tag quant --from <drive-download-dump.txt>   # base64 JSON
  python resumes.py install --tag quant --file already_decoded.pdf         # or a raw pdf
  python resumes.py sync-local [--tag quant]   # TEMP: copy from local Finals folder -> cache
  python resumes.py status                 # what's cached vs the manifest / profile
  python resumes.py pick --field "Quantitative Researcher" [--jd jd.txt]    # track selection

TEMP (2026-07-10): resumes are currently sourced from a LOCAL folder (profile.json
`_local_resume_source.base_dir` + each track's `local_source`), not Drive. Run
`sync-local` to refresh the cache from disk. The Drive path (install --from dump) is
preserved for when we move back.

Cache + provenance live in resumes/manifest.json. Update a resume by uploading a higher
_vN to Drive, then re-run the Claude-driven sync (see SYNC.md / project notes).
"""
from __future__ import annotations
import argparse, base64, json, shutil, sys
from datetime import date
from pathlib import Path

from core import paths
from core import profile as P  # reuse profile.json loader + pick_resume

# profile.json stores each track's `local_path` relative to the REPO ROOT
# ("resumes/Parth_Resume_q.pdf"), so resolve those against the root, not against core/.
HERE = paths.REPO_ROOT
CACHE = paths.RESUMES
MANIFEST = CACHE / "manifest.json"


def _tracks() -> dict[str, dict]:
    return {t["tag"]: t for t in P.load_profile()["resume_tracks"]}


def load_manifest() -> dict:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {}


def save_manifest(m: dict):
    CACHE.mkdir(exist_ok=True)
    MANIFEST.write_text(json.dumps(m, indent=2), encoding="utf-8")


def _write_cache(track: dict, raw: bytes, source_file: str | None):
    if raw[:5] != b"%PDF-":
        sys.exit(f"refusing to install: not a PDF (header {raw[:5]!r})")
    dest = HERE / track["local_path"]
    dest.parent.mkdir(exist_ok=True)
    dest.write_bytes(raw)
    m = load_manifest()
    m[track["tag"]] = {
        "drive_id": track["drive_id"],
        "current_file": source_file or track["current_file"],
        "local_path": track["local_path"],
        "bytes": len(raw),
        "synced": date.today().isoformat(),
    }
    save_manifest(m)
    print(f"installed {track['tag']}: {len(raw)} bytes -> {track['local_path']}")


def cmd_install(a):
    track = _tracks().get(a.tag) or sys.exit(f"unknown track '{a.tag}'")
    if a.from_dump:
        dump = json.loads(Path(a.from_dump).read_text(encoding="utf-8"))
        raw = base64.b64decode(dump["content"])
        _write_cache(track, raw, dump.get("title"))
    elif a.file:
        raw = Path(a.file).read_bytes()
        _write_cache(track, raw, Path(a.file).name)
    else:
        sys.exit("need --from <dump.txt> or --file <pdf>")


def cmd_sync_local(a):
    """TEMP: copy resumes from the local Finals folder into the cache instead of Drive."""
    prof = P.load_profile()
    src = prof.get("_local_resume_source") or sys.exit(
        "profile.json has no _local_resume_source block")
    base_dir = src.get("base_dir")
    # An unfilled profile carries the FILL_ME placeholder rather than omitting the key, so
    # a plain truthiness check passes and the failure surfaces later as a nonsense path
    # ("missing source for 'quant': FILL_ME\\FILL_ME"). Say what to fill in instead.
    if not base_dir or base_dir == P.FILL_ME:
        sys.exit(
            "profile.json `_local_resume_source.base_dir` is unset — point it at the folder\n"
            "holding your resume PDFs, then re-run. See `python -m core.profile checkfill`.")
    base = Path(base_dir)
    if not base.is_dir():
        sys.exit(f"resume source folder does not exist: {base}")
    tracks = _tracks()
    tags = [a.tag] if a.tag else list(tracks)
    for tag in tags:
        track = tracks.get(tag) or sys.exit(f"unknown track '{tag}'")
        rel = track.get("local_source")
        if not rel or rel == P.FILL_ME:
            sys.exit(
                f"resume track '{tag}' has no `local_source` — set it to that track's PDF\n"
                f"filename inside {base}, then re-run.")
        srcfile = base / rel
        if not srcfile.exists():
            sys.exit(f"missing source for '{tag}': {srcfile}")
        _write_cache(track, srcfile.read_bytes(), srcfile.name)


def cmd_status(a):
    m = load_manifest()
    local = P.load_profile().get("_local_resume_source")
    if local:
        print(f"# TEMP local-source mode: {local['base_dir']} (not Drive)")
    for tag, t in _tracks().items():
        cached = (HERE / t["local_path"]).exists()
        rec = m.get(tag, {})
        mark = "OK " if cached else "MISSING"
        if local:
            # In local mode, cached filename is the Finals PDF name — drift vs Drive is expected.
            src = t.get("local_source", "?")
            drift = f"  (from {src})" if rec.get("current_file") != t["current_file"] else ""
        else:
            drift = "" if rec.get("current_file") in (None, t["current_file"]) else \
                f"  (drive now {t['current_file']}, cached {rec.get('current_file')})"
        print(f"[{mark}] {tag:<5} {t['current_file']:<28} "
              f"{rec.get('bytes','?')} bytes synced={rec.get('synced','never')}{drift}")


def cmd_pick(a):
    jd = Path(a.jd).read_text(encoding="utf-8") if a.jd else ""
    r = P.pick_resume(a.field, jd)
    exists = (HERE / r["local_path"]).exists()
    r["cached"] = exists
    print(json.dumps(r, indent=2))
    if not exists:
        print(f"  ! {r['local_path']} not in cache — run install", file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install")
    p.add_argument("--tag", required=True)
    p.add_argument("--from", dest="from_dump")
    p.add_argument("--file")
    p = sub.add_parser("sync-local"); p.add_argument("--tag")
    p = sub.add_parser("status")
    p = sub.add_parser("pick"); p.add_argument("--field", required=True); p.add_argument("--jd")
    a = ap.parse_args()
    {"install": cmd_install, "sync-local": cmd_sync_local,
     "status": cmd_status, "pick": cmd_pick}[a.cmd](a)
