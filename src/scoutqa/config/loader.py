"""Load `scoutqa.yaml` into a validated `ProjectConfig`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from scoutqa.config.models import ProjectConfig
from scoutqa.errors import ConfigError

DEFAULT_CONFIG_NAME = "scoutqa.yaml"


def load_config(path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> ProjectConfig:
    """Read YAML config, apply dotted-key `overrides` (e.g. {"scope.max_pages": 10}) and validate."""
    cfg_path = Path(path) if path else Path.cwd() / DEFAULT_CONFIG_NAME
    if not cfg_path.is_file():
        raise ConfigError(f"Config file not found: {cfg_path}. Run `scoutqa init` to create one.")
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{cfg_path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{cfg_path}: top level must be a mapping")
    for dotted, value in (overrides or {}).items():
        if value is not None:
            set_dotted(data, dotted, value)
    return parse_config(data, source=str(cfg_path))


def parse_config(data: dict[str, Any], source: str = "<config>") -> ProjectConfig:
    try:
        return ProjectConfig.model_validate(data)
    except ValidationError as exc:
        lines = [f"{source}: invalid configuration"]
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"]) or "(root)"
            lines.append(f"  - {loc}: {err['msg']}")
        raise ConfigError("\n".join(lines)) from None


def set_dotted(data: dict[str, Any], dotted: str, value: Any) -> None:
    """Set `data`'s nested `dotted` key (e.g. "scope.max_pages") to `value`, creating intermediate
    mappings as needed. Shared by `load_config`'s CLI-style overrides and the web UI's config editor."""
    node = data
    *parents, leaf = dotted.split(".")
    for key in parents:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[leaf] = value
