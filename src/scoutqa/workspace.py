"""Per-project runtime directories.

Runtime data (session cookies, crawl results, later the app DB) lives in a per-user directory,
outside the repo and outside synced folders such as OneDrive:

    ~/.scoutqa/projects/<project>/        (Windows: %USERPROFILE%\\.scoutqa\\...)
    Override with SCOUTQA_HOME.

%LOCALAPPDATA% is deliberately not used: packaged (MSIX) hosts silently redirect writes there, so a
session saved from one host would be invisible to another.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def scoutqa_home() -> Path:
    if override := os.environ.get("SCOUTQA_HOME"):
        return Path(override)
    return Path.home() / ".scoutqa"


@dataclass(frozen=True)
class Workspace:
    root: Path

    @property
    def auth_dir(self) -> Path:
        return self.root / "auth"

    def storage_state_path(self, profile: str) -> Path:
        return self.auth_dir / profile / "storage_state.json"

    def session_meta_path(self, profile: str) -> Path:
        return self.auth_dir / profile / "session_meta.json"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    @property
    def db_path(self) -> Path:
        return self.root / "app.db"

    def new_run(self) -> tuple[str, Path]:
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        return run_id, run_dir

    def ensure(self) -> Workspace:
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        _restrict_to_owner(self.auth_dir)
        return self


def workspace_for(project: str) -> Workspace:
    return Workspace(scoutqa_home() / "projects" / project).ensure()


def _restrict_to_owner(path: Path) -> None:
    """Best effort: POSIX 0700. On Windows %LOCALAPPDATA% is already per-user."""
    if os.name == "posix":
        path.chmod(0o700)
