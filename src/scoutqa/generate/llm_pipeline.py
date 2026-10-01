"""LLM-based scenario + expansion generation (M8): the two-stage generator described in
docs/ARCHITECTURE.md §3.7. Deterministic code still does the heavy lifting — this module only calls a
model for what rules genuinely cannot do (judging which business scenarios are worth testing, and writing
them up); everything else (batching, ref-checking, dedup, incrementality via the M7 cache) is plain code.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scoutqa.appmodel.repo import AppModel
from scoutqa.config.models import ProjectConfig
from scoutqa.generate.cases import Source, Step, TestCase, case_key
from scoutqa.generate.context import AppView, ModulePage
from scoutqa.generate.context_pack import Snippet, for_module, load_context_pack
from scoutqa.generate.dedup import find_duplicate, is_duplicate_title
from scoutqa.generate.engine import build_app_view
from scoutqa.generate.llm_prompts import cross_module_scenario_messages, expand_messages, scenario_messages
from scoutqa.generate.llm_schemas import CaseBatchOut, ExpandedCaseOut, ScenarioBatchOut, ScenarioOut
from scoutqa.generate.serialize import module_pages_dsl, role_glossary
from scoutqa.llm.base import Generation, Usage
from scoutqa.llm.router import ModelRouter
from scoutqa.log import get_logger

log = get_logger(__name__)

CROSS_MODULE = "End-to-End"
_QUOTED = re.compile(r'"([^"]*)"')
_MIN_GROUNDED_LEN = 10


@dataclass
class LlmGenerationResult:
    cases: list[TestCase] = field(default_factory=list)
    by_module: Counter[str] = field(default_factory=Counter)
    scenarios_proposed: int = 0
    scenarios_skipped_duplicate: int = 0
    cases_skipped_duplicate: int = 0
    calls: int = 0
    cached_calls: int = 0
    usage: Usage = field(default_factory=Usage)

    @property
    def needs_review(self) -> int:
        return sum(1 for c in self.cases if c.needs_review)


@dataclass
class DryRunEstimate:
    calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def approx_input_tokens(self) -> int:
        return sum(int(c["approx_input_tokens"]) for c in self.calls)

    @property
    def approx_cost_usd(self) -> float | None:
        costs = [float(c["approx_cost_usd"]) for c in self.calls if c["approx_cost_usd"] is not None]
        return sum(costs) if costs else None


def primary_pages_by_module(app: AppView, roles: list[str]) -> dict[str, tuple[str, list[ModulePage]]]:
    """module -> (role, pages): the first role's view of a module, falling back to a later role only for
    modules that role never reaches. Keeps token cost near one role's worth, not one per role."""
    combined: dict[str, tuple[str, list[ModulePage]]] = {}
    for role in roles:
        for module, pages in app.pages_by_module(role).items():
            if module not in combined:
                combined[module] = (role, pages)
    return combined


def _chunks(items: Sequence[ScenarioOut], size: int) -> Iterable[list[ScenarioOut]]:
    for i in range(0, len(items), size):
        yield list(items[i:i + size])


def _preconditions(role: str, auth_enabled: bool, named: bool) -> list[str]:
    if not auth_enabled:
        return []
    return [f"User is signed in as '{role}'" if named else "User is signed in"]


def _state_refs(model: AppModel, state_id: str) -> set[str]:
    return {e.ref for e in model.elements(state_id)}


def _valid_refs(model: AppModel, page_refs: list[str]) -> set[str]:
    out: set[str] = set()
    for sid in page_refs:
        if model.state(sid) is not None:
            out |= _state_refs(model, sid)
    return out


def _is_grounded(text: str, dsl: str) -> str | None:
    """The DSL's own quoted text that `text` echoes, if any — real evidence rather than an invention."""
    lowered = text.lower()
    for match in _QUOTED.finditer(dsl):
        quoted = match.group(1)
        if len(quoted) >= _MIN_GROUNDED_LEN and quoted.lower() in lowered:
            return quoted
    return None


def _to_test_case(out: ExpandedCaseOut, scenario: ScenarioOut, module: str, role: str, cfg: ProjectConfig,
                  model: AppModel, dsl: str, generator: str) -> TestCase:
    auth_enabled = cfg.auth.type != "none"
    named = cfg.auth.profile_names() != ["default"]
    valid = _valid_refs(model, scenario.page_refs)
    steps: list[Step] = []
    invalid_ref = False
    for s in out.steps:
        ref = s.target_ref.strip() or None
        if ref and ref not in valid:
            invalid_ref = True
            ref = None
        steps.append(Step(action=s.action, target_ref=ref, data=s.data.strip() or None,
                          expected=s.expected.strip() or None))
    grounded = _is_grounded(out.expected_result, dsl)
    assumptions = []
    if not grounded:
        assumptions.append("Expected result was not directly observed in the app; confirm the actual behaviour.")
    if invalid_ref:
        assumptions.append("A step referenced an element the app model does not show on this page; verify it.")
    evidence = [f"Pages: {', '.join(scenario.page_refs) or '(none cited)'}"]
    if grounded:
        evidence.append(f'Observed message in the app: "{grounded}"')
    cited_state = next((model.state(sid) for sid in scenario.page_refs if model.state(sid) is not None), None)
    state_id = cited_state.id if cited_state else None
    pattern = cited_state.url_pattern if cited_state else None
    return TestCase(
        key=case_key("LLM", module, scenario.title.strip().lower()),
        module=module, title=scenario.title, type=scenario.type,
        priority=scenario.priority, roles=[role] if auth_enabled else [],
        preconditions=out.preconditions or _preconditions(role, auth_enabled, named),
        steps=steps, expected_result=out.expected_result, test_data=out.test_data.strip() or None,
        tags=sorted({*out.tags, "llm"}),
        source=Source(origin="llm", generator=generator, state_id=state_id, url_pattern=pattern,
                     refs=sorted(valid & {s.target_ref for s in steps if s.target_ref}),
                     evidence=evidence, assumptions=assumptions),
    )


def _dsl_for_states(model: AppModel, state_ids: Iterable[str]) -> str:
    """A DSL block for an arbitrary set of states (for cross-module scenarios, which may cite pages from
    several modules) — no in-page-variant diffing here, just each cited page on its own."""
    from scoutqa.appmodel.render import page_block

    lines: list[str] = []
    for sid in sorted({s for s in state_ids if s}):
        state = model.state(sid)
        if state is not None:
            lines += page_block(state, [], 1)
    return "\n".join(lines)


def _dedupe_scenario_titles(scenarios: list[ScenarioOut], already_covered: list[str],
                            result: LlmGenerationResult) -> list[ScenarioOut]:
    kept: list[ScenarioOut] = []
    seen = list(already_covered)
    for s in scenarios:
        if is_duplicate_title(s.title, seen):
            result.scenarios_skipped_duplicate += 1
            continue
        kept.append(s)
        seen.append(s.title)
    return kept


async def _expand_batches(router: ModelRouter, scenarios: list[ScenarioOut], module: str, role: str,
                          cfg: ProjectConfig, model: AppModel, dsl: str, glossary: str, context: list[Snippet],
                          result: LlmGenerationResult) -> list[TestCase]:
    out: list[TestCase] = []
    for batch in _chunks(scenarios, cfg.generation.cases_per_batch):
        scenarios_json = json.dumps([s.model_dump(mode="json") for s in batch], indent=2)
        messages = expand_messages(glossary, module, dsl, scenarios_json, context)
        gen: Generation[CaseBatchOut] = await router.generate("expand", messages, CaseBatchOut)
        result.calls += 1
        result.cached_calls += int(gen.cached)
        result.usage = result.usage + gen.usage
        generator = f"llm:{gen.provider}/{gen.model}"
        by_title = {s.title.strip().lower(): s for s in batch}
        for case_out in gen.value.cases:
            scenario = by_title.get(case_out.scenario_title.strip().lower())
            if scenario is None:
                log.warning("[%s] expand named a scenario ScoutQA did not ask for (%r); skipping",
                           module, case_out.scenario_title)
                continue
            out.append(_to_test_case(case_out, scenario, module, role, cfg, model, dsl, generator))
    return out


def _absorb_duplicates(cases: list[TestCase], module: str, existing: list[TestCase], all_new: list[TestCase],
                       covered: dict[str, list[str]], result: LlmGenerationResult) -> None:
    for tc in cases:
        if find_duplicate(tc, existing + all_new) is not None:
            result.cases_skipped_duplicate += 1
            continue
        all_new.append(tc)
        covered[module].append(tc.title)
        result.by_module[module] += 1


async def generate_llm_cases(model: AppModel, cfg: ProjectConfig, router: ModelRouter, *,
                             existing_cases: list[TestCase]) -> LlmGenerationResult:
    """Run the two-stage LLM generator over every module, plus (if configured) one cross-module pass.
    `existing_cases` — typically the rule-based cases from the same run, plus any LLM cases already stored
    — is shown to the model as "already covered" and used for the final duplicate check."""
    app = build_app_view(model, cfg)
    roles = cfg.auth.profile_names()
    pages_by_module = primary_pages_by_module(app, roles)
    snippets = load_context_pack(cfg.generation.context_pack, Path.cwd())
    covered: dict[str, list[str]] = defaultdict(list)
    for c in existing_cases:
        covered[c.module].append(c.title)

    result = LlmGenerationResult()
    glossaries: dict[str, str] = {}
    all_new: list[TestCase] = []

    def glossary_for(role: str) -> str:
        if role not in glossaries:
            glossaries[role] = role_glossary(app, role, app.pages_by_module(role))
        return glossaries[role]

    for module, (role, pages) in sorted(pages_by_module.items()):
        if not pages:
            continue
        dsl = module_pages_dsl(pages)
        context = for_module(snippets, module)
        messages = scenario_messages(glossary_for(role), module, dsl, covered.get(module, []), context,
                                     cfg.generation.scenarios_per_module)
        gen: Generation[ScenarioBatchOut] = await router.generate("scenarios", messages, ScenarioBatchOut)
        result.calls += 1
        result.cached_calls += int(gen.cached)
        result.usage = result.usage + gen.usage
        scenarios = _dedupe_scenario_titles(gen.value.scenarios, covered.get(module, []), result)
        result.scenarios_proposed += len(scenarios)
        if not scenarios:
            continue
        new_cases = await _expand_batches(router, scenarios, module, role, cfg, model, dsl,
                                          glossary_for(role), context, result)
        _absorb_duplicates(new_cases, module, existing_cases, all_new, covered, result)

    if cfg.generation.cross_module_scenarios and len(pages_by_module) > 1:
        primary_role = roles[0]
        cross_context = for_module(snippets, CROSS_MODULE)
        messages = cross_module_scenario_messages(glossary_for(primary_role), cross_context,
                                                  cfg.generation.cross_module_scenarios)
        gen = await router.generate("scenarios", messages, ScenarioBatchOut)
        result.calls += 1
        result.cached_calls += int(gen.cached)
        result.usage = result.usage + gen.usage
        scenarios = _dedupe_scenario_titles(gen.value.scenarios, covered.get(CROSS_MODULE, []), result)
        result.scenarios_proposed += len(scenarios)
        if scenarios:
            cited = {sid for s in scenarios for sid in s.page_refs}
            dsl = _dsl_for_states(model, cited)
            new_cases = await _expand_batches(router, scenarios, CROSS_MODULE, primary_role, cfg, model, dsl,
                                              glossary_for(primary_role), cross_context, result)
            _absorb_duplicates(new_cases, CROSS_MODULE, existing_cases, all_new, covered, result)

    model.assign_case_ids(all_new)
    result.cases = all_new
    return result


async def estimate_llm_generation(model: AppModel, cfg: ProjectConfig, router: ModelRouter, *,
                                  existing_cases: list[TestCase]) -> DryRunEstimate:
    """What `generate_llm_cases` would cost, without calling anything — one scenario-stage estimate per
    module (expansion cost is not estimated: it depends on how many scenarios come back)."""
    app = build_app_view(model, cfg)
    roles = cfg.auth.profile_names()
    pages_by_module = primary_pages_by_module(app, roles)
    snippets = load_context_pack(cfg.generation.context_pack, Path.cwd())
    covered: dict[str, list[str]] = defaultdict(list)
    for c in existing_cases:
        covered[c.module].append(c.title)
    glossaries: dict[str, str] = {}
    estimate = DryRunEstimate()
    for module, (role, pages) in sorted(pages_by_module.items()):
        if not pages:
            continue
        if role not in glossaries:
            glossaries[role] = role_glossary(app, role, app.pages_by_module(role))
        dsl = module_pages_dsl(pages)
        context = for_module(snippets, module)
        messages = scenario_messages(glossaries[role], module, dsl, covered.get(module, []), context,
                                     cfg.generation.scenarios_per_module)
        row = router.estimate("scenarios", messages, ScenarioBatchOut)
        row["module"] = module
        estimate.calls.append(row)
    if cfg.generation.cross_module_scenarios and len(pages_by_module) > 1:
        primary_role = roles[0]
        glossaries.setdefault(primary_role, role_glossary(app, primary_role, app.pages_by_module(primary_role)))
        messages = cross_module_scenario_messages(glossaries[primary_role], [],
                                                  cfg.generation.cross_module_scenarios)
        row = router.estimate("scenarios", messages, ScenarioBatchOut)
        row["module"] = CROSS_MODULE
        estimate.calls.append(row)
    return estimate
