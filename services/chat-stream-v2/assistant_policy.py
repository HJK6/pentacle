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
        # A second protected assistant may run beside the primary.
        # Its role slug is a separate opt-in key; it is preserved-on-death and
        # close-guarded exactly like the primary, but confers no fleet lifecycle
        # authority (that is governed solely by store_lifecycle_authority).  The
        # primary ``self.role`` still backs operator-credential and consent
        # protection byte-identically.
        self.extra_roles: set[str] = set()
        daff_role = os.environ.get("PENTACLE_ASSISTANT_DAFF_ROLE", "").strip()
        if daff_role:
            if len(daff_role) > 40 or not re.fullmatch(r"[a-z0-9_-]+", daff_role):
                raise ValueError("PENTACLE_ASSISTANT_DAFF_ROLE must be a short role slug")
            self.extra_roles.add(daff_role)
        # The primary assistant composite, wired by main: its in-memory binding
        # names the front-desk seat without a store read.
        self._front_desk = None
        self.lock = asyncio.Lock()
        # Fleet lifecycle authority: designate/transfer/revoke/handoff-carry and a
        # manager close/reparent from admission through effect all hold this
        # lock, so the grant cannot change under an admitted effect. Ordering:
        # authority (outermost) -> per-target lifecycle lock -> graph lock.
        # Nothing holding a lifecycle or graph lock may acquire it.
        self.authority_lock = asyncio.Lock()

    @property
    def front_desk(self):
        return self._front_desk

    @front_desk.setter
    def front_desk(self, composite):
        # The Store derives the FD principal from these binding coordinates.
        import store_lifecycle_authority
        self._front_desk = composite
        if self.store is not None:
            self.store.lifecycle_front_desk = store_lifecycle_authority.front_desk_spec(composite)

    @property
    def roles(self) -> set[str]:
        """Every protected assistant role on this host (primary + any extra)."""
        return ({self.role} if self.role else set()) | self.extra_roles

    def protects(self, row):
        return bool(row and self.roles and row.get("role") in self.roles)

    def is_daff_backend(self, row):
        return bool(row and row.get("host") == self.local_host
                    and row.get("role") in self.extra_roles)

    def is_front_desk_seat(self, stream_id, fields):
        """True for a parentless local seat that is, or is becoming, the bound front desk.

        Independent of the role env: a handoff from the bound stream, the bound
        stream itself (resume/restore), or a deliberately commissioned fresh recovery.
        """
        composite = self.front_desk
        if (composite is None or not composite.enabled or fields.get("parent_stream_id")
                or str(stream_id).partition(":")[0] != self.local_host):
            return False
        bound = composite.config.direct_primary_stream_id
        if bound and bound in (stream_id, fields.get("handoff_from_stream_id")):
            return True
        if fields.get("handoff_from_stream_id"):
            return False
        from store_assistant_binding import _configured_spec_authorized
        return _configured_spec_authorized(
            {**fields, "visibility": "hidden"},
            composite.env_config.rebind_authorized_spec_ids, allow_hidden=True,
        )

    @staticmethod
    def _error(code, message):
        from sessions import VerbError
        return VerbError(code, message)

    def operator(self, auth):
        return bool(auth.get("operator_authenticated") or auth.get("service_authenticated"))

    async def available(self, host, role=None, *, excluding="", predecessor=""):
        # Singleton is enforced per protected role, so the two assistants are each
        # their own singleton and never block the other.
        role = role or self.role
        if host != self.local_host:
            raise self._error("assistant_host_invalid", "assistant must run on the configured daemon host")
        rows = await self.store.list_open_sessions_with_event_summary()
        holders = [r for r in rows if r.get("role") == role and r.get("stream_id") != excluding]
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
            if intent.get("open_fields", {}).get("role") == role:
                raise self._error("assistant_exists", "assistant spawn is unresolved; reconcile its receipt before retrying")

    async def authorize_role(self, target, role, auth):
        if not self.roles or (role not in self.roles and not self.protects(target)):
            return
        if not self.operator(auth):
            raise self._error("role_authority_denied", "assistant role changes require authenticated operator authority")
        if role in self.roles:
            if target.get("parent_stream_id"):
                raise self._error("assistant_parent_invalid", "assistant must be a top-level session")
            await self.available(target["host"], role, excluding=target["stream_id"])

    @asynccontextmanager
    async def spawn(self, msg, host):
        if not self.roles:
            yield
            return
        predecessor = str(msg.get("handoff_from_stream_id") or "") if msg.get("handoff") else ""
        source = None
        if predecessor and ":" in predecessor:
            source = await self.store.fetch_session(*predecessor.split(":", 1))
        role = msg.get("role") or (source or {}).get("role")
        if role not in self.roles and not self.protects(source):
            yield
            return
        auth = msg.get("_auth_context") or {}
        own_handoff = bool(self.protects(source) and auth.get("token_verified") and auth.get("stream_id") == predecessor)
        if not (self.operator(auth) or own_handoff):
            raise self._error("role_authority_denied", "assistant activation requires operator authority; rotation requires its owner")
        if role not in self.roles:
            raise self._error("role_authority_denied", "assistant handoff must preserve its protected role")
        if self.protects(source) and role != source.get("role"):
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

    # -- fleet lifecycle authority -------------------------------------------
    # store_lifecycle_authority is the only source of this authority: the
    # current primary binding (front-desk mode) or the durable grant. No
    # role, title, old binding, lineage or config entry confers it.

    @property
    def front_desk_mode(self):
        """A configured primary direct binding names the lifecycle principal."""
        return bool(getattr(self.store, "lifecycle_front_desk", None))

    @property
    def manager_kind(self):
        return "front_desk" if self.front_desk_mode else "manager"

    async def manager_holds(self, auth):
        """True only for the verified current principal generation."""
        auth = auth or {}
        if not auth.get("token_verified"):
            return False
        return await self.store.lifecycle_authority_holder(
            str(auth.get("stream_id") or ""), auth.get("session_generation"))

    # CLI defaults are not an explicit reason for a fleet lifecycle action.
    DEFAULT_REASONS = frozenset({"", "manual", "reparent"})

    def manager_request_code(self, msg):
        """Refusal code when a manager request lacks explicit reason/request id."""
        if str(msg.get("reason") or "").strip() in self.DEFAULT_REASONS:
            return "lifecycle_reason_required"
        if not str(msg.get("request_id") or "").strip():
            return "lifecycle_request_id_required"
        return None

    async def manager_fences(self, target_row, generation, msg, *, children, pending_spawns,
                             allow_unavailable=False):
        """Refusal code for a manager close of a non-child, or None."""
        code = self.manager_request_code(msg)
        if code:
            return code
        if (target_row is None or str(target_row.get("status") or "") != "open"
                or (not allow_unavailable and
                    (target_row.get("offline_since_ts") or target_row.get("presumed_dead_at")))):
            return "lifecycle_target_unavailable"
        if self.protects(target_row):
            return "close_protected"
        if children:
            return "close_live_children"
        if pending_spawns:
            return "close_pending_spawn"
        return None
