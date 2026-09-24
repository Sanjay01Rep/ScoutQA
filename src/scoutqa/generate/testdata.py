"""Deterministic test data derived from field constraints (no randomness: reruns give identical cases)."""

from __future__ import annotations

import re
from datetime import date, timedelta

from scoutqa.distill.spec import FieldSpec

TEXT_KINDS = frozenset({"text", "search", "textarea", "password", "tel", "url", "email"})
_DATE_KINDS = frozenset({"date", "datetime-local", "month", "week", "time"})

INVALID_FORMATS: dict[str, list[str]] = {
    "email": ["plainaddress", "user@", "@example.com", "user name@example.com"],
    "url": ["not a url", "http//missing-colon.com", "www.example"],
    "number": ["abc", "1e", "--1"],
    "tel": ["phone", "12ab34"],
    "date": ["2026-13-45", "31/31/2026"],
}


def repeat_marker(n: int) -> str:
    """Readable description of an n-character string."""
    return f'{n} characters (e.g. "{"A" * min(n, 10)}{"…" if n > 10 else ""}")'


def _number(s: str | None) -> float | None:
    try:
        return float(s) if s not in (None, "") else None
    except ValueError:
        return None


def _fmt(n: float) -> str:
    return str(int(n)) if n == int(n) else str(n)


def pattern_example(pattern: str) -> str | None:
    """Tiny generator for simple patterns like [0-9]{10}, [A-Z]{2}-\\d{5}, \\d{3,5}. None if too complex."""
    out: list[str] = []
    token = re.compile(r"(\[[^\]]+\]|\\d|\\w|[A-Za-z0-9 _-])(\{(\d+)(?:,(\d*))?\}|\+|\*|\?)?")
    pos = 0
    while pos < len(pattern):
        m = token.match(pattern, pos)
        if not m:
            return None
        atom, quant, lo, _hi = m.group(1), m.group(2), m.group(3), m.group(4)
        if atom in (r"\d",) or atom.startswith("[0-9") or atom == "[\\d]":
            char = "1"
        elif atom == r"\w" or atom.startswith("[A-Za-z") or atom.startswith("[a-zA-Z") or atom.startswith("[A-Z"):
            char = "A"
        elif atom.startswith("[a-z"):
            char = "a"
        elif atom.startswith("["):
            inner = atom[1:-1]
            if inner.startswith("^") or not inner:
                return None
            char = inner[0]
        else:
            char = atom
        count = int(lo) if lo else (1 if quant in (None, "+", "?") else 0)
        out.append(char * count)
        pos = m.end()
    return "".join(out)


_SAMPLE_LETTERS = "SAMPLETEXT"
_SAMPLE_DIGITS = "1234567890"
_SHAPED_KINDS = frozenset({"text", "tel", "search", "textarea"})


def shape_example(shape: str) -> str:
    """'AA-99999' -> 'SA-12345': a deterministic value with the observed format (Record mode)."""
    letters = digits = 0
    out = []
    for ch in shape:
        if ch in "Aa":
            sample = _SAMPLE_LETTERS[letters % len(_SAMPLE_LETTERS)]
            out.append(sample if ch == "A" else sample.lower())
            letters += 1
        elif ch == "9":
            out.append(_SAMPLE_DIGITS[digits % len(_SAMPLE_DIGITS)])
            digits += 1
        else:
            out.append(ch)
    return "".join(out)


def _is_structured(shape: str) -> bool:
    """Worth reusing: contains digits or punctuation (an ID, code or phone), not just a word."""
    return any(ch == "9" or not (ch.isalpha() or ch.isspace()) for ch in shape)


def valid_value(f: FieldSpec, observed_shape: str | None = None) -> str:
    """A value that satisfies every observed constraint of the field (and its observed format, if any)."""
    kind = f.kind
    if observed_shape and kind in _SHAPED_KINDS and _is_structured(observed_shape):
        shaped = shape_example(observed_shape)
        return shaped[: f.max_length] if f.max_length else shaped
    if f.pattern and (example := pattern_example(f.pattern)):
        return example
    if kind == "email":
        return "qa.tester@example.com"
    if kind == "url":
        return "https://example.com"
    if kind == "tel":
        return "5551234567"
    if kind == "password":
        return "Str0ng!Passw0rd"
    if kind in ("number", "range"):
        lo, hi = _number(f.min), _number(f.max)
        step = _number(f.step) or 1
        if lo is not None and hi is not None:
            return _fmt(lo + ((hi - lo) // (2 * step)) * step)  # a mid-range value on the step grid
        return _fmt(lo if lo is not None else (hi if hi is not None and hi < 1 else 1))
    if kind in _DATE_KINDS:
        return f.min or date(2026, 1, 15).isoformat()
    if kind in ("select", "radio"):
        return next((o for o in f.options if o.strip() and not o.lower().startswith(("select", "choose", "--"))),
                    f.options[0] if f.options else "any option")
    if kind == "checkbox":
        return "checked"
    if kind == "file":
        accept = (f.accept or ".pdf").split(",")[0].strip()
        return f"sample{accept if accept.startswith('.') else '.pdf'}"
    base = f"Test {f.label}".strip() if f.label else "Test value"
    if f.max_length is not None:
        base = base[: f.max_length]
    if f.min_length is not None and len(base) < f.min_length:
        base = base + "x" * (f.min_length - len(base))
    return base


def date_shift(iso: str, days: int) -> str | None:
    try:
        return (date.fromisoformat(iso) + timedelta(days=days)).isoformat()
    except ValueError:
        return None


def range_values(f: FieldSpec) -> tuple[list[str], list[str]] | None:
    """(accepted boundary values, rejected out-of-range values) for number/date fields with min/max."""
    if f.kind in ("number", "range"):
        step = _number(f.step) or 1
        lo, hi = _number(f.min), _number(f.max)
        accepted = [_fmt(v) for v in (lo, hi) if v is not None]
        rejected = [_fmt(v) for v in ((lo - step) if lo is not None else None,
                                      (hi + step) if hi is not None else None) if v is not None]
        return (accepted, rejected) if accepted else None
    if f.kind == "date":
        accepted = [v for v in (f.min, f.max) if v]
        rejected = [v for v in ((date_shift(f.min, -1) if f.min else None),
                                (date_shift(f.max, 1) if f.max else None)) if v]
        return (accepted, rejected) if accepted else None
    return None


def invalid_pattern_value(f: FieldSpec) -> str:
    example = pattern_example(f.pattern or "") or ""
    return "!!" + example[:-1] if example else "!!invalid!!"
