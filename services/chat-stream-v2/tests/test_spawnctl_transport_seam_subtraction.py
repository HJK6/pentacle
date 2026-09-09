"""Static guard for retiring SpawnCtl's older transport-double fallbacks."""

from pathlib import Path


def test_spawnctl_uses_only_the_concrete_transport_protocol() -> None:
    source = (Path(__file__).resolve().parents[1] / "spawnctl.py").read_text()

    assert "except AttributeError:" not in source
    assert 'getattr(tmux, "session_state", None)' not in source
    assert "return \"alive\" if await tmux.has_session(name) else \"gone\"" not in source
    assert "await tmux.session_state(name)" in source
