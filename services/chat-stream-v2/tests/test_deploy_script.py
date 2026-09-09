from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


DEPLOY_PATH = Path(__file__).resolve().parents[1] / "deploy" / "deploy.py"
SPEC = importlib.util.spec_from_file_location("chat_stream_v2_deploy", DEPLOY_PATH)
assert SPEC is not None and SPEC.loader is not None
deploy_mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = deploy_mod
SPEC.loader.exec_module(deploy_mod)


TARGET_SHA = "a" * 40


class _FakeWebsocket:
    def __init__(self, payload: str) -> None:
        self._payload = payload

    def __enter__(self) -> "_FakeWebsocket":
        return self

    def __exit__(self, *_: object) -> bool:
        return False

    def recv(self, timeout: float | None = None) -> str:
        return self._payload


def _stepping_clock(values: list[float]):
    """A monotonic-style clock that walks `values` and then holds the last one."""
    state = {"i": 0}

    def _clock() -> float:
        i = state["i"]
        if i < len(values):
            state["i"] = i + 1
            return values[i]
        return values[-1]

    return _clock


class _ScriptedRunner:
    """A deploy Runner whose result is chosen by matching a substring against the command."""

    def __init__(self, rules: dict[str, tuple[int, str, str]] | None = None) -> None:
        self.rules = rules or {}
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, cmd, cwd) -> subprocess.CompletedProcess:
        parts = tuple(str(c) for c in cmd)
        self.calls.append(parts)
        rc, out, err = 0, "", ""
        for needle, (rc2, out2, err2) in self.rules.items():
            if any(needle in part for part in parts):
                rc, out, err = rc2, out2, err2
                break
        return subprocess.CompletedProcess(parts, rc, out, err)


V2_SERVICE = deploy_mod.SERVICES["chat-streamd-v2"]


def _boot_observed(*_a, **_k):
    return deploy_mod.BootReadback(deploy_mod.BOOT_OBSERVED, "boot line observed", 0.3, 999, 111)


def test_v2_service_config_has_no_v1_requirements_reference() -> None:
    assert set(deploy_mod.SERVICES) == {"chat-streamd-v2"}
    service = deploy_mod.SERVICES["chat-streamd-v2"]

    assert service.requirements_path == "services/chat-stream-v2/requirements.txt"
    assert "services/chat-stream/" not in service.requirements_path
    assert deploy_mod._venv_python(Path("/release")) == Path(
        "/release/services/chat-stream-v2/.venv/bin/python"
    )


def test_v2_requirements_include_the_canonical_gate_dependencies() -> None:
    requirements = (DEPLOY_PATH.parents[1] / "requirements.txt").read_text(encoding="utf-8")

    for requirement in (
        "websockets==17.0.1",
        "pytest==9.0.3",
        "pytest-asyncio==1.3.0",
        "pytest-xdist==3.8.0",
        "pytest-timeout==2.4.0",
        "pytest-rerunfailures==16.3",
    ):
        assert requirement in requirements


def test_deploy_cli_exposes_only_the_v2_service() -> None:
    assert deploy_mod.build_parser().parse_args(["chat-streamd-v2"]).service == "chat-streamd-v2"
    with pytest.raises(SystemExit):
        deploy_mod.build_parser().parse_args(["chat-streamd"])


def test_verify_runtime_sha_confirms_on_matching_welcome() -> None:
    def connect_fn(*_a: object, **_k: object) -> _FakeWebsocket:
        return _FakeWebsocket(json.dumps({"runtime_sha": TARGET_SHA}))

    confirmed, last_observed = deploy_mod._verify_runtime_sha(
        TARGET_SHA,
        4321,
        deadline_seconds=5.0,
        connect_fn=connect_fn,
        clock=lambda: 0.0,
        sleep=lambda _s: None,
    )
    assert confirmed is True
    assert last_observed["sha"] == TARGET_SHA
    assert last_observed["process_state"] == "running"


def test_verify_runtime_sha_deadline_miss_returns_not_confirmed_without_raising() -> None:
    """A forced readback timeout is a classified outcome, never an exception (would become
    EXIT_REFUSED in main)."""

    def connect_fn(*_a: object, **_k: object) -> _FakeWebsocket:
        raise ConnectionRefusedError("daemon not reachable yet")

    confirmed, last_observed = deploy_mod._verify_runtime_sha(
        TARGET_SHA,
        4321,
        deadline_seconds=5.0,
        connect_fn=connect_fn,
        clock=_stepping_clock([0.0, 1.0, 99.0]),
        sleep=lambda _s: None,
    )
    assert confirmed is False
    assert last_observed["sha"] is None
    assert last_observed["process_state"] == "running_unreachable"


def _run_main(monkeypatch, capsys, stamp: dict | None = None, error: Exception | None = None):
    def fake_deploy(*_a: object, **_k: object) -> dict:
        if error is not None:
            raise error
        assert stamp is not None
        return stamp

    monkeypatch.setattr(deploy_mod, "deploy", fake_deploy)
    code = deploy_mod.main(["chat-streamd-v2"])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_main_runtime_sha_not_confirmed_maps_to_exit_5(monkeypatch, capsys) -> None:
    stamp = {
        "sha": TARGET_SHA,
        "daemon_boot_readback": {"outcome": deploy_mod.BOOT_OBSERVED, "detail": "boot line observed"},
        "daemon_runtime_readback": {
            "target_sha": TARGET_SHA,
            "outcome": deploy_mod.RUNTIME_SHA_NOT_CONFIRMED,
            "sha": None,
            "process_state": "running_unreachable",
        },
    }
    code, out, err = _run_main(monkeypatch, capsys, stamp=stamp)
    assert code == deploy_mod.EXIT_RUNTIME_SHA_NOT_CONFIRMED == 5
    assert code != deploy_mod.EXIT_REFUSED
    assert "deploy refused" not in err  # an applied bounce is never reported as refused
    assert "runtime SHA not confirmed" in err
    assert "Do NOT retry" in err
    assert json.loads(out)["daemon_runtime_readback"]["outcome"] == deploy_mod.RUNTIME_SHA_NOT_CONFIRMED


def test_main_fleet_smoke_failure_maps_to_exit_6(monkeypatch, capsys) -> None:
    stamp = {
        "sha": TARGET_SHA,
        "daemon_boot_readback": {"outcome": deploy_mod.BOOT_OBSERVED},
        "daemon_runtime_readback": {"outcome": deploy_mod.RUNTIME_SHA_OBSERVED, "sha": TARGET_SHA},
        "fleet_smoke": {"outcome": deploy_mod.FLEET_SMOKE_FAILED, "detail": '{"host": "hostb", "ok": false}'},
    }
    code, out, err = _run_main(monkeypatch, capsys, stamp=stamp)
    assert code == deploy_mod.EXIT_FLEET_SMOKE_FAILED == 6
    assert code != deploy_mod.EXIT_REFUSED
    assert "deploy refused" not in err
    assert "fleet smoke failed" in err
    assert "hostb" in err
    assert json.loads(out)["fleet_smoke"]["outcome"] == deploy_mod.FLEET_SMOKE_FAILED


def test_main_fleet_smoke_untested_is_distinct_from_pass_and_regression(monkeypatch, capsys) -> None:
    stamp = {
        "sha": TARGET_SHA,
        "daemon_boot_readback": {"outcome": deploy_mod.BOOT_OBSERVED},
        "daemon_runtime_readback": {"outcome": deploy_mod.RUNTIME_SHA_OBSERVED, "sha": TARGET_SHA},
        "fleet_smoke": {
            "outcome": deploy_mod.FLEET_SMOKE_UNTESTED,
            "untested": [{"host": "hostb", "reason": "quota_exhausted"}],
        },
    }
    code, _out, err = _run_main(monkeypatch, capsys, stamp=stamp)
    assert code == deploy_mod.EXIT_FLEET_SMOKE_UNTESTED
    assert code != deploy_mod.EXIT_OK
    assert code != deploy_mod.EXIT_FLEET_SMOKE_FAILED
    assert "UNTESTED" in err
    assert "neither a pass nor a daemon regression" in err


def test_main_clean_bounce_maps_to_exit_0(monkeypatch, capsys) -> None:
    stamp = {
        "sha": TARGET_SHA,
        "daemon_boot_readback": {"outcome": deploy_mod.BOOT_OBSERVED},
        "daemon_runtime_readback": {"outcome": deploy_mod.RUNTIME_SHA_OBSERVED, "sha": TARGET_SHA},
        "fleet_smoke": {"outcome": deploy_mod.FLEET_SMOKE_PASSED},
    }
    code, _out, err = _run_main(monkeypatch, capsys, stamp=stamp)
    assert code == deploy_mod.EXIT_OK
    assert err == ""


def test_main_pre_activation_refusal_stays_exit_2(monkeypatch, capsys) -> None:
    code, _out, err = _run_main(
        monkeypatch, capsys, error=deploy_mod.DeployError("release checkout is dirty")
    )
    assert code == deploy_mod.EXIT_REFUSED == 2
    assert "deploy refused: release checkout is dirty" in err


def test_main_post_activation_error_maps_to_exit_7(monkeypatch, capsys) -> None:
    stamp = {
        "sha": TARGET_SHA,
        "daemon_boot_readback": {"outcome": deploy_mod.BOOT_OBSERVED},
        "daemon_runtime_readback": {"outcome": deploy_mod.RUNTIME_SHA_OBSERVED, "sha": TARGET_SHA},
        "fleet_smoke": {"outcome": deploy_mod.FLEET_SMOKE_PASSED},
        "post_activation_error": "DeployError: command failed rc=5: launchctl bootstrap",
    }
    code, out, err = _run_main(monkeypatch, capsys, stamp=stamp)
    assert code == deploy_mod.EXIT_POST_ACTIVATION_ERROR == 7
    assert code != deploy_mod.EXIT_REFUSED
    assert "deploy refused" not in err
    assert "error after the restart was attempted" in err
    assert "--rollback" in err
    assert json.loads(out)["post_activation_error"].startswith("DeployError")


def test_exit_codes_are_the_documented_distinct_values() -> None:
    assert (
        deploy_mod.EXIT_OK,
        deploy_mod.EXIT_REFUSED,
        deploy_mod.EXIT_BOOT_LINE_NOT_OBSERVED,
        deploy_mod.EXIT_DAEMON_BOOT_FAILED,
        deploy_mod.EXIT_RUNTIME_SHA_NOT_CONFIRMED,
        deploy_mod.EXIT_FLEET_SMOKE_FAILED,
        deploy_mod.EXIT_POST_ACTIVATION_ERROR,
        deploy_mod.EXIT_SLOW_CONSUMER_FAILED,
        deploy_mod.EXIT_FLEET_SMOKE_UNTESTED,
    ) == (0, 2, 3, 4, 5, 6, 7, 8, 9)


def test_slow_consumer_scan_fails_only_on_post_boot_overflow_and_counts_warnings() -> None:
    fixture = "\n".join([
        "2026-09-05T00:00:01Z slow_consumer overflow: dropping client=before-boot (1011)",
        "2026-09-05T00:00:02Z chat_streamd_v2 listening on 127.0.0.1:7791",
        "2026-09-05T00:00:03Z slow_consumer queue depth=204/256 client=desktop-main",
        "2026-09-05T00:00:04Z slow_consumer overflow: dropping client=desktop-main (1011)",
    ])

    scan = deploy_mod.classify_slow_consumer_log(fixture)

    assert scan["outcome"] == deploy_mod.SLOW_CONSUMER_FAILED
    assert scan["queue_depth_warnings"] == 1
    assert [row["client"] for row in scan["failures"]] == ["desktop-main"]


def test_slow_consumer_scan_ignores_an_overflow_before_the_boot_marker() -> None:
    fixture = "\n".join([
        "2026-09-05T00:00:01Z slow_consumer overflow: dropping client=desktop-main (1011)",
        "2026-09-05T00:00:02Z chat_streamd_v2 listening on 127.0.0.1:7791",
        "2026-09-05T00:00:03Z slow_consumer queue depth=3/256 client=desktop-main",
    ])

    scan = deploy_mod.classify_slow_consumer_log(fixture)

    assert scan["outcome"] == deploy_mod.SLOW_CONSUMER_PASSED
    assert scan["queue_depth_warnings"] == 1
    assert scan["failures"] == []


def test_slow_consumer_scan_catches_ws_1011_and_4000_closes() -> None:
    fixture = "\n".join([
        "chat_streamd_v2 listening on 127.0.0.1:7791",
        "slow_consumer close code=1011 client=desktop-main",
        "websocket closed code=4000 client=pentacle-mobile",
    ])

    scan = deploy_mod.classify_slow_consumer_log(fixture)

    assert scan["outcome"] == deploy_mod.SLOW_CONSUMER_FAILED
    assert [(row["kind"], row["client"]) for row in scan["failures"]] == [
        ("1011_close", "desktop-main"),
        ("4000_close", "pentacle-mobile"),
    ]


def test_main_slow_consumer_failure_maps_to_exit_8_and_names_clients(monkeypatch, capsys) -> None:
    stamp = {
        "sha": TARGET_SHA,
        "daemon_boot_readback": {"outcome": deploy_mod.BOOT_OBSERVED},
        "daemon_runtime_readback": {"outcome": deploy_mod.RUNTIME_SHA_OBSERVED, "sha": TARGET_SHA},
        "slow_consumer": {
            "outcome": deploy_mod.SLOW_CONSUMER_FAILED,
            "failures": [{"kind": "overflow", "client": "desktop-main", "line": "fixture"}],
        },
        "fleet_smoke": {"outcome": deploy_mod.FLEET_SMOKE_PASSED},
    }

    code, _out, err = _run_main(monkeypatch, capsys, stamp=stamp)

    assert code == deploy_mod.EXIT_SLOW_CONSUMER_FAILED == 8
    assert "slow_consumer" in err
    assert "desktop-main" in err


def test_apply_post_activation_records_fleet_smoke_failure_without_raising(monkeypatch) -> None:
    """The laneM/laneN reproduction at deploy() glue level: smoke rc!=0 records failed, does
    not raise, and leaves no post_activation_error (so main maps it to exit 6, not 2/7)."""
    monkeypatch.setattr(deploy_mod, "_write_stamp", lambda _repo, _svc, _stamp: None)
    runner = _ScriptedRunner({"spawn_fleet_smoke.py": (1, "", '{"host": "hostb", "ok": false}')})
    stamp: dict = {"sha": TARGET_SHA}
    deploy_mod._apply_post_activation(
        V2_SERVICE,
        Path("/release"),
        TARGET_SHA,
        stamp,
        prior_log_size=0,
        prior_pid=111,
        reload_launchd=False,
        runner=runner,
        verify_boot=_boot_observed,
        verify_runtime=lambda _sha, _pid: (True, {"sha": TARGET_SHA, "process_state": "running", "pid": 999}),
    )
    assert stamp["fleet_smoke"]["outcome"] == deploy_mod.FLEET_SMOKE_FAILED
    assert "hostb" in stamp["fleet_smoke"]["detail"]
    assert "post_activation_error" not in stamp
    assert any("kickstart" in " ".join(c) for c in runner.calls)


def test_apply_post_activation_treats_quota_exhausted_smoke_as_untested(monkeypatch) -> None:
    """Quota-only smoke is explicitly UNTESTED, never a deploy PASS."""
    monkeypatch.setattr(deploy_mod, "_write_stamp", lambda _repo, _svc, _stamp: None)
    monkeypatch.setattr(deploy_mod, "_install_fleet_smoke_schedule", lambda _repo, _runner: None)
    quota_stdout = json.dumps({
        "ok": True, "failures": [],
        "quota_exhausted": [{"host": "hostb", "provider": "codex",
                             "prompt_mode": "promptless", "stage": "event",
                             "class": "quota_exhausted", "reset_at": "Jan 1st, 2030 12:00 PM"}],
    })
    runner = _ScriptedRunner({"spawn_fleet_smoke.py": (0, quota_stdout, "")})
    stamp: dict = {"sha": TARGET_SHA}
    deploy_mod._apply_post_activation(
        V2_SERVICE, Path("/release"), TARGET_SHA, stamp,
        prior_log_size=0, prior_pid=111, reload_launchd=False, runner=runner,
        verify_boot=_boot_observed,
        verify_runtime=lambda _sha, _pid: (True, {"sha": TARGET_SHA, "process_state": "running", "pid": 999}),
    )
    assert stamp["fleet_smoke"]["outcome"] == deploy_mod.FLEET_SMOKE_UNTESTED
    assert stamp["fleet_smoke"]["untested"][0]["host"] == "hostb"
    assert "post_activation_error" not in stamp


def test_main_quota_exhausted_smoke_is_untested_with_operator_line(monkeypatch, capsys) -> None:
    """Quota-only smoke maps to the distinct UNTESTED exit."""
    stamp = {
        "sha": TARGET_SHA,
        "daemon_boot_readback": {"outcome": deploy_mod.BOOT_OBSERVED, "detail": "boot"},
        "daemon_runtime_readback": {"target_sha": TARGET_SHA, "outcome": deploy_mod.RUNTIME_SHA_OBSERVED, "sha": TARGET_SHA},
        "fleet_smoke": {"outcome": deploy_mod.FLEET_SMOKE_UNTESTED,
                        "untested": [{"host": "hostb", "reset_at": "Jan 1st, 2030 12:00 PM"}]},
    }
    code, _out, err = _run_main(monkeypatch, capsys, stamp=stamp)
    assert code == deploy_mod.EXIT_FLEET_SMOKE_UNTESTED
    assert "hostb" in err and "UNTESTED" in err


def test_apply_post_activation_captures_schedule_install_error_without_raising(monkeypatch) -> None:
    """A raise from the recurring-smoke schedule install (post-activation) is captured, not
    propagated: it would otherwise reach main() as EXIT_REFUSED with the stamp already lost."""
    def boom(_repo, _runner):
        raise deploy_mod.DeployError("command failed rc=5: launchctl bootstrap")

    monkeypatch.setattr(deploy_mod, "_install_fleet_smoke_schedule", boom)
    monkeypatch.setattr(deploy_mod, "_write_stamp", lambda _repo, _svc, _stamp: None)
    runner = _ScriptedRunner({"spawn_fleet_smoke.py": (0, "", "")})
    stamp: dict = {"sha": TARGET_SHA}
    deploy_mod._apply_post_activation(
        V2_SERVICE,
        Path("/release"),
        TARGET_SHA,
        stamp,
        prior_log_size=0,
        prior_pid=111,
        reload_launchd=False,
        runner=runner,
        verify_boot=_boot_observed,
        verify_runtime=lambda _sha, _pid: (True, {"sha": TARGET_SHA, "process_state": "running", "pid": 999}),
    )
    assert stamp["fleet_smoke"]["outcome"] == deploy_mod.FLEET_SMOKE_PASSED
    assert stamp["post_activation_error"].startswith("DeployError")


def test_apply_post_activation_persists_the_classified_stamp(monkeypatch) -> None:
    writes: list[dict] = []
    monkeypatch.setattr(deploy_mod, "_write_stamp", lambda _repo, _svc, stamp: writes.append(dict(stamp)))
    monkeypatch.setattr(deploy_mod, "_install_fleet_smoke_schedule", lambda _repo, _runner: None)
    runner = _ScriptedRunner({"spawn_fleet_smoke.py": (0, "", "")})
    stamp: dict = {"sha": TARGET_SHA}
    deploy_mod._apply_post_activation(
        V2_SERVICE, Path("/release"), TARGET_SHA, stamp,
        prior_log_size=0, prior_pid=111, reload_launchd=False, runner=runner,
        verify_boot=_boot_observed,
        verify_runtime=lambda _s, _p: (True, {"sha": TARGET_SHA, "process_state": "running", "pid": 999}),
    )
    assert writes and writes[-1]["fleet_smoke"]["outcome"] == deploy_mod.FLEET_SMOKE_PASSED
    assert "post_activation_error" not in stamp


def test_apply_post_activation_survives_stamp_write_failure(monkeypatch) -> None:
    """A post-commit stamp-write failure must not escape deploy() (main catches only DeployError)
    nor lose the in-memory record — this was the r2 REJECT boundary."""
    monkeypatch.setattr(deploy_mod, "_install_fleet_smoke_schedule", lambda _repo, _runner: None)

    def raising_write(_repo, _svc, _stamp):
        raise OSError("disk full")

    monkeypatch.setattr(deploy_mod, "_write_stamp", raising_write)
    runner = _ScriptedRunner({"spawn_fleet_smoke.py": (0, "", "")})
    stamp: dict = {"sha": TARGET_SHA}
    deploy_mod._apply_post_activation(  # must not raise
        V2_SERVICE, Path("/release"), TARGET_SHA, stamp,
        prior_log_size=0, prior_pid=111, reload_launchd=False, runner=runner,
        verify_boot=_boot_observed,
        verify_runtime=lambda _s, _p: (True, {"sha": TARGET_SHA, "process_state": "running", "pid": 999}),
    )
    assert "OSError" in stamp["post_activation_error"]
    # the classified record survives on the returned/in-memory stamp for main() to print
    assert stamp["fleet_smoke"]["outcome"] == deploy_mod.FLEET_SMOKE_PASSED
    assert stamp["daemon_runtime_readback"]["outcome"] == deploy_mod.RUNTIME_SHA_OBSERVED


def test_apply_post_activation_captures_restart_failure_without_raising(monkeypatch) -> None:
    """A failed kickstart (the restart itself) is captured as post_activation_error, never a
    refusal, and the boot readback never runs."""
    monkeypatch.setattr(deploy_mod, "_write_stamp", lambda _repo, _svc, _stamp: None)
    runner = _ScriptedRunner({"kickstart": (1, "", "Could not kickstart")})
    stamp: dict = {"sha": TARGET_SHA}
    deploy_mod._apply_post_activation(
        V2_SERVICE,
        Path("/release"),
        TARGET_SHA,
        stamp,
        prior_log_size=0,
        prior_pid=111,
        reload_launchd=False,
        runner=runner,
    )
    assert "post_activation_error" in stamp
    assert "daemon_boot_readback" not in stamp
