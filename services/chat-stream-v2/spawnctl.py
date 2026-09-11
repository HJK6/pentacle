"""spawnctl.py - spawn, await-spawn, receipt-confirmed brief delivery.

Contract (v2_design.md module table):
  owns: spawn/await-spawn, receipt-confirmed brief delivery, crash-intent
        identity and settlement.
  notes: **boot-readiness gate only** (B1: provider CLI up and accepting input,
         else the brief lands in a shell) - distinct from turn-idleness
         watching, which stays banned (ledger req 1). Explicit receipt event;
         no evidence-tuple machinery.

Binding requirements:
  - Human-equivalent injection (ledger req 1): send straight into the pane and
    let the provider CLI's own input queue handle an active turn. One atomic
    bracketed-paste + Enter, no stray control sequences, no multi-chunk sends.
    Receipt = observed echo in the pane/transcript. Daemon-side
    wait-for-idle/readiness gating is BANNED.
  - B6 urgent interrupt: sender-chosen `--urgent` = one Escape then the
    message. The sender decides; no daemon turn-heuristics.
  - B3: the spawn envelope carries the declared env + caller identity; the
    `_validate_environment`-style boot check stays.
  - The spawn receipt echoes the effective model/profile at launch (arm (a) of
    the removed Codex runtime guard).
  - Reserved stream-id fencing is enforced via `sessions.py` (ledger req 4).

**No autonomous agent-spawning remediation** (spec constraint 3): nothing here
may be triggered by a daemon-internal failure signal.

"""

from __future__ import annotations

from _shared.spawn_objective import objective_error, objective_required_for, resolve_objective

import asyncio
import tmux_transport
import hashlib
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, NamedTuple

from boot_ready import (
    CODEX_RESET_BLOCKED,
    READY_PREDICATES,
    SUBMIT_PREDICATES,
    codex_reset_interstitial_visible,
    codex_tui_readiness,
)
from prockill import process_records, process_tree
from session_names import is_ephemeral_probe_session
from sessions import VerbError
from seat_token_telemetry import SeatTokenTelemetry
from submission_events import (
    DurableUserEventProof,
    EventProof,
    EventWatermark,
    PROOF_FAST_WAIT_S,
    PROOF_TERMINAL_BOUND_S,
    SPAWN_SUBMISSION_PROOF_BOUND_S,
    REMOTE_EVENT_LOOKUP_BOUND_S,
)
from v2_runtime import iso_now

import launch

SERVICES_ROOT = str(Path(__file__).resolve().parents[1])
if SERVICES_ROOT not in sys.path:  # `_shared` is the fleet-wide module, never a v2 copy
    sys.path.insert(0, SERVICES_ROOT)

from _shared.spawn_profiles import (  # noqa: E402
    HANDOFF_TUPLE_FIELDS,
    SpawnProfileError,
    boot_limits,
    resolve_handoff,
    resolve_spawn,
    validate_v2,
)
from store import STREAM_TOKEN_HASH_VERSION, normalize_spec_binding_provenance, normalize_spec_ids  # noqa: E402

log = logging.getLogger("chat_streamd_v2.spawnctl")

RESERVATION_TTL_S = 180.0
RESERVED_LIFECYCLE_ACTORS = frozenset({"daemon:scheduler"})
BOOT_READY_HARD_DEADLINE_S = 180.0
CREATION_PROBE_TIMEOUT_S = BOOT_READY_HARD_DEADLINE_S
# Remote proof performs capture, transcript, and liveness reads over SSH. Give
# those serialized transports three local receipt windows without adding a
# deployment knob; the Enter retry remains bounded to one attempt.
REMOTE_RECEIPT_TIMEOUT_FACTOR = 3.0


class BootReadyOutcome(NamedTuple):
    ready: bool
    elapsed_s: float
    polls: int
    reason: str | None = None


class SpawnQueueTimeout(VerbError):
    """A Codex boot permit did not become available within its fixed budget."""

    def __init__(self, host: str, timeout_s: float) -> None:
        super().__init__("spawn_queue_timeout", f"Codex boot queue on {host} exceeded {timeout_s}s")
# Prompt-bearing spawns get one bounded second chance for boot/transport
# failures. A confirmed prompt submit failure is terminal after its Enter-only
# retry: starting another pane would duplicate the provider-side operation.
# A value of two means at most two total boot cycles.
PROMPT_SPAWN_RETRY_ATTEMPTS = 2
#: Rollback pane-kill retry budget. A single best-effort `kill-session` can time
#: out under the very load that slowed the boot, orphaning the pane we created
#: (3 codex orphans observed on the boot_not_ready flap). Retry+verify so a
#: transient tmux stall never strands a live TUI; a pane that survives every
#: attempt is left to the SessionReconciler (v2 never SIGKILLs blind).
ROLLBACK_KILL_ATTEMPTS = 3
ROLLBACK_KILL_RETRY_S = 0.3
# Stable, non-secret marker emitted by the remote-host Claude launch shim when its
# SSM/keychain prerequisite cannot establish an auth context.  This is a
# public SpawnCtl outcome code, not a shim-internal vocabulary.
AUTH_CONTEXT_FAILURE_MARKER = "provider_auth_context_unavailable:"
# Lane 5's remote-host shim uses this exact, non-secret artifact only for its
# failure prelude.  Its path never includes the raw stream id: a session name
# is operator-controlled text, so the agreed SHA-256 derivation is part of the
# cross-host contract, not an implementation detail.
AUTH_CONTEXT_MARKER_HOST = os.environ.get("PENTACLE_AUTH_CONTEXT_MARKER_HOST", "").strip()
AUTH_CONTEXT_MARKER_DIR = "/tmp/pentacle-auth-context"
AUTH_CONTEXT_MARKER_BYTES = b"provider_auth_context_unavailable\n"
AUTH_CONTEXT_MARKER_READ_BYTES = 64
AUTH_CONTEXT_MARKER_TIMEOUT_S = 5.0
#: How long a real-provider brief must stay PROVEN-unsubmitted (viewport shows
#: it in the active draft, or the transcript is located but lacks it) before the
#: receipt retries Enter once. Long enough that a healthy submit's in-flight
#: transcript write / viewport repaint lands first; short enough to recover the
#: race well inside tmux_transport.RECEIPT_TIMEOUT_S without duplicating the prompt body.
RESUBMIT_GRACE_S = 2.0
#: How long adoption re-checks for a provider transcript to appear before it
#: rules the agent never started (QA #16 FINAL / determinism item (a)). A live
#: pane that never creates a session log inside this window is `agent_never_started`.
ADOPTION_BOOT_DEADLINE_S = 20.0
# Reserve part of the remaining adoption budget for the authoritative Store
# read that follows the advisory transcript probe.  The slice is capped by the
# remote event-read bound and shrinks with short deterministic test deadlines.
ADOPTION_FINAL_EVENT_LOOKUP_FRACTION = 0.10
#: The creation-nonce environment variable injected into every spawned pane
#: (spec §D1). Readable off the live pane's process environment from the instant
#: the pane exists — the identity signal that survives the F1 crash window.
PANE_NONCE_ENV = "PENTACLE_SPAWN_NONCE"
#: Kept well under the narrowest pane we expect to meet (40 columns) so a
#: needle can never be longer than a single unwrapped row.

#: Path fragments of the provider CLIs' durable session logs (QA #16 ruling
#: upgrade). A submitted brief is a user message here — evidence that never
#: scrolls off or repaints, unlike a pane capture.
#:   claude: ~/.claude/projects/<project>/<session>.jsonl
#:   codex:  ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
class SpawnCtl:
    """spawn / await_spawn + receipt-confirmed brief delivery."""

    def __init__(
        self, store: Any, sessions: Any, tmux: tmux_transport.Tmux | None = None,
        default_command: str = "", machine: "launch.LocalMachine | None" = None,
        hosts: Any = None, prompt_blobs: Any = None, specs: Any = None,
        token_telemetry: SeatTokenTelemetry | None = None,
        owner_instance_id: str = "",
        submission_proof: Any = None,
    ) -> None:
        self.store = store
        self.sessions = sessions
        self.window_schedule = None
        self.tmux = tmux or tmux_transport.Tmux()
        self.default_command = default_command
        #: Durable ownership token (spec §D3/INV-2): this daemon instance's
        #: `DaemonLifecycle.instance_id`, stamped onto every reservation this
        #: run claims. Adoption is owner-gated — a reconcile pass adopts only a
        #: reservation whose owner is NOT this live instance, so it can never
        #: race a finishing in-process spawn (the durable replacement for the
        #: rejected candidate's in-memory `_active_spawn_keys` fence). Empty in
        #: the unit suite, where no concurrent SessionReconciler exists.
        self.instance_id = owner_instance_id
        #: Local machine profile for the tuple-path launch-command + env
        #: construction (item 1). None only for the explicit-`command` path
        #: (the smoke tier); a tuple spawn without it is `spawn_launch_unavailable`.
        self.machine = machine
        #: The probe pool + transport seam. When present, a spawn whose `host`
        #: is a peer is fenced behind `ensure_reachable` and driven over that
        #: peer's `tmux_for`. None keeps v2 localhost-only (the unit suite).
        self.hosts = hosts
        self.submission_proof = submission_proof or DurableUserEventProof(
            store, local_host=str(getattr(sessions, "local_host", "")),
        )
        #: BlobStore is injected by main.py so large `--initial-prompt-file`
        #: uploads are dereferenced before the spawn reservation is settled.
        self.prompt_blobs = prompt_blobs
        #: The shared shared-memory catalog is read-only here. Spawn owns
        #: binding validation, while catalog ownership remains with the lifted
        #: specs subsystem.
        self.specs = specs
        self.token_telemetry = token_telemetry or SeatTokenTelemetry()
        # Per-reserved-stream submit-key count used to make failure evidence
        # truthful. Stream reservations prevent two live confirmations from
        # sharing a key on this controller.
        self._submission_attempts: dict[str, int] = {}
        self._submission_proof_failures: dict[str, str] = {}
        #: A disconnected requester must not cancel the daemon's spawn
        #: obligation. The task remains owned here until it records a terminal
        #: delivered/failed outcome and cleans up any pane it created.
        self._background_spawns: set[asyncio.Task[Any]] = set()
        self._codex_boot_semaphores: dict[str, asyncio.Semaphore] = {}
        #: The existing SpawnCtl is the sole intent owner. This lock serializes
        #: startup, recurring, and test-triggered passes inside one daemon; the
        #: reservation owner CAS below fences an overlapping process.
        self._intent_reconcile_lock = asyncio.Lock()
        #: One actor-local key rotates the single recurring external operation.
        #: It is scheduling state, not a retry queue or durable cursor.
        self._intent_reconcile_cursor = ""

    def _codex_boot_semaphore(self, host: str, cap: int) -> asyncio.Semaphore:
        return self._codex_boot_semaphores.setdefault(host, asyncio.Semaphore(cap))

    def boot_queue_depths(self) -> dict[str, int]:
        return {host: len(semaphore._waiters or ()) for host, semaphore in self._codex_boot_semaphores.items()}

    async def _persist_reset_blocked_spawn(
        self,
        host: str,
        name: str,
        request_id: str,
        open_flds: dict[str, Any],
        brief: str,
        delivery_receipt: dict[str, Any],
        *,
        spawn_generation: str,
        idempotency_key: str,
        payload_hash: str,
        operator_initiated_top_level: bool,
    ) -> dict[str, Any]:
        """Persist a Codex reset block while preserving the live pane.

        A reset offer is neither a normal boot timeout nor a delivery failure:
        the pane is still addressable, but no daemon-originated input may be
        sent until an operator resolves the provider decision.  Keep the same
        reservation/intent as the recovery handle and write the typed state to
        every read path used after a daemon restart.
        """
        receipt = {
            **delivery_receipt,
            "state": "blocked",
            "delivery_status": "blocked",
            "failure_code": CODEX_RESET_BLOCKED,
            "readiness_reason": CODEX_RESET_BLOCKED,
            "bootstrap_state": CODEX_RESET_BLOCKED,
            "submission_confirmed": False,
            "delivery_failed_at": iso_now(),
        }
        current = await self.store.fetch_session(host, name)
        expected_created_at = str((current or {}).get("created_at") or "")
        row = await self.store.update_session(
            host,
            name,
            expected_generation=expected_created_at,
            bootstrap_state=CODEX_RESET_BLOCKED,
            pane_status="pane_alive",
        )
        if row is not None:
            self.sessions.apply_durable(
                f"{host}:{name}",
                bootstrap_state=CODEX_RESET_BLOCKED,
                pane_status="pane_alive",
            )
        await self.store.record_spawn_intent(
            host,
            name,
            {
                "open_fields": open_flds,
                "brief": brief,
                "delivery_receipt": receipt,
                "readiness_state": CODEX_RESET_BLOCKED,
                "operator_initiated_top_level": operator_initiated_top_level,
            }, request_id=request_id,
        )
        await self.store.set_spawn_outcome(
            host,
            name,
            "failed",
            request_id=request_id,
            reason=CODEX_RESET_BLOCKED,
            delivery_evidence=CODEX_RESET_BLOCKED,
            delivery_receipt=receipt,
            effective_model=open_flds.get("effective_model"),
            effective_effort=open_flds.get("effective_effort"),
            idempotency_key=idempotency_key or None,
            request_payload_hash=payload_hash or None,
            readiness_timed_out=False,
        )
        return receipt

    async def _resolve_spec_binding(
        self, msg: dict[str, Any], host: str, name: str,
    ) -> dict[str, Any]:
        """Resolve and materialize the binding before reserving or creating a pane."""
        explicit = normalize_spec_ids(msg.get("spec_ids"), msg.get("spec_id"))
        if any(not tmux_transport.SPEC_ID_RE.fullmatch(spec_id) for spec_id in explicit):
            raise VerbError("invalid_spec_id", "spec_id must match <repo>__<topic>")

        inherited = False
        source_id = str(
            msg.get("handoff_from_stream_id") or msg.get("parent_stream_id") or ""
        ).strip()
        source_row: dict[str, Any] = {}
        if not explicit and source_id and ":" in source_id:
            source_host, source_name = source_id.split(":", 1)
            source_row = await self.store.fetch_session(source_host, source_name) or {}
            explicit = normalize_spec_ids(source_row.get("spec_ids"), source_row.get("spec_id"))
            inherited = bool(explicit)

        if not explicit:
            return {
                "spec_id": None,
                "spec_ids": [],
                "spec_resolution": None,
                "qualified_spec_ids": [],
                "spec_binding_provenance": [],
            }

        resolver = getattr(self.specs, "resolution_for", None) if self.specs is not None else None
        if not callable(resolver):
            raise VerbError(
                "spec_unresolved",
                "requested spec-id cannot be resolved: catalog unavailable; "
                "live work/ tree unavailable; catalog may be stale; regenerate the catalog "
                "after Syncthing converges",
                spec_id=explicit[0], spec_resolution=None,
            )
        spawn_resolver = getattr(self.specs, "resolve_for_spawn", None)
        canonicalizer = getattr(self.specs, "canonical_spec_identity", None)
        canonical_explicit: list[str] = []
        for spec_id in explicit:
            details = spawn_resolver(spec_id) if callable(spawn_resolver) else None
            if isinstance(details, dict):
                resolution = details.get("resolution")
                catalog_resolution = details.get("catalog_resolution")
                tree_resolution = details.get("tree_resolution")
                resolution_source = str(details.get("source") or "work_tree")
                candidates = list(details.get("tree_candidates") or [])
            else:
                resolution = resolver(spec_id)
                catalog_resolution = resolution
                tree_resolution = None
                resolution_source = "catalog"
                candidates = []
            if resolution != "resolved":
                lookup = (
                    f"catalog={catalog_resolution or 'unknown'}, "
                    f"live work/ tree={tree_resolution or 'unavailable'}"
                )
                candidate_hint = f"; live candidates: {', '.join(candidates)}" if candidates else ""
                raise VerbError(
                    "spec_unresolved",
                    f"spec-id {spec_id} unresolved after both lookups ({lookup}){candidate_hint}; "
                    "catalog may be stale; regenerate the catalog after Syncthing converges",
                    spec_id=spec_id,
                    spec_resolution=resolution,
                    spec_catalog_resolution=catalog_resolution,
                    spec_tree_resolution=tree_resolution,
                    spec_resolution_source=resolution_source,
                    spec_candidates=candidates,
                )
            canonical_spec_id = (
                str(details.get("canonical_spec_id") or "") if isinstance(details, dict) else ""
            )
            if not canonical_spec_id and callable(canonicalizer):
                canonical_spec_id = str(canonicalizer(spec_id) or "")
            if not canonical_spec_id:
                raise VerbError(
                    "spec_unresolved",
                    f"spec-id {spec_id} resolved without a canonical document identity",
                    spec_id=spec_id,
                    spec_resolution="canonical_identity_missing",
                    spec_resolution_source=resolution_source,
                    spec_candidates=candidates,
                )
            if canonical_spec_id not in canonical_explicit:
                canonical_explicit.append(canonical_spec_id)

        explicit = canonical_explicit

        granted_at = iso_now()
        if inherited:
            source_bindings = normalize_spec_binding_provenance(
                source_row.get("spec_binding_provenance"),
                spec_ids=source_row.get("spec_ids"),
            )

            def source_identity(value: str | None) -> str | None:
                if not callable(canonicalizer):
                    return None
                try:
                    identity = canonicalizer(value)
                except Exception:
                    return None
                normalized = str(identity or "").strip()
                return normalized or None

            provenance: list[dict[str, str]] = []
            for spec_id in explicit:
                source_binding = next(
                    (
                        binding for binding in source_bindings
                        if source_identity(binding["spec_id"]) == spec_id
                    ),
                    None,
                )
                if source_binding is None:
                    continue
                provenance.append({
                    "spec_id": spec_id,
                    "provenance": (
                        "handoff_inherited"
                        if msg.get("handoff_from_stream_id")
                        else "parent_inherited"
                    ),
                    "granting_principal": source_id,
                    "granted_at": granted_at,
                })
            if len(provenance) != len(explicit):
                missing = next(
                    spec_id for spec_id in explicit
                    if not any(binding["spec_id"] == spec_id for binding in provenance)
                )
                raise VerbError(
                    "spec_unresolved",
                    f"inherited spec-id {missing} has no qualified source binding",
                    spec_id=missing, spec_resolution="source_binding_missing",
                )
        else:
            principal = str(
                msg.get("caller_stream_id")
                or msg.get("from_stream_id")
                or "operator"
            ).strip() or "operator"
            provenance = [
                {
                    "spec_id": spec_id,
                    "provenance": "spawn_explicit",
                    "granting_principal": principal,
                    "granted_at": granted_at,
                }
                for spec_id in explicit
            ]

        return {
            "spec_id": explicit[0],
            "spec_ids": explicit,
            "spec_resolution": "resolved",
            "qualified_spec_ids": [binding["spec_id"] for binding in provenance],
            "spec_binding_provenance": provenance,
        }

    def _tmux_for(self, host: str) -> tmux_transport.Tmux:
        """Local tmux, or a peer's ssh-scoped tmux when `hosts` knows it."""
        if self.hosts is not None:
            return self.hosts.tmux_for(host)
        return self.tmux

    async def _brief_from_message(self, msg: dict[str, Any]) -> str:
        inline_keys = ("prompt", "brief", "initial_prompt")
        inline_present = any(key in msg and msg.get(key) is not None for key in inline_keys)
        blob_sha = str(msg.get("initial_prompt_blob_sha") or "").strip()
        if inline_present and blob_sha:
            raise VerbError("bad_request", "initial_prompt and initial_prompt_blob_sha are mutually exclusive")
        if blob_sha:
            if self.prompt_blobs is None:
                raise VerbError("prompt_blob_unavailable", "v2 prompt blob transport is not configured")
            try:
                return await self.prompt_blobs.read_prompt(blob_sha)
            except ValueError as exc:
                raise VerbError("prompt_blob_unavailable", str(exc)) from exc
        for key in inline_keys:
            if key in msg and msg.get(key) is not None:
                return str(msg.get(key))
        return ""

    @staticmethod
    def _prompt_requested(msg: dict[str, Any]) -> bool:
        """Server-authoritative prompt intent: did this spawn request an initial
        prompt? True iff a prompt blob sha is present OR an inline
        prompt/brief/initial_prompt carries a non-empty value. An empty-string
        inline value is NOT a request — it matches `_brief_from_message` /
        `_prepare_brief` (empty text -> not_requested). Evaluated on the raw
        message BEFORE staging so a failure before `_prepare_brief` still records
        a truthful `requested` receipt instead of `not_requested`.
        rpc_delivery_determinism lane."""
        if str(msg.get("initial_prompt_blob_sha") or "").strip():
            return True
        return any(
            key in msg and msg.get(key) is not None and str(msg.get(key))
            for key in ("prompt", "brief", "initial_prompt")
        )

    async def _prepare_brief(
        self,
        text: str,
        tmux: tmux_transport.Tmux,
        *,
        stage_host: str,
        delivery_receipt: dict[str, Any],
        native_initial_prompt: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        """Stage a native Codex prompt or bound the legacy paste wire text."""
        if not text:
            return "", {"state": "not_requested"}
        data = text.encode("utf-8")
        size = len(data)
        if size < tmux_transport.INITIAL_PROMPT_STAGE_THRESHOLD_BYTES:
            if not native_initial_prompt:
                receipt = {
                    "transport": "direct",
                    "prompt_sha256": hashlib.sha256(data).hexdigest(),
                    "prompt_size_bytes": size,
                }
                delivery_receipt.clear()
                delivery_receipt.update(receipt)
                return text, delivery_receipt
        path, digest, data = tmux_transport._prompt_stage_path(text)
        receipt = delivery_receipt
        receipt.update({
            "transport": "native_argv" if native_initial_prompt else "staged",
            "stage_host": stage_host,
            "stage_path": str(path),
            "stage_status": "pending",
            "prompt_sha256": digest,
            "prompt_size_bytes": len(data),
        })
        if native_initial_prompt:
            receipt["native_delivery"] = "staged_file_to_argv"
        try:
            await tmux.stage_text(str(path), data)
        except VerbError as exc:
            receipt.update({
                "state": "failed",
                "delivery_status": "failed",
                "failure_code": exc.code,
                "failure_reason": str(exc),
                "delivery_failed_at": iso_now(),
            })
            raise
        except Exception as exc:  # noqa: BLE001 - normalize the handoff error
            receipt.update({
                "state": "failed",
                "delivery_status": "failed",
                "failure_code": "prompt_stage_failed",
                "failure_reason": str(exc),
                "delivery_failed_at": iso_now(),
            })
            raise VerbError("prompt_stage_failed", str(exc)) from exc
        receipt.update({"stage_status": "written", "stage_written_at": iso_now()})
        if native_initial_prompt:
            return text, receipt
        pointer = f"Read {path} and follow the complete prompt exactly."
        tmux_transport.assert_injectable(pointer, "staged prompt pointer")
        receipt["pointer"] = pointer
        return pointer, delivery_receipt

    def _launch_machine(self, host: str, provider: str | None = None) -> "launch.LocalMachine | None":
        """Resolve the PATH-bearing profile for the actual target host."""
        machine = self.machine
        peer = getattr(self.hosts, "peers", {}).get(host) if self.hosts is not None else None
        if peer is not None:
            try:
                machine = (
                    launch.raw_command_machine_from_config(peer)
                    if provider is None
                    else launch.machine_from_config(peer, provider=provider)
                )
            except ValueError as exc:
                raise VerbError("spawn_launch_unavailable", str(exc)) from exc
        return machine

    async def _stage_launch_token(
        self,
        plan: launch.LaunchPlan,
        *,
        host: str,
        name: str,
        provider: str,
    ) -> None:
        """Write the launch token before the pane exists, without argv data."""
        tmux = tmux_transport._ACTIVE_LAUNCH_TMUX.get()
        if tmux is None:
            # Direct resolution tests intentionally exercise pure command
            # construction. A real spawn always sets this context first.
            return
        stager = getattr(tmux, "stage_text", None)
        if not callable(stager):
            raise VerbError(
                "token_stage_failed",
                "target transport cannot stage the private stream token",
            )
        try:
            await stager(plan.stream_token_file, plan.stream_token.encode("utf-8"))
        except VerbError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize without token data
            raise VerbError("token_stage_failed", str(exc)) from exc
        self.token_telemetry.record_issuance(
            stream_id=f"{host}:{name}",
            operation=f"spawn:{provider}",
        )

    @staticmethod
    def _envelope_command(command: str, machine: "launch.LocalMachine | None") -> str:
        prefix = launch.agent_orch_path_export(machine)
        return f"{prefix}{command}" if prefix else command

    async def _session_state_for_wait(self, name: str, tmux: "tmux_transport.Tmux") -> str:
        """Read liveness; only explicit ``gone`` permits cleanup."""
        try:
            value = await tmux.session_state(name)
            return value if value in {"alive", "gone"} else "unknown"
        except VerbError as exc:
            if exc.code == "tmux_timeout":
                return "unknown"
            raise
        except Exception:  # noqa: BLE001 - liveness is inconclusive, never death
            return "unknown"

    async def _admitted_live_pane(
        self, host: str, name: str, tmux: "tmux_transport.Tmux", *, expected_generation: str = "",
    ) -> bool:
        """Protect a successor lifecycle; the current spawn may roll itself back."""
        row = await self.store.fetch_session(host, name)
        if (
            not isinstance(row, dict)
            or row.get("status") != "open"
        ):
            return False
        row_generation = str(row.get("session_generation") or "")
        if expected_generation:
            if row_generation == expected_generation:
                return False
            if row_generation:
                return await self._session_state_for_wait(name, tmux) != "gone"
        if not row.get("spec_id"):
            return False
        return await self._session_state_for_wait(name, tmux) != "gone"

    async def _current_spawn_pane_verified_alive(
        self, host: str, name: str, tmux: "tmux_transport.Tmux", *, expected_generation: str,
    ) -> bool:
        """Prove the admitted generation still owns the live tmux pane process."""
        row = await self.store.fetch_session(host, name)
        if (
            not isinstance(row, dict)
            or row.get("status") != "open"
            or str(row.get("session_generation") or "") != expected_generation
        ):
            return False
        stored_pid = str(row.get("pane_pid") or "")
        identity_reader = getattr(tmux, "pane_identity", None)
        if not stored_pid or not callable(identity_reader):
            return False
        try:
            identity = await identity_reader(name)
        except Exception:  # noqa: BLE001 - transport ambiguity is not live proof
            return False
        return bool(
            isinstance(identity, dict)
            and identity.get("pane_pid") == stored_pid
            and identity.get("session_name") == name
        )

    async def _wait_for_created_session(self, name: str, tmux: "tmux_transport.Tmux") -> str:
        """Return alive, gone, or unknown after a timed-out ``new-session``."""
        deadline = time.monotonic() + CREATION_PROBE_TIMEOUT_S
        while True:
            state = await self._session_state_for_wait(name, tmux)
            if state == "alive":
                return "alive"
            if state == "gone":
                return "gone"
            if time.monotonic() >= deadline:
                return "unknown"
            await asyncio.sleep(tmux_transport.POLL_INTERVAL_S)

    # -- observation (bounded polls, never turn-watching) -------------------

    async def _await_provider_ready(
        self, name: str, provider: str, tmux: "tmux_transport.Tmux",
        *, absolute_deadline: float | None = None,
    ) -> "BootReadyOutcome":
        """Poll readiness against one absolute deadline."""
        predicate = READY_PREDICATES[provider]
        start = time.monotonic()
        deadline = float(absolute_deadline) if absolute_deadline is not None else start + BOOT_READY_HARD_DEADLINE_S
        polls = 0
        final_probe = False

        def _outcome(ready: bool, *, reason: str | None = None) -> "BootReadyOutcome":
            return BootReadyOutcome(ready, time.monotonic() - start, polls, reason)

        while True:
            polls += 1
            pane_text = ""
            try:
                pane_text = await tmux.capture(name)
            except VerbError as exc:
                if exc.code != "tmux_timeout":
                    raise
                # A capture timeout is transport ambiguity, not a dead pane.
            now = time.monotonic()
            if provider == "codex" and pane_text:
                if codex_tui_readiness(pane_text) == CODEX_RESET_BLOCKED:
                    return _outcome(False, reason=CODEX_RESET_BLOCKED)
            if pane_text and predicate(pane_text):
                return _outcome(True)
            if await self._session_state_for_wait(name, tmux) == "gone":
                return _outcome(False)
            if now >= deadline:
                if final_probe:
                    return _outcome(False)
                final_probe = True
                continue
            await asyncio.sleep(min(0.5, max(0.0, deadline - time.monotonic())))

    @staticmethod
    def _auth_context_marker_path(name: str) -> str:
        """Return lane 5's bounded, injection-safe remote-host marker path."""
        stream_id = f"{AUTH_CONTEXT_MARKER_HOST}:{name}"
        digest = hashlib.sha256(stream_id.encode("utf-8")).hexdigest()
        return f"{AUTH_CONTEXT_MARKER_DIR}/{digest}.code"

    @staticmethod
    def _uses_auth_context_marker(host: str, provider: str) -> bool:
        return bool(AUTH_CONTEXT_MARKER_HOST) and host == AUTH_CONTEXT_MARKER_HOST and provider == "claude"

    async def _clear_auth_context_marker(self, host: str, name: str, provider: str) -> bool:
        """Fence one remote-host Claude launch against a marker from an earlier retry.

        The shim pre-clears too, but that is insufficient: a launch can fail
        before the shim starts.  A successful clear immediately before *every*
        ``tmux.new_session`` is therefore the only authority to accept a later
        marker.  ``Hosts.run_command`` keeps this SSH operation off the daemon
        event loop.  Any transport failure is intentionally indistinguishable
        from generic ``boot_not_ready`` to callers.
        """
        if not self._uses_auth_context_marker(host, provider):
            return True
        if self.hosts is None:
            return False
        try:
            rc, _out = await self.hosts.run_command(
                host,
                "/bin/rm",
                "-f",
                "--",
                self._auth_context_marker_path(name),
                timeout=AUTH_CONTEXT_MARKER_TIMEOUT_S,
            )
        except Exception:  # noqa: BLE001 - absence is not auth evidence
            log.warning("auth-context marker clear failed for %s:%s", host, name)
            return False
        if rc == 0:
            return True
        log.warning("auth-context marker clear returned nonzero for %s:%s", host, name)
        return False

    async def _auth_context_marker_observed(self, host: str, name: str, provider: str) -> bool:
        """Read only the exact lane-5 marker after a fenced remote-host boot failure.

        A missing, unreadable, oversized/truncated, or malformed artifact is
        deliberately not evidence of authentication loss.  It falls through to
        the existing generic boot failure without exposing marker contents.
        """
        if not self._uses_auth_context_marker(host, provider) or self.hosts is None:
            return False
        try:
            rc, out = await self.hosts.run_command(
                host,
                "/usr/bin/head",
                "-c",
                str(AUTH_CONTEXT_MARKER_READ_BYTES),
                self._auth_context_marker_path(name),
                timeout=AUTH_CONTEXT_MARKER_TIMEOUT_S,
            )
        except Exception:  # noqa: BLE001 - missing transport is generic boot failure
            log.warning("auth-context marker read failed for %s:%s", host, name)
            return False
        if rc == 0 and out == AUTH_CONTEXT_MARKER_BYTES.decode("ascii"):
            return True
        if rc != 0:
            log.info("auth-context marker absent or unreadable for %s:%s", host, name)
        else:
            log.warning("auth-context marker bytes invalid for %s:%s", host, name)
        return False

    async def _await_marker(
        self, name: str, marker: str, timeout: float, *, since: str | None = None,
        tmux: tmux_transport.Tmux | None = None, reset_guard: bool = False,
        absolute_deadline: float | None = None,
    ) -> bool:
        """Poll a boot or receipt marker; `since` excludes an old matching echo."""
        tmux = tmux or self.tmux
        needle = tmux_transport.collapse_ws(marker)
        anchor = tmux_transport.collapse_ws(since) if since is not None else None
        if anchor is not None and needle and needle not in anchor:
            anchor = None
        timeout = max(0.01, float(timeout))
        deadline = time.monotonic() + timeout
        if absolute_deadline is not None:
            deadline = min(deadline, float(absolute_deadline))
        previous = ""
        while True:
            raw_capture = ""
            try:
                raw_capture = await tmux.capture(name)
            except VerbError as exc:
                if exc.code != "tmux_timeout":
                    raise
            if reset_guard and raw_capture and codex_reset_interstitial_visible(raw_capture):
                raise VerbError(
                    CODEX_RESET_BLOCKED,
                    "Codex TUI displayed a usage-limit reset offer; automated input is blocked until a verified operator resolves it",
                    readiness_reason=CODEX_RESET_BLOCKED,
                    reset_blocked=True,
                    retryable=False,
                    nonretryable=True,
                    pane_preserved=True,
                    startup_input_blocked=True,
                    do_not_retry=True,
                )
            now = time.monotonic()
            cap = tmux_transport.collapse_ws(raw_capture)
            changed = bool(cap) and cap != previous
            if changed:
                previous = cap
                deadline = max(deadline, now + timeout)
                if absolute_deadline is not None:
                    deadline = min(deadline, float(absolute_deadline))
            haystack = tmux_transport.new_since(anchor, cap) if anchor is not None else cap
            if needle and needle in haystack:
                return True
            if await self._session_state_for_wait(name, tmux) == "gone":
                return False
            if now >= deadline and (absolute_deadline is not None or not changed):
                return False
            await asyncio.sleep(min(
                tmux_transport.POLL_INTERVAL_S,
                max(0.0, deadline - time.monotonic()),
            ))

    async def _confirm_brief_delivery(
        self, name: str, brief: str, before: str, provider: str, tmux: tmux_transport.Tmux,
        *, host: str | None = None, watermark: EventWatermark | None = None,
        absolute_deadline: float | None = None, proof_timeout_s: float | None = None,
    ) -> bool:
        """Receipt for a freshly pasted spawn brief. A confirmed receipt means
        the brief was SUBMITTED, not merely pasted — a `spawn.ok` that returned
        on a pane echo of an *unsubmitted* draft (Enter lost to the paste→submit
        buffer race) is the racy initial-prompt loss: `spawn.ok` with an idle
        pane and nothing delivered.

        Explicit-`command` path (no provider TUI — the smoke stub, a
        line-oriented CLI): the process echoes ONLY a submitted line, so the
        anchored pane echo IS submission proof. Keep the fast echo receipt
        (578a6d7a anchored semantics; remote tells use `_await_marker` directly
        and are untouched). Provider and remote-spawn paths use only the exact
        durable post-watermark USER event."""
        authoritative_host = host or self.sessions.local_host
        if provider not in SUBMIT_PREDICATES and authoritative_host == self.sessions.local_host:
            needle = tmux_transport.collapse_ws(tmux_transport.receipt_needle(brief))
            return await self._await_marker(name, needle, tmux_transport.RECEIPT_TIMEOUT_S, since=before, tmux=tmux)
        return await self._confirm_submission(
            name, brief, before, provider, tmux, host=host, watermark=watermark,
            absolute_deadline=absolute_deadline, proof_timeout_s=proof_timeout_s,
        )

    async def _watermark_before_action(
        self, stream_id: str, *, absolute_deadline: float | None = None,
    ) -> EventWatermark:
        """Take a bounded pre-action watermark when the proof supports it.

        Older injected proof doubles only expose ``watermark()``; retain that
        seam while the production proof uses its retry-capable API.
        """
        waiter = getattr(self.submission_proof, "wait_for_watermark", None)
        if callable(waiter):
            kwargs: dict[str, Any] = {"timeout_s": PROOF_FAST_WAIT_S}
            if absolute_deadline is not None:
                kwargs["absolute_deadline"] = absolute_deadline
            return await waiter(stream_id, **kwargs)
        return await self.submission_proof.watermark(stream_id)

    async def _confirm_submission(
        self, name: str, brief: str, before: str, provider: str, tmux: tmux_transport.Tmux,
        *, host: str | None = None, watermark: EventWatermark | None = None,
        absolute_deadline: float | None = None, proof_timeout_s: float | None = None,
    ) -> bool:
        """Confirm only through the exact durable post-watermark USER event."""
        del before, provider, tmux
        stream_id = f"{host or self.sessions.local_host}:{name}"
        watermark = watermark or await self.submission_proof.watermark(stream_id)
        proof_timeout = (
            PROOF_TERMINAL_BOUND_S
            if proof_timeout_s is None else max(0.0, float(proof_timeout_s))
        )
        if absolute_deadline is not None:
            proof_timeout = min(
                proof_timeout,
                max(0.0, float(absolute_deadline) - time.monotonic()),
            )
        if proof_timeout <= 0:
            self._submission_proof_failures[stream_id] = (
                "readiness_deadline_expired_before_submission_proof"
            )
            return False
        observed: EventProof = await self.submission_proof.wait(
            stream_id,
            expected_text=brief,
            watermark=watermark,
            timeout_s=proof_timeout,
        )
        if observed.proven:
            self._submission_proof_failures.pop(stream_id, None)
            return True
        self._submission_proof_failures[stream_id] = observed.reason or "submission_proof_pending"
        return False

    async def _confirm_native_initial_prompt(
        self, stream_id: str, text: str, *, absolute_deadline: float | None = None,
    ) -> EventProof:
        """Use first-event ingest as the sole native-delivery confirmation."""
        timeout_s = SPAWN_SUBMISSION_PROOF_BOUND_S
        if absolute_deadline is not None:
            timeout_s = min(timeout_s, max(0.0, absolute_deadline - time.monotonic()))
        waiter = getattr(self.submission_proof, "wait_for_initial_user_event", None)
        if not callable(waiter):
            return EventProof(
                "unreachable", stream_id, 0,
                reason="initial_event_proof_unavailable",
            )
        return await waiter(stream_id, expected_text=text, timeout_s=timeout_s)

    # -- spawn -------------------------------------------------------------

    @staticmethod
    def _finish_background_spawn(task: asyncio.Task[Any], pending: set[asyncio.Task[Any]]) -> None:
        pending.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except VerbError:
            pass
        except Exception:  # noqa: BLE001 - the terminal outcome is already durable
            log.exception("background spawn obligation failed after requester disconnect")

    async def _publish_spawn_state(
        self, host: str, name: str, state: str, *, queue_handle: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> None:
        async with self.sessions._lifecycle_lock(host, name):
            row = await self.store.update_session(host, name, bootstrap_state=state)
            if row is not None:
                projection = {"bootstrap_state": state, "queue_handle": queue_handle}
                if reason is not None:
                    projection["reason"] = reason
                self.sessions.apply_durable(
                    f"{host}:{name}", **projection,
                )
        if row is not None:
            if emit_if_changed := getattr(getattr(self.sessions, "_inventory_emitter", None), "emit_if_changed", None):
                await emit_if_changed(immediate=True)

    @staticmethod
    def _starting_spawn_reply(
        host: str, name: str, request_id: str, reason: str,
        delivery_receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        receipt = dict(delivery_receipt or {})
        receipt.setdefault("state", receipt.get("delivery_status") or ("pending" if receipt else "not_requested"))
        stream_id = f"{host}:{name}"
        return {
            "type": "spawn.ok", "ok": True, "request_id": request_id,
            "stream_id": stream_id, "state": "starting", "reason": reason,
            "session": {
                "stream_id": stream_id, "host": host, "session_name": name,
                "state": "starting",
                "bootstrap_state": receipt.get("bootstrap_state", "starting"),
            },
            "initial_prompt_delivery": receipt,
        }

    async def spawn(self, msg: dict[str, Any], local_host: str) -> dict[str, Any]:
        """Keep the durable spawn obligation running after request cancellation."""
        # Objectives are required only for parented child spawns (roster projection);
        # a top-level/handoff spawn derives one, and the brief is read only then.
        supported, parent = msg.get("objective_supported"), msg.get("parent_stream_id")
        obj = msg.get("objective")
        will_derive = not objective_required_for(supported, parent) and (
            obj is None or (isinstance(obj, str) and not obj.strip())
        )
        brief = await self._brief_from_message(msg) if will_derive else ""
        objective, objective_source, error = resolve_objective(
            obj, objective_supported=supported, parent_stream_id=parent,
            brief=brief, title=msg.get("title"),
        )
        if error:
            raise VerbError(error, error)
        # Caller-supplied provenance cannot relabel a resolved objective.
        msg = {**msg, "objective": objective, "objective_source": objective_source}
        if "no_watch" in msg and not isinstance(msg["no_watch"], bool):
            raise VerbError("invalid_request", "no_watch must be boolean")
        admission: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(self._spawn_impl(msg, local_host, admission=admission))
        self._background_spawns.add(task)
        def finish(done: asyncio.Task[Any]) -> None:
            self._finish_background_spawn(done, self._background_spawns)
            if admission.done():
                return
            try:
                admission.set_result(done.result())
            except BaseException as exc:  # propagate pre-admission rejection to dispatch
                admission.set_exception(exc)

        task.add_done_callback(finish)
        reply = await asyncio.shield(admission)
        # Keyed initial replies carry the truthful admitted enumeration too (AC9).
        idempotency_key = str(msg.get("idempotency_key") or msg.get("request_id") or "").strip()
        if (
            idempotency_key and isinstance(reply, dict)
            and "admitted_count" not in reply
            and reply.get("type") == "spawn.ok"
        ):
            host = str(msg.get("host") or local_host).strip()
            reply.update(await self._admitted_for_key(host, idempotency_key))
        return reply

    async def admit_schedule(
        self, msg: dict[str, Any], local_host: str, *, admission_name: str
    ) -> dict[str, Any]:
        """Run the read-only portion of immediate-spawn admission.

        Scheduling freezes this measured result; it must not reserve a stream,
        stage a token, create a pane, or otherwise begin dispatch.
        """
        # The insert path resolves the objective (parented required, top-level
        # derived) before this runs, so presence re-validation is sufficient.
        if error := objective_error(msg.get("objective")):
            raise VerbError(error, error)
        host = str(msg.get("host") or local_host).strip()
        if host != local_host:
            if self.hosts is None:
                raise VerbError("unsupported_host", "v2 spawn is localhost-only")
            await self.hosts.ensure_reachable(host, "schedule.insert")
        brief = await self._brief_from_message(msg)
        if brief:
            tmux_transport.assert_injectable(brief, "brief")
        binding = await self._resolve_spec_binding(msg, host, admission_name)
        provider = str(msg.get("provider") or "").strip()
        requested_model = msg.get("model")
        requested_effort = msg.get("effort")
        try:
            if msg.get("handoff"):
                resolved = await self._resolve_handoff(msg, host, name=admission_name)
            elif msg.get("schema") is not None:
                resolved = validate_v2(
                    schema=str(msg["schema"]), provider=provider,
                    model=str(requested_model or ""), effort=str(requested_effort or ""),
                    host=host, spawn_profile=str(msg.get("spawn_profile") or ""),
                    catalog_version=str(msg.get("catalog_version") or ""),
                    resolution_source=str(msg.get("resolution_source") or ""),
                )
            else:
                resolved = resolve_spawn(
                    provider=provider or None, model=requested_model,
                    effort=requested_effort, host=host, legacy=True,
                )
        except SpawnProfileError as exc:
            raise VerbError(exc.code, str(exc)) from exc
        resolved_provider = str(resolved["provider"])
        if not str(msg.get("command") or "").strip() and self._launch_machine(host, resolved_provider) is None:
            raise VerbError("spawn_launch_unavailable", "no target-host machine profile for scheduled spawn")
        return {
            "host": host,
            "role": resolved.get("role", msg.get("role")),
            "requested_provider": msg.get("requested_provider", msg.get("provider")),
            "requested_model": msg.get("requested_model", msg.get("model")),
            "requested_effort": msg.get("requested_effort", msg.get("effort")),
            "resolved_provider": resolved_provider,
            "resolved_model": str(resolved["model"]),
            "resolved_effort": str(resolved["effort"]),
            "spec_binding": binding,
        }

    @staticmethod
    def _spawn_payload_hash(msg: dict[str, Any]) -> str:
        """Content hash of the logical spawn request for same-key conflict
        detection. Excludes the volatile identity fields (`request_id`,
        `idempotency_key`) so a legitimate same-key retry of the SAME request
        matches, while a same-key reuse for a DIFFERENT spawn is a conflict.
        rpc_delivery_determinism lane."""
        # Capability/provenance metadata must not break a pre-hotfix explicit retry.
        volatile = {"request_id", "idempotency_key", "objective_supported", "objective_source"}
        canonical = {k: msg[k] for k in sorted(msg) if k not in volatile}
        return hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()

    async def _admitted_for_key(self, host: str, idempotency_key: str) -> dict[str, Any]:
        """Truthful admitted-session enumeration for an idempotency key (AC9): the
        distinct stream ids admitted for this key across reservations + outcomes.
        The atomic claim guarantees exactly one, but the response ENUMERATES so a
        caller can never be told `reconciled` while unreported duplicates exist —
        a success signal that under-reports duplicates is worse than an error.
        rpc_delivery_determinism lane."""
        names = await self.store.admitted_session_names_for_key(host, idempotency_key)
        streams = [f"{host}:{n}" for n in names]
        return {
            "admitted_count": len(streams),
            "admitted_sessions": streams,
            "admitted_scope": "idempotency_key",
            "admitted_set_authoritative": True,
        }

    async def _reply_from_replay(
        self, host: str, request_id: str, kind: str, row: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Build the truthful reply for a same-key spawn that the atomic claim
        resolved to an existing session — no second pane. Carries the AC9 admitted
        enumeration. rpc_delivery_determinism lane."""
        recorded_name = str(row.get("session_name") or "")
        recorded_request_id = str(row.get("request_id") or request_id)
        receipt = row.get("delivery_receipt")
        if not isinstance(receipt, dict):
            receipt = {"state": str(row.get("reason") or "not_requested")}
        admitted = await self._admitted_for_key(host, idempotency_key)
        if kind == "terminal":
            state = str(row.get("state"))
            if state == "delivered":
                # A terminal DELIVERED outcome with a still-live row is the clean
                # replay: same spawn.ok, same stream id, no new pane.
                session = self.sessions.get(f"{host}:{recorded_name}")
                if session is not None:
                    return {
                        "type": "spawn.ok", "ok": True,
                        "stream_id": f"{host}:{recorded_name}",
                        "session": session,
                        "initial_prompt_delivery": receipt,
                        "replayed": True,
                        **admitted,
                    }
                # Delivered, but the row is gone (retention purge, or a later
                # close) — NOT a failure. Point the caller at the durable request
                # id to reconcile; never mint a second pane, never fake a failure.
                reply = self._starting_spawn_reply(
                    host, recorded_name, recorded_request_id,
                    "replayed_delivered_no_live_row", receipt,
                )
                reply.update(admitted)
                return reply
            # A terminal FAILED (or other non-delivered terminal) outcome replays
            # the stored FAILURE truthfully (Outcome Matrix class B) — NOT as
            # pending. Same key = same operation; to re-attempt, use a new key.
            recorded_reason = str(row.get("reason") or "spawn_failed")
            code = recorded_reason.split(":", 1)[0].strip() or "spawn_failed"
            raise VerbError(
                code,
                f"replayed terminal {state} spawn for idempotency_key: {recorded_reason}",
                replayed=True,
                outcome_class="failed",
                initial_prompt_delivery=receipt,
                stream_id=f"{host}:{recorded_name}",
                spawn_request_id=recorded_request_id,
                **admitted,
            )
        # In-flight duplicate still creating its pane: do NOT mint a second pane;
        # return the existing durable starting handle.
        reply = self._starting_spawn_reply(
            host, recorded_name, recorded_request_id,
            "duplicate_in_flight_same_idempotency_key", receipt,
        )
        reply.update(admitted)
        return reply

    @staticmethod
    def _frozen_reason(host: str, hold: dict[str, Any] | None) -> str:
        reason = (hold or {}).get("reason")
        return (
            f"spawn admission is frozen on {host}"
            + (f": {reason}" if reason else "")
        )

    async def _spawn_impl(
        self,
        msg: dict[str, Any],
        local_host: str,
        *,
        admission: asyncio.Future[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        host = str(msg.get("host") or local_host).strip()
        name = str(msg.get("session_name") or "").strip() or f"v2-{uuid.uuid4().hex[:8]}"
        if f"{host}:{name}" in RESERVED_LIFECYCLE_ACTORS:
            raise VerbError("reserved_actor", "daemon lifecycle actor ids cannot be registered")
        # Remote spawn is fenced BEFORE anything is reserved or created: an
        # offline peer fails fast (`host_offline`) via one bounded SSH probe, so
        # a dead host costs nothing and never stalls (spec item 2). Without a
        # `hosts` pool v2 stays localhost-only.
        if host != local_host:
            if self.hosts is None:
                raise VerbError("unsupported_host", "v2 spawn is localhost-only in this increment")
            await self.hosts.ensure_reachable(host, "spawn")
        tmux = self._tmux_for(host)
        # Scheduled spawn/handoff (`--at`/`--delay`) is OUT of scope in v2. The CLI
        # sends those as `schedule.insert` (already `unsupported_in_v2` by dispatch
        # fall-through); reject any scheduling field on the spawn path too so the
        # contract is explicit rather than silent.
        if any(msg.get(k) for k in ("at", "delay", "fires_at_utc", "scheduled")):
            raise VerbError("unsupported_in_v2", "scheduled spawn/handoff is not supported in v2")
        request_id = str(msg.get("request_id") or f"spawn-{uuid.uuid4().hex}")
        # Atomic claim below collapses concurrent same-key fires to one admission.
        idempotency_key = str(msg.get("idempotency_key") or msg.get("request_id") or "").strip()
        payload_hash = self._spawn_payload_hash(msg) if idempotency_key else ""
        # Daemon-side admission freeze (deploy window): refuse only a NEW claim
        # with `spawn_frozen` while a TTL'd hold is set (BEFORE any reservation/
        # pane). A retry whose key ALREADY has an admitted seat must still replay
        # that seat during a hold -- otherwise an interrupted caller's retry is
        # wrongly refused mid-freeze and the idempotency guarantee breaks. The
        # hold self-expires, so an abandoned freeze never wedges admission.
        hold = await self.store.get_spawn_admission_hold(host)
        if hold is not None:
            already = (
                await self._admitted_for_key(host, idempotency_key)
                if idempotency_key else {"admitted_count": 0}
            )
            if not already.get("admitted_count"):
                raise VerbError(
                    "spawn_frozen",
                    f"spawn admission is frozen on {host}"
                    + (f": {hold['reason']}" if hold.get("reason") else ""),
                )
        # Accept CLI `initial_prompt`, smoke `prompt`/`brief`, and staged blobs.
        full_brief = await self._brief_from_message(msg)
        ready_marker = str(msg.get("ready_marker") or "READY")
        # Checked BEFORE anything is reserved or created: a hostile brief must
        # cost nothing, not leave a pane behind for the cleanup path to kill.
        if full_brief:
            tmux_transport.assert_injectable(full_brief, "brief")

        # Binding resolution is a precondition of spawn success. Do it before
        # launch resolution and reservation so an explicit but unknown item can
        # never produce a pane, intent, or misleading spawn.ok.
        spec_binding = await self._resolve_spec_binding(msg, host, name)

        # Resolve provider tuples before reservation; explicit commands still win.
        # The deterministic staged path is safe to put in a launcher command
        # before the reservation exists; the file itself is written only after
        # that reservation succeeds.
        native_prompt_path = (
            str(tmux_transport._prompt_stage_path(full_brief)[0])
            if full_brief and not str(msg.get("command") or "").strip()
            else ""
        )
        launch_msg = {
            **msg,
            **({"_native_initial_prompt_path": native_prompt_path} if native_prompt_path else {}),
        }
        launch_context = tmux_transport._ACTIVE_LAUNCH_TMUX.set(tmux)
        try:
            command, resolution, open_overrides = await self._resolve_launch(launch_msg, host, name)
        finally:
            tmux_transport._ACTIVE_LAUNCH_TMUX.reset(launch_context)

        # Reserve the stream and its creation identity before any pane exists.
        nonce = uuid.uuid4().hex
        if idempotency_key:
            # Atomic check+claim: exactly one concurrent same-key caller wins
            # `claimed`; the rest `replay` the winner's session (no second pane).
            claim = await self.store.atomic_claim_or_replay(
                host, name, idempotency_key=idempotency_key,
                request_payload_hash=payload_hash, ttl_s=RESERVATION_TTL_S,
                request_id=request_id, nonce=nonce, owner_instance_id=self.instance_id,
                refuse_if_held=True,
            )
            status = claim["status"]
            if status == "replay":
                return await self._reply_from_replay(
                    host, request_id, claim["kind"], claim["row"], idempotency_key
                )
            if status == "conflict":
                raise VerbError(
                    "idempotency_key_conflict",
                    f"idempotency_key already bound to a different spawn payload on {host}",
                )
            # The hold is enforced INSIDE the atomic claim (QA cycle-3 astra-[3]),
            # so a freeze set after the pre-check above cannot slip a new seat in.
            if status == "frozen":
                raise VerbError("spawn_frozen", self._frozen_reason(host, hold))
            if status != "claimed":
                raise VerbError("stream_id_unavailable", f"{host}:{name} is live or already reserved")
        elif not await self.store.reserve_stream_id(
            host, name, ttl_s=RESERVATION_TTL_S, request_id=request_id,
            nonce=nonce, owner_instance_id=self.instance_id, refuse_if_held=True,
        ):
            # Keyless NEW admission: the reservation is refused in-txn if held; a
            # sticky hold re-read only picks the error (spawn_frozen vs unavailable).
            if await self.store.get_spawn_admission_hold(host) is not None:
                raise VerbError("spawn_frozen", self._frozen_reason(host, None))
            raise VerbError("stream_id_unavailable", f"{host}:{name} is live or already reserved")

        # A reservation does not prove tmux ownership; never kill an ambiguous pane.
        # The persisted open-row columns: v1's base fields, then the resolved
        # tuple overrides (provider + canonical model/effort + session identity).
        open_flds = {**tmux_transport.open_fields(msg), **spec_binding, **open_overrides}
        # Fence this spawn's lifecycle to a generation IT owns. Both boot-failure
        # cleanup closes below pass it as `expected_generation`, so a cleanup can
        # only ever close the row THIS spawn created — never a newer same-name
        # lifecycle that legitimately reused the name after a confirmed close
        # (the live-row VANISH class: a stray close landing on a live successor).
        open_flds.setdefault("session_generation", uuid.uuid4().hex)
        open_flds.setdefault("bootstrap_state", "starting")
        spawn_generation = str(open_flds["session_generation"])
        created: list[bool] = [False]
        # Creation uncertainty retains intent but never authorizes a blind kill.
        creation_uncertain: list[bool] = [False]
        boot_semaphore: asyncio.Semaphore | None = None
        keep_reservation = False
        intent_recorded = False
        # Preserve prompt intent across failures before `_prepare_brief`.
        delivery_receipt: dict[str, Any] = {
            "state": "requested" if self._prompt_requested(msg) else "not_requested"
        }
        try:
            if await tmux.has_session(name):
                raise VerbError(
                    "stream_id_unavailable",
                    f"{host}:{name} already has a live tmux session with no open row; "
                    "v2 never kills a pane it did not create",
                )
            # Persist intent before creation so restart reconciliation can recover it.
            provider = str((resolution.get("resolved_launch_tuple") or {}).get("provider") or "")
            native_initial_prompt = bool(
                full_brief
                and provider == "codex"
                and launch.NATIVE_INITIAL_PROMPT_LAUNCHER in command
            )
            brief, delivery_receipt = await self._prepare_brief(
                full_brief,
                tmux,
                stage_host=host,
                delivery_receipt=delivery_receipt,
                native_initial_prompt=native_initial_prompt,
            )
            recorded = await self.store.record_spawn_intent(
                host, name,
                {
                    "open_fields": open_flds,
                    "brief": brief,
                    "delivery_receipt": delivery_receipt,
                    "operator_initiated_top_level": (
                        not open_flds.get("parent_stream_id")
                        and not open_flds.get("handoff_from_stream_id")
                        and bool((msg.get("_auth_context") or {}).get("operator_authenticated"))
                    ),
                }, request_id=request_id, nonce=nonce,
            )
            if not recorded:
                raise VerbError("spawn_fence_lost", "spawn intent reservation expired or was cancelled")
            # From here the reservation carries a durable spawn intent — the
            # recovery handle a mid-spawn crash or cancellation is settled from
            # (INV-3). It must never be blind-released on a teardown.
            intent_recorded = True
            total_attempts = (
                1 if native_initial_prompt
                else (PROMPT_SPAWN_RETRY_ATTEMPTS if full_brief else 1)
            )
            for attempt in range(total_attempts):
                created[0] = False
                try:
                    if not await self._clear_auth_context_marker(host, name, provider):
                        raise VerbError(
                            "boot_not_ready",
                            "Claude boot could not establish the auth-context clear fence",
                        )
                    if provider == "codex":
                        if boot_semaphore is None:
                            boot_semaphore = await self._acquire_codex_boot_permit(
                                host,
                                name,
                                request_id,
                                open_flds,
                                idempotency_key=idempotency_key,
                                payload_hash=payload_hash,
                                admission=admission,
                            )
                    reply = await self._spawn_fenced(
                        host, name, request_id, command, brief, ready_marker, msg, created,
                        creation_uncertain, open_flds, resolution, tmux, delivery_receipt,
                        nonce, admission=admission, attempt=attempt, total_attempts=total_attempts,
                    )
                    if reply.get("type") == "spawn.cancelled":
                        keep_reservation = True
                        for res in await self.store.reservations(include_expired=True):
                            if (res["host"], res["session_name"], res.get("request_id")) == (host, name, request_id):
                                await self._cleanup_cancelled_spawn(res)
                        return reply
                    if reply.get("type") == "spawn.ok" and reply.get("state") == "starting":
                        keep_reservation = True
                        return reply
                except Exception as exc:
                    retryable = isinstance(exc, VerbError) and exc.code in {
                        "boot_not_ready", "tmux_timeout",
                    }
                    if isinstance(exc, VerbError) and exc.code == CODEX_RESET_BLOCKED:
                        # A reset offer is terminal for this readiness attempt.
                        # Do not enter the ordinary admitted-live retry branch:
                        # retrying would create another provider-side decision
                        # point and its cleanup would be authorized to kill the
                        # pane that must remain available to the operator.
                        raise
                    if (
                        full_brief and created[0]
                        and await self._admitted_live_pane(
                            host, name, tmux, expected_generation=spawn_generation,
                        )
                    ):
                        reason = getattr(exc, "code", "spawn_failed")
                        receipt = dict(delivery_receipt)
                        receipt.update({
                            "state": "indeterminate",
                            "failure_code": str(reason),
                            "failure_reason": str(exc),
                            "delivery_failed_at": iso_now(),
                        })
                        await self.store.set_spawn_outcome(
                            host,
                            name,
                            "indeterminate",
                            request_id=request_id,
                            reason=f"spawn_retry_abandoned_after_admission: {reason}",
                            delivery_evidence="admitted_live_pane_preserved",
                            delivery_receipt=receipt,
                            effective_model=open_flds.get("effective_model"),
                            effective_effort=open_flds.get("effective_effort"),
                            idempotency_key=idempotency_key or None,
                            request_payload_hash=payload_hash or None,
                        )
                        keep_reservation = True
                        return self._starting_spawn_reply(host, name, request_id, str(reason), receipt)
                    if not (full_brief and retryable and attempt + 1 < total_attempts):
                        raise
                    # Every boot/transport retry starts from a clean pane. If
                    # the pane cannot be proven gone, do not risk a duplicate.
                    if created[0] and not await self._rollback_kill(
                        host, name, tmux, expected_generation=spawn_generation,
                    ):
                        # The pane may still exist, but transport ambiguity
                        # means it is not ours to declare dead. Keep the
                        # reservation marked as orphan evidence and let boot
                        # reconciliation settle delivered-or-rolled-back.
                        keep_reservation = True
                        try:
                            await self.store.mark_tmux_created(host, name, request_id=request_id)
                        except Exception:  # noqa: BLE001 - the intent is already durable
                            log.exception("could not mark rollback-unconfirmed pane %s:%s", host, name)
                        return self._starting_spawn_reply(
                            host, name, request_id, f"{exc.code}: rollback unconfirmed", delivery_receipt
                        )
                    if await self._session_state_for_wait(name, tmux) == "gone":
                        if await self.store.fetch_session(host, name):
                            await self.sessions.mark_closed(
                                host, name, reason=f"{exc.code}: {exc}",
                                expected_generation=spawn_generation,
                                close_kind="spawn_rollback",
                            )
                    created[0] = False
                    await asyncio.sleep(0.25 * (attempt + 1))
                    continue
                # Handoff post-steps (item 2), only after the successor is boot-ready
                # + persisted + brief delivered. The successor is already live, so
                # reparent/close are kept best-effort at this boundary; the metadata
                # move itself is central-store-only and covers every host.
                if msg.get("handoff"):
                    await self._finish_handoff(msg, f"{host}:{name}")
                return reply
            raise RuntimeError("prompt spawn retry loop exhausted without an outcome")
        except Exception as exc:
            reason = getattr(exc, "code", None) or "spawn_failed"
            if await self.store.spawn_cancelled(host, name, request_id):
                keep_reservation = intent_recorded
                if not created[0] and not creation_uncertain[0]:
                    for res in await self.store.reservations(include_expired=True):
                        if (res["host"], res["session_name"], res.get("request_id")) == (host, name, request_id):
                            await self._cleanup_cancelled_spawn(res)
                return {
                    "type": "spawn.cancelled", "ok": False, "state": "cancelled",
                    "stream_id": f"{host}:{name}", "request_id": request_id,
                }
            outcome_fields: dict[str, Any] = {}
            if reason == CODEX_RESET_BLOCKED:
                # The provider pane is intentionally left OPEN and alive.  The
                # operator must resolve the interstitial directly; this daemon
                # records the block but never pastes, presses Enter, retries,
                # reconciles, adopts, or kills this pane on that signal.
                keep_reservation = True
                blocked_receipt = {
                    **delivery_receipt,
                    "state": "blocked",
                    "delivery_status": "blocked",
                    "failure_code": CODEX_RESET_BLOCKED,
                    "readiness_reason": CODEX_RESET_BLOCKED,
                    "bootstrap_state": CODEX_RESET_BLOCKED,
                    "submission_confirmed": False,
                }
                try:
                    blocked_receipt = await self._persist_reset_blocked_spawn(
                        host,
                        name,
                        request_id,
                        open_flds,
                        brief,
                        delivery_receipt,
                        spawn_generation=spawn_generation,
                        idempotency_key=idempotency_key,
                        payload_hash=payload_hash,
                        operator_initiated_top_level=(
                            not open_flds.get("parent_stream_id")
                            and not open_flds.get("handoff_from_stream_id")
                            and bool((msg.get("_auth_context") or {}).get("operator_authenticated"))
                        ),
                    )
                except Exception:  # noqa: BLE001 - never authorize cleanup on a write failure
                    log.exception("could not persist reset-blocked spawn for %s:%s", host, name)
                if isinstance(exc, VerbError):
                    exc.extra.update({
                        "readiness_reason": CODEX_RESET_BLOCKED,
                        "reset_blocked": True,
                        "retryable": False,
                        "nonretryable": True,
                        "pane_preserved": True,
                        "stream_id": f"{host}:{name}",
                        "initial_prompt_delivery": blocked_receipt,
                    })
                raise
            if creation_uncertain[0] and not created[0]:
                # `tmux new-session` timed out and its follow-up liveness probe
                # remained inconclusive.  We cannot prove a pane exists, and so
                # cannot kill it; equally, we must not release the only durable
                # handle if recording its pane evidence fails.  The intent was
                # written before the launch attempt and is enough for the
                # existing reconciler to settle it later. The requester gets a
                # typed pre-admission error, never an unowned success handle.
                keep_reservation = True
                try:
                    await self.store.mark_tmux_created(host, name, request_id=request_id)
                except Exception:  # noqa: BLE001 - retain the already-durable intent
                    log.exception("could not mark creation-uncertain pane %s:%s", host, name)
                raise VerbError(
                    "spawn_launch_unconfirmed",
                    f"{reason}: creation unconfirmed before durable admission",
                )
            # DATA-only (rpc_delivery_determinism lane, outcome-matrix ownership):
            # attach a truthful failed receipt on ANY prompt-requested failure,
            # including one raised BEFORE staging (no transport yet) — not only the
            # direct/staged paths. This closes the missing-receipt gap where a
            # prompt-bearing spawn that hit the live-pane collision recorded no
            # receipt at all. It does NOT change kill/confirm SEMANTICS:
            # `_rollback_kill`, `mark_closed` conditions, and confirm timeouts below
            # are untouched (lane 2 surface).
            prompt_involved = (
                delivery_receipt.get("transport") in {"direct", "staged", "native_argv"}
                or delivery_receipt.get("state") not in (None, "not_requested")
            )
            if prompt_involved:
                if delivery_receipt.get("state") != "delivered":
                    delivery_receipt = {
                        **delivery_receipt,
                        "state": "failed",
                        "delivery_status": "failed",
                        "failure_code": reason,
                        "failure_reason": str(exc),
                        "delivery_failed_at": iso_now(),
                    }
                outcome_fields = {
                    "delivery_evidence": "failed",
                    "delivery_receipt": delivery_receipt,
                }
            # Admission owns the pane, but a missing proof is not proof that it
            # failed. Preserve its handle until a definitive observation arrives.
            proof_preserved = False
            proof_preservation_basis = "identity-matched pane remains alive"
            if reason in {
                "prompt_delivery_failed",
                "native_initial_prompt_delivery_failed",
                "boot_binding_indeterminate",
            } and created[0]:
                if str(open_flds.get("provider") or "") == "codex":
                    # A Codex pane that is alive OR of ambiguous liveness keeps
                    # its handle: rolling one back on an inconclusive probe kills
                    # real work. This is the retained ownership rule, previously
                    # keyed on attestation presence only because the manifest was
                    # mandatory and therefore always present for Codex.
                    pane_state = await self._session_state_for_wait(name, tmux)
                    proof_preserved = pane_state != "gone"
                    proof_preservation_basis = (
                        "codex pane remains alive or liveness is ambiguous"
                    )
                else:
                    proof_preserved = await self._current_spawn_pane_verified_alive(
                        host, name, tmux, expected_generation=spawn_generation,
                    )
            if proof_preserved:
                keep_reservation = True
                proof_pending_reason = (
                    str(exc)
                    if reason in {
                        "boot_binding_indeterminate",
                        "native_initial_prompt_delivery_failed",
                    }
                    else str(
                        delivery_receipt.get("proof_watermark_reason")
                        or self._submission_proof_failures.pop(f"{host}:{name}", "")
                        or "prompt_delivery_unproven"
                    )
                )
                delivery_receipt = {
                    **delivery_receipt,
                    "state": "indeterminate",
                    "delivery_status": "indeterminate",
                    "bootstrap_state": "starting",
                    "proof_state": (
                        "unreachable"
                        if delivery_receipt.get("proof_watermark_state") == "unreachable"
                        else "pending"
                    ),
                    "proof_watermark": delivery_receipt.get("proof_watermark"),
                    "proof_watermark_state": delivery_receipt.get("proof_watermark_state"),
                    "proof_watermark_reason": delivery_receipt.get("proof_watermark_reason"),
                    "failure_code": reason,
                    "failure_reason": str(exc),
                    "delivery_failed_at": iso_now(),
                }
                async with self.sessions._lifecycle_lock(host, name):
                    current_row = await self.store.fetch_session(host, name)
                    current_created_at = ""
                    if (
                        isinstance(current_row, dict)
                        and str(current_row.get("session_generation") or "") == spawn_generation
                    ):
                        current_created_at = str(current_row.get("created_at") or "")
                    row = await self.store.update_session(
                        host, name,
                        expected_generation=current_created_at,
                        bootstrap_state="starting",
                    )
                if row is None:
                    return self._starting_spawn_reply(
                        host, name, request_id,
                        "prompt_delivery_failed: lifecycle advanced",
                        delivery_receipt,
                    )
                failure_reason = f"{proof_pending_reason}: {proof_preservation_basis}"
                await self.store.set_spawn_outcome(
                    host, name, "indeterminate", request_id=request_id,
                    reason=failure_reason,
                    delivery_evidence="live_pane_unproven",
                    delivery_receipt=delivery_receipt,
                    effective_model=open_flds.get("effective_model"),
                    effective_effort=open_flds.get("effective_effort"),
                    idempotency_key=idempotency_key or None,
                    request_payload_hash=payload_hash or None,
                )
                await self._publish_spawn_state(host, name, "starting")
                return self._starting_spawn_reply(
                    host, name, request_id, proof_pending_reason, delivery_receipt,
                )
            # Cleanup is scoped to the pane THIS spawn created — tracked
            # explicitly, never inferred from the name. Best-effort: a remote
            # kill that itself times out must not mask the original failure nor
            # skip recording the outcome (SessionReconciler re-collects a stray pane).
            row = None
            if created[0]:
                rolled_back = await self._rollback_kill(
                    host, name, tmux, expected_generation=spawn_generation,
                )
                if not rolled_back:
                    keep_reservation = True
                    try:
                        await self.store.mark_tmux_created(host, name, request_id=request_id)
                    except Exception:  # noqa: BLE001 - retain the already-durable intent
                        log.exception("could not mark rollback-unconfirmed pane %s:%s", host, name)
                    return self._starting_spawn_reply(
                        host, name, request_id, f"{reason}: rollback unconfirmed", delivery_receipt
                    )
                # Admission now precedes readiness and delivery, so every
                # confirmed rollback can have an OPEN row to settle. Close it,
                # but ONLY on a CONFIRMED death: the tri-state `session_state`
                # tells "gone" (tmux exit 1) apart from "unreachable" (a dropped
                # ssh reads as no-pane), so a remote kill racing an ssh blip never
                # marks a row closed over a live remote pane (the
                # `row_closed_tree_alive` cascade the close ladder forbids).
                pane_gone = await tmux.session_state(name) == "gone"
            elif admission is not None and admission.done():
                row = await self.store.fetch_session(host, name)
            # Key the FAILED outcome (DATA-only) so a same-key retry finds this
            # terminal failure after the reservation is released and replays it
            # truthfully (Outcome Matrix class B) instead of minting a new pane.
            failure_reason = f"{reason}: {exc}"
            await self.store.set_spawn_outcome(
                host, name, "failed", request_id=request_id, reason=failure_reason,
                idempotency_key=idempotency_key or None,
                request_payload_hash=payload_hash or None,
                # Defect 2 (spawn_outcomes coverage/semantics spec): `boot_not_ready`
                # is the bounded readiness poll timing out, not a boot verdict --
                # admission already happened. Flag it so a consumer can tell this
                # apart from a genuine boot/registration failure without parsing
                # `reason` prose (AC3).
                readiness_timed_out=(reason == "boot_not_ready"),
                **outcome_fields,
            )
            await self._publish_spawn_state(
                host, name, "failed", reason=failure_reason,
            )
            if created[0] and pane_gone and await self.store.fetch_session(host, name):
                # Rollback has only confirmed the tmux pane is gone. It has
                # not captured the process/boot identity needed to prove an
                # empty tree, so leave a durable unknown for the closed
                # survivor reconciler instead of silently recording reaped.
                await self.sessions.mark_closed(
                    host, name, reason=failure_reason,
                    reap_status="unknown", survivors=[],
                    expected_generation=spawn_generation,
                    close_kind="spawn_rollback",
                )
            elif (
                isinstance(row, dict)
                and str(row.get("session_generation") or "") == spawn_generation
            ):
                await self.sessions.mark_closed(
                    host, name, reason=failure_reason,
                    expected_generation=spawn_generation,
                    close_kind="spawn_rollback",
                )
                if emit_if_changed := getattr(
                    getattr(self.sessions, "_inventory_emitter", None), "emit_if_changed", None,
                ):
                    await emit_if_changed(immediate=True)
            if (
                isinstance(exc, VerbError)
                and exc.code == AUTH_CONTEXT_FAILURE_MARKER.removesuffix(":")
                and isinstance(outcome_fields.get("delivery_receipt"), dict)
            ):
                # `Server._dispatch` exposes VerbError extras in `spawn.error`.
                # Preserve the prompt-bearing receipt in the same public
                # envelope where the terminal outcome records it.
                exc.extra.setdefault(
                    "initial_prompt_delivery", outcome_fields["delivery_receipt"]
                )
            raise
        except BaseException:
            # INV-3: `asyncio.CancelledError` (and any other `BaseException`) is
            # NOT a confirmed terminal outcome — it bypasses the `except
            # Exception` cleanup above. Once a durable spawn intent exists, a
            # teardown must RETAIN the reservation + intent as the recovery
            # handle: the shielded spawn task can leave a live pane whose only
            # addressable record is this reservation. Never blind-release it.
            if intent_recorded:
                keep_reservation = True
            # Defect 1 (spawn_outcomes coverage/semantics spec): a bare
            # BaseException (asyncio.CancelledError, etc.) skips the `except
            # Exception` outcome write above entirely. If admission already
            # happened (a `sessions` row exists), that leaves a live,
            # row-admitted session with NO forensic record it was ever spawned
            # -- invisible to anything auditing spawns through this table
            # (repro: an admitted session with zero `v2_spawn_outcomes` rows,
            # unbounded). This is a coverage floor, not the final truth: a
            # later reconciliation pass (once one applies to this reservation)
            # still overwrites it with a definite `delivered`/`failed`
            # (`state` here is neither, so the recurring reconciler's
            # already-resolved check does not treat this as settled).
            # Best-effort -- a write failure here must never mask the real
            # interruption being re-raised.
            try:
                if await self.store.fetch_session(host, name):
                    await self.store.set_spawn_outcome(
                        host, name, "indeterminate", request_id=request_id,
                        reason="spawn_interrupted: request cancelled after "
                               "admission; boot/delivery outcome unresolved",
                        idempotency_key=idempotency_key or None,
                        request_payload_hash=payload_hash or None,
                    )
            except Exception:  # noqa: BLE001 - never mask the original interruption
                log.exception(
                    "could not record interrupted-spawn outcome for %s:%s", host, name
                )
            raise
        finally:
            if boot_semaphore is not None:
                boot_semaphore.release()
            # The id is released on BOTH paths: on success the sessions row is
            # authoritative, on a confirmed failure the id becomes reusable at
            # once, and on an indeterminate remote outcome (or a mid-spawn
            # cancellation) the reservation is retained as the durable
            # reconciliation handle.
            if not keep_reservation:
                await self._release_spawn_reservation(host, name, request_id)
            elif intent_recorded and self.instance_id:
                # Durable-only adoptability: once this in-process obligation
                # has finished while retaining its recovery handle, clear the
                # owner as the final teardown act. The existing CAS prevents a
                # later generation from being altered by a stale finisher.
                await self.store.restore_spawn_intent_owner(
                    host, name, request_id=request_id,
                    owner_instance_id=self.instance_id, prior_owner="",
                )

    async def _rollback_kill(
        self, host: str, name: str, tmux: "tmux_transport.Tmux", *,
        expected_generation: str | None = None,
    ) -> bool:
        """Kill the pane THIS spawn created, persistently. A single best-effort
        `kill-session` can time out under the same load that caused the slow boot
        (the tmux command queues behind a saturated host and raises
        `tmux_timeout`), which orphaned live codex TUIs on the boot_not_ready
        flap. Retry with verification so a transient tmux stall cannot strand the
        pane; if it outlives every attempt, log and leave it to the
        SessionReconciler — v2 never escalates to a blind SIGKILL.

        Resolve and kill the tmux pane id, not the reusable session name. The
        durable generation and stored pane pid are rechecked immediately before
        each destructive call. A newer lifecycle or ambiguous identity is a
        preserve outcome, never authorization to kill."""
        for attempt in range(ROLLBACK_KILL_ATTEMPTS):
            row = await self.store.fetch_session(host, name)
            if expected_generation:
                if (
                    not isinstance(row, dict)
                    or row.get("status") != "open"
                    or str(row.get("session_generation") or "") != expected_generation
                ):
                    return False
            # An explicit tmux absence is already a confirmed rollback. Do this
            # before resolving a pane identity so a proof-only failure can never
            # issue a destructive kill after the pane has already exited.
            try:
                if await tmux.session_state(name) == "gone":
                    return True
            except Exception:  # noqa: BLE001 - ambiguity remains non-destructive
                pass
            identity: dict[str, str] | None = None
            identity_reader = getattr(tmux, "pane_identity", None)
            if callable(identity_reader):
                try:
                    identity = await identity_reader(name)
                except Exception:  # noqa: BLE001 - ambiguity preserves the pane
                    identity = None
                if identity is None:
                    # A provider that exited between admission and rollback
                    # leaves no pane identity to kill. Only explicit tmux
                    # absence is a successful rollback; alive/unreachable is
                    # still ambiguity and preserves the durable handle.
                    try:
                        return await tmux.session_state(name) == "gone"
                    except Exception:  # noqa: BLE001
                        return False
                stored_pid = str((row or {}).get("pane_pid") or "")
                if (
                    (expected_generation and not stored_pid)
                    or (stored_pid and identity.get("pane_pid") != stored_pid)
                ):
                    return False
                # Close the DB->tmux check/use gap as far as the two authorities
                # permit. The pane-id kill below closes the remaining tmux name
                # reuse gap: a vanished old id cannot resolve to its successor.
                if expected_generation:
                    current = await self.store.fetch_session(host, name)
                    if (
                        not isinstance(current, dict)
                        or current.get("status") != "open"
                        or str(current.get("session_generation") or "") != expected_generation
                        or not str(current.get("pane_pid") or "")
                        or str(current.get("pane_pid")) != identity.get("pane_pid")
                    ):
                        return False
            try:
                pane_killer = getattr(tmux, "kill_pane", None)
                if identity is not None and callable(pane_killer):
                    await pane_killer(identity["pane_id"])
                else:
                    # Compatibility for test/transport doubles without the
                    # identity API. Production tmux_transport.Tmux always takes the pane-id path.
                    await tmux.kill_session(name)
            except Exception:  # noqa: BLE001
                log.warning(
                    "rollback kill attempt %d/%d failed for %s:%s",
                    attempt + 1, ROLLBACK_KILL_ATTEMPTS, host, name,
                )
            try:
                if identity is not None and callable(identity_reader):
                    current_identity = await identity_reader(name)
                    if (
                        current_identity is None
                        or current_identity.get("pane_id") != identity.get("pane_id")
                    ):
                        return True
                elif await tmux.session_state(name) == "gone":
                    return True
            except Exception:  # noqa: BLE001
                pass  # can't confirm death this cycle; retry the kill
            if attempt + 1 < ROLLBACK_KILL_ATTEMPTS:
                await asyncio.sleep(ROLLBACK_KILL_RETRY_S)
        log.error(
            "rollback could not confirm pane death for %s:%s after %d attempts "
            "(orphan; SessionReconciler will reconcile)",
            host, name, ROLLBACK_KILL_ATTEMPTS,
        )
        return False

    async def _acquire_codex_boot_permit(
        self,
        host: str,
        name: str,
        request_id: str,
        open_flds: dict[str, Any],
        *,
        idempotency_key: str,
        payload_hash: str,
        admission: asyncio.Future[dict[str, Any]] | None,
    ) -> asyncio.Semaphore:
        """Persist a queue handle before exposing it, then await FIFO admission."""
        cap, timeout_s = boot_limits(host)
        stream_id = f"{host}:{name}"

        async def write_queued() -> None:
            handle = {"request_id": request_id, "host": host}
            receipt = {
                "state": "queued",
                "delivery_status": "queued",
                "queue_handle": handle,
            }
            written = await self.store.set_spawn_outcome(
                host,
                name,
                "queued",
                request_id=request_id,
                reason="awaiting_codex_boot_permit",
                delivery_evidence="queued",
                delivery_receipt=receipt,
                effective_model=open_flds.get("effective_model"),
                effective_effort=open_flds.get("effective_effort"),
                idempotency_key=idempotency_key or None,
                request_payload_hash=payload_hash or None,
            )
            if written is False:
                raise VerbError("spawn_fence_lost", "cancelled spawn cannot enter the boot queue")
            if self.sessions.get(stream_id) is None:
                session = await self.sessions.open(
                    host, name, **{**open_flds, "bootstrap_state": "queued"}, fence=request_id,
                )
                open_flds["created_at"] = session["created_at"]
            await self._publish_spawn_state(host, name, "queued", queue_handle=handle)
            if admission is not None and not admission.done():
                admission.set_result({
                    "type": "spawn.ok",
                    "ok": True,
                    "request_id": request_id,
                    "spawn_request_id": request_id,
                    "stream_id": stream_id,
                    "state": "queued",
                    "queue_handle": handle,
                })

        async def write_admitted() -> None:
            handle = {"request_id": request_id, "host": host}
            written = await self.store.set_spawn_outcome(
                host,
                name,
                "admitted",
                request_id=request_id,
                reason="codex_boot_permit_admitted",
                delivery_evidence="queue_admitted",
                delivery_receipt={
                    "state": "admitted",
                    "delivery_status": "pending",
                    "queue_handle": handle,
                },
                effective_model=open_flds.get("effective_model"),
                effective_effort=open_flds.get("effective_effort"),
                idempotency_key=idempotency_key or None,
                request_payload_hash=payload_hash or None,
            )
            if written is False:
                raise VerbError("spawn_fence_lost", "cancelled spawn cannot leave the boot queue")
            await self._publish_spawn_state(host, name, "starting")

        semaphore = self._codex_boot_semaphore(host, cap)
        queued = semaphore.locked()
        permit = asyncio.create_task(semaphore.acquire())
        try:
            if queued:
                await write_queued()
            await asyncio.wait_for(permit, timeout_s)
            if queued:
                await write_admitted()
        except TimeoutError as exc:
            raise SpawnQueueTimeout(host, timeout_s) from exc
        except BaseException:
            if permit.done() and not permit.cancelled():
                if permit.exception() is None:
                    semaphore.release()
            else:
                permit.cancel()
            raise
        return semaphore

    async def _spawn_fenced(
        self, host: str, name: str, request_id: str, command: str,
        brief: str, ready_marker: str, msg: dict[str, Any], created: list[bool],
        creation_uncertain: list[bool], open_flds: dict[str, Any],
        resolution: dict[str, Any], tmux: tmux_transport.Tmux,
        delivery_receipt: dict[str, Any], nonce: str = "",
        *, admission: asyncio.Future[dict[str, Any]] | None = None,
        attempt: int = 0, total_attempts: int = 1,
    ) -> dict[str, Any]:
        if not await self.store.owns_spawn_intent(host, name, request_id, nonce):
            raise VerbError("spawn_fence_lost", "spawn no longer owns its creation intent")
        provider = str((resolution.get("resolved_launch_tuple") or {}).get("provider") or "")
        promptless_codex = provider == "codex" and not brief
        try:
            # Inject the creation nonce into the pane's environment (spec §D1) so
            # the pane is born carrying its identity — adoptable by a restart
            # even in the crash window before `mark_tmux_created`.
            await tmux.new_session(
                name, command, cwd=msg.get("cwd") or None,
                env={PANE_NONCE_ENV: nonce} if nonce else None,
            )
        except VerbError as exc:
            if exc.code != "tmux_timeout":
                raise
            creation_state = await self._wait_for_created_session(name, tmux)
            if creation_state == "gone":
                raise
            if creation_state == "unknown":
                # Conservatively mark the reservation as pane evidence. It is
                # TTL-exempt and startup reconciliation can adopt a live pane
                # or release the intent after tmux explicitly says gone. The pane
                # identity is captured best-effort — the nonce in the pane's env
                # is the primary adoption signal if the pid read is inconclusive.
                creation_uncertain[0] = True
                await self._commit_pane_bound(host, name, request_id, tmux, nonce=nonce)
                raise VerbError(
                    "spawn_launch_unconfirmed",
                    "tmux creation could not be confirmed before durable admission",
                )
            # The command timed out after tmux created the pane. Treat the
            # positive liveness read as creation evidence and continue the same
            # boot+delivery obligation instead of returning a false negative.
        created[0] = True  # from here on, and only here on, cleanup may kill it
        release_deadline = (
            time.monotonic() + BOOT_READY_HARD_DEADLINE_S
            if provider == "codex" else None
        )
        # The immutable cancellation fence and request/nonce ownership are
        # checked in the same transaction as binding, including restart adoption.
        if not await self._commit_pane_bound(host, name, request_id, tmux, nonce=nonce):
            gone = await self._kill_uncommitted_pane(host, name, tmux, nonce=nonce)
            created[0] = False
            if not await self.store.spawn_cancelled(host, name, request_id):
                creation_uncertain[0] = not gone
                raise VerbError("spawn_fence_lost", "pane bind lost request ownership")
            return {
                "type": "spawn.cancelled", "ok": False, "state": "cancelled",
                "stream_id": f"{host}:{name}", "request_id": request_id,
            }

        # Admission is deliberately independent of boot readiness and initial
        # prompt receipt.  From the moment `new-session` has returned and we
        # have durable positive pane evidence, every external verb needs an
        # addressable session row.  A later, *confirmed* rollback closes this
        # row; an ambiguous rollback returns `spawn.ok` with state `starting` and the
        # row still OPEN for inspect/tell/close and periodic reconciliation.
        # Never infer pane death from a readiness or delivery failure.
        # Pane identity enriches reconciliation but is not an admission
        # precondition: the bind CAS already records it best-effort and
        # a transport that can prove `new-session` but cannot read its pid must
        # still leave the new pane addressable.
        try:
            pane_pid = await tmux.pane_pid(name)
        except Exception:  # noqa: BLE001 - identity capture is best-effort
            pane_pid = ""
        session = await self.sessions.open(
            host, name, **open_flds,
            pane_pid=pane_pid,
            pane_status="pane_alive",
            fence=request_id,
        )
        if admission is not None and msg.get("schema") == "SpawnRequestV2" and not admission.done():
            accepted = self._starting_spawn_reply(host, name, request_id, "admitted", delivery_receipt)
            accepted["session"].update(session | resolution)
            idempotency_key = str(msg.get("idempotency_key") or request_id).strip()
            if idempotency_key:
                accepted.update(await self._admitted_for_key(host, idempotency_key))
            admission.set_result(accepted)

        # B1 boot-readiness: the provider CLI must be up and accepting input,
        # else the brief lands in a shell. This is the ONLY pre-send gate.
        # A REAL provider CLI never prints a marker string — a tuple-path spawn
        # (resolution names the provider) is gated on that provider's lifted v1
        # pane predicate instead (boot_ready.py; cutover-day canary finding).
        # An explicit `ready_marker`/`command` spawn keeps the marker path (the
        # smoke tier's stub prints "READY").
        native_initial_prompt = (
            provider == "codex"
            and str(delivery_receipt.get("transport") or "") == "native_argv"
        )
        if native_initial_prompt:
            # Native argv delivery begins while Codex is booting, so an idle
            # composer is neither expected nor useful. First USER-event ingest
            # below proves both process startup and delivery.
            pass
        elif provider in READY_PREDICATES and not msg.get("ready_marker"):
            outcome = await self._await_provider_ready(
                name, provider, tmux, absolute_deadline=release_deadline,
            )
            if not outcome.ready:
                if await self._auth_context_marker_observed(host, name, provider):
                    raise VerbError(
                        AUTH_CONTEXT_FAILURE_MARKER.removesuffix(":"),
                        "provider authentication context unavailable before Claude boot",
                    )
                if outcome.reason == CODEX_RESET_BLOCKED:
                    raise VerbError(
                        CODEX_RESET_BLOCKED,
                        "Codex TUI displayed a usage-limit reset offer; automated input is blocked until a verified operator resolves it",
                        readiness_reason=CODEX_RESET_BLOCKED,
                        reset_blocked=True,
                        retryable=False,
                        nonretryable=True,
                        pane_preserved=True,
                        startup_input_blocked=True,
                        do_not_retry=True,
                    )
                raise VerbError(
                    "boot_not_ready",
                    f"{provider} TUI not ready after {outcome.elapsed_s:.1f}s "
                    f"(budget {BOOT_READY_HARD_DEADLINE_S:.0f}s; {outcome.polls} polls; "
                    f"attempt {attempt + 1}/{total_attempts})",
                )
        elif not await self._await_marker(
            name,
            ready_marker,
            BOOT_READY_HARD_DEADLINE_S,
            tmux=tmux,
            reset_guard=provider == "codex",
            absolute_deadline=release_deadline,
        ):
            raise VerbError(
                "boot_not_ready",
                f"no '{ready_marker}' within {BOOT_READY_HARD_DEADLINE_S:.0f}s",
            )

        receipt = dict(delivery_receipt)
        native_prompt_text = brief if native_initial_prompt else ""
        if native_initial_prompt:
            observed = await self._confirm_native_initial_prompt(
                f"{host}:{name}", native_prompt_text, absolute_deadline=release_deadline,
            )
            if not observed.proven:
                pane_capture = await tmux.capture(name)
                if codex_reset_interstitial_visible(pane_capture):
                    raise VerbError(
                        CODEX_RESET_BLOCKED,
                        "Codex TUI displayed a usage-limit reset offer; automated input is blocked",
                        readiness_reason=CODEX_RESET_BLOCKED,
                        reset_blocked=True,
                        retryable=False,
                        nonretryable=True,
                        pane_preserved=True,
                        startup_input_blocked=True,
                    )
                raise VerbError(
                    "native_initial_prompt_delivery_failed",
                    "native initial prompt first USER event was not confirmed: "
                    f"{observed.reason or observed.state}",
                    bootstrap_state="failed",
                    delivery_reason=observed.reason or observed.state,
                    submission_attempts=0,
                    pane_capture=pane_capture[-4000:],
                )
            receipt.update({
                "state": "delivered",
                "delivery_status": "delivered",
                "to_stream_id": f"{host}:{name}",
                "native_prompt_submitted_at": iso_now(),
                "delivery_ack_at": iso_now(),
            })
            delivery_receipt.update(receipt)
            # The prompt is already inside Codex. Clearing this local variable
            # prevents every paste/watermark/needle branch below from running.
            brief = ""

        if provider == "codex" and promptless_codex:
            try:
                current_pid = await tmux.pane_pid(name)
                readable, current_nonce = await self._tmux_nonce(name, tmux)
            except Exception:  # noqa: BLE001 - identity proof fails closed
                current_pid, readable, current_nonce = "", False, ""
            if (
                not nonce
                or str(current_pid or "") != str(pane_pid or "")
                or not readable
                or current_nonce != nonce
            ):
                raise VerbError("boot_not_ready", "Codex promptless pane identity is not verified")

        # Delivery is a readiness-confirmed *advisory* event, not admission.
        # If it fails, the outer handler closes the already-addressable row
        # only after a confirmed rollback; otherwise the row remains open for
        # durable reconciliation.
        if brief:
            before = await tmux.capture(name)  # anchor the receipt (QA #15)
            stream_id = f"{host}:{name}"
            watermark = None
            if provider in SUBMIT_PREDICATES or host != self.sessions.local_host:
                watermark = await self._watermark_before_action(
                    stream_id, absolute_deadline=release_deadline,
                )
                proof_watermark_fields = {
                    "proof_watermark": watermark.daemon_seq,
                    "proof_watermark_state": watermark.state,
                    **({"proof_watermark_reason": watermark.reason} if watermark.reason else {}),
                }
                receipt.update(proof_watermark_fields)
                delivery_receipt.update(proof_watermark_fields)
                # The reservation intent is the crash-window recovery handle.
                # Refresh its receipt before the paste so adoption can use the
                # exact pre-action boundary even if the daemon dies before the
                # indeterminate outcome row is written.  This is deliberately
                # an update of the existing intent, not a new admission.
                await self.store.record_spawn_intent(
                    host, name,
                    {
                        "open_fields": open_flds,
                        "brief": brief,
                        "delivery_receipt": delivery_receipt,
                        "operator_initiated_top_level": (
                            not open_flds.get("parent_stream_id")
                            and not open_flds.get("handoff_from_stream_id")
                            and bool((msg.get("_auth_context") or {}).get("operator_authenticated"))
                        ),
                    }, request_id=request_id, nonce=nonce,
                )
            submission_deadline = release_deadline or (
                time.monotonic() + SPAWN_SUBMISSION_PROOF_BOUND_S
            )
            initial_proof_timeout_s = SPAWN_SUBMISSION_PROOF_BOUND_S
            await tmux.paste(name, brief)
            self._submission_attempts[stream_id] = 1
            try:
                if host == self.sessions.local_host:
                    observed = await self._confirm_brief_delivery(
                        name, brief, before, provider, tmux, watermark=watermark,
                        absolute_deadline=submission_deadline,
                        proof_timeout_s=initial_proof_timeout_s,
                    )
                else:
                    observed = await self._confirm_brief_delivery(
                        name, brief, before, provider, tmux, host=host, watermark=watermark,
                        absolute_deadline=submission_deadline,
                        proof_timeout_s=initial_proof_timeout_s,
                    )
            finally:
                submission_attempts = self._submission_attempts.pop(stream_id, 1)
            if not observed:
                proof_reason = self._submission_proof_failures.pop(stream_id, "")
                detail = "brief submission not confirmed"
                if proof_reason:
                    detail = f"{detail}: {proof_reason}"
                pane_capture = await tmux.capture(name)
                if provider == "codex" and codex_reset_interstitial_visible(pane_capture):
                    raise VerbError(
                        CODEX_RESET_BLOCKED,
                        "Codex TUI displayed a usage-limit reset offer; automated input is blocked",
                        readiness_reason=CODEX_RESET_BLOCKED,
                        reset_blocked=True,
                        retryable=False,
                        nonretryable=True,
                        pane_preserved=True,
                        startup_input_blocked=True,
                    )
                raise VerbError(
                    "prompt_delivery_failed",
                    detail,
                    bootstrap_state="unsubmitted",
                    submission_attempts=submission_attempts,
                    pane_capture=pane_capture[-4000:],
                )
            receipt.update({
                "state": "delivered",
                "delivery_status": "delivered",
                "to_stream_id": f"{host}:{name}",
                "pointer_submitted_at": iso_now(),
                "delivery_ack_at": iso_now(),
            })
            delivery_receipt.update(receipt)

        # `confirmed` when a brief's echo was seen in this same live spawn,
        # `not_requested` when there was none. The adoption path (QA #16) records
        # `transcript`/`resubmitted` instead; keeping the field on every delivered
        # outcome lets `await_spawn` consumers key on it uniformly.
        evidence = (
            "native_argv" if receipt.get("transport") == "native_argv"
            else ("staged_pointer" if receipt.get("transport") == "staged"
                  else ("confirmed" if brief else "not_requested"))
        )
        # Persist the idempotency key on the DELIVERED outcome so a later same-key
        # re-fire replays this session instead of minting a duplicate pane
        # (rpc_delivery_determinism lane). Derived from `msg` here since this
        # runs inside `_spawn_fenced`.
        _idem_key = str(msg.get("idempotency_key") or request_id).strip()
        await self.store.set_spawn_outcome(
            host, name, "delivered", request_id=request_id,
            reason=receipt["state"], delivery_evidence=evidence,
            delivery_receipt=receipt,
            effective_model=open_flds.get("effective_model"),
            effective_effort=open_flds.get("effective_effort"),
            idempotency_key=_idem_key or None,
            request_payload_hash=(self._spawn_payload_hash(msg) if _idem_key else None),
        )
        # `resolution` carries the byte-parity launch-tuple fields (v1
        # daemon_worker_boot: spawn_profile / catalog_version / resolution_source
        # / requested_/resolved_/actual_launch_tuple). Empty for the explicit-
        # command path (no provider to resolve).
        #
        # AUTHORITATIVE IDENTIFIER (naming contract): the top-level `stream_id`
        # (`f"{host}:{name}"`, where `name` is the minted `v2-<hash>` or the
        # caller-supplied `session_name`) is the ONLY string a consumer passes
        # back to `inspect`/`tell`/`close`. `inspect` exact-matches the stored
        # `session_name` (sessions.resolve) — it does not strip a provider/host
        # prefix or translate conventions. v2 rows are `v2-<hash>`; v1-adopted
        # rows keep their `claude-<host>-<hash>` name. The daemon NEVER emits or
        # accepts a reconstructed `claude-<host>-<hash>` name for a v2 row — a
        # consumer that rebuilds one from `session.session_name` + a provider/host
        # of its own will get `unknown_session`. Read `stream_id`; do not synthesize.
        reply = {
            "type": "spawn.ok", "ok": True, "stream_id": f"{host}:{name}",
            "state": "ready",
            "session": {**session, **resolution, "state": "ready", "bootstrap_state": "ready"},
            "initial_prompt_delivery": receipt,
        }
        # AC9 truthful reconcile: enumerate every session admitted for this key so
        # a success can never under-report duplicates. The atomic claim guarantees
        # exactly one.
        if _idem_key:
            reply.update(await self._admitted_for_key(host, _idem_key))
        await self._publish_spawn_state(host, name, "ready")
        return reply

    # -- resolution layer (item 1: server-side provider resolution) ----------

    async def _resolve_launch(
        self, msg: dict[str, Any], host: str, name: str
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        """Turn the real CLI spawn payload into `(command, resolution, overrides)`.

        `resolution` is the byte-parity launch-tuple metadata for `spawn.ok`;
        `overrides` are the resolved columns to persist on the open row. Both are
        empty on the explicit-`command`, no-provider path (the smoke tier), while
        that command still receives the target-host PATH envelope.
        """
        explicit = str(msg.get("command") or "").strip()
        provider = str(msg.get("provider") or "").strip()
        handoff = bool(msg.get("handoff"))

        # Captured BEFORE resolution: v1 passes the model/effort FLAG to the CLI
        # only when the client explicitly sent it (else the CLI default, which the
        # profile is defined to match). The launch tuple still records the
        # resolved canonical value either way.
        requested_model = msg.get("model")
        requested_effort = msg.get("effort")

        if not provider and not handoff:
            command = explicit or self.default_command
            if not command:
                raise VerbError("bad_request", "provider (or command) is required")
            return self._envelope_command(command, self._launch_machine(host)), {}, {}

        schema = msg.get("schema")
        try:
            if handoff:
                # The successor's tuple comes ENTIRELY from `resolve_handoff`
                # against the retiring stream (item 2). A bare `--handoff` with no
                # --model or --role inherits the source effective tuple; the real
                # CLI resolves this client-side and sends the same inherited tuple, which
                # re-resolves to the identical result here.
                resolved = await self._resolve_handoff(msg, host, name=name)
            elif schema is not None:
                resolved = validate_v2(
                    schema=str(schema), provider=provider,
                    model=str(requested_model or ""), effort=str(requested_effort or ""),
                    host=host, spawn_profile=str(msg.get("spawn_profile") or ""),
                    catalog_version=str(msg.get("catalog_version") or ""),
                    resolution_source=str(msg.get("resolution_source") or ""),
                )
            else:
                resolved = resolve_spawn(
                    provider=provider or None, model=requested_model,
                    effort=requested_effort, host=host, legacy=True,
                )
        except SpawnProfileError as exc:
            raise VerbError(exc.code, str(exc)) from exc

        provider = resolved["provider"]
        model = resolved["model"]
        effort = resolved["effort"]
        role = resolved.get("role", msg.get("role"))
        # Explicit-only flags (v1 `launch_model = model if requested_model is not
        # None`): a profile-default spawn passes no flag and the CLI uses its own
        # default. A handoff always launches the successor on the inherited tuple,
        # so its flags are always passed even when the client sent no --model.
        launch_model = model if (requested_model is not None or handoff) else None
        launch_effort = effort if (requested_effort is not None or handoff) else None

        session_id = jsonl_path = token_hash = ""
        if explicit:
            # Tests may resolve a tuple AND pin the command (stub launch). Honor
            # the command but still stamp the resolved tuple fields.
            command = self._envelope_command(explicit, self._launch_machine(host, provider))
        else:
            # A remote tuple spawn builds its launch from the TARGET host's
            # machines.json profile — the local stopgap's paths are wrong on a
            # peer (cutover-day remote-canary finding: local-host claude
            # command shipped to remote-host → boot_not_ready).
            machine = self._launch_machine(host, provider)
            if machine is None:
                raise VerbError("spawn_launch_unavailable",
                                "no local machine profile configured for tuple-path spawn")
            try:
                plan = launch.build_launch(
                    machine, provider=provider, tmux_session=name,
                    launch_model=launch_model, launch_effort=launch_effort,
                    initial_prompt_file=(
                        str(msg.get("_native_initial_prompt_path") or "")
                        if provider == "codex" and not explicit else None
                    ),
                )
            except ValueError as exc:
                raise VerbError("spawn_launch_unavailable", str(exc)) from exc
            await self._stage_launch_token(
                plan,
                host=host,
                name=name,
                provider=provider,
            )
            token_hash = hashlib.sha256(plan.stream_token.encode("utf-8")).hexdigest()
            command, session_id, jsonl_path = plan.command, plan.session_id, plan.jsonl_path

        tuple_fields = {"provider": provider, "model": model, "effort": effort}
        resolution = {
            "spawn_profile": resolved["spawn_profile"],
            "catalog_version": resolved["catalog_version"],
            "resolution_source": resolved["resolution_source"],
            "requested_launch_tuple": dict(tuple_fields),
            "resolved_launch_tuple": dict(tuple_fields),
            "actual_launch_tuple": dict(tuple_fields),
        }
        if resolved.get("_handoff_model_change_warning"):
            resolution["handoff_model_change_warning"] = resolved["_handoff_model_change_warning"]
        overrides: dict[str, Any] = {
            "role": role,
            "provider": provider,
            "requested_model": model,
            "requested_effort": effort,
            "effective_model": model,
            "effective_effort": effort,
        }
        if not explicit:
            configured = machine.claude_bin if provider == "claude" else machine.codex_bin
            parts = launch._command_parts(configured)
            overrides["observer_binding"] = {
                "executable": launch._resolve_local_executable(parts[0]) if parts else "",
            }
        if session_id:
            overrides["claude_session_id"] = session_id
        if jsonl_path:
            overrides["jsonl_path"] = jsonl_path
        if token_hash:
            overrides["token_hash"] = token_hash
            overrides["token_hash_version"] = STREAM_TOKEN_HASH_VERSION
        return command, resolution, overrides

    # -- handoff (item 2: spawn --handoff minimal contract) ------------------

    async def _resolve_handoff(
        self, msg: dict[str, Any], host: str, *, name: str | None = None
    ) -> dict[str, Any]:
        """The successor inherits the retiring stream's effective (provider,
        model, effort, role) via `resolve_handoff` (`source_effective` inheritance,
        `missing_effective: reject` — see `_handoff_policy()`). The RAW requested
        provider/model/effort/role are the target overrides: a field absent from the
        wire (a bare `--handoff`) is None and inherits the source; an explicit
        value changes the tuple. A changed tuple warns and proceeds;
        `--confirm-model-change` suppresses only that warning. Legacy override
        objects remain input-compatible as request metadata."""
        handoff_from = str(msg.get("handoff_from_stream_id") or "").strip()
        if not handoff_from or ":" not in handoff_from:
            raise VerbError("handoff_source_unknown", "handoff_from_stream_id is required")
        src_host, src_name = handoff_from.split(":", 1)
        source_row = await self.store.fetch_session(src_host, src_name) or {}
        try:
            hr = resolve_handoff(
                source_provider=source_row.get("provider"),
                source_model=source_row.get("effective_model"),
                source_effort=source_row.get("effective_effort"),
                source_role=source_row.get("role"),
                provider=(str(msg.get("provider") or "").strip() or None),
                model=msg.get("model"),
                effort=msg.get("effort"),
                role=msg.get("role"),
                host=host,
            )
        except SpawnProfileError as exc:
            raise VerbError(exc.code, str(exc)) from exc
        resolved = dict(hr.spawn)
        if not hr.changed:
            return resolved

        # HandoffResolution is the policy boundary shared with agent-orch. Do
        # not duplicate an approval-era decision here: the actual SpawnCtl/RPC
        # path must execute the deployment-owned warn-and-proceed contract that
        # resolved this tuple. A malformed or stale resolution fails closed
        # rather than silently restoring the retired confirmation gate.
        policy = hr.policy
        if policy.get("tuple_change") != "warn_and_proceed":
            raise VerbError(
                "handoff_policy_invalid",
                "handoff tuple-change policy must be warn_and_proceed",
            )
        if (
            msg.get("confirm_model_change") is True
            and policy.get("confirmation_flag_effect") == "suppress_warning"
        ):
            return resolved

        warning = {
            "changed_fields": list(hr.changed_fields),
            "source_tuple": hr.source,
            "requested_tuple": {
                field: hr.spawn[field] for field in HANDOFF_TUPLE_FIELDS
            },
        }
        log.warning(
            "handoff model-change warning: source_seat=%s target_seat=%s "
            "changed_fields=%s source_tuple=%s requested_tuple=%s",
            handoff_from,
            f"{host}:{name}" if name else host,
            ",".join(hr.changed_fields),
            warning["source_tuple"],
            warning["requested_tuple"],
        )
        resolved["_handoff_model_change_warning"] = warning
        return resolved

    async def _finish_handoff(self, msg: dict[str, Any], successor_stream_id: str) -> None:
        """Post-boot handoff steps, best-effort and non-raising: the successor is
        already live and returned, so neither failing here fails the spawn.
        Re-parent the retiring leader's direct children to the successor
        (v1 default; `--no-reparent-children` opts out), THEN close the retiring
        session via the normal close path.  Parentage is central metadata, so
        children on configured SSH peers are moved in the same operation."""
        handoff_from = str(msg.get("handoff_from_stream_id") or "").strip()
        if not handoff_from or ":" not in handoff_from:
            return
        if bool(msg.get("reparent_children", True)):
            try:
                moved = await self.sessions.reparent_children(handoff_from, successor_stream_id)
                if moved:
                    log.info("handoff reparented %d child(ren) %s -> %s",
                             moved, handoff_from, successor_stream_id)
            except Exception:  # noqa: BLE001 - a reparent failure never fails the handoff
                log.exception("handoff child reparent failed")
        src_host, src_name = handoff_from.split(":", 1)
        try:
            await self.sessions.close(src_host, src_name, reason="handed_off")
        except VerbError as exc:
            log.info("handoff close of %s: %s", handoff_from, exc.code)

    # -- reservation identity probes (spec §D1) ------------------------------

    async def _pane_started_at(self, pane_pid: str, *, host: str = "") -> str:
        """A pane's process start identity (lstart-based, stable across PID
        reuse on macOS and Linux). Corroborates `pane_pid` at adoption (spec
        §D1). Empty when unreadable — the match then rests on `pane_pid` alone."""
        if not pane_pid or not str(pane_pid).isdigit():
            return ""
        if host and host != self.sessions.local_host and self.hosts is not None:
            try:
                rc, out = await self.hosts.run_command(
                    host, "ps", "-o", "lstart=", "-p", str(pane_pid), timeout=10.0,
                )
            except Exception:  # noqa: BLE001 - remote identity is best-effort
                return ""
            return out.strip() if rc == 0 else ""
        from prockill import process_record
        record = await process_record(str(pane_pid))
        return str(record.get("start_id") or "") if record else ""

    async def _commit_pane_bound(
        self, host: str, name: str, request_id: str, tmux: tmux_transport.Tmux,
        *, nonce: str | None = None,
    ) -> bool:
        """Capture pane identity and atomically bind the original request and nonce.

        A cancelled, missing, or replaced reservation cannot admit the pane."""
        pane_pid = ""
        started_at = ""
        try:
            pane_pid = await tmux.pane_pid(name)
            if pane_pid:
                started_at = await self._pane_started_at(pane_pid, host=host)
        except Exception:  # noqa: BLE001 - identity is best-effort corroboration
            pass
        return await self.store.commit_tmux_created_fenced(
            host, name, request_id=request_id, nonce=nonce,
            pane_pid=pane_pid, pane_started_at=started_at,
        )

    async def _kill_uncommitted_pane(
        self, host: str, name: str, tmux: tmux_transport.Tmux, *, nonce: str = "",
    ) -> bool:
        """Verify teardown of a pane created before a rejected bind.

        Production transports verify the creation nonce and kill a stable pane
        id. An unconfirmed kill retains the durable reservation for reconciliation."""
        for _ in range(ROLLBACK_KILL_ATTEMPTS):
            try:
                if await tmux.session_state(name) == "gone":
                    return True
            except Exception:  # noqa: BLE001 - ambiguity stays non-destructive
                pass
            try:
                identity_reader = getattr(tmux, "pane_identity", None)
                identity = await identity_reader(name) if callable(identity_reader) else None
                if nonce and callable(getattr(tmux, "run", None)):
                    readable, observed = await self._tmux_nonce(name, tmux)
                    if not readable or observed != nonce:
                        return False
                if callable(identity_reader):
                    if not identity or not identity.get("pane_id"):
                        return False
                    await tmux.kill_pane(identity["pane_id"])
                else:
                    await tmux.kill_session(name)
            except Exception:  # noqa: BLE001 - a transient tmux stall must not strand us
                pass
        try:
            gone = await tmux.session_state(name) == "gone"
        except Exception:  # noqa: BLE001
            gone = False
        if not gone:
            log.warning(
                "cancelled spawn %s:%s: pane teardown unconfirmed, left to reconciler",
                host, name,
            )
        return gone

    async def _tmux_nonce(self, name: str, tmux: tmux_transport.Tmux) -> tuple[bool, str]:
        """Read the nonce from the target session's own tmux environment."""
        try:
            rc, out = await tmux.run(
                "show-environment", "-t", tmux_transport._target(name), PANE_NONCE_ENV,
                timeout=10.0,
            )
        except Exception:  # noqa: BLE001 - transport ambiguity defers
            return (False, "")
        prefix = f"{PANE_NONCE_ENV}="
        line = next((item for item in out.splitlines() if item.startswith(prefix)), "")
        if rc == 0 and line:
            return (True, line[len(prefix):])
        return (False, "")

    async def _settle_identity_alive(
        self, res: dict[str, Any], host: str, name: str, request_id: str,
        past_deadline: bool, tmux: tmux_transport.Tmux,
    ) -> str:
        """Bind a LIVE same-name pane to its reservation (spec A1 three-way, A2).

        Returns ``"adopt"`` (our pane — caller persists the row and delivers),
        ``"released"`` (terminalized — foreign or nonce-less; the pane is NEVER
        killed), or ``"deferred"`` (retained for a later pass). The nonce path
        is explicit: **match → adopt; read-and-differs → terminalize foreign;
        unreadable → defer to the bounded deadline**."""
        outcome_identity = self._reconcile_outcome_identity(res)
        res_nonce = str(res.get("nonce") or "")
        if res_nonce:
            readable, pane_nonce = await self._tmux_nonce(name, tmux)
            if not readable:
                # A1: an unreadable tmux probe is NOT a mismatch. Defer; a
                # transient transport failure must never destroy a live spawn's
                # recovery handle. INV-5 bounds the deferral.
                if past_deadline:
                    await self.store.set_spawn_outcome(
                        host, name, "failed", request_id=request_id,
                        reason="spawn_identity_unverifiable: pane nonce unreadable past deadline",
                        **outcome_identity,
                    )
                    return "quarantined"
                log.info("deferred unreadable-nonce spawn intent host=%s name=%s", host, name)
                return "deferred"
            if pane_nonce != res_nonce:
                # A1: read-and-differs -> FOREIGN. Never adopt, never deliver the
                # brief, never kill the pane (ambiguous liveness -> never kill).
                await self.store.set_spawn_outcome(
                    host, name, "failed", request_id=request_id,
                    reason="foreign_pane: nonce mismatch",
                    **outcome_identity,
                )
                await self.store.release_stream_id_fenced(host, name, request_id)
                return "released"
            return "adopt"
        await self.store.set_spawn_outcome(
            host, name, "failed", request_id=request_id,
            reason="boot_not_ready", delivery_evidence="failed",
            **outcome_identity,
        )
        await self.store.release_stream_id_fenced(host, name, request_id)
        return "released"

    @staticmethod
    def _reconcile_outcome_identity(reservation: dict[str, Any]) -> dict[str, str | None]:
        """Carry a reconciled reservation's existing same-key identity into
        its terminal outcome.

        The atomic initial claim already owns admission.  This is only the
        data-completeness bridge that makes a terminal decision made later by
        the existing reconciler replayable through that same claim.
        """
        return {
            "idempotency_key": str(reservation.get("idempotency_key") or "") or None,
            "request_payload_hash": (
                str(reservation.get("request_payload_hash") or "") or None
            ),
        }

    @staticmethod
    def _reset_blocked_recorded(
        intent: dict[str, Any] | None,
        outcome: dict[str, Any] | None,
        session: dict[str, Any] | None,
    ) -> bool:
        """Recognize the durable reset block without reading or touching tmux."""
        return any(
            isinstance(value, dict)
            and (
                value.get("readiness_state") == CODEX_RESET_BLOCKED
                or value.get("readiness_reason") == CODEX_RESET_BLOCKED
                or value.get("bootstrap_state") == CODEX_RESET_BLOCKED
                or value.get("reason") == CODEX_RESET_BLOCKED
                or value.get("delivery_evidence") == CODEX_RESET_BLOCKED
            )
            for value in (intent, outcome, session)
        )

    async def _reset_blocked_spawn_recorded(
        self, host: str, name: str, intent: dict[str, Any] | None = None,
    ) -> bool:
        """Return true when restart reconciliation must preserve this pane."""
        if self._reset_blocked_recorded(intent, None, None):
            return True
        session = await self.store.fetch_session(host, name)
        # A current non-reset session wins over a stale name-keyed outcome from
        # an older lifecycle.  Reset outcomes predate generation-keyed outcome
        # storage, so checking them first could defer a fresh spawn forever.
        if session is not None:
            return self._reset_blocked_recorded(None, None, session)
        outcome = await self.store.get_spawn_outcome(host, name)
        return self._reset_blocked_recorded(None, outcome, None)

    # -- boot reconciliation of interrupted spawns (QA #5) -------------------

    async def _release_spawn_reservation(
        self, host: str, name: str, request_id: str,
    ) -> bool:
        return await self.store.release_stream_id_fenced(host, name, request_id)

    async def reconcile_spawn_intents(
        self, *, limit: int | None = None, recurring: bool = False,
    ) -> dict[str, int]:
        """Resolve the spawn intents a crashed daemon left behind.

        A spawn writes its intent, creates the pane, then persists the session
        row; a death in between leaves a live pane no row points at — invisible
        to `sessions.refresh()` and to every verb. Each surviving intent is
        settled the only way the design allows:

          pane alive -> ADOPT   finish persisting the row; the pane keeps running
                       or ROLLBACK if registration itself fails
          pane gone  -> RELEASE drop the reservation, record the failed spawn

        Normal adoption never kills a pane. A registration failure is different:
        the reservation is the spawn's ownership fence and the pane has no
        addressable session row, so a confirmed rollback is required to avoid
        burning an invisible provider seat. Ambiguous rollback liveness retains
        the intent for a later reconciliation pass.

        Startup retains the existing full local pass. The existing
        SessionReconciler also calls this with ``recurring=True, limit=1``;
        positive host evidence and one actor-local rotation keep that pass both
        cheap and fair without another scheduler or retry queue.
        """
        async with self._intent_reconcile_lock:
            return await self._reconcile_spawn_intents_locked(limit=limit, recurring=recurring)

    async def _reconcile_spawn_intents_locked(
        self, *, limit: int | None, recurring: bool,
    ) -> dict[str, int]:
        adopted = released = 0
        reservations = await self.store.reservations(include_expired=True)
        actionable: list[dict[str, Any]] = []
        for res in reservations:
            host = str(res.get("host") or "")
            owner = str(res.get("owner_instance_id") or "")
            if self.instance_id and owner == self.instance_id:
                continue
            name = str(res.get("session_name") or "")
            request_id = str(res.get("request_id") or "").strip()
            outcome = await self.store.get_spawn_outcome(host, name)
            outcome_request_id = str((outcome or {}).get("request_id") or "").strip()
            if (
                request_id
                and outcome_request_id == request_id
                and str((outcome or {}).get("state") or "")
                in {"delivered", "failed", "indeterminate"}
            ):
                if float(res.get("expires_at") or 0.0) < time.time():
                    released += int(
                        await self._release_spawn_reservation(host, name, request_id)
                    )
                continue
            if recurring:
                intent = tmux_transport._intent(res.get("payload"))
                session = await self.store.fetch_session(host, name)
                if self._reset_blocked_recorded(
                    intent,
                    outcome if session is None else None,
                    session,
                ):
                    continue
                if host != self.sessions.local_host:
                    configured = self.hosts is not None and host in getattr(self.hosts, "peers", {})
                    if configured and not self.hosts.is_online(host):
                        continue
            actionable.append(res)
        actionable.sort(key=lambda row: (str(row.get("host") or ""), str(row.get("session_name") or "")))
        if recurring and actionable and self._intent_reconcile_cursor:
            cursor = self._intent_reconcile_cursor
            split = next(
                (i for i, row in enumerate(actionable)
                 if f"{row.get('host')}:{row.get('session_name')}" > cursor),
                0,
            )
            actionable = actionable[split:] + actionable[:split]
        if limit is not None and limit > 0:
            actionable = actionable[:limit]

        for res in actionable:
            host, name = str(res["host"]), str(res["session_name"])
            request_id = str(res.get("request_id") or "")
            prior_owner = str(res.get("owner_instance_id") or "")
            if recurring:
                self._intent_reconcile_cursor = f"{host}:{name}"
            claimed = False
            if self.instance_id:
                claimed = await self.store.claim_spawn_intent(
                    host, name, request_id=request_id, prior_owner=prior_owner,
                    owner_instance_id=self.instance_id,
                )
                if not claimed:
                    continue
            try:
                disposition = await self._reconcile_one_spawn_intent(res)
            finally:
                if claimed:
                    await self.store.restore_spawn_intent_owner(
                        host, name, request_id=request_id,
                        owner_instance_id=self.instance_id, prior_owner=prior_owner,
                    )
            if disposition == "adopted":
                adopted += 1
            elif disposition == "released":
                released += 1
        if adopted or released:
            log.info("reconciled spawn intents: adopted=%d released=%d", adopted, released)
        return {"adopted": adopted, "released": released}

    async def _cleanup_cancelled_spawn(self, res: dict[str, Any]) -> str:
        """Consume a retained cancellation obligation only after confirmed cleanup.

        Unknown/offline/foreign identity keeps the handle for the existing
        recurring reconciler. Cancellation never authorizes adopting or briefing.
        """
        host, name = str(res["host"]), str(res["session_name"])
        request_id = str(res.get("request_id") or "")
        tmux = self.tmux
        if host != self.sessions.local_host:
            if (self.hosts is None or host not in getattr(self.hosts, "peers", {})
                    or not self.hosts.is_online(host)):
                return "deferred"
            tmux = self.hosts.tmux_for(host)
        state = await self._session_state_for_wait(name, tmux)
        if state == "alive":
            nonce = str(res.get("nonce") or "")
            if nonce:
                readable, observed = await self._tmux_nonce(name, tmux)
                if not readable or observed != nonce:
                    return "deferred"
            else:
                # Older rows can establish ownership with persisted process
                # identity; a name alone never grants destructive authority.
                pid = str(res.get("pane_pid") or "")
                if not pid or pid != str(await tmux.pane_pid(name)):
                    return "deferred"
                started = str(res.get("pane_started_at") or "")
                if started and started != await self._pane_started_at(pid, host=host):
                    return "deferred"
            if not await self._kill_uncommitted_pane(host, name, tmux, nonce=nonce):
                return "deferred"
        elif state != "gone":
            return "deferred"
        row = await self.store.fetch_session(host, name)
        generation = (tmux_transport._intent(res.get("payload")).get("open_fields") or {}).get(
            "session_generation"
        )
        if row and row.get("status") == "open" and generation == row.get("session_generation"):
            await self.sessions.mark_closed(
                host, name, reason="cancelled_before_bind", expected_generation=generation,
                close_kind="spawn_rollback",
            )
        await self.store.release_stream_id_fenced(
            host, name, request_id, cleanup_confirmed=True,
        )
        return "released"

    async def _reconcile_one_spawn_intent(self, res: dict[str, Any]) -> str:
        host, name = str(res["host"]), str(res["session_name"])
        request_id = str(res.get("request_id") or "")
        if await self.store.spawn_cancelled(host, name, request_id):
            return await self._cleanup_cancelled_spawn(res)
        outcome_identity = self._reconcile_outcome_identity(res)
        intent = tmux_transport._intent(res.get("payload"))
        queued_outcome = await self.store.get_spawn_outcome(host, name)
        if str((queued_outcome or {}).get("state") or "") == "queued":
            # The runtime FIFO is intentionally not replayed after a daemon
            # restart. No pane has been created, so fail the durable handle and
            # let the client resubmit rather than silently hanging it forever.
            await self.store.set_spawn_outcome(
                host,
                name,
                "failed",
                request_id=request_id,
                reason="spawn_queue_evicted",
                delivery_evidence="queue_evicted",
                delivery_receipt={
                    "state": "evicted",
                    "delivery_status": "failed",
                    "queue_handle": (
                        queued_outcome.get("delivery_receipt", {}).get("queue_handle")
                        if isinstance(queued_outcome.get("delivery_receipt"), dict) else None
                    ),
                },
                **outcome_identity,
            )
            row = await self.store.fetch_session(host, name)
            if isinstance(row, dict) and str(row.get("status") or "") == "open":
                await self._publish_spawn_state(
                    host, name, "failed", reason="spawn_queue_evicted",
                )
                await self.sessions.mark_closed(
                    host, name, reason="spawn_queue_evicted",
                    expected_generation=str(row.get("session_generation") or "") or None,
                    close_kind="spawn_rollback",
                )
                if emit_if_changed := getattr(
                    getattr(self.sessions, "_inventory_emitter", None), "emit_if_changed", None,
                ):
                    await emit_if_changed(immediate=True)
            await self.store.release_stream_id_fenced(host, name, request_id)
            return "released"
        if await self._reset_blocked_spawn_recorded(host, name, intent):
            # The pane remains the operator's recovery surface.  Do not even
            # run the liveness/identity adoption path: it can eventually call
            # `_settle_adoption`, whose only automatic recovery is input.
            log.info("preserving reset-blocked spawn intent host=%s name=%s", host, name)
            return "deferred"
        past_deadline = float(res.get("expires_at") or 0.0) < time.time()
        if is_ephemeral_probe_session(name):
            await self.store.set_spawn_outcome(
                host, name, "failed", request_id=request_id,
                reason="ephemeral probe session is never adopted",
                **outcome_identity,
            )
            await self.store.release_stream_id_fenced(host, name, request_id)
            return "released"

        tmux = self.tmux
        if host != self.sessions.local_host:
            configured = self.hosts is not None and host in getattr(self.hosts, "peers", {})
            if configured:
                # No positive reachability means no transport attempt and no
                # blind deadline release: the reservation remains the only safe
                # handle for a pane that may still be live.
                if not self.hosts.is_online(host):
                    log.info("deferred remote spawn intent host=%s name=%s", host, name)
                    return "deferred"
                tmux = self.hosts.tmux_for(host)
            else:
                if not int(res.get("tmux_created") or 0) and res.get("payload") is not None:
                    if past_deadline:
                        await self.store.set_spawn_outcome(
                            host, name, "failed", request_id=request_id,
                            reason="spawn_identity_unverifiable: host no longer matches --local-host past deadline",
                            **outcome_identity,
                        )
                        await self.store.release_stream_id_fenced(host, name, request_id)
                        return "released"
                    return "deferred"
                await self.store.set_spawn_outcome(
                    host, name, "failed", request_id=request_id,
                    reason="spawn_interrupted: reservation host no longer matches --local-host",
                    **outcome_identity,
                )
                await self.store.release_stream_id_fenced(host, name, request_id)
                return "released"

        state = await self._session_state_for_wait(name, tmux)
        if state == "alive":
            gate = await self._settle_identity_alive(
                res, host, name, request_id, past_deadline, tmux,
            )
            if gate in {"released", "quarantined"}:
                return gate
            if gate == "deferred":
                return gate
            disposition = await self._adopt_interrupted_spawn(
                host, name, request_id, intent, tmux,
                reservation=res,
                **outcome_identity,
            )
            if disposition in {"adopted", "released"}:
                await self.store.release_stream_id_fenced(host, name, request_id)
            return disposition
        if state == "gone":
            await self.store.set_spawn_outcome(
                host, name, "failed", request_id=request_id,
                reason=objective_error((intent.get("open_fields") or {}).get("objective")) or "spawn_interrupted: daemon restarted before the session row was persisted",
                **outcome_identity,
            )
            await self.store.release_stream_id_fenced(host, name, request_id)
            return "released"
        log.info("deferred indeterminate spawn intent host=%s name=%s", host, name)
        return "deferred"

    async def _adopt_interrupted_spawn(
        self, host: str, name: str, request_id: str, intent: dict[str, Any], tmux: tmux_transport.Tmux,
        *, idempotency_key: str | None = None, request_payload_hash: str | None = None,
        reservation: dict[str, Any] | None = None,
    ) -> str:
        deferred = await self.store.get_deferred_reap(f"{host}:{name}")
        if deferred is not None and (
            not deferred["done_at"]
            or not (intent.get("open_fields") or {}).get("session_generation")
            or (intent.get("open_fields") or {}).get("session_generation") == deferred["generation"]
        ):
            # Retain the tombstone even after completion: a stale interrupted
            # spawn must not resurrect the closed generation or brief its pane.
            return "deferred"
        if reservation is None:
            reservations = [r for r in await self.store.reservations(include_expired=True)
                            if r["host"] == host and r["session_name"] == name
                            and str(r.get("request_id") or "") == request_id]
            if not reservations:
                return "deferred"
            reservation = reservations[0]
        if not await self._commit_pane_bound(
            host, name, request_id, tmux, nonce=str(reservation.get("nonce") or ""),
        ):
            if await self.store.spawn_cancelled(host, name, request_id):
                return await self._cleanup_cancelled_spawn(reservation)
            return "deferred"
        if await self._reset_blocked_spawn_recorded(host, name, intent):
            # Adoption includes prompt reconciliation on the live-pane path.
            # A reset offer is explicitly outside that recovery contract, so
            # retain the reservation and leave the pane untouched.
            log.info("skipping adoption for reset-blocked pane %s:%s", host, name)
            return "deferred"
        row = await self.store.fetch_session(host, name)
        if row is None or str(row.get("status") or "") != "open":
            try:
                # `"open_fields" not in intent` means the persisted intent
                # itself could not be read back (`tmux_transport._intent()` on a missing or
                # unparseable `payload`) — distinct from a genuinely-fielded
                # intent whose `open_fields` happen to be empty/None. Admitting
                # a row in the former case would silently register a
                # wrong-tuple, unbriefed seat as if it faithfully reflected
                # the original request (spawn param-drop registration).
                if "open_fields" not in intent:
                    raise VerbError(
                        "spawn_intent_unreadable",
                        f"persisted spawn intent for {host}:{name} could not be read; "
                        "refusing to admit a defaulted registration",
                    )
                if error := objective_error((intent.get("open_fields") or {}).get("objective")):
                    raise VerbError(error, error)
                adopted_fields = dict(intent["open_fields"])
                adopted_fields["visibility"] = tmux_transport.open_fields(adopted_fields)["visibility"]
                await self.sessions.open(
                    host, name, **adopted_fields,
                    pane_pid=await tmux.pane_pid(name),
                    pane_status="pane_alive",
                    fence=request_id,
                )
            except Exception as exc:  # noqa: BLE001 - registration must fail closed
                # The pane is already live and the row write is the only step
                # that makes it addressable. The durable reservation fences this
                # pane to this spawn, so a confirmed rollback is safer than
                # leaving an invisible Claude/Codex seat burning a provider slot.
                log.exception("session registration failed during adoption for %s", f"{host}:{name}")
                rolled_back = await self._rollback_kill(host, name, tmux)
                if not rolled_back:
                    # Transport ambiguity means we cannot claim the pane is
                    # gone. Keep the intent and reservation so a later boot can
                    # retry registration; never release a live orphan blind.
                    await self.store.mark_tmux_created(host, name, request_id=request_id)
                    log.error("deferred registration rollback for %s:%s", host, name)
                    return "deferred"
                # `Sessions.open` normally either writes and caches or raises
                # before the insert. Keep the residual case safe too: a cache
                # failure after the store commit must not leave an OPEN row over
                # the pane we just confirmed dead.
                try:
                    if await self.store.fetch_session(host, name):
                        await self.sessions.mark_closed(
                            host, name,
                            reason=f"session registration failed after pane creation: {exc}",
                            reap_status="unknown",
                            survivors=[],
                            close_kind="spawn_rollback",
                        )
                except Exception:  # noqa: BLE001 - rollback already confirmed
                    log.exception("could not close partial registration row for %s:%s", host, name)
                try:
                    await self.store.set_spawn_outcome(
                        host, name, "failed", request_id=request_id,
                        delivery_evidence="registration_failed",
                        reason=exc.code if isinstance(exc, VerbError) and exc.code.startswith("objective_") else f"session registration failed after pane creation: {exc}",
                        idempotency_key=idempotency_key,
                        request_payload_hash=request_payload_hash,
                    )
                except Exception:  # noqa: BLE001 - pane rollback already confirmed
                    log.exception("could not persist registration-failure outcome for %s:%s", host, name)
                return "released"
        if objective_error((intent.get("open_fields") or {}).get("objective")):
            # Preserve the already-running legacy generation, retire its unproven intent, and never replay its brief.
            await self.store.set_spawn_outcome(host, name, "failed", request_id=request_id, reason="objective_required")
            return "adopted"
        # `await_spawn` must never pend forever on a spawn nobody will finish.
        # `_settle_adoption` reconciles the live pane to a definite outcome:
        # delivered, failed, or indeterminate. Only a dead pane is failed by
        # the caller before we are ever reached.
        brief = str(intent.get("brief") or "")
        receipt = intent.get("delivery_receipt")
        if not (
            isinstance(receipt, dict)
            and str(receipt.get("proof_watermark_state") or "") == "reachable"
        ):
            # A crash after the outcome write can leave the newest receipt in
            # `v2_spawn_outcomes` while the reservation payload still carries
            # the pre-paste version. Reuse that durable boundary when it is
            # reachable; never manufacture a fresh watermark during adoption.
            try:
                prior_outcome = await self.store.get_spawn_outcome(host, name)
                outcome_receipt = (
                    prior_outcome.get("delivery_receipt")
                    if isinstance(prior_outcome, dict) else None
                )
                if (
                    isinstance(prior_outcome, dict)
                    and request_id
                    and str(prior_outcome.get("request_id") or "") == request_id
                    and str(prior_outcome.get("state") or "") == "indeterminate"
                    and isinstance(outcome_receipt, dict)
                    and str(outcome_receipt.get("proof_watermark_state") or "") == "reachable"
                ):
                    receipt = outcome_receipt
            except Exception:  # noqa: BLE001 - missing outcome is not proof
                pass
        try:
            state, evidence, reason = await self._settle_adoption(
                host, name, brief, tmux,
                delivery_receipt=receipt if isinstance(receipt, dict) else None,
            )
        except Exception:  # noqa: BLE001 - one bad intent never aborts the boot pass
            # Preserve the reservation: a definite failure could release the
            # only durable handle while the remote pane is still live.
            log.exception("adoption reconcile failed for %s", name)
            return "deferred"
        if state == "delivered" and brief:
            async with self.sessions._lifecycle_lock(host, name):
                current = await self.store.fetch_session(host, name)
                if isinstance(current, dict):
                    created_at = str(current.get("created_at") or "")
                    updated = await self.store.update_session(
                        host,
                        name,
                        expected_generation=created_at,
                        bootstrap_state="started",
                    )
                    if updated is not None:
                        self.sessions.apply_durable(
                            f"{host}:{name}", bootstrap_state="started",
                        )
        if state == "indeterminate":
            # An authoritative Store outage is not a no-submission result. Keep
            # the existing reservation and receipt so the next reconcile can
            # retry the same post-watermark proof without admitting or pasting
            # another pane.
            receipt = dict(receipt) if isinstance(receipt, dict) else {}
            receipt.update({
                "state": "indeterminate",
                "delivery_status": "indeterminate",
                "bootstrap_state": "starting",
                "proof_state": (
                    "unreachable"
                    if "unreachable" in reason or "timeout" in reason
                    else "pending"
                ),
                "proof_watermark": receipt.get("proof_watermark"),
                "proof_watermark_state": receipt.get("proof_watermark_state"),
                "proof_watermark_reason": receipt.get("proof_watermark_reason"),
                "failure_code": (
                    receipt.get("failure_code")
                    or "native_initial_prompt_delivery_unproven"
                ),
                "failure_reason": receipt.get("failure_reason") or reason,
                "delivery_failed_at": receipt.get("delivery_failed_at") or iso_now(),
            })
            async with self.sessions._lifecycle_lock(host, name):
                current = await self.store.fetch_session(host, name)
                if isinstance(current, dict) and str(current.get("status") or "") == "open":
                    updated = await self.store.update_session(
                        host,
                        name,
                        expected_generation=str(current.get("created_at") or ""),
                        bootstrap_state="starting",
                    )
                    if updated is not None:
                        self.sessions.apply_durable(
                            f"{host}:{name}", bootstrap_state="starting",
                        )
            await self.store.set_spawn_outcome(
                host, name, "indeterminate", request_id=request_id,
                delivery_evidence=evidence, reason=reason,
                idempotency_key=idempotency_key,
                request_payload_hash=request_payload_hash,
                delivery_receipt=receipt,
            )
            return "deferred"
        pane_evidence_line = ""
        if isinstance(receipt, dict) and state == "delivered":
            receipt = {
                **receipt,
                "state": "delivered",
                "delivery_status": "delivered",
                "to_stream_id": f"{host}:{name}",
                "delivery_ack_at": iso_now(),
            }
        await self.store.set_spawn_outcome(
            host, name, state, request_id=request_id,
            delivery_evidence=evidence, reason=reason,
            idempotency_key=idempotency_key,
            request_payload_hash=request_payload_hash,
            **({"delivery_receipt": receipt} if isinstance(receipt, dict) else {}),
        )
        return "adopted"

    async def _adoption_event_proof(
        self,
        stream_id: str,
        brief: str,
        delivery_receipt: dict[str, Any] | None,
        *,
        timeout_s: float,
    ) -> EventProof | None:
        """Use the pre-admission coordinator Store watermark as adoption authority.

        A delivery receipt with a reachable pre-action watermark is the only
        safe boundary for post-restart proof.  Do not take a fresh watermark
        here: doing so could turn a USER event from this spawn into the
        boundary and erase the very evidence adoption is meant to recover.
        Old test doubles may expose only ``watermark()``; those remain
        transcript-only and fail closed when lsof cannot find a file.
        """
        if not isinstance(delivery_receipt, dict):
            return None
        if str(delivery_receipt.get("proof_watermark_state") or "") != "reachable":
            return None
        try:
            watermark = EventWatermark(
                stream_id,
                int(delivery_receipt.get("proof_watermark")),
                "reachable",
                str(delivery_receipt.get("proof_watermark_reason") or ""),
            )
        except (TypeError, ValueError):
            return None
        lookup = getattr(self.submission_proof, "lookup", None)
        if not callable(lookup):
            return EventProof(
                "unreachable", stream_id, watermark.daemon_seq,
                reason="event_store_unavailable",
            )
        bounded_timeout = min(REMOTE_EVENT_LOOKUP_BOUND_S, float(timeout_s))
        if bounded_timeout <= 0:
            return EventProof(
                "unreachable", stream_id, watermark.daemon_seq,
                reason="event_store_deadline_expired",
            )
        try:
            observed = await asyncio.wait_for(
                lookup(
                    stream_id,
                    expected_text=brief,
                    watermark=watermark,
                    timeout_s=bounded_timeout,
                ),
                timeout=bounded_timeout,
            )
        except asyncio.TimeoutError:
            return EventProof(
                "unreachable", stream_id, watermark.daemon_seq,
                reason="event_store_timeout",
            )
        except Exception:  # noqa: BLE001 - durable proof remains fail-closed
            log.exception("adoption event proof read failed for %s", stream_id)
            return EventProof(
                "unreachable", stream_id, watermark.daemon_seq,
                reason="event_store_unreachable",
            )
        if isinstance(observed, EventProof):
            return observed
        return EventProof(
            "unreachable", stream_id, watermark.daemon_seq,
            reason="event_store_invalid_response",
        )

    async def _bounded_adoption_transcript_status(
        self, name: str, needle: str, *, host: str | None, tmux: tmux_transport.Tmux,
        deadline: float,
    ) -> str:
        """Run the advisory transcript probe inside the adoption deadline.

        The fallback invocation preserves injected transcript doubles that
        predate the absolute-deadline keyword.  The outer wait still bounds
        those doubles to the same deadline.
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "no_transcript"
        try:
            try:
                probe = self._transcript_status(
                    name, needle, host=host, tmux=tmux,
                    absolute_deadline=deadline,
                )
            except TypeError as exc:
                if "absolute_deadline" not in str(exc):
                    raise
                probe = self._transcript_status(name, needle, host=host, tmux=tmux)
            return await asyncio.wait_for(probe, timeout=remaining)
        except asyncio.TimeoutError:
            return "no_transcript"

    async def _mark_reset_blocked_session(self, host: str, name: str) -> None:
        """Persist a reset interstitial discovered during live-pane adoption."""
        row = await self.store.fetch_session(host, name)
        if not isinstance(row, dict):
            return
        created_at = str(row.get("created_at") or "")
        updated = await self.store.update_session(
            host,
            name,
            expected_generation=created_at,
            bootstrap_state=CODEX_RESET_BLOCKED,
            pane_status="pane_alive",
        )
        if updated is not None:
            self.sessions.apply_durable(
                f"{host}:{name}",
                bootstrap_state=CODEX_RESET_BLOCKED,
                pane_status="pane_alive",
            )

    async def _settle_adoption(
        self, host: str, name: str, brief: str, tmux: tmux_transport.Tmux,
        *, delivery_receipt: dict[str, Any] | None = None,
    ) -> tuple[str, str, str]:
        """QA #16 FINAL reconcile for an adopted LIVE pane. Returns
        `(state, delivery_evidence, reason)`; state is `delivered`, `failed`, or
        `indeterminate`. Claude's unavailable authoritative Store is
        indeterminate, not proof of no submission:

          generation-fenced USER event after the saved watermark
                                      -> delivered/event_store
          transcript needle present  -> delivered/transcript   (submitted pre-crash)
          transcript located, absent  -> failed/transcript_absent
          no transcript located yet   -> the CLI is still booting; re-check until it
                                          appears. Boot deadline with no transcript
                                          EVER created -> failed/agent_never_started
                                          (a live pane whose agent never started).

        All file/process probes run off the event loop; boot-path only."""
        session = await self.store.fetch_session(host, name)
        if self._reset_blocked_recorded(None, None, session):
            # This defensive guard also covers direct callers of the adoption
            # settle seam.  The normal reconcile path returns earlier.
            return (
                "indeterminate",
                CODEX_RESET_BLOCKED,
                CODEX_RESET_BLOCKED,
            )
        if not brief:
            return "delivered", "not_requested", "adopted_after_restart: no brief to deliver"
        provider = str((session or {}).get("provider") or "")
        stream_id = f"{host}:{name}"
        if isinstance(delivery_receipt, dict) and (
            str(delivery_receipt.get("transport") or "") == "native_argv"
        ):
            observed = await self._confirm_native_initial_prompt(
                stream_id, brief,
                absolute_deadline=(
                    time.monotonic() + SPAWN_SUBMISSION_PROOF_BOUND_S
                ),
            )
            if observed.proven:
                return (
                    "delivered",
                    "native_argv",
                    "adopted_after_restart: first USER event proves native argv delivery",
                )
            return (
                "indeterminate",
                "live_pane_unproven",
                "native_initial_prompt_delivery_failed: "
                f"{observed.reason or observed.state}",
            )
        if provider == "codex":
            # Existing staged Codex intents predate the native route. They may
            # never be re-pasted or Enter-recovered after a restart.
            return (
                "indeterminate",
                "live_pane_unproven",
                "legacy Codex initial prompt lacks native first-event proof",
            )
        needle = tmux_transport.receipt_needle(brief)
        deadline = time.monotonic() + ADOPTION_BOOT_DEADLINE_S
        event_proof: EventProof | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                event_proof = await self._adoption_event_proof(
                    stream_id, brief, delivery_receipt,
                    timeout_s=min(REMOTE_EVENT_LOOKUP_BOUND_S, remaining),
                )
            if event_proof is not None and event_proof.proven:
                return (
                    "delivered",
                    "event_store",
                    "adopted_after_restart: exact post-watermark USER event present in generation-fenced Store",
                )
            final_store_budget = min(
                REMOTE_EVENT_LOOKUP_BOUND_S,
                remaining * ADOPTION_FINAL_EVENT_LOOKUP_FRACTION,
            ) if remaining > 0 and event_proof is not None else 0.0
            status = await self._bounded_adoption_transcript_status(
                name, needle,
                host=host if host != self.sessions.local_host else None,
                tmux=tmux,
                deadline=deadline - final_store_budget,
            )
            # The provider probe is advisory and may consume the remainder of
            # the boot budget. Always give the authoritative Store one literal
            # final chance after that probe before classifying the pane. Keep
            # the reservation as a duration, rather than recomputing it from
            # the wall clock: an event-loop wakeup can arrive just after the
            # advisory probe deadline, and dropping the pre-reserved read then
            # turns a real durable USER event into agent_never_started.
            final_proof = (
                await self._adoption_event_proof(
                    stream_id, brief, delivery_receipt,
                    timeout_s=final_store_budget,
                )
                if final_store_budget > 0 else None
            )
            if final_proof is not None and final_proof.proven:
                return (
                    "delivered",
                    "event_store",
                    "adopted_after_restart: exact post-watermark USER event present in generation-fenced Store",
                )
            if final_proof is not None:
                event_proof = final_proof
            if status == "found":
                return "delivered", "transcript", "adopted_after_restart: brief present in provider transcript"
            if event_proof is not None and event_proof.state != "pending":
                if not await tmux.has_session(name):
                    return "failed", "pane_died_during_boot", "adopted pane died before its transcript appeared"
                return (
                    "indeterminate",
                    "live_pane_unproven",
                    f"{event_proof.reason or 'event_store_unreachable'}: adoption reconciliation deferred",
                )
            if status == "absent":
                return (
                    "failed",
                    "transcript_absent",
                    "adopted_after_restart: transcript located without brief; daemon does not re-paste",
                )
            # no_transcript: the provider has not created its session log yet.
            # NEVER re-paste blind here — wait out the boot window instead.
            if not await tmux.has_session(name):
                return "failed", "pane_died_during_boot", "adopted pane died before its transcript appeared"
            if time.monotonic() >= deadline:
                log.warning("adoption: %s never created a provider transcript; agent_never_started", name)
                return "failed", "agent_never_started", "boot deadline expired with no provider transcript ever created"
            await asyncio.sleep(min(tmux_transport.POLL_INTERVAL_S, max(0.0, deadline - time.monotonic())))

    async def _transcript_status(
        self, name: str, needle: str, *, host: str | None = None,
        tmux: tmux_transport.Tmux | None = None, absolute_deadline: float | None = None,
    ) -> str:
        """Where the brief stands in the provider's durable session log:

          found         -> `needle` is in a `.jsonl` an agent process holds open
                           (submitted, durable — never scrolls off / repaints)
          absent        -> a transcript IS located but `needle` is not in it
                           (proof the brief was never submitted)
          no_transcript -> no provider `.jsonl` is located (CLI still booting, or
                           a non-agent pane)

        pane pid -> process tree (the agent CLI descends from the pane shell) ->
        the open `.jsonl` (lsof) -> a worker-thread tail read. Every step degrades
        to `no_transcript` rather than raising (QA #16 ruling)."""
        if not needle:
            return "no_transcript"
        target_tmux = tmux or self.tmux
        pane_pid = await target_tmux.pane_pid(name)
        if not pane_pid:
            return "no_transcript"
        if host and host != self.sessions.local_host:
            return await self._remote_transcript_status(
                host, pane_pid, needle, absolute_deadline=absolute_deadline,
            )
        paths = await self._open_transcripts(await process_tree(pane_pid))
        if not paths:
            return "no_transcript"
        loop = asyncio.get_running_loop()
        found = await loop.run_in_executor(None, tmux_transport._search_transcripts, paths, needle)
        return "found" if found else "absent"

    async def _remote_transcript_status(
        self, host: str, pane_pid: str, needle: str, *,
        absolute_deadline: float | None = None,
    ) -> str:
        """Read remote provider transcript evidence through the Hosts seam."""
        if self.hosts is None:
            return "no_transcript"

        async def run_bounded(*args: str) -> tuple[int, str] | None:
            remaining = (
                absolute_deadline - time.monotonic()
                if absolute_deadline is not None else REMOTE_EVENT_LOOKUP_BOUND_S
            )
            if remaining <= 0:
                return None
            timeout = min(REMOTE_EVENT_LOOKUP_BOUND_S, remaining)
            try:
                return await asyncio.wait_for(
                    self.hosts.run_command(host, *args, timeout=timeout),
                    timeout=timeout,
                )
            except Exception:  # noqa: BLE001 - remote evidence is fail-closed
                return None

        result = await run_bounded("ps", "-eo", "pid=,ppid=")
        if result is None:
            return "no_transcript"
        rc, ps_out = result
        if rc != 0:
            return "no_transcript"
        children: dict[str, list[str]] = {}
        for line in ps_out.splitlines():
            fields = line.split()
            if len(fields) >= 2:
                children.setdefault(fields[1], []).append(fields[0])
        pids: list[str] = []
        pending = [str(pane_pid)]
        while pending:
            pid = pending.pop()
            if pid in pids:
                continue
            pids.append(pid)
            pending.extend(children.get(pid, []))
        result = await run_bounded("lsof", "-p", ",".join(pids), "-Fn")
        if result is None:
            return "no_transcript"
        _lsof_rc, lsof_out = result
        paths = sorted({
            line[1:] for line in lsof_out.splitlines()
            if line.startswith("n") and line[1:].endswith(".jsonl")
            and any(frag in line for frag in tmux_transport.TRANSCRIPT_DIRS)
        })
        if not paths:
            return "no_transcript"
        result = await run_bounded("tail", "-c", str(tmux_transport.MAX_TRANSCRIPT_BYTES), *paths)
        if result is None:
            return "no_transcript"
        tail_rc, tail_out = result
        if tail_rc != 0 and not tail_out:
            return "no_transcript"
        return "found" if needle in tmux_transport.collapse_ws(tail_out) else "absent"

    @staticmethod
    async def _open_transcripts(pids: list[str]) -> list[str]:
        """The provider `.jsonl` logs those pids hold open, per lsof. `-Fn`
        prints one `n<path>` field per open file; keep the transcript ones.
        lsof exits non-zero when some pid has no files — parse stdout anyway."""
        if not pids:
            return []
        _rc, out = await tmux_transport._exec("lsof", "-p", ",".join(pids), "-Fn")
        paths = {
            line[1:] for line in out.splitlines()
            if line.startswith("n") and line[1:].endswith(".jsonl")
            and any(frag in line for frag in tmux_transport.TRANSCRIPT_DIRS)
        }
        return sorted(paths)

    # -- await_spawn (answers from the durable store, never from transport) --

    async def set_spawn_freeze(
        self, host: str, *, reason: str = "", ttl_s: float = 900.0
    ) -> dict[str, Any]:
        """Freeze admission on `host` for `ttl_s` seconds (deploy window). New
        spawns are refused with `spawn_frozen` until cleared or expiry."""
        held_until = time.time() + float(ttl_s)
        await self.store.set_spawn_admission_hold(host, held_until=held_until, reason=reason)
        return {
            "type": "spawn_freeze.ok", "ok": True, "host": host,
            "held_until": held_until, "reason": reason or None,
        }

    async def clear_spawn_freeze(self, host: str) -> dict[str, Any]:
        await self.store.clear_spawn_admission_hold(host)
        return {"type": "spawn_freeze.ok", "ok": True, "host": host, "held_until": None, "reason": None}

    async def _resolve_spawn_target(self, host: str, target: str) -> dict[str, Any]:
        """Resolve a cancel/status target (an idempotency_key OR a request_id)
        to candidate session names, live reservations, and outcome rows."""
        target = str(target or "").strip()
        names: list[str] = []
        if target:
            names = list(await self.store.admitted_session_names_for_key(host, target))
        reservations = [
            r for r in await self.store.reservations(include_expired=True)
            if str(r.get("host")) == host
            and (r.get("idempotency_key") == target or r.get("request_id") == target)
        ]
        for r in reservations:
            if r["session_name"] not in names:
                names.append(r["session_name"])
        # `tombstones` are the REQUEST/KEY-scoped cancellation records for THIS
        # target (QA cycle-3 astra-[1]); `outcomes` also folds in name-keyed rows
        # which, after a name is REUSED by a different request, may belong to a
        # DIFFERENT request. Cancellation decisions must use `tombstones`, never
        # a stray name-keyed outcome, so a reused-name new request is cancelled
        # via its own CAS instead of short-circuiting on the prior request's row.
        tombstones = await self.store.spawn_cancellations(host, target)
        outcomes = list(tombstones)
        seen = {str(oc["session_name"]) for oc in outcomes}
        for name in sorted(seen):
            if name not in names:
                names.append(name)
        for name in names:
            if name in seen:
                continue
            oc = await self.store.get_spawn_outcome(host, name)
            if oc is not None:
                outcomes.append(oc)
                seen.add(name)
        # Host-scope the request-id lookup (QA cycle-3 astra-[2]): a request id is
        # only per-host unique, so never import another host's row sharing the id.
        by_rid = await self.store.get_spawn_outcome_by_request_id(target, host=host)
        if by_rid is not None and str(by_rid.get("session_name")) not in seen:
            outcomes.append(by_rid)
            if by_rid.get("session_name") not in names:
                names.append(str(by_rid.get("session_name")))
        return {
            "names": names, "reservations": reservations,
            "outcomes": outcomes, "tombstones": tombstones,
        }

    async def spawn_cancel(self, msg: dict[str, Any], local_host: str) -> dict[str, Any]:
        """Cancel a request before pane bind; retain intent until cleanup is confirmed.

        The store transaction arbitrates cancellation against both normal and
        reconciler binding. A bound seat returns cancel_after_bind with its id."""
        host = str(msg.get("host") or local_host).strip()
        target = str(
            msg.get("target") or msg.get("idempotency_key") or msg.get("request_id") or ""
        ).strip()
        if not target:
            raise VerbError("bad_request", "spawn cancel requires a key or request_id target")
        resolved = await self._resolve_spawn_target(host, target)
        # Idempotent re-cancel: only THIS request/key's own tombstone short-circuits
        # (QA cycle-3 astra-[1]); a name-keyed outcome from a different request that
        # reused the name must NOT report success without cancelling the live request.
        for oc in resolved["tombstones"]:
            if str(oc.get("state")) == "cancelled":
                return {
                    "type": "spawn_cancel.ok", "ok": True, "state": "cancelled",
                    "stream_id": f"{host}:{oc.get('session_name')}",
                }
        for name in resolved["names"]:
            session = await self.store.fetch_session(host, name)
            if (session is not None and str(session.get("status")) == "open"
                    and not any(r["session_name"] == name for r in resolved["reservations"])):
                return {
                    "type": "spawn_cancel.error", "ok": False,
                    "error_code": "cancel_after_bind", "stream_id": f"{host}:{name}",
                    "error": f"spawn already bound to {host}:{name}; close it instead",
                }
        if resolved["reservations"]:
            res = resolved["reservations"][0]
            name = res["session_name"]
            # Bind and cancel share one store transaction boundary. A winning
            # cancel leaves the intent as a cleanup handle, never an adoption permit.
            result = await self.store.cancel_reservation_fenced(
                host, name, str(res.get("request_id") or "")
            )
            if result["status"] == "cancelled":
                return {
                    "type": "spawn_cancel.ok", "ok": True, "state": "cancelled",
                    "stream_id": f"{host}:{name}",
                }
            if result["status"] == "already_bound":
                return {
                    "type": "spawn_cancel.error", "ok": False,
                    "error_code": "cancel_after_bind", "stream_id": f"{host}:{name}",
                    "error": f"spawn already bound to {host}:{name}; close it instead",
                }
            # no_reservation: it was released/bound between resolve and CAS; fall
            # through to the outcome-based resolution below.
        for oc in resolved["tombstones"]:
            if str(oc.get("state")) == "cancelled":
                return {
                    "type": "spawn_cancel.ok", "ok": True, "state": "cancelled",
                    "stream_id": f"{host}:{oc.get('session_name')}",
                }
        if resolved["outcomes"]:
            oc = resolved["outcomes"][0]
            return {
                "type": "spawn_cancel.error", "ok": False, "error_code": "cancel_after_bind",
                "stream_id": f"{host}:{oc.get('session_name')}",
                "error": f"spawn already terminal ({oc.get('state')}); nothing to cancel",
            }
        return {
            "type": "spawn_cancel.error", "ok": False, "error_code": "not_found",
            "error": f"no pending or bound spawn for target on {host}",
        }

    async def spawn_status(self, msg: dict[str, Any], local_host: str) -> dict[str, Any]:
        """List the outcome/reservation rows for an idempotency key or request
        id so a caller can find the seat before retrying, plus any active hold."""
        host = str(msg.get("host") or local_host).strip()
        target = str(
            msg.get("target") or msg.get("idempotency_key") or msg.get("request_id") or ""
        ).strip()
        resolved = await self._resolve_spawn_target(host, target)
        outcomes = [
            {
                "stream_id": f"{host}:{oc.get('session_name')}" if oc.get("session_name") else None,
                "state": oc.get("state"),
                "request_id": oc.get("request_id"),
                "idempotency_key": oc.get("idempotency_key"),
                "request_payload_hash": oc.get("request_payload_hash"),
                "updated_at": oc.get("updated_at"),
            }
            for oc in resolved["outcomes"]
        ]
        reservations = [
            {
                "stream_id": f"{host}:{r.get('session_name')}",
                "request_id": r.get("request_id"),
                "idempotency_key": r.get("idempotency_key"),
                "expires_at": r.get("expires_at"),
            }
            for r in resolved["reservations"]
        ]
        hold = await self.store.get_spawn_admission_hold(host)
        return {
            "type": "spawn_status.ok", "ok": True,
            "found": bool(outcomes or reservations),
            "outcomes": outcomes,
            "reservations": reservations,
            "hold": (
                {"reason": hold.get("reason"), "held_until": hold.get("held_until")}
                if hold else None
            ),
        }

    async def await_spawn(self, msg: dict[str, Any]) -> dict[str, Any]:
        request_id = str(msg.get("spawn_request_id") or "").strip()
        stream_id = str(msg.get("stream_id") or msg.get("to_stream_id") or "").strip()
        if stream_id or msg.get("host") or msg.get("session_name"):
            host, name = await self.sessions.resolve(msg)
            stream_id = f"{host}:{name}"
            outcome = await self.store.get_spawn_outcome(host, name)
        elif request_id:
            # The request id is the only identity retained by a caller whose
            # spawn reply was lost. Resolve terminal state directly from the
            # outcome row; requiring a stream id here defeats this command's
            # purpose and was the live `bad_request` failure.
            outcome = await self.store.get_spawn_outcome_by_request_id(request_id)
            if outcome is None:
                return {
                    "type": "await_spawn.ok",
                    "ok": True,
                    "state": "starting",
                    "stream_id": None,
                    "spawn_request_id": request_id,
                }
            host = str(outcome.get("host") or "")
            name = str(outcome.get("session_name") or "")
            stream_id = f"{host}:{name}" if host and name else ""
        else:
            # Preserve the established structured bad_request for callers that
            # supplied neither a stream target nor a spawn request identity.
            host, name = await self.sessions.resolve(msg)
            stream_id = f"{host}:{name}"
            outcome = await self.store.get_spawn_outcome(host, name)
        if outcome is None:
            return {"type": "await_spawn.ok", "ok": True, "state": "starting", "stream_id": stream_id}
        state = str(outcome["state"])
        if state == "delivered":
            state = "ready"
        elif state in {"admitted", "indeterminate"}:
            state = "starting"
        base = {
            "stream_id": stream_id,
            "session": self.sessions.get(stream_id) or await self.store.fetch_session(host, name),
            "state": state,
            # How delivery was established: `confirmed` / `not_requested` on a
            # fresh spawn; `transcript` / `resubmitted` when a live pane was
            # adopted after a restart; `agent_never_started` on a failed adoption
            # whose provider never came up (QA #16 FINAL / determinism item (a)).
            "delivery_evidence": outcome.get("delivery_evidence"),
        }
        if isinstance(outcome.get("delivery_receipt"), dict):
            base["initial_prompt_delivery"] = outcome["delivery_receipt"]
        queue_handle = (
            outcome["delivery_receipt"].get("queue_handle")
            if isinstance(outcome.get("delivery_receipt"), dict) else None
        )
        if outcome["state"] in {"queued", "admitted"}:
            return {
                "type": "await_spawn.ok",
                "ok": True,
                **base,
                "queue_handle": queue_handle,
            }
        if outcome["state"] == "delivered":
            return {"type": "await_spawn.ok", "ok": True, **base}
        if (
            outcome.get("reason") == CODEX_RESET_BLOCKED
            or outcome.get("delivery_evidence") == CODEX_RESET_BLOCKED
        ):
            return {
                "type": "await_spawn.error",
                "ok": False,
                "error_code": CODEX_RESET_BLOCKED,
                "error": "Codex TUI reset offer requires a verified operator action",
                "retryable": False,
                "nonretryable": True,
                "pane_preserved": True,
                "readiness_reason": CODEX_RESET_BLOCKED,
                **base,
            }
        if outcome["state"] == "indeterminate":
            spawn_request_id = str(outcome.get("request_id") or request_id).strip()
            return {
                "type": "await_spawn.ok",
                "ok": True,
                **base,
                "pending_reconcile": True,
                "action_status": "committed",
                "confirmation_status": "pending",
                "action_committed": True,
                "confirmation_pending": True,
                "do_not_respawn": True,
                "reconcile": "await_spawn",
                "reconcile_command": (
                    f"agent-orch await-spawn --request-id {spawn_request_id} --timeout 30"
                    if spawn_request_id else
                    f"agent-orch await-spawn --stream-id {stream_id} --timeout 30"
                ),
                "retry_guidance": (
                    "Action is committed; confirmation is pending. DO NOT RESPAWN; "
                    "reconcile through await_spawn."
                ),
            }
        error_code = str(outcome.get("reason") or "").split(":", 1)[0]
        if error_code not in {"spawn_queue_timeout", "spawn_queue_evicted"}:
            error_code = "spawn_failed"
        return {"type": "await_spawn.error", "ok": False, "error_code": error_code,
                "error": outcome.get("reason") or "spawn failed", **base}
