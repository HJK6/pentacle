"""uiverbs.py - the UI-sent verb group (mobile/desktop day-1), lifted per
`v1_code_reuse_map.md` (K; review H2: omitting these breaks mobile enrollment/
push/interrupt + the desktop specs pane on cutover day 1).

Each verb is classified by the reuse-map's liftability analysis and handled at
the honest level v2 can support this increment:

  CLEANLY LIFTED (pure/near-pure, byte-identical):
    - `spawn_catalog_get` (CLI `models`): echoes `_shared.spawn_profiles.catalog()`,
      the same static catalog v1 returns. No store, no panes.
    - `grant_token` (CLI `grant-self-token`): mint -> sha256 hash -> bootstrap-once
      conditional set on the OPEN `sessions` row (`store.grant_stream_token`);
      plaintext returned exactly once. v1 vocabulary: invalid_request /
      stream_unknown / token_already_set.
    - `register_push`: refreshes v1's DynamoDB `PushTokens` registry with the
      same item shape, 90-day TTL, and active field.

  FUNCTIONAL-DEGRADED (the action runs; the confirm that needs the tmux mirror
  does not, so the outcome is honestly labelled, never faked):
    - `send.interrupt`: injects the Escape into the local pane (the interrupt
      itself) but cannot scrape the `Interrupted` landing marker without the
      mirror (lane 4), so `confirm` is `interrupt_unconfirmed` (a live pane) or
      `pane_unavailable` (no pane). Keyed on (host, session_name) like v1.

  HONEST-DEGRADED (the backing subsystem is a REDESIGN-out or not-yet-built
  plane; v2 returns v1's own error for that state rather than a fake success):
    - `close.cancel`: v2 has no deferred-close plane (close is synchronous +
      bounded, lane 3), so there is never a matching intent -> `close_intent_not_found`,
      exactly what v1 returns for an absent intent.
    - `question.dismiss`: answers the terminal-SCRAPED question that lives on the
      session summary (mirror, lane 4). v2 has no scrape cache yet -> `stale_question`,
      v1's error when no active scraped question matches.
    - `specs.list/get/capabilities/drive`: the full specs UI remains degraded,
      while the session binding path uses the shared catalog only to resolve
      explicit attach requests.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import boto3

from sessions import VerbError
from enrollment import EnrollmentError, EnrollmentRegistry
from seat_token_telemetry import SeatTokenTelemetry
from store import normalize_spec_binding_provenance, normalize_spec_ids
from v2_runtime import iso_now

SERVICES_ROOT = str(Path(__file__).resolve().parents[1])
if SERVICES_ROOT not in sys.path:
    sys.path.insert(0, SERVICES_ROOT)

from _shared.spawn_profiles import catalog as spawn_catalog  # noqa: E402

log = logging.getLogger("chat_streamd_v2.uiverbs")

PUSH_TTL_SECONDS = 90 * 24 * 60 * 60
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
PUSH_TOKENS_TABLE = os.environ.get("PUSH_TOKENS_TABLE", "PushTokens")
SPEC_ID_RE = re.compile(r"^[A-Za-z0-9_-]+__[A-Za-z0-9_-]+$")


class UIVerbs:
    def __init__(
        self,
        store: Any,
        sessions: Any,
        spawnctl: Any,
        *,
        specs: Any = None,
        enrollment_registry: EnrollmentRegistry | None = None,
    ) -> None:
        self.store = store
        self.sessions = sessions
        self.spawnctl = spawnctl
        self._spec_catalog = specs
        self.enrollment_registry = enrollment_registry or EnrollmentRegistry()
        self.token_telemetry = (
            getattr(spawnctl, "token_telemetry", None) or SeatTokenTelemetry()
        )

    def wire_handlers(self) -> dict[str, Callable[[dict[str, Any]], Awaitable[Any]]]:
        return {
            "spawn_catalog_get": self.spawn_catalog_get,
            "grant_token": self.grant_token,
            "register_push": self.register_push,
            "send.interrupt": self.send_interrupt,
            "close.cancel": self.close_cancel,
            "question.dismiss": self.question_dismiss,
            "enroll": self.enroll,
            "specs.list": self.specs,
            "specs.get": self.specs,
            "specs.capabilities": self.specs,
            "specs.drive": self.specs,
            "session.spec_update": self.session_spec_update,
        }

    # -- cleanly lifted ----------------------------------------------------

    async def spawn_catalog_get(self, msg: dict[str, Any]) -> dict[str, Any]:
        rid = str(msg.get("request_id") or "")
        return {"type": "spawn_catalog_get.ok", "request_id": rid, **spawn_catalog()}

    async def grant_token(self, msg: dict[str, Any]) -> dict[str, Any]:
        rid = str(msg.get("request_id") or "")
        stream_id = str(msg.get("stream_id") or "")
        if ":" not in stream_id:
            return _err("grant_token", rid, "invalid_request", message="stream_id must be host:session")
        host, name = self.sessions.split(stream_id)
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        from store import STREAM_TOKEN_HASH_VERSION
        result = await self.store.grant_stream_token(host, name, token_hash, STREAM_TOKEN_HASH_VERSION)
        if result == "stream_unknown":
            return _err("grant_token", rid, "stream_unknown", message="no open session for stream_id")
        if result == "token_already_set":
            return _err("grant_token", rid, "token_already_set", message="stream token already granted")
        self.token_telemetry.record_issuance(
            stream_id=stream_id,
            operation="grant_token",
        )
        # Plaintext returned exactly once (never stored).
        return {"type": "grant_token.ok", "request_id": rid, "stream_id": stream_id,
                "stream_token": token, "token_hash_version": STREAM_TOKEN_HASH_VERSION}

    async def register_push(self, msg: dict[str, Any]) -> dict[str, Any]:
        rid = str(msg.get("request_id") or "")
        push_token = str(msg.get("push_token") or "").strip()
        if not push_token:
            return {"type": "register_push.error", "request_id": rid, "error": "push_token is required"}
        item = {
            "push_token": push_token,
            "platform": str(msg.get("platform") or "ios"),
            "device_name": str(msg.get("device_name") or ""),
            "registered_at": iso_now(),
            "active": True,
            "ttl": int(time.time()) + PUSH_TTL_SECONDS,
        }
        await asyncio.to_thread(
            lambda: boto3.resource("dynamodb", region_name=AWS_REGION)
            .Table(PUSH_TOKENS_TABLE).put_item(Item=item)
        )
        return {"type": "register_push.ok", "request_id": rid}

    # -- functional-degraded ----------------------------------------------

    async def send_interrupt(self, msg: dict[str, Any]) -> dict[str, Any]:
        rid = str(msg.get("request_id") or "")
        host = str(msg.get("host") or "").strip()
        name = str(msg.get("session_name") or "").strip()
        if not host or not name:
            return {"type": "send.interrupt.error", "request_id": rid,
                    "error": "host and session_name are required"}
        try:
            self.sessions.assert_local(host, "send.interrupt")
        except VerbError as exc:
            return {"type": "send.interrupt.error", "request_id": rid, "error": f"{exc.code}: {exc}"}
        tmux = self.spawnctl.tmux
        if not (tmux and await tmux.has_session(name)):
            return {"type": "send.interrupt.ok", "request_id": rid, "host": host, "session_name": name,
                    "interrupted": False, "landed": False, "confirm": "pane_unavailable", "coalesced": False}
        # The interrupt itself: one Escape into the pane (v1's B6 mechanism). The
        # `Interrupted` landing marker is scraped from the transcript by the
        # mirror (lane 4); without it v2 reports the honest `interrupt_unconfirmed`
        # rather than claiming a confirmed landing.
        await tmux.run("send-keys", "-t", f"={name}:", "Escape")
        return {"type": "send.interrupt.ok", "request_id": rid, "host": host, "session_name": name,
                "interrupted": True, "landed": False, "confirm": "interrupt_unconfirmed", "coalesced": False}

    # -- honest-degraded ---------------------------------------------------

    async def close_cancel(self, msg: dict[str, Any]) -> dict[str, Any]:
        rid = str(msg.get("request_id") or "")
        # v2 has no deferred-close plane, so no intent ever matches. v1 returns
        # this same code for an absent intent, and re-cancels are idempotent.
        return {"type": "close.cancel.error", "request_id": rid,
                "error": "close_intent_not_found", "error_code": "close_intent_not_found"}

    async def question_dismiss(self, msg: dict[str, Any]) -> dict[str, Any]:
        rid = str(msg.get("request_id") or "")
        # The scraped-question cache lives in the tmux mirror (lane 4). Until it
        # exists v2 cannot validate `question_key` or map an answer to an option,
        # so it returns v1's own "no matching active question" error.
        return {"type": "question.dismiss.error", "request_id": rid,
                "error_code": "stale_question", "error": "stale_question"}

    async def enroll(self, msg: dict[str, Any]) -> dict[str, Any]:
        # Enrollment replies intentionally carry no request_id; that is the
        # established mobile wire contract. The registry owns code validation,
        # atomic one-use consumption, and v2 credential issuance.
        try:
            return self.enrollment_registry.exchange(msg)
        except EnrollmentError as exc:
            return {"type": "enroll.error", "error": str(exc)}

    async def specs(self, msg: dict[str, Any]) -> dict[str, Any]:
        rid = str(msg.get("request_id") or "")
        verb = str(msg.get("type") or "")
        if verb == "specs.drive":
            # drive spawns a session — the spawn subsystem is owned elsewhere.
            return {"type": "specs.error", "request_id": rid,
                    "error": "unsupported_in_v2", "error_code": "unsupported_in_v2"}
        # list/get/capabilities: the configured memory work/ tree + watcher are not
        # wired this increment; v1 degrades to exactly this when the root is absent.
        return {"type": "specs.error", "request_id": rid,
                "error": "specs_not_configured", "error_code": "specs_not_configured"}

    async def session_spec_update(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Attach or detach one catalog-resolved work item durably."""
        rid = str(msg.get("request_id") or "")
        action = str(msg.get("action") or "").strip()
        host = str(msg.get("host") or "").strip()
        name = str(msg.get("session_name") or "").strip()
        spec_id = str(msg.get("spec_id") or "").strip()
        if action not in {"attach", "detach"}:
            raise VerbError("invalid_action", "action must be attach or detach")
        if not host or not name:
            raise VerbError("invalid_stream_id", "host and session_name are required")
        if not SPEC_ID_RE.fullmatch(spec_id):
            raise VerbError("invalid_spec_id", "spec_id must match <repo>__<topic>")
        resolver = getattr(self._spec_catalog, "resolution_for", None) if self._spec_catalog is not None else None
        if not callable(resolver):
            raise VerbError(
                "spec_unresolved",
                "requested spec-id cannot be resolved because the catalog is unavailable",
                spec_id=spec_id, spec_resolution=None,
            )
        resolution = resolver(spec_id)
        if resolution != "resolved":
            raise VerbError(
                "spec_unresolved",
                f"spec-id {spec_id} is not catalog-resolved ({resolution or 'unknown'})",
                spec_id=spec_id, spec_resolution=resolution,
            )
        canonicalizer = getattr(self._spec_catalog, "canonical_spec_identity", None)
        canonical_spec_id = canonicalizer(spec_id) if callable(canonicalizer) else None
        if not canonical_spec_id:
            raise VerbError(
                "spec_unresolved",
                "spec-id resolved without a canonical document identity",
                spec_id=spec_id,
                spec_resolution="canonical_identity_missing",
            )
        spec_id = str(canonical_spec_id)

        stream_id = f"{host}:{name}"
        source = await self.store.fetch_session(host, name)
        if source is None:
            raise VerbError("session_not_found", "session does not exist", stream_id=stream_id)
        spec_ids = normalize_spec_ids(source.get("spec_ids"), source.get("spec_id"))
        bindings = normalize_spec_binding_provenance(
            source.get("spec_binding_provenance"), spec_ids=spec_ids
        )
        def canonical_identity(value: str | None) -> str | None:
            if not callable(canonicalizer):
                return None
            try:
                identity = canonicalizer(value)
            except Exception:
                return None
            normalized = str(identity or "").strip()
            return normalized or None

        def same(left: str, right: str) -> bool:
            left_identity = canonical_identity(left)
            right_identity = canonical_identity(right)
            return bool(left_identity and left_identity == right_identity)

        if action == "attach":
            if not any(same(existing, spec_id) for existing in spec_ids):
                spec_ids.append(spec_id)
            if not any(same(binding["spec_id"], spec_id) for binding in bindings):
                bindings.append({
                    "spec_id": spec_id,
                    "provenance": "operator_v2",
                    "granting_principal": str(msg.get("from_stream_id") or "operator").strip() or "operator",
                    "granted_at": iso_now(),
                })
        else:
            spec_ids = [existing for existing in spec_ids if not same(existing, spec_id)]
            bindings = [binding for binding in bindings if not same(binding["spec_id"], spec_id)]

        first_spec_id = spec_ids[0] if spec_ids else None
        updated = await self.store.update_session(
            host,
            name,
            spec_id=first_spec_id,
            spec_ids=spec_ids,
            spec_resolution=resolver(first_spec_id) if first_spec_id else None,
            spec_binding_provenance=bindings,
        )
        if updated is None:
            raise VerbError("session_not_found", "session disappeared during spec update", stream_id=stream_id)
        self.sessions.apply_durable(
            stream_id,
            **{key: value for key, value in updated.items() if key != "stream_id"},
        )
        return {
            "type": "session.spec_update.ok",
            "request_id": rid,
            "action": action,
            "session": updated,
        }


def _err(verb: str, rid: str, error: str, *, message: str = "") -> dict[str, Any]:
    reply = {"type": f"{verb}.error", "request_id": rid, "error": error}
    if message:
        reply["message"] = message
    return reply
