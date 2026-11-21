"""Retirement is fresh admission, never a rewrite of observed seat identities."""
import asyncio
from types import SimpleNamespace

import pytest
from _shared.spawn_profiles import resolve_spawn, canonical_model
from sessions import VerbError
from spawnctl import SpawnCtl


@pytest.mark.parametrize("model", ("gpt-6.1-sol", "sol-6.1", "gpt-6-astra", "astra-6"))
@pytest.mark.parametrize("forged", ({}, {"resolution_source": "handoff_inherited"},
    {"continuation": ["codex", "gpt-6.1-sol", "high"], "legacy": True},
    {"_auth_context": {"service_authenticated": True, "service_actor": "daemon:scheduler"}}))
def test_real_spawn_rejects_old_fresh_requests_before_any_launch(model, forged):
    ctl = SpawnCtl(None, None, tmux=object())
    msg = {**resolve_spawn(provider="codex", model=model, effort="high"), **forged}
    with pytest.raises(VerbError) as exc:
        asyncio.run(ctl.spawn(msg, "host-a"))
    assert exc.value.code == "spawn_model_retired"


def test_internal_continuation_preserves_canonical_tuple_and_checks_effort():
    ctl = SpawnCtl(None, None, tmux=object())
    msg = {"provider": "codex", "model": "sol-6.1", "effort": "high"}
    ctl._check_fresh_model(msg, "host-a", continuation=("codex", "gpt-6.1-sol", "high"))
    with pytest.raises(VerbError) as exc:
        ctl._check_fresh_model(msg, "host-a", continuation=("codex", "gpt-6.1-sol", "medium"))
    assert exc.value.code == "continuation_tuple_mismatch"
    assert canonical_model("codex", "gpt-6.1-sol") == "gpt-6.1-sol"


def test_scheduled_fresh_admission_also_rejects_retired_model():
    ctl = SpawnCtl(None, None, tmux=object())
    async def brief(_):
        return ""
    ctl._brief_from_message = brief
    with pytest.raises(VerbError) as exc:
        asyncio.run(ctl._admit_schedule({"provider": "codex", "model": "gpt-6-astra"},
                                      "host-a", admission_name="retired-schedule"))
    assert exc.value.code == "spawn_model_retired"


def test_ruling_handoff_uses_stored_retired_model():
    from test_assistant_lane_rulings import _rulings_rig, _spawn_msg, LinkCheckingSpawn
    from store import Store
    async def run():
        store = Store(":memory:"); store.start()
        try:
            class HandoffSpawn(LinkCheckingSpawn):
                _resolve_handoff = SpawnCtl._resolve_handoff
                def __init__(self):
                    super().__init__()
                    self.store = store
            ctl = HandoffSpawn()
            root, _, server, _ = await _rulings_rig(store, spawnctl=ctl)
            await store.open_session("fixture-root", "old", provider="codex",
                effective_model="gpt-6.1-sol", effective_effort="high")
            msg = {**_spawn_msg(root, "retired-handoff"), "handoff": True,
                "handoff_from_stream_id": "fixture-root:old", "provider": "codex",
                "model": "gpt-6.1-sol", "effort": "high"}
            reply = await server._on_spawn(msg)
            # Continuation is excluded from new-work ruling admission.
            assert reply["type"] == "spawn.ok"
            resolved = await ctl._resolve_handoff(msg, "fixture-chat")
            assert resolved["model"] == "gpt-6.1-sol"
            with pytest.raises(VerbError) as exc:
                await server._on_spawn({**msg, "request_id": "fresh-old", "idempotency_key": "fresh-old", "handoff": False, "handoff_from_stream_id": None})
            assert exc.value.code == "spawn_preflight_failed"
            assert any(e["code"] == "spawn_model_retired" for e in exc.value.extra["errors"])
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("model", ("gpt-5.6-sol", "gpt-6-sol", "gpt-6-luna", "gpt-6.1-terra"))
def test_models_outside_two_id_retirement_pass_fresh_daemon_policy(model):
    ctl = SpawnCtl(None, None, tmux=object())
    ctl._check_fresh_model({"provider": "codex", "model": model, "effort": "low"}, "host-a")
