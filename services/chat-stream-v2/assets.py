"""assets.py - the operator review-tab asset verbs, LIFTED from v1.

Per `v1_code_reuse_map.md` (asset verbs: KS generic CRUD, 430 rows, no failure
class) this is a migration: the store is the shared `AssetStore` implementation
under `services/_shared/` (its own `assets.db`, so open reviews survive cutover)
and the report body validator is the shared `asset_schema.py` - so publishing a `report`
asset gets the exact nested run/block/section validation, byte-for-byte. The
handler orchestration is lifted from `chat_streamd.py`'s `_handle_asset_message`.

Adaptations at the two seams the reuse map allows:
  - I/O placement: the synchronous store runs behind a single worker thread.
  - by-ID verbs use v2's connection-bound operator fact or verified stream-token
    owner. Operators select one exact target session. Token owners may select
    their own row or a row carrying one of their raw server-known spec ids; a
    targetless shared-spec lookup must be unique. Caller identity/spec claims
    never authorize a row. This intentionally does not reproduce v1's broader
    qualified/equivalent-spec plane. The `approved`/`send_to_chat` review tell
    rides `comms.tell` (v2's one injection path) best-effort. The delete
    `audit_id` is synthesized (no lifecycle_audit subsystem in v2).

Wire shapes (types + fields + error vocabulary) are byte-identical - verified
against the `agent-orch asset` CLI (publish/list/get/comments/comments resolve/
health) and the Pentacle review tab (asset.get/comments.list/read.set/review.set
/comment.* + the asset.update / asset.removed pushes).
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Awaitable, Callable

SERVICES_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICES_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICES_ROOT))

from _shared.asset_schema import AssetBodyTooLarge, AssetValidationError, normalize_tags
from _shared.assets_store import AssetNotFound, AssetStore, AssetStoreError, InvalidAsset, asset_metadata

log = logging.getLogger("chat_streamd_v2.assets")

DEFAULT_ASSETS_DB = str(Path.home() / ".local/share/pentacle-stream/assets.db")


def _nullable_text(value: object) -> str | None:
    text = str(value).strip() if isinstance(value, str) else str(value or "").strip()
    return text or None


class Assets:
    """Asset CRUD + review workflow, lifted. The store is synchronous; every
    call runs on one worker thread (SQLite off the loop). Broadcasts/injection
    go through v2's server + comms."""

    def __init__(self, db_path: str = DEFAULT_ASSETS_DB, *, sessions: Any = None,
                 comms: Any = None, broadcast: Any = None) -> None:
        self._db_path = db_path
        self._sessions = sessions
        self._comms = comms
        self._broadcast = broadcast
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="asset-store")
        self._store: AssetStore | None = None
        self._ready = asyncio.Event()

    async def start(self) -> None:
        self._store = await self._run(AssetStore, self._db_path)
        self._ready.set()

    async def stop(self) -> None:
        if self._store is not None:
            try:
                await self._run(self._store.close)
            except Exception:  # pragma: no cover
                pass
        self._pool.shutdown(wait=False)

    async def _run(self, fn: Callable[..., Any], *a: Any, **kw: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(
            self._pool, functools.partial(fn, *a, **kw))

    async def _call(self, method: str, /, **kwargs: Any) -> Any:
        if self._store is None:
            raise AssetStoreError("asset store not started")
        return await self._run(functools.partial(getattr(self._store, method), **kwargs))

    async def _await_ready(self) -> None:
        if not self._ready.is_set():
            await asyncio.wait_for(self._ready.wait(), timeout=10.0)

    def wire_handlers(self) -> dict[str, Callable[[dict[str, Any]], Awaitable[Any]]]:
        verbs = ("publish", "get", "list", "health", "delete", "read.set", "review.set",
                 "comment.add", "comment.edit", "comment.delete", "comment.resolve",
                 "comments.list", "comments.send_to_chat")
        return {f"asset.{v}": self.asset for v in verbs}

    # -- dispatch (lifted) -------------------------------------------------

    async def asset(self, msg: dict[str, Any]) -> dict[str, Any]:
        request_id = str(msg.get("request_id") or "")
        verb = str(msg.get("type") or "")
        try:
            await self._await_ready()
            handler = getattr(self, "_" + verb.replace(".", "_"), None)
            if handler is None:
                return self._error(request_id, "asset_unknown_command", command=verb)
            return await handler(msg, request_id)
        except asyncio.TimeoutError:
            return self._error(request_id, "asset_store_not_ready")
        except AssetBodyTooLarge as exc:
            return self._error(request_id, "asset_body_too_large", message=str(exc))
        except AssetNotFound:
            return self._error(request_id, "asset_not_found", asset_id=_asset_id(msg))
        except (InvalidAsset, AssetValidationError) as exc:
            return self._error(request_id, "asset_invalid", message=str(exc))
        except ValueError as exc:
            # v1 passes a whitelist of value-error codes through verbatim.
            code = str(exc)
            if code in {"asset_unauthorized", "asset_spec_unattached", "asset_spec_ambiguous",
                        "asset_spec_anchor_mismatch", "asset.session_closed"}:
                return self._error(request_id, code)
            return self._error(request_id, "asset_invalid", message=code)
        except AssetStoreError as exc:
            return self._error(request_id, "asset_store_error", message=str(exc))
        except Exception:  # noqa: BLE001 - every asset RPC must settle with a typed error
            log.exception("asset RPC failed before reply verb=%s", verb)
            return self._error(request_id, "asset_store_error")

    async def _asset_publish(self, msg: dict, request_id: str) -> dict:
        host, name = self._resolve(msg)
        stream_id = f"{host}:{name}"
        record = await self._call(
            "publish_asset", host=host, session_name=name, stream_id=stream_id,
            asset_id=_asset_id(msg) or None,
            title=str(msg.get("title") or ""),
            content_type=str(msg.get("content_type") or msg.get("type_hint") or ""),
            body=str(msg.get("body") if msg.get("body") is not None else ""),
            tags=normalize_tags(msg.get("tags")),
            producer=_nullable_text(msg.get("producer")) or stream_id,
            spec_id=_nullable_text(msg.get("spec_id")),
        )
        await self._broadcast_update(record)
        return {"type": "asset.publish.ok", "request_id": request_id, "asset": record}

    async def _asset_health(self, msg: dict, request_id: str) -> dict:
        return {"type": "asset.health.ok", "request_id": request_id,
                "store": await self._call("identity")}

    async def _asset_list(self, msg: dict, request_id: str) -> dict:
        # v1 parity (chat_streamd.py asset.list): explicit spec-ids with no
        # session identity → union of list_by_spec_id; otherwise scope to the
        # caller session + its spec tags. The pre-v2 handler listed every asset.
        explicit_spec_ids = _normalize_spec_ids(msg.get("spec_ids"), msg.get("spec_id"))
        raw_limit = msg.get("limit")
        limit = int(raw_limit) if isinstance(raw_limit, int) and not isinstance(raw_limit, bool) else None
        session_key: dict | None = None
        if explicit_spec_ids and not (
            _nullable_text(msg.get("stream_id")) or _nullable_text(msg.get("host"))
        ):
            records: list[dict] = []
            for spec_id in explicit_spec_ids:
                records.extend(await self._call("list_by_spec_id", spec_id=spec_id))
            records.sort(key=lambda r: (r.get("updated_at") or "", r.get("asset_id") or ""), reverse=True)
            if limit is not None:
                records = records[:limit]
        else:
            session_key = self._session_key_from_msg(msg)
            if session_key is None:
                raise InvalidAsset("stream_id (or host + session_name) is required")
            records = await self._call(
                "list_for_session_and_specs",
                host=session_key["host"], session_name=session_key["session_name"],
                spec_ids=self._spec_ids_from_msg(msg, session_key), limit=limit)
        return {"type": "asset.list.ok", "request_id": request_id,
                "session_key": session_key,
                "assets": [asset_metadata(r) for r in records]}

    async def _asset_get(self, msg: dict, request_id: str) -> dict:
        record = await self._require_record(msg)
        return {"type": "asset.get.ok", "request_id": request_id,
                "session_key": self._record_key(record),
                "asset": {**record, "read": bool(record.get("read_at"))}}

    async def _asset_delete(self, msg: dict, request_id: str) -> dict:
        record = await self._require_record(msg)
        await self._call("delete_asset", host=record["host"], session_name=record["session_name"],
                         asset_id=record["asset_id"])
        await self._broadcast_removed(record)
        # No lifecycle_audit subsystem in v2; a synthesized id keeps the reply
        # shape (the CLI/UI only echo it).
        return {"type": "asset.delete.ok", "request_id": request_id,
                "asset_id": record["asset_id"], "audit_id": uuid.uuid4().hex}

    async def _asset_comment_add(self, msg: dict, request_id: str) -> dict:
        record = await self._require_record(msg)
        run_index = msg.get("run_index")
        comment = await self._call(
            "add_comment", host=record["host"], session_name=record["session_name"],
            asset_id=record["asset_id"], section_id=str(msg.get("section_id") or ""),
            block_id=str(msg.get("block_id") or ""),
            run_index=run_index if isinstance(run_index, int) and not isinstance(run_index, bool) else None,
            excerpt=_nullable_text(msg.get("excerpt")), body=str(msg.get("body") or ""),
            author=self._author(msg, record), parent_comment_id=_nullable_text(msg.get("parent_comment_id")),
            comment_id=_nullable_text(msg.get("comment_id")))
        await self._broadcast_update(record)
        return {"type": "asset.comment.add.ok", "request_id": request_id, "comment": comment}

    async def _asset_comment_edit(self, msg: dict, request_id: str) -> dict:
        record = await self._require_record(msg)
        comment = await self._call("edit_comment", comment_id=str(msg.get("comment_id") or ""),
                                   body=str(msg.get("body") or ""), host=record["host"],
                                   session_name=record["session_name"], asset_id=record["asset_id"])
        await self._broadcast_update(record)
        return {"type": "asset.comment.edit.ok", "request_id": request_id, "comment": comment}

    async def _asset_comment_delete(self, msg: dict, request_id: str) -> dict:
        record = await self._require_record(msg)
        comment = await self._call("delete_comment", comment_id=str(msg.get("comment_id") or ""),
                                   host=record["host"], session_name=record["session_name"],
                                   asset_id=record["asset_id"])
        await self._broadcast_update(record)
        return {"type": "asset.comment.delete.ok", "request_id": request_id, "comment": comment}

    async def _asset_comment_resolve(self, msg: dict, request_id: str) -> dict:
        record = await self._require_record(msg)
        comment = await self._call(
            "resolve_comment", comment_id=str(msg.get("comment_id") or ""),
            resolved=msg.get("resolved") is not False,
            resolved_by=_nullable_text(msg.get("resolved_by")) or _nullable_text(msg.get("from_stream_id"))
            or self._author(msg, record),
            resolution_note=_nullable_text(msg.get("note")), host=record["host"],
            session_name=record["session_name"], asset_id=record["asset_id"])
        await self._broadcast_update(record)
        return {"type": "asset.comment.resolve.ok", "request_id": request_id, "comment": comment}

    async def _asset_comments_list(self, msg: dict, request_id: str) -> dict:
        record = await self._require_record(msg)
        comments = await self._call("list_comments", host=record["host"], session_name=record["session_name"],
                                    asset_id=record["asset_id"], unresolved_only=bool(msg.get("unresolved")))
        return {"type": "asset.comments.list.ok", "request_id": request_id,
                "session_key": self._record_key(record), "asset": asset_metadata(record), "comments": comments}

    async def _asset_read_set(self, msg: dict, request_id: str) -> dict:
        record = await self._require_record(msg)
        if not isinstance(msg.get("read"), bool):
            raise InvalidAsset("read must be a boolean")
        updated = await self._call("set_read", host=record["host"], session_name=record["session_name"],
                                   asset_id=record["asset_id"], read=msg["read"])
        await self._broadcast_update(updated)
        return {"type": "asset.read.set.ok", "request_id": request_id, "asset": asset_metadata(updated)}

    async def _asset_review_set(self, msg: dict, request_id: str) -> dict:
        record = await self._require_record(msg)
        review_status = str(msg.get("review_status") or msg.get("status") or "")
        updated = await self._call("set_review_status", host=record["host"], session_name=record["session_name"],
                                   asset_id=record["asset_id"], review_status=review_status)
        reply = {"type": "asset.review.set.ok", "request_id": request_id, "asset": asset_metadata(updated)}
        if review_status == "approved":
            delivery = await self._deliver_review_tell(updated, approved=True)
            if delivery:
                reply["delivery"] = delivery
        await self._broadcast_update(updated)
        return reply

    async def _asset_comments_send_to_chat(self, msg: dict, request_id: str) -> dict:
        record = await self._require_record(msg)
        updated = await self._call("set_review_status", host=record["host"], session_name=record["session_name"],
                                   asset_id=record["asset_id"], review_status="changes_requested")
        unresolved = await self._call("list_comments", host=record["host"], session_name=record["session_name"],
                                      asset_id=record["asset_id"], unresolved_only=True)
        delivery = await self._deliver_review_tell(updated, approved=False, unresolved=len(unresolved))
        await self._broadcast_update(updated)
        return {"type": "asset.comments.send_to_chat.ok", "request_id": request_id,
                "asset": asset_metadata(updated), "unresolved": len(unresolved), "delivery": delivery}

    # -- helpers -----------------------------------------------------------

    async def _require_record(self, msg: dict) -> dict:
        asset_id = _asset_id(msg)
        if not asset_id:
            raise InvalidAsset("asset_id is required")

        auth = msg.get("_auth_context")
        if not isinstance(auth, dict):
            auth = {}
        operator_authenticated = auth.get("operator_authenticated") is True
        token_verified = auth.get("token_verified") is True
        owner_stream_id = _nullable_text(auth.get("stream_id"))

        # The sufficient connection-bound operator fact wins even if the same
        # request carried a bad stream token. Its scope is nevertheless exact:
        # knowing an asset id never permits newest-row fallback.
        if operator_authenticated:
            target = self._target_session_key(msg, required=True)
            records = await self._call("find_assets_by_id", asset_id=asset_id)
            for record in records:
                if self._record_in_session(record, target):
                    return record
            raise AssetNotFound(asset_id)

        # Only the server-derived token owner is authority. Wire claims and
        # message spec ids are deliberately absent from this decision.
        if not token_verified or not owner_stream_id:
            raise ValueError("asset_unauthorized")

        owner_key = self._stream_session_key(owner_stream_id)
        if owner_key is None:
            raise ValueError("asset_unauthorized")
        records = await self._call("find_assets_by_id", asset_id=asset_id)
        target = self._target_session_key(msg, required=False)
        owner_spec_ids = set(self._session_spec_ids(owner_key))

        if target is not None:
            record = next(
                (candidate for candidate in records if self._record_in_session(candidate, target)),
                None,
            )
            if record is None:
                raise ValueError("asset_unauthorized")
            if self._record_in_session(record, owner_key):
                return record
            if _nullable_text(record.get("spec_id")) in owner_spec_ids:
                return record
            raise ValueError("asset_unauthorized")

        # With no selected target the owner's own row is deterministic and
        # wins before shared-spec fallback, regardless of same-ID collisions.
        for record in records:
            if self._record_in_session(record, owner_key):
                return record
        authorized = [
            record for record in records
            if _nullable_text(record.get("spec_id")) in owner_spec_ids
        ]
        if not authorized:
            raise ValueError("asset_unauthorized")
        if len(authorized) > 1:
            raise ValueError("asset_spec_ambiguous")
        return authorized[0]

    @staticmethod
    def _stream_session_key(stream_id: str) -> dict | None:
        host, separator, session_name = stream_id.partition(":")
        if not separator or not host or not session_name:
            return None
        return {"host": host, "session_name": session_name, "stream_id": stream_id}

    def _target_session_key(self, msg: dict, *, required: bool) -> dict | None:
        stream_id = _nullable_text(msg.get("stream_id"))
        host = _nullable_text(msg.get("host"))
        session_name = _nullable_text(msg.get("session_name"))
        if stream_id:
            target = self._stream_session_key(stream_id)
            if target is None:
                raise InvalidAsset("stream_id must be host:session_name")
            if (host and host != target["host"]) or (
                session_name and session_name != target["session_name"]
            ):
                raise InvalidAsset("stream_id conflicts with host/session_name")
            return target
        if host or session_name:
            if not host or not session_name:
                raise InvalidAsset("host and session_name are both required")
            return {"host": host, "session_name": session_name,
                    "stream_id": f"{host}:{session_name}"}
        if required:
            raise InvalidAsset("stream_id (or host + session_name) is required")
        return None

    @staticmethod
    def _record_in_session(record: dict, session_key: dict) -> bool:
        return (
            record.get("host") == session_key["host"]
            and record.get("session_name") == session_key["session_name"]
        )

    def _resolve(self, msg: dict) -> tuple[str, str]:
        sid = _nullable_text(msg.get("stream_id")) or _nullable_text(msg.get("from_stream_id"))
        if sid:
            host, _, name = sid.partition(":")
        else:
            host, name = str(msg.get("host") or "").strip(), str(msg.get("session_name") or "").strip()
        if not host or not name:
            raise InvalidAsset("stream_id (or host + session_name) is required")
        return host, name

    def _session_key_from_msg(self, msg: dict) -> dict | None:
        try:
            host, name = self._resolve(msg)
        except InvalidAsset:
            return None
        return {"host": host, "session_name": name, "stream_id": f"{host}:{name}"}

    def _spec_ids_from_msg(self, msg: dict, session_key: dict | None) -> list[str]:
        """v1 `_asset_spec_ids_from_message`: explicit message spec ids win;
        else the caller session's tagged spec ids."""
        explicit = _normalize_spec_ids(msg.get("spec_ids"), msg.get("spec_id"))
        if explicit:
            return explicit
        return self._session_spec_ids(session_key)

    def _session_spec_ids(self, session_key: dict | None) -> list[str]:
        """Source the session's spec ids from the v2 sessions store (raw TEXT
        `spec_ids`/`spec_id`, as the store serves them)."""
        if self._sessions is None or not session_key:
            return []
        row = self._sessions.get(session_key["stream_id"])
        if not isinstance(row, dict):
            return []
        return _normalize_spec_ids(row.get("spec_ids"), row.get("spec_id"))

    @staticmethod
    def _record_key(record: dict) -> dict:
        return {"host": record["host"], "session_name": record["session_name"], "stream_id": record["stream_id"]}

    @staticmethod
    def _author(msg: dict, record: dict) -> str:
        return (_nullable_text(msg.get("from_stream_id")) or _nullable_text(msg.get("author"))
                or _nullable_text(msg.get("resolved_by")) or "operator")

    async def _broadcast_update(self, record: dict) -> None:
        if self._broadcast is not None:
            await self._broadcast({"type": "asset.update",
                                   "session_key": self._record_key(record), **asset_metadata(record)})

    async def _broadcast_removed(self, record: dict) -> None:
        if self._broadcast is not None:
            await self._broadcast({"type": "asset.removed", "session_key": self._record_key(record),
                                   "asset_id": record["asset_id"], "spec_id": record.get("spec_id")})

    async def _deliver_review_tell(self, record: dict, *, approved: bool, unresolved: int = 0) -> dict | None:
        """v1 sent the producer a peer tell on approve / changes-requested. v2
        routes it through the one injection path, best-effort (a closed/remote
        producer is not an error)."""
        if self._comms is None:
            return None
        target = str(record.get("stream_id") or "")
        if not target:
            return None
        if approved:
            text = f"[asset.review] approved: {record.get('title') or record.get('asset_id')}"
        else:
            text = (f"[asset.review] changes requested ({unresolved} unresolved): "
                    f"{record.get('title') or record.get('asset_id')}")
        try:
            reply = await self._comms.tell({"tell_id": f"asset-review-{record['asset_id']}-{record.get('updated_at')}",
                                            "stream_id": target, "message": text})
            return {"delivered": True, "to_stream_id": target, "tell_type": reply.get("type")}
        except Exception as exc:  # noqa: BLE001 - review status already persisted + broadcast
            log.info("asset review tell failed asset=%s: %s", record.get("asset_id"), exc)
            return {"delivered": False, "to_stream_id": target, "error": str(exc)}

    @staticmethod
    def _error(request_id: str, error_code: str, **extra: Any) -> dict:
        return {"type": "asset.error", "request_id": request_id,
                "error_code": error_code, "error": error_code, **extra}


def _asset_id(msg: dict) -> str:
    return str(msg.get("asset_id") or msg.get("asset_id_arg") or "").strip()


def _normalize_spec_ids(spec_ids: object = None, spec_id: object = None) -> list[str]:
    """Lifted verbatim from v1 `_normalize_spec_ids`: accepts a JSON-encoded
    list, a comma-separated string, or a sequence, plus an optional scalar
    `spec_id`; returns a de-duplicated, order-preserving list of non-empty ids."""
    values: list[object] = []
    if isinstance(spec_ids, str):
        try:
            decoded = json.loads(spec_ids)
            values.extend(decoded if isinstance(decoded, list) else [spec_ids])
        except Exception:
            values.extend(part.strip() for part in spec_ids.split(","))
    elif isinstance(spec_ids, (list, tuple, set)):
        values.extend(spec_ids)
    if spec_id:
        values.insert(0, spec_id)
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized
