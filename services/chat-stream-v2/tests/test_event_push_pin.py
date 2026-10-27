"""Unit coverage for the explicit, manual event.push target-pin window step."""
from __future__ import annotations

import asyncio
import json

import pytest

from tools import event_push_pin
from pin_fixtures import pin_wall_clock, preflight, runtime
from store import (
    EVENT_PUSH_TARGET_SHA_KEY,
    EVENT_PUSH_TARGET_SHA_PREVIOUS_KEY,
    Store,
)


PREVIOUS = "a" * 40
TARGET = "b" * 40


def test_stage_records_previous_before_target_and_reads_back() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            await store.put(EVENT_PUSH_TARGET_SHA_KEY, PREVIOUS)
            writes: list[tuple[str, str]] = []
            put = store.put

            async def traced_put(key: str, value: str) -> None:
                writes.append((key, value))
                await put(key, value)

            store.put = traced_put  # type: ignore[method-assign]
            assert await store.stage_event_push_target_sha(TARGET) == PREVIOUS
            assert writes == [
                (EVENT_PUSH_TARGET_SHA_PREVIOUS_KEY, PREVIOUS),
                (EVENT_PUSH_TARGET_SHA_KEY, TARGET),
            ]
            assert await store.get(EVENT_PUSH_TARGET_SHA_PREVIOUS_KEY) == PREVIOUS
            assert await store.get(EVENT_PUSH_TARGET_SHA_KEY) == TARGET
        finally:
            store.stop()

    asyncio.run(go())


def test_stage_rejects_invalid_sha_but_ignores_retired_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            with pytest.raises(ValueError, match="exact 40-character"):
                await store.stage_event_push_target_sha("not-a-sha")
            monkeypatch.setenv("PENTACLE_EVENT_PUSH_TARGET_SHA", TARGET)
            assert await store.stage_event_push_target_sha(TARGET) is None
            assert await store.get(EVENT_PUSH_TARGET_SHA_KEY) == TARGET
        finally:
            store.stop()

    asyncio.run(go())


def test_rollback_restores_the_durable_captured_previous_pin() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            await store.put(EVENT_PUSH_TARGET_SHA_KEY, PREVIOUS)
            await store.stage_event_push_target_sha(TARGET)
            assert await store.rollback_event_push_target_sha() == PREVIOUS
            assert await store.get(EVENT_PUSH_TARGET_SHA_KEY) == PREVIOUS
        finally:
            store.stop()

    asyncio.run(go())


def test_rollback_refuses_without_a_captured_previous_pin() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            with pytest.raises(RuntimeError, match="no captured previous"):
                await store.rollback_event_push_target_sha()
        finally:
            store.stop()

    asyncio.run(go())


@pytest.mark.parametrize("rows, accepted", [
    ([{"host": "workstation", "class": "untested", "reason": "quota_exhausted", "reset_at": "2030-01-01T12:00:00Z"}], True),
    ([], True),
    ([{"host": "workstation", "class": "critical", "reason": "spawn_failed"}], False),
    ([{"host": "workstation", "class": "untested", "reason": "host_offline"}], False),
    ([{"host": "workstation", "class": "untested", "reason": "quota_exhausted"},
      {"host": "workstation", "class": "critical", "reason": "spawn_failed"}], False),
])
def test_stage_consumes_actual_smoke_exit_and_json_contract(tmp_path, monkeypatch, capsys, rows, accepted):
    """Use actual smoke.main output; only its provider-spawning matrix is isolated."""
    import contextlib
    import io
    import subprocess
    from tools import spawn_fleet_smoke

    machine_file = tmp_path / "machines.json"
    machine_file.write_text(json.dumps({"machines": [{"name": "workstation", "ssh_target": None}]}))
    monkeypatch.setenv("PENTACLE_MACHINES_FILE", str(machine_file))
    monkeypatch.delenv("PENTACLE_MACHINES_JSON", raising=False)
    monkeypatch.delenv("PENTACLE_SMOKE_HOSTS", raising=False)
    monkeypatch.setattr(event_push_pin, "SATELLITE_HOSTS", ("workstation",))
    # This slice measures peer/smoke outcomes; the CLI suite exercises real evidence.
    monkeypatch.setattr(event_push_pin, "_load_gate_evidence", lambda *_: {})
    rows = [{"provider": "codex", "prompt_mode": "promptless", **row} for row in rows]
    def complete_matrix(*args, evidence, **kwargs):
        reported = {(row["host"], row["provider"], row["prompt_mode"]) for row in rows}
        evidence.extend({"host": host, "provider": provider, "prompt_mode": mode, "outcome": "passed"}
                        for host, provider, mode in args[3] if (host, provider, mode) not in reported)
        return rows
    monkeypatch.setattr(spawn_fleet_smoke, "run_matrix", complete_matrix)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = spawn_fleet_smoke.main(['ws://fixture', 'workstation'])
    smoke = subprocess.CompletedProcess([], code, output.getvalue(), "")
    monkeypatch.setattr(event_push_pin.subprocess, "run", lambda *a, **k: smoke)

    async def go():
        db = str(tmp_path / "sessions.db")
        store = Store(db)
        store.start()
        try:
            await store.put("event_push.runtime.workstation", json.dumps(runtime(TARGET)))
        finally:
            store.stop()
        if not accepted:
            with pytest.raises(RuntimeError):
                await event_push_pin._run(db, stage=TARGET, rollback=False, gate_evidence=tmp_path / "gate.json",
                                          satellite_preflight=preflight(tmp_path, TARGET))
            return
        result = await event_push_pin._run(db, stage=TARGET, rollback=False, gate_evidence=tmp_path / "gate.json",
                                          satellite_preflight=preflight(tmp_path, TARGET))
        assert result["action"] == "stage" and result["readback"] == TARGET
        assert result["quota_exhausted"] == rows

    asyncio.run(go())
    if accepted and rows:
        assert "quota exhausted" in capsys.readouterr().err


def test_stage_fails_closed_with_last_stale_runtime_state(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(event_push_pin, "SATELLITE_HOSTS", ("workstation",))
    # This slice measures peer/smoke outcomes; the CLI suite exercises real evidence.
    monkeypatch.setattr(event_push_pin, "_load_gate_evidence", lambda *_: {})
    async def go() -> None:
        db = str(tmp_path / "sessions.db")
        store = Store(db)
        store.start()
        try:
            for host in event_push_pin.SATELLITE_HOSTS:
                await store.put(
                    f"event_push.runtime.{host}",
                    json.dumps({
                        "sha": PREVIOUS,
                        "pid": 4321,
                        "observed_at": "2026-08-31T00:00:00Z",
                        "observed_at_epoch": 1,
                    }),
                )
        finally:
            store.stop()

        monkeypatch.setattr(event_push_pin, "SATELLITE_READBACK_DEADLINE_SECONDS", 0.01)
        with pytest.raises(RuntimeError) as raised:
            await event_push_pin._run(db, stage=TARGET, rollback=False, gate_evidence=tmp_path / "gate.json",
                                          satellite_preflight=preflight(tmp_path, TARGET))
        message = str(raised.value)
        assert TARGET in message
        assert PREVIOUS in message
        assert '"process_state": "connected"' in message

    asyncio.run(go())


@pytest.mark.parametrize("returncode, output", [
    (0, ""), (2, "not-json"), (0, "[]"),
    (2, '{"ok":false,"status":"UNTESTED","failures":[],"untested":[]}'),
    (0, '{"ok":true,"status":"PASS","failures":[{"reason":"spawn_failed"}],"untested":[]}'),
    (2, '{"ok":false,"status":"UNTESTED","failures":[],"untested":[null]}'),
    (1, '{"ok":true,"status":"PASS","failures":[],"untested":[]}'),
])
def test_smoke_malformed_or_contradictory_results_fail_closed(returncode, output):
    import subprocess
    with pytest.raises(RuntimeError, match="post-deploy spawn smoke failed"):
        event_push_pin._smoke_quota_note(subprocess.CompletedProcess([], returncode, output, ""))


def test_restage_preserves_original_rollback_pin():
    async def run():
        store = Store(':memory:')
        store.start()
        try:
            await store.put(EVENT_PUSH_TARGET_SHA_KEY, PREVIOUS)
            assert await store.stage_event_push_target_sha(TARGET) == PREVIOUS
            assert await store.stage_event_push_target_sha(TARGET) == PREVIOUS
            assert await store.rollback_event_push_target_sha() == PREVIOUS
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('missing', ['incarnation', 'admitted', 'useful_events', 'recovery_pending', 'recovery_gaps'])
def test_boot_telemetry_cannot_substitute_for_useful_drained_delivery(tmp_path, monkeypatch, missing):
    async def run():
        store = Store(':memory:')
        store.start()
        try:
            row = runtime(TARGET)
            row[missing] = {'incarnation': 'prior-boot', 'admitted': False, 'useful_events': 0,
                            'recovery_pending': 1, 'recovery_gaps': [{'reason': 'missing-source'}]}[missing]
            await store.put('event_push.runtime.workstation', json.dumps(row))
            participants = json.loads(preflight(tmp_path, TARGET).read_text())['participants']
            with pytest.raises(RuntimeError, match='activation deadline'):
                await event_push_pin._observe(store, participants, TARGET, 0,
                    asyncio.get_running_loop().time() + 0.001, useful=True)
        finally:
            store.stop()
    asyncio.run(run())


def test_optional_offline_stays_partial_and_canary_is_only_affected_codex(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(event_push_pin, 'SATELLITE_HOSTS', ('workstation', 'optional'))
    monkeypatch.setattr(event_push_pin, 'OPTIONAL_SATELLITE_HOSTS', ('optional',))
    monkeypatch.setattr(event_push_pin, '_load_gate_evidence', lambda *_: {})
    commands = []
    def smoke(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps({
            'ok': True, 'status': 'PASS', 'failures': [], 'untested': []}), '')
    monkeypatch.setattr(event_push_pin.subprocess, 'run', smoke)
    async def run():
        db = str(tmp_path / 'sessions.db')
        store = Store(db)
        store.start()
        try:
            await store.put('event_push.target_sha', PREVIOUS)
            await store.put('event_push.runtime.workstation', json.dumps(runtime(TARGET)))
        finally:
            store.stop()
        receipt = preflight(tmp_path, TARGET, hosts=('workstation', 'optional'), optional=('optional',))
        result = await event_push_pin._run(db, stage=TARGET, rollback=False,
            gate_evidence=tmp_path / 'gate.json', satellite_preflight=receipt, url='ws://owned-fixture')
        assert result['status'] == 'PARTIAL' and result['pending'] == ['optional']
        assert len(commands) == 1 and commands[0][2:] == ['ws://owned-fixture', 'workstation']
    asyncio.run(run())


@pytest.mark.parametrize('bad', ['missing', 'candidate', 'stale', 'dependencies', 'interpreter', 'compatibility', 'omitted_host', 'running_source', 'config_identity', 'cli_identity'])
def test_preflight_refuses_before_stage(tmp_path, monkeypatch, bad):
    monkeypatch.setattr(event_push_pin, 'SATELLITE_HOSTS', ('workstation',))
    monkeypatch.setattr(event_push_pin, '_load_gate_evidence', lambda *_: {})
    receipt = preflight(tmp_path, TARGET)
    payload = json.loads(receipt.read_text())
    if bad == 'candidate': payload['candidate'] = PREVIOUS
    if bad == 'stale': payload['observed_at_epoch'] = 1
    if bad == 'dependencies': payload['participants'][0]['imports_passed'] = False
    if bad == 'interpreter': payload['participants'][0]['interpreter'] = '/global/python'
    if bad == 'compatibility': payload['participants'][0]['compatibility_passed'] = False
    if bad == 'omitted_host': payload['participants'] = []
    if bad == 'running_source': payload['participants'][0]['installed_source_sha'] = 'checkout-head-only'
    if bad == 'config_identity': payload['participants'][0].pop('configuration_sha256')
    if bad == 'cli_identity': payload['participants'][0].pop('installed_cli_sha256')
    receipt.write_text(json.dumps(payload))
    async def run():
        db = str(tmp_path / 'sessions.db')
        with pytest.raises(ValueError):
            await event_push_pin._run(db, stage=TARGET, rollback=False, gate_evidence=tmp_path / 'gate.json',
                                     satellite_preflight=None if bad == 'missing' else receipt)
        store = Store(db)
        store.start()
        try:
            assert await store.get(EVENT_PUSH_TARGET_SHA_KEY) is None
            assert await store.get(EVENT_PUSH_TARGET_SHA_PREVIOUS_KEY) is None
        finally:
            store.stop()
    asyncio.run(run())
