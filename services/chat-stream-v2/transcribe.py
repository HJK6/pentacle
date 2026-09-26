"""Daemon-side transcription verb: turn an uploaded audio blob into text.

`transcribe_blob {request_id, blob_sha, mime}` reads a completed blob from the
content-addressed blob store and forwards it to the managed mic backend's
configured loopback `POST /transcribe` route, always with
`prompt_profile=fleet` so the fleet vocabulary is applied. The daemon never
loads a model itself; it is a thin, authenticated, idempotent proxy.

Contract (spec_pentacle_mobile__voice_input_thoth_transcription_2026_09
§ Shared contract item 2):

- Reply `transcribe_blob.ok {text, duration_s, model, vocabulary_version}`.
- Errors `transcribe_blob.error {error_code}` where error_code is one of
  ``blob_unknown``, ``mime_unsupported``, ``backend_unavailable``,
  ``transcribe_failed``, ``too_long``.
- Idempotent on ``request_id`` and content-addressed on ``blob_sha``: because a
  retried take re-uploads identical bytes (same sha), results are cached by
  ``blob_sha`` for 10 minutes and concurrent identical calls are coalesced, so a
  retry never produces a second transcript.
- ``backend_unavailable`` when the mic backend is off (``MIC_API`` unset-to-empty
  i.e. ``features.mic`` off) or :7780 is unreachable — never an in-daemon load.

The mic backend URL is resolved from the ``MIC_API`` environment variable,
defaulting to the managed loopback backend. Setting ``MIC_API=`` (empty) is the
explicit "feature off" state and yields ``backend_unavailable``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from sessions import VerbError

DEFAULT_MIC_API = "http://127.0.0.1:7780"
PROMPT_PROFILE = "fleet"
CACHE_TTL_S = 600.0  # 10 minutes, content-addressed by blob_sha
#: Route cap mirror (spec default 10 min / 16 MiB). Recorded takes are far under
#: this (M4A ~240 KB/min), so anything larger is treated as too_long.
MAX_BLOB_BYTES = 16 * 1024 * 1024
BACKEND_TIMEOUT_S = 120.0

#: The verb accepts the two container types the mock recorder and WAV fixtures
#: produce; faster-whisper / the MLX adapter decode M4A (audio/mp4) directly.
SUPPORTED_MIME = {"audio/mp4", "audio/wav"}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def resolve_mic_api() -> str | None:
    """Resolve the mic backend base URL from the environment.

    Returns ``None`` when the feature is explicitly off (``MIC_API`` set to an
    empty value), which the caller surfaces as ``backend_unavailable``.
    """
    if "MIC_API" not in os.environ:
        return DEFAULT_MIC_API
    value = os.environ["MIC_API"].strip()
    return value.rstrip("/") if value else None


def _urllib_post(url: str, *, data: bytes, content_type: str, timeout: float) -> tuple[int, bytes]:
    """Blocking loopback POST. Runs in a thread; never on the event loop.

    Returns ``(status, body)``. A transport-level failure (connection refused,
    DNS, timeout) raises ``ConnectionError`` so the caller maps it to
    ``backend_unavailable``; an HTTP error status is returned as data so the
    caller can map 4xx/5xx to the right verb error.
    """
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": content_type, "Content-Length": str(len(data))},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - loopback only
            return int(resp.getcode() or 0), resp.read()
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except Exception:  # noqa: BLE001 - best effort error body
            body = b""
        return int(exc.code), body
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ConnectionError(str(exc)) from exc


class Transcriber:
    """Idempotent, content-addressed proxy to the managed mic backend route."""

    def __init__(
        self,
        blob_store: Any,
        *,
        mic_api: Callable[[], str | None] | str | None = resolve_mic_api,
        http_post: Callable[..., tuple[int, bytes]] = _urllib_post,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._blobs = blob_store
        self._mic_api = mic_api
        self._http_post = http_post
        self._clock = clock
        # blob_sha -> (expiry_monotonic, result_dict)
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        # blob_sha -> lock, so concurrent identical requests coalesce onto one call
        self._locks: dict[str, asyncio.Lock] = {}
        # Correlation identities cannot be rebound to a different take during the
        # cache window, including after an error. Set before the first await.
        self._requests: dict[str, tuple[float, str, str, dict[str, Any] | None]] = {}

    def _resolve_mic_api(self) -> str | None:
        api = self._mic_api
        return api() if callable(api) else api

    def _lock_for(self, blob_sha: str) -> asyncio.Lock:
        lock = self._locks.get(blob_sha)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[blob_sha] = lock
        return lock

    def _cached(self, blob_sha: str) -> dict[str, Any] | None:
        entry = self._cache.get(blob_sha)
        if entry is None:
            return None
        expiry, result = entry
        if self._clock() >= expiry:
            self._cache.pop(blob_sha, None)
            return None
        return result

    async def transcribe_blob(self, *, request_id: str, blob_sha: str, mime: str) -> dict[str, Any]:
        request_id = str(request_id or "").strip()
        if not request_id:
            raise VerbError("bad_request", "request_id is required")
        blob_sha = str(blob_sha or "").strip().lower()
        if not _SHA256_RE.match(blob_sha):
            raise VerbError("bad_request", "blob_sha must be a sha256 hex digest")
        mime = str(mime or "").strip().lower()
        if mime not in SUPPORTED_MIME:
            raise VerbError("mime_unsupported", f"unsupported audio mime: {mime or '<empty>'}")

        now = self._clock()
        self._requests = {key: value for key, value in self._requests.items() if value[0] > now}
        bound = self._requests.get(request_id)
        if bound is not None and bound[1:3] != (blob_sha, mime):
            raise VerbError("bad_request", "request_id is already bound to another audio take")
        if bound is None:
            self._requests[request_id] = (now + CACHE_TTL_S, blob_sha, mime, None)
        elif bound[3] is not None:
            return dict(bound[3])

        # Content-addressed short-circuit: a retry re-uploads identical bytes.
        cached = self._cached(blob_sha)
        if cached is not None:
            return self._remember_result(request_id, blob_sha, mime, cached)

        lock = self._lock_for(blob_sha)
        async with lock:
            # Re-check under the lock: a coalesced concurrent caller may have
            # just populated the cache while we waited.
            cached = self._cached(blob_sha)
            if cached is not None:
                return self._remember_result(request_id, blob_sha, mime, cached)

            data = await self._read_blob(blob_sha)
            result = await self._call_backend(data=data, mime=mime)
            self._cache[blob_sha] = (self._clock() + CACHE_TTL_S, result)
            return self._remember_result(request_id, blob_sha, mime, result)

    def _remember_result(self, request_id: str, blob_sha: str, mime: str, result: dict[str, Any]) -> dict[str, Any]:
        # A newer request that hits an older SHA cache still owns a full result
        # window; its replay must not re-infer when the SHA entry expires first.
        self._requests[request_id] = (self._clock() + CACHE_TTL_S, blob_sha, mime, dict(result))
        return dict(result)

    async def _read_blob(self, blob_sha: str) -> bytes:
        try:
            return await self._blobs.read_verified(blob_sha, max_bytes=MAX_BLOB_BYTES)
        except KeyError as exc:
            raise VerbError("blob_unknown", "blob was not uploaded") from exc
        except ValueError as exc:
            reason = str(exc)
            if reason == "blob_too_large":
                raise VerbError("too_long", "audio blob exceeds the transcription cap") from exc
            if reason == "blob_unknown":
                raise VerbError("blob_unknown", "blob was not uploaded") from exc
            # blob_corrupt or any other integrity failure
            raise VerbError("transcribe_failed", f"blob integrity error: {reason}") from exc

    async def _call_backend(self, *, data: bytes, mime: str) -> dict[str, Any]:
        base = self._resolve_mic_api()
        if not base:
            raise VerbError("backend_unavailable", "mic backend is disabled (features.mic off)")
        url = f"{base}/transcribe?prompt_profile={PROMPT_PROFILE}"
        try:
            status, body = await asyncio.to_thread(
                self._http_post, url, data=data, content_type=mime, timeout=BACKEND_TIMEOUT_S,
            )
        except ConnectionError as exc:
            raise VerbError("backend_unavailable", f"mic backend unreachable: {exc}") from exc

        if status == 503:
            raise VerbError("backend_unavailable", "mic backend model not loaded")
        if status == 413:
            raise VerbError("too_long", "audio exceeds the backend cap")
        if status == 400:
            raise VerbError("transcribe_failed", "backend could not decode the audio")
        if status != 200:
            raise VerbError("transcribe_failed", f"backend returned status {status}")

        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise VerbError("transcribe_failed", "backend response was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise VerbError("transcribe_failed", "backend response was not an object")

        # Only the fields the verb contract echoes; the fleet profile was applied
        # backend-side and its identity travels as vocabulary_version so the
        # integrated test can assert it.
        return {
            "text": str(payload.get("text") or ""),
            "duration_s": _as_float(payload.get("duration_s")),
            "model": str(payload.get("model") or ""),
            "vocabulary_version": str(payload.get("vocabulary_version") or ""),
        }


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
