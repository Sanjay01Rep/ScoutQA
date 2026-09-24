"""Turn a Pydantic output model into the strict JSON-Schema subset every provider understands.

The target is the tightest widely-supported dialect (OpenAI's "strict" structured-output mode), which
everything else (Anthropic tool schemas, Gemini's `response_json_schema`, Ollama/vLLM's `format`) also
accepts:
  - every object has `additionalProperties: false`
  - `required` lists *every* property; a field optional in the Pydantic model becomes a nullable type
    instead of being left out of `required` (OpenAI strict forbids omitting keys from `required`)
  - `$defs`/`$ref` are inlined — some local/weaker models only handle a flat schema
  - noisy keywords Pydantic adds (`title`, `default`, `examples`, `$schema`) are stripped
"""

from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel

from scoutqa.llm.base import SchemaError

_STRIP_KEYS = frozenset({"title", "default", "examples", "$schema", "$id"})
_MAX_DEPTH = 12  # guards against schemas nested deeper than any real output model needs


def portable_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The model's schema, self-contained (no $ref) and valid under OpenAI strict mode."""
    raw = model.model_json_schema()
    defs = {**raw.pop("$defs", {}), **raw.pop("definitions", {})}
    try:
        inlined = _inline_refs(raw, defs, frozenset(), 0)
    except RecursionError as exc:
        raise SchemaError(f"{model.__name__}: schema is too deeply nested to inline for structured output") from exc
    return cast(dict[str, Any], _tighten(inlined))  # the top level is always the model's own object schema


def render_for_prompt(schema: dict[str, Any]) -> str:
    """A compact, human-readable rendering of a portable schema, for embedding in a prompt as a fallback
    description (useful for providers/models without real schema enforcement)."""
    return _render(schema, 0)


def _inline_refs(node: Any, defs: dict[str, Any], seen: frozenset[str], depth: int) -> Any:
    if depth > _MAX_DEPTH:
        raise RecursionError
    if isinstance(node, dict):
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            if name in seen:
                raise SchemaError(f"recursive type {name!r} cannot be used as structured LLM output")
            if name not in defs:
                raise SchemaError(f"schema references undefined {node['$ref']!r}")
            resolved = _inline_refs(defs[name], defs, seen | {name}, depth + 1)
            # sibling keys (e.g. a per-field `description`) win over the definition's own.
            return {**resolved, **{k: v for k, v in node.items() if k != "$ref"}}
        return {k: _inline_refs(v, defs, seen, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline_refs(v, defs, seen, depth + 1) for v in node]
    return node


def _tighten(node: Any) -> Any:
    if isinstance(node, dict):
        out = {k: _tighten(v) for k, v in node.items() if k not in _STRIP_KEYS}
        if out.get("type") == "object" or "properties" in out:
            out["type"] = "object"
            props = out.get("properties", {})
            out["properties"] = props
            out["required"] = list(props.keys())
            out["additionalProperties"] = False
        return out
    if isinstance(node, list):
        return [_tighten(v) for v in node]
    return node


def _type_of(schema: dict[str, Any]) -> str:
    t = schema.get("type")
    if isinstance(t, list):
        return "|".join(x for x in t if x != "null")
    if isinstance(t, str):
        return t
    if "enum" in schema:
        return "enum"
    if "anyOf" in schema:
        return "|".join(_type_of(s) for s in schema["anyOf"] if s.get("type") != "null")
    return "any"


def _render(schema: dict[str, Any], indent: int) -> str:
    pad = "  " * indent
    if schema.get("type") == "object" and "properties" in schema:
        lines = ["{"]
        required = set(schema.get("required", []))
        for name, prop in schema["properties"].items():
            optional = "?" if name not in required else ""
            desc = f"  // {prop['description']}" if prop.get("description") else ""
            if _type_of(prop) == "object" and "properties" in prop:
                lines.append(f'{pad}  "{name}"{optional}: {_render(prop, indent + 1)}{desc}')
            elif prop.get("type") == "array":
                items = _render(prop.get("items", {}), indent + 1)
                lines.append(f'{pad}  "{name}"{optional}: [{items}]{desc}')
            else:
                enum = f" one of {prop['enum']}" if "enum" in prop else ""
                lines.append(f'{pad}  "{name}"{optional}: {_type_of(prop)}{enum}{desc}')
        lines.append(f"{pad}}}")
        return "\n".join(lines)
    return _type_of(schema)
