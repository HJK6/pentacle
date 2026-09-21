"""event_push.py — the `event.push` verb: per-host satellite ingest sink.

A stateless per-host satellite agent (`satellite.py`) tails that host's local
provider transcripts and pushes normalized wire events here over one outbound
WebSocket. This verb is the coordinator-side ingest sink that replaces both v1's
per-session SSH tail pipes (the remote-tail wedge) and v2's never-built "remote
host runs its own daemon" deferral, so linux-workstation/workstation session chat reaches
`session_event_tail` (and therefore mobile) with the same exactly-once
guarantee as local ingest.

REUSES THE LOCAL-INGEST FLOOR (nothing new about dedupe):
  - `store.append_session_event`'s INSERT OR IGNORE is the single writer, keyed
    on `event_key` (content hash) AND the durable `identity` unique index.
  - `chat.event` is broadcast ONLY on a real insert, so a satellite replay
    (restart rescans from offset 0) never re-broadcasts. This is the exact
    accept-order lifted in `ingest.py` — the satellite is just a remote source
    feeding the same store.
  - Identity is recomputed HERE from the payload via `ingest._identity_key`, not
    trusted from the wire, so dedupe is version-independent of the satellite.

VERSION GATE + DEPLOY-OWNED PIN: every push carries the satellite's git SHA +
wire schema version. The deploy procedure stages a full SHA in the durable
`event_push.target_sha` key. There is no implicit daemon-HEAD default: an exact
match ingests, while any unequal SHA is told to update to the target. coordinator only
names a SHA; code only ever comes from git origin (the satellite never runs
coordinator-supplied code).

AUTH: a bearer push-secret (env `PENTACLE_EVENT_PUSH_SECRET`, kv fallback
`event_push.secret`) verified constant-time BEFORE any insert/broadcast — the
same authenticate-before-side-effect ordering as the fleet stream_token verbs
(`ledger.ingest`), adapted for a host-level, multi-session agent that is not
itself a registered session. Fail-closed: with no secret configured coordinator refuses
every push (`event_push_unconfigured`) rather than accept unauthenticated bytes.

LOOP RULE / kill switch: `--disable-event-push-ingest` (main.py) makes this verb
return `event.push.error` `ingest_disabled`; the satellite backs off and keeps
its offsets so nothing is lost while ingest is off.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from typing import Any, Awaitable, Callable, Mapping

from ingest import _identity_key, codex_source_pane_pid, validate_event_payload
from machine_stats import WIRE_VERSION as STATS_WIRE_VERSION, validate_machine_stats
from store import ENTRY_DROPPED

log = logging.getLogger("chat_streamd_v2.event_push")

#: Wire schema version of the normalized payloads the satellite sends. Bumped
#: only when the on-the-wire event shape changes incompatibly; the ack echoes
#: the server's supported version so a too-old/too-new satellite is visible.
WIRE_VERSION = 1

#: Hard cap on events accepted in a single push, so one oversized batch cannot
#: monopolize the store thread (loop rule 2, sink side). The satellite's per-pass
#: cap keeps it well under this; a batch over the cap is rejected, not truncated.
MAX_BATCH = 2000

#: Minimum seconds between pin-drift alerts for one host, so repeated reconcile
#: ticks do not become their own alert storm.
STALE_ALERT_MIN_INTERVAL_S = 300.0

_FULL_GIT_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_HOST_PROOF_FIELDS = frozenset({"type", "request_id", "push_secret", "satellite_sha", "satellite_pid", "wire_version", "host", "events", "usage", "high_water", "inventory", "frozen_streams", "source_host_proof"})
_USAGE_REQUIRED_FIELDS = frozenset({
    "stream_id", "provider", "session_generation", "source_pane_pid",
    "native_session_id", "source_file_identity_digest", "records",
})
_USAGE_FORBIDDEN_KEYS = frozenset({
    "collection_host", "prompt", "path", "file_path", "source_path",
    "credential", "credentials", "secret", "token",
})

Broadcast = Callable[[dict[str, Any]], Awaitable[None]]


class EventPush:
    """The `event.push` ingest sink.

    Its durable inputs are the pin and secret read from kv.
    """

    def __init__(
        self,
        store: Any,
        broadcast: Broadcast,
        alerts: Any,
        *,
        recent_limit: int,
        enabled: bool = True,
        routing_integrity: Any = None,
        presence: Any = None,
        sessions: Any = None,
        inventory_emitter: Any = None,
        daemon_sha: str = "",
        host_stats_handler: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        host_secrets: Mapping[str, str] | None = None,
    ) -> None:
        self.store = store
        self.broadcast = broadcast
        self.alerts = alerts
        self.recent_limit = recent_limit
        self.enabled = enabled
        self.routing_integrity = routing_integrity
        self.presence = presence
        self.sessions = sessions
        self.inventory_emitter = inventory_emitter
        self.daemon_sha = daemon_sha.strip()
        self.host_stats_handler = host_stats_handler
        self.host_secrets = {
            str(host): str(secret) for host, secret in (host_secrets or {}).items()
            if str(host) and str(secret)
        }
        self._last_alert: dict[tuple[str, str], float] = {}
        # A rejected tail cannot offer an event proving that its row opened
        # again.  Keep its observed lifecycle only until that transition, then
        # return the stream in an ack for the satellite to unfreeze.  This is
        # also the log-dedup state, so recovery deliberately re-arms a later
        # independent drop instead of suppressing it for the daemon lifetime.
        self._dropped_admissions: dict[tuple[str, str], tuple[str, str]] = {}

    def wire_handlers(self) -> dict[str, Callable[[dict], Awaitable[dict]]]:
        return {"event.push": self.handle_push, "host.stats": self.handle_host_stats}

    async def handle_host_stats(self, msg: dict) -> dict:
        """Authenticate one satellite sample, then hand it to the daemon owner."""
        rid = msg.get("request_id")

        def err(code: str, *, version: dict | None = None) -> dict:
            reply = {"type": "host.stats.error", "request_id": rid, "error": code}
            if version is not None:
                reply["version"] = version
            return reply

        if not self.enabled:
            return err("ingest_disabled")
        secret = await self._secret()
        if not secret:
            return err("event_push_unconfigured")
        if not hmac.compare_digest(str(msg.get("push_secret") or ""), secret):
            return err("unauthorized")
        host = str(msg.get("host") or "")
        if not host:
            return err("bad_stats")
        if msg.get("wire_version") != STATS_WIRE_VERSION:
            return err("unsupported_version")
        version = await self._version_verdict(host, str(msg.get("satellite_sha") or ""))
        if version["status"] != "ok":
            return err("satellite_version_mismatch", version=version)
        stats = validate_machine_stats(msg.get("stats"), host)
        if stats is None:
            return err("bad_stats")
        if self.host_stats_handler is None:
            return err("host_stats_unconfigured")
        await self.host_stats_handler(host, stats)
        reply = {
            "type": "host.stats.ok",
            "request_id": rid,
            "version": version,
            "wire_version": STATS_WIRE_VERSION,
        }
        try:
            fences = await self._usage_fences(host)
        except Exception as exc:  # noqa: BLE001 - stats remains available if usage projection fails
            log.warning("usage fence projection failed host=%s: %s", host, exc)
            fences = []
        if fences:
            reply["usage_fences"] = fences
        return reply

    # -- config -------------------------------------------------------------

    async def _secret(self) -> str | None:
        env = os.environ.get("PENTACLE_EVENT_PUSH_SECRET")
        if env:
            return env
        return await self.store.get("event_push.secret")

    async def _host_secret(self, host: str) -> str | None:
        """Resolve the per-host proof secret without accepting a caller secret."""
        value = await self.store.get(f"event_push.host_secret.{host}")
        if value:
            return str(value)
        value = self.host_secrets.get(host)
        if value:
            return value
        # A single-host test/deployment can use the satellite's documented
        # environment value; durable per-host keys take precedence above.
        value = os.environ.get("PENTACLE_EVENT_PUSH_HOST_SECRET")
        return value or None

    @staticmethod
    def _host_proof_message(host: str, satellite_sha: str, satellite_pid: int) -> bytes:
        return f"event.push.v1\0{host}\0{satellite_sha}\0{satellite_pid}".encode("utf-8")

    async def _authenticate_source_host(self, msg: dict[str, Any]) -> str | None:
        """Verify the host identity before runtime, event or usage side effects."""
        host = str(msg.get("host") or "").strip()
        proof = msg.get("source_host_proof")
        satellite_sha = str(msg.get("satellite_sha") or "")
        satellite_pid = msg.get("satellite_pid")
        if (
            not host
            or not isinstance(proof, str)
            or _FULL_GIT_SHA.fullmatch(satellite_sha) is None
            or type(satellite_pid) is not int
            or satellite_pid <= 0
            or re.fullmatch(r"[0-9a-f]{64}", proof) is None
        ):
            return None
        secret = await self._host_secret(host)
        if not secret:
            return None
        expected = hmac.new(
            secret.encode("utf-8"),
            self._host_proof_message(host, satellite_sha, satellite_pid),
            hashlib.sha256,
        ).hexdigest()
        return host if hmac.compare_digest(proof, expected) else None

    @classmethod
    def _validate_usage_item(cls, item: Any, *, authenticated_host: str) -> str | None:
        if not isinstance(item, dict) or set(item) != _USAGE_REQUIRED_FIELDS:
            return "bad_usage"
        stream_id = str(item.get("stream_id") or "")
        provider = item.get("provider")
        generation = item.get("session_generation")
        source_pid = item.get("source_pane_pid")
        native_id = item.get("native_session_id")
        digest = item.get("source_file_identity_digest")
        records = item.get("records")
        if (
            not stream_id.startswith(f"{authenticated_host}:")
            or stream_id.removeprefix(f"{authenticated_host}:") == ""
            or provider not in {"codex", "claude"}
            or not isinstance(generation, str) or not generation
            or not isinstance(source_pid, str) or not source_pid
            or not isinstance(native_id, str) or not native_id
            or not isinstance(digest, str) or _HEX64.fullmatch(digest) is None
            or not isinstance(records, list) or not records
        ):
            return "bad_usage"
        def contains_forbidden(value: Any) -> bool:
            if isinstance(value, dict):
                return bool(set(value) & _USAGE_FORBIDDEN_KEYS) or any(
                    contains_forbidden(child) for child in value.values()
                )
            if isinstance(value, list):
                return any(contains_forbidden(child) for child in value)
            return False
        if contains_forbidden(records):
            return "bad_usage"
        return None

    async def _usage_fences(self, host: str) -> list[dict[str, str]]:
        """Return current coordinator-issued usage fences to a satellite."""
        rows = await self.store.list_open_sessions_with_event_summary()
        fences: list[dict[str, str]] = []
        for row in rows:
            if row.get("host") != host or row.get("provider") not in {"codex", "claude"}:
                continue
            fences.append({
                "stream_id": str(row.get("stream_id") or ""),
                "session_generation": str(row.get("session_generation") or ""),
                "provider": str(row.get("provider") or ""),
                "source_pane_pid": str(row.get("pane_pid") or ""),
            })
        return [fence for fence in fences if all(fence.values())]

    async def _record_remote_usage(
        self, usage: Any, *, authenticated_host: str,
    ) -> tuple[int, int, list[dict[str, str]], list[dict[str, Any]]]:
        """Admit usage items independently, keeping request ids correlation-only."""
        if not isinstance(usage, list) or len(usage) > MAX_BATCH:
            return 0, 0, [], []
        recorded = replayed = 0
        rejected: list[dict[str, str]] = []
        accepted_items: list[dict[str, Any]] = []
        for item in usage:
            invalid = self._validate_usage_item(item, authenticated_host=authenticated_host)
            stream_id = str(item.get("stream_id") or "") if isinstance(item, dict) else ""
            if invalid is not None:
                rejected.append({"stream_id": stream_id, "reason": invalid})
                continue
            host, _, session_name = stream_id.partition(":")
            row = await self.store.fetch_session(host, session_name)
            if row is None or row.get("status") != "open":
                rejected.append({"stream_id": stream_id, "reason": "session_not_open"})
                continue
            if row.get("host") != authenticated_host:
                rejected.append({"stream_id": stream_id, "reason": "unauthorized_source_host"})
                continue
            if row.get("provider") != item["provider"]:
                rejected.append({"stream_id": stream_id, "reason": "provider_mismatch"})
                continue
            if str(row.get("session_generation") or "") != item["session_generation"]:
                rejected.append({"stream_id": stream_id, "reason": "generation_mismatch"})
                continue
            if str(row.get("pane_pid") or "") != item["source_pane_pid"]:
                rejected.append({"stream_id": stream_id, "reason": "pane_pid_mismatch"})
                continue
            outcome = await self.store.record_usage_checked(
                row, item["records"],
                native_session_id=item["native_session_id"],
                collection_host=authenticated_host,
                source_file_identity_digest=item["source_file_identity_digest"],
            )
            if not outcome.get("accepted"):
                rejected.append({"stream_id": stream_id, "reason": str(outcome.get("reason") or "usage_rejected")})
                continue
            if outcome.get("recorded"):
                recorded += 1
            elif outcome.get("replayed"):
                replayed += 1
            accepted_items.append({
                "stream_id": stream_id,
                "snapshot": outcome.get("snapshot"),
                "recorded": bool(outcome.get("recorded")),
                "replayed": bool(outcome.get("replayed")),
                "source_file_identity_digest": item["source_file_identity_digest"],
                "request_id": None,
            })
        return recorded, replayed, rejected, accepted_items

    async def _target_sha(self) -> str | None:
        pinned = await self.store.get("event_push.target_sha")
        return pinned.strip() if pinned else None

    def set_daemon_sha(self, daemon_sha: str) -> None:
        """Record the one boot-time checkout SHA captured by ``main`` off-loop."""
        self.daemon_sha = daemon_sha.strip()

    # -- verb ------------------------------------------------------------------

    async def handle_push(self, msg: dict) -> dict:
        rid = msg.get("request_id")

        def err(code: str, *, version: dict | None = None) -> dict:
            reply = {"type": "event.push.error", "request_id": rid, "error": code}
            target_sha = version.get("target_sha") if isinstance(version, dict) else None
            if (
                code in {"codex_identity_unproven", "satellite_version_mismatch"}
                and isinstance(target_sha, str)
                and _FULL_GIT_SHA.fullmatch(target_sha) is not None
            ):
                reply["version"] = version
            return reply

        if not self.enabled:
            # Kill switch: refuse without side effects. The satellite keeps its
            # in-memory offsets and backs off, so nothing is lost or duplicated.
            return err("ingest_disabled")

        # AUTH before any side effect (fleet authenticate-before-write order).
        secret = await self._secret()
        if not secret:
            log.error(
                "event.push refused: no push secret configured "
                "(set PENTACLE_EVENT_PUSH_SECRET or kv event_push.secret)"
            )
            return err("event_push_unconfigured")
        presented = str(msg.get("push_secret") or "")
        if not hmac.compare_digest(presented, secret):
            return err("unauthorized")

        host = str(msg.get("host") or "")
        # New satellites prove the source host on every event.push. Legacy
        # event-only callers remain compatible until they send a usage item;
        # a supplied proof is never optional or ignored.
        source_proof_required = "usage" in msg or msg.get("source_host_proof") is not None
        if source_proof_required:
            authenticated_source_host = await self._authenticate_source_host(msg)
            if authenticated_source_host is None:
                return err("unauthorized_source_host")
        else:
            authenticated_source_host = host
        if "usage" in msg and not isinstance(msg.get("usage"), list):
            return err("bad_usage")

        events = msg.get("events")
        if not isinstance(events, list):
            return err("bad_batch")
        if len(events) > MAX_BATCH:
            return err("batch_too_large")
        inventory = self._inventory(msg, host=str(msg.get("host") or ""))
        if inventory is False:
            return err("bad_inventory")
        frozen_streams = self._frozen_streams(msg, host=str(msg.get("host") or ""))
        if frozen_streams is False:
            return err("bad_frozen_streams")

        satellite_sha = str(msg.get("satellite_sha") or "")
        version = await self._version_verdict(host, satellite_sha)
        stale = version["status"] != "ok"
        satellite_pid = msg.get("satellite_pid")
        await self.store.put(
            f"event_push.runtime.{host}",
            json.dumps({
                "sha": satellite_sha,
                "pid": satellite_pid if type(satellite_pid) is int and satellite_pid > 0 else None,
                "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "observed_at_epoch": time.time(),
            }, sort_keys=True),
        )
        if stale:
            return err("satellite_version_mismatch", version=version)

        # A missing field comes from a satellite predating this recovery
        # handshake.  It cannot consume `reopened`, so retain its existing
        # admission state rather than mistaking the absence for an ack.
        if frozen_streams is None:
            reopened: list[str] = []
        else:
            self._forget_unfrozen_drops(host, frozen_streams)
            reopened = await self._reopened_drops(host, frozen_streams)
        new_drops: set[tuple[str, str]] = set()
        entries: list[dict[str, Any]] = []
        codex_proofs: dict[str, str] = {}
        dropped: list[dict[str, str]] = []

        def drop(stream_id: str, reason: str = "codex_identity_unproven") -> None:
            record = {"stream_id": stream_id, "reason": reason}
            if record not in dropped:
                dropped.append(record)
            key = (stream_id, reason)
            if key not in self._dropped_admissions:
                self._dropped_admissions[key] = ("", "")
                new_drops.add(key)
                log.warning("event.push dropped stream=%s reason=%s", stream_id, reason)

        for payload in events:
            invalid = validate_event_payload(payload, host)
            if invalid is not None:
                return err(invalid)
            assert isinstance(payload, dict)  # narrowed by _validate_event_payload
            stream_id = str(payload["stream_id"])
            provider = str(payload["provider"])
            wire_payload = dict(payload)
            raw = payload.get("raw")
            binding = payload.get("session_id") if provider == "claude" and isinstance(raw, dict) and payload.get("session_id") == raw.get("source_session_identity") else None
            if provider == "codex":
                source_pid = codex_source_pane_pid(payload.get("source_pane_pid"))
                if source_pid is None:
                    drop(stream_id)
                    continue
                prior_pid = codex_proofs.get(stream_id)
                if prior_pid is not None and prior_pid != source_pid:
                    drop(stream_id)
                    continue
                codex_proofs[stream_id] = source_pid
                # Source proof is only an in-flight admission field.  It is
                # deliberately not persisted in the normalized transcript row.
                wire_payload.pop("source_pane_pid", None)
            entries.append({
                "stream_id": stream_id,
                "event": wire_payload,
                "identity": _identity_key(payload),
                "lifecycle": provider == "codex",
                "claude_binding": binding,
            })

        snapshots: dict[str, dict[str, str]] = {}
        for stream_id, source_pid in codex_proofs.items():
            lifecycle = await self.store.fetch_open_session_lifecycle(
                stream_id, pane_pid=source_pid,
            )
            if lifecycle is None:
                drop(stream_id)
                continue
            snapshots[stream_id] = lifecycle
        entries = [
            entry for entry in entries
            if entry["lifecycle"] is not True or entry["stream_id"] in snapshots
        ]
        for entry in entries:
            entry["lifecycle"] = snapshots.get(entry["stream_id"])

        # The whole batch has passed admission. Stamp the existing event from
        # the current durable receipt projection before it is persisted and
        # broadcast through this established event path.
        try:
            for entry in entries:
                entry["event"] = await self.store.stamp_event_with_send_receipt(entry["event"])
        except Exception as exc:  # noqa: BLE001 - fail closed before durable append
            log.warning("event.push receipt stamp failed host=%s: %s", host, exc)
            return err("ingest_failed")

        # One transaction repeats the open pane/generation predicate for every
        # admitted append.  A close/reopen or pane replacement is that one
        # entry's verdict; a storage fault still rolls the complete batch back
        # before anything can broadcast.
        try:
            sequences = await self.store.append_session_events_lifecycle_cas(
                entries, limit=self.recent_limit,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed before broadcast
            log.warning("event.push durable batch failed host=%s: %s", host, exc)
            return err("ingest_failed")
        if sequences is None:
            return err("lifecycle_changed")

        # A predicate miss drops only its own entry.  Rejecting the batch here
        # let ONE closed-row-but-alive pane stop every session on its host from
        # ingesting; reporting it as a drop instead stops just that stream's
        # tail (the behaviour admission drops already established) and lets the
        # host's other streams land.
        admitted: list[tuple[dict[str, Any], int | None]] = []
        for entry, seq in zip(entries, sequences):
            if seq is ENTRY_DROPPED:
                drop(entry["stream_id"], "lifecycle_changed")
                continue
            admitted.append((entry, seq))
        entries = [entry for entry, _ in admitted]
        sequences = [seq for _, seq in admitted]

        # A new event that passed the durable lifecycle CAS is genuine session
        # activity. Rejected, pre-admission, lifecycle-raced, and replayed
        # events never reach this point with a new sequence, so they cannot
        # refresh the in-memory session activity overlay. RemotePresence owns
        # that overlay when enabled; updating its cache here prevents the next
        # working heartbeat from restoring its earlier activity seed.
        admitted_at_ms = int(time.time() * 1000)
        for entry, seq in zip(entries, sequences):
            if entry["claude_binding"] and self.sessions is not None:
                host, _, name = entry["stream_id"].partition(":")
                row = await self.store.fetch_session(host, name)
                if row is not None:
                    self.sessions.apply_durable(entry["stream_id"], claude_session_id=row["claude_session_id"], claude_session_lineage=row["claude_session_lineage"])
            lifecycle = entry["lifecycle"]
            if seq is None:
                continue
            if self.sessions is not None:
                self.sessions.apply_genuine_activity_event(
                    entry["stream_id"], entry["event"],
                )
            if lifecycle is None:
                continue
            if self.presence is not None:
                self.presence.record_admitted_activity(
                    entry["stream_id"],
                    generation=lifecycle["generation"],
                    now_ms=admitted_at_ms,
                )
            elif self.sessions is not None:
                last_activity = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(admitted_at_ms / 1000),
                )
                self.sessions.apply_live(entry["stream_id"], last_activity=last_activity)

        for entry in entries:
            if self.routing_integrity is not None:
                try:
                    await self.routing_integrity.observe_claude_event(entry["event"])
                except Exception as exc:  # noqa: BLE001 - existing best-effort observer
                    log.warning("routing-integrity remote observation failed sid=%s: %s", entry["stream_id"], exc)

        # Broadcast only after every submitted append has committed.  A replay
        # remains durable but has a None sequence and therefore never replays a
        # live frame.
        try:
            inserted_events = [
                {**entry["event"], "daemon_seq": seq}
                for entry, seq in zip(entries, sequences)
                if seq is not None
            ]
            projected = await self.store.project_session_events(inserted_events)
            projected_index = 0
            for entry, seq in zip(entries, sequences):
                if seq is not None:
                    await self.broadcast({
                        "type": "chat.event",
                        "event": projected[projected_index],
                    })
                    projected_index += 1
        except Exception as exc:  # noqa: BLE001 - never acknowledge a failed push
            log.warning("event.push broadcast failed host=%s: %s", host, exc)
            return err("ingest_failed")
        inserted = sum(seq is not None for seq in sequences)
        if inserted and self.inventory_emitter is not None:
            try:
                await self.inventory_emitter.emit_if_changed()
            except Exception as exc:  # noqa: BLE001 - never acknowledge a failed fanout
                log.warning("event.push inventory broadcast failed host=%s: %s", host, exc)
                return err("ingest_failed")

        usage_recorded = usage_replayed = 0
        usage_rejected: list[dict[str, str]] = []
        if "usage" in msg:
            usage_recorded, usage_replayed, usage_rejected, accepted_usage = await self._record_remote_usage(
                msg.get("usage"), authenticated_host=authenticated_source_host,
            )
            usage_projected = False
            if self.sessions is not None:
                for accepted in accepted_usage:
                    snapshot = accepted.get("snapshot")
                    stream_id = accepted.get("stream_id")
                    if (
                        not isinstance(snapshot, dict)
                        or not isinstance(stream_id, str)
                        or snapshot.get("stream_id") != stream_id
                        or not isinstance(snapshot.get("session_generation"), str)
                    ):
                        continue
                    if self.sessions.apply_durable(
                        stream_id,
                        expected_generation=snapshot["session_generation"],
                        usage=snapshot,
                    ) is not None:
                        usage_projected = True
            if usage_projected and self.inventory_emitter is not None:
                try:
                    await self.inventory_emitter.emit_if_changed()
                except Exception as exc:  # noqa: BLE001 - never acknowledge a failed fanout
                    log.warning("event.push usage inventory broadcast failed host=%s: %s", host, exc)
                    return err("ingest_failed")

        # High-water marks: echo the per-file offsets this batch reached so the
        # satellite advances its in-memory offset ONLY after a durable accept.
        # A dropped ack tells the satellite to stop its stale stream tail; failed
        # acks leave offsets unmoved, preserving at-least-once delivery.
        await self._remember_drops(new_drops)
        high_water = msg.get("high_water") if isinstance(msg.get("high_water"), dict) else {}
        return {
            "type": "event.push.ok",
            "request_id": rid,
            "accepted": len(entries),
            "dropped": dropped,
            "reopened": reopened,
            "inserted": inserted,
            "high_water": high_water,
            "version": version,
            "wire_version": WIRE_VERSION,
            "stale": stale,
            "usage_recorded": usage_recorded,
            "usage_replayed": usage_replayed,
            "usage_rejected": usage_rejected,
        }

    async def _stream_lifecycle_state(self, stream_id: str) -> tuple[str, str]:
        """Return the durable status and generation for one source stream."""
        host, separator, session_name = str(stream_id or "").partition(":")
        if not separator or not host or not session_name:
            return "missing", ""
        row = await self.store.fetch_session(host, session_name)
        if row is None:
            return "missing", ""
        return (
            str(row.get("status") or "missing"),
            str(row.get("session_generation") or ""),
        )

    async def _reopened_drops(
        self,
        host: str,
        frozen_streams: frozenset[str],
    ) -> list[str]:
        """Return frozen local streams whose rejected lifecycle was replaced.

        An initially-open stream can also be dropped (for example for an
        unproven Codex identity), so a merely open row is insufficient.  A
        closed/missing row opening, or an open generation changing, is the
        durable reopen transition that makes a retry safe.
        """
        resumed: set[str] = set()
        for (stream_id, _reason), prior in tuple(self._dropped_admissions.items()):
            stream_host, separator, _name = stream_id.partition(":")
            if (
                not separator
                or stream_host != host
                or stream_id not in frozen_streams
            ):
                continue
            try:
                current = await self._stream_lifecycle_state(stream_id)
            except Exception as exc:  # noqa: BLE001 - recovery hint must not reject ingest
                log.warning("event.push reopen probe failed stream=%s: %s", stream_id, exc)
                continue
            prior_status, prior_generation = prior
            if current[0] == "open" and (
                prior_status != "open" or current[1] != prior_generation
            ):
                resumed.add(stream_id)
        return sorted(resumed)

    def _forget_unfrozen_drops(
        self,
        host: str,
        frozen_streams: frozenset[str],
    ) -> None:
        """Retire recovery/log-dedup state after the satellite saw an ack."""
        for key in tuple(self._dropped_admissions):
            stream_id, _reason = key
            stream_host, separator, _name = stream_id.partition(":")
            if separator and stream_host == host and stream_id not in frozen_streams:
                self._dropped_admissions.pop(key, None)

    async def _remember_drops(self, drops: set[tuple[str, str]]) -> None:
        """Capture the lifecycle that must change before a tail may retry."""
        for key in drops:
            try:
                self._dropped_admissions[key] = await self._stream_lifecycle_state(key[0])
            except Exception as exc:  # noqa: BLE001 - preserve the conservative freeze
                log.warning("event.push drop lifecycle probe failed stream=%s: %s", key[0], exc)

    @staticmethod
    def _inventory(msg: dict, *, host: str) -> frozenset[str] | None | bool:
        """Validate an optional authenticated host inventory.

        Absence proves an inconclusive discovery and must never close a row. A
        list, including `[]` for tmux's explicit no-server result, is a bounded
        authoritative host snapshot after the existing frame authentication.
        """
        if "inventory" not in msg:
            return None
        inventory = msg.get("inventory")
        if (
            not host
            or not isinstance(inventory, list)
            or len(inventory) > MAX_BATCH
            or any(not isinstance(name, str) or not name for name in inventory)
        ):
            return False
        return frozenset(inventory)

    @staticmethod
    def _frozen_streams(msg: dict, *, host: str) -> frozenset[str] | None | bool:
        """Validate the frozen-tail recovery acknowledgement from one host."""
        if "frozen_streams" not in msg:
            return None
        streams = msg.get("frozen_streams")
        if (
            not host
            or not isinstance(streams, list)
            or len(streams) > MAX_BATCH
            or any(
                not isinstance(stream_id, str)
                or not stream_id
                or not stream_id.startswith(f"{host}:")
                or stream_id.removeprefix(f"{host}:") == ""
                for stream_id in streams
            )
        ):
            return False
        return frozenset(streams)

    async def _version_verdict(self, _host: str, satellite_sha: str) -> dict:
        """Accept only the exact durable pin; every other SHA must update."""
        target = await self._target_sha()
        if not target:
            return {"status": "ok", "target_sha": None}
        if satellite_sha == target:
            return {"status": "ok", "target_sha": target}
        return {"status": "update_required", "target_sha": target}

    async def check_pin_drift(self) -> bool:
        """Alert (never repair) when the durable pin differs from boot-time HEAD."""
        if _FULL_GIT_SHA.fullmatch(self.daemon_sha) is None:
            return False
        pinned = await self._target_sha()
        if pinned == self.daemon_sha:
            return False
        self._maybe_alert("pin_drift", "daemon", pinned_sha=pinned, daemon_sha=self.daemon_sha)
        return True

    def _maybe_alert(self, kind: str, host: str, **fields: object) -> None:
        now = time.time()
        key = (kind, host or "?")
        last = self._last_alert.get(key, 0.0)
        if now - last < STALE_ALERT_MIN_INTERVAL_S:
            return
        self._last_alert[key] = now
        self.alerts.emit(kind, host=host, **fields)
