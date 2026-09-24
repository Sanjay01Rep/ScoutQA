"""Canonical test case model.

Every generator (rule packs now, the LLM later) produces these; templates only *map* them (M4), so a
template change never requires regeneration. Each case carries its source for traceability, and keeps
*evidence* (observed by the crawler) apart from *assumptions* (expected behaviour nobody observed).
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class CaseType(StrEnum):
    FUNCTIONAL = "functional"
    NEGATIVE = "negative"
    BOUNDARY = "boundary"
    NAVIGATION = "navigation"
    UI = "ui"
    SECURITY = "security"
    ACCESS = "access"
    SMOKE = "smoke"


class Priority(StrEnum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


class Step(BaseModel):
    action: str
    target_ref: str | None = None  # element ref within the source state, e.g. "e12"
    data: str | None = None
    expected: str | None = None


class Source(BaseModel):
    origin: Literal["rule", "llm"] = "rule"
    generator: str  # rule id (R-FIELD-REQUIRED) or model + prompt version
    state_id: str | None = None
    url_pattern: str | None = None
    refs: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class TestCase(BaseModel):
    __test__ = False  # not a pytest test class

    key: str  # deterministic fingerprint; the stable ID is assigned from it
    id: str = ""  # e.g. TC-ITEMS-007, assigned by the app model
    module: str
    title: str
    type: CaseType
    priority: Priority
    roles: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    steps: list[Step]
    expected_result: str
    test_data: str | None = None
    tags: list[str] = Field(default_factory=list)
    source: Source
    review_status: Literal["draft", "reviewed", "rejected"] = "draft"
    extra: dict[str, str] = Field(default_factory=dict)  # custom template columns (M4/M8)

    @property
    def needs_review(self) -> bool:
        return bool(self.source.assumptions)


def case_key(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(parts).encode()).hexdigest()[:14]
