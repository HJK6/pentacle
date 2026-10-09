"""Runtime cells for the narrowed delivery core (amendment v4 §6).

I01: an installed-client (untagged) audio transcription failure over the real
authenticated wire reaches the bound front desk with generation-bound proof.
R01: a pending alert follows a rebind to a new generation; the old target is
never submitted to and gets no proof.
"""

import asyncio
import base64
import hashlib
import json
import time

from error_alerts_fixture import require
from error_alerts_transport import OwnedProvider
from transcribe import Transcriber
from cosmo_e2e.harness import StubTranscriberPoster


async def _projection(h, predicate):
    service = h.server.error_alerts
    notices = {r["notice_id"]: r for r in await service.notice_rows()}
    out = []
    for fact in await h.notify._db.call("error_rows"):
        ctx = fact["error_context"]
        if predicate(ctx):
            out.append((fact, [service.delivery(notices.get(n), notices, ctx) for n in ctx["notice_ids"]]))
    return out


async def _until(predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(0.5)
    return False


async def installed_transcribe_failure(h):
    poster = StubTranscriberPoster([ConnectionError("backend down")] * 4)
    h.server._transcriber = Transcriber(h.blobs, mic_api="http://127.0.0.1:1", http_post=poster)
    content = b"installed-client audio bytes"
    sha = hashlib.sha256(content).hexdigest()
    before = len(h.provider.pastes)
    async with h.client() as client:
        rid = "upload_blob-i01"
        require((await client.rpc("upload_blob_init", request_id=rid))["type"] == "upload_blob.init.ok", "I01 init")
        done = await client.rpc("upload_blob_chunk", request_id=rid, final=True,
                                data_b64=base64.b64encode(content).decode())
        require(done.get("blob_sha") == sha, "I01 upload")
        reply = await client.rpc("transcribe_blob", request_id="transcribe-rec-i01-0",
                                 blob_sha=sha, mime="audio/mp4")
    require(not str(reply.get("type")).endswith(".ok"), "I01 stub transcription did not fail")
    started = time.monotonic()

    async def delivered():
        rows = await _projection(h, lambda c: c["family"] == "voice_operation.v1" and c["code"] == "transcribe_failed")
        return rows and any(d["state"] == "delivered" for d in rows[0][1])

    ok = await _until(delivered, 90)
    rows = await _projection(h, lambda c: c["code"] == "transcribe_failed")
    require(ok and len(rows) == 1, "I01: one installed-client transcribe alert with bound proof required")
    fact, deliveries = rows[0]
    pastes = h.provider.pastes[before:]
    require(len(pastes) == 1 and fact["notification_id"] in pastes[0], "I01: exactly one submission")
    require(sha not in pastes[0], "I01: notice carries content-derived identity")
    proof = next(d for d in deliveries if d["state"] == "delivered")
    require(proof["recipient_generation"] == h.origin["session_generation"], "I01 generation")
    return {"classification": "PASS", "notification_id": fact["notification_id"],
            "elapsed_s": round(time.monotonic() - started, 1), "proof_at": proof["proof_at"],
            "wire_reply_type": reply.get("type"), "asr_calls": len(poster.calls)}


async def rebind_follows_generation(h):
    from error_alerts_fixture import produce_voice

    await h.pause_pump()
    second = OwnedProvider(h.root, name="v2-next", socket=h.provider.socket)
    try:
        made = await produce_voice(h)
        oid = made["operation_id"]
        await asyncio.sleep(made["alert_due_after_s"])
        await h.server.error_alerts.reconcile()
        old = [r for r in await h.server.error_alerts.notice_rows() if oid in r["body"]]
        require(len(old) == 1 and old[0]["recipient_stream_id"] == "fixture:v2-test", "R01 pending old-target notice")
        require(not await h.store.get_tell_delivery(old[0]["tell_id"]), "R01 old notice already attempted")
        old_pastes = len(h.provider.pastes)
        origin = await second.start(h.store, h.sessions)
        binding = await h.composite.binding()
        await h.store.rebind_assistant(
            env_binding={"stream_id": binding["stream_id"], "generation": binding["generation"]},
            actor_stream_id=binding["stream_id"], actor_generation=binding["generation"],
            target_stream_id="fixture:v2-next", target_generation=origin["session_generation"],
            request_id="r01-rebind", expected_revision=binding["revision"])
        await h.composite.load_binding()
        h.resume_pump()

        async def landed():
            return len(second.pastes) >= 1

        require(await _until(landed, 30), "R01: alert did not follow the new binding")
        await asyncio.sleep(6)  # one more pass: no duplicate, no old-target submission
        rows = {r["notice_id"]: r for r in await h.server.error_alerts.notice_rows()}
        prior = rows[old[0]["notice_id"]]
        require(len(second.pastes) == 1 and len(h.provider.pastes) == old_pastes, "R01 duplicate or old-target submission")
        require(prior["terminal_reason"] == "superseded_binding" and not prior["delivered_at"], "R01 old notice not superseded")
        require(h.server.error_alerts.proof_at(prior) is None, "R01 false proof on old notice")
        new = [r for r in rows.values() if oid in r["body"] and r["recipient_stream_id"] == "fixture:v2-next"]
        require(len(new) == 1 and h.server.error_alerts.proof_at(new[0]), "R01 new notice lacks generation-bound proof")
        meta = json.loads(new[0]["metadata"])
        require(meta["root_generation"] == origin["session_generation"]
                and meta["predecessor_notice_id"] == prior["notice_id"], "R01 successor linkage")
        return {"classification": "PASS", "operation_id": oid, "old_notice_id": prior["notice_id"],
                "new_notice_id": new[0]["notice_id"], "new_generation": origin["session_generation"],
                "new_provider_identity": getattr(second, "identity", None)}
    finally:
        if second.started:
            await second.run("kill-session", "-t", second.name)


async def isolated(name, tmp, out, cells):
    """Run cells in a fresh harness: own root, DBs, tmux socket, port, provider.

    Retains raw evidence under out/<name> before stopping; never shares
    rate-limit history with other cells.
    """
    import shutil
    from error_alerts_fixture import ErrorAlertsHarness

    root = tmp / name
    target = out / name
    target.mkdir(parents=True, exist_ok=True)
    h = ErrorAlertsHarness(root, real_transport=True)
    results = {}
    try:
        await h.start()
        (target / "runtime.json").write_text(json.dumps({
            "owner_pid": __import__("os").getpid(), "host": "127.0.0.1", "port": h.port,
            "fixture_root": str(root), "provider_pid": h.provider.pid,
            "tmux_socket": h.provider.socket, "disposable": True}, indent=2) + "\n")
        for cell, fn in cells:
            results[cell] = await fn(h)
    finally:
        try:
            for source in [root / "provider-input.jsonl", *root.glob(".codex/sessions/*.jsonl")]:
                if source.exists():
                    shutil.copyfile(source, target / ("provider-input.jsonl" if source.name == "provider-input.jsonl" else "provider-user-" + source.name))
            if h.store and getattr(h.server, "error_alerts", None):
                trace = {"notices": await h.server.error_alerts.notice_rows(),
                         "facts": await h.notify._db.call("error_rows")}
                for sid in ("fixture:v2-test", "fixture:v2-next"):
                    trace[sid] = await h.store.list_tell_deliveries(sid, limit=100)
                (target / "proof-trace.json").write_text(json.dumps(trace, indent=2, default=str) + "\n")
        finally:
            await h.stop()
            for pattern in ("sessions.db*", "notifications.db*"):
                for source in root.glob(pattern):
                    shutil.copyfile(source, target / source.name)
    return results
