#!/usr/bin/env python3
"""Tests for the resume cache: hash-based syncing, and where the files land.

Every application attaches whatever is in this cache, so two failures here are invisible
until an employer already has the wrong PDF: a sync that does not notice an edit, and a
sync that writes to a different directory than the one every reader looks in.

Run:  python test_resume_sync.py
"""
from __future__ import annotations
import sys
import tempfile
from pathlib import Path

from core import paths
from core import resumes as R

PDF_A = b"%PDF-1.4 version A"
PDF_B = b"%PDF-1.4 version B, longer"

TRACKS = [
    {"tag": "quant", "current_file": "old.pdf", "local_source": "Quant.pdf",
     "local_path": "resumes/quant.pdf", "keywords": []},
    {"tag": "ai", "current_file": "old.pdf", "local_source": "AI ML Resume.pdf",
     "local_path": "resumes/ai.pdf", "keywords": []},
]


def fixture(tmp: Path) -> dict:
    """A source folder with both PDFs in it, and the cache pointed somewhere throwaway."""
    src = tmp / "Finals"
    src.mkdir(parents=True)
    (src / "Quant.pdf").write_bytes(PDF_A)
    (src / "AI ML Resume.pdf").write_bytes(PDF_A)
    R.CACHE = tmp / "cache" / "resumes"
    R.MANIFEST = R.CACHE / "manifest.json"
    return {"identity": {"first_name": "Ada", "last_name": "Lovelace"},
            "resume_tracks": [dict(t) for t in TRACKS],
            "_local_resume_source": {"base_dir": str(src)}}


def main() -> int:
    checks = 0
    fails = []

    def check(name, cond):
        nonlocal checks
        checks += 1
        if not cond:
            fails.append(name)

    tmp = Path(tempfile.mkdtemp())
    prof = fixture(tmp)
    src = Path(prof["_local_resume_source"]["base_dir"])

    # --- first sync copies everything -------------------------------------------------
    check("first sync reports both tracks", sorted(R.sync_local(profile=prof)) == ["ai", "quant"])
    check("bytes landed", R.cached_path(prof["resume_tracks"][0]).read_bytes() == PDF_A)

    # --- a second sync copies nothing, because nothing changed -------------------------
    check("re-running a sync is a no-op", R.sync_local(profile=prof) == [])

    # THE POINT OF THE HASH. An edited source must be picked up with no version bump, no
    # rename, and no command: the file is simply different, and that is the whole signal.
    (src / "AI ML Resume.pdf").write_bytes(PDF_B)
    check("an edited PDF is detected", R.sync_local(profile=prof) == ["ai"])
    check("the new bytes are cached",
          R.cached_path(prof["resume_tracks"][1]).read_bytes() == PDF_B)
    check("the untouched track was left alone", R.sync_local(profile=prof) == [])

    # A cache file replaced by hand disagrees with the manifest. The sync must read the
    # FILE, not its own record of it, or it skips exactly the track that needs copying.
    R.cached_path(prof["resume_tracks"][1]).write_bytes(b"%PDF-1.4 tampered")
    check("a hand-edited cache file is restored", R.sync_local(profile=prof) == ["ai"])

    # --- the manifest records what a reader needs -------------------------------------
    m = R.load_manifest()
    check("manifest carries the hash", m["ai"]["sha256"] == R.sha256(PDF_B))
    check("manifest carries the source name", m["ai"]["current_file"] == "AI ML Resume.pdf")
    check("manifest carries a sync date", bool(m["ai"].get("synced")))

    # --- one track at a time ----------------------------------------------------------
    (src / "Quant.pdf").write_bytes(PDF_B)
    check("--tag syncs only that track", R.sync_local("ai", profile=prof) == [])
    check("the other track is still stale", R.sync_local("quant", profile=prof) == ["quant"])

    # --- reading back -----------------------------------------------------------------
    check("read_cached returns the cached bytes",
          R.read_cached(prof["resume_tracks"][0]) == PDF_B)
    R.cached_path(prof["resume_tracks"][0]).unlink()
    try:
        R.read_cached(prof["resume_tracks"][0])
        check("read_cached raises for a missing file", False)
    except R.SyncError:
        check("read_cached raises for a missing file", True)

    # --- failures are exceptions, not sys.exit ----------------------------------------
    # The bridge calls these on a live connection, where exiting kills the control plane.
    for name, bad in [
        ("no source block", {k: v for k, v in prof.items() if k != "_local_resume_source"}),
        ("unset base_dir", {**prof, "_local_resume_source": {"base_dir": "FILL_ME"}}),
        ("missing folder", {**prof, "_local_resume_source": {"base_dir": str(tmp / "nope")}}),
    ]:
        try:
            R.sync_local(profile=bad)
            check(f"{name} raises SyncError", False)
        except R.SyncError:
            check(f"{name} raises SyncError", True)
        except SystemExit:
            check(f"{name} raises SyncError, not SystemExit", False)

    (src / "Quant.pdf").write_bytes(b"not a pdf at all")
    try:
        R.sync_local("quant", profile=prof)
        check("a non-PDF source is refused", False)
    except R.SyncError:
        check("a non-PDF source is refused", True)

    # --- the cache directory follows JOBCOPILOT_HOME ----------------------------------
    # `local_path` is written as "resumes/<name>" against the repo root, but paths.RESUMES
    # moves under HOME. Resolving the whole relative path here would write to the checkout
    # while build.py and the bridge read from HOME, and nothing would report a problem.
    R.CACHE = Path("/somewhere/home/resumes")
    check("the cache directory is paths.RESUMES, not the repo root",
          R.cached_path({"local_path": "resumes/ai.pdf"}) == Path("/somewhere/home/resumes/ai.pdf"))
    check("paths.RESUMES is what the module caches into",
          paths.RESUMES.name == "resumes")

    if fails:
        print(f"FAIL ({len(fails)}):")
        for f in fails:
            print(f"  {f}")
        return 1
    print(f"resume sync: {checks}/{checks} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
