from __future__ import annotations

import importlib.util
import json
import plistlib
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


@pytest.fixture
def synthetic_smoke_pass(tmp_path, monkeypatch) -> str:
    """Explicit passing evidence for stamp/schedule tests, not an empty rc=0 oracle."""
    machines_file = tmp_path / "synthetic-machines.json"
    machines_file.write_text(json.dumps([{"name": "local-fixture", "ssh_target": None}]))
    monkeypatch.setattr(deploy_mod, "_launchd_environment", lambda _label: {
        "PENTACLE_MACHINES_FILE": str(machines_file),
    })
    return json.dumps({"ok": True, "status": "PASS", "failures": [], "untested": [], "cells": [
        {"host": "local-fixture", "provider": provider, "prompt_mode": mode, "outcome": "passed"}
        for provider in ("claude", "codex") for mode in ("prompted", "promptless")
    ]})


def test_apply_post_activation_captures_schedule_install_error_without_raising(monkeypatch, synthetic_smoke_pass) -> None:
    """A raise from the recurring-smoke schedule install (post-activation) is captured, not
    propagated: it would otherwise reach main() as EXIT_REFUSED with the stamp already lost."""
    def boom(_repo, _runner):
        raise deploy_mod.DeployError("command failed rc=5: launchctl bootstrap")

    monkeypatch.setattr(deploy_mod, "_install_fleet_smoke_schedule", boom)
    monkeypatch.setattr(deploy_mod, "_write_stamp", lambda _repo, _svc, _stamp: None)
    runner = _ScriptedRunner({"spawn_fleet_smoke.py": (0, synthetic_smoke_pass, "")})
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


def test_apply_post_activation_persists_the_classified_stamp(monkeypatch, synthetic_smoke_pass) -> None:
    writes: list[dict] = []
    monkeypatch.setattr(deploy_mod, "_write_stamp", lambda _repo, _svc, stamp: writes.append(dict(stamp)))
    monkeypatch.setattr(deploy_mod, "_install_fleet_smoke_schedule", lambda _repo, _runner: None)
    runner = _ScriptedRunner({"spawn_fleet_smoke.py": (0, synthetic_smoke_pass, "")})
    stamp: dict = {"sha": TARGET_SHA}
    deploy_mod._apply_post_activation(
        V2_SERVICE, Path("/release"), TARGET_SHA, stamp,
        prior_log_size=0, prior_pid=111, reload_launchd=False, runner=runner,
        verify_boot=_boot_observed,
        verify_runtime=lambda _s, _p: (True, {"sha": TARGET_SHA, "process_state": "running", "pid": 999}),
    )
    assert writes and writes[-1]["fleet_smoke"]["outcome"] == deploy_mod.FLEET_SMOKE_PASSED
    assert "post_activation_error" not in stamp


def test_apply_post_activation_survives_stamp_write_failure(monkeypatch, synthetic_smoke_pass) -> None:
    """A post-commit stamp-write failure must not escape deploy() (main catches only DeployError)
    nor lose the in-memory record — this was the r2 REJECT boundary."""
    monkeypatch.setattr(deploy_mod, "_install_fleet_smoke_schedule", lambda _repo, _runner: None)

    def raising_write(_repo, _svc, _stamp):
        raise OSError("disk full")

    monkeypatch.setattr(deploy_mod, "_write_stamp", raising_write)
    runner = _ScriptedRunner({"spawn_fleet_smoke.py": (0, synthetic_smoke_pass, "")})
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


@pytest.mark.parametrize("configuration", ["configured", "configured-clean", "configured-json-conflict", "configured-host-subset", "missing-setting", "missing-file", "invalid-json"])
def test_post_activation_smoke_uses_installed_machine_file(
    tmp_path, monkeypatch, capsys, configuration
) -> None:
    """Execute the real smoke configuration in the spawned child; never acquire fleet cells."""
    installed_file = tmp_path / "installed machines.json"
    installed_file.write_text(json.dumps([{"name": "installed", "ssh_target": None}, {"name": "peer", "ssh_target": None}]))
    ambient_file = tmp_path / "ambient.json"
    ambient_file.write_text(json.dumps([{"name": "ambient", "ssh_target": None}]))
    monkeypatch.setenv("PENTACLE_MACHINES_FILE", str(ambient_file))
    if configuration == "configured-clean":
        monkeypatch.delenv("PENTACLE_MACHINES_FILE")
    monkeypatch.delenv("PENTACLE_MACHINES_JSON", raising=False)
    monkeypatch.delenv("PENTACLE_SMOKE_HOSTS", raising=False)
    if configuration == "configured-json-conflict":
        monkeypatch.setenv("PENTACLE_MACHINES_JSON", '[{"name": "ambient"}]')
    elif configuration == "configured-host-subset":
        monkeypatch.setenv("PENTACLE_SMOKE_HOSTS", "installed")
    environment = {"PENTACLE_MACHINES_FILE": str(installed_file)}
    if configuration == "missing-setting":
        environment = {}
    elif configuration == "missing-file":
        installed_file.unlink()
    elif configuration == "invalid-json":
        installed_file.write_text("not JSON")
    installed_plist = tmp_path / "daemon.plist"
    installed_plist.write_bytes(plistlib.dumps({"EnvironmentVariables": environment}))
    monkeypatch.setattr(deploy_mod, "_launchd_plist_path", lambda _label: installed_plist)
    monkeypatch.setattr(deploy_mod, "_venv_python", lambda *_args: Path(sys.executable))
    monkeypatch.setattr(deploy_mod, "_install_fleet_smoke_schedule", lambda *_args: None)
    monkeypatch.setattr(deploy_mod, "_scan_slow_consumer_window", lambda *_args: {"outcome": deploy_mod.SLOW_CONSUMER_PASSED})
    writes = []
    monkeypatch.setattr(deploy_mod, "_write_stamp", lambda _repo, _svc, stamp: writes.append(dict(stamp)))
    calls = []
    smoke_results = []
    smoke_path = DEPLOY_PATH.parents[3] / "services/chat-stream-v2/tools/spawn_fleet_smoke.py"

    def runner(cmd, cwd):
        parts = list(cmd)
        calls.append(parts)
        if str(smoke_path) in parts:
            # Retain the actual child environment invocation, substituting only the live-cell
            # entry point with the script's real smoke_plan (which cannot spawn or connect).
            index = parts.index(str(smoke_path))
            parts[index:index + 1] = [
                "-c", "import json,runpy,sys; from pathlib import Path; sys.path.insert(0,str(Path(sys.argv[1]).parent)); print(json.dumps(runpy.run_path(sys.argv[1])['smoke_plan']()))",
                str(smoke_path),
            ]
            result = subprocess.run(parts, cwd=cwd, text=True, capture_output=True, check=False)
            smoke_results.append(result)
            if result.returncode == 0:
                # smoke_plan validates configuration only. Supply synthetic successful cell
                # execution separately; a bare plan is not evidence of a successful smoke.
                plan = json.loads(result.stdout)
                payload = {"ok": True, "status": "PASS", "failures": [], "untested": [], "cells": [
                    {"host": host, "provider": provider, "prompt_mode": mode, "outcome": "passed"}
                    for host, provider, mode in plan["cells"]
                ]}
                return subprocess.CompletedProcess(result.args, 0, json.dumps(payload), result.stderr)
            return result
        return subprocess.CompletedProcess(parts, 0, "", "")

    stamp = {"sha": TARGET_SHA}
    deploy_mod._apply_post_activation(
        V2_SERVICE, DEPLOY_PATH.parents[3], TARGET_SHA, stamp,
        prior_log_size=0, prior_pid=111, reload_launchd=False, runner=runner,
        verify_boot=_boot_observed,
        verify_runtime=lambda _sha, _pid: (True, {"sha": TARGET_SHA, "process_state": "running", "pid": 999}),
    )
    assert stamp["sha"] == TARGET_SHA
    assert stamp["daemon_runtime_readback"]["pid"] == 999
    assert len([cmd for cmd in calls if "kickstart" in cmd]) == 1
    smoke_calls = [cmd for cmd in calls if str(smoke_path) in cmd]
    assert len(smoke_calls) == 1
    assert writes[-1] == stamp
    if configuration.startswith("configured"):
        assert stamp["fleet_smoke"]["outcome"] == deploy_mod.FLEET_SMOKE_PASSED
        plan = json.loads(smoke_results[0].stdout)
        assert plan["source"] == str(installed_file.resolve())
        assert plan["hosts"] == ["installed", "peer"]
        assert len(plan["cells"]) == 8
        assert {cell[0] for cell in plan["cells"]} == {"installed", "peer"}
        assert stamp["fleet_smoke"]["outcome"] == deploy_mod.FLEET_SMOKE_PASSED
        code, _out, _err = _run_main(monkeypatch, capsys, stamp=stamp)
        assert code == 0
    else:
        assert stamp["fleet_smoke"]["outcome"] == deploy_mod.FLEET_SMOKE_FAILED
        errors = {"missing-setting": "PENTACLE_MACHINES_FILE is required", "missing-file": "machine file does not exist", "invalid-json": "JSONDecodeError"}
        assert errors[configuration] in stamp["fleet_smoke"]["detail"]
        code, output, error = _run_main(monkeypatch, capsys, stamp=stamp)
        assert code == deploy_mod.EXIT_FLEET_SMOKE_FAILED == 6
        assert "do NOT retry the deploy" in error
        assert json.loads(output)["daemon_runtime_readback"]["pid"] == 999


class _GracefulLaunchd:
    """launchd as observed on the daemon host (2026-10-07 21:02Z): `bootout` returns at once, but the
    job stays loaded while the old daemon finishes its graceful shutdown, and a `bootstrap`
    in that window fails with rc 5 (Input/output error)."""

    def __init__(self, unload_after_s: float) -> None:
        self.now = 0.0
        self.unload_at: float | None = None
        self.unload_after_s = unload_after_s
        self.calls: list[tuple[str, float]] = []

    def loaded(self) -> bool:
        return self.unload_at is None or self.now < self.unload_at

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def monotonic(self) -> float:
        return self.now

    def runner(self, cmd, _cwd):
        verb = cmd[1]
        self.calls.append((verb, self.now))
        if verb == "bootout":
            self.unload_at = self.now + self.unload_after_s
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if verb == "print":
            if self.loaded():
                return subprocess.CompletedProcess(cmd, 0, "state = running\n\tpid = 7603\n", "")
            return subprocess.CompletedProcess(cmd, 113, "", "Could not find service")
        if verb == "bootstrap":
            if self.loaded():
                return subprocess.CompletedProcess(cmd, 5, "", "Bootstrap failed: 5: Input/output error")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(cmd)


def test_reload_waits_for_the_job_to_leave_launchd_before_bootstrapping():
    """RED for the rc=5 race: the old daemon takes 3 s to shut down after bootout."""
    launchd = _GracefulLaunchd(unload_after_s=3.0)
    deploy_mod._reload_launchd(
        "com.pentacle.chat-streamd-v2", launchd.runner, sleep=launchd.sleep, monotonic=launchd.monotonic,
    )
    bootstraps = [at for verb, at in launchd.calls if verb == "bootstrap"]
    assert bootstraps and all(at >= 3.0 for at in bootstraps), launchd.calls
    assert len(bootstraps) == 1, launchd.calls


def test_reload_wait_is_bounded_and_a_stuck_job_still_reports_the_bootstrap_failure():
    launchd = _GracefulLaunchd(unload_after_s=1e9)
    with pytest.raises(deploy_mod.DeployError, match="rc=5"):
        deploy_mod._reload_launchd(
            "com.pentacle.chat-streamd-v2", launchd.runner, sleep=launchd.sleep, monotonic=launchd.monotonic,
        )
    assert launchd.now <= deploy_mod.RELOAD_UNLOAD_WAIT_S + 1.0, launchd.now


# U2 fixtures deliberately expand C/Q/F/T, without importing the U1 emitter.
_U2_CONN_A = "62aca9ea43c14f7b8caf864618eb361b"
_U2_CONN_B = "0a6d459052154a8696f765576d015fdf"
_U2_BUCKETS = (
    "snapshot", "session.inventory", "work_lanes.inventory", "host.status",
    "working.state", "schedule.inventory", "hosts.stats", "limits.update",
    "chat.event", "pong", "other",
)


def _u2_record(event="close", *, conn_id=_U2_CONN_A, **updates):
    record = dict(
        schema=1, event=event, conn_id=conn_id, age_ms=0 if event == "connect" else 20,
        transport="loopback", tls=False, client_kind="unknown", client_name="unknown",
        client_metadata_source="unavailable", app_build=None, stream_id=None,
    )
    if event == "connect":
        record.update(queue_max=256)
    elif event in ("auth_ok", "auth_fail"):
        record.update(auth_method="none", auth_stage="hello", auth_elapsed_ms=10, auth_suppressed=0)
        if event == "auth_ok":
            record.update(events_mode="unknown", snapshot_requested=None, work_lanes_v1=None)
        else:
            record.update(reason="absent")
    else:
        record.update(
            queue_depth=0, queue_max=256, queue_peak=0,
            queued_by_type={bucket: 0 for bucket in _U2_BUCKETS},
            traffic={bucket: dict(broadcast_enqueued=0, broadcast_sent=0,
                                 broadcast_sent_bytes=0, direct_sent=0, direct_sent_bytes=0,
                                 coalesced=0, deduped=0) for bucket in _U2_BUCKETS},
            last_rx_age_ms=None, last_tx_age_ms=None, last_ping_age_ms=None,
            last_pong_age_ms=None, send_lock_wait_max_ms=0, send_call_max_ms=0,
        )
        if event == "slow_consumer":
            record.update(phase="enter", episode=1, episode_ms=0, pressure_episodes_suppressed=0)
        elif event == "force_close":
            record.update(initiator="server", cause="slow_consumer", cause_source="server_policy",
                          close_code=1011, close_reason="slow_consumer")
        elif event == "close":
            record.update(
                initiator="peer", termination="handshake", close_code=1000, close_reason="normal",
                close_sent_code=1000, close_received_code=1000,
                close_sent_reason="normal", close_received_reason="normal", duration_ms=20,
                auth_state="never", rx_messages=0, rx_bytes=0, tx_messages=0, tx_bytes=0,
                queue_snapshot_age_ms=0, auth_suppressed_total=0, pressure_episodes_suppressed_total=0,
            )
    record.update(updates)
    if event == "close":
        record["duration_ms"] = record["age_ms"]
    return record


def _u2_line(event="close", **updates):
    return "2026-10-09T20:00:00.000Z INFO chat_streamd_v2.server conn_diag " + json.dumps(
        _u2_record(event, **updates), separators=(",", ":"), allow_nan=False,
    )


def _u2_scan(*lines):
    return deploy_mod.classify_slow_consumer_log("\n".join([deploy_mod.V2_BOOT_LINE, *lines]))


def _u2_peer_close(reason="redacted", **updates):
    fields = dict(close_code=4000, close_received_code=4000, close_sent_code=4000,
                  close_reason=reason, close_received_reason=reason, close_sent_reason=reason)
    fields.update(updates)
    return _u2_line(**fields)


def _u2_liveness(reason="liveness_force_close", **updates):
    fields = dict(initiator="peer", cause="liveness", cause_source="peer_close_frame",
                  close_code=4000, close_reason=reason)
    fields.update(updates)
    return _u2_line("force_close", **fields)


def _u2_log_paths(tmp_path, monkeypatch):
    stdout, stderr = tmp_path / "daemon.out.log", tmp_path / "daemon.err.log"
    monkeypatch.setattr(deploy_mod, "_launchd_plist", lambda _label: {
        "StandardOutPath": str(stdout), "StandardErrorPath": str(stderr),
    })
    monkeypatch.setattr(deploy_mod, "_launchd_environment", lambda _label: {})
    return stdout, stderr


@pytest.mark.parametrize("event", ["connect", "auth_ok", "auth_fail", "slow_consumer", "force_close", "close"])
def test_u2_six_supported_events(event):
    scan = _u2_scan(_u2_line(event))
    assert scan["unparsed_conn_diag"] == 0
    assert scan["peer_4000_other"] == 0
    assert scan["queue_depth_warnings"] == (event == "slow_consumer")
    assert scan["overflow_drops"] == (event == "force_close")
    assert [row["kind"] for row in scan["failures"]] == (["overflow"] if event == "force_close" else [])


@pytest.mark.parametrize("payload", [
    "", "   ", "{", '{"close":1011,"text":"slow_consumer overflow 4000"',
    "not json close 1011 4000 slow_consumer overflow", "[]", "null", '"close 1011"',
])
def test_u2_malformed_never_falls_back_to_legacy(payload):
    scan = _u2_scan("WARNING conn_diag " + payload)
    assert scan["outcome"] == deploy_mod.SLOW_CONSUMER_PASSED
    assert scan["failures"] == []
    assert scan["unparsed_conn_diag"] == 1
    assert scan["queue_depth_warnings"] == scan["overflow_drops"] == scan["peer_4000_other"] == 0


@pytest.mark.parametrize("updates", [{"schema": 2}, {"schema": "1"}, {"schema": True}, {"event": "future_close"}])
def test_u2_unsupported_diagnostic_is_not_legacy(updates):
    record = _u2_record("force_close")
    record.update(updates)
    scan = _u2_scan("INFO conn_diag " + json.dumps(record))
    assert scan["outcome"] == deploy_mod.SLOW_CONSUMER_PASSED
    assert scan["failures"] == []
    assert scan["overflow_drops"] == 0


@pytest.mark.parametrize("field", ["close_code", "close_sent_code", "close_received_code"])
def test_u2_each_1011_code_field_is_a_failure(field):
    line = _u2_line(**{field: 1011})
    scan = _u2_scan(line)
    assert scan["failures"] == [{"kind": "1011_close", "client": _U2_CONN_A,
                                  "transport": "loopback", "line": line}]
    assert scan["outcome"] == deploy_mod.SLOW_CONSUMER_FAILED
    assert scan["overflow_drops"] == 0


@pytest.mark.parametrize("reason", ["liveness_force_close", "focused_heartbeat_timeout"])
def test_u2_peer_liveness_is_a_failure(reason):
    line = _u2_liveness(reason)
    scan = _u2_scan(line)
    assert scan["failures"] == [{"kind": "peer_liveness", "client": _U2_CONN_A,
                                  "transport": "loopback", "line": line}]
    assert scan["outcome"] == deploy_mod.SLOW_CONSUMER_FAILED


@pytest.mark.parametrize("code", [1000, 1001, 1006])
def test_u2_ordinary_closes_are_not_failures(code):
    scan = _u2_scan(_u2_line(close_code=code, close_received_code=None, close_sent_code=None))
    assert scan["failures"] == []
    assert scan["outcome"] == deploy_mod.SLOW_CONSUMER_PASSED
    assert scan["peer_4000_other"] == 0


def test_u2_server_keepalive_needs_terminal_1011():
    force = _u2_line("force_close", cause="liveness", cause_source="protocol_close",
                     close_reason="keepalive_ping_timeout")
    alone = _u2_scan(force)
    assert alone["failures"] == []
    assert alone["outcome"] == deploy_mod.SLOW_CONSUMER_PASSED
    close = _u2_line(initiator="server", close_sent_code=1011,
                     close_sent_reason="keepalive_ping_timeout")
    scan = _u2_scan(force, close)
    assert scan["failures"] == [{"kind": "1011_close", "client": _U2_CONN_A,
                                  "transport": "loopback", "line": close}]
    assert scan["overflow_drops"] == 0


@pytest.mark.parametrize("order", [(0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0)])
@pytest.mark.parametrize("include_overflow", [False, True])
def test_u2_precedence_and_duplicate_records_are_order_independent(order, include_overflow):
    # Deliberately colliding valid records prove precedence under replay/reordering.
    rows = [_u2_line(close_code=1011), _u2_liveness(), _u2_line("force_close")]
    selected = [rows[index] for index in order if include_overflow or index != 2]
    scan = _u2_scan(*selected, *selected, _u2_peer_close())
    expected = "overflow" if include_overflow else "peer_liveness"
    assert scan["failures"] == [{"kind": expected, "client": _U2_CONN_A,
                                  "transport": "loopback", "line": rows[2 if include_overflow else 1]}]
    assert scan["overflow_drops"] == int(include_overflow)
    assert scan["peer_4000_other"] == 0


def test_u2_distinct_connections_do_not_merge():
    scan = _u2_scan(_u2_line("force_close"), _u2_line(close_code=1011),
                    _u2_line(close_received_code=1011, conn_id=_U2_CONN_B))
    assert [(row["kind"], row["client"]) for row in scan["failures"]] == [
        ("overflow", _U2_CONN_A), ("1011_close", _U2_CONN_B),
    ]
    assert scan["overflow_drops"] == 1


@pytest.mark.parametrize("reason", ["redacted", "unknown", "empty", "normal", "going_away", "slow_consumer", "keepalive_ping_timeout"])
def test_u2_other_peer_4000_is_telemetry(reason):
    scan = _u2_scan(_u2_peer_close(reason))
    assert scan["outcome"] == deploy_mod.SLOW_CONSUMER_PASSED
    assert scan["failures"] == []
    assert scan["peer_4000_other"] == 1
    assert scan["overflow_drops"] == 0


@pytest.mark.parametrize("reason", ["liveness_force_close", "focused_heartbeat_timeout"])
def test_u2_recognized_close_alone_is_not_other_telemetry(reason):
    scan = _u2_scan(_u2_peer_close(reason))
    assert scan["failures"] == []
    assert scan["peer_4000_other"] == 0


@pytest.mark.parametrize("initiator", ["server", "unknown"])
def test_u2_non_peer_4000_is_not_peer_telemetry(initiator):
    scan = _u2_scan(_u2_peer_close(initiator=initiator))
    assert scan["failures"] == []
    assert scan["peer_4000_other"] == 0


def test_u2_pressure_counts_enters_only():
    scan = _u2_scan(_u2_line("slow_consumer"), _u2_line("slow_consumer", phase="recover"),
                    _u2_line("slow_consumer", episode=2),
                    _u2_line("slow_consumer", phase="recover", episode=2))
    assert scan["queue_depth_warnings"] == 2
    assert scan["failures"] == []
    assert scan["overflow_drops"] == 0


@pytest.mark.parametrize("updates", [
    {"conn_id": "10119ea543c14f7b8caf864618eb361b"},
    {"conn_id": "40009ea543c14f7b8caf864618eb361b"},
    {"age_ms": 1011}, {"age_ms": 4000},
])
@pytest.mark.parametrize("event", ["close", "auth_ok", "auth_fail", "slow_consumer"])
def test_u2_incidental_numbers_do_not_fail(event, updates):
    if event == "slow_consumer":
        updates = {**updates, "phase": "recover"}
    scan = _u2_scan(_u2_line(event, **updates))
    assert scan["outcome"] == deploy_mod.SLOW_CONSUMER_PASSED
    assert scan["failures"] == []
    assert scan["peer_4000_other"] == scan["unparsed_conn_diag"] == 0


def test_u2_failure_keeps_raw_line_and_transport():
    record = _u2_record("force_close")
    raw = "  INFO conn_diag " + json.dumps(record) + "  "
    scan = _u2_scan(raw)
    assert scan["failures"] == [{"kind": "overflow", "client": _U2_CONN_A,
                                  "transport": "loopback", "line": raw}]


def test_u2_legacy_unknown_identity_and_branch_precedence():
    lines = ["slow_consumer overflow close 1011 4000", "slow_consumer close 1011 websocket 4000",
             "websocket closed 4000 client=legacy,", "slow_consumer queue depth=9 client=legacy"]
    scan = _u2_scan(*lines, lines[0])
    assert scan["failures"] == [
        {"kind": "overflow", "client": "unknown", "line": lines[0]},
        {"kind": "1011_close", "client": "unknown", "line": lines[1]},
        {"kind": "4000_close", "client": "legacy", "line": lines[2]},
        {"kind": "overflow", "client": "unknown", "line": lines[0]},
    ]
    assert scan["queue_depth_warnings"] == 1
    assert scan["overflow_drops"] == 2


def test_u2_mixed_formats_keep_independent_rules():
    legacy = "slow_consumer overflow client=old-daemon 1011"
    structured = _u2_line("force_close")
    scan = _u2_scan(legacy, structured, _u2_line(close_code=1011), _u2_peer_close(conn_id=_U2_CONN_B))
    assert [(row["kind"], row["client"]) for row in scan["failures"]] == [
        ("overflow", "old-daemon"), ("overflow", _U2_CONN_A),
    ]
    assert scan["overflow_drops"] == 2
    assert scan["peer_4000_other"] == 1


@pytest.mark.parametrize("boot_line", [deploy_mod.V2_BOOT_LINE, "custom boot"])
def test_u2_missing_marker_selects_empty_window(boot_line):
    scan = deploy_mod.classify_slow_consumer_log(
        "\n".join(["websocket close 1011", _u2_line("force_close"), "conn_diag {"]), boot_line=boot_line,
    )
    assert scan == {"class": "slow_consumer", "outcome": deploy_mod.SLOW_CONSUMER_PASSED,
                    "boot_marker_seen": False, "queue_depth_warnings": 0, "overflow_drops": 0,
                    "peer_4000_other": 0, "unparsed_conn_diag": 0, "failures": []}


def test_u2_explicit_no_marker_scans_whole_text():
    scan = deploy_mod.classify_slow_consumer_log(
        "\n".join(["websocket close 4000 client=legacy", _u2_peer_close(), "conn_diag {"]), boot_line=None,
    )
    assert scan["boot_marker_seen"] is True
    assert [row["kind"] for row in scan["failures"]] == ["4000_close"]
    assert scan["peer_4000_other"] == scan["unparsed_conn_diag"] == 1


def test_u2_first_marker_preserves_window_and_ignores_preboot():
    scan = deploy_mod.classify_slow_consumer_log("\n".join([
        "websocket close 1011", _u2_line("force_close"), "conn_diag {",
        deploy_mod.V2_BOOT_LINE, _u2_peer_close(), deploy_mod.V2_BOOT_LINE,
        "slow_consumer queue depth=1", _u2_line("slow_consumer"),
    ]))
    assert scan["boot_marker_seen"] is True
    assert scan["failures"] == []
    assert scan["peer_4000_other"] == 1
    assert scan["unparsed_conn_diag"] == 0
    assert scan["queue_depth_warnings"] == 2


@pytest.mark.parametrize("logs", ["none", "missing", "empty"])
def test_u2_scan_empty_defaults(tmp_path, monkeypatch, logs):
    stdout, stderr = _u2_log_paths(tmp_path, monkeypatch)
    if logs == "none":
        monkeypatch.setattr(deploy_mod, "_launchd_plist", lambda _: {})
    elif logs == "empty":
        stdout.touch()
        stderr.touch()
    scan = deploy_mod._scan_slow_consumer_window(V2_SERVICE, 0)
    assert scan["outcome"] == deploy_mod.SLOW_CONSUMER_FAILED
    assert scan["boot_marker_seen"] is False
    assert scan["failures"] == []
    assert scan["scan_complete"] is (logs != "missing")
    assert scan["log_paths"] == ([] if logs == "none" else [str(stdout), str(stderr)])


@pytest.mark.parametrize("marker", [False, True])
def test_u2_scan_handoff_offsets_and_telemetry(tmp_path, monkeypatch, marker):
    stdout, stderr = _u2_log_paths(tmp_path, monkeypatch)
    old = "\n".join([deploy_mod.V2_BOOT_LINE, "slow_consumer overflow client=old", _u2_line("force_close"), ""])
    stdout.write_text(old)
    stderr.write_text(old)
    offsets = deploy_mod._capture_log_offsets((stdout, stderr))
    with stdout.open("a") as stream:
        stream.write((deploy_mod.V2_BOOT_LINE if marker else "no boot yet") + "\n")
        stream.write(_u2_peer_close(conn_id=_U2_CONN_B) + "\nconn_diag {\n")
    with stderr.open("a") as stream:
        stream.write("websocket close 4000 client=legacy\n" + _u2_peer_close() + "\nconn_diag {\n")
    scan = deploy_mod._scan_slow_consumer_window(V2_SERVICE, offsets)
    assert scan["boot_marker_seen"] is marker
    assert [row["kind"] for row in scan["failures"]] == (["4000_close"] if marker else [])
    assert scan["peer_4000_other"] == scan["unparsed_conn_diag"] == (2 if marker else 0)
    assert scan["overflow_drops"] == 0
    assert scan["log_paths"] == [str(stdout), str(stderr)]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("force_kind", ["overflow", "peer_liveness"])
def test_u2_scan_deduplicates_across_sinks(tmp_path, monkeypatch, reverse, force_kind):
    stdout, stderr = _u2_log_paths(tmp_path, monkeypatch)
    force = _u2_line("force_close") if force_kind == "overflow" else _u2_liveness()
    first = [force, _u2_liveness()]
    second = [_u2_line(close_received_code=1011), _u2_peer_close(), _u2_line("slow_consumer")]
    if reverse:
        first, second = second, first
    stdout.write_text("\n".join([deploy_mod.V2_BOOT_LINE, *first, "conn_diag {", ""]))
    stderr.write_text("\n".join([*second, ""]))
    scan = deploy_mod._scan_slow_consumer_window(V2_SERVICE, 0)
    assert scan["failures"] == [{"kind": force_kind, "client": _U2_CONN_A,
                                  "transport": "loopback", "line": force}]
    assert scan["overflow_drops"] == int(force_kind == "overflow")
    assert scan["peer_4000_other"] == 0
    assert scan["unparsed_conn_diag"] == scan["queue_depth_warnings"] == 1


def test_u2_each_sink_keeps_its_own_first_marker_selection(tmp_path, monkeypatch):
    stdout, stderr = _u2_log_paths(tmp_path, monkeypatch)
    stdout.write_text("\n".join([_u2_line("force_close"), deploy_mod.V2_BOOT_LINE, _u2_peer_close(), ""]))
    stderr.write_text("\n".join(["slow_consumer overflow", "conn_diag {", deploy_mod.V2_BOOT_LINE,
                                "slow_consumer queue depth=1", _u2_line("slow_consumer"), ""]))
    scan = deploy_mod._scan_slow_consumer_window(V2_SERVICE, 0)
    assert scan["failures"] == []
    assert scan["peer_4000_other"] == 1
    assert scan["queue_depth_warnings"] == 2
    assert scan["unparsed_conn_diag"] == 0


@pytest.mark.parametrize("fleet_state,expected_exit", [
    ("passed", 0), ("partial", 10), ("untested", 9), ("failed", 6),
])
@pytest.mark.parametrize("with_failure", [False, True])
def test_u2_real_scan_stamp_stdout_preserves_fleet_verdicts(
    tmp_path, monkeypatch, capsys, fleet_state, expected_exit, with_failure,
):
    # Do not use _apply: its legacy scan/write stubs would erase this boundary.
    from .test_deploy_fleet_verdict import _rows, _partial_payload

    stdout, stderr = _u2_log_paths(tmp_path, monkeypatch)
    old = "\n".join([deploy_mod.V2_BOOT_LINE, "slow_consumer overflow client=historical", ""])
    stdout.write_text(old)
    stderr.write_text(old)
    offsets = deploy_mod._capture_log_offsets((stdout, stderr))
    with stdout.open("a") as stream:
        stream.write(deploy_mod.V2_BOOT_LINE + "\n")
        stream.write(_u2_peer_close() + "\n")
    with stderr.open("a") as stream:
        stream.write('WARNING conn_diag {"close":1011,"text":"slow_consumer overflow 4000"\n')
        if with_failure:
            stream.write(_u2_line("force_close", conn_id=_U2_CONN_B) + "\n")
            stream.write(_u2_line(conn_id=_U2_CONN_B, close_sent_code=1011) + "\n")

    machines = tmp_path / "synthetic-machines.json"
    machines.write_text(json.dumps([
        {"name": "primary", "ssh_target": None},
        {"name": "satellite", "ssh_target": "satellite.example.com"},
    ]))
    monkeypatch.setattr(deploy_mod, "_launchd_environment", lambda _: {
        "PENTACLE_MACHINES_FILE": str(machines),
    })
    schedules = []
    monkeypatch.setattr(deploy_mod, "_install_fleet_smoke_schedule", lambda *_: schedules.append(True))
    if fleet_state == "passed":
        payload = {"ok": True, "status": "PASS", "cells": _rows("primary") + _rows("satellite"),
                   "failures": [], "untested": []}
        rc = 0
    else:
        payload = _partial_payload()
        rc = 2
        if fleet_state == "untested":
            for row in payload["untested"]:
                row["reason"] = "quota_exhausted"
        elif fleet_state == "failed":
            payload["failures"] = [{"host": "satellite", "provider": "codex", "prompt_mode": "promptless",
                                    "class": "failure", "reason": "event", "detail": "fixture failure"}]
            rc = 1
    runner = _ScriptedRunner({"spawn_fleet_smoke.py": (rc, json.dumps(payload), "")})
    release = tmp_path / "release"
    release.mkdir()
    stamp = {"sha": TARGET_SHA}
    deploy_mod._apply_post_activation(
        V2_SERVICE, release, TARGET_SHA, stamp,
        prior_log_size=offsets, prior_pid=111, reload_launchd=False, runner=runner,
        verify_boot=_boot_observed,
        verify_runtime=lambda *_: (True, {"sha": TARGET_SHA, "pid": 999}),
    )
    assert "post_activation_error" not in stamp
    persisted = json.loads(deploy_mod._stamp_path(release, V2_SERVICE).read_text())
    assert persisted == stamp
    assert stamp["fleet_smoke"]["outcome"] == fleet_state
    assert schedules == ([True] if fleet_state == "passed" else [])
    scan = persisted["slow_consumer"]
    assert scan["peer_4000_other"] == scan["unparsed_conn_diag"] == 1
    assert scan["overflow_drops"] == int(with_failure)
    assert scan["queue_depth_warnings"] == 0
    assert scan["boot_marker_seen"] is True
    assert [row["kind"] for row in scan["failures"]] == (["overflow"] if with_failure else [])
    assert scan["outcome"] == (deploy_mod.SLOW_CONSUMER_FAILED if with_failure else deploy_mod.SLOW_CONSUMER_PASSED)
    assert scan["log_paths"] == [str(stdout), str(stderr)]
    code, output, error = _run_main(monkeypatch, capsys, stamp=stamp)
    assert json.loads(output) == persisted
    assert code == (deploy_mod.EXIT_SLOW_CONSUMER_FAILED if with_failure else expected_exit)
    if with_failure:
        assert code == 8
        assert _U2_CONN_B in error
    assert len([call for call in runner.calls if "kickstart" in call]) == 1
    assert len([call for call in runner.calls if any("spawn_fleet_smoke.py" in part for part in call)]) == 1
