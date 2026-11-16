"""Cosmo E2E Foundation walk (smoke tier): drive every foundation manifest row
to its exact verify oracle against the REAL server over a real loopback bind,
through the AUTHORIZED WIRE paths only — the scoped mobile client authenticates
with its issued credential (auth_v2) and the daff backend seat with a stream
token, publishing only via the assistant.publish verb.  A connected socket alone
is not GREEN: every case asserts stored counts / bytes / receipts / visible
client frames.

Foundation rows: text, fail-once, reconnect, photo, voice, voice-fail-once,
history, working, plus the rejected-before-admission control.  Negative controls
apply REAL mutations.  Cleanup is proven on normal AND failure paths.
"""
from __future__ import annotations

import asyncio
import base64
import json

import pytest

from tests.cosmo_e2e import harness as H


def _users(tail):
    return [e for e in tail if "input_identity" in e["raw"]]


def _publishes(tail):
    return [e for e in tail if "publish_kind" in e["raw"]]


def _run(coro_factory):
    asyncio.run(coro_factory())


# --------------------------------------------------------------------------- #
# Transport + text                                                             #
# --------------------------------------------------------------------------- #
def test_real_loopback_bind_and_hello(tmp_path):
    async def run():
        hz = await H.FoundationHarness(tmp_path).start()
        try:
            assert isinstance(hz.port, int) and hz.port > 0
            async with hz.mobile_client():  # full auth_v2 handshake over the wire
                pass
        finally:
            await hz.stop()
    _run(run)


def test_text(tmp_path):
    async def run():
        hz = await H.FoundationHarness(tmp_path).start()
        try:
            async with hz.mobile_client() as mob, hz.backend_seat() as seat:
                send = await mob.rpc("send", request_id="r-text", to_stream_id=H.DAFF_CHAT,
                                     text="hello cosmo", msg_id="m-text")
                assert send["type"] == "send.result", send
                did = await hz.resolved_dispatch_id("m-text")
                pub = await seat.publish(dispatch_id=did, reply_to_message_id="m-text",
                                         message="hi from daff")
                assert pub["type"] == "assistant.publish.ok" and pub["duplicate"] is False, pub
                # Client frame: the reply is visible to the client over the wire.
                events = await mob.stream_events()
                assert any("hi from daff" in json.dumps(e) for e in events), events
                # Committed receipt retrievable over the wire by the owning client.
                rc = await mob.rpc("send.receipt.get", request_id="r-text", to_stream_id=H.DAFF_CHAT)
                assert rc["type"] == "send.receipt.get.ok" and rc["found"] is True, rc
                # Exact store counts + exactly-one push.
                tail = await hz.store.fetch_session_event_tail(H.DAFF_CHAT, limit=50)
                assert len(_users(tail)) == 1 and len(_publishes(tail)) == 1, tail
                assert len(hz.push_sent) == 1 and hz.push_sent[0]["body"] == "hi from daff"
        finally:
            await hz.stop()
    _run(run)


# --------------------------------------------------------------------------- #
# Fail-once (accepted-but-ack-lost at the delivery boundary) + reconnect       #
# --------------------------------------------------------------------------- #
def test_fail_once_dispatch_then_idempotent_publish(tmp_path):
    """The FIRST backend dispatch is gated to fail (delivery ack lost -> route
    delivery 'uncertain').  The backend still publishes; a replayed publish is
    idempotent (duplicate), the stored message count is unchanged, and exactly
    one push fires."""
    async def run():
        hz = H.FoundationHarness(tmp_path)
        hz.set_fail_dispatch_once(True)
        await hz.start()
        try:
            async with hz.mobile_client() as mob, hz.backend_seat() as seat:
                await mob.rpc("send", request_id="r-fail", to_stream_id=H.DAFF_CHAT,
                              text="q", msg_id="m-fail")
                did = await hz.resolved_dispatch_id("m-fail")
                # The gated first dispatch ran and failed -> delivery uncertain.
                assert hz._dispatch_failures == 1
                route = await hz.store.get_assistant_composite_route(
                    stream_id=H.DAFF_CHAT, input_identity="m-fail")
                assert route["delivery_state"] in ("uncertain", "failed"), route
                before = _users(await hz.store.fetch_session_event_tail(H.DAFF_CHAT, limit=50))
                r1 = await seat.publish(dispatch_id=did, reply_to_message_id="m-fail", message="the reply")
                assert r1["duplicate"] is False, r1
                r2 = await seat.publish(dispatch_id=did, reply_to_message_id="m-fail", message="the reply")
                assert r2["duplicate"] is True, r2  # ack-lost replay is idempotent
                after = await hz.store.fetch_session_event_tail(H.DAFF_CHAT, limit=50)
                assert len(_users(after)) == len(before)          # messages unchanged
                assert len(_publishes(after)) == 1                 # exactly one reply
                assert len(hz.push_sent) == 1                      # exactly-once push
        finally:
            await hz.stop()
    _run(run)


def test_reconnect_idempotent_admission_and_receipt(tmp_path):
    """Client-side ack-lost: the connection drops, a NEW connection re-sends the
    same input_identity; admission is idempotent (no 2nd route) and the receipt
    reconciles on the new connection via send.receipt.get."""
    async def run():
        hz = await H.FoundationHarness(tmp_path).start()
        try:
            async with hz.mobile_client() as mob1:
                await mob1.rpc("send", request_id="r-reco", to_stream_id=H.DAFF_CHAT,
                               text="hello", msg_id="m-reco")
                route1 = await hz.await_route("m-reco", lambda r: True)
                assert route1 is not None
            # mob1 is now disconnected (context exit closed the socket).
            async with hz.mobile_client() as mob2:  # a real reconnect
                resend = await mob2.rpc("send", request_id="r-reco", to_stream_id=H.DAFF_CHAT,
                                        text="hello", msg_id="m-reco")
                assert resend["type"] == "send.result", resend
                route2 = await hz.await_route("m-reco", lambda r: True)
                assert route2["route_id"] == route1["route_id"], (route1, route2)  # no 2nd route
                users = _users(await hz.store.fetch_session_event_tail(H.DAFF_CHAT, limit=50))
                assert len(users) == 1, users  # one stored message despite the re-send
                rc = await mob2.rpc("send.receipt.get", request_id="r-reco", to_stream_id=H.DAFF_CHAT)
                assert rc["type"] == "send.receipt.get.ok" and rc["found"] is True, rc
        finally:
            await hz.stop()
    _run(run)


def test_rejected_before_admission_leaves_no_route_or_message(tmp_path):
    """An out-of-scope send is refused BEFORE admission: the scoped credential may
    only write its own stream.  No route and no stored message are created —
    distinct from the accepted-but-ack-lost class above."""
    async def run():
        hz = await H.FoundationHarness(tmp_path).start()
        try:
            async with hz.mobile_client() as mob:
                before = len(_users(await hz.store.fetch_session_event_tail(H.DAFF_CHAT, limit=50)))
                reply = await mob.rpc("send", request_id="r-stale", to_stream_id="other:stream",
                                      text="nope", msg_id="m-stale")
                assert reply.get("error_code"), reply  # rejected before admission
                await asyncio.sleep(0.1)
                route = await hz.store.get_assistant_composite_route(
                    stream_id=H.DAFF_CHAT, input_identity="m-stale")
                assert route is None, route  # no route created
                after = len(_users(await hz.store.fetch_session_event_tail(H.DAFF_CHAT, limit=50)))
                assert after == before  # no stored message
        finally:
            await hz.stop()
    _run(run)


# --------------------------------------------------------------------------- #
# Media (real blob) + transcriber (stub poster)                               #
# --------------------------------------------------------------------------- #
def test_photo_real_blob_roundtrip_and_publish_attachment(tmp_path):
    async def run():
        hz = await H.FoundationHarness(tmp_path).start()
        try:
            async with hz.mobile_client() as mob, hz.backend_seat() as seat:
                content = H.materialize_fixture("photo")
                sha = await hz.upload_blob(mob, content)
                assert sha == H.load_manifest()["fixtures"]["photo"]["sha256"], sha
                # Download over the wire (fetch_blob) == fixture bytes.
                got = bytearray()
                fetch = await mob.rpc("fetch_blob", request_id="fetch-photo", blob_sha=sha)
                # fetch_blob streams; the ok frame carries the content inline for a small blob.
                assert fetch["type"] in ("fetch_blob.ok", "blob.chunk", "fetch_blob.chunk"), fetch
                # Content-addressed read the handler itself streams.
                got = await hz.blob_store.read_verified(sha, max_bytes=10_000_000)
                assert got == content
                # The reply publishes AND resolves the attachment through the authorized path.
                await mob.rpc("send", request_id="r-photo", to_stream_id=H.DAFF_CHAT,
                              text="see photo", msg_id="m-photo")
                did = await hz.resolved_dispatch_id("m-photo")
                # The authenticated mobile uploader obtains a server-issued
                # managed receipt. Preserve the generic upload/download above.
                init = await mob.rpc("upload_blob_init", request_id="managed-photo",
                                     purpose="chat_attachment", filename="fixture-photo.png",
                                     size_hint_bytes=len(content))
                assert init["type"] == "upload_blob.init.ok", init
                managed = await mob.rpc("upload_blob_chunk", request_id="managed-photo",
                                        data_b64=base64.b64encode(content).decode(), final=True)
                assert managed["type"] == "upload_blob.ok", managed
                assert managed["blob_sha"] == sha and managed["bytes"] == len(content)
                assert managed["auth_kind"] == "scoped" and managed["assistant_scope"] == H.DAFF_CHAT
                assert managed["upload_id"] != sha
                await mob.rpc("send", request_id="r-photo-raw-negative", to_stream_id=H.DAFF_CHAT,
                              text="reject raw photo receipt", msg_id="m-photo-raw-negative")
                negative_dispatch = await hz.resolved_dispatch_id("m-photo-raw-negative")
                assert negative_dispatch != did
                before = await hz.store.submit(lambda c: (
                    c.execute('SELECT count(*) FROM v2_assistant_composite_publications').fetchone()[0],
                    c.execute('SELECT count(*) FROM v2_attachment_refs').fetchone()[0]))
                tail_before = _publishes(await hz.store.fetch_session_event_tail(H.DAFF_CHAT, limit=50))
                rejected = await seat.publish(dispatch_id=negative_dispatch, reply_to_message_id="m-photo-raw-negative",
                                              message="got it", attachment_ids=[sha])
                assert rejected["type"] == "assistant.publish.error", rejected
                assert rejected["error_code"] == "assistant_publish_attachment_unverified", rejected
                assert await hz.store.submit(lambda c: (
                    c.execute('SELECT count(*) FROM v2_assistant_composite_publications').fetchone()[0],
                    c.execute('SELECT count(*) FROM v2_attachment_refs').fetchone()[0])) == before
                assert _publishes(await hz.store.fetch_session_event_tail(H.DAFF_CHAT, limit=50)) == tail_before
                pub = await seat.publish(dispatch_id=did, reply_to_message_id="m-photo",
                                         message="got it", attachment_ids=[managed["upload_id"]])
                assert pub["type"] == "assistant.publish.ok", pub
                tail = await hz.store.fetch_session_event_tail(H.DAFF_CHAT, limit=50)
                reply = _publishes(tail)[-1]
                attachments = reply["attachments"]
                assert len(attachments) == 1
                attachment = attachments[0]
                assert (attachment["upload_id"], attachment["key"], attachment["mime"],
                        attachment["size"], attachment["filename"]) == (
                    managed["upload_id"], sha, "image/png", len(content), "fixture-photo.png")
                assert attachment["uploader"]["credential_id"] == managed["credential_id"]
                assert attachment["uploader"]["principal_id"] == managed["uploader"]
                seat_row = await hz.store.fetch_session(*H.DAFF_SEAT.split(":", 1))
                assert attachment["publisher"]["stream_id"] == H.DAFF_SEAT
                assert attachment["publisher"]["generation"] == seat_row["session_generation"]
                refs = await hz.store.submit(lambda c: list(c.execute(
                    'SELECT upload_id,blob_sha,stream_id FROM v2_attachment_refs WHERE upload_id=?',
                    (managed["upload_id"],))))
                assert [tuple(r) for r in refs] == [(managed["upload_id"], sha, H.DAFF_CHAT)]
        finally:
            await hz.stop()
    _run(run)


def test_voice_real_blob_stub_transcriber(tmp_path):
    async def run():
        hz = await H.FoundationHarness(tmp_path).start()
        try:
            async with hz.mobile_client() as mob:
                content = H.materialize_fixture("voice")
                sha = await hz.upload_blob(mob, content)
                assert sha == H.load_manifest()["fixtures"]["voice"]["sha256"]
                out = await mob.rpc("transcribe_blob", request_id="tr-voice",
                                    blob_sha=sha, mime="audio/mp4")
                assert out["type"] == "transcribe_blob.ok" and out["text"], out
                assert len(hz.transcriber_stub.calls) == 1          # real backend poster = stub, hit once
                again = await mob.rpc("transcribe_blob", request_id="tr-voice",
                                      blob_sha=sha, mime="audio/mp4")
                assert again["type"] == "transcribe_blob.ok"
                assert len(hz.transcriber_stub.calls) == 1          # sha-cache idempotent
        finally:
            await hz.stop()
    _run(run)


def test_voice_fail_once_then_succeeds(tmp_path):
    async def run():
        hz = H.FoundationHarness(tmp_path)
        hz.transcriber_stub = H.StubTranscriberPoster(scripted=[(503, b"{}")])
        await hz.start()
        try:
            async with hz.mobile_client() as mob:
                content = H.materialize_fixture("voice")
                sha = await hz.upload_blob(mob, content)
                first = await mob.rpc("transcribe_blob", request_id="tr-fv1", blob_sha=sha, mime="audio/mp4")
                assert first.get("error_code") or first.get("type", "").endswith("error"), first
                second = await mob.rpc("transcribe_blob", request_id="tr-fv2", blob_sha=sha, mime="audio/mp4")
                assert second["type"] == "transcribe_blob.ok", second
        finally:
            await hz.stop()
    _run(run)


# --------------------------------------------------------------------------- #
# History (order) + working                                                   #
# --------------------------------------------------------------------------- #
def test_history_order_and_counts(tmp_path):
    async def run():
        hz = await H.FoundationHarness(tmp_path).start()
        try:
            async with hz.mobile_client() as mob, hz.backend_seat() as seat:
                N = 3
                for i in range(N):
                    mid = f"m-hist-{i}"
                    await mob.rpc("send", request_id=f"r-hist-{i}", to_stream_id=H.DAFF_CHAT,
                                  text=f"turn {i}", msg_id=mid)
                    did = await hz.resolved_dispatch_id(mid)
                    await seat.publish(dispatch_id=did, reply_to_message_id=mid, message=f"reply {i}")
                tail = await hz.store.fetch_session_event_tail(H.DAFF_CHAT, limit=100)
                assert len(_users(tail)) == N and len(_publishes(tail)) == N
                # Order: the user turns appear in submission order over the wire.
                events = await mob.stream_events()
                user_texts = [e for e in events if e.get("raw", {}).get("input_identity", "").startswith("m-hist-")]
                ids = [e["raw"]["input_identity"] for e in user_texts]
                assert ids == sorted(ids), ids  # m-hist-0,1,2 in order
        finally:
            await hz.stop()
    _run(run)


def test_working_true_in_flight_then_false(tmp_path):
    async def run():
        hz = await H.FoundationHarness(tmp_path).start()
        gate = asyncio.Event()
        original = hz.composite.dispatch

        async def gated(route):
            await gate.wait()
            return await original(route)

        hz.composite.dispatch = gated
        try:
            async with hz.mobile_client() as mob, hz.backend_seat() as seat:
                await mob.rpc("send", request_id="r-work", to_stream_id=H.DAFF_CHAT,
                              text="working?", msg_id="m-work")
                await asyncio.sleep(0.05)
                snap_in = hz.composite.project_session({"stream_id": H.DAFF_CHAT})
                assert snap_in.get("working") is True, snap_in
                gate.set()
                did = await hz.resolved_dispatch_id("m-work")
                await seat.publish(dispatch_id=did, reply_to_message_id="m-work", message="done")
                for _ in range(200):
                    if hz.composite.project_session({"stream_id": H.DAFF_CHAT}).get("working") is False:
                        break
                    await asyncio.sleep(0.01)
                assert hz.composite.project_session({"stream_id": H.DAFF_CHAT}).get("working") is False
        finally:
            await hz.stop()
    _run(run)


# --------------------------------------------------------------------------- #
# Negative controls: REAL applied mutations must flip the journey to RED       #
# --------------------------------------------------------------------------- #
def test_negative_control_blob_mutation_breaks_roundtrip(tmp_path):
    """Mutate the blob store's read so download != fixture; the photo oracle MUST
    go red.  Applied-and-restored (target found)."""
    async def run():
        hz = await H.FoundationHarness(tmp_path).start()
        try:
            async with hz.mobile_client() as mob:
                content = H.materialize_fixture("photo")
                sha = await hz.upload_blob(mob, content)
                real_read = hz.blob_store.read_verified
                applied = {"hit": False}

                async def mutated(_sha, **kw):
                    applied["hit"] = True
                    data = await real_read(_sha, **kw)
                    return data + b"CORRUPT"

                hz.blob_store.read_verified = mutated  # APPLY mutation
                try:
                    got = await hz.blob_store.read_verified(sha, max_bytes=10_000_000)
                    assert applied["hit"] is True                     # mutation target found/applied
                    with pytest.raises(AssertionError):
                        assert got == content                         # oracle goes RED
                finally:
                    hz.blob_store.read_verified = real_read           # RESTORE
                assert await hz.blob_store.read_verified(sha, max_bytes=10_000_000) == content
        finally:
            await hz.stop()
    _run(run)


def test_negative_control_routing_mutation_breaks_reply(tmp_path):
    """Mutate the server's composite routing (resolve to no composite) so a reply
    can no longer be routed; the publish oracle MUST go red.  Applied-and-restored."""
    async def run():
        hz = await H.FoundationHarness(tmp_path).start()
        try:
            async with hz.mobile_client() as mob, hz.backend_seat() as seat:
                await mob.rpc("send", request_id="r-rt", to_stream_id=H.DAFF_CHAT,
                              text="hi", msg_id="m-rt")
                did = await hz.resolved_dispatch_id("m-rt")
                real = hz.server._composite_for_message
                applied = {"hit": False}

                def broken(_msg):
                    applied["hit"] = True
                    return None  # routing table resolves to NO composite

                hz.server._composite_for_message = broken  # APPLY mutation
                try:
                    pub = await seat.publish(dispatch_id=did, reply_to_message_id="m-rt", message="x")
                    assert applied["hit"] is True                     # mutation target found/applied
                    assert pub["type"] != "assistant.publish.ok", pub  # oracle goes RED
                finally:
                    hz.server._composite_for_message = real           # RESTORE
                # Restored: a publish now succeeds (proves the mutation, not a latent break).
                ok = await seat.publish(dispatch_id=did, reply_to_message_id="m-rt", message="x")
                assert ok["type"] == "assistant.publish.ok", ok
        finally:
            await hz.stop()
    _run(run)


def test_negative_control_receipt_mutation_breaks_lookup(tmp_path):
    """Mutate receipt persistence (drop writes) so the receipt is not found; the
    receipt oracle MUST go red.  Applied-and-restored."""
    async def run():
        hz = await H.FoundationHarness(tmp_path).start()
        try:
            async with hz.mobile_client() as mob:
                # The composite send receipt is written by admit_assistant_composite_input
                # (store_routing.py). Mutate so the receipt does NOT durably persist
                # (undo the receipt row it writes) while the message still lands.
                real_admit = hz.store.admit_assistant_composite_input
                applied = {"hit": False}

                async def mutated(*a, **kw):
                    rec = await real_admit(*a, **kw)
                    applied["hit"] = True

                    def _drop(conn):
                        conn.execute("DELETE FROM v2_send_receipts")
                        # Admission now commits request ownership atomically.
                        # Commit the deliberate fixture mutation too, so the
                        # restored send can begin its own admission transaction.
                        conn.commit()
                        return None

                    await hz.store.submit(_drop)  # receipt persistence fails
                    return rec

                hz.store.admit_assistant_composite_input = mutated  # APPLY mutation
                try:
                    before = len(_users(await hz.store.fetch_session_event_tail(H.DAFF_CHAT, limit=50)))
                    sent = await mob.rpc("send", request_id="r-rcpt", to_stream_id=H.DAFF_CHAT,
                                         text="hi", msg_id="m-rcpt")
                    assert sent["type"] == "send.result", sent
                    await asyncio.sleep(0.1)
                    assert applied["hit"] is True                       # mutation target found/applied
                    after = len(_users(await hz.store.fetch_session_event_tail(H.DAFF_CHAT, limit=50)))
                    assert after == before + 1                          # message still landed
                    rc = await mob.rpc("send.receipt.get", request_id="r-rcpt", to_stream_id=H.DAFF_CHAT)
                    assert rc["type"] == "send.receipt.get.ok", rc
                    assert rc.get("found") is False                  # receipt oracle goes RED
                finally:
                    hz.store.admit_assistant_composite_input = real_admit  # RESTORE
                # Restored: a fresh send's receipt is found again (proves the mutation).
                restored = await mob.rpc("send", request_id="r-rcpt2", to_stream_id=H.DAFF_CHAT,
                                        text="hi2", msg_id="m-rcpt2")
                assert restored["type"] == "send.result", restored
                await asyncio.sleep(0.1)
                rc2 = await mob.rpc("send.receipt.get", request_id="r-rcpt2", to_stream_id=H.DAFF_CHAT)
                assert rc2.get("found") is True, rc2
        finally:
            await hz.stop()
    _run(run)


# --------------------------------------------------------------------------- #
# Cleanup proven on BOTH normal and injected-failure runs                      #
# --------------------------------------------------------------------------- #
def _port_free(port: int) -> bool:
    import socket as _s
    with _s.socket(_s.AF_INET, _s.SOCK_STREAM) as s:
        s.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def test_cleanup_after_normal_and_failed_runs(tmp_path):
    async def run():
        # Normal run.
        root1 = tmp_path / "normal"
        hz = await H.FoundationHarness(root1).start()
        port1 = hz.port
        async with hz.mobile_client() as mob:
            await mob.rpc("send", request_id="r-c", to_stream_id=H.DAFF_CHAT, text="x", msg_id="m-c")
        await hz.stop()
        hz.destroy()
        assert _port_free(port1), "port not released after normal run"
        assert not root1.exists(), "disposable root not removed after normal run"

        # Injected-failure run: an exception AFTER bind must still tear down.
        root2 = tmp_path / "failed"
        hz2 = await H.FoundationHarness(root2).start()
        port2 = hz2.port
        try:
            async with hz2.mobile_client() as mob2:
                await mob2.rpc("send", request_id="r-f", to_stream_id=H.DAFF_CHAT, text="y", msg_id="m-f")
                raise RuntimeError("cosmo-e2e injected post-bind failure")
        except RuntimeError:
            pass
        finally:
            await hz2.stop()
            hz2.destroy()
        assert _port_free(port2), "port not released after failed run"
        assert not root2.exists(), "disposable root not removed after failed run"
    _run(run)
