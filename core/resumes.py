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

Syncing compares CONTENT HASHES, so it copies only what changed and re-running it is
free. The bridge calls `sync_local()` directly — on startup, before a queue run, and
before it hands the extension a resume's bytes — which is what keeps an edited PDF from
needing any command at all.

Cache + provenance live in resumes/manifest.json. Update a resume by uploading a higher
_vN to Drive, then re-run the Claude-driven sync (see SYNC.md / project notes).
"""
from __future__ import annotations
import argparse, base64, hashlib, json, sys
from datetime import date
from pathlib import Path

from core import paths
from core import profile as P  # reuse profile.json loader + pick_resume

CACHE = paths.RESUMES
MANIFEST = CACHE / "manifest.json"


def cached_path(track: dict) -> Path:
    """Where this track's PDF lives, from its profile `local_path`.

    Only the FILENAME is taken from local_path; the directory is always paths.RESUMES.
    The two are the same thing until JOBCOPILOT_HOME is set, and then they are not: this
    module used to resolve "resumes/ai.pdf" against the repo root while paths.RESUMES —
    which build.py and the bridge both read — moved to HOME. The sync wrote to one
    directory and every reader looked in the other.
    """
    return CACHE / Path(track.get("local_path") or "").name


def _tracks() -> dict[str, dict]:
    return {t["tag"]: t for t in P.load_profile()["resume_tracks"]}


def load_manifest() -> dict:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {}


def save_manifest(m: dict):
    CACHE.mkdir(exist_ok=True)
    MANIFEST.write_text(json.dumps(m, indent=2), encoding="utf-8")


class SyncError(Exception):
    """A sync that cannot proceed: no source folder configured, a missing file, bad bytes.

    An exception rather than sys.exit because the bridge calls sync_local() on a live
    connection — exiting the process there would take the whole control plane down over a
    resume nobody asked for yet.
    """


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def cached_hash(track: dict) -> str | None:
    """The hash of the bytes currently in the cache, or None if nothing is cached.

    Read from the FILE, not from the manifest. The manifest is a record of the last sync
    and can disagree with the disk — a file replaced by hand, a half-written copy — and a
    sync that trusts it would then skip the one track that actually needs copying.
    """
    dest = cached_path(track)
    if not dest.is_file():
        return None
    return sha256(dest.read_bytes())


def _write_cache(track: dict, raw: bytes, source_file: str | None) -> bool:
    """Install `raw` as this track's cached PDF. True if the bytes changed.

    Identical bytes are not rewritten. Every application reads this cache, so a sync that
    copied unconditionally would move `synced` forward on files nobody touched and make
    the date useless as an answer to "is this current".
    """
    if raw[:5] != b"%PDF-":
        raise SyncError(f"refusing to install: not a PDF (header {raw[:5]!r})")
    digest = sha256(raw)
    if cached_hash(track) == digest:
        return False
    dest = cached_path(track)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    m = load_manifest()
    m[track["tag"]] = {
        "drive_id": track.get("drive_id", ""),
        "current_file": source_file or track["current_file"],
        "local_path": track["local_path"],
        "bytes": len(raw),
        "sha256": digest,
        "synced": date.today().isoformat(),
    }
    save_manifest(m)
    return True


def sync_local(tag: str | None = None, profile: dict | None = None) -> list[str]:
    """Copy changed resumes from the local source folder into the cache.

    Returns the tags whose bytes actually changed, so a caller can restage only when
    something moved. Compares content hashes, so re-running it costs four file reads and
    writes nothing. Raises SyncError if the source folder or a source file is missing.
    """
    prof = profile if profile is not None else P.load_profile()
    src = prof.get("_local_resume_source")
    if not src:
        raise SyncError("profile.json has no _local_resume_source block")
    base_dir = src.get("base_dir")
    # An unfilled profile carries the FILL_ME placeholder rather than omitting the key, so
    # a plain truthiness check passes and the failure surfaces later as a nonsense path
    # ("missing source for 'quant': FILL_ME\\FILL_ME"). Say what to fill in instead.
    if not base_dir or base_dir == P.FILL_ME:
        raise SyncError(
            "profile.json `_local_resume_source.base_dir` is unset — point it at the folder\n"
            "holding your resume PDFs, then re-run. See `python -m core.profile checkfill`.")
    base = Path(base_dir)
    if not base.is_dir():
        raise SyncError(f"resume source folder does not exist: {base}")

    tracks = {t["tag"]: t for t in prof["resume_tracks"]}
    tags = [tag] if tag else list(tracks)
    changed = []
    for t in tags:
        track = tracks.get(t)
        if track is None:
            raise SyncError(f"unknown track '{t}'")
        rel = track.get("local_source")
        if not rel or rel == P.FILL_ME:
            raise SyncError(
                f"resume track '{t}' has no `local_source` — set it to that track's PDF\n"
                f"filename inside {base}, then re-run.")
        srcfile = base / rel
        if not srcfile.exists():
            raise SyncError(f"missing source for '{t}': {srcfile}")
        if _write_cache(track, srcfile.read_bytes(), srcfile.name):
            changed.append(t)
    return changed


def read_cached(track: dict) -> bytes:
    """The cached PDF's bytes. Raises SyncError if the track was never cached."""
    dest = cached_path(track)
    if not dest.is_file():
        raise SyncError(f"'{track.get('tag')}' is not cached — run sync-local")
    return dest.read_bytes()


def cmd_install(a):
    track = _tracks().get(a.tag) or sys.exit(f"unknown track '{a.tag}'")
    try:
        if a.from_dump:
            dump = json.loads(Path(a.from_dump).read_text(encoding="utf-8"))
            raw = base64.b64decode(dump["content"])
            changed = _write_cache(track, raw, dump.get("title"))
        elif a.file:
            raw = Path(a.file).read_bytes()
            changed = _write_cache(track, raw, Path(a.file).name)
        else:
            sys.exit("need --from <dump.txt> or --file <pdf>")
    except SyncError as e:
        sys.exit(str(e))
    verb = "installed" if changed else "unchanged"
    print(f"{verb} {a.tag}: {len(raw)} bytes -> {track['local_path']}")


def cmd_sync_local(a):
    """TEMP: copy resumes from the local Finals folder into the cache instead of Drive."""
    try:
        changed = sync_local(a.tag)
    except SyncError as e:
        sys.exit(str(e))
    if changed:
        for tag in changed:
            print(f"updated {tag}")
    else:
        print("everything already current — nothing copied")


def cmd_status(a):
    m = load_manifest()
    local = P.load_profile().get("_local_resume_source")
    if local:
        print(f"# TEMP local-source mode: {local['base_dir']} (not Drive)")
    for tag, t in _tracks().items():
        cached = cached_path(t).is_file()
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
    exists = cached_path(r).is_file()
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
