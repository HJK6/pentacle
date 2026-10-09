"""TH-H5 synthetic post-activation smoke receipts; no fleet processes or sockets."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from .test_deploy_script import (
    TARGET_SHA, V2_SERVICE, _ScriptedRunner, _boot_observed, _run_main, deploy_mod,
)


PROVIDERS = ("claude", "codex")
MODES = ("prompted", "promptless")


def _rows(host: str, outcome: str = "passed", reason: str | None = None) -> list[dict]:
    return [
        {"host": host, "provider": provider, "prompt_mode": mode,
         "outcome": outcome, **({"reason": reason} if reason else {})}
        for provider in PROVIDERS for mode in MODES
    ]


def _apply(tmp_path, monkeypatch, payload, rc=0, *, machines=None):
    machines_file = tmp_path / "synthetic-machines.json"
    machines_file.write_text(json.dumps(machines if machines is not None else [
        {"name": "primary", "ssh_target": None},
        {"name": "satellite", "ssh_target": "satellite.example.com"},
    ]))
    monkeypatch.setattr(deploy_mod, "_launchd_environment", lambda _label: {
        "PENTACLE_MACHINES_FILE": str(machines_file),
    })
    monkeypatch.setattr(deploy_mod, "_scan_slow_consumer_window", lambda *_: {})
    writes, schedules = [], []
    monkeypatch.setattr(deploy_mod, "_write_stamp", lambda _repo, _svc, stamp: writes.append(json.loads(json.dumps(stamp))))
    monkeypatch.setattr(deploy_mod, "_install_fleet_smoke_schedule", lambda *_: schedules.append(True))
    runner = _ScriptedRunner({"spawn_fleet_smoke.py": (rc, json.dumps(payload), "")})
    stamp = {"sha": TARGET_SHA}
    deploy_mod._apply_post_activation(
        V2_SERVICE, Path("/synthetic-release"), TARGET_SHA, stamp,
        prior_log_size=0, prior_pid=111, reload_launchd=False, runner=runner,
        verify_boot=_boot_observed,
        verify_runtime=lambda *_: (True, {"sha": TARGET_SHA, "pid": 999}),
    )
    assert "post_activation_error" not in stamp
    assert writes[-1] == stamp
    assert len([call for call in runner.calls if "kickstart" in call]) == 1
    assert len([call for call in runner.calls if any("spawn_fleet_smoke.py" in part for part in call)]) == 1
    return stamp, schedules


def _partial_payload():
    unreachable = _rows("satellite", "UNTESTED", "host_unavailable")
    return {"ok": False, "status": "UNTESTED", "cells": _rows("primary") + unreachable,
            "untested": unreachable, "failures": []}


def test_local_pass_remote_unreachable_is_partial_never_a_release_pass(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PENTACLE_HOST_ID", "satellite")
    monkeypatch.setenv("AGENT_ORCH_HOST_ID", "wrong-ambient-host")
    monkeypatch.setenv("PENTACLE_MACHINES_JSON", '[{"name":"wrong-ambient-host"}]')
    stamp, schedules = _apply(tmp_path, monkeypatch, _partial_payload(), rc=2)
    assert stamp["fleet_smoke"]["outcome"] == "partial"
    assert stamp["fleet_smoke"]["unreachable_hosts"] == ["satellite"]
    assert stamp["fleet_smoke"]["local_hosts"] == ["primary"]
    assert schedules == []
    code, output, error = _run_main(monkeypatch, capsys, stamp=stamp)
    assert code == deploy_mod.EXIT_FLEET_SMOKE_PARTIAL == 10
    assert code != deploy_mod.EXIT_OK
    assert "PARTIAL" in error and "satellite" in error
    assert "do NOT retry" in error
    assert json.loads(output)["fleet_smoke"]["outcome"] == "partial"


@pytest.mark.parametrize("host", ["primary", "satellite"])
@pytest.mark.parametrize("duplicate_pass", [True, False])
def test_failure_only_in_failures_dominates_pass_evidence(tmp_path, monkeypatch, capsys, host, duplicate_pass):
    failure = {"host": host, "provider": "codex", "prompt_mode": "promptless",
               "class": "failure", "reason": "teardown", "detail": "synthetic teardown failure"}
    payload = {"ok": True, "cells": _rows("primary") + _rows("satellite"), "failures": [failure]}
    if not duplicate_pass:
        payload["cells"] = [row for row in payload["cells"] if not (
            row["host"] == host and row["provider"] == "codex" and row["prompt_mode"] == "promptless"
        )]
    # Even a contradictory zero exit and pass row cannot erase explicit failure evidence.
    stamp, schedules = _apply(tmp_path, monkeypatch, payload)
    code, output, _error = _run_main(monkeypatch, capsys, stamp=stamp)
    assert code == deploy_mod.EXIT_FLEET_SMOKE_FAILED == 6
    assert schedules == []
    cells = json.loads(output)["fleet_smoke"]["cells"]
    failed = [row for row in cells if row["outcome"] == "failed"]
    assert len(failed) == 1 and failed[0]["host"] == host
    assert failed[0]["reason"] == "teardown"


@pytest.mark.parametrize("local_state", ["quota", "missing", "unreachable"])
def test_local_incomplete_never_becomes_partial(tmp_path, monkeypatch, capsys, local_state):
    payload = _partial_payload()
    payload["cells"] = [row for row in payload["cells"] if row["host"] != "primary"]
    if local_state != "missing":
        local = _rows("primary", "UNTESTED", "quota_exhausted" if local_state == "quota" else "host_unavailable")
        payload["cells"] += local
        payload["untested"] += local
    stamp, schedules = _apply(tmp_path, monkeypatch, payload, rc=2)
    assert stamp["fleet_smoke"]["outcome"] == "untested"
    code, _out, _err = _run_main(monkeypatch, capsys, stamp=stamp)
    assert code == deploy_mod.EXIT_FLEET_SMOKE_UNTESTED == 9
    assert schedules == []
    assert len(stamp["fleet_smoke"]["cells"]) == 8


def test_remote_quota_is_still_exit_9(tmp_path, monkeypatch, capsys):
    payload = _partial_payload()
    for row in payload["untested"]:
        row["reason"] = "quota_exhausted"
    stamp, schedules = _apply(tmp_path, monkeypatch, payload, rc=2)
    code, _out, _err = _run_main(monkeypatch, capsys, stamp=stamp)
    assert code == deploy_mod.EXIT_FLEET_SMOKE_UNTESTED == 9
    assert schedules == []


def test_every_host_cell_and_reason_survives_stamp_and_stdout(tmp_path, monkeypatch, capsys):
    payload = _partial_payload()
    failed = {"host": "satellite", "provider": "codex", "prompt_mode": "promptless",
              "class": "failure", "reason": "event", "detail": "evidence-" + "x" * 1800}
    payload["failures"] = [failed]
    stamp, schedules = _apply(tmp_path, monkeypatch, payload, rc=1)
    code, output, _err = _run_main(monkeypatch, capsys, stamp=stamp)
    smoke = json.loads(output)["fleet_smoke"]
    assert code == 6 and schedules == []
    assert len(smoke["cells"]) == 8
    assert smoke["evidence"] == payload
    merged = next(row for row in smoke["cells"] if row["host"] == "satellite" and row["provider"] == "codex" and row["prompt_mode"] == "promptless")
    assert merged["outcome"] == "failed"
    assert set(merged["reasons"]) == {"host_unavailable", "event"}


def test_full_pass_alone_installs_schedule_and_stays_exit_zero(tmp_path, monkeypatch, capsys):
    payload = {"ok": True, "status": "PASS", "cells": _rows("primary") + _rows("satellite"), "failures": [], "untested": []}
    stamp, schedules = _apply(tmp_path, monkeypatch, payload)
    assert schedules == [True]
    assert {row["outcome"] for row in stamp["fleet_smoke"]["cells"]} == {"passed"}
    code, _output, _error = _run_main(monkeypatch, capsys, stamp=stamp)
    assert code == 0


@pytest.mark.parametrize("payload,rc", [({}, 0), ({"ok": True, "cells": []}, 0), ({"ok": True, "cells": _rows("primary")}, 0)])
def test_zero_exit_without_complete_evidence_is_untested(tmp_path, monkeypatch, capsys, payload, rc):
    stamp, schedules = _apply(tmp_path, monkeypatch, payload, rc=rc)
    assert schedules == []
    assert any(row["reason"] == "missing_evidence" for row in stamp["fleet_smoke"]["cells"])
    code, _out, _err = _run_main(monkeypatch, capsys, stamp=stamp)
    assert code == 9


@pytest.mark.parametrize("extra", [
    {"host": "outsider", "provider": "codex", "prompt_mode": "prompted", "outcome": "passed"},
    {"host": "primary", "outcome": "passed"},
    "invalid-row",
])
def test_unrecognized_evidence_is_preserved_and_never_green(tmp_path, monkeypatch, capsys, extra):
    payload = {"cells": _rows("primary") + _rows("satellite") + [extra]}
    stamp, schedules = _apply(tmp_path, monkeypatch, payload)
    code, _out, _err = _run_main(monkeypatch, capsys, stamp=stamp)
    assert code == 9 and schedules == []
    assert stamp["fleet_smoke"]["evidence"] == payload


def test_unknown_local_identity_never_partial(tmp_path, monkeypatch, capsys):
    stamp, schedules = _apply(tmp_path, monkeypatch, _partial_payload(), rc=2, machines=[
        {"name": "primary", "ssh_target": "primary.example.com"},
        {"name": "satellite", "ssh_target": "satellite.example.com"},
    ])
    code, _out, _err = _run_main(monkeypatch, capsys, stamp=stamp)
    assert code == 9 and schedules == []


@pytest.mark.parametrize("state,expected_code", [
    ("missing", 9), ("quota", 9), ("unreachable", 10), ("failed", 6),
])
def test_incomplete_remote_cell_never_hides_behind_other_passes(tmp_path, monkeypatch, capsys, state, expected_code):
    cells = _rows("primary") + _rows("satellite")
    cell = cells.pop()
    if state != "missing":
        cell.update(outcome="failed" if state == "failed" else "UNTESTED",
                    reason={"quota": "quota_exhausted", "unreachable": "host_unavailable", "failed": "event"}[state])
        cells.append(cell)
    stamp, schedules = _apply(tmp_path, monkeypatch, {"cells": cells})
    code, _out, _err = _run_main(monkeypatch, capsys, stamp=stamp)
    assert code == expected_code and schedules == []
    assert len(stamp["fleet_smoke"]["cells"]) == 8


@pytest.mark.parametrize("rc,status,expected_code", [(1, "PASS", 6), (2, "PASS", 9), (0, "FAIL", 6), (0, "UNTESTED", 9)])
def test_command_or_summary_non_success_cannot_become_green(tmp_path, monkeypatch, capsys, rc, status, expected_code):
    stamp, schedules = _apply(tmp_path, monkeypatch, {
        "status": status, "cells": _rows("primary") + _rows("satellite"),
    }, rc=rc)
    code, _out, _err = _run_main(monkeypatch, capsys, stamp=stamp)
    assert code == expected_code and schedules == []


def test_missing_machine_configuration_cannot_prove_partial_or_full_pass(tmp_path, monkeypatch, capsys):
    stamp, schedules = _apply(tmp_path, monkeypatch, _partial_payload(), rc=2, machines=[])
    code, _out, _err = _run_main(monkeypatch, capsys, stamp=stamp)
    assert code == 9 and schedules == []
    assert stamp["fleet_smoke"]["issues"]


def test_mixed_remote_quota_and_unreachable_remains_untested(tmp_path, monkeypatch, capsys):
    payload = _partial_payload()
    payload["untested"][0]["reason"] = "quota_exhausted"
    payload["untested"][0]["reset_at"] = "synthetic reset time"
    stamp, schedules = _apply(tmp_path, monkeypatch, payload, rc=2)
    code, _out, _err = _run_main(monkeypatch, capsys, stamp=stamp)
    assert code == 9 and schedules == []
    assert stamp["fleet_smoke"]["untested"][0]["reset_at"] == "synthetic reset time"


def test_legacy_quota_channel_merges_without_dropping_other_untested_rows(tmp_path, monkeypatch, capsys):
    payload = _partial_payload()
    payload["quota_exhausted"] = [{"host": "satellite", "provider": "codex", "prompt_mode": "promptless",
                                   "class": "quota_exhausted", "reset_at": "synthetic reset time"}]
    stamp, schedules = _apply(tmp_path, monkeypatch, payload, rc=2)
    code, _out, _err = _run_main(monkeypatch, capsys, stamp=stamp)
    assert code == 9 and schedules == []
    cell = next(row for row in stamp["fleet_smoke"]["cells"] if row["host"] == "satellite" and row["provider"] == "codex" and row["prompt_mode"] == "promptless")
    assert cell["outcome"] == "untested" and cell["reset_at"] == "synthetic reset time"
    assert cell["reasons"] == ["host_unavailable", "quota_exhausted"]
    assert stamp["fleet_smoke"]["evidence"] == payload


def test_object_machine_schema_and_all_local_markers_are_honored(tmp_path, monkeypatch, capsys):
    stamp, schedules = _apply(tmp_path, monkeypatch, _partial_payload(), rc=2, machines={"machines": [
        {"name": "primary", "ssh_target": None}, {"name": "satellite", "ssh_target": None},
    ]})
    code, _out, _err = _run_main(monkeypatch, capsys, stamp=stamp)
    assert code == 9 and schedules == []
    assert stamp["fleet_smoke"]["local_hosts"] == ["primary", "satellite"]


def test_relative_installed_machine_file_uses_smoke_cwd_for_local_identity(tmp_path, monkeypatch, capsys):
    caller = tmp_path / "caller"
    release = tmp_path / "release"
    caller.mkdir()
    release.mkdir()
    name = "synthetic-machines.json"
    (release / name).write_text(json.dumps([
        {"name": "primary", "ssh_target": None},
        {"name": "satellite", "ssh_target": "satellite.example.com"},
    ]))
    # This conflicting file must never define the local identity: the smoke's cwd
    # is the release checkout, even when deploy is invoked from somewhere else.
    (caller / name).write_text(json.dumps([
        {"name": "primary", "ssh_target": "primary.example.com"},
        {"name": "satellite", "ssh_target": None},
    ]))
    monkeypatch.chdir(caller)
    monkeypatch.setattr(deploy_mod, "_launchd_environment", lambda _: {"PENTACLE_MACHINES_FILE": name})
    monkeypatch.setattr(deploy_mod, "_scan_slow_consumer_window", lambda *_: {})
    monkeypatch.setattr(deploy_mod, "_write_stamp", lambda *_: None)
    schedules = []
    monkeypatch.setattr(deploy_mod, "_install_fleet_smoke_schedule", lambda *_: schedules.append(True))
    runner = _ScriptedRunner({"spawn_fleet_smoke.py": (2, json.dumps(_partial_payload()), "")})
    stamp = {"sha": TARGET_SHA}
    deploy_mod._apply_post_activation(
        V2_SERVICE, release, TARGET_SHA, stamp,
        prior_log_size=0, prior_pid=111, reload_launchd=False, runner=runner,
        verify_boot=_boot_observed,
        verify_runtime=lambda *_: (True, {"sha": TARGET_SHA, "pid": 999}),
    )
    assert stamp["fleet_smoke"]["local_hosts"] == ["primary"]
    assert stamp["fleet_smoke"]["outcome"] == "partial"
    smoke_calls = [call for call in runner.calls if any("spawn_fleet_smoke.py" in part for part in call)]
    assert len(smoke_calls) == 1
    assert f"PENTACLE_MACHINES_FILE={(release / name).resolve()}" in smoke_calls[0]
    assert len([call for call in runner.calls if "kickstart" in call]) == 1
    code, _out, _err = _run_main(monkeypatch, capsys, stamp=stamp)
    assert code == 10 and schedules == []


@pytest.mark.parametrize("fleet_state,expected_exit", [
    ("passed", 0), ("partial", 10), ("untested", 9), ("failed", 6),
])
@pytest.mark.parametrize("with_failure", [False, True])
def test_u2_real_scan_stamp_stdout_preserves_fleet_verdicts(
    tmp_path, monkeypatch, capsys, fleet_state, expected_exit, with_failure,
):
    # Do not use _apply: its legacy scan/write stubs would erase this boundary.
    from .test_deploy_script import _u2_line, _u2_peer_close, _u2_log_paths, _U2_CONN_B

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
