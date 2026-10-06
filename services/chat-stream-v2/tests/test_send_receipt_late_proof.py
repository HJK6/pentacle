"""Late receipt proof journeys use synthetic intent and provider records only."""
import asyncio
import hashlib
import json
import sqlite3

import pytest

from server import Server
from store import SESSION_EVENT_TAIL_DDL, Store
from test_send_receipts import TARGET, _VisibleSessions


GENERATION = "synthetic-generation"
BIRTH = "2026-10-05T09:00:00Z"
CLAIM = "2026-10-05T10:00:00Z"
EVENT_TIME = "2026-10-05T10:00:10Z"
BODY = "Synthetic handoff for receipt proof."


def intent():
    proof = {"logical_id": "SYNTH-LOGICAL-ONE", "target": TARGET,
             "generation": GENERATION, "sequence": 1}
    key = "dot-email-" + hashlib.sha256(
        (proof["logical_id"] + TARGET + GENERATION + "1").encode()).hexdigest()[:40]
    return key, proof


async def setup_receipt(tmp_path):
    store = Store(str(tmp_path / "sessions.db"))
    store.start()
    host, name = TARGET.split(":", 1)
    await store.open_session(host, name, created_at=BIRTH,
                             session_generation=GENERATION, visibility="visible")
    key, proof = intent()
    await store.append_send_receipt(
        to_stream_id=TARGET, request_id=key, receipt_id="synthetic-receipt",
        state="accepted", wire_text=BODY, display_text=BODY, attachments=[],
        delivery="committed_pending_proof", submission_confirmed=False,
        reason="submit_unconfirmed", attempts=1, created_at=CLAIM)
    return store, key, proof


async def add_proof(store, key, *, archive_only=False):
    event = {"kind": "USER", "provider": "claude", "stream_id": TARGET,
             "text": BODY, "timestamp": EVENT_TIME, "request_id": key,
             "receipt_id": "synthetic-receipt"}
    identity = json.dumps([TARGET, "claude", "synthetic-record", 0, "USER"])
    await store.append_session_event(TARGET, event, identity=identity, limit=100)
    if archive_only:
        row = await store.submit(lambda c: dict(c.execute(
            "SELECT * FROM session_event_tail WHERE identity=?", (identity,)).fetchone()))
        with sqlite3.connect(str(store.path).replace("sessions.db", "sessions_archive.db")) as archive:
            archive.execute(SESSION_EVENT_TAIL_DDL)
            names = list(row)
            archive.execute("INSERT INTO session_event_tail (" + ",".join(names) +
                            ") VALUES (" + ",".join("?" for _ in names) + ")",
                            [row[n] for n in names])
        await store.submit(lambda c: (c.execute(
            "DELETE FROM session_event_tail WHERE identity=?", (identity,)), c.commit()))


async def read_receipt(store, key, proof):
    server = Server(store=store, sessions=_VisibleSessions())
    msg = {"type": "send.receipt.get", "to_stream_id": TARGET, "request_id": key}
    if proof is not None:
        msg["original_request"] = proof
    (reply,) = await server._dispatch(json.dumps(msg))
    assert reply["type"] == "send.receipt.get.ok"
    return reply["receipts"][0]


@pytest.mark.parametrize("archive_only", [False, True])
def test_late_provider_proof_promotes_without_resending(tmp_path, archive_only):
    async def run():
        store, key, proof = await setup_receipt(tmp_path)
        try:
            await add_proof(store, key, archive_only=archive_only)
            row = await read_receipt(store, key, proof)
            assert row["state"] == "landed"
            assert row["submission_confirmed"] is True
            history = await store.submit(lambda c: c.execute(
                "SELECT state FROM v2_send_receipts WHERE request_id=? ORDER BY rowid",
                (key,)).fetchall())
            assert [r[0] for r in history] == ["accepted", "landed"]
            await read_receipt(store, key, proof)
            count = await store.submit(lambda c: c.execute(
                "SELECT COUNT(*) FROM v2_send_receipts WHERE request_id=?", (key,)).fetchone()[0])
            assert count == 2
        finally:
            store.stop()
    asyncio.run(run())


def test_missing_original_request_keeps_ordinary_read(tmp_path):
    async def run():
        store, key, _ = await setup_receipt(tmp_path)
        try:
            await add_proof(store, key)
            assert (await read_receipt(store, key, None))["state"] == "accepted"
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("damage", ["logical_id", "target", "generation", "sequence", "extra", "bool"])
def test_invalid_preimage_never_promotes(tmp_path, damage):
    async def run():
        store, key, proof = await setup_receipt(tmp_path)
        try:
            await add_proof(store, key)
            if damage == "extra":
                proof["extra"] = "untrusted"
            elif damage == "bool":
                proof["sequence"] = True
            else:
                proof[damage] = "wrong" if damage != "sequence" else 2
            assert (await read_receipt(store, key, proof))["state"] == "accepted"
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("race", ["generation", "second_identity", "insert_delete", "receipt_body", "not_landed"])
def test_change_during_archive_lookup_refuses(tmp_path, monkeypatch, race):
    import receipt_proof
    real = receipt_proof.scan_archive

    async def run():
        store, key, proof = await setup_receipt(tmp_path)
        try:
            await add_proof(store, key)
            def racing(path, target, birth, claim):
                with sqlite3.connect(store.path) as c:
                    if race == "generation":
                        c.execute("UPDATE v2_session_generations SET generation='changed'")
                    elif race in {"second_identity", "insert_delete"}:
                        row = c.execute("SELECT stream_id,session_created_at,event_json,event_ts,recorded_at FROM session_event_tail LIMIT 1").fetchone()
                        c.execute("INSERT INTO session_event_tail(stream_id,session_created_at,event_key,event_json,event_ts,recorded_at,identity) VALUES (?,?,?, ?,?,?,?)",
                                  (row[0], row[1], "other-key", row[2], row[3], row[4], "other-identity"))
                        if race == "insert_delete":
                            c.execute("DELETE FROM session_event_tail WHERE identity='other-identity'")
                    elif race == "receipt_body":
                        # Fixture-only simulation of a new contradictory immutable row.
                        c.execute("INSERT INTO v2_send_receipts SELECT to_stream_id,request_id,receipt_id,state,optimistic_id,wire_digest,'changed body',content_kind,attachments_json,delivery,submission_confirmed,reason,attempts,created_at,from_stream_id,actor_stream_id,actor_trusted,meta_json FROM v2_send_receipts LIMIT 1")
                    else:
                        c.execute("INSERT INTO v2_send_receipts SELECT to_stream_id,request_id,receipt_id,'not_landed',optimistic_id,wire_digest,display_text,content_kind,attachments_json,'not_landed',0,reason,attempts,created_at,from_stream_id,actor_stream_id,actor_trusted,meta_json FROM v2_send_receipts LIMIT 1")
                return real(path, target, birth, claim)
            monkeypatch.setattr(receipt_proof, "scan_archive", racing)
            assert (await read_receipt(store, key, proof))["state"] != "landed"
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("later", ["pending", "body", "provenance", "terminal", "terminal_pending"])
def test_precedence_only_for_identical_pending_history(tmp_path, later):
    async def run():
        store, key, proof = await setup_receipt(tmp_path)
        try:
            await add_proof(store, key)
            assert (await read_receipt(store, key, proof))["state"] == "landed"
            async def append(state, body=BODY, actor=None):
                await store.append_send_receipt(
                    to_stream_id=TARGET, request_id=key, receipt_id="synthetic-receipt",
                    state=state, wire_text=body, display_text=body, attachments=[],
                    delivery="committed_pending_proof" if state == "accepted" else state,
                    submission_confirmed=False, reason="submit_unconfirmed", attempts=1,
                    actor_stream_id=actor)
            if later in {"terminal", "terminal_pending"}:
                await append("not_landed")
            if later != "terminal":
                await append("accepted", "changed" if later == "body" else BODY,
                             "fixture:other" if later == "provenance" else None)
            row = await read_receipt(store, key, proof)
            assert (row["state"] == "landed") == (later == "pending")
            ordinary = await store.get_send_receipt(TARGET, key)
            assert ordinary["state"] != "landed"
        finally:
            store.stop()
    asyncio.run(run())


def test_private_preimage_is_not_returned_or_logged(tmp_path, caplog, monkeypatch):
    import receipt_proof
    async def run():
        store, key, proof = await setup_receipt(tmp_path)
        try:
            await add_proof(store, key)
            monkeypatch.setattr(receipt_proof, "scan_archive", lambda *_: (_ for _ in ()).throw(RuntimeError(proof["logical_id"])))
            row = await read_receipt(store, key, proof)
            assert row["state"] == "accepted"
            assert proof["logical_id"] not in json.dumps(row)
            assert proof["logical_id"] not in caplog.text
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("damage", ["old_time", "wrong_stamp", "wrong_body", "wrong_lifecycle", "two_identities", "nonmonotone"])
def test_unproven_or_ambiguous_provider_event_refuses(tmp_path, damage):
    async def run():
        store, key, proof = await setup_receipt(tmp_path)
        try:
            await add_proof(store, key)
            def alter(c):
                row = dict(c.execute("SELECT * FROM session_event_tail LIMIT 1").fetchone())
                event = json.loads(row["event_json"])
                if damage == "old_time":
                    event["timestamp"] = "2026-10-05T09:59:59Z"
                elif damage == "wrong_stamp":
                    event["request_id"] = "other-request"
                elif damage == "wrong_body":
                    event["text"] = "Different synthetic body"
                elif damage == "wrong_lifecycle":
                    c.execute("UPDATE session_event_tail SET session_created_at='other-lifecycle'")
                else:
                    if damage == "nonmonotone":
                        c.execute("INSERT INTO session_event_tail(stream_id,session_created_at,event_key,event_json,recorded_at,identity) VALUES (?,?,?,?,?,?)",
                                  (TARGET, BIRTH, "old-between", json.dumps({"kind": "USER", "text": "irrelevant"}), 0, "old-between"))
                    event["timestamp"] = "2026-10-05T10:00:11Z"
                    c.execute("INSERT INTO session_event_tail(stream_id,session_created_at,event_key,event_json,event_ts,recorded_at,identity) VALUES (?,?,?,?,?,?,?)",
                              (TARGET, BIRTH, "second-record", json.dumps(event), event["timestamp"], row["recorded_at"], "second-record"))
                if damage in {"old_time", "wrong_stamp", "wrong_body"}:
                    c.execute("UPDATE session_event_tail SET event_json=?", (json.dumps(event),))
                c.commit()
            await store.submit(alter)
            assert (await read_receipt(store, key, proof))["state"] == "accepted"
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("conflicting", [False, True])
def test_same_identity_hot_archive_copies(conflicting, tmp_path):
    async def run():
        store, key, proof = await setup_receipt(tmp_path)
        try:
            await add_proof(store, key)
            row = await store.submit(lambda c: dict(c.execute("SELECT * FROM session_event_tail").fetchone()))
            row["event_id"] += 100
            if conflicting:
                event = json.loads(row["event_json"]); event["text"] = "Conflicting copy"
                row["event_json"] = json.dumps(event)
            with sqlite3.connect(tmp_path / "sessions_archive.db") as c:
                c.execute(SESSION_EVENT_TAIL_DDL)
                c.execute("INSERT INTO session_event_tail (" + ",".join(row) + ") VALUES (" + ",".join("?" for _ in row) + ")", tuple(row.values()))
            assert ((await read_receipt(store, key, proof))["state"] == "landed") is not conflicting
        finally:
            store.stop()
    asyncio.run(run())


def test_concurrent_reconciliation_appends_one_outcome(tmp_path):
    async def run():
        store, key, proof = await setup_receipt(tmp_path)
        try:
            await add_proof(store, key)
            rows = await asyncio.gather(*(read_receipt(store, key, proof) for _ in range(10)))
            assert all(r["state"] == "landed" for r in rows)
            count = await store.submit(lambda c: c.execute("SELECT COUNT(*) FROM v2_send_receipts").fetchone()[0])
            assert count == 2
        finally:
            store.stop()
    asyncio.run(run())


def test_missing_proof_does_not_open_archive(tmp_path, monkeypatch):
    import receipt_proof
    calls = []
    monkeypatch.setattr(receipt_proof, "scan_archive", lambda *_: calls.append(1))
    async def run():
        store, key, _ = await setup_receipt(tmp_path)
        try:
            assert (await read_receipt(store, key, None))["state"] == "accepted"
            assert not calls and not (tmp_path / "sessions_archive.db").exists()
        finally:
            store.stop()
    asyncio.run(run())

@pytest.mark.parametrize('damage', ['terminal', 'body', 'actor', 'receipt_id'])
def test_contradictory_history_cannot_gain_fresh_landing(tmp_path, damage):
    async def run():
        store, key, proof = await setup_receipt(tmp_path)
        try:
            await store.append_send_receipt(
                to_stream_id=TARGET, request_id=key,
                receipt_id='different-receipt' if damage == 'receipt_id' else 'synthetic-receipt',
                state='not_landed' if damage == 'terminal' else 'accepted',
                wire_text='changed' if damage == 'body' else BODY,
                display_text='changed' if damage == 'body' else BODY,
                attachments=[], delivery='committed_pending_proof',
                submission_confirmed=False,
                actor_stream_id='fixture:other' if damage == 'actor' else None)
            await store.append_send_receipt(
                to_stream_id=TARGET, request_id=key, receipt_id='synthetic-receipt',
                state='accepted', wire_text=BODY, display_text=BODY, attachments=[],
                delivery='committed_pending_proof', submission_confirmed=False)
            await add_proof(store, key)
            assert (await read_receipt(store, key, proof))['state'] == 'accepted'
        finally:
            store.stop()
    asyncio.run(run())

@pytest.mark.parametrize('bound', ['hot', 'archive', 'deadline'])
def test_incomplete_lookup_remains_pending(tmp_path, monkeypatch, bound):
    import receipt_proof
    async def run():
        store, key, proof = await setup_receipt(tmp_path)
        try:
            await add_proof(store, key, archive_only=bound != 'hot')
            if bound == 'hot':
                monkeypatch.setattr(receipt_proof, 'HOT_ROW_LIMIT', 0)
            elif bound == 'archive':
                monkeypatch.setattr(receipt_proof, 'ARCHIVE_USER_LIMIT', 0)
            else:
                monkeypatch.setattr(receipt_proof, 'ARCHIVE_SECONDS', 0)
            assert (await read_receipt(store, key, proof))['state'] == 'accepted'
        finally:
            store.stop()
    asyncio.run(run())


def test_event_before_final_pending_receipt_retains_original_claim(tmp_path):
    async def run():
        store, key, proof = await setup_receipt(tmp_path)
        try:
            await add_proof(store, key)
            await store.append_send_receipt(
                to_stream_id=TARGET, request_id=key, receipt_id='synthetic-receipt',
                state='accepted', wire_text=BODY, display_text=BODY, attachments=[],
                delivery='committed_pending_proof', submission_confirmed=False,
                created_at='2026-10-05T10:00:20Z')
            assert (await read_receipt(store, key, proof))['state'] == 'landed'
        finally:
            store.stop()
    asyncio.run(run())


def test_read_only_archive_and_private_preimage_never_stored(tmp_path):
    import hashlib
    async def run():
        store, key, proof = await setup_receipt(tmp_path)
        try:
            await add_proof(store, key, archive_only=True)
            archive = tmp_path / 'sessions_archive.db'
            before = hashlib.sha256(archive.read_bytes()).hexdigest()
            assert (await read_receipt(store, key, proof))['state'] == 'landed'
            assert hashlib.sha256(archive.read_bytes()).hexdigest() == before
            for p in tmp_path.glob('sessions*'):
                if p.is_file():
                    assert proof['logical_id'].encode() not in p.read_bytes()
        finally:
            store.stop()
    asyncio.run(run())


def test_plain_read_and_valid_proof_preserve_access_guard(tmp_path, monkeypatch):
    from sessions import VerbError
    async def run():
        store, key, proof = await setup_receipt(tmp_path)
        try:
            server = Server(store=store, sessions=_VisibleSessions())
            calls = []
            async def forbidden(*args, **kwargs):
                calls.append(True)
                raise AssertionError('proof lookup preceded access guard')
            monkeypatch.setattr(store, 'reconcile_send_receipt', forbidden)
            for value in (None, proof):
                message = {'type': 'send.receipt.get', 'to_stream_id': TARGET,
                           'request_id': key, 'original_request': value,
                           '_auth_context': {'scoped_principal': True,
                                             'scope_stream': 'fixture:other'}}
                with pytest.raises(VerbError):
                    await server._on_send_receipt_get(message)
            assert not calls
        finally:
            store.stop()
    asyncio.run(run())


def test_qa_precedence_checks_lifecycle_birth(tmp_path):
    async def run():
        store, key, proof = await setup_receipt(tmp_path)
        try:
            await add_proof(store, key)
            assert (await read_receipt(store, key, proof))["state"] == "landed"
            await store.append_send_receipt(to_stream_id=TARGET, request_id=key,
                receipt_id="synthetic-receipt", state="accepted", wire_text=BODY,
                display_text=BODY, attachments=[], delivery="committed_pending_proof",
                submission_confirmed=False, reason="submit_unconfirmed", attempts=1)
            def change_birth(c):
                c.execute("UPDATE sessions SET created_at='2026-10-05T11:00:00Z'")
                c.commit()
            await store.submit(change_birth)
            assert (await store.get_send_receipt(TARGET, key))["state"] == "accepted"
            assert (await read_receipt(store, key, proof))["state"] == "accepted"
        finally:
            store.stop()
    asyncio.run(run())
