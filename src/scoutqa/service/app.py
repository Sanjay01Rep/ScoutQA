"""`scoutqa serve`: the local HTTP service the browser extension talks to.

Hardening:
  - binds to 127.0.0.1 only
  - Host header must be 127.0.0.1:<port> or localhost:<port> (blocks DNS rebinding)
  - requests with a web Origin (http/https) are refused (blocks cross-site requests from pages)
  - everything except /api/health and /api/pair needs a bearer token bound to the extension's origin
  - request bodies are size-limited and validated; downloads are confined to the output folder
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from pydantic import ValidationError

from scoutqa import __version__
from scoutqa.appmodel.repo import AppModel
from scoutqa.config.models import ProjectConfig
from scoutqa.errors import ScoutQAError
from scoutqa.log import get_logger
from scoutqa.service.crawler import CrawlPagePayload, ExtensionCrawler
from scoutqa.service.pairing import Pairing
from scoutqa.service.recorder import CapturePayload, EventsPayload, Recorder, RecordError
from scoutqa.workspace import Workspace

log = get_logger(__name__)

DEFAULT_PORT = 8765
MAX_BODY = 5 * 1024 * 1024
_EXPORTABLE = frozenset({".xlsx", ".csv", ".md", ".json"})


class HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


Route = Callable[[dict[str, Any], dict[str, list[str]]], Any]


@dataclass
class ServiceContext:
    cfg: ProjectConfig
    ws: Workspace
    port: int
    pairing: Pairing
    recorder: Recorder
    crawler: ExtensionCrawler
    output_dir: Path

    # ------------------------------------------------------------------ routes

    def routes(self) -> dict[tuple[str, str], tuple[Route, bool]]:
        """(method, path) -> (handler, needs_auth)."""
        return {
            ("GET", "/api/health"): (self.health, False),
            ("POST", "/api/pair"): (self.pair, False),
            ("GET", "/api/project"): (self.project, True),
            ("GET", "/api/summary"): (self.summary, True),
            ("GET", "/api/gaps"): (self.gaps, True),
            ("POST", "/api/record/start"): (self.record_start, True),
            ("POST", "/api/record/capture"): (lambda b, q: self.recorder.capture(CapturePayload.model_validate(b)),
                                              True),
            ("POST", "/api/record/events"): (lambda b, q: self.recorder.events(EventsPayload.model_validate(b)),
                                             True),
            ("POST", "/api/record/stop"): (lambda b, q: self.recorder.stop(str(b.get("run_id", ""))), True),
            ("POST", "/api/crawl/start"): (self.crawl_start, True),
            ("POST", "/api/crawl/next"): (lambda b, q: self.crawler.next(str(b.get("run_id", ""))), True),
            ("POST", "/api/crawl/page"): (lambda b, q: self.crawler.page(CrawlPagePayload.model_validate(b)), True),
            ("POST", "/api/crawl/events"): (lambda b, q: self.crawler.events(EventsPayload.model_validate(b)), True),
            ("POST", "/api/crawl/clicking"): (lambda b, q: self.crawler.clicking(
                str(b.get("run_id", "")), str(b.get("url", "")), str(b.get("ref", ""))), True),
            ("POST", "/api/crawl/error"): (lambda b, q: self.crawler.error(
                str(b.get("run_id", "")), str(b.get("url", ""))[:4000], str(b.get("message", ""))), True),
            ("POST", "/api/crawl/stop"): (lambda b, q: self.crawler.stop(
                str(b.get("run_id", "")), cancelled=bool(b.get("cancelled"))), True),
            ("POST", "/api/generate"): (self.generate, True),
            ("POST", "/api/export"): (self.export, True),
        }

    def record_start(self, body: dict[str, Any], query: dict[str, list[str]]) -> dict[str, Any]:
        if self.crawler.active():
            raise HttpError(409, "stop the crawl before recording")
        return self.recorder.start(body.get("role"))

    def crawl_start(self, body: dict[str, Any], query: dict[str, list[str]]) -> dict[str, Any]:
        if self.recorder.active():
            raise HttpError(409, "stop recording before starting a crawl")
        return self.crawler.start(body.get("role"))

    def health(self, body: dict[str, Any], query: dict[str, list[str]]) -> dict[str, Any]:
        return {"ok": True, "service": "scoutqa", "version": __version__}

    def pair(self, body: dict[str, Any], query: dict[str, list[str]]) -> dict[str, Any]:
        origin = str(body.get("_origin", ""))
        if not origin.startswith("chrome-extension://"):
            raise HttpError(403, "pairing is only allowed from the ScoutQA extension")
        token = self.pairing.pair(str(body.get("code", "")), origin)
        if token is None:
            raise HttpError(403, "wrong or expired pairing code; restart `scoutqa serve` for a new one")
        log.info("Paired with %s", origin)
        return {"token": token, "project": self.cfg.project}

    def project(self, body: dict[str, Any], query: dict[str, list[str]]) -> dict[str, Any]:
        return {"project": self.cfg.project, "base_url": self.cfg.base_url, "allowed_domains": self.cfg.allowed_domains,
                "roles": self.cfg.auth.profile_names(), "auth": self.cfg.auth.type,
                "recording": self.recorder.active(), "crawling": self.crawler.active()}

    def _model(self) -> AppModel:
        return AppModel.open(self.ws.db_path)

    def summary(self, body: dict[str, Any], query: dict[str, list[str]]) -> dict[str, Any]:
        model = self._model()
        try:
            roles = {role: len(model.states(role)) for role in model.roles()}
            cases = model.cases()
            return {"states": roles, "cases": len(cases), "needs_review": sum(1 for c in cases if c.needs_review),
                    "api_calls": len(model.api_calls()), "recording": self.recorder.active()}
        finally:
            model.close()

    def gaps(self, body: dict[str, Any], query: dict[str, list[str]]) -> dict[str, Any]:
        """Links seen but never captured for a role: what to record next."""
        role = (query.get("role") or [self.cfg.auth.profile_names()[0]])[0]
        model = self._model()
        try:
            captured = {s.url for s in model.states(role)}
            seen: dict[str, str] = {}
            for t in model.transitions(role):
                if t["kind"] == "link" and t["to_state"] is None and t["to_url"] not in captured:
                    seen.setdefault(t["to_url"], t["label"])
            items = [{"url": url, "label": label} for url, label in sorted(seen.items())][:50]
            return {"role": role, "gaps": items, "total": len(seen)}
        finally:
            model.close()

    def generate(self, body: dict[str, Any], query: dict[str, list[str]]) -> dict[str, Any]:
        from scoutqa import pipeline

        report = pipeline.generate(self.cfg, workspace=self.ws)
        return {"cases": len(report.cases), "needs_review": report.needs_review, "by_module": report.by_module}

    def export(self, body: dict[str, Any], query: dict[str, list[str]]) -> dict[str, Any]:
        from scoutqa import pipeline

        fmt = str(body.get("format", "xlsx"))
        if fmt not in ("xlsx", "csv", "md", "json"):
            raise HttpError(400, f"unknown format {fmt!r}")
        from datetime import datetime

        name = f"{self.cfg.project}-testcases-{datetime.now().strftime('%Y%m%d-%H%M%S')}.{fmt}"
        report = pipeline.export(self.cfg, fmt=fmt, output=self.output_dir / name, workspace=self.ws)  # type: ignore[arg-type]
        return {"name": name, "cases": report.cases, "rows": report.rows, "path": str(report.path)}

    def download(self, name: str) -> Path:
        safe = Path(name).name
        path = (self.output_dir / safe).resolve()
        if safe != name or path.parent != self.output_dir.resolve() or path.suffix not in _EXPORTABLE \
                or not path.is_file():
            raise HttpError(404, "no such export")
        return path


def make_handler(ctx: ServiceContext) -> type[BaseHTTPRequestHandler]:
    routes = ctx.routes()
    allowed_hosts = {f"127.0.0.1:{ctx.port}", f"localhost:{ctx.port}"}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "ScoutQA"

        def log_message(self, format: str, *args: object) -> None:
            log.debug("service: " + format, *args)

        # -------------------------------------------------------------- plumbing
        def _origin(self) -> str:
            return self.headers.get("Origin", "")

        def _cors(self) -> dict[str, str]:
            origin = self._origin()
            if origin.startswith("chrome-extension://"):
                return {"Access-Control-Allow-Origin": origin, "Vary": "Origin",
                        "Access-Control-Allow-Headers": "Authorization, Content-Type",
                        "Access-Control-Allow-Methods": "GET, POST, OPTIONS"}
            return {}

        def _send(self, status: int, payload: Any = None, raw: bytes | None = None,
                  ctype: str = "application/json", extra: dict[str, str] | None = None) -> None:
            data = raw if raw is not None else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in {**self._cors(), **(extra or {})}.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def _guard(self, needs_auth: bool) -> None:
            if self.headers.get("Host", "") not in allowed_hosts:
                raise HttpError(403, "bad host")
            origin = self._origin()
            if origin and not origin.startswith("chrome-extension://"):
                raise HttpError(403, "requests from web pages are not allowed")
            if needs_auth:
                # Chrome omits Origin on extension GETs. That is safe to accept with a valid token: a web page
                # cannot attach an Authorization header without a CORS preflight, and preflights carry the
                # page's Origin, which is refused above.
                auth = self.headers.get("Authorization", "")
                token = auth[7:] if auth.startswith("Bearer ") else ""
                if not token or not ctx.pairing.verify(token, origin or None):
                    raise HttpError(401, "not paired: enter the pairing code shown by `scoutqa serve`")

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise HttpError(413, "request too large")
            if not length:
                return {}
            try:
                data = json.loads(self.rfile.read(length))
            except ValueError:
                raise HttpError(400, "invalid JSON") from None
            if not isinstance(data, dict):
                raise HttpError(400, "expected a JSON object")
            return data

        def _dispatch(self, method: str) -> None:
            parts = urlsplit(self.path)
            try:
                if method == "GET" and parts.path == "/api/download":
                    self._guard(needs_auth=True)
                    name = (parse_qs(parts.query).get("name") or [""])[0]
                    path = ctx.download(name)
                    self._send(200, raw=path.read_bytes(), ctype="application/octet-stream",
                               extra={"Content-Disposition": f'attachment; filename="{path.name}"'})
                    return
                route = routes.get((method, parts.path))
                if route is None:
                    raise HttpError(404, "not found")
                handler, needs_auth = route
                self._guard(needs_auth)
                body = self._body() if method == "POST" else {}
                if parts.path == "/api/pair":
                    body["_origin"] = self._origin()
                self._send(200, handler(body, parse_qs(parts.query)))
            except HttpError as exc:
                self._send(exc.status, {"error": str(exc)})
            except ValidationError as exc:
                details = [{"loc": ".".join(str(p) for p in e["loc"]), "msg": e["msg"]} for e in exc.errors()[:5]]
                self._send(400, {"error": "invalid request", "details": details})
            except (RecordError, ScoutQAError) as exc:
                self._send(400, {"error": str(exc)})
            except Exception:  # never leak a traceback to the client
                log.exception("service error on %s %s", method, parts.path)
                self._send(500, {"error": "internal error (see the scoutqa serve console)"})

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_OPTIONS(self) -> None:
            if not self._origin().startswith("chrome-extension://"):
                self._send(403, {"error": "forbidden"})
                return
            self._send(204, raw=b"")

    return Handler


class ScoutQAService:
    def __init__(self, cfg: ProjectConfig, ws: Workspace, port: int = DEFAULT_PORT, pairing_code: str | None = None,
                 output_dir: Path | None = None) -> None:
        pairing = Pairing(ws.root / "service_tokens.json")
        if pairing_code:
            pairing.code = pairing_code
        output = output_dir or (Path.cwd() / "scoutqa-output")
        output.mkdir(parents=True, exist_ok=True)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
        self.port = int(self._httpd.server_address[1])
        self.ctx = ServiceContext(cfg, ws, self.port, pairing, Recorder(cfg, ws), ExtensionCrawler(cfg, ws), output)
        self._httpd.RequestHandlerClass = make_handler(self.ctx)
        self._httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def pairing_code(self) -> str:
        return self.ctx.pairing.code

    def start(self) -> ScoutQAService:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        self._httpd.serve_forever()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
