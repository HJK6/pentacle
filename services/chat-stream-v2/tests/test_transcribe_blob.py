"""`transcribe_blob` verb: content-addressed, idempotent proxy to the managed
mic backend route.

Covers the spec § Validation cells: unknown blob, unsupported mime, backend
down, idempotent retry / cached sha, plus the fleet-profile assertion (the URL
always carries ``prompt_profile=fleet`` and the backend's ``vocabulary_version``
is echoed), backend status mapping (503/413/400), concurrent coalescing, the
feature-off state, and the too-large blob.

The Transcriber is exercised directly against a real BlobStore with a stub HTTP
poster, so no network or model is required (harness invariance: mock the
backend, never the subject).
"""

from __future__ import annotations

import asyncio
import base64
import itertools
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import pytest

from blobs import BlobStore, Promptless
from sessions import VerbError
from transcribe import Transcriber


_rid = itertools.count()


async def _store(tmp: Path) -> BlobStore:
    store = BlobStore(str(tmp / "blobs"))
    await store.start()
    return store


_CHUNK = 512 * 1024  # under the 1 MiB per-chunk cap


async def _put_blob(store: BlobStore, content: bytes) -> str:
    """Store bytes via the real upload verbs, chunked under the per-chunk cap."""
    rid = f"put-{next(_rid)}"
    conn = object()
    init = await store._on_init(
        {"request_id": rid, "size_hint_bytes": len(content), "_client_websocket": conn},
        Promptless,
    )
    assert init["type"] == "upload_blob.init.ok"
    offsets = list(range(0, max(len(content), 1), _CHUNK)) or [0]
    ok = None
    for i, off in enumerate(offsets):
        slice_ = content[off:off + _CHUNK]
        final = i == len(offsets) - 1
        ok = await store._on_chunk(
            {"request_id": rid, "data_b64": base64.b64encode(slice_).decode(), "final": final},
            Promptless,
        )
        if not final:
            assert ok == [] or ok.get("type") != "upload_blob.error", ok
    assert ok is not None and ok["type"] == "upload_blob.ok", ok
    return ok["blob_sha"]


class _StubPoster:
    """Records calls and returns a scripted (status, body) tuple; can raise."""

    def __init__(self, status: int = 200, body: bytes = b"{}", *, raise_conn: bool = False):
        self.status = status
        self.body = body
        self.raise_conn = raise_conn
        self.calls: list[dict] = []

    def __call__(self, url, *, data, content_type, timeout):
        self.calls.append({"url": url, "data": data, "content_type": content_type})
        if self.raise_conn:
            raise ConnectionError("connection refused")
        return self.status, self.body


def _ok_body(text="hello Juniper", vocab="fleet-v3", model="large-v3", duration=1.5) -> bytes:
    import json
    return json.dumps(
        {
            "text": text,
            "language": "en",
            "duration_s": duration,
            "model": model,
            "compute": "float16",
            "segments": [{"start": 0.0, "end": duration, "text": text}],
            "vocabulary_version": vocab,
        }
    ).encode()


def test_happy_path_applies_fleet_profile_and_echoes_fields(tmp_path: Path) -> None:
    async def run() -> None:
        store = await _store(tmp_path)
        sha = await _put_blob(store, b"m4a-bytes-here")
        poster = _StubPoster(200, _ok_body(text="Hey Juniper", vocab="fleet-abc123"))
        t = Transcriber(store, mic_api="http://127.0.0.1:7780", http_post=poster)

        result = await t.transcribe_blob(request_id="r1", blob_sha=sha, mime="audio/mp4")

        assert result == {
            "text": "Hey Juniper",
            "duration_s": 1.5,
            "model": "large-v3",
            "vocabulary_version": "fleet-abc123",
        }
        # The fleet profile is always applied (no client-selectable profile).
        assert len(poster.calls) == 1
        parsed = urlparse(poster.calls[0]["url"])
        assert parsed.path == "/transcribe"
        assert parse_qs(parsed.query)["prompt_profile"] == ["fleet"]
        assert poster.calls[0]["content_type"] == "audio/mp4"
        assert poster.calls[0]["data"] == b"m4a-bytes-here"

    asyncio.run(run())


def test_unknown_blob_is_blob_unknown(tmp_path: Path) -> None:
    async def run() -> None:
        store = await _store(tmp_path)
        poster = _StubPoster()
        t = Transcriber(store, mic_api="http://127.0.0.1:7780", http_post=poster)
        missing = "0" * 64
        with pytest.raises(VerbError) as exc:
            await t.transcribe_blob(request_id="r1", blob_sha=missing, mime="audio/mp4")
        assert exc.value.code == "blob_unknown"
        assert poster.calls == []  # never reached the backend

    asyncio.run(run())


def test_unsupported_mime_is_mime_unsupported(tmp_path: Path) -> None:
    async def run() -> None:
        store = await _store(tmp_path)
        sha = await _put_blob(store, b"bytes")
        poster = _StubPoster()
        t = Transcriber(store, mic_api="http://127.0.0.1:7780", http_post=poster)
        with pytest.raises(VerbError) as exc:
            await t.transcribe_blob(request_id="r1", blob_sha=sha, mime="audio/ogg")
        assert exc.value.code == "mime_unsupported"
        assert poster.calls == []

    asyncio.run(run())


def test_backend_down_is_backend_unavailable(tmp_path: Path) -> None:
    async def run() -> None:
        store = await _store(tmp_path)
        sha = await _put_blob(store, b"bytes")
        poster = _StubPoster(raise_conn=True)
        t = Transcriber(store, mic_api="http://127.0.0.1:7780", http_post=poster)
        with pytest.raises(VerbError) as exc:
            await t.transcribe_blob(request_id="r1", blob_sha=sha, mime="audio/wav")
        assert exc.value.code == "backend_unavailable"

    asyncio.run(run())


def test_feature_off_is_backend_unavailable(tmp_path: Path) -> None:
    """MIC_API resolved to empty (features.mic off) never reaches the network."""

    async def run() -> None:
        store = await _store(tmp_path)
        sha = await _put_blob(store, b"bytes")
        poster = _StubPoster()
        t = Transcriber(store, mic_api=None, http_post=poster)  # feature off
        with pytest.raises(VerbError) as exc:
            await t.transcribe_blob(request_id="r1", blob_sha=sha, mime="audio/mp4")
        assert exc.value.code == "backend_unavailable"
        assert poster.calls == []

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,code",
    [(503, "backend_unavailable"), (413, "too_long"), (400, "transcribe_failed"), (500, "transcribe_failed")],
)
def test_backend_status_mapping(tmp_path: Path, status: int, code: str) -> None:
    async def run() -> None:
        store = await _store(tmp_path)
        sha = await _put_blob(store, b"bytes")
        poster = _StubPoster(status, b"backend error")
        t = Transcriber(store, mic_api="http://127.0.0.1:7780", http_post=poster)
        with pytest.raises(VerbError) as exc:
            await t.transcribe_blob(request_id="r1", blob_sha=sha, mime="audio/mp4")
        assert exc.value.code == code

    asyncio.run(run())


def test_idempotent_cached_by_sha(tmp_path: Path) -> None:
    """A retry (new request_id, same audio → same sha) hits the cache; the
    backend is called exactly once, so no second transcript is produced."""

    async def run() -> None:
        store = await _store(tmp_path)
        sha = await _put_blob(store, b"same-audio")
        poster = _StubPoster(200, _ok_body(text="cached text"))
        t = Transcriber(store, mic_api="http://127.0.0.1:7780", http_post=poster)

        first = await t.transcribe_blob(request_id="r1", blob_sha=sha, mime="audio/mp4")
        second = await t.transcribe_blob(request_id="r2-retry", blob_sha=sha, mime="audio/mp4")

        assert first == second
        assert len(poster.calls) == 1  # content-addressed cache hit on retry

    asyncio.run(run())


def test_cache_expires(tmp_path: Path) -> None:
    async def run() -> None:
        store = await _store(tmp_path)
        sha = await _put_blob(store, b"expiring")
        poster = _StubPoster(200, _ok_body())
        now = [1000.0]
        t = Transcriber(store, mic_api="http://127.0.0.1:7780", http_post=poster, clock=lambda: now[0])
        await t.transcribe_blob(request_id="r1", blob_sha=sha, mime="audio/mp4")
        now[0] += 601.0  # past the 10-minute TTL
        await t.transcribe_blob(request_id="r2", blob_sha=sha, mime="audio/mp4")
        assert len(poster.calls) == 2  # re-fetched after expiry

    asyncio.run(run())


def test_concurrent_identical_calls_coalesce(tmp_path: Path) -> None:
    async def run() -> None:
        store = await _store(tmp_path)
        sha = await _put_blob(store, b"concurrent")

        started = asyncio.Event()
        release = asyncio.Event()
        calls = {"n": 0}

        def slow_poster(url, *, data, content_type, timeout):
            calls["n"] += 1
            return 200, _ok_body(text="one")

        # Wrap with an async gate so both callers enter transcribe_blob before
        # either backend call completes.
        real = Transcriber(store, mic_api="http://127.0.0.1:7780", http_post=slow_poster)
        orig_call = real._call_backend

        async def gated_call(*, data, mime):
            started.set()
            await release.wait()
            return await orig_call(data=data, mime=mime)

        real._call_backend = gated_call  # type: ignore[assignment]

        t1 = asyncio.create_task(real.transcribe_blob(request_id="a", blob_sha=sha, mime="audio/mp4"))
        t2 = asyncio.create_task(real.transcribe_blob(request_id="b", blob_sha=sha, mime="audio/mp4"))
        await started.wait()
        await asyncio.sleep(0)  # let the second task queue on the sha lock
        release.set()
        r1, r2 = await asyncio.gather(t1, t2)

        assert r1 == r2
        assert calls["n"] == 1  # coalesced onto one backend inference

    asyncio.run(run())


def test_too_large_blob_is_too_long(tmp_path: Path) -> None:
    async def run() -> None:
        import transcribe
        store = await _store(tmp_path)
        big = b"x" * (transcribe.MAX_BLOB_BYTES + 1)
        sha = await _put_blob(store, big)
        poster = _StubPoster()
        t = Transcriber(store, mic_api="http://127.0.0.1:7780", http_post=poster)
        with pytest.raises(VerbError) as exc:
            await t.transcribe_blob(request_id="r1", blob_sha=sha, mime="audio/mp4")
        assert exc.value.code == "too_long"
        assert poster.calls == []

    asyncio.run(run())


def test_missing_request_id_is_bad_request(tmp_path: Path) -> None:
    async def run() -> None:
        store = await _store(tmp_path)
        sha = await _put_blob(store, b"bytes")
        t = Transcriber(store, mic_api="http://127.0.0.1:7780", http_post=_StubPoster())
        with pytest.raises(VerbError) as exc:
            await t.transcribe_blob(request_id="", blob_sha=sha, mime="audio/mp4")
        assert exc.value.code == "bad_request"

    asyncio.run(run())


def test_request_identity_cannot_be_rebound_to_another_take(tmp_path):
    async def run():
        store = await _store(tmp_path)
        first = await _put_blob(store, b'first take')
        second = await _put_blob(store, b'second take')
        poster = _StubPoster(body=_ok_body())
        subject = Transcriber(store, http_post=poster)
        await subject.transcribe_blob(request_id='bound-take', blob_sha=first, mime='audio/mp4')
        with pytest.raises(VerbError) as error:
            await subject.transcribe_blob(request_id='bound-take', blob_sha=second, mime='audio/mp4')
        assert error.value.code == 'bad_request'
        assert len(poster.calls) == 1
    asyncio.run(run())


def test_request_result_survives_older_sha_cache_expiry(tmp_path):
    async def run():
        store = await _store(tmp_path)
        sha = await _put_blob(store, b'the same take')
        poster = _StubPoster(body=_ok_body())
        now = [0.0]
        subject = Transcriber(store, http_post=poster, clock=lambda: now[0])
        await subject.transcribe_blob(request_id='first', blob_sha=sha, mime='audio/mp4')
        now[0] = 599.0
        await subject.transcribe_blob(request_id='second', blob_sha=sha, mime='audio/mp4')
        now[0] = 601.0
        await subject.transcribe_blob(request_id='second', blob_sha=sha, mime='audio/mp4')
        assert len(poster.calls) == 1
    asyncio.run(run())
