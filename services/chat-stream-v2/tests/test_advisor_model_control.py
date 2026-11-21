"""Authenticated template-only model CAS; existing advisor identity survives."""
import asyncio
import json

import pytest

from advisor_sessions import SHAPE_KEY, KINDS_KEY
from sessions import VerbError
from test_advisor_sessions import session_rig
from test_assistant_lane_rulings import ROOT, ADVISOR

pytestmark = pytest.mark.usefixtures("ruling_spec_root")


def actor(h):
    return dict(actor_stream_id=ROOT, actor_generation=h.root["session_generation"])


async def change(h, **overrides):
    return await h.r.configure_sessions("advice", **{**actor(h), "model": "astra",
        "expected_model": "gpt-6-astra", **overrides})


def test_canonical_model_update_preserves_fields_and_bound_identity(monkeypatch):
    async def run():
        async with session_rig(monkeypatch) as h:
            before = json.loads(await h.store.get(SHAPE_KEY))
            bound_before = await h.r.binding()
            authority_before = await h.store.get("assistant.authority.shape")
            result = await change(h)
            assert result["session_shape"] == {**before, "model": "gpt-6.2-astra"}
            assert await h.r.binding() == bound_before
            assert await h.store.get("assistant.authority.shape") == authority_before
            assert json.loads(await h.store.get(KINDS_KEY)) == ["advice"]
    asyncio.run(run())


@pytest.mark.parametrize("overrides,code", [
    ({"expected_model": "gpt-6.1-sol"}, "assistant_session_model_conflict"),
    ({"model": "missing-model"}, "spawn_model_unsupported"),
    ({"model": ""}, "assistant_session_model_invalid"),
    ({"expected_model": None}, "assistant_session_model_invalid"),
    ({"account": "other"}, "assistant_session_model_invalid"),
    ({"actor_stream_id": ADVISOR}, "assistant_authority_config_unauthorized"),
    ({"actor_generation": "stale"}, "assistant_authority_config_unauthorized"),
])
def test_invalid_or_unauthorized_cas_has_no_write_or_cleanup(monkeypatch, overrides, code):
    async def run():
        async with session_rig(monkeypatch) as h:
            before = await h.store.get(SHAPE_KEY)
            kinds = await h.store.get(KINDS_KEY)
            async def forbidden(*args):
                raise AssertionError("failed model CAS performed cleanup")
            monkeypatch.setattr(h.r.advisor_sessions, "close_request", forbidden)
            with pytest.raises(ValueError, match=code):
                await change(h, **overrides)
            assert await h.store.get(SHAPE_KEY) == before
            assert await h.store.get(KINDS_KEY) == kinds
    asyncio.run(run())


def test_model_change_requires_existing_template_and_same_kinds(monkeypatch):
    async def run():
        async with session_rig(monkeypatch) as h:
            with pytest.raises(ValueError, match="assistant_session_model_kinds_conflict"):
                await h.r.configure_sessions("none", **actor(h), model="astra", expected_model="gpt-6-astra")
            await h.store.put(SHAPE_KEY, "")
            with pytest.raises(ValueError, match="assistant_session_shape_required"):
                await change(h)
            assert await h.store.get(SHAPE_KEY) == ""
    asyncio.run(run())


@pytest.mark.parametrize("field,value,conflict", [("account", "concurrent-account", False), ("model", "gpt-6.2-sol", True)])
def test_final_transaction_recomputes_shape_and_rechecks_cas(monkeypatch, field, value, conflict):
    async def run():
        async with session_rig(monkeypatch) as h:
            original = h.store.submit
            injected = False
            async def interpose(fn):
                nonlocal injected
                result = await original(fn)
                if getattr(fn, "__name__", "") == "plan" and not injected:
                    injected = True
                    current = json.loads(await h.store.get(SHAPE_KEY))
                    current[field] = value
                    await h.store.put(SHAPE_KEY, json.dumps(current))
                return result
            monkeypatch.setattr(h.store, "submit", interpose)
            if conflict:
                with pytest.raises(ValueError, match="assistant_session_model_conflict"):
                    await change(h)
                assert json.loads(await h.store.get(SHAPE_KEY))["model"] == value
            else:
                result = await change(h)
                assert result["session_shape"]["model"] == "gpt-6.2-astra"
                assert result["session_shape"]["account"] == value
    asyncio.run(run())


def test_model_cas_over_authenticated_socket_rejects_stale_and_bad_fields(tmp_path):
    from front_desk_ingress_replay import harness, HOST, DESK
    from smoke.test_advisor_sessions_socket import client
    async def run():
        async with harness(tmp_path) as h:
            server, store = h["server"], h["store"]
            server.lane_rulings._ensure_worker = lambda: None
            shape = {"provider": "codex", "model": "gpt-6-astra", "effort": "high", "account": "fixture"}
            await store.put(SHAPE_KEY, json.dumps(shape))
            await store.put(KINDS_KEY, json.dumps(["spawn", "session_close"]))
            server.port, server.binds = 0, ["127.0.0.1"]
            port = await server.bind()
            try:
                async with client(f"ws://127.0.0.1:{port}", DESK, h["tokens"]["desk"]) as rpc:
                    base = {"type": "assistant.authority", "action": "session_kinds", "session_kinds": "spawn,session_close",
                        "model": "gpt-6.2-astra", "expected_model": "gpt-6-astra"}
                    for i, bad in enumerate(({"expected_model": None}, {"model": 42}, {"action": "read"}, {"account": "other"})):
                        result = await rpc({**base, **bad, "request_id": "bad-model-" + str(i)})
                        assert result["error_code"] == "assistant_session_model_invalid"
                        assert json.loads(await store.get(SHAPE_KEY)) == shape
                    result = await rpc({**base, "request_id": "model-cas-ok"})
                    assert result["session_shape"] == {**shape, "model": "gpt-6.2-astra"}
                    stale = await rpc({**base, "request_id": "model-cas-stale"})
                    assert stale["error_code"] == "assistant_session_model_conflict"
                    assert (await rpc({"type": "assistant.authority", "request_id": "read-model"}))["session_shape"]["model"] == "gpt-6.2-astra"
            finally:
                await server.close()
    asyncio.run(run())


def test_integrated_policy_rejects_retired_template_target(monkeypatch):
    async def run():
        async with session_rig(monkeypatch) as h:
            current = json.loads(await h.store.get(SHAPE_KEY))
            current["model"] = "gpt-6.2-astra"
            await h.store.put(SHAPE_KEY, json.dumps(current))
            with pytest.raises(ValueError, match="spawn_model_retired"):
                await change(h, model="gpt-6-astra", expected_model="gpt-6.2-astra")
            assert json.loads(await h.store.get(SHAPE_KEY)) == current
    asyncio.run(run())
