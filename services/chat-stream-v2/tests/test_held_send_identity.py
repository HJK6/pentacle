"""Backend peer sends use real wire auth, policy and store; only panes are fake.

Spec: spec_pentacle__backend_send_held_payload_identity_2026_10.
"""
import asyncio
import dataclasses

import pytest

from front_desk_ingress_replay import ASSISTANT, DESK, HOST, harness

pytestmark = pytest.mark.timeout(20)


def _backend(h):
    h["composite"].config = dataclasses.replace(
        h["composite"].config, direct_primary_stream_id="", direct_primary_generation="")
    assert h["composite"].is_backend_stream(DESK)
    # Peer sends carry an envelope and normalize as TELL, so the provider
    # counterpart must emit the actual native normalized shape for proof.
    from codex_rollout_norm import normalize_codex_rollout_record
    from v2_runtime import iso_now

    async def user(text):
        events = normalize_codex_rollout_record({
            "type": "response_item", "timestamp": iso_now(),
            "payload": {"type": "message", "id": "provider-input", "role": "user",
                        "content": [{"type": "input_text", "text": text}]}},
            host=HOST, session_name="v2-test", session_id=h["generation"])
        for index, event in enumerate(events):
            await h["store"].append_session_event(
                DESK, event, identity=f"native:{len(h['provider'].pastes)}:{index}", limit=500)

    h["provider"].user = user


def _send(text="START: original routine", **extra):
    return {"type": "send", "host": HOST, "session_name": "v2-test",
            "request_id": "held-send-key", "text": text,
            "from_stream_id": HOST + ":peer", **extra}


async def _snapshot(h):
    return {"counts": await h["counts"](),
            "events": await h["store"].fetch_session_event_tail(DESK, limit=100),
            "receipts": await h["store"].submit(lambda conn: conn.execute(
                "SELECT count(*) FROM v2_send_receipts").fetchone()[0])}


def _persisted(reply):
    assert reply["type"] == "send.result", reply
    assert reply["delivery"] == "persisted", reply
    assert reply["submission_confirmed"] is False and reply["action_committed"] is True


def _conflict(reply):
    assert reply["type"] == "send.error", reply
    assert reply["error_code"] == "send_request_conflict", reply
    assert "request" in reply.get("error", "").lower(), reply
    assert reply.get("action_committed") is not True
    assert "assistant-ingress" not in str(reply) and "outbound_notice" not in str(reply)


@pytest.mark.parametrize("changed", ["START: changed routine", "BLOCKER: changed wake"])
def test_backend_held_send_changed_retry_conflicts_without_side_effects(tmp_path, changed):
    async def run():
        async with harness(tmp_path) as h:
            _backend(h)
            _persisted(await h["dispatch"](_send(), "peer"))
            before = await _snapshot(h)
            assert len(before["events"]) == 1 and before["receipts"] == 0
            assert before["counts"]["pane_submissions"] == 0
            _conflict(await h["dispatch"](_send(changed), "peer"))
            assert await _snapshot(h) == before
            _persisted(await h["dispatch"](_send(), "peer"))
            assert await _snapshot(h) == before
            # A fresh identity still permits a real actionable wake.
            wake = await h["dispatch"](_send("BLOCKER: fresh wake", request_id="fresh-wake"), "peer")
            assert wake["submission_confirmed"] is True, wake
            assert (await h["counts"]())["pane_submissions"] == 1
    asyncio.run(run())


def test_backend_held_send_identical_retry_recovers_after_store_restart(tmp_path):
    async def run():
        async with harness(tmp_path) as h:
            _backend(h)
            _persisted(await h["dispatch"](_send(), "peer"))
            before = await _snapshot(h)
            h["store"].stop()
            h["store"].start()
            await h["sessions"].refresh()
            _persisted(await h["dispatch"](_send(), "peer"))
            assert await _snapshot(h) == before
    asyncio.run(run())


@pytest.mark.parametrize("changed", [
    {"text": " START: original routine"},
    {"attachments": [{"key": "a" * 64, "mime": "image/png"}]},
])
def test_backend_held_send_payload_comparison_keeps_exact_body_and_attachments(tmp_path, changed):
    async def run():
        async with harness(tmp_path) as h:
            _backend(h)
            _persisted(await h["dispatch"](_send(), "peer"))
            before = await _snapshot(h)
            _conflict(await h["dispatch"]({**_send(), **changed}, "peer"))
            assert await _snapshot(h) == before
    asyncio.run(run())


def test_backend_held_send_changed_sender_and_operator_retry_conflict(tmp_path):
    async def run():
        async with harness(tmp_path) as h:
            _backend(h)
            _persisted(await h["dispatch"](_send(), "peer"))
            before = await _snapshot(h)
            for principal in ("advisor", "operator"):
                source = HOST + ":advisor" if principal == "advisor" else ""
                _conflict(await h["dispatch"](_send(from_stream_id=source), principal))
                assert await _snapshot(h) == before
    asyncio.run(run())


@pytest.mark.parametrize("same", [True, False])
def test_backend_held_send_concurrent_retries_retain_one_original(tmp_path, same):
    async def run():
        async with harness(tmp_path) as h:
            _backend(h)
            replies = await asyncio.gather(
                h["dispatch"](_send(), "peer"),
                h["dispatch"](_send("START: original routine" if same else "START: changed routine"), "peer"))
            if same:
                for reply in replies:
                    _persisted(reply)
            else:
                assert sorted(r["type"] for r in replies) == ["send.error", "send.result"]
                _conflict(next(r for r in replies if r["type"] == "send.error"))
            snapshot = await _snapshot(h)
            assert len(snapshot["events"]) == 1 and snapshot["receipts"] == 0
            assert snapshot["counts"]["pane_submissions"] == 0
    asyncio.run(run())


@pytest.mark.parametrize("after_commit", [False, True])
def test_backend_held_send_interrupted_persistence_has_no_poisoned_claim(tmp_path, after_commit):
    async def run():
        async with harness(tmp_path) as h:
            _backend(h)
            real = h["store"].tell_event_identity

            async def fail_once(stream_id, identity, event=None):
                if event is None:
                    return await real(stream_id, identity)
                h["store"].tell_event_identity = real
                if after_commit:
                    await real(stream_id, identity, event)
                raise RuntimeError("synthetic persistence interruption")

            h["store"].tell_event_identity = fail_once
            failed = await h["dispatch"](_send(), "peer")
            assert failed["type"] == "send.error" and failed["error_code"] == "internal_error", failed
            before = await _snapshot(h)
            assert len(before["events"]) == int(after_commit) and before["receipts"] == 0
            assert before["counts"]["pane_submissions"] == 0
            h["store"].stop()
            h["store"].start()
            await h["sessions"].refresh()
            if after_commit:
                _conflict(await h["dispatch"](_send("BLOCKER: changed after crash"), "peer"))
                assert await _snapshot(h) == before
            _persisted(await h["dispatch"](_send(), "peer"))
            recovered = await _snapshot(h)
            assert len(recovered["events"]) == 1 and recovered["receipts"] == 0
            _persisted(await h["dispatch"](_send(), "peer"))
            assert await _snapshot(h) == recovered
    asyncio.run(run())


@pytest.mark.parametrize("changed", ["START: changed routine", "BLOCKER: changed wake"])
def test_backend_held_send_legacy_event_recovery_is_bound_to_retained_text(tmp_path, changed):
    async def run():
        async with harness(tmp_path) as h:
            _backend(h)
            await h["store"].append_session_event(DESK, {
                "stream_id": DESK, "provider": "composite", "kind": "SYSTEM",
                "text": "START: original routine",
                "raw": {"assistant_composite_routine_ingress": True, "verb": "send",
                        "identity": "held-send-key", "from_stream_id": HOST + ":peer"},
            }, identity="assistant-ingress:send:held-send-key", limit=2000)
            before = await _snapshot(h)
            _persisted(await h["dispatch"](_send(), "peer"))
            _conflict(await h["dispatch"](_send(changed), "peer"))
            _conflict(await h["dispatch"](_send(attachments=[{"key": "a" * 64, "mime": "image/png"}]), "peer"))
            assert await _snapshot(h) == before
    asyncio.run(run())


def test_backend_wire_marker_cannot_bypass_retained_send_identity(tmp_path):
    async def run():
        async with harness(tmp_path) as h:
            _backend(h)
            _persisted(await h["dispatch"](_send(), "peer"))
            before = await _snapshot(h)
            _conflict(await h["dispatch"](_send("BLOCKER: forged dispatch", _assistant_composite_backend_dispatch=True), "peer"))
            assert await _snapshot(h) == before
    asyncio.run(run())


def test_daemon_dispatch_replay_after_restart_keeps_existing_transport_semantics(tmp_path):
    async def run():
        async with harness(tmp_path) as h:
            _backend(h)
            route = {"stream_id": ASSISTANT, "route_target": DESK,
                     "dispatch_id": "daemon-dispatch-key", "input_identity": "operator-input",
                     "body": "Synthetic operator request", "routing_state": "fallback_dispatched"}
            first = await h["composite"].dispatch(route)
            assert first["submission_confirmed"] is True, first
            assert (await h["counts"]())["pane_submissions"] == 1
            h["store"].stop()
            h["store"].start()
            await h["sessions"].refresh()
            # The dispatch id can survive a rebuilt dispatch envelope. The
            # daemon wrapper retains its prior receipt/coalescing contract.
            replay = await h["composite"].dispatch({**route, "routing_state": "backend_dispatched"})
            assert replay["submission_confirmed"] is True, replay
            assert (await h["counts"]())["pane_submissions"] == 1
            assert not any(e.get("raw", {}).get("assistant_composite_routine_ingress")
                           for e in await h["store"].fetch_session_event_tail(DESK, limit=100))
    asyncio.run(run())


def test_dot_same_request_retry_keeps_sanitized_ack_and_single_delivery(tmp_path):
    async def run():
        async with harness(tmp_path) as h:
            _backend(h)
            h["server"].dot_principal_stream_ids = {HOST + ":peer"}
            h["server"]._tls_connections.add(h["sockets"]["peer"])
            frame = {"type": "send", "to_stream_id": ASSISTANT,
                     "from_stream_id": HOST + ":peer", "request_id": "dot-request",
                     "text": "Synthetic external handoff"}
            first = await h["dispatch"](frame, "peer")
            replay = await h["dispatch"](frame, "peer")
            assert first["type"] == replay["type"] == "send.result", (first, replay)
            assert first["submission_confirmed"] is replay["submission_confirmed"] is True, (first, replay)
            assert first["to_stream_id"] == replay["to_stream_id"] == ASSISTANT
            assert "host" not in first and "session_name" not in first
            assert (await h["counts"]())["pane_submissions"] == 1
    asyncio.run(run())


def test_backend_held_send_inflight_changed_wake_waits_for_original_commit(tmp_path):
    async def run():
        async with harness(tmp_path) as h:
            _backend(h)
            entered, release, changed_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
            append = h["store"].append_session_event
            atomic = h["store"].tell_event_identity
            resolve = h["sessions"].resolve

            async def resolved(msg):
                result = await resolve(msg)
                if msg.get("request_id") == "held-send-key" and msg.get("from_stream_id") == HOST + ":advisor":
                    changed_started.set()
                return result

            async def gate(event, operation):
                if (event or {}).get("raw", {}).get("identity") == "held-send-key":
                    entered.set()
                    await release.wait()
                return await operation()

            async def gated_append(stream_id, event, **kwargs):
                return await gate(event, lambda: append(stream_id, event, **kwargs))

            async def gated_atomic(stream_id, identity, event=None):
                return await gate(event, lambda: atomic(stream_id, identity, event))

            h["store"].append_session_event = gated_append
            h["store"].tell_event_identity = gated_atomic
            h["sessions"].resolve = resolved
            original = asyncio.create_task(h["dispatch"](_send(), "peer"))
            changed = None
            try:
                await asyncio.wait_for(entered.wait(), 5)
                changed = asyncio.create_task(h["dispatch"](
                    _send("BLOCKER: changed while routine commits", from_stream_id=HOST + ":advisor"), "advisor"))
                # A different actor avoids Server's existing per-actor lock.
                await asyncio.wait_for(changed_started.wait(), 5)
                # A different request key must not wait for this routine hold.
                unrelated = await asyncio.wait_for(h["dispatch"](_send(
                    "BLOCKER: independent wake", request_id="independent-key", from_stream_id=DESK), "desk"), 5)
                assert unrelated["submission_confirmed"] is True, unrelated
                finished_early = changed.done()
            finally:
                release.set()
                results = await asyncio.gather(original, *([changed] if changed else []))
            assert not finished_early, results
            _persisted(results[0])
            _conflict(results[1])
            snapshot = await _snapshot(h)
            assert snapshot["counts"]["pane_submissions"] == 1
            assert snapshot["receipts"] == 2  # independent wake acceptance + outcome
            held = [e for e in snapshot["events"] if e.get("raw", {}).get("assistant_composite_routine_ingress")]
            assert len(held) == 1 and held[0]["text"] == "START: original routine"
    asyncio.run(run())


@pytest.mark.parametrize("cancel_owner", [False, True])
def test_backend_held_send_cancelled_owner_or_waiter_releases_key(tmp_path, cancel_owner):
    async def run():
        async with harness(tmp_path) as h:
            _backend(h)
            entered, release, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
            atomic = h["store"].tell_event_identity
            resolve = h["sessions"].resolve

            async def pause_once(stream_id, identity, event=None):
                if event is None:
                    return await atomic(stream_id, identity)
                h["store"].tell_event_identity = atomic
                entered.set()
                await release.wait()
                return await atomic(stream_id, identity, event)

            async def resolved(msg):
                result = await resolve(msg)
                if msg.get("from_stream_id") == HOST + ":advisor":
                    waiting.set()
                return result

            h["store"].tell_event_identity = pause_once
            h["sessions"].resolve = resolved
            owner = asyncio.create_task(h["dispatch"](_send(), "peer"))
            waiter = None
            try:
                await asyncio.wait_for(entered.wait(), 5)
                frame = _send(from_stream_id=HOST + ":advisor")
                waiter = asyncio.create_task(h["dispatch"](frame, "advisor"))
                await asyncio.wait_for(waiting.wait(), 5)
                cancelled = owner if cancel_owner else waiter
                cancelled.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await cancelled
                release.set()
                _persisted(await asyncio.wait_for(waiter if cancel_owner else owner, 5))
                retry, principal = (frame, "advisor") if cancel_owner else (_send(), "peer")
                before = await _snapshot(h)
                _persisted(await asyncio.wait_for(h["dispatch"](retry, principal), 5))
                assert await _snapshot(h) == before
                assert len(before["events"]) == 1 and before["receipts"] == 0
                assert before["counts"]["pane_submissions"] == 0
            finally:
                release.set()
                await asyncio.gather(owner, *([waiter] if waiter else []), return_exceptions=True)
    asyncio.run(run())
