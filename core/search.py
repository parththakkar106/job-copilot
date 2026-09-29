"""The career paths and levels to search, and the resume tracks that follow from them.

filters.yaml holds the catalog: `paths` (title keywords per career path) and `levels`
(title words per experience level). search.yaml holds your picks from the console's Search
page. Each path is also a resume track, so the paths you pick decide which resumes the
profile asks for.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from core import paths


def load(filt: dict, path: Path | None = None) -> dict:
    """Your saved picks, with the catalog defaults for any list you have not saved.

    Keys the catalog no longer has are dropped, so a renamed path cannot break a run.
    """
    path = Path(path or paths.SEARCH)
    saved = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}
    out = {}
    for kind in ("paths", "levels"):
        catalog = filt.get(kind) or {}
        picked = saved.get(kind)
        if picked is None:
            picked = [k for k, v in catalog.items() if v.get("default")]
        out[kind] = [k for k in picked if k in catalog]
    return out


def save(sel: dict, path: Path | None = None) -> None:
    path = Path(path or paths.SEARCH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"paths": sel["paths"], "levels": sel["levels"]},
                                   sort_keys=False), encoding="utf-8")


def fields(filt: dict, sel: dict) -> dict[str, list[str]]:
    """Title keywords per chosen path, in catalog order, which is also priority order.

    A filters.yaml from before paths existed has a flat `fields` map instead, and is used
    as is.
    """
    if not filt.get("paths"):
        return filt.get("fields") or {}
    return {k: list(p.get("keywords") or []) for k, p in filt["paths"].items()
            if k in sel["paths"]}


def seniority(filt: dict, sel: dict) -> dict:
    """The scraper's seniority config, with the level words and the chosen levels added."""
    cfg = dict(filt.get("seniority") or {})
    if filt.get("levels"):
        cfg["levels"] = {k: v.get("titles") or [] for k, v in filt["levels"].items()}
        cfg["keep"] = list(sel["levels"])
    return cfg


def tracks(filt: dict, sel: dict) -> list[str]:
    """The resume track tags for the chosen paths, in catalog order."""
    return list(fields(filt, sel))


def new_track(tag: str, path: dict) -> dict:
    """A resume track for a path, with the path's resume keywords and no file yet."""
    return {"tag": tag, "title_prefix": f"Resume_{tag}_", "drive_id": "",
            "current_file": "FILL_ME", "local_source": "FILL_ME",
            "local_path": f"resumes/{tag}.pdf",
            "keywords": list(path.get("resume_keywords") or path.get("keywords") or [])}


def apply_tracks(profile: dict, tags: list[str], catalog: dict) -> dict:
    """Keep a track for each chosen path, add the missing ones, and drop the rest.

    Only tracks named after a catalog path are added or dropped. A track you named yourself
    is kept, because no path can select it. An existing track keeps its file and keywords.
    A dropped track's PDF stays in the resume cache, so picking the path again brings the
    same file back.
    """
    current = profile.get("resume_tracks") or []
    have = {t.get("tag") for t in current}
    kept = [t for t in current if t.get("tag") in tags or t.get("tag") not in catalog]
    added = [new_track(tag, catalog[tag]) for tag in tags if tag in catalog and tag not in have]
    profile["resume_tracks"] = kept + added
    left = [t["tag"] for t in profile["resume_tracks"]]
    if left and profile.get("default_resume_tag") not in left:
        profile["default_resume_tag"] = left[0]
    return profile
