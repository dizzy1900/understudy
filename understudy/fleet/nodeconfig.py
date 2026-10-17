"""Node config schema, canonical template and tolerant loaders shared by the node, supervisor and fault injectors."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from config import settings

CONFIG_NAME = "config.yaml"
LOG_REL = Path("logs") / "node.log"
DATA_REL = Path("data")


class NodeConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    node_id: str = ""
    port: int
    upstream_base_url: str
    model: str
    max_tokens: int = settings.NODE_MAX_TOKENS
    api_key_env: str = settings.API_KEY_ENV
    extra_body: dict[str, Any] = Field(default_factory=dict)
    worker_recycle_after: int = 0
    worker_concurrency: int = settings.NODE_WORKER_CONCURRENCY
    upstream_limit: int = settings.NODE_UPSTREAM_LIMIT
    retry_backoff: bool = True
    quota_mb: int = settings.NODE_QUOTA_MB
    upstream_timeout_s: float = 30.0


class ConfigError(Exception):
    def __init__(self, line: int, problem: str):
        super().__init__(f"line {line}: {problem}")
        self.line = line
        self.problem = problem


def canonical_config(node_id: str, port: int, model: str | None = None) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "port": port,
        "upstream_base_url": settings.CRUSOE_BASE_URL,
        "model": model or settings.fleet_model_id(),
        "max_tokens": settings.NODE_MAX_TOKENS,
        "api_key_env": settings.API_KEY_ENV,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        "worker_recycle_after": 0,
        "worker_concurrency": settings.NODE_WORKER_CONCURRENCY,
        "upstream_limit": settings.NODE_UPSTREAM_LIMIT,
        "retry_backoff": True,
        "quota_mb": settings.NODE_QUOTA_MB,
    }


def dump_config(cfg: dict[str, Any]) -> str:
    return yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False)


def parse_config_text(text: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
        problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        raise ConfigError((mark.line + 1) if mark else 1, str(problem)) from exc
    if not isinstance(data, dict):
        raise ConfigError(1, "top level must be a mapping")
    return data


def load_node_config(path: Path) -> NodeConfig:
    data = parse_config_text(path.read_text())
    try:
        return NodeConfig(**data)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first["loc"])
        raise ConfigError(1, f"invalid value for {field}: {first['msg']}") from exc


def load_config_dict(path: Path) -> dict[str, Any]:
    return parse_config_text(path.read_text())


def read_port(path: Path, default: int) -> int:
    """The port named in config.yaml, tolerating a file that no longer parses."""
    try:
        text = path.read_text()
    except OSError:
        return default
    try:
        value = parse_config_text(text).get("port")
        if isinstance(value, int):
            return value
    except ConfigError:
        pass
    match = re.search(r"^port:\s*(\d+)\s*$", text, re.MULTILINE)
    return int(match.group(1)) if match else default


@dataclass(frozen=True)
class NodePaths:
    root: Path

    @property
    def config(self) -> Path:
        return self.root / CONFIG_NAME

    @property
    def log(self) -> Path:
        return self.root / LOG_REL

    @property
    def data(self) -> Path:
        return self.root / DATA_REL

    @property
    def stderr(self) -> Path:
        return self.root / "stderr.log"

    @property
    def state(self) -> Path:
        return self.root / "state.json"

    @property
    def exit_info(self) -> Path:
        return self.root / "exit.json"
