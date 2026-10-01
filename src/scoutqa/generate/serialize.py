"""Turns the app model into the small, structured text the LLM stages see (docs/ARCHITECTURE.md §3.8).

Layout, for provider prompt caching: a *glossary* (modules + the role's shared layout) is built once per
role and marked cacheable, since it is identical across every module's scenario call in a run; only the
module's own pages vary per call.
"""

from __future__ import annotations

from scoutqa.appmodel.render import layout_lines, page_block
from scoutqa.generate.context import AppView, ModulePage

MAX_TITLES_PER_MODULE_IN_GLOSSARY = 6


def role_glossary(app: AppView, role: str, pages_by_module: dict[str, list[ModulePage]]) -> str:
    """One block per run (per role): the role's nav layout + a one-line summary of every module. Put this
    in a `Message(..., cacheable=True)` — it does not change between a run's module-by-module calls."""
    lines = [f"ROLE {role}"]
    if app.model is not None:
        used_layouts = {mp.state.layout_id for pages in pages_by_module.values() for mp in pages
                        if mp.state.layout_id}
        layouts = app.model.layouts()
        for lid in sorted(used_layouts):
            if lid in layouts:
                lines += layout_lines(layouts[lid])
    lines.append("MODULES:")
    for module, pages in sorted(pages_by_module.items()):
        titles = [mp.state.title or mp.state.url_pattern for mp in pages[:MAX_TITLES_PER_MODULE_IN_GLOSSARY]]
        more = f" (+{len(pages) - len(titles)} more)" if len(pages) > len(titles) else ""
        lines.append(f"  {module}: {len(pages)} page(s) — {', '.join(titles)}{more}")
    return "\n".join(lines)


def module_pages_dsl(pages: list[ModulePage]) -> str:
    """The compact DSL for one module's pages (and their in-page states), for one LLM call."""
    lines: list[str] = []
    for mp in pages:
        lines += page_block(mp.state, mp.variants, mp.instances)
    return "\n".join(lines)
