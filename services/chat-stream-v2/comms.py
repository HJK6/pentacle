"""comms.py - send/tell, questions (prompt), notifications/updates.

Contract (v2_design.md module table):
  owns: send/tell, questions (`prompt`), notifications/updates.

Binding requirements:
  - **Handoff-aware tell routing (operator-named must-keep).** A tell/send
    addressed to a dead or handed-off stream follows the handoff lineage
    (`resolve_route_target`-style, bounded depth) to the live successor.
    Carries a pinned regression.
  - `child_report_ready` is a top-level WS push type AND a lineage-routed pane
    tell - see `_artifacts/phase_c_entry_gates.md`. Both paths are required;
    keeping `chat.event` does not cover it.
  - B11: notifications carry TTLs; exactly one gated push emitter (the
    phone-spam SEV class); questions are single durable rows.
  - Retry simplifies to receipt-or-explicit-failure. v1's reply-type
    vocabulary is preserved for the verbs v2 keeps (`send.result`, ...) -
    mobile enumerates them by name. Two v1 uncertainty replies are gone by
    ruling: `close.degraded` (recovered to `close.ok` or an honest
    `close.failed`) and `send.indeterminate` (v2 never emits it).
  - No standing nexus awareness-delivery loop (design L10; the Phase C entry
    gate found zero programmatic consumers). `nexus.context` stays as a pull.

Notification-expiry loop rules: cadence configurable; per-pass cap on rows
expired; backoff on failure; kill switch `--disable-notification-expiry`.
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
import hashlib
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_jsonl_norm import strip_peer_delivery_envelope
from boot_ready import (
    CODEX_RESET_BLOCKED,
    DRAFT_PREDICATES,
    SUBMIT_PREDICATES,
    claude_prompt_in_active_draft,
    codex_prompt_in_active_draft,
    codex_reset_interstitial_visible,
    codex_tui_session_visible,
    submission_proven_after,
)
from outbound_notices import ensure_notice_marker, notice_needle
from sessions import VerbError
from tmux_transport import RECEIPT_TIMEOUT_S, assert_injectable, receipt_needle, sanitize_injectable
from submission_events import (
    COMMITTED_PENDING_PROOF,
    DurableUserEventProof,
    EventProof,
    EventWatermark,
    PROOF_FAST_WAIT_S,
)
from v2_runtime import iso_now

log = logging.getLogger("chat_streamd_v2.comms")

ROUTE_MAX_DEPTH = 8

ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024
ATTACHMENT_ROOT = Path.home() / ".local/share/pentacle-stream/attachments"
SUPPORTED_ATTACHMENT_MIME = {"image/jpeg": "jpg", "image/png": "png"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_NOTICE_TOKEN_RE = re.compile(r"\[pentacle-notice:[^\]\s]+\]")


def is_codex_usage_command(value: object) -> bool:
    """Recognize the quota-reset slash command without retaining its body."""
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    first_line = stripped.splitlines()[0].strip() if stripped else ""
    return bool(first_line) and first_line.split(maxsplit=1)[0].casefold() == "/usage"


CODEX_INITIAL_PROMPT_PENDING = "initial_prompt_pending"


def _verified_operator_provenance(msg: dict[str, Any]) -> bool:
    """Use only the server-bound operator bit for provider reset actions.

    A wire ``operator``/``trusted`` claim is intentionally ignored.  The v2
    server attaches ``_auth_context`` after filtering wire fields; if a caller
    cannot present that distinguishable connection-bound provenance, the
    action fails closed as agent-originated.
    """
    auth = msg.get("_auth_context")
    return isinstance(auth, dict) and auth.get("operator_authenticated") is True


#: How long the post-paste submission probe watches the pane for evidence that
#: the message left the draft.
SUBMISSION_EVIDENCE_TIMEOUT_S = 4.0
SUBMISSION_EVIDENCE_POLL_S = 0.25


def watermark_fields(watermark: EventWatermark) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "proof_watermark": watermark.daemon_seq,
        "proof_watermark_state": watermark.state,
    }
    if watermark.reason:
        fields["proof_watermark_reason"] = watermark.reason
    return fields


def payload_digest(target: str, body: str) -> str:
    """What a `tell_id` is allowed to stand for: one body, delivered to one
    stream. The target is the RESOLVED final stream, not the address the caller
    typed, so a lineage change makes the digest differ (QA #13)."""
    return hashlib.sha256(f"{target}\x00{body}".encode()).hexdigest()


def _normalize_pane(text: str) -> str:
    cleaned = _ANSI_RE.sub("", text or "").replace("\r", "").replace("\u00a0", " ")
    return "\n".join(line.rstrip() for line in cleaned.split("\n")).strip()


def _same_submission_prompt(sent_text: str, echo_text: str) -> bool:
    """The pinned semantic matcher used only for immediate pane evidence."""
    sent_value = _normalize_pane(sent_text.replace("\r", "\n")).strip()
    echo_value = _normalize_pane(echo_text.replace("\r", "\n")).strip()
    if not sent_value or not echo_value:
        return False
    if sent_value == echo_value:
        return True
    return re.sub(r"\s+", " ", sent_value).strip() == re.sub(r"\s+", " ", echo_value).strip()


# Stamping this envelope is the only signal that makes both provider normalizers
# classify a peer delivery as kind:TELL (claude_jsonl_norm._PEER_TELL_RE, imported
# by the Codex normalizer), so the transcript renders it as a peer/agent row. The
# matching strip helper (strip_peer_delivery_envelope) lives with the normalizer.
def _peer_delivery_envelope(from_stream_id: str, anchor: str, anchor_id: str, payload: str) -> str:
    return f"[from {from_stream_id}] [{anchor}:{anchor_id}]\n{payload}"


class AttachmentValidationError(ValueError):
    pass


class AttachmentFetchError(RuntimeError):
    pass


class AttachmentStageError(RuntimeError):
    pass


def _attachment_ext(mime: object) -> str:
    try:
        return SUPPORTED_ATTACHMENT_MIME[str(mime)]
    except KeyError:
        raise AttachmentValidationError(f"unsupported mime: {mime!r}") from None


def _attachment_sha(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA256_RE.fullmatch(value.lower()))


def _optional_attachment_int(item: dict, field: str, index: int, *, max_value: int | None = None) -> int | None:
    raw = item.get(field)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise AttachmentValidationError(f"attachments[{index}] {field} must be an integer")
    if raw < 0:
        raise AttachmentValidationError(f"attachments[{index}] {field} must be non-negative")
    if max_value is not None and raw > max_value:
        raise AttachmentValidationError(f"attachments[{index}] {field} exceeds max {max_value}")
    return raw


def validate_send_attachments(value: object, *, max_bytes: int = ATTACHMENT_MAX_BYTES) -> list[dict]:
    if not isinstance(value, list) or not value:
        raise AttachmentValidationError("attachments must be a non-empty list")
    if len(value) > 5:
        raise AttachmentValidationError("at most 5 attachments per send")
    normalized: list[dict] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise AttachmentValidationError(f"attachments[{index}] must be an object")
        key = item.get("key")
        if not _attachment_sha(key):
            raise AttachmentValidationError(f"attachments[{index}] key must be a 64-hex sha256 blob ref")
        key = str(key).lower()
        mime = item.get("mime")
        _attachment_ext(mime)
        supplied_sha = item.get("sha256")
        if supplied_sha is not None:
            if not _attachment_sha(supplied_sha) or str(supplied_sha).lower() != key:
                raise AttachmentValidationError(f"attachments[{index}] sha256 must match key")
        entry: dict[str, Any] = {"key": key, "mime": mime}
        for field in ("width", "height"):
            number = _optional_attachment_int(item, field, index)
            if number is not None:
                entry[field] = number
        declared = _optional_attachment_int(item, "bytes", index, max_value=max_bytes)
        if declared is not None:
            entry["bytes"] = declared
        if supplied_sha is not None:
            entry["sha256"] = key
        normalized.append(entry)
    return normalized


def build_attachment_inject_text(paths: list[str], caption: str) -> str:
    if not paths:
        raise AttachmentValidationError("at least one attachment path is required")
    caption = (caption or "").strip()
    noun = "file" if len(paths) == 1 else "files"
    joined = ", ".join(paths)
    if caption:
        return f"Look at the image {noun} at {joined}, then respond to the user's message: {caption}"
    return f"Image at {joined}" if len(paths) == 1 else f"Images at {joined}"


class _AttachmentMaterializer:
    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        self.root = Path(root or ATTACHMENT_ROOT).expanduser()
        self.max_bytes = ATTACHMENT_MAX_BYTES

    @staticmethod
    def _write_local(root: Path, sha: str, ext: str, data: bytes) -> Path:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = root / f"{sha}.{ext}"
        if target.exists():
            try:
                if target.stat().st_size == len(data) and hashlib.sha256(target.read_bytes()).hexdigest() == sha:
                    os.chmod(target, 0o600)
                    return target
            except OSError:
                pass
        temporary = root / f".{sha}.{uuid.uuid4().hex}.partial"
        try:
            with temporary.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            os.chmod(target, 0o600)
        except OSError:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise
        return target

    async def _remote_path(self, hosts: Any, host: str, sha: str, ext: str) -> str:
        runner = getattr(hosts, "run_command", None)
        if not callable(runner):
            raise AttachmentStageError("remote attachment transport is unavailable")
        script = (
            "from pathlib import Path; "
            "p=Path.home()/'.cache/pentacle-stream/attachments'; "
            "p.mkdir(mode=0o700, parents=True, exist_ok=True); print(p)"
        )
        rc, output = await runner(host, "python3", "-c", script)
        if rc != 0:
            raise AttachmentStageError(output.strip() or "remote attachment directory creation failed")
        remote_dir = str(output or "").strip().splitlines()[-1] if str(output or "").strip() else ""
        if not remote_dir.startswith("/"):
            raise AttachmentStageError("remote attachment directory is not absolute")
        return f"{remote_dir}/{sha}.{ext}"

    async def materialize(
        self, attachments: list[dict], blob_store: Any, *, host: str, tmux: Any, hosts: Any,
    ) -> list[str]:
        paths: list[str] = []
        for attachment in attachments:
            sha = str(attachment["key"]).lower()
            ext = _attachment_ext(attachment["mime"])
            try:
                if blob_store is None or not callable(getattr(blob_store, "read_verified", None)):
                    raise ValueError("blob reader is unavailable")
                data = await blob_store.read_verified(sha, max_bytes=self.max_bytes)
                if len(data) > self.max_bytes:
                    raise ValueError("attachment exceeds byte limit")
            except Exception as exc:  # noqa: BLE001 - one typed send failure
                raise AttachmentFetchError(f"failed to materialize attachment {sha}") from exc
            if hosts is None or hosts.is_local(host):
                try:
                    local = await asyncio.to_thread(self._write_local, self.root, sha, ext, data)
                except Exception as exc:  # noqa: BLE001 - typed send failure
                    raise AttachmentFetchError(f"failed to write attachment {sha}") from exc
                paths.append(str(local))
                continue
            try:
                remote = await self._remote_path(hosts, host, sha, ext)
                await tmux.stage_text(remote, data)
            except Exception as exc:  # noqa: BLE001 - typed send failure
                if isinstance(exc, AttachmentStageError):
                    raise
                raise AttachmentStageError(f"failed to stage attachment {sha}: {exc}") from exc
            paths.append(remote)
        return paths


@dataclass
class SendPlan:
    message: dict[str, Any]
    route: dict[str, Any]
    body: str
    display_text: str
    attachments: list[dict]
    wire_text: str
    optimistic_id: str | None


class Comms:
    """tell/send delivery through one per-resolved-pane input lock.

    Injection is spawnctl's atomic paste.  Codex tells may issue one bounded
    Enter-only recovery when the exact body is still structurally editable;
    submission proof is the delivered/Sent boundary for that provider.  The
    lock is deliberately owned here and shared by direct tells, durable
    outbound notices, and send.  Spawn delivery is pre-registration and stays
    outside this addressable-pane path.
    """

    def __init__(
        self, store: Any, sessions: Any, spawnctl: Any, hosts: Any = None,
        *, blob_store: Any = None, attachment_root: str | os.PathLike[str] | None = None,
        submission_proof: Any = None,
    ) -> None:
        self.store = store
        self.sessions = sessions
        self.spawnctl = spawnctl
        #: The probe pool + transport seam. When present, a tell whose resolved
        #: target is a peer is fenced behind `ensure_reachable` and injected over
        #: that peer's `tmux_for`. None keeps tell localhost-only (unit suite).
        self.hosts = hosts
        self.submission_proof = submission_proof or DurableUserEventProof(
            store, local_host=str(getattr(sessions, "local_host", "")),
        )
        self.blob_store = blob_store
        self.attachment_materializer = _AttachmentMaterializer(attachment_root)
        #: tell_ids whose delivery is IN FLIGHT, each paired with the digest of
        #: what it is delivering. The durable table remembers completed
        #: deliveries; this covers the retry that matters most — the one fired
        #: while the first attempt is still waiting for its receipt.
        self._inflight: dict[str, tuple[str, asyncio.Future]] = {}
        #: Exactly one input lock per resolved pane.  Route resolution happens
        #: before this map is touched; all baseline capture, paste, Enter-only
        #: recovery, and final evidence for a pane occur under this lock.
        self._pane_input_locks: dict[str, asyncio.Lock] = {}
        # The observer seam is attached after bind-first construction. A single
        # queue/drain serializes buffered replays with live arrivals even while
        # an observer awaits broadcasts, so receipt order cannot invert.

    def _pane_input_lock(self, target: str) -> asyncio.Lock:
        """Return the Comms-owned lock for one resolved ``host:session``."""
        return self._pane_input_locks.setdefault(target, asyncio.Lock())

    async def _watermark_before_action(
        self, stream_id: str,
    ) -> EventWatermark:
        """Take a bounded pre-action watermark when the proof supports it.

        Test and rollout doubles from before the retry API expose only
        ``watermark()``. Keep that fallback one-shot while production proof
        uses its bounded retry window.
        """
        waiter = getattr(self.submission_proof, "wait_for_watermark", None)
        if callable(waiter):
            return await waiter(stream_id, timeout_s=PROOF_FAST_WAIT_S)
        return await self.submission_proof.watermark(stream_id)

    async def resolve_route_target(self, stream_id: str, *, max_depth: int = ROUTE_MAX_DEPTH) -> dict[str, Any]:
        """A tell addressed to a handed-off stream follows the lineage to the
        live successor, bounded in depth and loop-guarded.

        Lineage comes from the successor row's `handoff_from_stream_id` — the
        only forward link the contracted `sessions` table carries. v1 also kept
        a `stream_progeny` table for non-handoff progeny; that path is not
        implemented here (see `spec.md` Phase C bootstrap state)."""
        hops = [stream_id]
        seen = {stream_id}
        current = stream_id
        for _ in range(max_depth):
            host, name = self.sessions.split(current)
            row = self.sessions.get(current) or await self.store.fetch_session(host, name)
            if row is not None and str(row.get("status") or "open") != "closed":
                break
            successor = await self.store.find_successor(current)
            if successor is None:
                break
            if successor in seen:
                hops.append(successor)
                return {"ok": False, "reason": "route_loop", "original_target": stream_id,
                        "final_target": current, "hops": hops, "forwarded": current != stream_id}
            hops.append(successor)
            seen.add(successor)
            current = successor
        else:
            return {"ok": False, "reason": "route_depth_exceeded", "original_target": stream_id,
                    "final_target": current, "hops": hops, "forwarded": current != stream_id}
        return {"ok": True, "reason": None, "original_target": stream_id,
                "final_target": current, "hops": hops, "forwarded": current != stream_id}

    # -- tell --------------------------------------------------------------

    async def tell(self, msg: dict[str, Any]) -> dict[str, Any]:
        """`tell_id` is the CALLER's idempotency key (the CLI documents it as
        one): a retry must never inject the message a second time.

        The key is bound to what was actually DELIVERED — the digest of the
        resolved target plus the body (QA #13). A reuse carrying the same
        payload replays the stored `tell.ok` (v1's duplicate vocabulary,
        unchanged). A reuse carrying a DIFFERENT body, or the same body now
        routing to a different stream, is `tell_id_conflict`: the daemon will
        not answer `.ok` for a message it never delivered.

        Routing and the host guard run BEFORE the dedupe lookup, so a replayed
        id can never be a way around `assert_local` or handoff lineage, and the
        digest is always compared against TODAY's routing, never the target the
        first attempt happened to land on.

        A transport failure is deliberately not remembered, so a retry after a
        real failure is still free to attempt delivery once. A Codex
        submission-unconfirmed result is different: its one paste is durable as
        ``pasted_unsubmitted`` and a same-ID replay performs no input.
        """
        tell_id = str(msg.get("tell_id") or "").strip() or uuid.uuid4().hex[:12]
        try:
            route, body = await self._route(msg)
        except VerbError as exc:
            if exc.code in {CODEX_RESET_BLOCKED, CODEX_INITIAL_PROMPT_PENDING}:
                await self._record_blocked_input(
                    msg, tell_id, exc, verb="tell", receipt=False,
                )
            raise
        # The idempotency digest hashes the ORIGINAL (unstamped) payload so a
        # tell_id retried across a daemon upgrade — where a prior row hashed the
        # bare body and this build would otherwise hash header+body — is still
        # recognized as the same payload instead of a spurious tell_id_conflict.
        # Only the injected wire text carries the peer-provenance envelope.
        digest = payload_digest(str(route["final_target"]), body)
        wire = self._peer_delivery_wire(msg, "tell", tell_id, body)

        prior = await self.store.get_tell_delivery(tell_id)
        if prior is not None:
            self._assert_same_payload(tell_id, digest, prior)
            return {**prior["reply"], "duplicate": True}
        pending = self._inflight.get(tell_id)
        if pending is not None:
            prior_digest, fut_pending = pending
            if prior_digest != digest:
                raise self._conflict(tell_id)
            kind, value = await fut_pending
            if kind == "error":
                raise value
            return {**value, "duplicate": True}

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[tell_id] = (digest, fut)
        outcome: tuple[str, Any] = ("error", VerbError("delivery_failed", "tell did not complete"))
        try:
            async with self._pane_input_lock(str(route["final_target"])):
                reply = await self._deliver_tell(msg, tell_id, route, wire, digest)
            outcome = ("ok", reply)
            return reply
        except Exception as exc:
            if isinstance(exc, VerbError) and exc.code == CODEX_RESET_BLOCKED:
                await self._record_blocked_input(
                    msg, tell_id, exc, verb="tell", receipt=False,
                )
            outcome = ("error", exc)
            raise
        finally:
            self._inflight.pop(tell_id, None)
            if not fut.done():
                fut.set_result(outcome)

    async def deliver_outbound_notice(
        self, msg: dict[str, Any], *, check_existing: bool = False
    ) -> dict[str, Any]:
        """Deliver one queued daemon notice with crash-after-paste recovery.

        A queue claim is durable before this method runs.  A retry performs an
        evidence-only lookup from the watermark stored with the first paste.
        It never consults pane chrome and never sends a second paste.
        """
        tell_id = str(msg.get("tell_id") or "").strip()
        route, body = await self._route(msg)
        target = str(route["final_target"])
        host, name = self.sessions.split(target)
        tmux = self.hosts.tmux_for(host) if self.hosts is not None else self.spawnctl.tmux
        marked_body = ensure_notice_marker(tell_id, body)
        if check_existing:
            async with self._pane_input_lock(target):
                prior = await self.store.get_tell_delivery(tell_id)
                digest = payload_digest(target, marked_body)
                if prior is not None:
                    self._assert_same_payload(tell_id, digest, prior)
                    delivery = prior.get("delivery") if isinstance(prior, dict) else None
                    if isinstance(delivery, dict) and delivery.get("proof_watermark") is not None:
                        watermark = EventWatermark(
                            target,
                            int(delivery.get("proof_watermark") or 0),
                            str(delivery.get("proof_watermark_state") or "reachable"),
                            str(delivery.get("proof_watermark_reason") or ""),
                        )
                        observed = await self.submission_proof.lookup(
                            target, expected_text=marked_body, watermark=watermark,
                        )
                    else:
                        observed = EventProof(
                            "unreachable", target, 0, reason="proof_watermark_missing",
                        )
                else:
                    observed = EventProof("unreachable", target, 0, reason="tell_row_missing")
                if observed.proven:
                    promoted = await self.store.promote_tell_delivery(
                        tell_id, digest, proof=observed.audit_fields(),
                    )
                    if promoted is not None:
                        reply = dict(promoted["reply"])
                        reply["duplicate"] = True
                        reply["already_present"] = True
                        return reply
                if prior is not None:
                    return {
                        **prior["reply"],
                        **observed.audit_fields(),
                        "duplicate": True,
                    }
                provider = await self._provider_for_route(route)
                if provider not in SUBMIT_PREDICATES:
                    pane = await tmux.capture(name)
                    if notice_needle(tell_id) in pane:
                        acknowledged_at = iso_now()
                        reply = self._tell_reply(
                            tell_id, route, submission_confirmed=True,
                            delivery_status="delivered", submission_attempts=1,
                            delivery_ack_at=acknowledged_at,
                        )
                        reply["already_present"] = True
                        delivery = {
                            "tell_id": tell_id,
                            "to_stream_id": target,
                            "text": marked_body,
                            "delivery_status": "delivered",
                            "delivery_attempts": 1,
                            "submission_attempts": 1,
                            "submission_confirmed": True,
                            "enqueued_at": acknowledged_at,
                            "delivered_at": acknowledged_at,
                            "delivery_ack_at": acknowledged_at,
                            "request_payload_hash": digest,
                        }
                        reply["ledger_row_id"] = await self.store.put_tell_delivery(
                            tell_id,
                            {"payload_digest": digest, "reply": reply, "delivery": delivery},
                        )
                        return reply
                # The durable outbound row is expected to have a tell row after
                # the initial attempt.  If a crash fell into that pre-persist
                # window, keep this recovery pass evidence-only and retryable.
                return self._tell_reply(
                    tell_id, route, submission_confirmed=False,
                    delivery_status="proof_unavailable", submission_attempts=0,
                    reason="submission_proof_pending",
                )
        return await self.tell({
            **msg,
            "message": marked_body,
            "stream_id": target,
            "_durable_notice_proof": True,
        })

    async def _deliver_tell(
        self, msg: dict[str, Any], tell_id: str, route: dict[str, Any], body: str, digest: str,
    ) -> dict[str, Any]:
        """Inject once, then durably record the provider's truth boundary."""
        binding = await self.store.exchange_binding(str(msg.get("from_stream_id") or msg.get("actor_stream_id") or ""), str(route["final_target"]))
        confirmed, attempts, provider, active_draft, proof, watermark = await self._attempt_delivery(
            msg, route, body,
        )
        durable_notice = msg.get("_durable_notice_proof") is True
        delivery_status = "delivered"
        reason = None
        if durable_notice and provider in SUBMIT_PREDICATES and not confirmed:
            # Durable notice paste committed; async proof is late. Non-fatal,
            # promotable via promote_tell_delivery when the event lands.
            delivery_status = COMMITTED_PENDING_PROOF
            reason = proof.reason or "submission_proof_pending"
        elif provider == "codex" and not confirmed:
            if active_draft:
                delivery_status = "pasted_unsubmitted"
                reason = "active_draft_present"
            else:
                # Paste left the composer (no active draft) but the post-
                # watermark USER event has not yet been ingested. Committed, not
                # failed: report the non-fatal, do_not_resubmit status.
                delivery_status = COMMITTED_PENDING_PROOF
                reason = "submission_proof_pending"
        ack_at = iso_now() if confirmed else None
        reply = self._tell_reply(
            tell_id,
            route,
            submission_confirmed=confirmed,
            delivery_status=delivery_status,
            submission_attempts=attempts,
            delivery_ack_at=ack_at,
            reason=reason,
            action_committed=not confirmed,
            confirmation_pending=not confirmed,
        )
        if durable_notice:
            reply.update(watermark_fields(watermark))
            reply.update(proof.audit_fields())
        enqueued_at = iso_now()
        delivery = {
            "tell_id": tell_id,
            "from_stream_id": str(msg.get("from_stream_id") or msg.get("actor_stream_id") or ""),
            "to_stream_id": str(route["final_target"]),
            "original_to_stream_id": str(route["original_target"]),
            "route_hops": list(route.get("hops") or []),
            "text": body,
            "delivery_status": delivery_status,
            "delivery_attempts": attempts,
            "submission_attempts": attempts,
            "submission_confirmed": confirmed,
            "enqueued_at": enqueued_at,
            "delivered_at": enqueued_at if delivery_status == "delivered" else None,
            "delivery_ack_at": ack_at,
            "request_payload_hash": digest,
            **(watermark_fields(watermark) if durable_notice else {}),
            **(proof.audit_fields() if durable_notice else {}),
            **(
                {
                    key: reply[key]
                    for key in (
                        "action_status", "confirmation_status", "action_committed",
                        "confirmation_pending", "do_not_resubmit", "reconcile",
                    )
                    if key in reply
                }
                if not confirmed else {}
            ),
        }
        from store_exchange import source
        delivery["exchange"] = source(binding, "tell", tell_id, strip_peer_delivery_envelope(body), enqueued_at)
        reply["ledger_row_id"] = await self.store.put_tell_delivery(
            tell_id, {"payload_digest": digest, "reply": reply, "delivery": delivery}
        )
        return reply

    def _tell_reply(
        self,
        tell_id: str,
        route: dict[str, Any],
        *,
        submission_confirmed: bool,
        delivery_status: str = "delivered",
        submission_attempts: int = 1,
        delivery_ack_at: str | None = None,
        reason: str | None = None,
        action_committed: bool = False,
        confirmation_pending: bool = False,
    ) -> dict[str, Any]:
        """The compatibility ``tell.ok`` envelope and its evidence fields."""
        reply = {
            "type": "tell.ok", "tell_id": tell_id,
            "delivery_status": delivery_status, "to_stream_id": str(route["final_target"]),
            "original_target": route["original_target"], "forwarded": route["forwarded"],
            "hops": route["hops"], "submission_confirmed": submission_confirmed,
            "submission_attempts": int(submission_attempts),
            "delivery_ack_at": delivery_ack_at,
        }
        if reason:
            reply["reason"] = reason
        if action_committed:
            reply.update({
                "action_status": "committed",
                "confirmation_status": "pending" if confirmation_pending else "confirmed",
                "action_committed": True,
                "confirmation_pending": confirmation_pending,
            })
            if confirmation_pending:
                reply.update({
                    "do_not_resubmit": True,
                    "reconcile": "tell_id_evidence_only",
                    "reconcile_command": f"agent-orch ledger get {tell_id}",
                    "retry_guidance": (
                        "Action is committed; confirmation is pending. "
                        "DO NOT RESUBMIT; reconcile with the existing tell_id."
                    ),
                })
        return reply

    @staticmethod
    def _conflict(tell_id: str) -> VerbError:
        return VerbError(
            "tell_id_conflict",
            f"tell_id {tell_id} was already used for a different message or a different "
            "target stream; reusing an idempotency key requires an identical payload",
        )

    def _assert_same_payload(self, tell_id: str, digest: str, prior: dict[str, Any]) -> None:
        """A stored record that predates the envelope has no digest to compare,
        so it cannot be proved to be the same message: fail closed rather than
        replay a `tell.ok` that may name a stream this call never reached."""
        if not isinstance(prior, dict) or prior.get("payload_digest") != digest:
            raise self._conflict(tell_id)

    async def _resolve_route(self, msg: dict[str, Any], *, verb: str) -> dict[str, Any]:
        """Resolve lineage, routing integrity, host fencing, and open-session state."""
        host, name = await self.sessions.resolve(msg)
        route = await self.resolve_route_target(f"{host}:{name}")
        if not route["ok"]:
            raise VerbError(route["reason"] or "route_failed", f"cannot route to {host}:{name}")
        target = str(route["final_target"])
        host, name = self.sessions.split(target)
        # Read the authoritative row before any peer reachability/RPC probe.
        # A rejected Codex reset or unresolved initial prompt must not even
        # open the remote transport that would precede pane input.
        row = await self.store.fetch_session(host, name)
        if row is None:
            row = self.sessions.get(target)
        if row is not None and str(row.get("status") or "") == "open":
            raw_body = msg.get("message") if "message" in msg else msg.get("text")
            await self._assert_codex_input_allowed(
                route,
                "" if raw_body is None else str(raw_body),
                msg,
            )

        # Injection pastes into `=<name>:` on the target's OWN host, so the host
        # must be resolved before delivery: a bare local paste would hit an
        # unrelated local pane that happens to share the name. A configured peer
        # is fenced behind a bounded reachability probe (offline -> fast
        # `host_offline`); without a `hosts` pool, the unit path stays local-only.
        if self.hosts is not None and not self.hosts.is_local(host):
            await self.hosts.ensure_reachable(host, verb)
        else:
            self.sessions.assert_local(host, verb)

        if row is None or str(row.get("status") or "") != "open":
            raise VerbError("unknown_session", f"{target} is not an open session")

        return route

    async def _route(self, msg: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """The tell route retains its pinned text-only validation contract."""
        route = await self._resolve_route(msg, verb="tell")
        body = str(msg.get("message") or msg.get("text") or "")
        if not body.strip():
            raise VerbError("bad_request", "message is required")
        # Opt-in only (QA #17): a tell explicitly relaying captured terminal
        # output (ANSI colour, a `capture-pane` excerpt — routine agent traffic)
        # sets `sanitize` to STRIP the control sequences instead of being
        # rejected. Absent the flag the behaviour is exactly as before —
        # `assert_injectable` rejects a raw ESC, so no caller ever silently gets
        # a mangled injection, and briefs/spawns (a different path) are untouched.
        if msg.get("sanitize"):
            body = sanitize_injectable(body)
            if not body.strip():
                raise VerbError("bad_request", "message is empty after sanitize")
        await self._assert_codex_input_allowed(route, body, msg)
        # Rejected before the urgent Escape, so a hostile tell leaves the target
        # pane completely untouched. After a sanitize this always passes; it
        # stays the single guarantee that nothing unsafe reaches the pane.
        assert_injectable(body)
        return route, body

    # -- the one injection path (submission-confirmed, busy-aware) ----------

    async def _provider_for_route(self, route: dict[str, Any]) -> str:
        target = str(route["final_target"])
        host, name = self.sessions.split(target)
        row = self.sessions.get(target) or await self.store.fetch_session(host, name)
        return str((row or {}).get("provider") or "")

    @staticmethod
    def _send_provenance(msg: dict[str, Any]) -> tuple[str | None, str | None, bool]:
        """Return bounded actor/from metadata without copying message content."""
        auth = msg.get("_auth_context")
        auth = auth if isinstance(auth, dict) else {}
        from_stream_id = str(
            msg.get("from_stream_id") or msg.get("actor_stream_id") or ""
        ).strip() or None
        actor_stream_id = str(
            auth.get("stream_id")
            or auth.get("service_actor")
            or auth.get("operator_principal")
            or from_stream_id
            or ""
        ).strip() or None
        actor_trusted = bool(
            auth.get("token_verified")
            or auth.get("service_authenticated")
            or auth.get("operator_authenticated")
        )
        return from_stream_id, actor_stream_id, actor_trusted

    @staticmethod
    def _peer_delivery_wire(msg: dict[str, Any], anchor: str, anchor_id: str, payload: str) -> str:
        """Wrap ``payload`` in a `[from …]` envelope for a genuine bound peer, else
        return it unchanged.

        The gate is a token-verified seat whose verified identity equals the
        claimed sender (`token_verified and stream_id == from_stream_id`). The
        server only sets `token_verified` after checking a seat-owned stream token
        against the store, and rejects a mismatched claim as WRONG_SEAT — so this
        binds the stamp to a proven peer. An OPERATOR (operator_authenticated but no
        seat token → token_verified False, empty stream_id) and a system producer
        (whose service_actor is an unverified claim) are deliberately excluded, so
        neither can spoof a peer attribution. Daemon housekeeping
        (`_durable_notice_proof`) and already-enveloped bodies are left bare.
        """
        from_stream_id, _actor, _actor_trusted = Comms._send_provenance(msg)
        auth = msg.get("_auth_context")
        auth = auth if isinstance(auth, dict) else {}
        verified_peer = bool(
            auth.get("token_verified")
            and from_stream_id
            and str(auth.get("stream_id") or "") == from_stream_id
        )
        anchor_id = str(anchor_id or "").strip()
        if (
            verified_peer
            and anchor_id
            and not msg.get("_durable_notice_proof")
            and strip_peer_delivery_envelope(payload) == payload
        ):
            return _peer_delivery_envelope(from_stream_id, anchor, anchor_id, payload)
        return payload

    async def _reconcile_codex_reset_state(
        self, target: str, host: str, name: str, row: dict[str, Any],
    ) -> dict[str, Any]:
        """Clear a stale reset block after observing a normal Codex session."""
        tmux = self.hosts.tmux_for(host) if self.hosts is not None else self.spawnctl.tmux
        try:
            pane = await tmux.capture(name)
        except Exception:  # noqa: BLE001 - capture ambiguity preserves the block
            return row
        if not codex_tui_session_visible(pane):
            return row

        if str(row.get("bootstrap_state") or "") != CODEX_RESET_BLOCKED:
            return row

        updated = await self.store.update_session(
            host,
            name,
            expected_generation=str(row.get("created_at") or ""),
            bootstrap_state="started",
        )
        if updated is None:
            return await self.store.fetch_session(host, name) or row
        self.sessions.apply_durable(target, bootstrap_state="started")
        return updated

    async def _assert_codex_pane_input_safe(self, target: str, pane: str) -> None:
        """Persist and reject the actual ``/usage`` selection surface."""
        if not codex_reset_interstitial_visible(pane):
            return
        host, name = self.sessions.split(target)
        row = await self.store.fetch_session(host, name)
        if row is not None:
            updated = await self.store.update_session(
                host,
                name,
                expected_generation=str(row.get("created_at") or ""),
                bootstrap_state=CODEX_RESET_BLOCKED,
            )
            if updated is not None:
                self.sessions.apply_durable(target, bootstrap_state=CODEX_RESET_BLOCKED)
        raise VerbError(
            CODEX_RESET_BLOCKED,
            "Codex /usage reset selection is visible; automated input is blocked",
            phase="not_started",
            readiness_reason=CODEX_RESET_BLOCKED,
            reset_blocked=True,
            retryable=False,
            nonretryable=True,
            action_committed=False,
            confirmation_pending=False,
            pane_preserved=True,
            startup_input_blocked=True,
            do_not_retry=True,
            target_stream_id=target,
        )

    async def _assert_codex_input_allowed(
        self, route: dict[str, Any], body: str, msg: dict[str, Any],
    ) -> None:
        """Fail closed before any tmux/RPC input can reach a Codex pane."""
        target = str(route["final_target"])
        host, name = self.sessions.split(target)
        row = await self.store.fetch_session(host, name)
        if row is None:
            row = self.sessions.get(target)
        if str((row or {}).get("provider") or "") != "codex":
            return
        operator = _verified_operator_provenance(msg)
        reset_state = str((row or {}).get("bootstrap_state") or "") == CODEX_RESET_BLOCKED
        usage_command = is_codex_usage_command(body)

        # A verified operator may still deliberately use the existing /usage
        # action. No wire-controlled operator claim reaches this branch because
        # `_verified_operator_provenance` reads only server auth.
        if operator and usage_command:
            return
        if reset_state and not usage_command:
            row = await self._reconcile_codex_reset_state(target, host, name, row or {})
            reset_state = str(row.get("bootstrap_state") or "") == CODEX_RESET_BLOCKED
        initial_pending = str((row or {}).get("bootstrap_state") or "") == "unproven"

        if reset_state or usage_command:
            code = CODEX_RESET_BLOCKED
            message = (
                "agent-originated /usage is disabled for Codex; a usage-limit reset "
                "requires a verified operator action"
                if usage_command else
                "Codex pane is reset-blocked; no automated input is permitted until "
                "a verified operator resolves the interstitial"
            )
        elif initial_pending:
            code = CODEX_INITIAL_PROMPT_PENDING
            message = (
                "Codex initial prompt submission is unproven; follow-up input is "
                "blocked until exact USER proof settles bootstrap"
            )
        else:
            return

        from_stream_id, actor_stream_id, actor_trusted = self._send_provenance(msg)
        reset = code == CODEX_RESET_BLOCKED
        raise VerbError(
            code,
            message,
            readiness_reason=CODEX_RESET_BLOCKED if reset else CODEX_INITIAL_PROMPT_PENDING,
            reset_blocked=reset,
            pending=not reset,
            retryable=False,
            nonretryable=True,
            action_committed=False,
            confirmation_pending=False,
            pane_preserved=True,
            startup_input_blocked=True,
            do_not_retry=True,
            target_stream_id=target,
            from_stream_id=from_stream_id,
            actor_stream_id=actor_stream_id,
            actor_trusted=actor_trusted,
        )

    async def _record_blocked_input(
        self,
        msg: dict[str, Any],
        request_id: str,
        exc: VerbError,
        *,
        verb: str = "send",
        receipt: bool = True,
    ) -> None:
        """Record blocked input without storing its body."""
        target = str(exc.extra.get("target_stream_id") or "").strip()
        if not target or ":" not in target:
            return
        from_stream_id, actor_stream_id, actor_trusted = self._send_provenance(msg)
        code = str(exc.code or CODEX_RESET_BLOCKED)
        if receipt:
            receipt_id = f"receipt-{uuid.uuid4()}"
            try:
                await self.store.append_send_receipt(
                    to_stream_id=target,
                    request_id=request_id,
                    receipt_id=receipt_id,
                    state="not_landed",
                    wire_text="",
                    display_text="",
                    attachments=[],
                    delivery="not_landed",
                    submission_confirmed=False,
                    reason=code,
                    attempts=0,
                    from_stream_id=from_stream_id,
                    actor_stream_id=actor_stream_id,
                    actor_trusted=actor_trusted,
                )
            except Exception:  # noqa: BLE001 - preserve the typed rejection
                log.exception("could not append blocked send receipt target=%s", target)
    async def _attempt_delivery(
        self, msg: dict[str, Any], route: dict[str, Any], body: str,
    ) -> tuple[bool, int, str, bool, EventProof, EventWatermark]:
        """Deliver ``body`` and return proof, attempts, provider, and active-draft evidence."""
        target = str(route["final_target"])
        host, name = self.sessions.split(target)
        row = self.sessions.get(target) or await self.store.fetch_session(host, name)
        provider = str((row or {}).get("provider") or "")
        tmux = self.hosts.tmux_for(host) if self.hosts is not None else self.spawnctl.tmux
        confirmed, attempts, active_draft, proof, watermark = await self._inject_body(
            tmux, name, body, provider, bool(msg.get("urgent")), target,
            durable_proof=msg.get("_durable_notice_proof") is True,
        )
        return confirmed, attempts, provider, active_draft, proof, watermark

    async def _inject_body(
        self, tmux: Any, name: str, body: str, provider: str, urgent: bool, stream_id: str,
        *, durable_proof: bool = False,
    ) -> tuple[bool, int, bool, EventProof, EventWatermark]:
        """Paste once, then perform only the bounded provider recovery."""
        # B6: `urgent` is SENDER-chosen — one Escape, then the message. The
        # caller holds the resolved-pane input lock while this entire sequence
        # runs. The baseline is captured after that optional Escape and before
        # the paste, so all proof is anchored to this input region.
        before: str | None = None
        if provider == "codex":
            before = await tmux.capture(name)
            await self._assert_codex_pane_input_safe(stream_id, before)
        if urgent:
            await tmux.run("send-keys", "-t", f"={name}:", "Escape")
            before = None
            if provider == "codex":
                before = await tmux.capture(name)
                await self._assert_codex_pane_input_safe(stream_id, before)
        if provider not in SUBMIT_PREDICATES:
            before = before if before is not None else await tmux.capture(name)
            await tmux.paste(name, body)
            confirmed = await self.spawnctl._await_marker(
                name, receipt_needle(body), RECEIPT_TIMEOUT_S, since=before, tmux=tmux,
            )
            watermark = EventWatermark(stream_id, 0, "not_required")
            proof = EventProof("proven" if confirmed else "pending", stream_id, 0)
            return confirmed, 1, False, proof, watermark
        before = before if before is not None else await tmux.capture(name)
        if durable_proof:
            watermark = await self._watermark_before_action(stream_id)
        else:
            legacy_watermark = await self._submission_watermark(stream_id) if provider == "codex" else 0
            watermark = EventWatermark(stream_id, legacy_watermark, "reachable")
        await tmux.paste(name, body)
        if durable_proof:
            proof = await self._durable_submission_evidence(
                stream_id, body, watermark=watermark,
            )
            confirmed = proof.proven
        else:
            confirmed = await self._submission_evidence(
                tmux, name, body, provider, baseline=before, stream_id=stream_id,
                watermark=watermark.daemon_seq,
            )
            proof = EventProof("proven" if confirmed else "pending", stream_id, watermark.daemon_seq)
        if confirmed or (not durable_proof and provider != "codex"):
            return confirmed, 1, False, proof, watermark
        if durable_proof:
            # Pane chrome is advisory for durable notices. The baseline wait
            # ends in a keyed proof_pending row and never authorizes input.
            return False, 1, False, proof, watermark

        # Tmux.paste already issued the first Enter.  A second Enter is legal
        # only when this exact body (or its exact collapsed placeholder) is
        # still in the editable Codex composer.  It is never a re-paste.
        pane = await tmux.capture(name)
        in_draft = (
            codex_prompt_in_active_draft(pane, body, exact=True)
            if provider == "codex" else DRAFT_PREDICATES[provider](pane, body)
        )
        if not in_draft:
            return False, 1, False, proof, watermark
        await self._send_enter(tmux, name)
        if durable_proof:
            proof = await self._durable_submission_evidence(
                stream_id, body, watermark=watermark,
            )
            confirmed = proof.proven
        else:
            confirmed = await self._submission_evidence(
                tmux, name, body, provider, baseline=before, stream_id=stream_id,
                watermark=watermark.daemon_seq,
            )
            proof = EventProof("proven" if confirmed else "pending", stream_id, watermark.daemon_seq)
        if confirmed:
            return True, 2, False, proof, watermark
        pane = await tmux.capture(name)
        in_draft = (
            codex_prompt_in_active_draft(pane, body, exact=True)
            if provider == "codex" else DRAFT_PREDICATES[provider](pane, body)
        )
        return False, 2, in_draft, proof, watermark

    async def _submission_evidence(
        self,
        tmux: Any,
        name: str,
        body: str,
        provider: str,
        *,
        baseline: str | None = None,
        stream_id: str = "",
        watermark: int = 0,
    ) -> bool:
        """Existing general send/tell pane receipt seam (not item4 authority)."""
        submitted = SUBMIT_PREDICATES[provider]
        deadline = time.monotonic() + SUBMISSION_EVIDENCE_TIMEOUT_S
        while True:
            pane = await tmux.capture(name)
            if (
                submission_proven_after(pane, baseline, body, provider)
                if baseline is not None
                else submitted(pane, body)
            ):
                return True
            if provider == "codex" and await self._submission_event_proven(
                stream_id, body, watermark,
            ):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(SUBMISSION_EVIDENCE_POLL_S)

    async def _durable_submission_evidence(
        self, stream_id: str, body: str, *, watermark: EventWatermark,
    ) -> EventProof:
        """Item4 authority: one exact post-watermark durable USER event."""
        return await self.submission_proof.wait(
            stream_id,
            expected_text=body,
            watermark=watermark,
            timeout_s=PROOF_FAST_WAIT_S,
        )

    async def _submission_watermark(self, stream_id: str) -> int:
        """Snapshot the highest durable event sequence visible before one paste."""
        tail = await self.store.fetch_session_event_tail(stream_id, limit=500)
        return max((self._daemon_seq(event) for event in tail), default=0)

    @staticmethod
    def _daemon_seq(event: dict[str, Any]) -> int:
        try:
            return max(0, int(event.get("daemon_seq") or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _tail_event_matches_submission(event: dict[str, Any], stream_id: str, body: str) -> bool:
        if str(event.get("stream_id") or "") != stream_id:
            return False
        if str(event.get("kind") or "") not in {"USER", "TELL"}:
            return False
        text = event.get("text")
        if not isinstance(text, str):
            return False
        # The normalizer stores only the header-stripped payload, so compare the
        # stripped body (a bare body is returned unchanged) to keep proof matching.
        if not _same_submission_prompt(strip_peer_delivery_envelope(body), text):
            return False
        return all(token in text for token in _NOTICE_TOKEN_RE.findall(body))

    async def _submission_event_proven(self, stream_id: str, body: str, watermark: int) -> bool:
        """Preserved non-item4 general send/tell event receipt seam."""
        if not stream_id:
            return False
        tail = await self.store.fetch_session_event_tail(stream_id, limit=500)
        return any(
            self._daemon_seq(event) > watermark
            and self._tail_event_matches_submission(event, stream_id, body)
            for event in tail
        )


    # -- send --------------------------------------------------------------

    async def _assert_claude_send_ready(self, route: dict[str, Any]) -> None:
        """Early V2 admission does not authorize input before bootstrap readiness."""
        target = str(route["final_target"])
        host, name = self.sessions.split(target)
        row = await self.store.fetch_session(host, name)
        if row is None:
            row = self.sessions.get(target)
        if (row or {}).get("provider") == "claude" and (row or {}).get("bootstrap_state") in {
            "queued", "starting", "failed",
        }:
            raise VerbError(
                "bootstrap_not_ready", "Claude session is not ready for input",
                phase="not_started", action_committed=False, confirmation_pending=False,
                target_stream_id=target,
            )

    async def _prepare_send_plan(self, msg: dict[str, Any]) -> SendPlan:
        """Prepare the canonical text-or-attachments plan without pane I/O."""
        route = await self._resolve_route(msg, verb="send")
        await self._assert_claude_send_ready(route)
        raw_body = msg.get("message") if "message" in msg else msg.get("text")
        body = "" if raw_body is None else str(raw_body)
        if msg.get("sanitize"):
            body = sanitize_injectable(body)

        # This check is deliberately before attachment validation/materialization
        # and before the first receipt acceptance: a reset command must not turn
        # into any transport or pane action while an agent-originated reset is
        # being rejected.
        await self._assert_codex_input_allowed(route, body, msg)

        raw_attachments = msg.get("attachments")
        if raw_attachments is None or raw_attachments == []:
            attachments: list[dict] = []
        else:
            try:
                attachments = validate_send_attachments(raw_attachments)
            except AttachmentValidationError as exc:
                raise VerbError("attachment_invalid", str(exc)) from exc

        if msg.get("sanitize") and not body.strip() and not attachments:
            raise VerbError("bad_request", "message is empty after sanitize")
        if not body.strip() and not attachments:
            raise VerbError("bad_request", "message is required")
        if body:
            assert_injectable(body)
        display_text = body.strip() if attachments else body
        # Stamp wire_text only; display_text stays the operator-visible payload.
        # Attachments re-derive wire_text in _materialize_send_plan (re-stamped there).
        send_anchor = str(msg.get("request_id") or msg.get("msg_id") or "send")
        return SendPlan(
            message=msg,
            route=route,
            body=body,
            display_text=display_text,
            attachments=attachments,
            wire_text=self._peer_delivery_wire(msg, "send", send_anchor, body),
            optimistic_id=str(msg.get("optimistic_id") or "") or None,
        )

    async def _materialize_send_plan(self, plan: SendPlan) -> SendPlan:
        """Materialize and stage every attachment before the first paste."""
        if not plan.attachments:
            return plan
        target = str(plan.route["final_target"])
        host, name = self.sessions.split(target)
        tmux = self.hosts.tmux_for(host) if self.hosts is not None else self.spawnctl.tmux
        try:
            paths = await self.attachment_materializer.materialize(
                plan.attachments,
                self.blob_store,
                host=host,
                tmux=tmux,
                hosts=self.hosts,
            )
        except AttachmentStageError as exc:
            raise VerbError("attachment_stage_failed", str(exc)) from exc
        except AttachmentFetchError as exc:
            raise VerbError("attachment_fetch_failed", str(exc)) from exc
        wire_text = build_attachment_inject_text(paths, plan.display_text)
        send_anchor = str(plan.message.get("request_id") or plan.message.get("msg_id") or "send")
        wire_text = self._peer_delivery_wire(plan.message, "send", send_anchor, wire_text)
        assert_injectable(wire_text)
        return SendPlan(
            message=plan.message,
            route=plan.route,
            body=plan.body,
            display_text=plan.display_text,
            attachments=plan.attachments,
            wire_text=wire_text,
            optimistic_id=plan.optimistic_id,
        )

    async def _send_enter(self, tmux: Any, name: str) -> str | None:
        """Issue the one allowed Enter-only retry, never a second paste."""
        try:
            sender = getattr(tmux, "send_enter", None)
            if callable(sender):
                return await sender(name)
            rc, output = await tmux.run("send-keys", "-t", f"={name}:", "Enter")
            if rc != 0:
                raise VerbError("paste_failed", output.strip() or "send-keys Enter failed")
            return None
        except VerbError as exc:
            exc.extra.setdefault("phase", "enter_failed")
            raise
        except Exception as exc:  # noqa: BLE001 - public send error is stable
            raise VerbError("paste_failed", str(exc), phase="enter_failed") from exc

    async def _attempt_send_delivery(self, plan: SendPlan) -> tuple[bool, int, bool, str, str | None]:
        """Submit one prepared plan: one paste, then at most one Enter retry."""
        target = str(plan.route["final_target"])
        host, name = self.sessions.split(target)
        row = self.sessions.get(target) or await self.store.fetch_session(host, name)
        provider = str((row or {}).get("provider") or "")
        tmux = self.hosts.tmux_for(host) if self.hosts is not None else self.spawnctl.tmux
        before: str | None = None
        if provider == "codex":
            before = await tmux.capture(name)
            await self._assert_codex_pane_input_safe(target, before)
        if plan.message.get("urgent"):
            await tmux.run("send-keys", "-t", f"={name}:", "Escape")
            before = None
            if provider == "codex":
                before = await tmux.capture(name)
                await self._assert_codex_pane_input_safe(target, before)

        if provider not in SUBMIT_PREDICATES:
            before = before if before is not None else await tmux.capture(name)
            pane_mode_reason = await tmux.paste(name, plan.wire_text)
            confirmed = await self.spawnctl._await_marker(
                name, receipt_needle(plan.wire_text), RECEIPT_TIMEOUT_S, since=before, tmux=tmux,
            )
            if confirmed:
                return True, 1, False, provider, None
            enter_mode_reason = await self._send_enter(tmux, name)
            confirmed = await self.spawnctl._await_marker(
                name, receipt_needle(plan.wire_text), RECEIPT_TIMEOUT_S, since=before, tmux=tmux,
            )
            return confirmed, 2, False, provider, pane_mode_reason or enter_mode_reason

        before = before if before is not None else await tmux.capture(name)
        watermark = await self._submission_watermark(target) if provider == "codex" else 0
        pane_mode_reason = await tmux.paste(name, plan.wire_text)
        confirmed = await self._submission_evidence(
            tmux, name, plan.wire_text, provider, baseline=before,
            stream_id=target, watermark=watermark,
        )
        if confirmed:
            return True, 1, False, provider, None
        if provider == "claude":
            pane = await tmux.capture(name)
            if not claude_prompt_in_active_draft(pane, plan.wire_text):
                return False, 1, False, provider, pane_mode_reason
            enter_mode_reason = await self._send_enter(tmux, name)
            confirmed = await self._submission_evidence(
                tmux, name, plan.wire_text, provider, baseline=before,
            )
            if confirmed:
                return True, 2, False, provider, None
            pane = await tmux.capture(name)
            return False, 2, claude_prompt_in_active_draft(pane, plan.wire_text), provider, (
                pane_mode_reason or enter_mode_reason
            )
        if provider != "codex":
            enter_mode_reason = await self._send_enter(tmux, name)
            confirmed = await self._submission_evidence(
                tmux, name, plan.wire_text, provider, baseline=before,
            )
            if confirmed:
                return True, 2, False, provider, None
            pane = await tmux.capture(name)
            return False, 2, (
                claude_prompt_in_active_draft(pane, plan.wire_text) if provider == "claude" else False
            ), provider, pane_mode_reason or enter_mode_reason
        pane = await tmux.capture(name)
        if not codex_prompt_in_active_draft(pane, plan.wire_text, exact=True):
            return False, 1, False, provider, pane_mode_reason
        enter_mode_reason = await self._send_enter(tmux, name)
        confirmed = await self._submission_evidence(
            tmux, name, plan.wire_text, provider, baseline=before,
            stream_id=target, watermark=watermark,
        )
        if confirmed:
            return True, 2, False, provider, None
        pane = await tmux.capture(name)
        return False, 2, codex_prompt_in_active_draft(pane, plan.wire_text, exact=True), provider, (
            pane_mode_reason or enter_mode_reason
        )

    async def _append_send_receipt(
        self,
        plan: SendPlan,
        *,
        request_id: str,
        receipt_id: str,
        state: str,
        delivery: str,
        submission_confirmed: bool,
        reason: str | None = None,
        attempts: int | None = None,
    ) -> dict[str, Any]:
        """Append the receipt record; nothing in this lane updates a prior row."""
        from_stream_id, actor_stream_id, actor_trusted = self._send_provenance(plan.message)
        return await self.store.append_send_receipt(
            to_stream_id=str(plan.route["final_target"]),
            request_id=request_id,
            receipt_id=receipt_id,
            state=state,
            wire_text=plan.wire_text,
            display_text=plan.display_text,
            attachments=plan.attachments,
            delivery=delivery,
            submission_confirmed=submission_confirmed,
            optimistic_id=plan.optimistic_id,
            reason=reason,
            attempts=attempts,
            from_stream_id=from_stream_id,
            actor_stream_id=actor_stream_id,
            actor_trusted=actor_trusted,
        )

    async def _send_result(
        self,
        plan: SendPlan,
        *,
        request_id: str,
        receipt_id: str,
        state: str,
        delivery: str,
        submission_confirmed: bool,
        reason: str | None = None,
        attempts: int | None = None,
        action_committed: bool = False,
        confirmation_pending: bool = False,
    ) -> dict[str, Any]:
        projection = await self._append_send_receipt(
            plan,
            request_id=request_id,
            receipt_id=receipt_id,
            state=state,
            delivery=delivery,
            submission_confirmed=submission_confirmed,
            reason=reason,
            attempts=attempts,
        )
        target = str(plan.route["final_target"])
        host, name = self.sessions.split(target)
        result: dict[str, Any] = {
            "type": "send.result", "host": host, "session_name": name,
            "msg_id": plan.message.get("msg_id"), "to_stream_id": target,
            "original_target": plan.route["original_target"],
            "forwarded": plan.route["forwarded"], "hops": plan.route["hops"],
            "receipt_id": receipt_id, "delivery": delivery,
            "submission_confirmed": submission_confirmed,
        }
        if submission_confirmed and attempts is not None:
            result["attempt"] = attempts
        elif attempts is not None:
            result["attempts"] = attempts
        if reason:
            result["reason"] = reason
        if action_committed:
            result.update({
                "action_status": "committed",
                "confirmation_status": "pending" if confirmation_pending else "confirmed",
                "action_committed": True,
                "confirmation_pending": confirmation_pending,
            })
            if confirmation_pending:
                result.update({
                    "do_not_resubmit": True,
                    "reconcile": "send_receipt",
                    "reconcile_command": (
                        f"agent-orch send-receipt {target} {request_id}"
                    ),
                    "retry_guidance": (
                        "Action is committed; confirmation is pending. "
                        "DO NOT RESUBMIT; reconcile with the existing request_id."
                    ),
                })
        return result

    async def _submit_send_plan(
        self, plan: SendPlan, *, request_id: str, receipt_id: str,
    ) -> dict[str, Any]:
        """Inject one accepted plan and append its known durable outcome."""
        target = str(plan.route["final_target"])
        try:
            host, name = self.sessions.split(target)
            row = await self.store.fetch_session(host, name)
            lifecycle = (
                self.sessions._lifecycle_lock(host, name)
                if (row or {}).get("provider") == "claude" else nullcontext()
            )
            # Match bootstrap publishers: lifecycle before pane input, never the reverse.
            async with lifecycle, self._pane_input_lock(target):
                await self._assert_claude_send_ready(plan.route)
                confirmed, attempts, active_draft, provider, pane_mode_reason = await self._attempt_send_delivery(plan)
        except VerbError as exc:
            phase = str(exc.extra.get("phase") or "body_maybe_pasted")
            if phase == "not_started":
                return await self._send_result(
                    plan, request_id=request_id, receipt_id=receipt_id,
                    state="not_landed", delivery="not_landed", submission_confirmed=False,
                    reason=str(exc.code),
                )
            return await self._send_result(
                plan, request_id=request_id, receipt_id=receipt_id,
                state="accepted", delivery=COMMITTED_PENDING_PROOF, submission_confirmed=False,
                reason=phase,
                action_committed=True, confirmation_pending=True,
            )
        except Exception:  # noqa: BLE001 - body may have reached the pane
            return await self._send_result(
                plan, request_id=request_id, receipt_id=receipt_id,
                state="accepted", delivery=COMMITTED_PENDING_PROOF, submission_confirmed=False,
                reason="body_maybe_pasted",
                action_committed=True, confirmation_pending=True,
            )

        reason = pane_mode_reason or ("active_draft" if active_draft else "submit_unconfirmed")
        if confirmed:
            return await self._send_result(
                plan, request_id=request_id, receipt_id=receipt_id,
                state="landed", delivery="landed", submission_confirmed=True,
                attempts=attempts,
            )
        if provider != "codex" or active_draft:
            return await self._send_result(
                plan, request_id=request_id, receipt_id=receipt_id,
                state="not_landed", delivery="not_landed", submission_confirmed=False,
                reason=reason, attempts=attempts,
                action_committed=True, confirmation_pending=True,
            )
        return await self._send_result(
            plan, request_id=request_id, receipt_id=receipt_id,
            state="accepted", delivery=COMMITTED_PENDING_PROOF, submission_confirmed=False,
            reason=reason, attempts=attempts,
            action_committed=True, confirmation_pending=True,
        )

    async def send(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Append acceptance before materialization, then append the known result."""
        # Wire clients mint this ID. The fallback preserves direct in-process
        # callers during the mobile rollout; it deliberately adds no dedupe.
        request_id = str(msg.get("request_id") or f"send-{uuid.uuid4()}")
        # Bind the peer-delivery envelope anchor to the receipt identity: the
        # normalized event carries request_id as raw.tell_id, and the receipt
        # projection correlates on it. Without this, a fallback anchor (msg_id)
        # could diverge from the receipt request_id and defeat correlation.
        if msg.get("request_id") != request_id:
            msg = {**msg, "request_id": request_id}
        try:
            plan = await self._prepare_send_plan(msg)
        except VerbError as exc:
            if exc.code in {CODEX_RESET_BLOCKED, CODEX_INITIAL_PROMPT_PENDING, "bootstrap_not_ready"}:
                await self._record_blocked_input(msg, request_id, exc)
            raise
        receipt_id = f"receipt-{uuid.uuid4()}"
        await self._append_send_receipt(
            plan,
            request_id=request_id,
            receipt_id=receipt_id,
            state="accepted",
            delivery="accepted",
            submission_confirmed=False,
        )
        try:
            materialized = await self._materialize_send_plan(plan)
        except VerbError as exc:
            return await self._send_result(
                plan, request_id=request_id, receipt_id=receipt_id,
                state="not_landed", delivery="not_landed", submission_confirmed=False,
                reason=str(exc.code),
            )
        return await self._submit_send_plan(
            materialized, request_id=request_id, receipt_id=receipt_id,
        )
