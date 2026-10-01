"""Builds the ScoutQA web UI server: a Starlette app wrapping `scoutqa.pipeline`, one process per project
(like `scoutqa serve`/`scoutqa mcp`). Bound to 127.0.0.1 only; every `/api/*` call (besides the health
check) needs a token — generated at startup and printed in the URL, the same defense-in-depth the
extension's pairing token gives `scoutqa serve`, against another local process or a malicious page
reaching this port.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from scoutqa.config.loader import DEFAULT_CONFIG_NAME
from scoutqa.config.models import ProjectConfig
from scoutqa.errors import ScoutQAError
from scoutqa.webui import routes as r
from scoutqa.webui.context import UIContext
from scoutqa.workspace import Workspace, workspace_for

_STATIC_DIR = Path(__file__).parent / "static"
_UNGUARDED = frozenset({"/api/health"})


class _SecurityMiddleware(BaseHTTPMiddleware):
    """Host-header pinning (blocks DNS rebinding) and a bearer token on every other `/api/*` route."""

    def __init__(self, app: Any, *, port: int, token: str) -> None:
        super().__init__(app)
        self._allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self._token = token

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.headers.get("host", "") not in self._allowed_hosts:
            return JSONResponse({"error": "bad host"}, status_code=403)
        path = request.url.path
        if path.startswith("/api/") and path not in _UNGUARDED:
            supplied = request.headers.get("x-scoutqa-token") or request.query_params.get("token")
            if supplied != self._token:
                return JSONResponse({"error": "missing or invalid token"}, status_code=401)
        return await call_next(request)


async def _handle_scoutqa_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, ScoutQAError)
    return JSONResponse({"error": str(exc)}, status_code=400)


_ROUTES = [
    Route("/api/health", r.health),
    Route("/api/project", r.project_info),
    Route("/api/config", r.get_config),
    Route("/api/config", r.save_config, methods=["POST"]),
    Route("/api/login", r.login, methods=["POST"]),
    Route("/api/crawl", r.crawl_start, methods=["POST"]),
    Route("/api/jobs/{job_id}", r.job_status),
    Route("/api/jobs/{job_id}/events", r.job_events),
    Route("/api/map", r.app_map),
    Route("/api/generate", r.generate_test_cases, methods=["POST"]),
    Route("/api/review", r.review_list),
    Route("/api/review", r.review_apply, methods=["POST"]),
    Route("/api/export", r.export, methods=["POST"]),
    Route("/api/download", r.download),
    Route("/api/models", r.models_list),
    Route("/api/models/test", r.test_model, methods=["POST"]),
    Route("/api/models/configure", r.configure_model, methods=["POST"]),
    Route("/api/template", r.set_template),
    Route("/api/usage", r.usage),
]


def build_app(cfg: ProjectConfig, ws: Workspace | None = None, *, port: int, token: str | None = None,
             config_path: Path | None = None) -> Starlette:
    token = token or secrets.token_urlsafe(24)
    routes: list[Route | Mount] = list(_ROUTES)
    if _STATIC_DIR.is_dir():
        routes.append(Mount("/", app=StaticFiles(directory=_STATIC_DIR, html=True), name="static"))
    app = Starlette(
        routes=routes, middleware=[Middleware(_SecurityMiddleware, port=port, token=token)],
        exception_handlers={ScoutQAError: _handle_scoutqa_error},
    )
    path = config_path or (Path.cwd() / DEFAULT_CONFIG_NAME)
    app.state.ui_ctx = UIContext(cfg, ws or workspace_for(cfg.project), path)
    app.state.token = token
    return app
