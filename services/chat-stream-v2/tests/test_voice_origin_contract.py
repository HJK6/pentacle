"""Canonical Store generation survives projection into the voice operation."""
import uuid

import pytest

from assistant_composite import COMPOSITE_GENERATION
from test_error_alerts import subject  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["voice_chat", "voice_answer"])
async def test_projected_composite_registers_with_its_actual_store_generation(subject, kind):
    s = subject
    composite = s.server.assistant_composite
    await composite.ensure_projection()
    authoritative = await s.store.fetch_session("fixture", "assistant")
    projected = composite.project_session(authoritative)
    intent = {**s.intent, "origin_stream_id": "fixture:assistant",
              "origin_generation": projected["session_generation"], "operation_kind": kind}
    # Existing ingress + real Store, including on the unchanged base: failure
    # is origin_mismatch, not an absent future protocol method.
    principal, oid = await s.service.register_upload({
        "request_id": "origin-contract-upload", "voice_operation": intent,
        "_auth_context": s.auth,
    })
    row = await s.store.voice_get(principal, oid)
    assert row["origin_generation"] == authoritative["session_generation"]
    assert row["origin_generation"] != COMPOSITE_GENERATION
    await s.store.voice_milestone(principal, oid, "upload_committed", blob_sha="a" * 64)

    async def synthetic_asr(msg):
        return {"type": "transcribe_blob.ok", "text": "Synthetic voice fixture."}

    result = await s.service.transcribe({
        "voice_operation": intent, "request_id": "transcribe-" + oid,
        "blob_sha": "a" * 64, "mime": "audio/mp4", "_auth_context": s.auth,
    }, synthetic_asr)
    assert result["text"] == "Synthetic voice fixture."
    # Real composite input persists canonical USER/route; no provider is run.
    sent = await s.service.send({
        "operation_id": oid, "request_id": "origin-send", "stream_id": "fixture:assistant",
        "optimistic_id": "origin-optimistic", "message": result["text"],
        "meta": {"voice": {"duration_s": 2}}, "_auth_context": s.auth,
    }, composite.accept_input)
    assert sent["route_id"]
    events = await s.store.fetch_session_event_tail("fixture:assistant", limit=10)
    assert len([e for e in events if e["kind"] == "USER"]) == 1
    row = await s.store.voice_get(principal, oid)
    assert row["data"]["transcribe_result"]
    assert row["data"]["server_send_request_ids"] == ["origin-send"]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [None, "", 123, False])
async def test_projection_omits_absent_or_invalid_generation(subject, missing):
    row = {"stream_id": "fixture:assistant", "session_generation": missing}
    assert "session_generation" not in subject.server.assistant_composite.project_session(row)


@pytest.mark.asyncio
async def test_projection_does_not_fabricate_generation(subject):
    assert "session_generation" not in subject.server.assistant_composite.project_session({})


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["voice_chat", "voice_answer"])
@pytest.mark.parametrize("invalid", ["old_constant", "replaced", "closed"])
async def test_old_or_closed_origin_never_registers(subject, kind, invalid):
    s = subject
    await s.server.assistant_composite.ensure_projection()
    original = await s.store.fetch_session("fixture", "assistant")
    generation = original["session_generation"]
    if invalid == "old_constant":
        generation = COMPOSITE_GENERATION
    elif invalid == "replaced":
        await s.store.submit(lambda c: (c.execute(
            "UPDATE v2_session_generations SET generation=? WHERE host='fixture' AND session_name='assistant'",
            (str(uuid.uuid4()),)), c.commit()))
    else:
        await s.store.submit(lambda c: (c.execute(
            "UPDATE sessions SET status='closed' WHERE host='fixture' AND session_name='assistant'"), c.commit()))
    intent = {**s.intent, "origin_stream_id": "fixture:assistant",
              "origin_generation": generation, "operation_kind": kind}
    with pytest.raises(ValueError, match="origin_mismatch"):
        await s.service.register_upload({"request_id": "bad-origin", "voice_operation": intent,
                                         "_auth_context": s.auth})
