"""D02 crash boundaries over the real file stores and authenticated websocket."""

import asyncio
import base64
from contextlib import contextmanager
from datetime import datetime
import hashlib
import time
import uuid

from error_alerts_fixture import require


class InjectedPersistenceBoundary(RuntimeError):
    pass


@contextmanager
def interrupt_call(owner, name, predicate, *, after):
    """One owned IO boundary; execute the real operation unless cut before it."""
    original = getattr(owner, name)
    hits = []

    async def call(*args, **kwargs):
        selected = predicate(args, kwargs)
        if selected and not hits:
            hits.append({"after_commit": after})
            if after:
                await original(*args, **kwargs)
            raise InjectedPersistenceBoundary(name)
        return await original(*args, **kwargs)

    setattr(owner, name, call)
    try:
        yield hits
    finally:
        setattr(owner, name, original)


async def restart_boundaries(h):
    await h.pause_pump()
    oid = str(uuid.uuid4())
    principal = "operator:" + h.credential_id
    intent = {
        "version": 1,
        "operation_id": oid,
        "origin_stream_id": "fixture:v2-test",
        "origin_generation": h.origin["session_generation"],
        "operation_kind": "voice_chat",
        "client_build": "restart-fixture",
    }
    rid = "restart-upload-" + oid
    cuts = {}
    try:
        async with h.client() as client:
            init = await client.rpc(
                "upload_blob_init",
                request_id=rid,
                purpose="generic",
                voice_operation=intent,
            )
            require(init["type"] == "upload_blob.init.ok", "D02 init failed")
        registered = await h.store.voice_get(principal, oid)
        require(registered is not None, "D02 init ACK preceded operation commit")
        await h.restart()
        reopened = await h.store.voice_get(principal, oid)
        require(reopened == registered, "D02 init restart lost or changed operation")
        cuts["after_init_commit"] = {"classification": "PASS"}

        # A lost final reply cancels the request while the product joins its
        # already-committed bytes/milestone boundary. No synthetic ACK is used.
        entered, release = asyncio.Event(), asyncio.Event()
        original = h.store.voice_milestone
        calls = 0

        async def committed_before_reply(*args, **kwargs):
            nonlocal calls
            result = await original(*args, **kwargs)
            if args[:3] == (principal, oid, "upload_committed"):
                calls += 1
                entered.set()
                await release.wait()
            return result

        h.store.voice_milestone = committed_before_reply
        pending = None
        try:
            async with h.client() as client:
                init = await client.rpc(
                    "upload_blob_init",
                    request_id=rid,
                    purpose="generic",
                    voice_operation=intent,
                )
                require(
                    init["type"] == "upload_blob.init.ok", "D02 restarted upload failed"
                )
                pending = asyncio.create_task(
                    client.rpc(
                        "upload_blob_chunk",
                        request_id=rid,
                        final=True,
                        data_b64=base64.b64encode(b"disposable restart voice").decode(),
                    )
                )
                await asyncio.wait_for(entered.wait(), 5)
            # Closing the peer is the fault; release permits joined cleanup.
        finally:
            release.set()
            if pending:
                await asyncio.gather(pending, return_exceptions=True)
            h.store.voice_milestone = original
        require(calls == 1, "D02 finalization boundary not reached exactly once")
        committed = await h.store.voice_get(principal, oid)
        require(
            committed["data"]["blob_sha"]
            == hashlib.sha256(b"disposable restart voice").hexdigest(),
            "D02 committed blob identity lost",
        )
        started = committed["data"]["upload_committed"]
        await h.restart()
        require(
            (await h.store.voice_get(principal, oid))["data"]["upload_committed"]
            == started,
            "D02 restart extended upload deadline",
        )
        cuts["after_bytes_commit_before_reply"] = {
            "classification": "PASS",
            "hits": calls,
        }
        due = datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp() + 60
        await asyncio.sleep(max(0, due - time.time()))

        for label, method, predicate, after in (
            (
                "before_notification_projection",
                "call",
                lambda a, k: a[0] == "error_upsert",
                False,
            ),
            (
                "after_notification_commit",
                "call",
                lambda a, k: a[0] == "error_upsert",
                True,
            ),
            (
                "after_enqueue_before_watermark",
                "call",
                lambda a, k: a[0] == "error_patch"
                and len(a) > 2
                and "notice_ids" in a[2],
                False,
            ),
        ):
            with interrupt_call(h.notify._db, method, predicate, after=after) as hits:
                try:
                    await h.server.error_alerts.reconcile()
                except InjectedPersistenceBoundary:
                    pass
                else:
                    raise AssertionError(
                        "D02 expected persistence cut not reached: " + label
                    )
            require(len(hits) == 1, "D02 persistence cut count changed")
            before = [
                r for r in await h.server.error_alerts.notice_rows() if oid in r["body"]
            ]
            await h.restart()
            require(
                (await h.store.voice_get(principal, oid))["data"]["upload_committed"]
                == started,
                "D02 bridge restart changed original deadline",
            )
            cuts[label] = {
                "classification": "PASS",
                "hits": len(hits),
                "notice_ids_before_restart": [r["notice_id"] for r in before],
            }

        await h.server.error_alerts.reconcile()
        rows = [
            r
            for r in await h.notify._db.call("error_rows")
            if (r.get("error_context") or {}).get("operation_id") == oid
        ]
        require(len(rows) == 1, "D02 restart duplicated canonical fact")
        notices = [
            r for r in await h.server.error_alerts.notice_rows() if oid in r["body"]
        ]
        require(len(notices) == 1, "D02 restart duplicated immutable notice")
        require(
            before
            and notices[0]["notice_id"] == before[0]["notice_id"]
            and notices[0]["body"] == before[0]["body"],
            "D02 recovery recomputed committed notice identity or bytes",
        )
        ctx = rows[0]["error_context"]
        require(
            ctx["enqueued_notice_revision"] == ctx["desired_notice_revision"]
            and notices[0]["notice_id"] in ctx["notice_ids"],
            "D02 recovery skipped enqueue/link watermark",
        )
        return {
            "classification": "PASS",
            "cuts": cuts,
            "operation_id": oid,
            "original_upload_committed_at": started,
            "notice_id": notices[0]["notice_id"],
            "notice_body_sha256": hashlib.sha256(
                notices[0]["body"].encode()
            ).hexdigest(),
        }
    finally:
        h.resume_pump()
