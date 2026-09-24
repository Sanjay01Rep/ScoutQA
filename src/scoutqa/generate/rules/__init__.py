"""Rule packs: deterministic, zero-token test case generators.

Page-level packs take a PageContext; app-level packs (navigation, auth, access) take the whole model.
"""

from __future__ import annotations

from collections.abc import Callable

from scoutqa.generate.cases import TestCase
from scoutqa.generate.context import PageContext
from scoutqa.generate.rules import actions, dialogs, fields, forms, smoke, tables

PageRule = Callable[[PageContext], list[TestCase]]

PAGE_PACKS: dict[str, PageRule] = {
    "fields": fields.rules,
    "forms": forms.rules,
    "actions": actions.rules,
    "tables": tables.rules,
    "dialogs": dialogs.rules,
    "smoke": smoke.rules,
}
