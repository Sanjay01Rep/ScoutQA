"""Local service: pairing, request hardening, and the Record-mode recorder (plain HTTP, no browser)."""

from __future__ import annotations

import http.client
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from scoutqa.appmodel.repo import AppModel
from scoutqa.config.loader import parse_config
from scoutqa.config.models import ProjectConfig
from scoutqa.distill.spec import FieldSpec
from scoutqa.generate.testdata import shape_example, valid_value
from scoutqa.service.app import ScoutQAService
from scoutqa.service.pairing import MAX_ATTEMPTS, Pairing
from scoutqa.workspace import workspace_for
from tests.unit.test_distill import snapshot

EXT = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
CODE = "TEST-CODE"


def cfg() -> ProjectConfig:
    return parse_config({"project": "svc", "base_url": "https://x.io/items/7"})


@pytest.fixture
def service(tmp_path: Path) -> Iterator[ScoutQAService]:
    svc = ScoutQAService(cfg(), workspace_for("svc"), port=0, pairing_code=CODE, output_dir=tmp_path / "out").start()
    yield svc
    svc.stop()


def call(svc: ScoutQAService, method: str, path: str, body: Any = None, *, origin: str | None = EXT,
         token: str | None = None, host: str | None = None) -> tuple[int, Any]:
    conn = http.client.HTTPConnection("127.0.0.1", svc.port, timeout=10)
    headers = {"Host": host or f"127.0.0.1:{svc.port}", "Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    if token:
        headers["Authorization"] = f"Bearer {token}"
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    res = conn.getresponse()
    raw = res.read()
    try:
        return res.status, json.loads(raw)
    except ValueError:
        return res.status, raw


def paired(svc: ScoutQAService) -> str:
    status, data = call(svc, "POST", "/api/pair", {"code": CODE})
    assert status == 200, data
    return str(data["token"])


# ---------------------------------------------------------------- pairing

def test_pairing_is_single_use_and_bound_to_origin(tmp_path: Path) -> None:
    pairing = Pairing(tmp_path / "tokens.json", code="AAAA-BBBB")
    token = pairing.pair("aaaa-bbbb", EXT)
    assert token and pairing.verify(token, EXT)
    assert not pairing.verify(token, "chrome-extension://other")
    assert pairing.pair("AAAA-BBBB", EXT) is None  # code rotated after use
    assert token not in (tmp_path / "tokens.json").read_text()  # only hashes are stored


def test_pairing_locks_after_failed_attempts(tmp_path: Path) -> None:
    pairing = Pairing(tmp_path / "tokens.json", code="AAAA-BBBB")
    for _ in range(MAX_ATTEMPTS):
        assert pairing.pair("WRONG", EXT) is None
    assert pairing.pair("AAAA-BBBB", EXT) is None


# ---------------------------------------------------------------- HTTP hardening

def test_health_needs_no_token(service: ScoutQAService) -> None:
    assert call(service, "GET", "/api/health") == (200, {"ok": True, "service": "scoutqa", "version": "0.1.0"})


def test_requests_from_web_pages_are_refused(service: ScoutQAService) -> None:
    token = paired(service)
    assert call(service, "GET", "/api/project", origin="https://evil.example", token=token)[0] == 403
    assert call(service, "POST", "/api/pair", {"code": CODE}, origin="https://evil.example")[0] == 403
    assert call(service, "OPTIONS", "/api/project", origin="https://evil.example")[0] == 403


def test_dns_rebinding_host_is_refused(service: ScoutQAService) -> None:
    token = paired(service)
    assert call(service, "GET", "/api/project", token=token, host=f"attacker.example:{service.port}")[0] == 403


def test_token_is_required_and_bound(service: ScoutQAService) -> None:
    assert call(service, "GET", "/api/project")[0] == 401
    assert call(service, "POST", "/api/pair", {"code": "NOPE-NOPE"})[0] == 403
    token = paired(service)
    assert call(service, "GET", "/api/project", token="forged")[0] == 401
    assert call(service, "GET", "/api/project", token=token, origin="chrome-extension://someoneelse")[0] == 401
    status, project = call(service, "GET", "/api/project", token=token)
    assert status == 200 and project["allowed_domains"] == ["x.io"]


def test_download_is_confined_to_the_output_folder(service: ScoutQAService, tmp_path: Path) -> None:
    token = paired(service)
    (tmp_path / "secret.xlsx").write_text("x")
    for name in ("../secret.xlsx", "..%2Fsecret.xlsx", "tokens.json", "missing.xlsx"):
        assert call(service, "GET", f"/api/download?name={name}", token=token)[0] == 404


def test_invalid_payloads_are_rejected(service: ScoutQAService) -> None:
    token = paired(service)
    run = call(service, "POST", "/api/record/start", {}, token=token)[1]["run_id"]
    status, data = call(service, "POST", "/api/record/events",
                        {"run_id": run, "page_url": "https://x.io/items/7",
                         "shapes": [{"ref": "e4", "shape": "John Smith", "length": 10}]}, token=token)
    assert status == 400 and data["details"][0]["loc"] == "shapes.0.shape"  # a real value is refused
    assert call(service, "POST", "/api/record/capture", {"run_id": "nope", "url": "https://x.io/",
                                                         "main": {"url": "https://x.io/"}}, token=token)[0] == 400


# ---------------------------------------------------------------- recorder over HTTP

def _capture(svc: ScoutQAService, token: str, run: str, raw: dict[str, Any], kind: str = "load",
             click: dict[str, Any] | None = None) -> dict[str, Any]:
    status, data = call(svc, "POST", "/api/record/capture",
                        {"run_id": run, "url": raw["url"], "main": raw, "trigger": {"kind": kind, "click": click}},
                        token=token)
    assert status == 200, data
    return dict(data)


def test_record_flow(service: ScoutQAService) -> None:
    token = paired(service)
    run = call(service, "POST", "/api/record/start", {}, token=token)[1]["run_id"]
    page = snapshot().model_dump(by_alias=True)
    first = _capture(service, token, run, page)
    assert first["status"] == "new" and not first["variant"]

    # same page, same structure after a click: nothing new
    assert _capture(service, token, run, page, "click", {"from_state_id": first["state_id"], "ref": "e7"})[
        "status"] == "same"

    # a click that opens a dialog: an in-page variant named after the extractor's element
    with_dialog = dict(page, dialogs=[{"ref": "e99", "name": "History"}])
    variant = _capture(service, token, run, with_dialog, "click", {"from_state_id": first["state_id"], "ref": "e7"})
    assert variant["variant"] is True

    out_of_scope = dict(page, url="https://other.io/x")
    assert _capture(service, token, run, out_of_scope)["ignored"] == "out_of_domain"

    status, events = call(service, "POST", "/api/record/events", {
        "run_id": run, "state_id": first["state_id"], "page_url": page["url"],
        "api_calls": [
            {"method": "POST", "url": "https://x.io/api/items", "status": 422,
             "request_shape": {"name": "string", "qty": "string"},
             "messages": ["Owner jane.doe@corp.com already has this item"]},
            {"method": "POST", "url": "https://x.io/api/items/7?draft=1", "status": 201,
             "response_shape": {"id": "number"}},
            {"method": "POST", "url": "https://analytics.example/collect", "status": 204},
        ],
        "shapes": [{"ref": "e4", "shape": "AA-99999", "length": 8}, {"ref": "e999", "shape": "Aa", "length": 2}],
        "trigger_label": "Save",
    }, token=token)
    assert status == 200 and events == {"api_calls": 2, "shapes": 1}

    status, stopped = call(service, "POST", "/api/record/stop", {"run_id": run}, token=token)
    assert status == 200 and stopped["stats"]["captures"] == 3  # the out-of-scope page is not counted

    model = AppModel.open(workspace_for("svc").db_path)
    states = model.states("default")
    assert {s.variant for s in states} == {"", "open_dialog tab 'History'"}
    assert all(s.source == "extension-record" for s in states)
    calls = {(r["method"], r["endpoint"], r["status"]): r for r in model.api_calls()}
    assert set(calls) == {("POST", "/api/items", 422), ("POST", "/api/items/{id}?draft={v}", 201)}
    assert json.loads(calls[("POST", "/api/items", 422)]["messages"]) == ["Owner <email> already has this item"]
    assert calls[("POST", "/api/items", 422)]["trigger"] == "Save"
    assert model.field_shapes() == {("/items/{id}", "Name"): "AA-99999"}
    model.close()


# ---------------------------------------------------------------- extension Crawl mode (service side)

def _page_payload(run: str, requested: str, raw: dict[str, Any], kind: str = "load",
                  ref: str | None = None) -> dict[str, Any]:
    return {"run_id": run, "requested_url": requested, "url": raw["url"], "main": raw, "kind": kind, "ref": ref}


def test_crawl_protocol(service: ScoutQAService) -> None:
    token = paired(service)
    status, start = call(service, "POST", "/api/crawl/start", {}, token=token)
    assert status == 200 and start["read_only"] and start["block_methods"] == ["post", "put", "patch", "delete"]
    run = start["run_id"]
    assert call(service, "POST", "/api/record/start", {}, token=token)[0] == 409  # one mode at a time

    first = call(service, "POST", "/api/crawl/next", {"run_id": run}, token=token)[1]
    assert first["url"] == "https://x.io/items/7"
    page = snapshot().model_dump(by_alias=True)
    loaded = call(service, "POST", "/api/crawl/page", _page_payload(run, first["url"], page), token=token)[1]
    assert loaded["status"] == "new"
    chosen = {a["name"] for a in loaded["actions"]}
    assert chosen == {"History", "Add note"}  # only safe controls; never 'Delete item' or 'Save'

    # a click the service did not choose is refused, a chosen one is accepted
    assert call(service, "POST", "/api/crawl/clicking", {"run_id": run, "url": first["url"], "ref": "e8"},
                token=token)[0] == 400
    history = next(a for a in loaded["actions"] if a["name"] == "History")
    assert call(service, "POST", "/api/crawl/clicking", {"run_id": run, "url": first["url"],
                                                         "ref": history["ref"]}, token=token)[0] == 200
    with_panel = dict(page, dialogs=[{"ref": "e99", "name": "History"}])
    clicked = call(service, "POST", "/api/crawl/page",
                   _page_payload(run, first["url"], with_panel, "click", history["ref"]), token=token)[1]
    assert clicked["variant"] == "open_dialog tab 'History'"

    call(service, "POST", "/api/crawl/events", {"run_id": run, "page_url": first["url"], "api_calls": [
        {"method": "POST", "url": "https://x.io/api/star", "status": 0}],
        "dialogs": [{"type": "confirm", "message": "Really?"}]}, token=token)

    # the frontier queued the page's in-scope links in document order; 'Log out' was skipped, never queued
    queued = call(service, "POST", "/api/crawl/next", {"run_id": run}, token=token)[1]
    assert queued["url"] == "https://x.io/dashboard" and queued["pending"] >= 1
    status, stopped = call(service, "POST", "/api/crawl/stop", {"run_id": run, "cancelled": True}, token=token)
    assert status == 200 and stopped["stopped_reason"] == "cancelled"

    model = AppModel.open(workspace_for("svc").db_path)
    events = model.events(run)
    assert any(e["type"] == "blocked" and e["method"] == "POST" and e["trigger"]
               and "History" in e["trigger"] for e in events)
    assert any(e["type"] == "dialog" and e["text"] == "Really?" for e in events)
    assert any(e["type"] == "skipped" and e["reason"] == "session_ending_link" for e in events)  # Log out
    assert model.run_stopped_reason(run) == "cancelled"
    model.close()


def test_crawl_detects_a_lost_session(tmp_path: Path) -> None:
    cfg_auth = parse_config({"project": "svc2", "base_url": "https://x.io/items/7", "auth": {
        "type": "form", "login_url": "https://x.io/login", "username_env": "U", "password_env": "P"}})
    svc = ScoutQAService(cfg_auth, workspace_for("svc2"), port=0, pairing_code=CODE, output_dir=tmp_path).start()
    try:
        token = paired(svc)
        run = call(svc, "POST", "/api/crawl/start", {}, token=token)[1]["run_id"]
        first = call(svc, "POST", "/api/crawl/next", {"run_id": run}, token=token)[1]
        login_page = {"url": "https://x.io/login", "fields": [
            {"ref": "e1", "tag": "input", "type": "password", "role": "textbox", "label": "Password"}]}
        res = call(svc, "POST", "/api/crawl/page", _page_payload(run, first["url"], login_page), token=token)[1]
        assert res == {"session_lost": True}
        assert call(svc, "POST", "/api/crawl/next", {"run_id": run}, token=token)[1]["reason"] == "session_lost"
    finally:
        svc.stop()


def test_shapes_become_test_data() -> None:
    assert shape_example("AA-99999") == "SA-12345"
    assert shape_example("9999-99-99") == "1234-56-78"
    field = FieldSpec(ref="e1", kind="text", label="Order no", max_length=6)
    assert valid_value(field, "AA-99999") == "SA-123"  # still honours maxlength
    assert valid_value(field, "Aaaa") == "Test O"  # plain words are not reused
    assert valid_value(FieldSpec(ref="e1", kind="email", label="Mail"), "aaaa@aaaa.aaa") == "qa.tester@example.com"
