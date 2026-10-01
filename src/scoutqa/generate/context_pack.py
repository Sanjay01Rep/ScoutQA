"""Optional grounding material for LLM generation: requirements, user stories, an OpenAPI spec, or an
existing test-case export (`generation.context_pack` in scoutqa.yaml). Matched to a module by a plain
case-insensitive keyword search — no embeddings, no network call, 0 tokens until a matching snippet is
actually placed in a prompt (docs/ARCHITECTURE.md §3.16).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scoutqa.log import get_logger

log = get_logger(__name__)

_HEADING = re.compile(r"^#{1,6}\s+(.*)$", re.MULTILINE)
_WORD = re.compile(r"[a-z0-9]+")
MAX_SNIPPETS_PER_MODULE = 4
MAX_CHARS_PER_MODULE = 1500
_SNIPPET_CHAR_LIMIT = 2000


@dataclass(frozen=True)
class Snippet:
    source: str  # file name, for traceability in generated evidence
    heading: str  # section heading / requirement id / "GET /items/{id}"
    text: str


def load_context_pack(paths: list[str], base_dir: Path) -> list[Snippet]:
    out: list[Snippet] = []
    for raw in paths:
        p = Path(raw)
        if not p.is_absolute():
            p = base_dir / p
        if not p.is_file():
            log.warning("generation.context_pack: %s not found, skipping", p)
            continue
        try:
            out += _from_json(p) if p.suffix.lower() == ".json" else _from_text(p)
        except (OSError, ValueError) as exc:
            log.warning("generation.context_pack: could not read %s (%s), skipping", p, exc)
    return out


def for_module(snippets: list[Snippet], module: str, *, max_snippets: int = MAX_SNIPPETS_PER_MODULE,
              max_chars: int = MAX_CHARS_PER_MODULE) -> list[Snippet]:
    """The snippets most relevant to `module`, cheapest-first truncated to a token budget."""
    words = _WORD.findall(module.lower())
    if not words:
        return []
    scored = []
    for s in snippets:
        haystack = f"{s.heading}\n{s.text}".lower()
        score = sum(haystack.count(w) for w in words)
        if module.lower() in haystack:
            score += 3
        if score:
            scored.append((score, s))
    scored.sort(key=lambda t: -t[0])
    out: list[Snippet] = []
    budget = max_chars
    for _, s in scored[:max_snippets]:
        if budget <= 0:
            break
        text = s.text[:budget]
        out.append(Snippet(s.source, s.heading, text))
        budget -= len(text)
    return out


def render(snippets: list[Snippet]) -> str:
    if not snippets:
        return ""
    return "\n".join(f"[{s.source} — {s.heading}]\n{s.text}" for s in snippets)


# ---------------------------------------------------------------- per-format extraction

def _from_text(path: Path) -> list[Snippet]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    matches = list(_HEADING.finditer(text))
    if not matches:
        return [Snippet(path.name, path.stem, text[:_SNIPPET_CHAR_LIMIT])]
    out = []
    for i, m in enumerate(matches):
        start, end = m.end(), matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            out.append(Snippet(path.name, m.group(1).strip(), body[:_SNIPPET_CHAR_LIMIT]))
    return out


def _from_json(path: Path) -> list[Snippet]:
    data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    if isinstance(data, dict) and ("openapi" in data or "swagger" in data):
        return _from_openapi(path, data)
    items: Any = data if isinstance(data, list) else (
        data.get("cases") or data.get("requirements") or data.get("items") or data.get("stories") or [])
    out: list[Snippet] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        heading = str(item.get("id") or item.get("Title") or item.get("title") or item.get("name")
                      or f"item {len(out) + 1}")
        out.append(Snippet(path.name, heading, json.dumps(item, ensure_ascii=False)[:_SNIPPET_CHAR_LIMIT]))
    return out


def _from_openapi(path: Path, data: dict[str, Any]) -> list[Snippet]:
    out = []
    for route, methods in (data.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        for verb, op in methods.items():
            if not isinstance(op, dict) or verb.lower() not in (
                "get", "post", "put", "patch", "delete"):
                continue
            summary = str(op.get("summary") or op.get("description") or op.get("operationId") or "").strip()
            out.append(Snippet(path.name, f"{verb.upper()} {route}", summary[:500]))
    return out
