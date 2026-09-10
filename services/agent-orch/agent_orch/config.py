from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import socket
from typing import Any


DEFAULT_WS_URL = "ws://127.0.0.1:7791"


@dataclass(frozen=True)
class Config:
    ws_url: str
    token: str
    host_id: str
    runtime_dir: Path
    memory_repo_path: Path | None = None


def _agent_config_path() -> Path:
    return Path("~/.agent-orch/config.json").expanduser()


def _token_path() -> Path:
    return Path("~/.config/pentacle-stream/token").expanduser()


def _read_json_config() -> dict[str, Any]:
    path = _agent_config_path()
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, dict):
        return data
    return {}


def _config_chat_stream_url(config: dict[str, Any]) -> str | None:
    chat_stream = config.get("chat_stream")
    if not isinstance(chat_stream, dict):
        return None
    value = chat_stream.get("url")
    return value if isinstance(value, str) and value else None


def _file_token() -> str | None:
    path = _token_path()
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").strip()


def _sanitize_hostname(short_hostname: str) -> str:
    """Lowercase and reduce a hostname to alphanumeric + dashes for use as a host id."""
    lowered = short_hostname.lower()
    cleaned = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in lowered)
    cleaned = cleaned.strip("-")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned


def _hostname_host_id() -> str:
    hostname = socket.gethostname()
    short_hostname = hostname.split(".", 1)[0]
    sanitized = _sanitize_hostname(short_hostname)
    if sanitized:
        return sanitized
    raise RuntimeError(f"unknown_local_host: {hostname}")


def load_config() -> Config:
    config = _read_json_config()

    ws_url = (
        os.environ.get("AGENT_ORCH_WS_URL")
        or _config_chat_stream_url(config)
        or DEFAULT_WS_URL
    )

    token = os.environ.get("AGENT_ORCH_TOKEN")
    if token is None:
        token = _file_token()
    if token is None:
        token = ""

    host_id = os.environ.get("AGENT_ORCH_HOST_ID")
    if not host_id:
        configured_host_id = config.get("local_host_id")
        if isinstance(configured_host_id, str) and configured_host_id:
            host_id = configured_host_id
        else:
            host_id = _hostname_host_id()

    runtime_dir = Path(
        os.environ.get("AGENT_ORCH_RUNTIME_DIR") or "~/.agent-orch/"
    ).expanduser()

    memory_repo_path = None
    configured_memory_repo_path = os.environ.get("AGENT_ORCH_MEMORY_REPO", config.get("memory_repo_path"))
    if isinstance(configured_memory_repo_path, str) and configured_memory_repo_path:
        memory_repo_path = Path(configured_memory_repo_path).expanduser()

    return Config(
        ws_url=ws_url,
        token=token,
        host_id=host_id,
        runtime_dir=runtime_dir,
        memory_repo_path=memory_repo_path,
    )
