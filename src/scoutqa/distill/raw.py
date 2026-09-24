"""Typed mirror of the JSON returned by extractor.js. Local only — never sent to an LLM."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Raw(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class RawHeading(_Raw):
    level: int
    text: str
    region: str


class RawLink(_Raw):
    ref: str
    href: str
    text: str
    id: str | None = None
    download: bool = False
    region: str = "main"
    visible: bool = True
    table: str | None = None
    locators: dict[str, Any] = Field(default_factory=dict)


class RawControl(_Raw):
    ref: str
    tag: str
    role: str
    name: str
    type: str | None = None
    id: str | None = None
    form: str | None = None
    form_method: str | None = Field(default=None, alias="formMethod")
    disabled: bool = False
    expanded: bool | None = None
    selected: bool | None = None
    has_popup: str | None = Field(default=None, alias="hasPopup")
    controls_id: str | None = Field(default=None, alias="controlsId")
    region: str = "main"
    table: str | None = None
    locators: dict[str, Any] = Field(default_factory=dict)


class RawField(_Raw):
    ref: str
    tag: str
    type: str
    role: str
    label: str
    name: str | None = None
    placeholder: str | None = None
    required: bool = False
    readonly: bool = False
    disabled: bool = False
    min: str | None = None
    max: str | None = None
    step: str | None = None
    min_length: int | None = Field(default=None, alias="minLength")
    max_length: int | None = Field(default=None, alias="maxLength")
    pattern: str | None = None
    accept: str | None = None
    multiple: bool = False
    autocomplete: str | None = None
    options: list[str] = Field(default_factory=list)
    option_count: int = Field(default=0, alias="optionCount")
    required_message: str | None = Field(default=None, alias="requiredMessage")
    described_by: str | None = Field(default=None, alias="describedBy")
    form: str | None = None
    region: str = "main"
    locators: dict[str, Any] = Field(default_factory=dict)


class RawForm(_Raw):
    ref: str
    name: str
    method: str
    region: str = "main"


class RawTable(_Raw):
    ref: str
    caption: str = ""
    columns: list[str] = Field(default_factory=list)
    row_count: int = Field(default=0, alias="rowCount")
    row_actions: list[str] = Field(default_factory=list, alias="rowActions")
    sortable: bool = False
    region: str = "main"


class RawDialog(_Raw):
    ref: str
    name: str = ""


class RawMessage(_Raw):
    kind: str
    text: str


class RawSnapshot(_Raw):
    url: str
    title: str = ""
    headings: list[RawHeading] = Field(default_factory=list)
    links: list[RawLink] = Field(default_factory=list)
    controls: list[RawControl] = Field(default_factory=list)
    fields: list[RawField] = Field(default_factory=list)
    forms: list[RawForm] = Field(default_factory=list)
    tables: list[RawTable] = Field(default_factory=list)
    dialogs: list[RawDialog] = Field(default_factory=list)
    messages: list[RawMessage] = Field(default_factory=list)
    pagination: bool = False
