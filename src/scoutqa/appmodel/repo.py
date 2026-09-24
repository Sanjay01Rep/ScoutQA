"""Typed repository over the SQLite app model."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Any

from scoutqa.distill.spec import ElementRecord, LayoutSpec, PageSpec
from scoutqa.generate.cases import TestCase
from scoutqa.generate.modules import module_code

SCHEMA_VERSION = "1"


class StateStatus(StrEnum):
    NEW = "new"
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    REMOVED = "removed"


def state_id(role: str, url: str, variant: str = "") -> str:
    return "s" + hashlib.sha1(f"{role}\n{url}\n{variant}".encode()).hexdigest()[:10]


@dataclass(frozen=True)
class StateRow:
    id: str
    role: str
    url: str
    variant: str
    parent_state: str | None
    url_pattern: str
    title: str
    structure_hash: str
    content_hash: str
    layout_id: str | None
    spec: PageSpec
    depth: int
    source: str
    status: StateStatus


@dataclass(frozen=True)
class TemplateGroup:
    """States of one role sharing a URL pattern and structure — one representative is enough."""

    url_pattern: str
    structure_hash: str
    state_ids: list[str]

    @property
    def representative(self) -> str:
        return self.state_ids[0]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class AppModel:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @classmethod
    def open(cls, path: Path | str) -> AppModel:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(files("scoutqa.appmodel").joinpath("schema.sql").read_text(encoding="utf-8"))
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
        conn.commit()
        return cls(conn)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    # ------------------------------------------------------------------ runs

    def begin_run(self, run_id: str, role: str, source: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO runs(id, role, source, started_at) VALUES (?, ?, ?, ?)",
                      (run_id, role, source, _now()))

    def finish_run(self, run_id: str, stopped_reason: str, stats: dict[str, Any], complete: bool) -> None:
        """Close a run. Only a complete run may mark unseen states as removed."""
        with self.tx() as c:
            row = c.execute("SELECT role, source FROM runs WHERE id = ?", (run_id,)).fetchone()
            c.execute("UPDATE runs SET finished_at = ?, stopped_reason = ?, stats = ? WHERE id = ?",
                      (_now(), stopped_reason, json.dumps(stats), run_id))
            if complete and row is not None:
                c.execute(
                    "UPDATE states SET status = 'removed' WHERE role = ? AND source = ? AND last_run != ?"
                    " AND status != 'removed'",
                    (row["role"], row["source"], run_id),
                )

    # ------------------------------------------------------------------ states

    def upsert_state(
        self,
        *,
        run_id: str,
        role: str,
        spec: PageSpec,
        structure_hash: str,
        content_hash: str,
        layout: LayoutSpec | None,
        elements: list[ElementRecord],
        depth: int,
        source: str,
        variant: str = "",
        parent_state: str | None = None,
    ) -> tuple[str, StateStatus]:
        sid = state_id(role, spec.url, variant)
        with self.tx() as c:
            if layout is not None:
                c.execute("INSERT OR REPLACE INTO layouts(id, spec) VALUES (?, ?)",
                          (layout.id, layout.model_dump_json(exclude_defaults=True)))
            prev = c.execute("SELECT content_hash, last_run FROM states WHERE id = ?", (sid,)).fetchone()
            if prev is None:
                status = StateStatus.NEW
            elif prev["last_run"] == run_id:
                status = StateStatus(c.execute("SELECT status FROM states WHERE id = ?", (sid,)).fetchone()[0])
            else:
                status = StateStatus.UNCHANGED if prev["content_hash"] == content_hash else StateStatus.CHANGED
            c.execute(
                """INSERT INTO states(id, role, url, variant, parent_state, url_pattern, title, structure_hash,
                       content_hash, layout_id, spec, depth, source, first_run, last_run, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       url_pattern = excluded.url_pattern, title = excluded.title,
                       structure_hash = excluded.structure_hash, content_hash = excluded.content_hash,
                       layout_id = excluded.layout_id, spec = excluded.spec, depth = MIN(depth, excluded.depth),
                       parent_state = excluded.parent_state, last_run = excluded.last_run,
                       status = excluded.status""",
                (sid, role, spec.url, variant, parent_state, spec.url_pattern, spec.title, structure_hash,
                 content_hash, spec.layout_id, spec.model_dump_json(exclude_defaults=True), depth, source,
                 run_id, run_id, status.value),
            )
            c.execute("DELETE FROM elements WHERE state_id = ?", (sid,))
            c.executemany(
                "INSERT INTO elements(state_id, ref, kind, role, name, risk, signature, locators)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(sid, e.ref, e.kind, e.role, e.name, e.risk, e.signature, json.dumps(e.locators))
                 for e in elements],
            )
        return sid, status

    def states(self, role: str | None = None, include_removed: bool = False) -> list[StateRow]:
        sql = "SELECT * FROM states WHERE 1=1"
        args: list[Any] = []
        if role is not None:
            sql += " AND role = ?"
            args.append(role)
        if not include_removed:
            sql += " AND status != 'removed'"
        rows = self.conn.execute(sql + " ORDER BY role, depth, url, variant", args).fetchall()
        return [self._state(r) for r in rows]

    def state(self, sid: str) -> StateRow | None:
        row = self.conn.execute("SELECT * FROM states WHERE id = ?", (sid,)).fetchone()
        return self._state(row) if row else None

    def roles(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT DISTINCT role FROM states ORDER BY role")]

    def layouts(self) -> dict[str, LayoutSpec]:
        return {r["id"]: LayoutSpec.model_validate_json(r["spec"])
                for r in self.conn.execute("SELECT * FROM layouts ORDER BY id")}

    def elements(self, sid: str) -> list[ElementRecord]:
        rows = self.conn.execute("SELECT * FROM elements WHERE state_id = ? ORDER BY rowid", (sid,)).fetchall()
        return [ElementRecord(ref=r["ref"], kind=r["kind"], role=r["role"], name=r["name"], risk=r["risk"],
                              signature=r["signature"], locators=json.loads(r["locators"])) for r in rows]

    def template_groups(self, role: str) -> list[TemplateGroup]:
        groups: dict[tuple[str, str], list[str]] = {}
        for s in self.states(role):
            if not s.variant:  # in-page variants belong to their base state, not to a template
                groups.setdefault((s.url_pattern, s.structure_hash), []).append(s.id)
        return [TemplateGroup(p, h, ids) for (p, h), ids in groups.items()]

    def status_counts(self, role: str, run_id: str | None = None) -> dict[str, int]:
        sql = "SELECT status, COUNT(*) FROM states WHERE role = ?"
        args: list[Any] = [role]
        if run_id:
            sql += " AND (last_run = ? OR status = 'removed')"
            args.append(run_id)
        return {r[0]: r[1] for r in self.conn.execute(sql + " GROUP BY status", args)}

    # ------------------------------------------------------------------ transitions & events

    def add_transition(self, *, run_id: str, role: str, from_state: str, to_url: str, kind: str,
                       element_ref: str = "", label: str = "", to_state: str | None = None) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO transitions(role, from_state, element_ref, kind, to_url, to_state, label, last_run)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(role, from_state, element_ref, kind, to_url) DO UPDATE SET
                       to_state = COALESCE(excluded.to_state, to_state), label = excluded.label,
                       last_run = excluded.last_run""",
                (role, from_state, element_ref, kind, to_url, to_state, label[:80], run_id),
            )

    def resolve_transitions(self, role: str) -> None:
        """Point link transitions at the captured base state of their target URL."""
        with self.tx() as c:
            c.execute(
                """UPDATE transitions SET to_state = (
                       SELECT s.id FROM states s
                       WHERE s.role = transitions.role AND s.url = transitions.to_url AND s.variant = ''
                   ) WHERE role = ? AND to_state IS NULL""",
                (role,),
            )

    def transitions(self, role: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM transitions WHERE role = ? ORDER BY from_state, kind, to_url", (role,)).fetchall()

    def add_events(self, run_id: str, role: str, events: list[dict[str, Any]]) -> None:
        with self.tx() as c:
            c.executemany(
                "INSERT INTO run_events(run_id, role, type, reason, url, page_url, method, text, trigger)"
                " VALUES (:run_id, :role, :type, :reason, :url, :page_url, :method, :text, :trigger)",
                [{"run_id": run_id, "role": role, "reason": "", "url": "", "page_url": "", "method": None,
                  "text": None, "trigger": None, **e} for e in events],
            )

    def events(self, run_id: str, type_: str | None = None) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM run_events WHERE run_id = ?", [run_id]
        if type_:
            sql += " AND type = ?"
            args.append(type_)
        return self.conn.execute(sql, args).fetchall()

    def latest_run(self, role: str, sources: tuple[str, ...] = ()) -> str | None:
        sql = "SELECT id FROM runs WHERE role = ? AND finished_at IS NOT NULL"
        args: list[Any] = [role]
        if sources:
            sql += f" AND source IN ({', '.join('?' * len(sources))})"
            args.extend(sources)
        row = self.conn.execute(sql + " ORDER BY started_at DESC, id DESC LIMIT 1", args).fetchone()
        return row[0] if row else None

    def run_stopped_reason(self, run_id: str) -> str | None:
        row = self.conn.execute("SELECT stopped_reason FROM runs WHERE id = ?", (run_id,)).fetchone()
        return row[0] if row else None

    def element(self, sid: str, ref: str) -> ElementRecord | None:
        r = self.conn.execute("SELECT * FROM elements WHERE state_id = ? AND ref = ?", (sid, ref)).fetchone()
        if r is None:
            return None
        return ElementRecord(ref=r["ref"], kind=r["kind"], role=r["role"], name=r["name"], risk=r["risk"],
                             signature=r["signature"], locators=json.loads(r["locators"]))

    # ------------------------------------------------------------------ record mode observations

    def record_api_call(self, *, run_id: str, role: str, page_pattern: str, method: str, endpoint: str, status: int,
                        messages: list[str], request_shape: Any, response_shape: Any, trigger: str | None) -> None:
        with self.tx() as c:
            key = (role, page_pattern, method, endpoint, status)
            row = c.execute("SELECT messages FROM api_calls WHERE role = ? AND page_pattern = ? AND method = ?"
                            " AND endpoint = ? AND status = ?", key).fetchone()
            known: list[str] = json.loads(row["messages"]) if row else []
            merged = known + [m for m in messages if m not in known]
            c.execute(
                """INSERT INTO api_calls(role, page_pattern, method, endpoint, status, count, messages, request_shape,
                       response_shape, trigger, last_run) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                   ON CONFLICT(role, page_pattern, method, endpoint, status) DO UPDATE SET
                       count = count + 1, messages = excluded.messages,
                       request_shape = COALESCE(excluded.request_shape, request_shape),
                       response_shape = COALESCE(excluded.response_shape, response_shape),
                       trigger = COALESCE(excluded.trigger, trigger), last_run = excluded.last_run""",
                (role, page_pattern, method, endpoint, status, json.dumps(merged[:10]),
                 json.dumps(request_shape) if request_shape is not None else None,
                 json.dumps(response_shape) if response_shape is not None else None, trigger, run_id),
            )

    def api_calls(self, role: str | None = None) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM api_calls", []
        if role:
            sql += " WHERE role = ?"
            args.append(role)
        return self.conn.execute(sql + " ORDER BY page_pattern, method, endpoint, status", args).fetchall()

    def record_shape(self, *, run_id: str, role: str, url_pattern: str, label: str, kind: str, shape: str,
                     length: int) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO field_shapes(role, url_pattern, field_label, field_kind, shape, length, count, last_run)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                   ON CONFLICT(role, url_pattern, field_label, shape) DO UPDATE SET
                       count = count + 1, length = excluded.length, last_run = excluded.last_run""",
                (role, url_pattern, label, kind, shape, length, run_id),
            )

    def field_shapes(self) -> dict[tuple[str, str], str]:
        """(url_pattern, field label) -> the most frequently observed shape across roles."""
        best: dict[tuple[str, str], tuple[int, str]] = {}
        for r in self.conn.execute("SELECT url_pattern, field_label, shape, SUM(count) AS n FROM field_shapes"
                                   " GROUP BY url_pattern, field_label, shape"):
            key = (r["url_pattern"], r["field_label"])
            if key not in best or r["n"] > best[key][0]:
                best[key] = (r["n"], r["shape"])
        return {k: v[1] for k, v in best.items()}

    # ------------------------------------------------------------------ test cases

    def assign_case_ids(self, cases: list[TestCase]) -> None:
        """Give every case its stable ID; new keys get the next free number in their module."""
        with self.tx() as c:
            known = {r["key"]: r["id"] for r in c.execute("SELECT key, id FROM case_ids")}
            for case in cases:
                if case.key in known:
                    case.id = known[case.key]
                    continue
                prefix = f"TC-{module_code(case.module)}-"
                rows = c.execute("SELECT id FROM case_ids WHERE id LIKE ?", (prefix + "%",)).fetchall()
                used = [int(r[0][len(prefix):]) for r in rows if r[0][len(prefix):].isdigit()]
                case.id = f"{prefix}{(max(used) + 1) if used else 1:03d}"
                c.execute("INSERT INTO case_ids(key, id, module) VALUES (?, ?, ?)", (case.key, case.id, case.module))
                known[case.key] = case.id

    def replace_cases(self, origin: str, cases: list[TestCase]) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM cases WHERE origin = ?", (origin,))
            now = _now()
            c.executemany(
                "INSERT OR REPLACE INTO cases(key, id, module, origin, spec, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                [(k.key, k.id, k.module, origin, k.model_dump_json(), now) for k in cases],
            )

    def cases(self, origin: str | None = None) -> list[TestCase]:
        sql, args = "SELECT spec FROM cases", []
        if origin:
            sql += " WHERE origin = ?"
            args.append(origin)
        return [TestCase.model_validate_json(r[0]) for r in self.conn.execute(sql + " ORDER BY rowid", args)]

    # ------------------------------------------------------------------ LLM response cache

    def cache_get(self, key: str) -> tuple[str, dict[str, int]] | None:
        row = self.conn.execute("SELECT text, usage FROM llm_cache WHERE key = ?", (key,)).fetchone()
        return (row["text"], json.loads(row["usage"])) if row else None

    def cache_set(self, key: str, *, provider: str, model: str, stage: str, text: str,
                 usage: dict[str, int]) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO llm_cache(key, provider, model, stage, text, usage, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key, provider, model, stage, text, json.dumps(usage), _now()),
            )

    def cache_size(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()
        return int(row[0])

    # ------------------------------------------------------------------ LLM usage ledger

    def record_llm_usage(self, *, run_id: str | None, stage: str, provider: str, model: str, input_tokens: int,
                         output_tokens: int, cached_input_tokens: int, cost_usd: float | None, latency_s: float,
                         attempts: int) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO llm_usage(run_id, stage, provider, model, input_tokens, output_tokens,
                       cached_input_tokens, cost_usd, latency_s, attempts, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, stage, provider, model, input_tokens, output_tokens, cached_input_tokens, cost_usd,
                 latency_s, attempts, _now()),
            )

    def llm_usage_total(self, run_id: str | None = None) -> dict[str, float]:
        """Tokens/cost spent so far (this run if `run_id` is given, else ever) — used for budget checks."""
        sql = ("SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
               "COALESCE(SUM(cost_usd),0) FROM llm_usage")
        args: list[Any] = []
        if run_id:
            sql += " WHERE run_id = ?"
            args.append(run_id)
        row = self.conn.execute(sql, args).fetchone()
        return {"input_tokens": row[0], "output_tokens": row[1], "cost_usd": row[2]}

    def llm_usage_rows(self, run_id: str | None = None) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM llm_usage", []
        if run_id:
            sql += " WHERE run_id = ?"
            args.append(run_id)
        return self.conn.execute(sql + " ORDER BY id", args).fetchall()

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _state(r: sqlite3.Row) -> StateRow:
        return StateRow(
            id=r["id"], role=r["role"], url=r["url"], variant=r["variant"], parent_state=r["parent_state"],
            url_pattern=r["url_pattern"], title=r["title"], structure_hash=r["structure_hash"],
            content_hash=r["content_hash"], layout_id=r["layout_id"], spec=PageSpec.model_validate_json(r["spec"]),
            depth=r["depth"], source=r["source"], status=StateStatus(r["status"]),
        )
