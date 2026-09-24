"""Serializable crawl outputs (Milestone 1: link-level crawl; Milestone 2 moves these into SQLite)."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

StopReason = Literal["completed", "max_pages", "max_duration", "cancelled", "session_lost"]


class SessionOutcome(StrEnum):
    NONE = "none"
    REUSED = "reused"
    FRESH_LOGIN = "fresh_login"


class FrameRef(BaseModel):
    url: str
    in_scope: bool


class PageVisit(BaseModel):
    url: str
    requested_url: str
    title: str
    depth: int
    status: int | None
    parent: str | None
    links_found: int
    frames: list[FrameRef] = Field(default_factory=list)
    has_password_field: bool = False
    state_id: str = ""
    change: str = ""  # new | changed | unchanged (vs. the previous run of this role)
    structure_hash: str = ""


class UiStateVisit(BaseModel):
    """A state reached by a safe in-page action (tab, dialog, disclosure) without a URL change."""

    state_id: str
    parent_state: str
    url: str
    via: str
    change: str


class LinkEdge(BaseModel):
    source: str
    target: str
    text: str


class SkippedUrl(BaseModel):
    url: str
    reason: str
    found_on: str | None = None
    text: str | None = None


class BlockedAction(BaseModel):
    kind: Literal["link", "request"]
    reason: str
    url: str
    page_url: str
    text: str | None = None
    method: str | None = None
    trigger: str | None = None  # in-page action that caused a blocked request


class DialogRecord(BaseModel):
    page_url: str
    type: str
    message: str


class NavigationError(BaseModel):
    url: str
    error: str


class CrawlResult(BaseModel):
    run_id: str
    project: str
    base_url: str
    started_at: datetime
    finished_at: datetime | None = None
    stopped_reason: StopReason = "completed"
    role: str = "default"
    session: SessionOutcome = SessionOutcome.NONE
    reauth_count: int = 0
    pages: list[PageVisit] = Field(default_factory=list)
    ui_states: list[UiStateVisit] = Field(default_factory=list)
    edges: list[LinkEdge] = Field(default_factory=list)
    skipped: list[SkippedUrl] = Field(default_factory=list)
    blocked: list[BlockedAction] = Field(default_factory=list)
    dialogs: list[DialogRecord] = Field(default_factory=list)
    errors: list[NavigationError] = Field(default_factory=list)

    def stats(self) -> dict[str, int]:
        skipped = Counter(s.reason for s in self.skipped)
        return {
            "pages": len(self.pages),
            "ui_states": len(self.ui_states),
            "edges": len(self.edges),
            "blocked_links": sum(1 for b in self.blocked if b.kind == "link"),
            "blocked_requests": sum(1 for b in self.blocked if b.kind == "request"),
            "dialogs_dismissed": len(self.dialogs),
            "errors": len(self.errors),
            **{f"skipped_{reason}": n for reason, n in sorted(skipped.items())},
        }
