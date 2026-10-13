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
        (root / "provider.jsonl").write_text(json.dumps({"text": BRIEF if variant == "found" else "provider initialized; no brief"}) + "\n")
        before = await store.reservations(include_expired=True)
        before_row = await store.fetch_session(HOST, NAME)
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
    for variant in ("absent", "no_transcript", "found", "null", "cancel_null"):
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


if __name__ == "__main__":
    assert sys.argv[1] == "--child"
    asyncio.run(child(Path(sys.argv[2]), sys.argv[3]))


@pytest.mark.soak
@pytest.mark.live_daemon
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
    assert delivered and record["delivered"] == 1, record
    assert record["after"]["route_target_generation"] == record["binding_after"]["generation"]
    assert record["binding_after"]["generation"] != before["generation"]
