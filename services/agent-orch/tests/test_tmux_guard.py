from __future__ import annotations

import os
import subprocess
from pathlib import Path


GUARD = Path(__file__).resolve().parents[1] / "deploy" / "bin" / "tmux"


def _fake_tmux(tmp_path: Path) -> Path:
    fake = tmp_path / "real-tmux"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$@\" > \"$PENTACLE_FAKE_TMUX_ARGS\"\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


def _run_guard(tmp_path: Path, args: list[str], *, tmux_env: bool) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PENTACLE_REAL_TMUX": str(_fake_tmux(tmp_path)),
        "PENTACLE_FAKE_TMUX_ARGS": str(tmp_path / "args.txt"),
    }
    if tmux_env:
        env["TMUX"] = "/tmp/tmux-default,123,0"
    else:
        env.pop("TMUX", None)
    return subprocess.run(
        ["bash", str(GUARD), *args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_tmux_guard_refuses_bare_kill_server_inside_tmux(tmp_path: Path) -> None:
    result = _run_guard(tmp_path, ["kill-server"], tmux_env=True)

    assert result.returncode == 64
    assert "explicit -S or -L" in result.stderr
    assert not (tmp_path / "args.txt").exists()


def test_tmux_guard_allows_explicit_socket_inside_tmux(tmp_path: Path) -> None:
    result = _run_guard(tmp_path, ["-S", "/tmp/private-socket", "kill-server"], tmux_env=True)

    assert result.returncode == 0
    assert (tmp_path / "args.txt").read_text(encoding="utf-8").splitlines() == [
        "-S",
        "/tmp/private-socket",
        "kill-server",
    ]


def test_tmux_guard_allows_outside_tmux_behavior(tmp_path: Path) -> None:
    result = _run_guard(tmp_path, ["kill-server"], tmux_env=False)

    assert result.returncode == 0
    assert (tmp_path / "args.txt").read_text(encoding="utf-8").splitlines() == ["kill-server"]
