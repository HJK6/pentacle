"""Optional protection for one ordinary assistant role on this daemon's host.

No role is special unless the operator enables it in private configuration.
The existing session rows, spawn reservations and handoff remain authoritative.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
import re


class AssistantPolicy:
    def __init__(self, store, local_host):
        self.store = store
        self.local_host = local_host
        self.role = os.environ.get("PENTACLE_ASSISTANT_ROLE", "").strip()
        if self.role and (len(self.role) > 40 or not re.fullmatch(r"[a-z0-9_-]+", self.role)):
            raise ValueError("PENTACLE_ASSISTANT_ROLE must be a short role slug")
        self.lock = asyncio.Lock()

    def protects(self, row):
        return bool(self.role and row and row.get("role") == self.role)

    @staticmethod
    def _error(code, message):
        from sessions import VerbError
        return VerbError(code, message)

    def operator(self, auth):
        return bool(auth.get("operator_authenticated") or auth.get("service_authenticated"))

    async def available(self, host, *, excluding="", predecessor=""):
        if host != self.local_host:
            raise self._error("assistant_host_invalid", "assistant must run on the configured daemon host")
        rows = await self.store.list_open_sessions_with_event_summary()
        holders = [r for r in rows if self.protects(r) and r.get("stream_id") != excluding]
        if any(r.get("stream_id") != predecessor for r in holders):
            raise self._error("assistant_exists", "an assistant is already open; use its managed handoff")
        # A spawn may return starting/uncertain before an open row exists. Its
        # persisted intent remains ownership evidence even after a restart.
        for reservation in await self.store.reservations(include_expired=True):
            sid = f"{reservation['host']}:{reservation['session_name']}"
            if sid == excluding:
                continue
            payload = reservation.get("payload")
            if not payload:
                continue
            intent = json.loads(payload) if isinstance(payload, str) else payload
            if self.protects(intent.get("open_fields", {})):
                raise self._error("assistant_exists", "assistant spawn is unresolved; reconcile its receipt before retrying")

    async def authorize_role(self, target, role, auth):
        if not self.role or (role != self.role and not self.protects(target)):
            return
        if not self.operator(auth):
            raise self._error("role_authority_denied", "assistant role changes require authenticated operator authority")
        if role == self.role:
            if target.get("parent_stream_id"):
                raise self._error("assistant_parent_invalid", "assistant must be a top-level session")
            await self.available(target["host"], excluding=target["stream_id"])

    @asynccontextmanager
    async def spawn(self, msg, host):
        if not self.role:
            yield
            return
        predecessor = str(msg.get("handoff_from_stream_id") or "") if msg.get("handoff") else ""
        source = None
        if predecessor and ":" in predecessor:
            source = await self.store.fetch_session(*predecessor.split(":", 1))
        role = msg.get("role") or (source or {}).get("role")
        if role != self.role and not self.protects(source):
            yield
            return
        auth = msg.get("_auth_context") or {}
        own_handoff = bool(self.protects(source) and auth.get("token_verified") and auth.get("stream_id") == predecessor)
        if not (self.operator(auth) or own_handoff):
            raise self._error("role_authority_denied", "assistant activation requires operator authority; rotation requires its owner")
        if role != self.role:
            raise self._error("role_authority_denied", "assistant handoff must preserve its protected role")
        if msg.get("parent_stream_id") and not predecessor:
            raise self._error("assistant_parent_invalid", "assistant must be a top-level session")
        if host != self.local_host:
            raise self._error("assistant_host_invalid", "assistant must run on the configured daemon host")
        async with self.lock:
            # Keep existing idempotent replay ahead of duplicate checks. A new
            # reservation is checked by SpawnCtl immediately before boot.
            yield

    def guard_close(self, row, close_kind):
        if self.protects(row) and close_kind not in {"handed_off", "spawn_rollback"}:
            raise self._error("close_protected", "assistant session is protected; use managed handoff for replacement")
