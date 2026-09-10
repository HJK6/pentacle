from __future__ import annotations

from pathlib import Path

from agent_orch.config import Config
from agent_orch.role_baseline import load_role_baseline, resolve_memory_repo_path


def _config(memory_repo_path: Path | None = None) -> Config:
    return Config(
        ws_url="ws://unused",
        token="",
        host_id="hostc",
        runtime_dir=Path("/tmp/agent-orch-test"),
        memory_repo_path=memory_repo_path,
    )


def test_resolve_memory_repo_path_uses_explicit_env_override(monkeypatch, tmp_path: Path) -> None:
    env_repo = tmp_path / "env-memory"
    config_repo = tmp_path / "config-memory"
    env_repo.mkdir()
    config_repo.mkdir()
    monkeypatch.setenv("AGENT_ORCH_MEMORY_REPO", str(env_repo))

    assert resolve_memory_repo_path(_config(config_repo)) == env_repo


def test_resolve_memory_repo_path_uses_env_var(monkeypatch, tmp_path: Path) -> None:
    env_repo = tmp_path / "env-memory"
    env_repo.mkdir()
    monkeypatch.setenv("AGENT_ORCH_MEMORY_REPO", str(env_repo))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    assert resolve_memory_repo_path(_config()) == env_repo


def test_resolve_memory_repo_path_does_not_probe_home_repos(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    memory_repo = home / "repos" / "hosta-memory"
    memory_repo.mkdir(parents=True)
    monkeypatch.delenv("AGENT_ORCH_MEMORY_REPO", raising=False)
    monkeypatch.setenv("HOME", str(home))

    assert resolve_memory_repo_path(_config()) is None


def test_resolve_memory_repo_path_does_not_probe_home_fallback(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    memory_repo = home / "hosta-memory"
    memory_repo.mkdir(parents=True)
    monkeypatch.delenv("AGENT_ORCH_MEMORY_REPO", raising=False)
    monkeypatch.setenv("HOME", str(home))

    assert resolve_memory_repo_path(_config()) is None


def test_resolve_memory_repo_path_returns_none_when_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AGENT_ORCH_MEMORY_REPO", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    assert resolve_memory_repo_path(_config()) is None


def test_resolve_memory_repo_path_never_raises_on_path_errors(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AGENT_ORCH_MEMORY_REPO", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    def raise_os_error(self: Path) -> bool:
        raise OSError("boom")

    monkeypatch.setattr(Path, "exists", raise_os_error)

    assert resolve_memory_repo_path(_config(tmp_path / "broken")) is None


def test_load_role_baseline_strips_front_matter(monkeypatch, tmp_path: Path) -> None:
    memory_repo = tmp_path / "memory"
    agents = memory_repo / "agents"
    agents.mkdir(parents=True)
    baseline = agents / "qa_baseline.md"
    baseline.write_text(
        "---\nid: qa\nrole: qa\n---\n\nRule body.\nKeep this.\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("AGENT_ORCH_MEMORY_REPO", raising=False)

    loaded = load_role_baseline(_config(memory_repo), "qa")

    assert loaded == {
        "role": "qa",
        "source_path": str(baseline.resolve()),
        "content": "Rule body.\nKeep this.\n",
    }


def test_load_role_baseline_without_front_matter_returns_raw_text(monkeypatch, tmp_path: Path) -> None:
    memory_repo = tmp_path / "memory"
    agents = memory_repo / "agents"
    agents.mkdir(parents=True)
    baseline = agents / "qa_baseline.md"
    raw_text = "\n--- not front matter because text already started\nBody\n"
    baseline.write_text(raw_text, encoding="utf-8")
    monkeypatch.delenv("AGENT_ORCH_MEMORY_REPO", raising=False)

    loaded = load_role_baseline(_config(memory_repo), "qa")

    assert loaded is not None
    assert loaded["content"] == raw_text


def test_load_role_baseline_returns_none_without_memory_repo(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AGENT_ORCH_MEMORY_REPO", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    assert load_role_baseline(_config(), "qa") is None


def test_load_role_baseline_returns_none_when_role_file_missing(tmp_path: Path) -> None:
    memory_repo = tmp_path / "memory"
    (memory_repo / "agents").mkdir(parents=True)

    assert load_role_baseline(_config(memory_repo), "qa") is None


def test_load_role_baseline_never_raises_on_read_error(monkeypatch, tmp_path: Path) -> None:
    memory_repo = tmp_path / "memory"
    agents = memory_repo / "agents"
    agents.mkdir(parents=True)
    (agents / "qa_baseline.md").write_text("body\n", encoding="utf-8")

    def raise_os_error(self: Path, encoding: str | None = None) -> str:
        raise OSError("boom")

    monkeypatch.setattr(Path, "read_text", raise_os_error)

    assert load_role_baseline(_config(memory_repo), "qa") is None

