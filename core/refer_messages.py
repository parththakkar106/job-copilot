#!/usr/bin/env python3
"""Referral messages: your own templates with a person, company, and role swapped in.

referral_templates.yaml holds one message per role type and contact level. No model writes
any part of a message, so what you send is exactly what you wrote. A blank template is
skipped everywhere, so you can fill the file in over time.

    python core/refer_messages.py     # runs the checks
"""
from __future__ import annotations

import re
import sys
from functools import lru_cache
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import paths, search  # noqa: E402
from core.scraper import classify_field  # noqa: E402

LEVELS = {"hm": "Hiring manager", "recruiter": "Recruiter", "employee": "Employee",
          "bitsian": "BITS alum"}
KIND_NAMES = {"ai": "AI", "ds": "DS", "tech": "Tech", "quant": "Quant"}
# The scraper's career paths that have their own templates. Every other path is Tech.
_PATH_KIND = {"ai": "ai", "ds": "ds", "analytics": "ds", "quant": "quant"}


@lru_cache(maxsize=1)
def _catalog() -> dict:
    filt = yaml.safe_load(paths.FILTERS.read_text(encoding="utf-8"))
    return search.fields(filt, {"paths": list(filt["paths"])})


def role_kind(title: str) -> str:
    # The catalog has no bare "ai" keyword, so "Software Engineer - AI" would read as Tech.
    if re.search(r"\bai\b", title or "", re.I):
        return "ai"
    return _PATH_KIND.get(classify_field(title or "", _catalog()), "tech")


def load_templates() -> dict:
    """{kind: {level: text}}, without blank templates or unknown names."""
    data = yaml.safe_load(paths.REFERRAL_TEMPLATES.read_text(encoding="utf-8")) or {}
    return {kind: {lvl: str(data[kind][lvl]).strip() for lvl in LEVELS
                   if str((data.get(kind) or {}).get(lvl) or "").strip()}
            for kind in KIND_NAMES if data.get(kind)}


def render(template: str, name: str, company: str, role: str) -> str:
    for key, value in (("{name}", name), ("{company}", company), ("{role}", role)):
        template = template.replace(key, value)
    return template


def recent_roles(rows: list[dict]) -> list[dict]:
    """Applied rows, newest first. `rejected` rows are roles Parth passed on."""
    kept = [r for r in rows if r.get("status") != "rejected"]
    # The file is oldest first, so reversing it puts the latest row first within a day.
    return sorted(reversed(kept), key=lambda r: (r.get("applied") or "")[:10], reverse=True)


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def roles_at(company: str, rows: list[dict]) -> list[dict]:
    """The rows whose company contains `company`, ignoring case and punctuation."""
    want = _squash(company)
    return [r for r in rows if want and want in _squash(r.get("company"))]


def _checks() -> int:
    import tempfile
    n = 0
    for title, kind in (("AI Engineer", "ai"), ("Software Engineer - AI", "ai"),
                        ("Data Scientist", "ds"), ("Data Analyst", "ds"),
                        ("Quantitative Researcher", "quant"), ("Backend Engineer", "tech"),
                        ("Security Engineer", "tech"), ("Thank you for applying.", "tech")):
        assert role_kind(title) == kind, (title, role_kind(title))
        n += 1

    with tempfile.TemporaryDirectory() as d:
        saved = paths.REFERRAL_TEMPLATES
        paths.REFERRAL_TEMPLATES = Path(d) / "t.yaml"
        paths.REFERRAL_TEMPLATES.write_text(
            "ai:\n  hm: |\n    Hey {name},\n    {role} at {company}\n  recruiter: ''\n"
            "  nonsense: text\nds:\n  hm:\nquant:\n", encoding="utf-8")
        got = load_templates()
        paths.REFERRAL_TEMPLATES = saved
    assert got == {"ai": {"hm": "Hey {name},\n{role} at {company}"}, "ds": {}}, got
    n += 1
    # An unknown placeholder stays as written instead of raising.
    assert render(got["ai"]["hm"] + " {other}", "Ivo", "Acme", "SWE") \
        == "Hey Ivo,\nSWE at Acme {other}"
    n += 1

    rows = [{"company": "Acme, Inc.", "title": "A", "applied": "2026-01-01"},
            {"company": "Acme, Inc.", "title": "B", "applied": "2026-03-01"},
            {"company": "Acme, Inc.", "title": "C", "applied": "2026-03-01"},
            {"company": "Acme, Inc.", "title": "D", "applied": "2026-04-01",
             "status": "rejected"},
            {"company": "Other", "title": "E", "applied": "2026-05-01"}]
    assert [r["title"] for r in roles_at("acme inc", recent_roles(rows))] == ["C", "B", "A"]
    n += 1
    assert roles_at("", rows) == [] and roles_at("!!", rows) == []
    n += 1
    return n


if __name__ == "__main__":
    print(f"refer_messages: {_checks()} checks passed")
