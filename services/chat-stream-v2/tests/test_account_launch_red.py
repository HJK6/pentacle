"""P0 reproducer against the current launch adapter's missing home selection."""
from types import SimpleNamespace
import launch


def test_named_home_is_used_by_real_launch_adapter(tmp_path):
    machine = launch.local_machine("host-a", cwd=str(tmp_path), codex_bin="/bin/echo")
    account = SimpleNamespace(provider="codex", host="host-a", home=str(tmp_path / "named-home"),
        executable="/bin/echo", transcript_root=str(tmp_path / "named-home/sessions"))
    plan = launch.build_launch(machine, provider="codex", tmux_session="fixture",
        launch_model="fixture-model", launch_effort="high", account_binding=account)
    assert "CODEX_HOME=" in plan.command
    assert account.home in plan.command
