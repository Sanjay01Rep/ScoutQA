"""Structured-output shapes for the two LLM stages. Kept small and flat — every field is required (M7's
`portable_schema` would make an optional one nullable, not omit it, but smaller schemas also mean fewer
tokens and fewer places for a model to go off-script).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from scoutqa.generate.cases import CaseType, Priority


class ScenarioOut(BaseModel):
    title: str = Field(description="A short, specific scenario name, e.g. 'Apply an expired coupon at checkout'.")
    type: CaseType
    priority: Priority
    page_refs: list[str] = Field(description="The PAGE/STATE ids (e.g. 's4a2b1c3') this scenario exercises, "
                                             "copied exactly from the pages shown above.")
    rationale: str = Field(description="One short sentence: why this is worth testing.")


class ScenarioBatchOut(BaseModel):
    scenarios: list[ScenarioOut]


class StepOut(BaseModel):
    action: str = Field(description="One imperative step, e.g. \"Click 'Apply coupon'\".")
    target_ref: str = Field(description="The element ref this step acts on (e.g. 'e4'), or '' if none applies.")
    data: str = Field(description="Data entered or chosen in this step, or '' if none.")
    expected: str = Field(description="What should happen right after this step, or '' if only the final "
                                      "result matters.")


class ExpandedCaseOut(BaseModel):
    scenario_title: str = Field(description="Exactly the 'title' of the scenario this expands.")
    preconditions: list[str]
    steps: list[StepOut]
    expected_result: str
    test_data: str = Field(description="A one-line summary of the data used, or '' if not applicable.")
    tags: list[str]


class CaseBatchOut(BaseModel):
    cases: list[ExpandedCaseOut]
