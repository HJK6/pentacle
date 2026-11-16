"""Public first-pin readiness follows the current census and input custody."""
import asyncio
import json

import pytest

from comms import Comms
from provider_cli_initial import snapshot
from provider_cli_pin import pristine
from sessions import VerbError
from spawnctl import SpawnCtl
from test_provider_cli_initial_activation import ready_fixture
from test_tell_immediate_delivery import StoreTailProofCodex


ADMIN = {"peer_loopback": True, "local_admin_verified": True}


def status(target):
    return {"action": "pin-status", "pin": target, "_auth_context": ADMIN}


def promote(target, **extra):
    return {"action": "pin-set", "pin": target, "expected_revision": 0,
            "operation_id": "first-census-pin", "reason": "census fixture", "_auth_context": ADMIN, **extra}


async def pending(store, target, *, verb="tell", key="pending-one"):
    if verb == "send":
        await store.append_send_receipt(to_stream_id=target, request_id=key, receipt_id=key,
            state="accepted", delivery="accepted", submission_confirmed=False,
            wire_text="fixture input", display_text="fixture input", attachments=[])
    else:
        await store.put_tell_delivery(key, {"payload_digest": "fixture-digest", "reply": {
            "to_stream_id": target, "delivery_status": "committed_pending_proof"}, "delivery": {
            "tell_id": key, "to_stream_id": target, "delivery_status": "committed_pending_proof"}})


async def receipts(store):
    return await store.submit(lambda conn: {
        "tells": [tuple(r) for r in conn.execute("SELECT * FROM v2_tell_deliveries ORDER BY tell_id")],
        "sends": [tuple(r) for r in conn.execute("SELECT * FROM v2_send_receipts ORDER BY rowid")]})


@pytest.mark.parametrize("holder", ["closed", "missing"])
@pytest.mark.parametrize("verb", ["tell", "send"])
def test_verified_not_open_obligations_do_not_block_first_pin_or_change_receipts(tmp_path, monkeypatch, holder, verb):
    async def run():
        store, runtime, host, selected, target, row, native = await ready_fixture(tmp_path, monkeypatch)
        try:
            old = "host-a:former-holder"
            if holder == "closed":
                await store.open_session("host-a", "former-holder", provider="codex")
                await store.mark_closed("host-a", "former-holder", closed_at="2026-10-30T10:00:01Z", pane_status="pane_dead")
            await pending(store, old, verb=verb)
            saved = await receipts(store)
            result = await runtime.operation(status(target))
            assert result["initial_readiness"]["missing"] == []
            assert result["initial_readiness"].get("input_obligation_counts") == {"blocking": 0, "not_open": 1}
            after = await runtime.operation(promote(target,
                expected_readiness_digest=result["initial_readiness"]["digest"]))
            assert after["pin"] == target
            assert await receipts(store) == saved
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("verb", ["tell", "send"])
def test_obligation_after_readiness_before_prepare_is_logged(tmp_path, monkeypatch, verb):
    import provider_cli_initial
    async def run():
        store, runtime, host, selected, target, row, native = await ready_fixture(tmp_path, monkeypatch)
        entered = []
        try:
            initial = (await runtime.operation(status(target)))["initial_readiness"]
            assert initial["missing"] == []
            await pending(store, row["stream_id"], verb=verb, key="after-readiness")
            saved = await receipts(store)
            commit = provider_cli_initial.commit
            def witnessed(a, *args, **kwargs):
                assert a.conn.in_transaction
                entered.append(True)
                return commit(a, *args, **kwargs)
            monkeypatch.setattr(provider_cli_initial, "commit", witnessed)
            result = await runtime.operation(promote(target, expected_readiness_digest=initial["digest"]))
            assert result["pin"] == target and entered == [True]
            expected = {"to_stream_id": row["stream_id"], "tell_id": "after-readiness", "delivery": "committed_pending_proof"}
            if verb == "send":
                expected = {"to_stream_id": row["stream_id"], "request_id": "after-readiness", "state": "accepted", "delivery": "accepted"}
            assert result["receipt"]["initial_readiness"]["input_obligations"] == {
                "prepare": [expected], "commit": [expected], "added": [], "removed": []}
            assert await receipts(store) == saved
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["open", "open-unbound", "invalid", "no-target", "composite", "malformed"])
def test_ordinary_obligations_are_logged_but_unknown_custody_refuses(tmp_path, monkeypatch, kind):
    async def run():
        store, runtime, host, selected, target, row, native = await ready_fixture(tmp_path, monkeypatch)
        try:
            holder = row["stream_id"]
            if kind == "open-unbound":
                holder = "host-a:unbound-holder"
                await store.open_session("host-a", "unbound-holder", provider="codex")
            if kind == "invalid": holder = "not-a-stream"
            if kind == "no-target": holder = None
            if kind == "composite":
                holder = "host-a:logical-assistant"
                runtime.server._composite_for = lambda value: object() if value == holder else None
            await pending(store, holder)
            if kind == "malformed":
                def corrupt(conn):
                    conn.execute("UPDATE v2_tell_deliveries SET reply='broken-json'"); conn.commit()
                await store.submit(corrupt)
            result = await runtime.operation(status(target))
            missing = result["initial_readiness"]["missing"]
            saved = await receipts(store)
            if kind == "open":
                assert missing == []
                after = await runtime.operation(promote(target))
                logged = after["receipt"]["initial_readiness"]["input_obligations"]
                assert logged["prepare"] == logged["commit"] == result["initial_readiness"]["input_obligations"]["prepare"]
                assert len(logged["prepare"]) == 1 and logged["added"] == logged["removed"] == []
            else:
                if kind == "open-unbound":
                    assert not any(v["reason"] == "input_submission_unresolved" for v in missing)
                    assert {"stream_id": holder, "reason": "seat_binding_unverified"} in missing
                else:
                    assert any(v["reason"] == "input_submission_unresolved" for v in missing)
                with pytest.raises(VerbError) as refused:
                    await runtime.operation(promote(target))
                assert refused.value.code == "provider_cli_initial_readiness_required"
                assert await runtime.run(pristine)
            assert await receipts(store) == saved
        finally: store.stop()
    asyncio.run(run())


def test_not_open_churn_changes_only_diagnostics_not_security_digest(tmp_path, monkeypatch):
    async def run():
        store, runtime, host, selected, target, row, native = await ready_fixture(tmp_path, monkeypatch)
        try:
            first = (await runtime.operation(status(target)))["initial_readiness"]
            before = await runtime.run(lambda a: snapshot(a, runtime.clock()))
            await pending(store, "retired-host:missing-holder")
            after = await runtime.run(lambda a: snapshot(a, runtime.clock()))
            second = (await runtime.operation(status(target)))["initial_readiness"]
            assert before == after and before["input_obligations"] == []
            assert first["snapshot_digest"] == second["snapshot_digest"]
            assert first["digest"] == second["digest"]
            assert second.get("input_obligation_counts") == {"blocking": 0, "not_open": 1}
            assert "input_obligation_counts" not in before
        finally: store.stop()
    asyncio.run(run())


def test_missing_holder_opening_during_remote_proof_refuses_first_cas(tmp_path, monkeypatch):
    async def run():
        store, runtime, host, selected, target, row, native = await ready_fixture(tmp_path, monkeypatch)
        verify = runtime.executor.verify_artifact
        changed = False
        async def raced(*args, **kwargs):
            nonlocal changed
            proof = await verify(*args, **kwargs)
            if not changed:
                changed = True
                await store.open_session("host-a", "reopened-holder", provider="codex")
            return proof
        try:
            await pending(store, "host-a:reopened-holder")
            initial = await runtime.operation(status(target))
            assert initial["initial_readiness"]["missing"] == []
            runtime.executor.verify_artifact = raced
            with pytest.raises(VerbError) as refused:
                await runtime.operation(promote(target))
            assert refused.value.code == "provider_cli_initial_readiness_required"
            assert changed and await runtime.run(pristine)
        finally: store.stop()
    asyncio.run(run())


def test_not_open_churn_during_remote_proof_does_not_invalidate_cas(tmp_path, monkeypatch):
    async def run():
        store, runtime, host, selected, target, row, native = await ready_fixture(tmp_path, monkeypatch)
        verify = runtime.executor.verify_artifact
        changed = False
        async def raced(*args, **kwargs):
            nonlocal changed
            proof = await verify(*args, **kwargs)
            if not changed:
                changed = True
                await pending(store, "retired-host:another-missing-holder")
            return proof
        try:
            initial = (await runtime.operation(status(target)))["initial_readiness"]
            runtime.executor.verify_artifact = raced
            result = await runtime.operation(promote(target, expected_readiness_digest=initial["digest"]))
            assert changed and result["pin"] == target
            assert await store.get_tell_delivery("pending-one") is not None
        finally: store.stop()
    asyncio.run(run())


def test_latest_send_terminal_proof_supersedes_its_pending_receipt(tmp_path, monkeypatch):
    async def run():
        store, runtime, host, selected, target, row, native = await ready_fixture(tmp_path, monkeypatch)
        try:
            await pending(store, row["stream_id"], verb="send")
            first = (await runtime.operation(status(target)))["initial_readiness"]
            assert first["missing"] == []
            assert first["input_obligations"]["prepare"] == [{"to_stream_id": row["stream_id"],
                "request_id": "pending-one", "state": "accepted", "delivery": "accepted"}]
            await store.append_send_receipt(to_stream_id=row["stream_id"], request_id="pending-one",
                receipt_id="pending-one", state="landed", delivery="landed", submission_confirmed=True,
                wire_text="fixture input", display_text="fixture input", attachments=[])
            assert (await runtime.operation(status(target)))["initial_readiness"]["missing"] == []
            assert (await runtime.operation(promote(target)))["pin"] == target
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_first_cas_logs_a_pasted_tell_whose_receipt_is_still_in_flight(tmp_path, monkeypatch, cancel):
    async def run():
        store, runtime, host, selected, target, row, native = await ready_fixture(tmp_path, monkeypatch)
        tmux = StoreTailProofCodex(store, row["stream_id"], append_on_paste=False)
        comms = Comms(store, runtime.sessions, SpawnCtl(store, runtime.sessions, tmux=tmux))
        comms.account_runtime = runtime
        entered, release = asyncio.Event(), asyncio.Event()
        async def proof_wait(*args, **kwargs):
            entered.set(); await release.wait(); return False
        comms._submission_evidence = proof_wait
        sending = moving = None
        try:
            await runtime.sessions.refresh()
            assert (await runtime.operation(status(target)))["initial_readiness"]["missing"] == []
            sending = asyncio.create_task(comms.tell({"stream_id": row["stream_id"],
                "tell_id": "paste-receipt-race", "message": "exact race input"}))
            await asyncio.wait_for(entered.wait(), 2)
            assert len(tmux.pastes) == 1
            if cancel:
                sending.cancel()
                await asyncio.sleep(0)
            moving = asyncio.create_task(runtime.operation(promote(target)))
            result = await asyncio.wait_for(moving, 1)
            assert result["pin"] == target
            logged = result["receipt"]["initial_readiness"]["input_obligations"]
            assert logged["prepare"] == logged["commit"] and len(logged["commit"]) == 1
            assert logged["commit"][0]["tell_id"] == "paste-receipt-race"
            # Pinning logs uncertainty without claiming the input was delivered.
            saved = await store.get_tell_delivery("paste-receipt-race")
            assert saved is not None and not saved["reply"]["submission_confirmed"]
            release.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError): await sending
            else: await sending
            assert not await runtime.run(pristine)
            saved = await store.get_tell_delivery("paste-receipt-race")
            assert saved["reply"]["delivery_status"] == ("proof_unavailable" if cancel else "committed_pending_proof")
            assert len(tmux.pastes) == 1 and tmux.enter_only == 0
        finally:
            release.set()
            for task in (sending, moving):
                if task is not None:
                    try: await task
                    except (asyncio.CancelledError, VerbError): pass
            store.stop()
    asyncio.run(run())


def test_proof_reader_failure_after_paste_keeps_durable_input_custody(tmp_path, monkeypatch):
    async def run():
        store, runtime, host, selected, target, row, native = await ready_fixture(tmp_path, monkeypatch)
        tmux = StoreTailProofCodex(store, row["stream_id"], append_on_paste=False)
        comms = Comms(store, runtime.sessions, SpawnCtl(store, runtime.sessions, tmux=tmux))
        comms.account_runtime = runtime
        async def unavailable(*args, **kwargs):
            raise RuntimeError("isolated proof reader unavailable after paste")
        comms._submission_evidence = unavailable
        try:
            await runtime.sessions.refresh()
            with pytest.raises(RuntimeError):
                await comms.tell({"stream_id": row["stream_id"], "tell_id": "proof-reader-failed",
                                  "message": "one uncertain native input"})
            assert len(tmux.pastes) == 1
            result = await runtime.operation(status(target))
            assert result["initial_readiness"]["missing"] == []
            after = await runtime.operation(promote(target))
            logged = after["receipt"]["initial_readiness"]["input_obligations"]
            assert logged["prepare"] == logged["commit"] and len(logged["commit"]) == 1
            assert logged["commit"][0]["tell_id"] == "proof-reader-failed"
            assert logged["commit"][0]["delivery"] == "proof_unavailable"
        finally: store.stop()
    asyncio.run(run())


def test_known_input_admission_refusal_creates_no_possible_input_receipt(tmp_path, monkeypatch):
    from provider_accounts import AccountError
    async def run():
        store, runtime, host, selected, target, row, native = await ready_fixture(tmp_path, monkeypatch)
        tmux = StoreTailProofCodex(store, row["stream_id"], append_on_paste=False)
        comms = Comms(store, runtime.sessions, SpawnCtl(store, runtime.sessions, tmux=tmux))
        comms.account_runtime = runtime
        checks = 0
        async def maintenance(_):
            nonlocal checks
            checks += 1
            if checks > 1:
                raise AccountError("provider_cli_input_maintenance")
        runtime.maintenance_input = maintenance
        try:
            await runtime.sessions.refresh()
            with pytest.raises(VerbError) as refused:
                await comms.tell({"stream_id": row["stream_id"], "tell_id": "known-no-input",
                                  "message": "must not reach native input"})
            assert refused.value.code == "provider_cli_input_maintenance"
            assert refused.value.extra["phase"] == "not_started"
            assert tmux.pastes == []
            assert await store.get_tell_delivery("known-no-input") is None
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("verb", ["tell", "send"])
@pytest.mark.parametrize("material", [None, "health_revision", "executable_sha256"])
def test_gap_obligation_is_logged_without_hiding_material_change(tmp_path, monkeypatch, verb, material):
    import provider_cli_initial
    from provider_cli_pin import history
    async def run():
        store, runtime, host, selected, target, row, native = await ready_fixture(tmp_path, monkeypatch)
        saved = []
        entered = []
        try:
            initial = (await runtime.operation(status(target)))["initial_readiness"]
            assert initial["missing"] == []
            verify = runtime.executor.verify_artifact
            async def arrived(*args, **kwargs):
                proof = await verify(*args, **kwargs)
                await pending(store, row["stream_id"], verb=verb, key="new-during-cas")
                saved.append(await receipts(store))
                return proof
            runtime.executor.verify_artifact = arrived
            commit = provider_cli_initial.commit
            def witnessed(a, *args, **kwargs):
                assert a.conn.in_transaction
                entered.append(True)
                if material == "health_revision":
                    a.conn.execute("UPDATE v2_account_bindings SET health_revision=health_revision+1 WHERE binding_key=?", (selected.key,))
                elif material == "executable_sha256":
                    data = json.loads(a._cell(selected.host, selected.provider, selected.alias)["data"])
                    data[material] = "0" * 64
                    a.conn.execute("UPDATE v2_account_bindings SET data=? WHERE binding_key=?", (json.dumps(data), selected.key))
                return commit(a, *args, **kwargs)
            monkeypatch.setattr(provider_cli_initial, "commit", witnessed)
            request = promote(target, expected_readiness_digest=initial["digest"])
            if material:
                with pytest.raises(VerbError) as refused:
                    await runtime.operation(request)
                assert refused.value.extra["reason"] == "readiness_state_changed"
                assert refused.value.extra["next_action"] == "Inspect changed fields: " + material
                assert await runtime.run(pristine)
                assert await runtime.run(history) == {}
            else:
                result = await runtime.operation(request)
                assert result["pin"] == target
                expected = {"to_stream_id": row["stream_id"], "delivery": "committed_pending_proof", "tell_id": "new-during-cas"}
                if verb == "send":
                    expected = {"to_stream_id": row["stream_id"], "request_id": "new-during-cas", "state": "accepted", "delivery": "accepted"}
                logged = result["receipt"]["initial_readiness"]["input_obligations"]
                assert logged == {"prepare": [], "commit": [expected], "added": [expected], "removed": []}
                assert "fixture input" not in json.dumps(logged)
            assert entered == [True]
            assert saved and await receipts(store) == saved[0]
            if not material:
                # Later traffic cannot rewrite the original idempotent receipt.
                runtime.executor.verify_artifact = verify
                await pending(store, row["stream_id"], key="after-pin")
                assert (await runtime.operation(request))["receipt"] == result["receipt"]
                assert await store.get_tell_delivery("after-pin") is not None
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("phase", ["before-prepare", "before-commit"])
@pytest.mark.parametrize("kind", ["invalid", "no-target", "composite", "malformed"])
def test_unknown_obligation_in_either_gap_still_refuses(tmp_path, monkeypatch, phase, kind):
    from provider_cli_pin import history
    async def run():
        store, runtime, host, selected, target, row, native = await ready_fixture(tmp_path, monkeypatch)
        saved = []
        try:
            initial = (await runtime.operation(status(target)))["initial_readiness"]
            async def unknown():
                holder = {"invalid": "not-a-stream", "no-target": None,
                          "composite": "host-a:logical", "malformed": row["stream_id"]}[kind]
                if kind == "composite":
                    runtime.server._composite_for = lambda value: object() if value == holder else None
                await pending(store, holder, key="unknown-in-gap")
                if kind == "malformed":
                    def corrupt(conn):
                        conn.execute("UPDATE v2_tell_deliveries SET reply='broken-json' WHERE tell_id='unknown-in-gap'")
                        conn.commit()
                    await store.submit(corrupt)
                saved.append(await receipts(store))
            if phase == "before-prepare":
                await unknown()
            else:
                verify = runtime.executor.verify_artifact
                async def arrived(*args, **kwargs):
                    proof = await verify(*args, **kwargs)
                    await unknown()
                    return proof
                runtime.executor.verify_artifact = arrived
            with pytest.raises(VerbError) as refused:
                await runtime.operation(promote(target, expected_readiness_digest=initial["digest"]))
            assert refused.value.code == "provider_cli_initial_readiness_required"
            assert refused.value.extra["reason"] == "input_submission_unresolved"
            assert await runtime.run(pristine) and await runtime.run(history) == {}
            assert saved and await receipts(store) == saved[0]
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("drift", ["expired", "artifact"])
def test_ordinary_obligation_does_not_hide_commit_proof_drift(tmp_path, monkeypatch, drift):
    import provider_cli_initial
    from provider_cli_pin import history
    async def run():
        store, runtime, host, selected, target, row, native = await ready_fixture(tmp_path, monkeypatch)
        try:
            initial = (await runtime.operation(status(target)))["initial_readiness"]
            await pending(store, row["stream_id"], key="ordinary-before-prepare")
            saved = await receipts(store)
            commit = provider_cli_initial.commit
            def changed(a, message, before, ready, now, **kwargs):
                assert a.conn.in_transaction
                proof = ready["proofs"][selected.key]
                if drift == "expired":
                    proof["verified_at"] = now - 121
                else:
                    proof["artifact"]["pin_digest"] = "0" * 64
                return commit(a, message, before, ready, now, **kwargs)
            monkeypatch.setattr(provider_cli_initial, "commit", changed)
            with pytest.raises(VerbError) as refused:
                await runtime.operation(promote(target, expected_readiness_digest=initial["digest"]))
            if drift == "expired":
                assert refused.value.code == "provider_cli_initial_readiness_required"
                assert refused.value.extra["reason"] == "proof_expired"
            else:
                assert refused.value.code == "account_repin_receipt_invalid"
            assert await runtime.run(pristine) and await runtime.run(history) == {}
            assert await receipts(store) == saved
        finally: store.stop()
    asyncio.run(run())
