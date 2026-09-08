#!/usr/bin/env python3
"""Stage the shared data layer into the extension folder.

The extension can't read the repo's YAML or reach files outside its own dir, so we
copy/convert the data layer into extension/ at build time:
  taxonomy.yaml  -> extension/taxonomy.json   (browsers can't parse YAML)
  profile.json   -> extension/profile.json    (verbatim copy)
  resumes/*.pdf  -> extension/resumes/*.pdf    (bytes for DataTransfer attach)

Run from the repo root:  python extension/build.py
Re-run whenever profile.json / taxonomy.yaml / resumes change. Bundled copies are
gitignored (see .gitignore) so we don't duplicate the source of truth in git.
"""
import json
import shutil
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from core import paths  # noqa: E402  — single source of truth for the data-layer layout
from server.store import get_store  # noqa: E402

EXT = ROOT / "extension"


def main() -> int:
    # taxonomy.yaml -> taxonomy.json
    tax = yaml.safe_load((paths.TAXONOMY).read_text(encoding="utf-8"))
    (EXT / "taxonomy.json").write_text(
        json.dumps(tax, indent=2, ensure_ascii=False), encoding="utf-8")
    print("taxonomy.yaml -> extension/taxonomy.json")

    # profile.json verbatim, warning loudly about FILL_ME placeholders that will ship
    # (they escalate to Telegram per-form; better to know at build time). Non-fatal:
    # links.github is FILL_ME by deliberate choice.
    profile = get_store().get_profile()

    def _fillme(node, prefix=""):
        holes = []
        if isinstance(node, dict):
            for k, v in node.items():
                holes += _fillme(v, f"{prefix}.{k}" if prefix else k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                holes += _fillme(v, f"{prefix}[{i}]")
        elif node == "FILL_ME":
            holes.append(prefix)
        return holes

    holes = _fillme(profile)
    if holes:
        print(f"  ! {len(holes)} FILL_ME placeholder(s) ship with the extension: "
              f"{', '.join(holes)}", file=sys.stderr)
    (EXT / "profile.json").write_text(
        json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
    print("profile.json -> extension/profile.json")

    # Resume bytes, from the store, falling back to the local cache for a sync that ran
    # before the store existed. The extension needs real files on this machine: it attaches
    # them by DataTransfer injection, which reads the bytes it was built with.
    store = get_store()
    dst_resumes = EXT / "resumes"
    dst_resumes.mkdir(exist_ok=True)
    copied = 0
    for track in profile.get("resume_tracks", []):
        name = Path(track["local_path"]).name
        row = store.get_resume(str(track.get("tag") or ""))
        raw = row.get("pdf") if row else None
        if raw is None:
            src = paths.RESUMES / name
            if not src.exists():
                print(f"  ! missing resume {name} — run `python -m core.resumes sync-local`"
                      " first", file=sys.stderr)
                continue
            raw = src.read_bytes()
        (dst_resumes / name).write_bytes(raw)
        copied += 1
    print(f"resumes -> extension/resumes/ ({copied} files)")

    # Chrome refuses to load an unpacked extension if ANY top-level file/dir starts
    # with "_" (reserved). Python's __pycache__ landing here (from a stray import/test
    # run) blocks the load, so sweep any "_"-prefixed dirs on the way out.
    for stray in EXT.glob("_*"):
        if stray.is_dir():
            shutil.rmtree(stray, ignore_errors=True)
            print(f"  removed reserved dir {stray.name}/ (Chrome won't load with it present)")

    print("build complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
