"""Two fingerprints per state.

structure_hash — text-insensitive shape (form field kinds, action roles/risks, link target patterns, table
                 widths, heading levels, layout). Equal for template instances like /items/1 and /items/2.
content_hash   — the full distilled spec. Changes when labels, constraints or controls change; drives
                 incremental re-crawl and (later) incremental generation.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from scoutqa.distill.spec import PageSpec


def _digest(obj: Any) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def structure_hash(spec: PageSpec) -> str:
    forms = [spec.forms, *(d.forms for d in spec.dialogs)]
    shape = {
        "forms": sorted(
            [f.method, sorted([fld.kind, fld.required] for fld in f.fields)] for group in forms for f in group
        ),
        "fields": sorted(f.kind for f in spec.fields),
        "actions": sorted({(a.role, a.risk) for a in spec.actions}),
        "links": sorted({link.target for link in spec.links}),
        "tables": sorted([len(t.columns), t.sortable] for t in spec.tables),
        "dialogs": len(spec.dialogs),
        "headings": [h.split(" ", 1)[0] for h in spec.headings],
        "layout": spec.layout_id,
        "pagination": spec.pagination,
    }
    return _digest(shape)


def content_hash(spec: PageSpec) -> str:
    return _digest(spec.compact())
