"""One `UIContext` per server process: the project this server was started for, plus its background jobs
— mirrors `scoutqa.mcp.tools.ToolContext`, kept as its own small class rather than shared, the same way
the CLI/MCP/extension-service layers each independently wrap `scoutqa.pipeline` without sharing code.
"""

from __future__ import annotations

from pathlib import Path

from scoutqa.config.models import ProjectConfig
from scoutqa.jobs import JobManager
from scoutqa.workspace import Workspace


class UIContext:
    def __init__(self, cfg: ProjectConfig, ws: Workspace, config_path: Path) -> None:
        self.cfg = cfg
        self.ws = ws
        self.config_path = config_path  # the file `cfg` was actually loaded from — /api/config edits this
        self.jobs = JobManager()
