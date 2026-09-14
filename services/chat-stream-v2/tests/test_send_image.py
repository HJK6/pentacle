"""Contract coverage for the agent → operator self-image send path.

An authenticated seat attaches an already-uploaded image blob to its OWN
conversation as an agent-authored (ASSIST) transcript event, with no pane
injection, genuine receipts, and exactly-once semantics on retry.
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from comms import Comms
from sessions import Sessions, VerbError
from spawnctl import SpawnCtl
from store import Store
from server import Server, RECENT_LIMIT


HOST = "localhost"
NAME = "img-seat"
STREAM = f"{HOST}:{NAME}"
PNG = b"\x89PNG\r\n\x1a\nfixture-png-bytes"
SHA = hashlib.sha256(PNG).hexdigest()
ABSENT_SHA = hashlib.sha256(b"never-uploaded").hexdigest()


class FakeTmux:
    """Records any pane I/O so a test can prove the image path never pastes."""

    def __init__(self) -> None:
        self.pastes: list[str] = []
        self.screen = "❯ \n"

    async def capture(self, _name: str) -> str:
        return self.screen

    async def paste(self, _name: str, text: str) -> None:
        self.pastes.append(text)

    async def send_enter(self, _name: str) -> None:
        self.pastes.append("<enter>")

    async def run(self, *_args: str, **_kwargs) -> tuple[int, str]:
        return 0, ""


class FakeBlobStore:
    def __init__(self, blobs: dict[str, bytes] | None = None) -> None:
        self.blobs = blobs if blobs is not None else {SHA: PNG}
        self.reads: list[str] = []

    async def read_verified(self, sha: str, *, max_bytes: int) -> bytes:
        self.reads.append(sha)
        data = self.blobs[sha]  # KeyError => not uploaded
        if len(data) > max_bytes:
            raise ValueError("blob exceeds attachment limit")
        return data


def _new_comms(tmux: FakeTmux, *, blob_store: FakeBlobStore | None = None):
    store = Store(":memory:")
    store.start()
    sessions = Sessions(store, tmux=tmux, local_host=HOST)
    comms = Comms(
        store,
        sessions,
        SpawnCtl(store, sessions, tmux=tmux),
        hosts=None,
        blob_store=blob_store or FakeBlobStore(),
    )
    return comms, store, sessions


def _collector():
    frames: list[dict] = []

    async def broadcast(frame: dict) -> None:
        frames.append(frame)

    return frames, broadcast


def _att(mime: str = "image/png", key: str = SHA) -> dict:
    return {"key": key, "mime": mime, "bytes": len(PNG)}


def _run(coro):
    return asyncio.run(coro)


async def _open(sessions: Sessions) -> None:
    await sessions.open(HOST, NAME, provider="claude")


# --- Comms.post_self_image core -------------------------------------------


def test_post_self_image_emits_one_assist_event_no_pane():
    tmux = FakeTmux()
    comms, _store, sessions = _new_comms(tmux)
    frames, broadcast = _collector()

    async def go():
        await _open(sessions)
        return await comms.post_self_image(
            stream_id=STREAM,
            raw_attachments=[_att()],
            caption="here is the chart",
            request_id="req-1",
            broadcast=broadcast,
            recent_limit=RECENT_LIMIT,
        )

    resp = _run(go())
    assert resp["type"] == "send_image.ok"
    assert resp["stream_id"] == STREAM
    assert resp["blob_shas"] == [SHA]
    assert isinstance(resp["event_id"], int) and resp["event_id"] > 0
    assert resp["duplicate"] is False
    # exactly one chat.event, agent-authored, carrying the attachment
    chat_events = [f for f in frames if f.get("type") == "chat.event"]
    assert len(chat_events) == 1
    event = chat_events[0]["event"]
    assert event["kind"] == "ASSIST"
    assert event["stream_id"] == STREAM
    assert event["attachments"][0]["key"] == SHA
    assert event["attachments"][0]["mime"] == "image/png"
    assert event["text"] == "here is the chart"
    assert event["daemon_seq"] == resp["event_id"]
    # never touches the pane
    assert tmux.pastes == []


def test_post_self_image_captionless_is_attachment_only():
    tmux = FakeTmux()
    comms, _store, sessions = _new_comms(tmux)
    frames, broadcast = _collector()

    async def go():
        await _open(sessions)
        return await comms.post_self_image(
            stream_id=STREAM, raw_attachments=[_att()], caption="",
            request_id="req-2", broadcast=broadcast, recent_limit=RECENT_LIMIT,
        )

    resp = _run(go())
    assert resp["content_kind"] == "attachment_only"
    event = [f for f in frames if f.get("type") == "chat.event"][0]["event"]
    assert event["text"] == ""
    assert event["attachments"][0]["key"] == SHA


def test_post_self_image_rejects_unsupported_mime():
    tmux = FakeTmux()
    comms, _store, sessions = _new_comms(tmux)
    frames, broadcast = _collector()

    async def go():
        await _open(sessions)
        await comms.post_self_image(
            stream_id=STREAM, raw_attachments=[_att(mime="image/gif")], caption="x",
            request_id="req-3", broadcast=broadcast, recent_limit=RECENT_LIMIT,
        )

    with pytest.raises(VerbError) as exc:
        _run(go())
    assert exc.value.code == "attachment_invalid"
    assert not [f for f in frames if f.get("type") == "chat.event"]


def test_post_self_image_rejects_missing_blob():
    tmux = FakeTmux()
    comms, _store, sessions = _new_comms(tmux)
    frames, broadcast = _collector()

    async def go():
        await _open(sessions)
        await comms.post_self_image(
            stream_id=STREAM, raw_attachments=[_att(key=ABSENT_SHA)], caption="x",
            request_id="req-4", broadcast=broadcast, recent_limit=RECENT_LIMIT,
        )

    with pytest.raises(VerbError) as exc:
        _run(go())
    assert exc.value.code in {"attachment_missing", "attachment_fetch_failed"}
    assert not [f for f in frames if f.get("type") == "chat.event"]


def test_post_self_image_rejects_empty_attachments():
    tmux = FakeTmux()
    comms, _store, sessions = _new_comms(tmux)
    frames, broadcast = _collector()

    async def go():
        await _open(sessions)
        await comms.post_self_image(
            stream_id=STREAM, raw_attachments=[], caption="just text",
            request_id="req-5", broadcast=broadcast, recent_limit=RECENT_LIMIT,
        )

    with pytest.raises(VerbError) as exc:
        _run(go())
    assert exc.value.code == "attachment_invalid"


def test_post_self_image_is_idempotent_by_request_id():
    tmux = FakeTmux()
    comms, _store, sessions = _new_comms(tmux)
    frames, broadcast = _collector()

    async def go():
        await _open(sessions)
        first = await comms.post_self_image(
            stream_id=STREAM, raw_attachments=[_att()], caption="c",
            request_id="dup-req", broadcast=broadcast, recent_limit=RECENT_LIMIT,
        )
        second = await comms.post_self_image(
            stream_id=STREAM, raw_attachments=[_att()], caption="c",
            request_id="dup-req", broadcast=broadcast, recent_limit=RECENT_LIMIT,
        )
        return first, second

    first, second = _run(go())
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    # retry must not add a second transcript row
    chat_events = [f for f in frames if f.get("type") == "chat.event"]
    assert len(chat_events) == 1


# --- server _on_send_image auth gate --------------------------------------


def _server(comms, store, sessions):
    server = Server(
        store=store, sessions=sessions,
        spawnctl=SpawnCtl(store, sessions, tmux=FakeTmux()),
        comms=comms, local_host=HOST,
    )
    frames, broadcast = _collector()
    server.broadcast = broadcast  # type: ignore[assignment]
    return server, frames


def test_handler_rejects_unverified_seat():
    tmux = FakeTmux()
    comms, store, sessions = _new_comms(tmux)
    server, frames = _server(comms, store, sessions)

    async def go():
        await _open(sessions)
        return await server._on_send_image({
            "type": "send_image", "request_id": "r", "attachments": [_att()],
            "_auth_context": {"token_verified": False, "stream_id": ""},
        })

    # The handler raises; the server's _dispatch turns this into a
    # send_image.error frame (server.py exception boundary). Either way, no
    # transcript event is emitted for an unverified caller.
    with pytest.raises(VerbError) as exc:
        _run(go())
    assert exc.value.code == "stream_ownership_unverified"
    assert not [f for f in frames if f.get("type") == "chat.event"]


def test_handler_posts_only_to_verified_stream_ignoring_spoofed_target():
    tmux = FakeTmux()
    comms, store, sessions = _new_comms(tmux)
    server, frames = _server(comms, store, sessions)

    async def go():
        await _open(sessions)
        return await server._on_send_image({
            "type": "send_image", "request_id": "r", "attachments": [_att()],
            "caption": "mine",
            # a spoofed target must be ignored; destination is the verified owner
            "from_stream_id": "localhost:someone-else",
            "_auth_context": {"token_verified": True, "stream_id": STREAM},
        })

    reply = _run(go())
    assert reply["type"] == "send_image.ok"
    event = [f for f in frames if f.get("type") == "chat.event"][0]["event"]
    assert event["stream_id"] == STREAM
