"""TemplateSpec: the user's template reduced to ordered columns, each mapped to a canonical field."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class CanonicalField(StrEnum):
    ID = "id"
    MODULE = "module"
    TITLE = "title"
    SCENARIO = "scenario"
    PRECONDITIONS = "preconditions"
    STEPS = "steps"
    EXPECTED = "expected_result"
    PRIORITY = "priority"
    TYPE = "type"
    TEST_DATA = "test_data"
    ROLES = "roles"
    TAGS = "tags"
    PAGE = "page"
    NOTES = "notes"
    REVIEW = "review_status"
    # step-per-row layouts
    STEP_NO = "step_no"
    STEP_ACTION = "step_action"
    STEP_EXPECTED = "step_expected"
    STEP_DATA = "step_data"
    # never generated
    BLANK = "blank"  # execution-time columns (Status, Actual Result, ...) and anything unknown
    CUSTOM = "custom"  # unknown column: blank until an LLM fills it (M8) or a default is configured


STEP_FIELDS = frozenset({CanonicalField.STEP_NO, CanonicalField.STEP_ACTION, CanonicalField.STEP_EXPECTED,
                         CanonicalField.STEP_DATA})


class ColumnSpec(BaseModel):
    name: str
    field: CanonicalField
    index: int  # 0-based position in the template
    enum: list[str] = Field(default_factory=list)  # allowed values (Excel dropdown / JSON schema enum)
    default: str | None = None  # fixed value from config
    reason: str = ""  # how the mapping was decided, shown by `scoutqa template`


class TemplateSpec(BaseModel):
    source: str  # file path, or "default"
    format: Literal["xlsx", "csv", "md", "json", "default"]
    columns: list[ColumnSpec]
    layout: Literal["case_per_row", "step_per_row"] = "case_per_row"
    sheet: str | None = None
    header_row: int = 1  # 1-based (Excel); data starts on the next row

    def column(self, field: CanonicalField) -> ColumnSpec | None:
        return next((c for c in self.columns if c.field is field), None)

    @property
    def custom_columns(self) -> list[ColumnSpec]:
        return [c for c in self.columns if c.field is CanonicalField.CUSTOM and c.default is None]
