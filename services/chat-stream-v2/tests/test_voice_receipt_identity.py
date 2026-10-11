"""Voice-v1 proof vectors and late canonical USER lookup, never a resend."""
import asyncio
import hashlib
import json

import pytest

from receipt_proof import verified_original_generation
from server import Server
from sessions import VerbError
from test_send_receipt_late_proof import (BODY, BIRTH, CLAIM, GENERATION, TARGET,
                                        add_proof, read_receipt)

LOGICAL = "123e4567-e89b-42d3-a456-426614174000"


async def setup_voice_store(tmp_path):
    from store import Store
    store = Store(str(tmp_path / "sessions.db"))
    store.start()
    host, name = TARGET.split(":", 1)
    await store.open_session(host, name, created_at=BIRTH,
                             session_generation=GENERATION, visibility="visible")
    return store, None, None


def voice_intent(target=TARGET, generation=GENERATION, sequence=1):
    proof = {"protocol": "voice-v1", "logical_id": LOGICAL, "target": target,
             "generation": generation, "sequence": sequence}
    packed = json.dumps([LOGICAL, target, generation, sequence], ensure_ascii=False,
                        separators=(",", ":"))
    return "voice-send-" + hashlib.sha256(packed.encode("utf-8")).hexdigest()[:40], proof


@pytest.mark.parametrize("target,generation,sequence,expected", [
    ("fixture:assistant", "generation-1", 1, "voice-send-b620c843aa1796134510973f91adb9be784e54e9"),
    ("fixture:会話", "génération-🚀", 1000, "voice-send-716c9023043fb12ca11a439c72b9128ef88750ab"),
    ("fixture:assistant", "generation-1", 2, "voice-send-90704e4dd6cdc962a6206371a3b832e8e4273b24"),
])
def test_cross_language_golden_vectors(target, generation, sequence, expected):
    key, proof = voice_intent(target, generation, sequence)
    assert key == expected
    assert verified_original_generation(proof, target, key) == generation


@pytest.mark.parametrize("damage", ["protocol", "missing_protocol", "extra", "missing", "uuid",
                                   "target", "generation", "sequence", "bool", "float",
                                   "zero", "exhausted", "target_long", "generation_long",
                                   "target_empty", "generation_empty", "null", "key", "key_case"])
def test_bad_voice_preimage_cannot_prove(damage):
    key, proof = voice_intent()
    if damage == "protocol": proof["protocol"] = "voice-v2"
    elif damage == "missing_protocol": del proof["protocol"]
    elif damage == "extra": proof["unexpected"] = 1
    elif damage == "missing": del proof["logical_id"]
    elif damage == "uuid": proof["logical_id"] = "not-a-uuid"
    elif damage == "target": proof["target"] = "fixture:other"
    elif damage == "generation": proof["generation"] = "other"
    elif damage == "sequence": proof["sequence"] = 2
    elif damage == "bool": proof["sequence"] = True
    elif damage == "float": proof["sequence"] = 1.0
    elif damage == "zero": proof["sequence"] = 0
    elif damage == "exhausted": proof["sequence"] = 1001
    elif damage == "target_long": proof["target"] = "x" * 161
    elif damage == "generation_long": proof["generation"] = "x" * 129
    elif damage == "target_empty": proof["target"] = ""
    elif damage == "generation_empty": proof["generation"] = ""
    elif damage == "null": proof = None
    elif damage == "key": key = "dot-email-" + key[11:]
    elif damage == "key_case": key = key.upper()
    assert verified_original_generation(proof, TARGET, key) is None


@pytest.mark.parametrize("archive_only", [False, True])
def test_voice_pending_receipt_promotes_from_late_user_without_resend(tmp_path, archive_only):
    async def run():
        store, _, _ = await setup_voice_store(tmp_path)
        try:
            key, proof = voice_intent()
            await store.append_send_receipt(to_stream_id=TARGET, request_id=key,
                receipt_id="synthetic-receipt", state="accepted", wire_text=BODY,
                display_text=BODY, attachments=[], delivery="committed_pending_proof",
                submission_confirmed=False, reason="submit_unconfirmed", attempts=1,
                created_at=CLAIM)
            await add_proof(store, key, archive_only=archive_only)
            assert (await read_receipt(store, key, proof))["state"] == "landed"
            assert (await read_receipt(store, key, proof))["state"] == "landed"
            rows = await store.submit(lambda c: c.execute(
                "SELECT state FROM v2_send_receipts WHERE request_id=? ORDER BY rowid", (key,)).fetchall())
            assert [r[0] for r in rows] == ["accepted", "landed"]
        finally:
            store.stop()
    asyncio.run(run())


def test_retry_proof_is_bound_to_its_own_attempt():
    key1, proof1 = voice_intent(sequence=1)
    key2, proof2 = voice_intent(sequence=2)
    assert key1 != key2
    assert verified_original_generation(proof1, TARGET, key2) is None
    assert verified_original_generation(proof2, TARGET, key1) is None
    assert verified_original_generation(proof2, TARGET, key2) == GENERATION


def test_voice_proof_does_not_bypass_target_access(tmp_path, monkeypatch):
    async def run():
        store, _, _ = await setup_voice_store(tmp_path)
        try:
            key, proof = voice_intent()
            from test_send_receipts import _VisibleSessions
            server = Server(store=store, sessions=_VisibleSessions())
            async def forbidden(*args, **kwargs):
                raise AssertionError("proof ran before target access")
            monkeypatch.setattr(store, "reconcile_send_receipt", forbidden)
            with pytest.raises(VerbError):
                await server._on_send_receipt_get({"type": "send.receipt.get", "to_stream_id": TARGET,
                    "request_id": key, "original_request": proof,
                    "_auth_context": {"scoped_principal": True, "scope_stream": "fixture:other"}})
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("field,value", [
    ("logical_id", "not-a-uuid"), ("logical_id", 4),
    ("target", ""), ("target", "t" * 161), ("target", 4),
    ("generation", ""), ("generation", "g" * 129), ("generation", None),
    ("sequence", True), ("sequence", 0), ("sequence", 1001), ("sequence", 1.0),
])
def test_malformed_fields_refused_even_with_matching_digest(field, value):
    _, proof = voice_intent()
    proof[field] = value
    packed = json.dumps([proof[k] for k in ("logical_id", "target", "generation", "sequence")],
                        ensure_ascii=False, separators=(",", ":"))
    key = "voice-send-" + hashlib.sha256(packed.encode()).hexdigest()[:40]
    assert verified_original_generation(proof, proof["target"], key) is None


@pytest.mark.parametrize("reason,attachments", [
    ("front_desk_held", []), ("submit_unconfirmed", [{"key": "a" * 64, "mime": "image/jpeg", "bytes": 10}]),
])
def test_voice_namespace_does_not_broaden_pending_proof_eligibility(tmp_path, reason, attachments):
    async def run():
        store, _, _ = await setup_voice_store(tmp_path)
        try:
            key, proof = voice_intent()
            await store.append_send_receipt(to_stream_id=TARGET, request_id=key,
                receipt_id="synthetic-receipt", state="accepted", wire_text=BODY,
                display_text=BODY, attachments=attachments, delivery="committed_pending_proof",
                submission_confirmed=False, reason=reason, attempts=1, created_at=CLAIM)
            await add_proof(store, key)
            assert (await read_receipt(store, key, proof))["state"] == "accepted"
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.asyncio
async def test_authenticated_wire_voice_lookup_promotes_matching_late_user(voice_harness):
    import time
    from store_voice_operations import iso
    h = voice_harness
    target = "fixture:v2-test"
    generation = h.origin["session_generation"]
    key, proof = voice_intent(target, generation)
    now = time.time()
    # Distinct fixture lifecycle birth must precede the send claim. Do not
    # weaken the production proof's birth/claim ordering or ambiguity fence.
    await h.store.submit(lambda c: (c.execute(
        "UPDATE sessions SET created_at=? WHERE host='fixture' AND session_name='v2-test'",
        (iso(now - 30),)), c.commit()))
    await h.store.append_send_receipt(to_stream_id=target, request_id=key,
        receipt_id="voice-wire-receipt", state="accepted", wire_text=BODY,
        display_text=BODY, attachments=[], delivery="committed_pending_proof",
        submission_confirmed=False, reason="submit_unconfirmed", attempts=1,
        created_at=iso(now - 10))
    event = {"kind": "USER", "provider": "codex", "stream_id": target,
             "text": BODY, "timestamp": iso(now - 5), "request_id": key,
             "receipt_id": "voice-wire-receipt"}
    await h.store.append_session_event(target, event, identity="voice-wire-user", limit=100)
    async with h.client() as client:
        result = await client.rpc("send.receipt.get", request_id=key,
                                  to_stream_id=target, original_request=proof)
    assert result["type"] == "send.receipt.get.ok"
    assert result["receipts"][0]["state"] == "landed"
    assert "original_request" not in json.dumps(result)


# Shared real authenticated server fixture. This candidate-only conformance is
# separate from U0's existing Store/verifier seam RED above.
from test_voice_upload_resume import voice_harness  # noqa: E402,F401
