"""Caller-keyed tell identity for held and backend-suppressed tells.

Spec: spec_pentacle__held_tell_payload_identity_2026_10.  Journeys run through the
real Server._on_tell / Comms.tell, the production ingress policy and a real Store;
the only seams are the pane recorder and (for crash gaps) a one-shot ledger fault.
"""
import asyncio
import json
import time

import pytest

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from comms import payload_digest
from server import Server
from sessions import VerbError
from test_context_nudges import _context_harness
from test_front_desk_digest import _desk
from test_nudges import HOST, _new_store

PEER = f"{HOST}:peer"
OPERATOR = {"operator_authenticated": True}


async def _front_desk(tmp_path, *, name="held.db"):
    store = _new_store(str(tmp_path / name))
    h = await _context_harness(store, parent=False)
    target = f"{HOST}:child"
    composite = _desk(store, target, h.sessions.get(target)["session_generation"])
    h.comms.assistant_ingress_policy = composite.suppress_routine_backend_ingress
    h.comms.front_desk_digest = composite.front_desk_digest
    h.server = Server(store=store, sessions=h.sessions, comms=h.comms, local_host=HOST)
    h.server.assistant_composite = composite  # main.py wires the primary composite on Server too
    return store, h, composite, target


async def _backend(tmp_path, *, name="backend.db"):
    store = _new_store(str(tmp_path / name))
    h = await _context_harness(store, parent=False)
    await h.open("astra", visibility="hidden", user_event_count=0)
    await h.observe()
    target = f"{HOST}:astra"
    composite = AssistantComposite(store, config=AssistantCompositeConfig(
        enabled=True, name="bart", stream_id=f"{HOST}:assistant", astra_stream_id=target))
    h.comms.assistant_ingress_policy = composite.suppress_routine_backend_ingress
    return store, h, composite, target


def _tell(target, tell_id, body, **extra):
    return {"type": "tell", "to_stream_id": target, "tell_id": tell_id, "message": body,
            "from_stream_id": PEER, **extra}


async def _conflict(awaitable):
    with pytest.raises(VerbError) as caught:
        await awaitable
    assert caught.value.code == "tell_id_conflict"
    return str(caught.value)


async def _fail_ledger_once(store):
    """Simulate the process dying after the hold/event commit, before the ledger."""
    real = store.put_tell_delivery

    async def crash(tell_id, envelope):
        store.put_tell_delivery = real
        raise RuntimeError("crash before ledger")

    store.put_tell_delivery = crash


def _run(coro_factory):
    asyncio.run(coro_factory())


# -- T1/T2: held key bound to its payload -------------------------------------

def test_changed_wake_under_held_key_is_conflict_and_original_hold_kept(tmp_path):
    async def run():
        store, h, composite, target = await _front_desk(tmp_path)
        try:
            first = await h.server._on_tell(_tell(target, "k1", "START: original"))
            assert first["delivery_status"] == "persisted" and not h.tmux.pasted
            rows = await composite.front_desk_digest._rows(target)
            await _conflict(h.server._on_tell(_tell(target, "k1", "GATE: changed wake")))
            assert not h.tmux.pasted
            assert await composite.front_desk_digest._rows(target) == rows
        finally:
            store.stop()
    _run(run)


def test_changed_held_body_is_typed_conflict_without_internal_notice_id(tmp_path):
    async def run():
        store, h, composite, target = await _front_desk(tmp_path)
        try:
            await h.server._on_tell(_tell(target, "k2", "START: original"))
            rows = await composite.front_desk_digest._rows(target)
            message = await _conflict(h.server._on_tell(_tell(target, "k2", "START: changed")))
            assert "frontdesk-held" not in message and "outbound_notice" not in message
            assert await composite.front_desk_digest._rows(target) == rows
            assert len(rows) == 1 and rows[0]["body"] == "START: original"
            assert not h.tmux.pasted
        finally:
            store.stop()
    _run(run)


# -- T3: identical / concurrent ------------------------------------------------

def test_identical_retry_replays_held_outcome_without_second_row_or_paste(tmp_path):
    async def run():
        store, h, composite, target = await _front_desk(tmp_path)
        try:
            first = await h.server._on_tell(_tell(target, "k3", "START: same"))
            again = await h.server._on_tell(_tell(target, "k3", "START: same"))
            assert again["delivery_status"] == first["delivery_status"] == "persisted"
            assert again.get("duplicate") is True
            assert len(await composite.front_desk_digest._rows(target)) == 1
            assert not h.tmux.pasted
            # A wake tell with its own key is still delivered directly (positive control).
            wake = await h.server._on_tell(_tell(target, "w3", "GATE: please decide"))
            assert wake["delivery_status"] != "persisted" and h.tmux.pasted
        finally:
            store.stop()
    _run(run)


def test_inflight_is_registered_before_the_policy_await(tmp_path):
    async def run():
        store, h, composite, target = await _front_desk(tmp_path)
        try:
            entered, release, calls = asyncio.Event(), asyncio.Event(), []
            real = h.comms.assistant_ingress_policy

            async def gated(**kw):
                calls.append(kw["msg"]["tell_id"])
                entered.set()
                await release.wait()
                return await real(**kw)

            h.comms.assistant_ingress_policy = gated
            first = asyncio.ensure_future(h.comms.tell(_tell(target, "k4", "START: held")))
            await asyncio.wait_for(entered.wait(), 5)
            # Different payload while the first is still inside the policy: conflict now.
            await asyncio.wait_for(_conflict(h.comms.tell(_tell(target, "k4", "START: other"))), 5)
            same = asyncio.ensure_future(h.comms.tell(_tell(target, "k4", "START: held")))
            await asyncio.sleep(0.05)
            assert not same.done() and calls == ["k4"]
            release.set()
            a, b = await asyncio.gather(first, same)
            assert a["delivery_status"] == b["delivery_status"] == "persisted"
            assert b.get("duplicate") is True and a.get("duplicate") is None
            assert len(await composite.front_desk_digest._rows(target)) == 1
            assert not h.tmux.pasted and not h.comms._inflight
        finally:
            store.stop()
    _run(run)


def test_concurrent_different_payloads_yield_one_hold_and_one_conflict(tmp_path):
    async def run():
        store, h, composite, target = await _front_desk(tmp_path)
        try:
            results = await asyncio.gather(
                h.server._on_tell(_tell(target, "k5", "START: a")),
                h.server._on_tell(_tell(target, "k5", "START: b")),
                return_exceptions=True)
            errors = [r for r in results if isinstance(r, VerbError)]
            assert len(errors) == 1 and errors[0].code == "tell_id_conflict"
            assert sum(1 for r in results if isinstance(r, dict)) == 1
            assert len(await composite.front_desk_digest._rows(target)) == 1
            assert not h.tmux.pasted
        finally:
            store.stop()
    _run(run)


def test_policy_exception_releases_only_owned_inflight_and_retry_succeeds(tmp_path):
    async def run():
        store, h, composite, target = await _front_desk(tmp_path)
        try:
            real = h.comms.assistant_ingress_policy

            async def boom(**kw):
                h.comms.assistant_ingress_policy = real
                raise RuntimeError("policy store failure")

            h.comms.assistant_ingress_policy = boom
            with pytest.raises(RuntimeError):
                await h.comms.tell(_tell(target, "k6", "START: x"))
            assert "k6" not in h.comms._inflight
            retry = await h.comms.tell(_tell(target, "k6", "START: x"))
            assert retry["delivery_status"] == "persisted" and not h.tmux.pasted
        finally:
            store.stop()
    _run(run)


# -- T4: hold committed, ledger lost -------------------------------------------

def test_hold_before_ledger_recovers_on_identical_retry_across_store_restart(tmp_path):
    async def run():
        store, h, composite, target = await _front_desk(tmp_path)
        try:
            await _fail_ledger_once(store)
            with pytest.raises(RuntimeError):
                await h.server._on_tell(_tell(target, "k7", "START: held"))
            assert await store.get_tell_delivery("k7") is None
            assert len(await composite.front_desk_digest._rows(target)) == 1
            store.stop(); store.start(); await h.sessions.refresh()
            retry = await h.server._on_tell(_tell(target, "k7", "START: held"))
            assert retry["delivery_status"] == "persisted" and not h.tmux.pasted
            ledger = await store.get_tell_delivery("k7")
            assert ledger is not None and ledger["payload_digest"] == payload_digest(target, "START: held")
            assert len(await composite.front_desk_digest._rows(target)) == 1
            replay = await h.server._on_tell(_tell(target, "k7", "START: held"))
            assert replay.get("duplicate") is True and not h.tmux.pasted
        finally:
            store.stop()
    _run(run)


@pytest.mark.parametrize("changed", ["GATE: changed wake", "START: changed routine"])
def test_hold_before_ledger_changed_retry_conflicts_and_original_survives(tmp_path, changed):
    async def run():
        store, h, composite, target = await _front_desk(tmp_path)
        try:
            await _fail_ledger_once(store)
            with pytest.raises(RuntimeError):
                await h.server._on_tell(_tell(target, "k8", "START: held"))
            store.stop(); store.start(); await h.sessions.refresh()
            rows = await composite.front_desk_digest._rows(target)
            await _conflict(h.server._on_tell(_tell(target, "k8", changed)))
            assert await composite.front_desk_digest._rows(target) == rows
            assert await store.get_tell_delivery("k8") is None and not h.tmux.pasted
            again = await h.server._on_tell(_tell(target, "k8", "START: held"))
            assert again["delivery_status"] == "persisted" and await store.get_tell_delivery("k8")
        finally:
            store.stop()
    _run(run)


def test_held_key_sender_change_is_conflict(tmp_path):
    async def run():
        store, h, composite, target = await _front_desk(tmp_path)
        try:
            await _fail_ledger_once(store)
            with pytest.raises(RuntimeError):
                await h.server._on_tell(_tell(target, "k9", "START: held"))
            await _conflict(h.server._on_tell(_tell(target, "k9", "START: held", from_stream_id=f"{HOST}:other")))
        finally:
            store.stop()
    _run(run)


def test_enqueue_identity_conflict_is_typed_only_for_keyed_tells(tmp_path):
    async def run():
        store, h, composite, target = await _front_desk(tmp_path)
        try:
            digest = composite.front_desk_digest
            await digest.ingress(target_stream_id=target, body="START: a", msg={"tell_id": "ke"}, verb="tell")
            # The pre-check cannot see a racing writer; the enqueue conflict still maps.
            async def blind(*a, **k):
                return None
            digest._retained_tell_hold = blind
            await _conflict(digest.ingress(target_stream_id=target, body="START: b", msg={"tell_id": "ke"}, verb="tell"))
            # Notice and send keep the shared store error, unchanged.
            for verb in ("notice", "send"):
                await digest.ingress(target_stream_id=target, body="START: a", msg={"tell_id": "kn"}, verb=verb)
                with pytest.raises(ValueError, match="outbound_notice_conflict"):
                    await digest.ingress(target_stream_id=target, body="START: b", msg={"tell_id": "kn"}, verb=verb)
        finally:
            store.stop()
    _run(run)


# -- G4: no extra held metadata -------------------------------------------------

def test_held_notice_metadata_is_root_generation_only(tmp_path):
    async def run():
        store, h, composite, target = await _front_desk(tmp_path)
        try:
            await h.server._on_tell(_tell(target, "kg", "START: keyed"))
            await h.server._on_tell({"type": "tell", "to_stream_id": target, "message": "START: keyless"})
            await h.server._on_tell({"type": "tell", "to_stream_id": target, "request_id": "rq", "message": "START: reqid"})
            rows = await composite.front_desk_digest._rows(target)
            assert len(rows) == 3
            for row in rows:
                assert list(json.loads(row["metadata"])) == ["root_generation"]
        finally:
            store.stop()
    _run(run)


# -- inherited limits: keyless / request-id tells ------------------------------

def test_keyless_tells_keep_inherited_body_hash_identity(tmp_path):
    async def run():
        store, h, composite, target = await _front_desk(tmp_path)
        try:
            msg = {"type": "tell", "to_stream_id": target, "message": "START: keyless", "from_stream_id": PEER}
            a = await h.server._on_tell(dict(msg))
            b = await h.server._on_tell(dict(msg))
            assert a["delivery_status"] == b["delivery_status"] == "persisted"
            assert len(await composite.front_desk_digest._rows(target)) == 1
            # A different keyless body is simply a new message, never a conflict.
            c = await h.server._on_tell({**msg, "message": "START: keyless two"})
            assert c["delivery_status"] == "persisted"
            assert len(await composite.front_desk_digest._rows(target)) == 2
            assert not h.tmux.pasted
        finally:
            store.stop()
    _run(run)


# -- T5: backend-tell event identity -------------------------------------------

def _events(store, target):
    return store.fetch_session_event_tail(target, limit=50)


def test_backend_tell_retains_exact_digest_and_identical_retry_recovers(tmp_path):
    async def run():
        store, h, composite, target = await _backend(tmp_path)
        try:
            body = "  status: heartbeat"
            first = await h.comms.tell(_tell(target, "b1", body))
            assert first["assistant_backend_ingress"] == "persisted_suppressed"
            events = await _events(store, target)
            assert len(events) == 1
            assert events[0]["raw"]["body_digest"] == payload_digest(target, body)
            again = await h.comms.tell(_tell(target, "b1", body))
            assert again.get("duplicate") is True and len(await _events(store, target)) == 1
            assert not h.tmux.pasted
        finally:
            store.stop()
    _run(run)


def test_backend_tell_event_before_ledger_recovers_and_rejects_changes(tmp_path):
    async def run():
        store, h, composite, target = await _backend(tmp_path)
        try:
            await _fail_ledger_once(store)
            with pytest.raises(RuntimeError):
                await h.comms.tell(_tell(target, "b2", "status: one"))
            assert await store.get_tell_delivery("b2") is None
            store.stop(); store.start(); await h.sessions.refresh()
            for changed in (
                _tell(target, "b2", "status: two"),                                  # routine
                _tell(target, "b2", "  status: one"),                                # leading whitespace
                _tell(target, "b2", "BLOCKER: stop"),                                # forward class
                _tell(target, "b2", "status: one", _auth_context=OPERATOR, message="operator says"),  # operator wake
                _tell(target, "b2", "status: one", _assistant_composite_backend_dispatch=True,
                      from_stream_id=f"{HOST}:other"),                               # daemon dispatch, other source
            ):
                await _conflict(h.comms.tell(changed))
            assert len(await _events(store, target)) == 1 and not h.tmux.pasted
            ok = await h.comms.tell(_tell(target, "b2", "status: one"))
            assert ok["assistant_backend_ingress"] == "persisted_suppressed"
            assert await store.get_tell_delivery("b2") is not None
            assert len(await _events(store, target)) == 1 and not h.tmux.pasted
        finally:
            store.stop()
    _run(run)


def test_backend_tell_legacy_event_compares_retained_text_and_source_only(tmp_path):
    async def run():
        store, h, composite, target = await _backend(tmp_path)
        try:
            legacy = {"stream_id": target, "provider": "composite", "kind": "SYSTEM",
                      "text": "status: legacy", "timestamp": "2026-10-01T00:00:00Z",
                      "raw": {"assistant_composite_routine_ingress": True, "verb": "tell",
                              "from_stream_id": PEER, "identity": "lg"}}
            await store.append_session_event(target, legacy, identity="assistant-ingress:tell:lg", limit=2000)
            # Coverage limit: the original leading whitespace of a legacy event is
            # unknown, so a retry differing only by it is accepted (own key: the
            # first recovery restores the ledger, which then binds the exact bytes).
            ws = await h.comms.tell(_tell(target, "lg", "   status: legacy"))
            assert ws["assistant_backend_ingress"] == "persisted_suppressed"
            await _conflict(h.comms.tell(_tell(target, "lg", "status: legacy")))
            await store.append_session_event(target, {**legacy, "timestamp": "2026-10-01T00:00:01Z"},
                                             identity="assistant-ingress:tell:lg2", limit=2000)
            same = await h.comms.tell(_tell(target, "lg2", "status: legacy"))
            assert same["assistant_backend_ingress"] == "persisted_suppressed"
            await _conflict(h.comms.tell(_tell(target, "lg2", "status: different")))
            # Source is compared on the retained event (before any ledger exists).
            await store.append_session_event(target, {**legacy, "timestamp": "2026-10-01T00:00:02Z"},
                                             identity="assistant-ingress:tell:lg3", limit=2000)
            await _conflict(h.comms.tell(_tell(target, "lg3", "status: legacy", from_stream_id=f"{HOST}:other")))
            assert len(await _events(store, target)) == 3 and not h.tmux.pasted
        finally:
            store.stop()
    _run(run)


def test_backend_forward_and_wake_tells_without_a_retained_identity_still_pass(tmp_path):
    async def run():
        store, h, composite, target = await _backend(tmp_path)
        try:
            await h.comms.tell(_tell(target, "f1", "BLOCKER: need decision"))
            await h.comms.tell(_tell(target, "f2", "operator input", _auth_context=OPERATOR))
            assert len(h.tmux.pasted) == 2 and not await _events(store, target)
        finally:
            store.stop()
    _run(run)


# -- T6: guards run before any hold ---------------------------------------------

def test_guards_refuse_before_holding(tmp_path):
    async def run():
        store, h, composite, target = await _front_desk(tmp_path)
        try:
            with pytest.raises(VerbError):
                await h.server._on_tell(_tell(f"{HOST}:missing", "g1", "START: x"))
            with pytest.raises(VerbError) as blank:
                await h.server._on_tell(_tell(target, "g2", "  \n "))
            assert blank.value.code == "bad_request"
            with pytest.raises(VerbError):
                await h.server._on_tell(_tell(target, "g3", "START: \x1b[31mcolour"))
            await h.open("dead", visibility="hidden", user_event_count=0)
            await store.mark_closed(HOST, "dead", closed_at="2026-10-01T00:00:00Z", pane_status="pane_dead")
            await h.sessions.refresh()
            with pytest.raises(VerbError):
                await h.server._on_tell(_tell(f"{HOST}:dead", "g4", "START: x"))
            assert await composite.front_desk_digest._rows(target) == []
            assert not h.tmux.pasted
            for key in ("g1", "g2", "g3", "g4"):
                assert await store.get_tell_delivery(key) is None
        finally:
            store.stop()
    _run(run)


@pytest.mark.parametrize("state", ["unproven", "reset_blocked"])
def test_codex_pending_target_refuses_instead_of_holding(tmp_path, state):
    async def run():
        store = _new_store(str(tmp_path / "codex.db"))
        try:
            h = await _context_harness(store, parent=False)
            await h.open("cx", provider="codex", visibility="hidden", bootstrap_state=state)
            await h.observe()
            target = f"{HOST}:cx"
            composite = _desk(store, target, h.sessions.get(target)["session_generation"])
            h.comms.assistant_ingress_policy = composite.suppress_routine_backend_ingress
            h.comms.front_desk_digest = composite.front_desk_digest
            server = Server(store=store, sessions=h.sessions, comms=h.comms, local_host=HOST)
            server.assistant_composite = composite
            with pytest.raises(VerbError):
                await server._on_tell(_tell(target, "cx1", "START: held?"))
            assert await composite.front_desk_digest._rows(target) == [] and not h.tmux.pasted
        finally:
            store.stop()
    _run(run)


# -- upgrade compatibility: sanitize=true tells held/recorded RAW before this change ----------
# The previous Server._on_tell held (or recorded) the caller's RAW text and wrote no ledger.
# An identical same-key retry must recover; any change must still be a typed conflict.

RAW = "status: \x1b[31mred\x1b[0m relay"
CLEAN = "status: red relay"


def _legacy_front_desk_hold(composite, target, key, raw=RAW, sender=PEER):
    msg = _tell(target, key, raw, sanitize=True, from_stream_id=sender)
    return composite.suppress_routine_backend_ingress(
        target_stream_id=target, body=str(msg.get("text") or msg.get("message") or ""), msg=msg, verb="tell")


def _legacy_backend_event(target, key, text=RAW.lstrip(), sender=PEER, **raw_extra):
    return {"stream_id": target, "provider": "composite", "kind": "SYSTEM", "text": text,
            "timestamp": "2026-10-01T00:00:00Z",
            "raw": {"assistant_composite_routine_ingress": True, "verb": "tell",
                    "from_stream_id": sender, "identity": key, **raw_extra}}


def test_legacy_raw_front_desk_hold_recovers_identical_sanitize_retry_across_restart(tmp_path):
    async def run():
        store, h, composite, target = await _front_desk(tmp_path)
        try:
            assert (await _legacy_front_desk_hold(composite, target, "u1"))["delivery_status"] == "persisted"
            assert await store.get_tell_delivery("u1") is None
            rows = await composite.front_desk_digest._rows(target)
            assert len(rows) == 1 and rows[0]["body"] == RAW
            store.stop(); store.start(); await h.sessions.refresh()
            first = await h.server._on_tell(_tell(target, "u1", RAW, sanitize=True))
            assert first["delivery_status"] == "persisted" and not h.tmux.pasted
            assert await composite.front_desk_digest._rows(target) == rows   # old record untouched
            ledger = await store.get_tell_delivery("u1")
            assert ledger["payload_digest"] == payload_digest(target, CLEAN)
            again = await h.server._on_tell(_tell(target, "u1", RAW, sanitize=True))
            assert again.get("duplicate") is True and not h.tmux.pasted
            assert await composite.front_desk_digest._rows(target) == rows
        finally:
            store.stop()
    _run(run)


def test_legacy_raw_backend_event_recovers_identical_sanitize_retry_across_restart(tmp_path):
    async def run():
        store, h, composite, target = await _backend(tmp_path)
        try:
            await store.append_session_event(target, _legacy_backend_event(target, "u2"),
                                             identity="assistant-ingress:tell:u2", limit=2000)
            before = await _events(store, target)
            store.stop(); store.start(); await h.sessions.refresh()
            first = await h.comms.tell(_tell(target, "u2", RAW, sanitize=True))
            assert first["assistant_backend_ingress"] == "persisted_suppressed" and not h.tmux.pasted
            assert await _events(store, target) == before                     # old record untouched
            assert (await store.get_tell_delivery("u2"))["payload_digest"] == payload_digest(target, CLEAN)
            again = await h.comms.tell(_tell(target, "u2", RAW, sanitize=True))
            assert again.get("duplicate") is True and await _events(store, target) == before
        finally:
            store.stop()
    _run(run)


@pytest.mark.parametrize("changed", [
    "status: \x1b[32mred\x1b[0m relay",   # same visible text, different raw escape bytes
    "status: \x1b[31mblue\x1b[0m relay",  # different visible text
    "status: red relay",                   # the cleaned form itself is not the retained raw text
])
def test_legacy_raw_changed_payload_conflicts_before_enqueue_or_paste(tmp_path, changed):
    async def run():
        store, h, composite, target = await _front_desk(tmp_path)
        try:
            await _legacy_front_desk_hold(composite, target, "u3")
            rows = await composite.front_desk_digest._rows(target)
            await _conflict(h.server._on_tell(_tell(target, "u3", changed, sanitize=True)))
            assert await composite.front_desk_digest._rows(target) == rows
            assert await store.get_tell_delivery("u3") is None and not h.tmux.pasted
        finally:
            store.stop()
    _run(run)


def test_legacy_raw_backend_changed_payload_and_new_digest_events_are_never_relaxed(tmp_path):
    async def run():
        store, h, composite, target = await _backend(tmp_path)
        try:
            await store.append_session_event(target, _legacy_backend_event(target, "u4"),
                                             identity="assistant-ingress:tell:u4", limit=2000)
            await _conflict(h.comms.tell(_tell(target, "u4", "status: \x1b[31mblue\x1b[0m relay", sanitize=True)))
            await _conflict(h.comms.tell(_tell(target, "u4", RAW, sanitize=True, from_stream_id=f"{HOST}:other")))
            # A digest-bearing event holding the same raw text but another digest never uses the raw fallback.
            await store.append_session_event(
                target, _legacy_backend_event(target, "u5", body_digest=payload_digest(target, "something else")),
                identity="assistant-ingress:tell:u5", limit=2000)
            await _conflict(h.comms.tell(_tell(target, "u5", RAW, sanitize=True)))
            assert len(await _events(store, target)) == 2 and not h.tmux.pasted
        finally:
            store.stop()
    _run(run)


def test_legacy_raw_compat_needs_a_valid_sanitize_request_and_the_validated_body(tmp_path):
    async def run():
        store, h, composite, target = await _front_desk(tmp_path)
        try:
            await _legacy_front_desk_hold(composite, target, "u6")
            rows = await composite.front_desk_digest._rows(target)
            # sanitize=false: the raw ESC still refuses at the route guard (not a conflict, no new hold).
            with pytest.raises(VerbError) as guard:
                await h.server._on_tell(_tell(target, "u6", RAW))
            assert guard.value.code != "tell_id_conflict"
            # message/text pair whose routed body is NOT what the raw text sanitizes to: no smuggling.
            await _conflict(h.server._on_tell(_tell(target, "u6", "status: other", text=RAW, sanitize=True)))
            # Different sender under the same key.
            await _conflict(h.server._on_tell(_tell(target, "u6", RAW, sanitize=True, from_stream_id=f"{HOST}:other")))
            assert await composite.front_desk_digest._rows(target) == rows
            assert await store.get_tell_delivery("u6") is None and not h.tmux.pasted
            # Identical message/text pair that cleans to the same body is the same payload and recovers.
            ok = await h.server._on_tell(_tell(target, "u6", CLEAN, text=RAW, sanitize=True))
            assert ok["delivery_status"] == "persisted"
        finally:
            store.stop()
    _run(run)


def test_new_sanitize_tell_records_cleaned_body_and_compares_exactly(tmp_path):
    async def run():
        store, h, composite, target = await _front_desk(tmp_path)
        try:
            await _fail_ledger_once(store)
            with pytest.raises(RuntimeError):
                await h.server._on_tell(_tell(target, "n1", RAW, sanitize=True))
            rows = await composite.front_desk_digest._rows(target)
            assert rows[0]["body"] == CLEAN
            await _conflict(h.server._on_tell(_tell(target, "n1", "status: blue relay", sanitize=True)))
            ok = await h.server._on_tell(_tell(target, "n1", RAW, sanitize=True))
            assert ok["delivery_status"] == "persisted" and await composite.front_desk_digest._rows(target) == rows
        finally:
            store.stop()
    _run(run)
