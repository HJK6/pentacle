from __future__ import annotations

from harness import run_certified_live_gate as runner


def test_certified_runner_maps_stages_to_v2_gates() -> None:
    assert runner.command("A")[-1] == "unit"
    assert runner.command("B")[-1] == "merge"
    assert runner.V2_GATE == runner.REPO_ROOT / "services/chat-stream-v2/tools/run_gate.py"


def test_certified_runner_has_no_v1_test_or_manifest_dependency() -> None:
    source = runner.Path(runner.__file__).read_text(encoding="utf-8")

    assert "live_gate_manifest" not in source
    assert "services/chat-stream/" not in source
