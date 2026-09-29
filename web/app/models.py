"""Pydantic request bodies for the write endpoints."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


class AnswerEdit(BaseModel):
    id: str                      # question id (askId)
    value: Optional[str] = None  # None resets to unanswered; "" = deliberately blank


class AnswersUpdate(BaseModel):
    answers: list[AnswerEdit]


class FilledEdit(BaseModel):
    label: str                   # the filled row's label, as the drawer shows it
    value: str


class ResumeOverride(BaseModel):
    tag: str                     # quant | ds | tech | ai
    display: Optional[str] = None


class StateChange(BaseModel):
    target: str                  # in_queue | filled | submitted | blocked | skipped | held
    reason: Optional[str] = None
    meta: Optional[dict[str, Any]] = None


class BankRow(BaseModel):
    id: Optional[str] = None
    key: str
    scope: str = "common"        # common | company
    company: Optional[str] = None
    answer: str = ""
    tags: Optional[Any] = None   # list[str] or comma string
    status: str = "active"       # active | draft | stale


class CompanyTier(BaseModel):
    name: str
    tier: str                    # pipeline | dream


class ProfileUpdate(BaseModel):
    profile: dict[str, Any]


class SearchPicks(BaseModel):
    paths: list[str]
    levels: list[str]


class DraftPref(BaseModel):
    enabled: bool                # turn Tier-2 Claude drafting on or off
