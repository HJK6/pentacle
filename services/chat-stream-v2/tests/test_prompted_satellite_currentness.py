"""Reach remote qualification admission during an authenticated satellite heartbeat."""
import asyncio
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from _shared.spawn_profiles import CATALOG_VERSION
import launch
import qualification_admission
from account_runtime import AccountRuntime
from deploy.first_qualification import change_admission
from provider_cli_fixtures import seed_pin
from satellite import Satellite, SatelliteConfig
from sessions import Sessions, VerbError
from spawnctl import SpawnCtl
from store import Store
from test_account_rotation import binding
from test_event_push import _sink
from test_provider_cli_pin import artifact_executor
from test_spawn_cancel_fence_holes import ReconTmux
from test_satellite_recovery import _Wire


def exercise_prompted_qualification(tmp_path, monkeypatch, window):
    async def exercise():
        host = "satellite"
        db = str(tmp_path / "ledger.db")
        store = Store(db)
        store.start()
        ctl = None
        second = None
        resume = asyncio.Event()
        try:
            tmux = ReconTmux()
            tmux.stage_text = AsyncMock()
            tmux.capture = AsyncMock(return_value="❯\n⏵⏵ bypass permissions")
            sessions = Sessions(store, local_host="coordinator", tmux=tmux)
            runtime = AccountRuntime(store, sessions, policy={"enabled": True})
            account = binding("ours", host=host, provider="claude")
            await runtime.run(lambda ledger: ledger.register(account))
            await seed_pin(runtime)
            await artifact_executor(runtime)
            hosts = SimpleNamespace(ensure_reachable=AsyncMock(), tmux_for=lambda _: tmux, peers={})
            ctl = SpawnCtl(store, sessions, tmux=tmux, hosts=hosts,
                           machine=launch.local_machine(host, cwd=str(tmp_path)))
            ctl.account_runtime = runtime
            key = "34567890abcdef12" * 2
            request = qualification_admission.smoke_request(host, key, "claude", "prompted", "claude-haiku-5-5", "low")
            message = dict(request, request_id="owned-request", catalog_version=CATALOG_VERSION,
                           _auth_context={"operator_authenticated": True})
            receipt = {"accounts": {"database": db}, "receipt_sha256": "a" * 64,
                       "qualification_admission_before": [None, None], "intent": {"first_qualification": {
                           "expires_at": "2099-01-01T00:00:00Z", "prior_exclusion": {"host": host}}}}
            change_admission(receipt, "fenced")
            change_admission(receipt, "testing", pid=os.getpid(), cells={key: {"request": request, "binding": account.public()}})

            sink, _, _ = _sink(store, secret="fixture")
            sat = Satellite(SatelliteConfig(host=host, checkout=str(tmp_path), no_autoupdate=True,
                                           push_secret="fixture", host_secret="owned-fixture",
                                           held_span_path=str(tmp_path / "held.json"),
                                           chat_checkpoint_path=str(tmp_path / "chat.json")))
            sat.sha = "a" * 40
            sink.host_secrets[host] = sat.config.host_secret
            await store.put("event_push.target_sha", sat.sha)

            assert (await sat._push(_Wire(sink), [], {}))["type"] == "event.push.ok"
            entered = asyncio.Event()
            append = store.append_session_events_lifecycle_cas

            async def gated_append(*args, **kwargs):
                entered.set()
                await resume.wait()
                return await append(*args, **kwargs)

            monkeypatch.setattr(store, "append_session_events_lifecycle_cas", gated_append)
            if window == "completed":
                resume.set()
            if window == "expired":
                runtime.clock = lambda: time.time() + 121
            if window == "restarted":
                sat._incarnation = "replacement-process"
            second = asyncio.create_task(sat._push(_Wire(sink), [], {}))
            await asyncio.wait_for(entered.wait(), timeout=5)
            # The provider and terminal are doubles. The real remote spawn must
            # claim the sealed request and commit the verified binding.
            if window == "completed":
                assert (await second)["type"] == "event.push.ok"

            stream = f"{host}:{request['session_name']}"
            pastes = []

            async def paste(name, brief):
                assert name == request["session_name"]
                pastes.append(brief)
                event = {"stream_id": stream, "provider": "claude", "kind": "USER", "text": brief,
                         "timestamp": "2026-11-21T16:18:09Z", "raw": {"jsonl_record_uuid": "owned-input"}}
                assert (await sat._push(_Wire(sink), [event], {}))["type"] == "event.push.ok"

            tmux.paste = paste
            if window in ("expired", "restarted"):
                with pytest.raises(VerbError) as error:
                    await ctl.spawn(message, sessions.local_host)
                assert (error.value.code, error.value.extra.get("reason")) == (
                    "provider_cli_verification_unavailable", "satellite_not_current")
                assert tmux.created == 0 and not pastes
                assert await store.fetch_session(host, request["session_name"]) is None
                assert not await store.submit(lambda conn: conn.execute(
                    "SELECT 1 FROM v2_account_session_bindings WHERE stream_id=?", (stream,)
                ).fetchone())
                return
            result = await ctl.spawn(message, sessions.local_host)
            assert result["type"] == "spawn.ok"
            assert tmux.created == 1
            resume.set()
            await asyncio.wait_for(asyncio.gather(*tuple(ctl._background_spawns)), timeout=12)
            assert pastes == [request["initial_prompt"]]
            outcome = await store.get_spawn_outcome(host, request["session_name"])
            assert outcome["state"] == "delivered"
            row = await store.fetch_session(host, request["session_name"])
            assert row["account_binding"]["generation"] == account.generation
            assert (row["requested_model"], row["requested_effort"]) == (request["model"], "low")
        finally:
            resume.set()
            if second is not None:
                assert (await second)["type"] == "event.push.ok"
            if ctl is not None:
                for task in tuple(ctl._background_spawns):
                    task.cancel()
                await asyncio.gather(*tuple(ctl._background_spawns), return_exceptions=True)
            store.stop()

    asyncio.run(exercise())


def test_prompted_qualification_after_completed_heartbeat(tmp_path, monkeypatch):
    exercise_prompted_qualification(tmp_path, monkeypatch, "completed")


def test_prompted_qualification_during_same_satellite_heartbeat(tmp_path, monkeypatch):
    exercise_prompted_qualification(tmp_path, monkeypatch, "refreshing")


def test_expired_heartbeat_refuses_prompted_qualification(tmp_path, monkeypatch):
    exercise_prompted_qualification(tmp_path, monkeypatch, "expired")


def test_replaced_satellite_refuses_prompted_qualification(tmp_path, monkeypatch):
    exercise_prompted_qualification(tmp_path, monkeypatch, "restarted")


@pytest.mark.parametrize("change", ["pid", "incarnation", "missing_pid", "missing_incarnation",
                                    "wrong_sha", "pin_changed", "pin_restored", "prior_false"])
def test_superseded_or_unproven_heartbeat_remains_unadmitted(change):
    async def exercise():
        store = Store()
        store.start()
        host, target = "satellite", "a" * 40
        key = "event_push.runtime." + host

        async def begin(sha=target, pid=12, incarnation="one", version_ok=True):
            return await store.begin_satellite_push(host, sha, pid, incarnation, version_ok=version_ok)

        async def finish(generation):
            await store.finish_satellite_push(host, generation, useful=1, recovery_pending=0, recovery_gaps=[])

        try:
            await store.stage_event_push_target_sha(target)
            first = await begin()
            if change != "prior_false":
                await finish(first)
            delayed = await begin()
            if change in ("pin_changed", "pin_restored"):
                await store.stage_event_push_target_sha("b" * 40)
                if change == "pin_restored":
                    await store.rollback_event_push_target_sha()
            elif change == "prior_false":
                assert json.loads(await store.get(key))["admitted"] is False
                return
            else:
                changed_fields = {"pid": {"pid": 13}, "incarnation": {"incarnation": "two"},
                                  "missing_pid": {"pid": None}, "missing_incarnation": {"incarnation": ""},
                                  "wrong_sha": {"sha": "b" * 40, "version_ok": False}}
                await begin(**changed_fields[change])
            await finish(delayed)
            assert json.loads(await store.get(key))["admitted"] is False
        finally:
            store.stop()
    asyncio.run(exercise())


def test_failed_ingest_does_not_renew_completed_proof(monkeypatch):
    from provider_accounts import AccountError
    from provider_cli_pin import consumer_current
    from test_event_push import _push

    async def exercise():
        store = Store()
        store.start()
        host, sha = "satellite", "a" * 40
        try:
            sink, _, _ = _sink(store)
            await store.stage_event_push_target_sha(sha)
            frame = dict(_push(sink, [], host=host, sha=sha), satellite_incarnation="one")
            assert (await sink.handle_push(frame))["type"] == "event.push.ok"
            key = "event_push.runtime." + host
            before = json.loads(await store.get(key))
            monkeypatch.setattr(store, "append_session_events_lifecycle_cas", AsyncMock(return_value=None))
            for _ in range(2):
                assert (await sink.handle_push(frame))["error"] == "lifecycle_changed"
            after = json.loads(await store.get(key))
            stamp = before["observed_at_epoch"]
            assert after["observed_at_epoch"] == stamp and after["last_seen_at_epoch"] >= stamp
            await store.submit(lambda conn: consumer_current(SimpleNamespace(conn=conn), host, "local", stamp + 120))
            with pytest.raises(AccountError) as error:
                await store.submit(lambda conn: consumer_current(SimpleNamespace(conn=conn), host, "local", stamp + 120.01))
            assert error.value.context["reason"] == "satellite_not_current"
        finally:
            store.stop()
    asyncio.run(exercise())


@pytest.mark.parametrize("legacy", [False, True])
def test_daemon_restart_requires_a_new_completed_push(tmp_path, legacy):
    async def exercise():
        host, sha = "satellite", "a" * 40
        key = "event_push.runtime." + host
        path = str(tmp_path / "restart.db")
        store = Store(path, daemon_start=True)
        store.start()
        try:
            await store.stage_event_push_target_sha(sha)
            old = await store.begin_satellite_push(host, sha, 12, "one", version_ok=True)
            await store.finish_satellite_push(host, old, useful=1, recovery_pending=0, recovery_gaps=[])
            if legacy:
                row = json.loads(await store.get(key))
                del row["observation_generation"]
                await store.put(key, json.dumps(row))
        finally:
            store.stop()

        # A diagnostic connection has no startup authority.
        reader = Store(path)
        reader.start()
        try:
            assert json.loads(await reader.get(key))["admitted"] is True
        finally:
            reader.stop()
        store = Store(path, daemon_start=True)
        store.start()
        try:
            assert json.loads(await store.get(key))["admitted"] is False
            new = await store.begin_satellite_push(host, sha, 12, "one", version_ok=True)
            await store.finish_satellite_push(host, old, useful=0, recovery_pending=0, recovery_gaps=[])
            assert json.loads(await store.get(key))["admitted"] is False
            await store.finish_satellite_push(host, new, useful=0, recovery_pending=0, recovery_gaps=[])
            assert json.loads(await store.get(key))["admitted"] is True
        finally:
            store.stop()
    asyncio.run(exercise())

@pytest.mark.parametrize("pin,incarnation", [("", "one"), ("a" * 40, "")])
def test_delivery_metrics_remain_available_without_cli_currentness(pin, incarnation):
    async def exercise():
        store = Store()
        store.start()
        try:
            if pin:
                await store.stage_event_push_target_sha(pin)
            generation = await store.begin_satellite_push("peer", "a" * 40, 123, incarnation, version_ok=True)
            await store.finish_satellite_push("peer", generation, useful=2, recovery_pending=1, recovery_gaps=[])
            runtime = json.loads(await store.get("event_push.runtime.peer"))
            assert runtime["useful_events"] == 2 and runtime["recovery_pending"] == 1
            assert runtime["admitted"] is False
        finally:
            store.stop()
    asyncio.run(exercise())
