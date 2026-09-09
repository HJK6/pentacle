"""blobs.py - chunked base64 blob transfer (upload + fetch), streamed off-loop.

Contract: v1's blob transport, reimplemented to the exact wire bytes but with
every syscall off the asyncio loop (spec constraint 2). v1 ran base64 decode,
`file.write`, `os.fsync`, `os.replace`, and a whole-blob `read_bytes` +
`b64encode` INLINE on the event loop; a single fsync stall wedged the daemon.
v2 does all of that in the default bounded executor and never holds a whole
blob in loop memory: uploads stream 1 MiB chunks to a temp file, fetches stream
1 MiB slices back out.

Consumers (spike_verbs.md, ~101 rpc/12d each): the `agent-orch` CLI blob
helpers (`upload_blob` / `fetch_blob` in `wsclient.py`) that back `report
--result-blob` and the spawn initial-prompt-blob path. `asset.publish` sends
its body inline and does NOT use this path.

Content addressing (v1 `blob_store.py`, byte-parity): the filename IS the
lowercase sha256 hex; layout is `<root>/<sha[:2]>/<sha>`; uploads land in
`<root>/.tmp/<sanitized request_id>` first, then `os.replace` into place, so a
re-upload of identical bytes dedups (`was_existing`). Dirs 0o700, files 0o600.

Wire (dotting is deliberate and matches v1): request types use underscores
(`upload_blob_init`, `upload_blob_chunk`, `fetch_blob`); reply types use dots
(`upload_blob.init.ok`, `upload_blob.ok`, `fetch_blob.ok`, `fetch_blob.chunk`,
`upload_blob.error`, ...). `request_id` doubles as the upload-session id - there
is no separate token. A non-final chunk is acked with NO frame (fire and
forget); completion is a `final: true` chunk, never a separate commit verb.

Size gates (v1 constants, preserved): total <= 64 MiB, per-chunk <= 1 MiB.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

log = logging.getLogger("chat_streamd_v2.blobs")

TOTAL_MAX_BYTES = 64 * 1024 * 1024
CHUNK_MAX_BYTES = 1 * 1024 * 1024
DEFAULT_BLOB_ROOT = str(Path.home() / ".local/share/pentacle-stream/blobs")

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_-]+")


class _Upload:
    """One in-flight upload's mutable state: the open temp file, the running
    sha, and the running total (the 64 MiB gate is a running check, not just an
    init hint - a client can lie about `size_hint_bytes`)."""

    __slots__ = ("fd", "tmp_path", "hasher", "size", "owner")

    def __init__(self, fd: int, tmp_path: str, owner: Any = None) -> None:
        self.fd = fd
        self.tmp_path = tmp_path
        self.hasher = hashlib.sha256()
        self.size = 0
        #: The connection (websocket) that sent this upload's `upload_blob_init`.
        #: It is the upload's one owning path: when the connection closes, the
        #: server calls `abort_connection` and this partial upload is torn down.
        self.owner = owner


class BlobStore:
    """Content-addressed blob store with the v1 wire protocol.

    Every disk/base64 operation runs through `_run` (the loop's default
    executor) so the event loop only ever touches small JSON envelopes. Chunks
    of one upload are serialized by a per-upload lock: the server dispatches one
    task per WS frame and those tasks run concurrently, so without the lock two
    chunks of the same blob could append out of order and corrupt the sha. The
    lock is acquired before the first `await`, so waiters queue in frame order.
    """

    def __init__(self, root: str = DEFAULT_BLOB_ROOT) -> None:
        self._root = Path(root)
        self._tmp = self._root / ".tmp"
        self._uploads: dict[str, _Upload] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        #: Owning connection -> the request_ids of its in-flight uploads. An
        #: upload is owned by the connection that sent its `upload_blob_init`; a
        #: partial upload's lifetime is bounded by that connection's. When the
        #: connection closes, `abort_connection` tears its uploads down. No
        #: reaper, no deadline timer: the socket close IS the bound.
        self._by_conn: dict[Any, set[str]] = {}
        #: Set once the store dirs exist; a blob verb in the boot window parks on
        #: it rather than writing under a missing root.
        self._ready = asyncio.Event()

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Create the store dirs and wipe stale temp uploads (v1 cleans `.tmp`
        on boot). Off-loop; called after the port binds."""
        await self._run(self._prepare_dirs)
        self._ready.set()

    async def _await_ready(self) -> None:
        if not self._ready.is_set():
            await asyncio.wait_for(self._ready.wait(), timeout=10.0)

    def _prepare_dirs(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._tmp.exists():
            for stale in self._tmp.iterdir():
                try:
                    stale.unlink()
                except OSError:  # pragma: no cover - best-effort cleanup
                    pass
        self._tmp.mkdir(parents=True, exist_ok=True, mode=0o700)

    def wire_handlers(self) -> dict[str, Callable[[dict[str, Any]], Awaitable[Any]]]:
        """The dispatch entries `server.py` merges. Two families share one code
        path, differing only in reply-type prefix + final field name + the
        prompt variant's UTF-8 gate."""
        return {
            "upload_blob_init": lambda m: self._on_init(m, Promptless),
            "upload_blob_chunk": lambda m: self._on_chunk(m, Promptless),
            "upload_prompt_blob_init": lambda m: self._on_init(m, PromptBlob),
            "upload_prompt_blob_chunk": lambda m: self._on_chunk(m, PromptBlob),
            "fetch_blob": self._on_fetch,
        }

    # -- upload ------------------------------------------------------------

    async def _on_init(self, msg: dict[str, Any], flavor: "_Flavor") -> dict[str, Any]:
        await self._await_ready()
        rid = str(msg.get("request_id") or "")
        owner = msg.get("_client_websocket")
        try:
            size_hint = int(msg.get("size_hint_bytes") or 0)
        except (TypeError, ValueError):
            size_hint = 0
        if size_hint > TOTAL_MAX_BYTES:
            return flavor.error(rid, flavor.code_too_large)
        # The per-upload lock is REUSED across an idempotent reset, never
        # replaced, so "one lock per request_id" holds even while a chunk task
        # for a prior attempt is in flight. Reset runs UNDER that lock: a
        # concurrent chunk is serialized against it instead of racing it (an
        # unlocked reset let a stale final chunk complete the wrong attempt and
        # left dangling ownership).
        lock = self._locks.get(rid)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[rid] = lock
        async with lock:
            # Idempotent reset: an init for a request_id that still has a live
            # upload (client resent init) discards the prior partial first so its
            # open fd + temp file cannot leak and a client retry is trivial.
            prior = self._uploads.pop(rid, None)
            if prior is not None:
                self._drop_owner(rid, prior)
                await self._run(self._discard, prior)
            try:
                fd, tmp_path = await self._run(self._open_tmp, rid)
            except OSError:
                self._locks.pop(rid, None)  # no live upload owns this rid now
                return flavor.error(rid, flavor.code_disk_full)
            self._uploads[rid] = _Upload(fd, tmp_path, owner)
            self._by_conn.setdefault(owner, set()).add(rid)
        return {"type": flavor.init_ok, "request_id": rid}

    async def _on_chunk(self, msg: dict[str, Any], flavor: "_Flavor") -> dict[str, Any]:
        rid = str(msg.get("request_id") or "")
        lock = self._locks.get(rid)
        if lock is None:  # no init seen (or already finished/aborted)
            return flavor.error(rid, flavor.code_unknown)
        async with lock:  # acquired before any await => chunks stay in arrival order
            up = self._uploads.get(rid)
            if up is None:
                return flavor.error(rid, flavor.code_unknown)
            final = bool(msg.get("final"))
            data = _b64decode(msg.get("data_b64"))
            if len(data) > CHUNK_MAX_BYTES:
                await self._abort(rid)
                return flavor.error(rid, flavor.code_chunk_oversize)
            if up.size + len(data) > TOTAL_MAX_BYTES:
                await self._abort(rid)
                return flavor.error(rid, flavor.code_too_large)
            try:
                # Joined: if this chunk task is cancelled (connection close), the
                # write finishes before the lock releases, so teardown cannot
                # close this fd mid-write.
                await self._run_joined(self._append, up, data)
            except OSError:
                await self._abort(rid)
                return flavor.error(rid, flavor.code_disk_full)
            if not final:
                # v1 acks non-final chunks with nothing. An empty frame list is
                # sent as zero frames by the dispatcher, so the CLI - which only
                # awaits the final chunk - is undisturbed.
                return []
            if flavor.require_utf8 and not _is_utf8(up):
                await self._abort(rid)
                return flavor.error(rid, flavor.code_invalid_utf8)
            try:
                # Joined for the same reason: fsync/close/replace must finish
                # before a cancelled task releases the lock.
                sha = await self._run_joined(self._finish, up)
            except OSError:
                await self._abort(rid)
                return flavor.error(rid, flavor.code_disk_full)
            size = up.size
            self._uploads.pop(rid, None)
            self._locks.pop(rid, None)
            self._drop_owner(rid, up)
            return {"type": flavor.upload_ok, "request_id": rid,
                    flavor.sha_field: sha, "size_bytes": size}

    async def _abort(self, rid: str) -> None:
        """Discard an upload's state. The caller must already hold the per-upload
        lock (chunk-error paths, `_on_init` reset, `_abort_locked`)."""
        up = self._uploads.pop(rid, None)
        self._locks.pop(rid, None)
        self._drop_owner(rid, up)
        if up is not None:
            await self._run(self._discard, up)

    async def _abort_locked(self, rid: str) -> None:
        """Acquire the per-upload lock, then abort. For callers that do NOT hold
        it, so teardown cannot race an in-flight chunk's `_append`/`_finish`."""
        lock = self._locks.get(rid)
        if lock is None:
            await self._abort(rid)  # nothing live; clear any residue
            return
        async with lock:
            await self._abort(rid)

    def _drop_owner(self, rid: str, up: "_Upload | None") -> None:
        """Forget this upload's connection ownership (completion or abort)."""
        if up is None:
            return
        owned = self._by_conn.get(up.owner)
        if owned is not None:
            owned.discard(rid)
            if not owned:
                self._by_conn.pop(up.owner, None)

    async def abort_connection(self, owner: Any) -> None:
        """Tear down every in-flight upload owned by a now-closed connection.

        The connection that sent `upload_blob_init` owns its uploads; when it
        closes there is no terminal reply to send (the socket is gone), so each
        partial upload — fd, temp file, and per-upload state — is discarded.
        Any later chunk for the same request_id then meets the existing
        `upload_blob_unknown_request_id` reply, which the client acts on. Each
        teardown takes the per-upload lock so it serializes with any chunk task
        still unwinding on that upload."""
        for rid in tuple(self._by_conn.pop(owner, ())):
            await self._abort_locked(rid)

    # -- fetch -------------------------------------------------------------

    async def _on_fetch(self, msg: dict[str, Any]) -> Any:
        await self._await_ready()
        rid = str(msg.get("request_id") or "")
        sha = str(msg.get("blob_sha") or "")
        path = self._path_for(sha)
        size = await self._run(self._size_if_present, path)
        if size is None:
            return {"type": "fetch_blob.error", "request_id": rid, "error_code": "blob_unknown"}
        return self._stream_fetch(rid, sha, path, size)

    async def read_prompt(self, sha: str) -> str:
        """Dereference a completed prompt upload for the spawn path.

        The content-addressed file is read in the blob executor, never on the
        daemon event loop. Validate the digest again at the handoff boundary so
        a missing or damaged upload cannot turn into an empty initial prompt.
        """
        await self._await_ready()
        normalized = str(sha or "").lower()
        if not _SHA_RE.fullmatch(normalized):
            raise ValueError("prompt_blob_unknown")
        path = self._path_for(normalized)
        try:
            data = await self._run(self._read_all, path)
        except OSError as exc:
            raise ValueError("prompt_blob_unknown") from exc
        if hashlib.sha256(data).hexdigest() != normalized:
            raise ValueError("prompt_blob_corrupt")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:  # defensive: upload already gates UTF-8
            raise ValueError("prompt_blob_invalid_utf8") from exc

    async def read_verified(self, sha: str, *, max_bytes: int | None = None) -> bytes:
        """Read one completed blob off-loop and verify its content address."""
        await self._await_ready()
        normalized = str(sha or "").lower()
        if not _SHA_RE.fullmatch(normalized):
            raise ValueError("blob_unknown")
        path = self._path_for(normalized)
        try:
            data = await self._run(self._read_all, path)
        except OSError as exc:
            raise ValueError("blob_unknown") from exc
        if max_bytes is not None and len(data) > max_bytes:
            raise ValueError("blob_too_large")
        if hashlib.sha256(data).hexdigest() != normalized:
            raise ValueError("blob_corrupt")
        return data

    async def _stream_fetch(self, rid: str, sha: str, path: Path, size: int) -> AsyncIterator[dict[str, Any]]:
        """v1 framing, byte-parity: a blob <= 1 MiB is one `fetch_blob.ok` with
        `content_b64` + `final: true`; a larger one is N `fetch_blob.chunk`
        frames (`size_bytes` = the TOTAL, `final: false`) then a terminal
        `fetch_blob.ok` carrying `final: true` and NO body. Each slice is read +
        encoded off-loop, one at a time, so the loop never holds the whole blob."""
        if size <= CHUNK_MAX_BYTES:
            body = await self._run(self._read_slice, path, 0, size)
            yield {"type": "fetch_blob.ok", "request_id": rid, "blob_sha": sha,
                   "size_bytes": size, "content_b64": _b64encode(body), "final": True}
            return
        offset = 0
        while offset < size:
            body = await self._run(self._read_slice, path, offset, CHUNK_MAX_BYTES)
            if not body:
                break
            yield {"type": "fetch_blob.chunk", "request_id": rid, "blob_sha": sha,
                   "size_bytes": size, "content_b64": _b64encode(body), "final": False}
            offset += len(body)
        yield {"type": "fetch_blob.ok", "request_id": rid, "blob_sha": sha,
               "size_bytes": size, "final": True}

    # -- disk (all of this runs in the executor, never on the loop) --------

    def _open_tmp(self, rid: str) -> tuple[int, str]:
        self._tmp.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp_path = str(self._tmp / (_SANITIZE_RE.sub("_", rid) or "blob"))
        fd = os.open(tmp_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        return fd, tmp_path

    @staticmethod
    def _append(up: _Upload, data: bytes) -> None:
        if data:
            os.write(up.fd, data)
            up.hasher.update(data)
            up.size += len(data)

    def _finish(self, up: _Upload) -> str:
        os.fsync(up.fd)
        os.close(up.fd)
        sha = up.hasher.hexdigest()
        dest = self._path_for(sha)
        if dest.exists():  # content-addressed dedup: identical bytes already stored
            os.unlink(up.tmp_path)
            return sha
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.replace(up.tmp_path, dest)
        os.chmod(dest, 0o600)
        return sha

    @staticmethod
    def _discard(up: _Upload) -> None:
        try:
            os.close(up.fd)
        except OSError:
            pass
        try:
            os.unlink(up.tmp_path)
        except OSError:
            pass

    @staticmethod
    def _size_if_present(path: Path) -> int | None:
        try:
            return path.stat().st_size
        except OSError:
            return None

    @staticmethod
    def _read_slice(path: Path, offset: int, length: int) -> bytes:
        with open(path, "rb") as fh:
            fh.seek(offset)
            return fh.read(length)

    @staticmethod
    def _read_all(path: Path) -> bytes:
        with open(path, "rb") as fh:
            return fh.read()

    def _path_for(self, sha: str) -> Path:
        sha = sha.lower()
        if not _SHA_RE.match(sha):
            return self._root / "__invalid__" / (sha or "empty")
        return self._root / sha[:2] / sha

    @staticmethod
    async def _run(fn: Callable[..., Any], *a: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(None, fn, *a)

    @staticmethod
    async def _run_joined(fn: Callable[..., Any], *a: Any) -> Any:
        """Run a disk op that must COMPLETE even if the awaiting task is cancelled.

        A `run_in_executor` worker cannot be cancelled once it is running; a bare
        `await self._run(...)` lets the cancelled coroutine unwind — releasing the
        per-upload lock — while the worker is still mid-write. Teardown could then
        take that lock and `os.close` the fd out from under an in-flight `_append`,
        landing bytes in whatever reuses the fd. Shield the future and JOIN it
        before re-raising, so the lock is held until the write actually finishes.
        """
        fut = asyncio.ensure_future(BlobStore._run(fn, *a))
        try:
            return await asyncio.shield(fut)
        except asyncio.CancelledError:
            with contextlib.suppress(BaseException):
                await fut  # let the in-flight disk op finish before we unwind
            raise


class _Flavor:
    """The two upload families are one code path; only these strings differ."""

    init_ok: str
    upload_ok: str
    error_type: str
    sha_field: str
    require_utf8: bool
    code_too_large: str
    code_unknown: str
    code_chunk_oversize: str
    code_disk_full: str
    code_invalid_utf8: str

    @classmethod
    def error(cls, rid: str, code: str) -> dict[str, Any]:
        return {"type": cls.error_type, "request_id": rid, "error_code": code}


class Promptless(_Flavor):
    init_ok = "upload_blob.init.ok"
    upload_ok = "upload_blob.ok"
    error_type = "upload_blob.error"
    sha_field = "blob_sha"
    require_utf8 = False
    code_too_large = "upload_blob_too_large"
    code_unknown = "upload_blob_unknown_request_id"
    code_chunk_oversize = "upload_blob_chunk_oversize"
    code_disk_full = "upload_blob_disk_full"
    code_invalid_utf8 = ""  # unused


class PromptBlob(_Flavor):
    init_ok = "upload_prompt_blob.init.ok"
    upload_ok = "upload_prompt_blob.ok"
    error_type = "upload_prompt_blob.error"
    sha_field = "prompt_blob_sha"
    require_utf8 = True  # a prompt blob is injected as text; it must be valid UTF-8
    code_too_large = "upload_prompt_blob_too_large"
    code_unknown = "upload_prompt_blob_unknown_request_id"
    code_chunk_oversize = "upload_prompt_blob_chunk_oversize"
    code_disk_full = "upload_prompt_blob_disk_full"
    code_invalid_utf8 = "prompt_blob_invalid_utf8"


def _b64decode(raw: object) -> bytes:
    """v1 tolerates a bad chunk as empty bytes (no error). Match that."""
    try:
        return base64.b64decode(str(raw or ""), validate=True)
    except (ValueError, TypeError):
        return b""


def _b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _is_utf8(up: _Upload) -> bool:
    """A prompt blob's final bytes must decode as UTF-8 (v1 gate). The bytes are
    already on disk in the temp file; re-read them off-loop only when the gate
    applies (prompt blobs, which are small briefs)."""
    try:
        with open(up.tmp_path, "rb") as fh:
            fh.read().decode("utf-8")
        return True
    except (OSError, UnicodeDecodeError):
        return False
