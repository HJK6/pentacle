"""Authenticated, durable protocol-2 voice staging for the existing blob service.

Only checkpointed raw bytes are acknowledged. Each upload has one joined writer;
connection lifetime is deliberately independent of its seven-day disk lifetime.
The Store's existing per-digest materializer owns canonical-blob coordination.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import time
import weakref
from functools import partial


MAX_BYTES = 16 * 1024 * 1024
CHUNK_BYTES = 65536
MAX_ACTIVE = 10
PRINCIPAL_RESERVED = 160 * 1024 * 1024
GLOBAL_RESERVED = 1024 * 1024 * 1024
EXPIRY_SECONDS = 7 * 24 * 60 * 60
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
_EMPTY_SHA = hashlib.sha256(b"").hexdigest()


class UploadError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class VoiceUploadStore:
    def __init__(self, blobs):
        self.blobs = blobs
        self.root = blobs._root / ".voice-uploads-v2"
        self._catalog_lock = asyncio.Lock()
        self._locks = weakref.WeakValueDictionary()

    async def start(self):
        await self.blobs._run_joined(self._startup)

    def _path(self, principal, upload_id):
        return self.root / hashlib.sha256(principal.encode()).hexdigest() / upload_id

    async def _principal(self, msg):
        if self.blobs.error_alerts is None:
            raise UploadError("upload_forbidden")
        try:
            return await self.blobs.error_alerts.principal(msg)
        except ValueError:
            raise UploadError("upload_forbidden") from None

    @staticmethod
    def _validate(msg, init):
        common = {"type", "request_id", "protocol", "upload_id"}
        allowed = common | ({"total_bytes", "sha256", "mime", "voice_operation", "purpose"} if init else
                            {"offset", "data_b64", "chunk_sha256", "final"})
        if (set(k for k in msg if not k.startswith("_")) - allowed
                or type(msg.get("protocol")) is not int or msg["protocol"] != 2
                or not isinstance(msg.get("request_id"), str) or not msg["request_id"]
                or not isinstance(msg.get("upload_id"), str) or not _UUID.fullmatch(msg["upload_id"])):
            raise UploadError("invalid_request")
        if init:
            if (type(msg.get("total_bytes")) is not int or not 0 < msg["total_bytes"] <= MAX_BYTES
                    or not isinstance(msg.get("sha256"), str) or not _SHA.fullmatch(msg["sha256"])
                    or msg.get("mime") not in ("audio/mp4", "audio/wav")
                    or not isinstance(msg.get("voice_operation"), dict)
                    or msg.get("purpose") not in (None, "generic")):
                raise UploadError("invalid_request")
        elif (type(msg.get("offset")) is not int or not 0 <= msg["offset"] <= MAX_BYTES
                or type(msg.get("final")) is not bool
                or not isinstance(msg.get("data_b64"), str)
                or len(msg["data_b64"]) > 4 * ((CHUNK_BYTES + 2) // 3)
                or not isinstance(msg.get("chunk_sha256"), str)
                or not _SHA.fullmatch(msg["chunk_sha256"])):
            raise UploadError("invalid_request")

    async def handle(self, msg, *, init):
        """Errors are a closed enum; never put disk paths or exceptions on wire."""
        rid = msg.get("request_id", "")
        try:
            principal = await self._principal(msg)
            self._validate(msg, init)
            if init:
                state = await self._init(msg, principal)
            else:
                state = await self._chunk(msg, principal)
            # Revocation during a slow durable write must not disclose offsets.
            await self._principal(msg)
            return {"type": "upload_blob.init.ok" if init else "upload_blob.ok",
                    "request_id": rid, "protocol": 2, "upload_id": state["upload_id"],
                    "next_offset": state["next_offset"], "complete": state["complete"],
                    **({"blob_sha": state["sha256"]} if state["complete"] else {})}
        except UploadError as exc:
            code = exc.code
        except OSError:
            code = "upload_blob_disk_full"
        except Exception:
            # Persistence/adapter failures are retryable without exposing text.
            code = "upload_blob_disk_full"
        return {"type": "upload_blob.error", "request_id": rid, "protocol": 2,
                "error_code": code}

    def _lock(self, principal, upload_id):
        key = (principal, upload_id)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    @contextlib.asynccontextmanager
    async def _locked(self, principal, upload_id):
        # Catalog exclusion makes reservation/reaping atomic with admission.
        # A writer never needs the catalog lock after acquiring its upload lock.
        async with self._catalog_lock:
            lock = self._lock(principal, upload_id)
            await lock.acquire()
        try:
            yield
        finally:
            lock.release()

    async def _init(self, msg, principal):
        path = self._path(principal, msg["upload_id"])
        async with self._catalog_lock:
            busy = {self._path(p, u) for (p, u), lock in list(self._locks.items()) if lock.locked()}
            await self.blobs._run_joined(self._reap, busy)
            lock = self._lock(principal, msg["upload_id"])
            async with lock:
                await self._principal(msg)
                state = await self.blobs._run_joined(self._load, path, principal, msg["upload_id"])
                if state is not None:
                    # Validate the existing registration before register_upload
                    # can fill in absent fields or create a missing operation.
                    await self._verify_binding(path, state)
                    if any(state[k] != msg[k] for k in ("total_bytes", "sha256", "mime", "voice_operation")):
                        raise UploadError("upload_conflict")
                else:
                    await self.blobs._run_joined(self._quota, principal, msg["total_bytes"])
                try:
                    operation = await self.blobs.error_alerts.register_upload(msg)
                except ValueError as exc:
                    code = str(exc)
                    raise UploadError("upload_forbidden" if code in (
                        "operator_required", "credential_revoked", "operation_forbidden") else
                        "upload_conflict" if code in ("event_conflict", "origin_mismatch") else "invalid_request") from None
                if operation != (principal, msg["voice_operation"].get("operation_id")):
                    raise UploadError("upload_forbidden")
                if state is None:
                    state = {"version": 2, "principal": principal, "upload_id": msg["upload_id"],
                             **{k: msg[k] for k in ("total_bytes", "sha256", "mime", "voice_operation")},
                             "next_offset": 0, "prefix_sha256": _EMPTY_SHA,
                             "complete": False, "final_requested": False, "last_activity": time.time()}
                    await self.blobs._run_joined(self._create, path, state)
                if state["final_requested"] and not state["complete"]:
                    state = await self._finish_joined(path, state)
                else:
                    state = {**state, "last_activity": time.time()}
                    await self.blobs._run_joined(self._save, path, state)
                await self._verify_binding(path, state)
                return state

    async def _chunk(self, msg, principal):
        path = self._path(principal, msg["upload_id"])
        async with self._locked(principal, msg["upload_id"]):
            await self._principal(msg)
            state = await self.blobs._run_joined(self._load, path, principal, msg["upload_id"])
            if state is None:
                raise UploadError("upload_expired")
            await self._verify_binding(path, state)
            # Use this validated snapshot under the same upload lock. Entire
            # append + fsync + checkpoint remains one joined worker operation.
            state = await self.blobs._run_joined(self._accept_chunk, path, state, msg)
            if state["final_requested"] and not state["complete"]:
                await self._principal(msg)
                state = await self._finish_joined(path, state)
            await self._verify_binding(path, state)
            return state

    async def _verify_binding(self, path, state):
        """Checkpoint JSON cannot assign or repair authoritative Store identity."""
        intent = state["voice_operation"]
        row = await self.blobs.error_alerts.store.voice_get(state["principal"], intent["operation_id"])
        data = row.get("data") if row else None
        if (not row or row.get("principal") != state["principal"]
                or any(row.get(key) != intent[key] for key in (
                    "operation_id", "origin_stream_id", "origin_generation", "operation_kind", "client_build"))
                or not isinstance(data, dict) or type(data.get("upload_protocol")) is not int
                or data["upload_protocol"] != 2 or data.get("upload_request_id") != state["upload_id"]):
            await self.blobs._run_joined(self._quarantine, path, "upload_conflict")

    async def _finish_joined(self, path, state):
        async def finish():
            if self.blobs.attachment_store is None:
                raise UploadError("upload_forbidden")
            await self._verify_binding(path, state)
            sha = await self.blobs.attachment_store.complete_unmanaged_upload(
                root=self.blobs._root, sha=state["sha256"],
                materialize=partial(self.blobs._materialize_verified, path / "audio.part",
                                    state["sha256"], state["total_bytes"]))
            await self.blobs.error_alerts.store.voice_milestone(
                state["principal"], state["voice_operation"]["operation_id"],
                "upload_committed", blob_sha=sha)
            complete = {**state, "complete": True, "blob_sha": sha, "last_activity": time.time()}
            await self.blobs._run_joined(self._save, path, complete)
            return complete
        return await self.blobs._join_task(asyncio.create_task(finish()))

    @staticmethod
    def _sync_dir(path):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _mkdir(self, path):
        if not path.exists():
            path.mkdir(mode=0o700)
            self._sync_dir(path.parent)

    def _save(self, path, state):
        temp = path / "checkpoint.json.tmp"
        fd = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(state, out, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path / "checkpoint.json")
        self._sync_dir(path)

    def _create(self, path, state):
        self._mkdir(path.parent)
        self._mkdir(path)
        # Commit zero before creating bytes, so restart can safely reconstruct
        # a missing empty file, but never invent a missing acknowledged prefix.
        self._save(path, state)
        with (path / "audio.part").open("wb") as out:
            os.chmod(path / "audio.part", 0o600)
            out.flush()
            os.fsync(out.fileno())
        self._sync_dir(path)

    @staticmethod
    def _read_state(path):
        # Small fixed metadata only. Corrupt/unbounded JSON is not a quota bypass.
        checkpoint = path / "checkpoint.json"
        if checkpoint.stat().st_size > 16384:
            raise ValueError("checkpoint_size")
        with checkpoint.open(encoding="utf-8") as inp:
            return json.load(inp)

    def _quarantine(self, path, code):
        marker = path / "quarantine.json"
        if not marker.exists():
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as out:
                json.dump({"error_code": code}, out)
                out.flush()
                os.fsync(out.fileno())
            self._sync_dir(path)
        raise UploadError(code)

    def _load(self, path, principal, upload_id):
        # The existing schema is available before an alert adapter is configured
        # at startup; validation must not depend on live server composition.
        from error_alerts import INTENT_VALIDATOR
        if not path.exists():
            return None
        if (path / "quarantine.json").exists():
            raise UploadError("upload_conflict")
        if not (path / "checkpoint.json").exists() and not (path / "audio.part").exists():
            # Crash before the first checkpoint: no bytes or ACK ever existed.
            return None
        try:
            s = self._read_state(path)
            if (not isinstance(s, dict) or s.get("version") != 2 or s.get("principal") != principal
                    or s.get("upload_id") != upload_id or type(s.get("total_bytes")) is not int
                    or not 0 < s["total_bytes"] <= MAX_BYTES or type(s.get("next_offset")) is not int
                    or not 0 <= s["next_offset"] <= s["total_bytes"]
                    or not isinstance(s.get("sha256"), str) or not _SHA.fullmatch(s["sha256"])
                    or not isinstance(s.get("prefix_sha256"), str) or not _SHA.fullmatch(s["prefix_sha256"])
                    or s.get("mime") not in ("audio/mp4", "audio/wav")
                    or not INTENT_VALIDATOR.is_valid(s.get("voice_operation"))
                    or type(s.get("complete")) is not bool or type(s.get("final_requested")) is not bool
                    or type(s.get("last_activity")) not in (int, float) or not math.isfinite(s["last_activity"])
                    or (s["complete"] and (not s["final_requested"] or s.get("blob_sha") != s["sha256"]))
                    or (s["final_requested"] and s["next_offset"] != s["total_bytes"])):
                raise ValueError("checkpoint_fields")
        except (ValueError, TypeError, KeyError, FileNotFoundError):
            self._quarantine(path, "upload_conflict")
        if time.time() - s["last_activity"] >= EXPIRY_SECONDS:
            raise UploadError("upload_expired")
        part = path / "audio.part"
        if s["complete"] or (s["final_requested"] and not part.exists()):
            canonical = self.blobs._path_for(s["sha256"])
            if not canonical.is_file():
                self._quarantine(path, "upload_conflict")
            sha, size, _ = self.blobs._file_identity(canonical)
            if sha != s["sha256"] or size != s["total_bytes"]:
                self._quarantine(path, "digest_mismatch")
            return s
        if not part.exists() and s["next_offset"] == 0:
            with part.open("wb") as out:
                os.chmod(part, 0o600)
                out.flush()
                os.fsync(out.fileno())
            self._sync_dir(path)
        if not part.is_file() or part.stat().st_size < s["next_offset"]:
            self._quarantine(path, "upload_conflict")
        if part.stat().st_size > s["next_offset"]:
            with part.open("r+b") as out:
                out.truncate(s["next_offset"])
                out.flush()
                os.fsync(out.fileno())
        sha, _, _ = self.blobs._file_identity(part)
        if sha != s["prefix_sha256"]:
            self._quarantine(path, "digest_mismatch")
        return s

    def _accept_chunk(self, path, s, msg):
        try:
            data = base64.b64decode(msg["data_b64"], validate=True)
        except (ValueError, binascii.Error):
            raise UploadError("invalid_request") from None
        if len(data) > CHUNK_BYTES or base64.b64encode(data).decode("ascii") != msg["data_b64"]:
            raise UploadError("invalid_request")
        if hashlib.sha256(data).hexdigest() != msg["chunk_sha256"]:
            raise UploadError("digest_mismatch")
        offset, end = msg["offset"], msg["offset"] + len(data)
        if (end > s["total_bytes"] or offset > s["next_offset"]
                or (offset < s["next_offset"] < end)
                or (msg["final"] and end != s["total_bytes"])):
            raise UploadError("offset_mismatch")
        if not data and not (msg["final"] and offset == s["total_bytes"]):
            raise UploadError("invalid_request")
        part = path / "audio.part"
        if offset < s["next_offset"]:
            source = self.blobs._path_for(s["sha256"]) if s["complete"] or not part.exists() else part
            if self.blobs._read_slice(source, offset, len(data)) != data:
                raise UploadError("upload_conflict")
        elif data:
            with part.open("r+b") as out:
                out.seek(offset)
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            s = {**s, "next_offset": end,
                 "prefix_sha256": self.blobs._file_identity(part)[0]}
        if msg["final"]:
            if s["prefix_sha256"] != s["sha256"]:
                # Keep the committed prefix. A digest-mismatching final has no
                # ACK; its uncheckpointed tail is truncated on the next init.
                raise UploadError("digest_mismatch")
            s = {**s, "final_requested": True}
        s = {**s, "last_activity": time.time()}
        self._save(path, s)
        return s

    def _entries(self):
        if self.root.exists():
            for parent in self.root.iterdir():
                if parent.is_dir() and _SHA.fullmatch(parent.name):
                    for path in parent.iterdir():
                        if path.is_dir() and _UUID.fullmatch(path.name):
                            yield path

    def _reap(self, busy=frozenset()):
        for path in list(self._entries()):
            if path in busy:
                continue
            try:
                s = self._read_state(path)
                stamp = s["last_activity"]
                if type(stamp) not in (int, float) or not math.isfinite(stamp):
                    raise ValueError("activity")
            except (ValueError, TypeError, KeyError, OSError):
                stamp = path.stat().st_mtime
            if time.time() - stamp >= EXPIRY_SECONDS:
                shutil.rmtree(path)
                self._sync_dir(path.parent)

    def _quota(self, principal, total):
        owner = hashlib.sha256(principal.encode()).hexdigest()
        count = reserved = global_reserved = 0
        for path in self._entries():
            try:
                s = self._read_state(path)
                size = s["total_bytes"]
                if type(size) is not int or not 0 < size <= MAX_BYTES:
                    raise ValueError("reservation")
                complete = s.get("complete") is True and not (path / "quarantine.json").exists()
            except (ValueError, KeyError, TypeError, OSError):
                size, complete = MAX_BYTES, False
            if complete:
                continue
            global_reserved += size
            if path.parent.name == owner:
                reserved += size
                count += 1
        if count >= MAX_ACTIVE or reserved + total > PRINCIPAL_RESERVED or global_reserved + total > GLOBAL_RESERVED:
            raise UploadError("upload_quota")

    def _startup(self):
        self._sync_dir(self.blobs._root.parent)
        self._mkdir(self.root)
        self._reap()
        for path in list(self._entries()):
            try:
                s = self._read_state(path)
                principal = s["principal"]
                if not isinstance(principal, str) or self._path(principal, path.name) != path:
                    self._quarantine(path, "upload_conflict")
                self._load(path, principal, path.name)
            except UploadError:
                pass  # Retained quarantine is an actionable error on next init.
            except (ValueError, TypeError, KeyError, FileNotFoundError):
                try:
                    self._quarantine(path, "upload_conflict")
                except UploadError:
                    pass
