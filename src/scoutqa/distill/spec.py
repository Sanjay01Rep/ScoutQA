"""PageSpec: the distilled, redacted, locator-free description of one UI state.

This is what generation (rules and, later, the LLM serializer) works from. Element refs (`e12`) are
stable within a state; locators live separately in `ElementRecord` (stored, never prompted).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class _Spec(BaseModel):
    def compact(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True, exclude_defaults=True)


class FieldSpec(_Spec):
    ref: str
    kind: str  # text, email, password, number, date, select, checkbox, radio, textarea, file, ...
    label: str
    required: bool = False
    readonly: bool = False
    disabled: bool = False
    min: str | None = None
    max: str | None = None
    step: str | None = None
    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None
    accept: str | None = None
    multiple: bool = False
    options: list[str] = Field(default_factory=list)
    option_count: int = 0
    placeholder: str | None = None
    hint: str | None = None
    required_message: str | None = None


class ActionSpec(_Spec):
    ref: str
    role: str
    name: str
    risk: str
    count: int = 1
    disabled: bool = False
    in_frame: bool = False


class FormSpec(_Spec):
    ref: str
    name: str = ""
    method: str = "get"
    fields: list[FieldSpec] = Field(default_factory=list)
    submit: str | None = None  # ref of the submit control
    actions: list[ActionSpec] = Field(default_factory=list)


class LinkSpec(_Spec):
    ref: str
    name: str
    target: str  # URL pattern, e.g. /items/{id}
    count: int = 1
    risk: str = "navigational"


class TableSpec(_Spec):
    ref: str
    caption: str = ""
    columns: list[str] = Field(default_factory=list)
    row_count: int = 0
    row_actions: list[str] = Field(default_factory=list)
    sortable: bool = False


class DialogSpec(_Spec):
    ref: str
    name: str = ""
    forms: list[FormSpec] = Field(default_factory=list)
    actions: list[ActionSpec] = Field(default_factory=list)


class LayoutSpec(_Spec):
    """Site chrome (header/nav/footer) shared by many states; summarised once."""

    id: str
    links: list[LinkSpec] = Field(default_factory=list)
    actions: list[ActionSpec] = Field(default_factory=list)


class PageSpec(_Spec):
    url: str
    url_pattern: str
    title: str = ""
    layout_id: str | None = None
    headings: list[str] = Field(default_factory=list)  # "h1 Title"
    forms: list[FormSpec] = Field(default_factory=list)
    fields: list[FieldSpec] = Field(default_factory=list)  # fields outside any form
    actions: list[ActionSpec] = Field(default_factory=list)
    links: list[LinkSpec] = Field(default_factory=list)
    tables: list[TableSpec] = Field(default_factory=list)
    dialogs: list[DialogSpec] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)
    pagination: bool = False
    frames: list[str] = Field(default_factory=list)


class ElementRecord(BaseModel):
    """Every addressable element of a state, with locator candidates (for replay and Phase 2)."""

    ref: str
    kind: str  # link | control | field | form | table | dialog
    role: str
    name: str
    risk: str | None = None
    signature: str  # role|name|nth — re-finds the element after a reload
    locators: dict[str, Any] = Field(default_factory=dict)
