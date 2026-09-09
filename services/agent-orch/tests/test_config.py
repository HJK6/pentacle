from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_orch.config import load_config


def test_config_precedence(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (home / ".agent-orch").mkdir()
    (home / ".agent-orch" / "config.json").write_text(
        json.dumps({"chat_stream": {"url": "ws://file"}, "local_host_id": "file-host"}),
        encoding="utf-8",
    )
    token_dir = home / ".config" / "pentacle-stream"
    token_dir.mkdir(parents=True)
    (token_dir / "token").write_text(" tok \n", encoding="utf-8")
    monkeypatch.setattr("socket.gethostname", lambda: "hostb.local")

    config = load_config()
    assert config.ws_url == "ws://file"
    assert config.token == "tok"
    assert config.host_id == "file-host"
    assert config.runtime_dir == home / ".agent-orch"
    assert config.memory_repo_path is None

    monkeypatch.setenv("AGENT_ORCH_WS_URL", "ws://env")
    monkeypatch.setenv("AGENT_ORCH_TOKEN", "tok")
    monkeypatch.setenv("AGENT_ORCH_HOST_ID", "env-host")
    monkeypatch.setenv("AGENT_ORCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    config = load_config()
    assert config.ws_url == "ws://env"
    assert config.token == "tok"
    assert config.host_id == "env-host"
    assert config.runtime_dir == tmp_path / "runtime"
    assert config.memory_repo_path is None


def test_memory_repo_path_defaults_to_none(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("socket.gethostname", lambda: "hostc.local")

    config = load_config()

    assert config.memory_repo_path is None


def test_memory_repo_path_loads_from_json(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    memory_repo = tmp_path / "memory repo"
    memory_repo.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (home / ".agent-orch").mkdir()
    (home / ".agent-orch" / "config.json").write_text(
        json.dumps(
            {
                "local_host_id": "hostc",
                "memory_repo_path": str(memory_repo),
            }
        ),
        encoding="utf-8",
    )

    config = load_config()

    assert config.memory_repo_path == memory_repo


def test_unknown_hostname_falls_back_to_sanitized(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("AGENT_ORCH_HOST_ID", raising=False)
    monkeypatch.setattr("socket.gethostname", lambda: "unknown.example.com")

    config = load_config()

    # Unknown hostnames now fall back to a sanitized form of the short hostname
    # so fresh installs work without explicitly setting `local_host_id`.
    # The short hostname `unknown` survives sanitization unchanged.
    assert config.host_id == "unknown"


def test_messy_hostname_is_sanitized(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("AGENT_ORCH_HOST_ID", raising=False)
    monkeypatch.setattr("socket.gethostname", lambda: "MBP_2024.local")

    config = load_config()

    # Underscores become dashes; case is lowered. `local` domain stripped first.
    assert config.host_id == "mbp-2024"


def test_canonical_example_hostname_still_canonicalizes(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("AGENT_ORCH_HOST_ID", raising=False)
    monkeypatch.setattr("socket.gethostname", lambda: "hosta.local")

    config = load_config()

    # Canonical public host labels remain stable.
    assert config.host_id == "hosta"


def test_canonical_host_d_hostname(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("AGENT_ORCH_HOST_ID", raising=False)
    monkeypatch.setattr("socket.gethostname", lambda: "hostd.local")

    config = load_config()

    assert config.host_id == "hostd"


def test_empty_hostname_still_raises(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("AGENT_ORCH_HOST_ID", raising=False)
    monkeypatch.setattr("socket.gethostname", lambda: "")

    with pytest.raises(RuntimeError, match="unknown_local_host"):
        load_config()
