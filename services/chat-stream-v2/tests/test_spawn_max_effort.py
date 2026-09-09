"""v2 spawn resolution and launch coverage for the codex max effort tier."""
from __future__ import annotations

import asyncio

import pytest

from _shared.spawn_profiles import SpawnProfileError, resolve_spawn  # noqa: E402
import launch  # noqa: E402
from spawnctl import SpawnCtl  # noqa: E402


LOCAL = launch.local_machine(
    "hosta", cwd="/tmp/public-test", codex_bin="/opt/example/bin/codex",
    projects_root="/tmp/public-test",
)


def _controller() -> SpawnCtl:
    return SpawnCtl(store=None, sessions=None, tmux=object(), machine=LOCAL, hosts=None)


@pytest.mark.parametrize("model", ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"])
def test_v2_resolves_codex_max_and_builds_launch_tuple(model: str) -> None:
    resolved = resolve_spawn(provider="codex", model=model, effort="max", host="hosta")
    command, resolution, overrides = asyncio.run(
        _controller()._resolve_launch(
            {"provider": "codex", "model": model, "effort": "max"}, "hosta", "v2-max",
        )
    )
    assert (resolved["model"], resolved["effort"]) == (model, "max")
    assert resolution["resolved_launch_tuple"] == {"provider": "codex", "model": model, "effort": "max"}
    assert overrides["effective_effort"] == "max"
    assert f"-m {model}" in command
    assert "-c model_reasoning_effort=max" in command


def test_v2_rejects_bogus_codex_effort_before_launch() -> None:
    with pytest.raises(SpawnProfileError) as exc:
        resolve_spawn(provider="codex", model="gpt-5.6-luna", effort="banana", host="hosta")
    assert exc.value.code == "spawn_effort_unsupported"
