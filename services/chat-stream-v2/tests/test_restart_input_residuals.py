"""Restart crash residuals against real Store/SpawnCtl and a fake provider boundary.

Only the owned child process receives SIGKILL. FakePane persists its external
boundary across that death. Real Store, Sessions, SpawnCtl and event proof run.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import uuid

SERVICE = Path(__file__).resolve().parents[1]
REPO = SERVICE.parents[1]
sys.dont_write_bytecode = True
for path in (SERVICE, REPO / "services", REPO / "services/_shared"):
    sys.path.insert(0, str(path))
os.environ["PENTACLE_HOST_ID"] = "diagnosis-host"

import pytest
import spawnctl
from spawnctl import SpawnCtl
from sessions import Sessions, VerbError
from store import Store

HOST, NAME, REQUEST, KEY, NONCE = "diagnosis-host", "residual-seat", "request-original", "key-original", "nonce-original"
BRIEF = "Isolated restart residual investigation brief with unique evidence text."
MSG = {"objective": "Investigate isolated restart residual", "command": "fake-provider", "provider": "claude",
       "model": "claude-opus-5-5", "effort": "high", "session_name": NAME,
       "request_id": REQUEST, "idempotency_key": KEY, "prompt": BRIEF, "ready_marker": "READY"}


def write(path, data):
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")


class FakePane:
    def __init__(self, root, crash=False):
        self.root, self.crash = root, crash

    def state(self):
        path = self.root / "pane.json"
        return json.loads(path.read_text()) if path.exists() else {"alive": False, "pastes": [], "enters": 0, "kills": 0, "creates": 0, "nonce": ""}

    async def new_session(self, name, command, cwd=None, env=None):
        state = self.state()
        state.update(alive=True, nonce=env["PENTACLE_SPAWN_NONCE"], creates=state["creates"] + 1)
        write(self.root / "pane.json", state)

    async def has_session(self, name): return self.state()["alive"]
    async def session_state(self, name): return "alive" if self.state()["alive"] else "gone"
    async def pane_pid(self, name): return "99999999"  # impossible fake OS pid, never a real pane
    async def capture(self, name): return "READY"
    async def run(self, *args, **kwargs):
        assert args[0] == "show-environment", args
        return 0, "PENTACLE_SPAWN_NONCE=" + self.state()["nonce"] + "\n"
    async def paste(self, name, text):
        if self.crash:
            # Physical fake-provider input has NOT happened; SpawnCtl has just
            # returned from its awaited durable pre-paste intent refresh.
            write(self.root / "crash-boundary.json", {"pid": os.getpid(), "boundary": "entered fake paste before external effect", "physical_pastes": len(self.state()["pastes"])})
            os.kill(os.getpid(), signal.SIGKILL)
        state = self.state(); state["pastes"].append(text); write(self.root / "pane.json", state)
    async def send_enter(self, name):
        state = self.state(); state["enters"] += 1; write(self.root / "pane.json", state)
    async def kill_session(self, name):
        state = self.state(); state.update(alive=False, kills=state["kills"] + 1); write(self.root / "pane.json", state)


async def child(root, mode):
    store = Store(str(root / "sessions.db")); store.start()
    pane = FakePane(root, crash=True)
    sessions = Sessions(store, tmux=pane, local_host=HOST)
    ctl = SpawnCtl(store, sessions, tmux=pane, owner_instance_id="dead-instance")
    claimed = await store.atomic_claim_or_replay(HOST, NAME, idempotency_key=KEY,
        request_payload_hash=ctl._spawn_payload_hash(MSG), ttl_s=600, request_id=REQUEST,
        nonce=NONCE, owner_instance_id="dead-instance")
    assert claimed["status"] == "claimed"
    if mode == "null":
        write(root / "crash-boundary.json", {"pid": os.getpid(), "boundary": "durable claim before intent", "physical_pastes": 0})
        os.kill(os.getpid(), signal.SIGKILL)
    fields = {"objective": MSG["objective"], "provider": "claude", "model": MSG["model"], "effort": "high", "bootstrap_state": "starting", "session_generation": "generation-original"}
    receipt = {"transport": "direct", "state": "pending", "delivery_status": "pending"}
    await store.record_spawn_intent(HOST, NAME, {"open_fields": fields, "brief": BRIEF, "delivery_receipt": receipt}, request_id=REQUEST, nonce=NONCE)
    await ctl._spawn_fenced(HOST, NAME, REQUEST, "fake-provider", BRIEF, "READY", MSG,
        [False], [False], fields, {"resolved_launch_tuple": {"provider": "claude"}}, pane, receipt, NONCE)
    raise AssertionError("SIGKILL boundary not reached")


async def inspect_case(root, variant):
    store = Store(str(root / "sessions.db")); store.start()
    try:
        pane = FakePane(root)
        sessions = Sessions(store, tmux=pane, local_host=HOST)
        await sessions.refresh()
        ctl = SpawnCtl(store, sessions, tmux=pane, owner_instance_id="new-instance")
        # External process/transcript discovery only: real transcript searching
        # and the full adoption/event-proof decision machinery remain intact.
        async def process_tree(pid): return [pid]
        async def paths(pids):
            return [] if variant == "no_transcript" else [str(root / "provider.jsonl")]
        original_tree = spawnctl.process_tree
        spawnctl.process_tree = process_tree
        ctl._open_transcripts = paths
        original_deadline = spawnctl.ADOPTION_BOOT_DEADLINE_S
        spawnctl.ADOPTION_BOOT_DEADLINE_S = 0.1
        (root / "provider.jsonl").write_text(json.dumps({"text": BRIEF if variant in {"found", "stale_generation"} else "provider initialized; no brief"}) + "\n")
        before = await store.reservations(include_expired=True)
        before_row = await store.fetch_session(HOST, NAME)
        if variant == "stale_generation":
            await store.open_session(HOST, NAME, provider="claude", objective=MSG["objective"], session_generation="unrelated-generation")
        elif variant == "closed_bound":
            await sessions.mark_closed(HOST, NAME, reason="synthetic intentional close", expected_generation=before_row["session_generation"])
        pre_cancel = await ctl.spawn_cancel({"target": KEY}, HOST) if variant == "cancel_null" else None
        try:
            reconciled = await ctl.reconcile_spawn_intents()
            outcome = await store.get_spawn_outcome(HOST, NAME)
            status = await ctl.spawn_status({"target": KEY}, HOST)
            awaited = await ctl.await_spawn({"spawn_request_id": REQUEST})
            try:
                retry = await ctl.spawn({**MSG, "request_id": "request-retry"}, HOST)
            except VerbError as exc:
                retry = {"error_code": exc.code, "error": str(exc)}
            cancel = await ctl.spawn_cancel({"target": KEY}, HOST)
            second = await ctl.reconcile_spawn_intents()
            record = {"variant": variant, "before": before, "before_row": before_row,
                "pre_cancel": pre_cancel, "reconciled": reconciled, "outcome": outcome,
                "status": status, "awaited": awaited, "retry": retry, "cancel": cancel,
                "second_reconcile": second, "reservations_after": await store.reservations(include_expired=True),
                "row_after": await store.fetch_session(HOST, NAME), "pane_after": pane.state()}
            record["root"] = str(root)
            write(root / ("receipt-" + variant + ".json"), record)
            return record
        finally:
            spawnctl.process_tree = original_tree
            spawnctl.ADOPTION_BOOT_DEADLINE_S = original_deadline
    finally:
        store.stop()


@pytest.fixture(scope="module")
def observations(tmp_path_factory):
    run = tmp_path_factory.mktemp("restart-input-residuals")
    records = {}
    for variant in ("absent", "no_transcript", "found", "null", "cancel_null", "stale_generation", "closed_bound"):
        root = run / variant; root.mkdir()
        mode = "null" if "null" in variant else "watermark"
        proc = subprocess.run([sys.executable, str(Path(__file__)), "--child", str(root), mode],
                              capture_output=True, text=True, timeout=20)
        write(root / "child.json", {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr})
        assert proc.returncode == -signal.SIGKILL, (root, proc.returncode, proc.stderr)
        records[variant] = asyncio.run(inspect_case(root, variant))
    write(run / "summary.json", {key: {"outcome": value["outcome"], "retry": value["retry"], "cancel": value["cancel"]} for key, value in records.items()})
    return records


def test_current_behavior_and_identity_controls(observations):
    for variant in ("absent", "no_transcript", "found"):
        rec = observations[variant]
        payload = json.loads(rec["before"][0]["payload"])
        assert payload["delivery_receipt"]["proof_watermark_state"] == "reachable"
        assert rec["pane_after"]["pastes"] == []
        assert rec["pane_after"]["creates"] == 1
        assert rec["pane_after"]["kills"] == rec["pane_after"]["enters"] == 0
        assert rec["cancel"]["error_code"] == "cancel_after_bind"
        assert rec["second_reconcile"] == {"adopted": 0, "released": 0}
    assert observations["found"]["outcome"]["state"] == "delivered"
    assert observations["found"]["retry"]["replayed"] is True
    assert observations["cancel_null"]["pre_cancel"]["state"] == "cancelled"
    assert observations["cancel_null"]["outcome"]["state"] == "cancelled"
    assert not observations["cancel_null"]["pane_after"]["alive"]


@pytest.mark.parametrize("variant", ["absent", "no_transcript"])
def test_watermark_gap_is_recoverable_unknown(observations, variant):
    rec = observations[variant]
    assert rec["outcome"]["state"] == "indeterminate", rec["outcome"]
    assert rec["reservations_after"], "retain same operation for proof-only recovery"
    assert rec["outcome"]["reason"].startswith("spawn_delivery_unknown")
    assert rec["retry"]["stream_id"] == f"{HOST}:{NAME}"
    assert rec["retry"]["admitted_count"] == 1
    assert rec["awaited"]["prompt_submission_status"] == "unknown"
    assert rec["awaited"]["do_not_respawn"] is True


def test_null_preintent_reports_interruption(observations):
    rec = observations["null"]
    assert rec["before"][0]["payload"] is None
    assert rec["outcome"]["reason"].startswith("spawn_interrupted"), rec["outcome"]


def test_later_proof_settles_original_unknown_without_repaste(observations):
    before = observations["absent"]
    after = asyncio.run(inspect_case(Path(before["root"]), "found"))
    assert after["outcome"]["state"] == "delivered"
    assert after["outcome"]["request_id"] == before["outcome"]["request_id"]
    assert after["reservations_after"] == []
    assert after["pane_after"]["pastes"] == []
    assert after["pane_after"]["creates"] == 1


def test_stale_spawn_does_not_adopt_replacement_generation(observations):
    rec = observations["stale_generation"]
    assert rec["outcome"]["state"] != "delivered", rec["outcome"]
    assert rec["row_after"]["session_generation"] == "unrelated-generation"
    assert rec["pane_after"]["pastes"] == [] and rec["pane_after"]["kills"] == 0


def test_bound_spawn_intent_does_not_reopen_intentionally_closed_row(observations):
    rec = observations["closed_bound"]
    assert rec["row_after"]["status"] == "closed", rec["row_after"]
    assert rec["pane_after"]["pastes"] == [] and rec["pane_after"]["kills"] == 0


@pytest.mark.soak
@pytest.mark.timeout(240)
def test_private_daemon_pre_reconcile_input_survives_restore(tmp_path):
    """Actual daemon/Store, private tmux and fake provider; no production input."""
    from tests.soak import restart_matrix as rm, fd_resume_matrix as fm
    evidence = tmp_path / "evidence"
    record = {}
    with rm.cell_env(tmp_path / "env", evidence) as cell:
        fd = fm.FdCell(cell, assistant_role=None)
        release = cell.root / "allow-restore"
        cell.daemon.extra_env.update({
            "PENTACLE_FORCE_LIVE_DAEMON": "1",
            "PENTACLE_ASSISTANT_AUTO_RESTORE": "1",
            "PENTACLE_RECONCILER_INTERVAL_S": "3",
            "PENTACLE_TEST_RESTORE_PAUSE_AT": "before_spawn",
            "PENTACLE_TEST_RESTORE_PAUSE_FILE": str(release),
        })
        cell.daemon.start()
        fd.seed("fd", role=fm.FD_ROLE)
        fd.seed("daff", role=fm.DAFF_ROLE)
        fd.configure_composites("fd", "daff")
        before = fd.binding()
        cell.daemon.stop(signal.SIGTERM)
        cell.namespace.run("kill-session", "-t", "=fd")
        cell.daemon.start()
        original = "Synthetic original restart input"
        identity = "restart-original-input"
        send = {"type": "send", "host": "soakchat", "session_name": "assistant",
                "text": original, "optimistic_id": identity}
        record["accepted"] = fd.op(send)
        record["retry"] = fd.op(send)
        def route():
            rows = cell.sql("SELECT * FROM v2_assistant_composite_routes WHERE input_identity=?", (identity,))
            return rows[0] if rows else {}
        rm.wait_for(lambda: route().get("routing_state") != "classifying", 10, .05)
        # Wait for a complete routing attempt, not just the initial queued row.
        import time
        time.sleep(1)
        record["held"] = dict(route())
        record["inputs_before_restore"] = fd.delivered("fd", original)
        cell.daemon.stop(signal.SIGTERM)
        cell.daemon.start()
        record["retry_after_restart"] = fd.op(send)
        record["held_after_restart"] = dict(route())
        release.write_text("go")
        rm.wait_for(lambda: (fd.binding().get("restore") or {}).get("state") == "restored", 120, .25)
        delivered = bool(rm.wait_for(lambda: fd.delivered("fd", original) == 1, 15, .1))
        record["binding_before"] = before
        record["binding_after"] = fd.binding()
        record["after"] = route()
        record["delivered"] = fd.delivered("fd", original)
        record["route_count"] = len(cell.sql("SELECT route_id FROM v2_assistant_composite_routes WHERE input_identity=?", (identity,)))
        record["owned_pids"] = cell.daemon.pids
        record["owned_socket"] = cell.namespace.socket
        write(evidence / "result.json", record)
        fd.keep_db()
    assert record["held"]["routing_state"] == "queued", record
    assert record["held"]["dispatch_id"] is None, record
    assert record["inputs_before_restore"] == 0
    assert record["retry"]["assistant_composite"]["duplicate"] is True
    assert record["route_count"] == 1
    assert record["retry_after_restart"]["assistant_composite"]["duplicate"] is True
    assert record["held_after_restart"]["dispatch_id"] is None
    assert delivered and record["delivered"] == 1, record
    assert record["after"]["route_target_generation"] == record["binding_after"]["generation"]
    assert record["binding_after"]["generation"] != before["generation"]


async def _route(rig, identity="recovery-input"):
    if rig.composite._worker:
        await rig.composite._worker
    return await rig.store.get_assistant_composite_route(stream_id=rig.composite.config.stream_id, input_identity=identity)


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("transition", ["unrelated", "handoff", "same_name", "intentional_close", "binding_race"])
def test_recovery_origin_fences(tmp_path, monkeypatch, provider, transition):
    from tests.test_assistant_restore import Rig, LOCAL, SEAT, OTHER, G0
    from assistant_composite import AssistantComposite

    async def run():
        rig = Rig(tmp_path, monkeypatch, auto=False)
        sent = []
        async def dispatch(route):
            sent.append(route)
            return {"delivery": "landed"}
        try:
            await rig.start(provider=provider)
            rig.composite.input_death_proof = rig.restore.input_death_proof
            rig.composite.dispatch = dispatch
            message = {"optimistic_id": "recovery-input", "text": "original synthetic input"}
            first = await rig.composite.accept_input(message)
            held = await _route(rig)
            assert held["routing_state"] == "queued" and held["dispatch_id"] is None
            assert held["error_code"] == "assistant_awaiting_recovery"
            origin = json.loads(held["route_json"])["direct_binding_origin"]
            assert origin == {"name": rig.composite.config.name, "stream_id": SEAT, "generation": G0, "revision": 0}
            duplicate = await rig.composite.accept_input({**message, "meta": {"direct_binding_origin": {"stream_id": OTHER}}})
            assert duplicate["duplicate"] is True and duplicate["event_id"] == first["event_id"]
            assert sent == []
            # Reconstruct the consumer against the same Store: the original tuple
            # and recovery observation survive the process-local owner's lifetime.
            await rig.composite.stop()
            config = rig.composite.env_config
            rig.composite = AssistantComposite(rig.store, config=config, dispatch=dispatch)
            rig.composite.input_death_proof = rig.restore.input_death_proof
            await rig.composite.recover()
            again = await _route(rig)
            assert again["route_id"] == held["route_id"] and again["dispatch_id"] is None
            if transition == "intentional_close":
                await rig.sessions.mark_closed(LOCAL, "fd", reason="operator requested closure", expected_generation=G0)
                await rig.composite.target_closed(SEAT, G0)
                failed = await _route(rig)
                assert failed["routing_state"] == "routing_failed"
                assert failed["error_code"] == "assistant_target_closed" and sent == []
                return
            target = SEAT if transition == "same_name" else OTHER
            name = target.split(":", 1)[1]
            new_generation = "successor-generation"
            await rig.store.open_session(LOCAL, name, provider="codex" if provider == "claude" else "claude",
                pane_status="pane_alive", session_generation=new_generation)
            rig.tmux.states[name] = "alive"
            if transition == "handoff":
                def record(conn):
                    conn.execute("INSERT INTO v2_assistant_direct_handoff_proofs VALUES(?,?,?,?,?)",
                                 (target, new_generation, SEAT, G0, "synthetic-proof"))
                    conn.commit()
                await rig.store.submit(record)
            if transition == "binding_race":
                original_probe = rig.restore.input_death_proof
                async def race(*args):
                    proof = await original_probe(*args)
                    await rig.move_binding(target, new_generation, 1)
                    return proof
                rig.composite.input_death_proof = race
            else:
                await rig.move_binding(target, new_generation, 1)
            await rig.composite.recover()
            final = await _route(rig)
            assert json.loads(final["route_json"])["direct_binding_origin"] == origin
            if transition == "handoff":
                assert len(sent) == 1 and final["route_target_generation"] == new_generation
            else:
                assert final["routing_state"] == "routing_failed" and sent == []
            # Inputs newly accepted at the new binding are a separate authority.
            rig.composite.input_death_proof = rig.restore.input_death_proof
            await rig.composite.accept_input({"optimistic_id": "new-input", "text": "new synthetic input"})
            fresh = await _route(rig, "new-input")
            assert fresh["route_target_generation"] == new_generation
            assert len(sent) == (2 if transition == "handoff" else 1)
        finally:
            if hasattr(rig, "composite"):
                await rig.composite.stop()
            rig.store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("condition", ["missing_identity", "unknown_process", "live_process", "intentional_close", "revived"])
def test_input_preservation_requires_positive_absence(tmp_path, monkeypatch, condition):
    from tests.test_assistant_restore import Rig, LOCAL, SEAT, G0, PS_SAME
    async def run():
        rig = Rig(tmp_path, monkeypatch, auto=False)
        try:
            await rig.start(observer=condition != "missing_identity")
            if condition == "unknown_process":
                rig.ps = (2, "", "permission denied")
            elif condition == "live_process":
                rig.ps = PS_SAME
            elif condition in {"intentional_close", "revived"}:
                await rig.sessions.mark_closed(LOCAL, "fd", expected_generation=G0)
                if condition == "revived":
                    rig.tmux.states["fd"] = "alive"
            rig.composite.input_death_proof = rig.restore.input_death_proof
            sent = []
            async def dispatch(route):
                sent.append(route)
                return {"delivery": "landed"}
            rig.composite.dispatch = dispatch
            await rig.composite.accept_input({"optimistic_id": "recovery-input", "text": "synthetic input"})
            route = await _route(rig)
            assert route["routing_state"] == "routing_failed"
            assert route["dispatch_id"] is None and sent == []
            assert "recovery_origin" not in json.loads(route["route_json"])
        finally:
            if hasattr(rig, "composite"):
                await rig.composite.stop()
            rig.store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_protected_holder_preservation_does_not_grant_restore(tmp_path, monkeypatch, provider):
    from tests.test_assistant_restore import Rig, SEAT, G0
    async def run():
        rig = Rig(tmp_path, monkeypatch, role="lead")
        try:
            await rig.start(provider=provider)
            proof = await rig.restore.input_death_proof(SEAT, G0)
            assert proof[0] is True
            restore = await rig.restore.death_proof(SEAT, G0)
            assert restore[0] is False
            assert restore[2] in {"protected_row", "not_resumable"}
        finally:
            rig.store.stop()
    asyncio.run(run())


@pytest.mark.soak
@pytest.mark.timeout(180)
def test_private_daemon_watermark_gap_stays_unknown(tmp_path, monkeypatch):
    import shlex
    import threading
    from tests.soak import restart_matrix as rm, fd_resume_matrix as fm
    original = rm.tmux_blocking_wrapper
    def wrapper(root, socket, real_tmux, control):
        path = original(root, socket, real_tmux, control)
        blocked = shlex.quote(str(control / "block_paste"))
        marker = shlex.quote(str(control / "paste_blocked"))
        path.write_text(path.read_text().replace('case " $* " in\n',
            'case " $* " in\n  *" paste-buffer "*) if [ -e ' + blocked + ' ]; then\n'
            '    echo $$ > ' + marker + '\n    while [ -e ' + blocked + ' ]; do sleep 0.05; done\n  fi ;;\n'))
        return path
    monkeypatch.setattr(rm, "tmux_blocking_wrapper", wrapper)
    with rm.cell_env(tmp_path / "env", tmp_path / "evidence") as cell:
        fd = fm.FdCell(cell, assistant_role=None)
        cell.daemon.extra_env["PENTACLE_FORCE_LIVE_DAEMON"] = "1"
        cell.daemon.start()
        (cell.control / "block_paste").write_text("1")
        message = rm.spawn_payload("gap-seat", "Synthetic restart watermark gap prompt", "gap-key", "gap-request")
        reply = {}
        def send():
            try:
                reply["value"] = fd.op(message, timeout=90)
            except Exception as exc:
                reply["exception"] = repr(exc)
        thread = threading.Thread(target=send, daemon=True)
        thread.start()
        assert rm.wait_for(lambda: (cell.control / "paste_blocked").exists(), 45, .05)
        before = cell.snapshot("watermark-before-kill", "gap-seat")
        cell.daemon.stop(signal.SIGKILL)
        thread.join(5)
        (cell.control / "block_paste").unlink(missing_ok=True)
        cell.daemon.start()
        assert rm.wait_for(lambda: bool(cell.outcomes("gap-seat")), 35, .1)
        after = cell.snapshot("after-startup-reconcile", "gap-seat")
        write(tmp_path / "evidence/result.json", {"before": before, "after": after, "rpc": reply})
        fd.keep_db()
        outcome = cell.outcomes("gap-seat")[0]
        assert outcome["state"] == "indeterminate", outcome
        assert outcome["reason"].startswith("spawn_delivery_unknown")
        assert cell.reservations("gap-seat") and cell.panes("gap-seat")
        assert fd.delivered("gap-seat", message["initial_prompt"]) == 0


async def daemon_null_claim(root, host):
    store = Store(str(root / "sessions.db"))
    store.start()
    result = await store.atomic_claim_or_replay(host, "null-seat", idempotency_key="null-key",
        request_payload_hash="synthetic-original-hash", ttl_s=600, request_id="null-request",
        nonce="null-nonce", owner_instance_id="dead-owner")
    assert result["status"] == "claimed"
    os.kill(os.getpid(), signal.SIGKILL)


@pytest.mark.soak
@pytest.mark.timeout(60)
def test_private_daemon_null_intent_is_interruption(tmp_path):
    from tests.soak import restart_matrix as rm
    with rm.cell_env(tmp_path / "env", tmp_path / "evidence") as cell:
        child = subprocess.run([sys.executable, __file__, "--daemon-null", str(cell.root), rm.LOCAL_HOST],
                               capture_output=True, text=True, timeout=20)
        assert child.returncode == -signal.SIGKILL, child.stderr
        before = cell.reservations("null-seat")
        assert before[0]["payload"] is None
        cell.daemon.extra_env["PENTACLE_FORCE_LIVE_DAEMON"] = "1"
        cell.daemon.start()
        assert rm.wait_for(lambda: bool(cell.outcomes("null-seat")), 10, .05)
        outcome = cell.outcomes("null-seat")[0]
        write(tmp_path / "evidence/result.json", {"before": before, "outcome": outcome})
        assert outcome["reason"] == "spawn_interrupted: daemon restarted before spawn intent was persisted"
        assert outcome["request_id"] == before[0]["request_id"]
        assert outcome["request_payload_hash"] == before[0]["request_payload_hash"]
        assert cell.reservations("null-seat") == [] and cell.panes("null-seat") == []


def test_batch_resolution_retains_each_input_recovery_origin():
    """The integrated batching callback cannot replace admission's recovery fence."""
    from tests.test_assistant_duplicate_reply_sources import (
        _accept, _landed_composite, _final, ROOT,
    )

    async def run():
        sends = []
        store, composite, generation = await _landed_composite(sends)
        try:
            binding = await composite.load_binding()
            expected = {"name": composite.config.name, "stream_id": ROOT,
                        "generation": generation, "revision": binding["revision"]}
            head = await _accept(composite, store, "recovery-batch-head", "first synthetic input")
            member = await _accept(composite, store, "recovery-batch-member", "second synthetic input")
            assert member["batched_into_dispatch_id"] == head["dispatch_id"]
            for route in (head, member):
                assert json.loads(route["route_json"])["direct_binding_origin"] == expected
            result = await composite.publish(
                _final(head["dispatch_id"], head["input_identity"], "One synthetic answer.",
                       reply_to_message_ids=[head["input_identity"], member["input_identity"]]),
                actor_stream_id=ROOT,
            )
            assert result["closed_dispatch_ids"] == [member["dispatch_id"]]
            assert len(sends) == 2
            for route in (head, member):
                stored = await store.get_assistant_composite_route(
                    stream_id=composite.config.stream_id, input_identity=route["input_identity"],
                )
                assert json.loads(stored["route_json"])["direct_binding_origin"] == expected
        finally:
            await composite.stop()
            store.stop()

    asyncio.run(run())


if __name__ == "__main__":
    if sys.argv[1] == "--daemon-null":
        asyncio.run(daemon_null_claim(Path(sys.argv[2]), sys.argv[3]))
    else:
        assert sys.argv[1] == "--child"
        asyncio.run(child(Path(sys.argv[2]), sys.argv[3]))
