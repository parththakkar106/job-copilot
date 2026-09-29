"""The topics and levels to search, and the resume tracks that follow from them.

filters.yaml holds the catalog: `topics` (keywords grouped by field) and `levels` (title
words per experience level). search.yaml holds your picks from the console's Search page.
Each topic's `field` doubles as a resume track tag, so your topics also decide which
resume tracks the profile keeps.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from core import paths


def load(filt: dict, path: Path | None = None) -> dict:
    """Your saved picks, with the catalog defaults for any list you have not saved.

    Keys the catalog no longer has are dropped, so a renamed topic cannot break a run.
    """
    path = Path(path or paths.SEARCH)
    saved = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}
    out = {}
    for kind in ("topics", "levels"):
        catalog = filt.get(kind) or {}
        picked = saved.get(kind)
        if picked is None:
            picked = [k for k, v in catalog.items() if v.get("default")]
        out[kind] = [k for k in picked if k in catalog]
    return out


def save(sel: dict, path: Path | None = None) -> None:
    path = Path(path or paths.SEARCH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"topics": sel["topics"], "levels": sel["levels"]},
                                   sort_keys=False), encoding="utf-8")


def fields(filt: dict, sel: dict) -> dict[str, list[str]]:
    """Keywords per field, from the chosen topics.

    A filters.yaml from before topics existed has a flat `fields` map instead, and is used
    as is.
    """
    if not filt.get("topics"):
        return filt.get("fields") or {}
    out: dict[str, list[str]] = {}
    for key, topic in filt["topics"].items():
        if key in sel["topics"]:
            out.setdefault(topic["field"], []).extend(topic.get("keywords") or [])
    return out


def seniority(filt: dict, sel: dict) -> dict:
    """The scraper's seniority config, with the level words and the chosen levels added."""
    cfg = dict(filt.get("seniority") or {})
    if filt.get("levels"):
        cfg["levels"] = {k: v.get("titles") or [] for k, v in filt["levels"].items()}
        cfg["keep"] = list(sel["levels"])
    return cfg


def tracks(filt: dict, sel: dict) -> list[str]:
    """The resume track tags for the chosen topics, in catalog order."""
    return list(fields(filt, sel))


def apply_tracks(profile: dict, tags: list[str], template: dict) -> dict:
    """Keep a track for each tag, add missing ones from the template, drop the rest.

    Only tracks the template defines are added or dropped. A track you named yourself is
    kept, because no topic can select it. A dropped track's PDF stays in the resume cache,
    so choosing the topic again brings the same file back.
    """
    known = {t["tag"]: t for t in template.get("resume_tracks") or []}
    current = profile.get("resume_tracks") or []
    have = {t.get("tag") for t in current}
    kept = [t for t in current if t.get("tag") in tags or t.get("tag") not in known]
    added = [dict(known[tag]) for tag in tags if tag in known and tag not in have]
    profile["resume_tracks"] = kept + added
    left = [t["tag"] for t in profile["resume_tracks"]]
    if left and profile.get("default_resume_tag") not in left:
        profile["default_resume_tag"] = left[0]
    return profile
