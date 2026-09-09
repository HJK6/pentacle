"""v2 routing-integrity observation and alerting."""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

SERVICES_ROOT = str(Path(__file__).resolve().parents[1])
if SERVICES_ROOT not in sys.path:
    sys.path.insert(0, SERVICES_ROOT)

from _shared.spawn_profiles import canonical_model
from context_adapters import (
    ContextReading,
    context_fields,
    parse_claude_context,
    parse_codex_context_reading,
)
from inventory import InventoryEmitter
from store_specs import normalize_spec_ids

log = logging.getLogger("chat_streamd_v2.routing_integrity")


MAX_CLAUDE_TUPLE_CACHE = 1024


def _canonical_or_self(provider: str, model: str) -> str:
    try:
        return canonical_model(provider, model)
    except Exception:  # noqa: BLE001 - an unknown observed label is evidence, not a crash
        return model


def _tuple_value(value: object) -> str:
    return str(value or "").strip().lower()


class RoutingIntegrity:
    """Apply one provider observation through the durable v2 state machine."""

    def __init__(
        self,
        store: Any,
        sessions: Any,
        *,
        comms: Any = None,
        notify: Any = None,
        broadcast: Any = None,
        inventory_emitter: InventoryEmitter | None = None,
    ) -> None:
        self.store = store
        self.sessions = sessions
        # Kept only for in-process constructor compatibility; R25 never uses
        # comms to route or refuse a live routing-drift observation.
        del comms
        self.notify = notify
        self.broadcast = broadcast
        self.inventory_emitter = inventory_emitter or (
            InventoryEmitter(sessions, broadcast) if callable(broadcast) else None
        )
        self._claude_tuples: dict[str, tuple[str, tuple[str, str]]] = {}

    @asynccontextmanager
    async def _observe_guard(self):
        """Keep the caller's observation sequence inside Store's lock boundary."""
        yield

    async def observe_tuple(
        self,
        host: str,
        session_name: str,
        *,
        provider: str,
        effective: tuple[str, str] | None,
        observed_at: str | None = None,
        expected_generation: str | None = None,
    ) -> dict[str, Any] | None:
        """Compare and persist one effective tuple through its lifecycle boundary."""
        stream_id = f"{host}:{session_name}"
        kwargs = {
            "provider": provider,
            "effective": effective,
            "observed_at": observed_at,
            "expected_generation": expected_generation,
        }
        async with self.store.routing_integrity_lifecycle_lock(stream_id):
            return await self._observe_tuple_locked(host, session_name, **kwargs)

    async def observe_context(
        self,
        host: str,
        session_name: str,
        *,
        provider: str,
        reading: ContextReading,
        observed_at: str | None = None,
        expected_generation: str | None = None,
    ) -> dict[str, Any] | None:
        """Persist one valid provider-native context measurement.

        This reuses the routing observer's lifecycle lock and session row: v2
        already has the columns and inspect/status-card projection, so no new
        protocol, queue, or background owner is needed.
        """
        stream_id = f"{host}:{session_name}"
        async with self.store.routing_integrity_lifecycle_lock(stream_id):
            return await self._observe_context_locked(
                host,
                session_name,
                provider=provider,
                reading=reading,
                observed_at=observed_at,
                expected_generation=expected_generation,
            )

    async def _observe_context_locked(
        self,
        host: str,
        session_name: str,
        *,
        provider: str,
        reading: ContextReading,
        observed_at: str | None,
        expected_generation: str | None,
    ) -> dict[str, Any] | None:
        async with self._observe_guard():
            row = await self.store.fetch_session(host, session_name)
            if not isinstance(row, dict) or str(row.get("status") or "open") != "open":
                return None
            generation = str(row.get("created_at") or "")
            if expected_generation is not None and generation != expected_generation:
                return None
            if str(row.get("provider") or "").lower() != provider:
                return None
            try:
                tokens, window, level = context_fields(provider, reading)
            except ValueError:
                return None
            updated_at = str(observed_at or "").strip() or time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            )
            persisted = await self.store.update_session(
                host,
                session_name,
                expected_generation=generation,
                context_tokens=tokens,
                model_context_window=window,
                context_level=level,
                context_updated_at=updated_at,
            )
            if persisted is not None:
                stream_id = f"{host}:{session_name}"
                before = self.sessions.get(stream_id)
                self.sessions.apply_durable(
                    stream_id,
                    context_tokens=persisted.get("context_tokens"),
                    model_context_window=persisted.get("model_context_window"),
                    context_level=persisted.get("context_level"),
                    context_updated_at=persisted.get("context_updated_at"),
                )
                after = self.sessions.get(stream_id)
                if self.inventory_emitter is not None and any(
                    (before or {}).get(field) != (after or {}).get(field)
                    for field in (
                        "context_tokens", "model_context_window",
                        "context_level",
                    )
                ):
                    await self.inventory_emitter.emit_if_changed()
            return persisted

    async def _observe_tuple_locked(
        self,
        host: str,
        session_name: str,
        *,
        provider: str,
        effective: tuple[str, str] | None,
        observed_at: str | None = None,
        expected_generation: str | None = None,
    ) -> dict[str, Any] | None:
        """Compare and persist one effective tuple, including an unverified miss."""
        stream_id = f"{host}:{session_name}"
        async with self._observe_guard():
            row = await self.store.fetch_session(host, session_name)
            if not isinstance(row, dict):
                return None
            generation = str(row.get("created_at") or "")
            if expected_generation is not None and generation != expected_generation:
                return None
            requested_model = _tuple_value(row.get("requested_model"))
            requested_effort = _tuple_value(row.get("requested_effort"))
            effective_model = _tuple_value(effective[0]) if effective else ""
            effective_effort = _tuple_value(effective[1]) if effective else ""

            if not requested_model or not requested_effort:
                integrity = "unverified"
                reason = "requested_model_effort_missing"
            elif not effective_model or not effective_effort:
                integrity = "unverified"
                reason = "effective_model_effort_missing"
            elif (
                _canonical_or_self(provider, requested_model),
                requested_effort,
            ) == (
                _canonical_or_self(provider, effective_model),
                effective_effort,
            ):
                integrity = "verified"
                reason = None
            else:
                integrity = "mismatch"
                reason = (
                    f"requested={requested_model}/{requested_effort};"
                    f"effective={effective_model}/{effective_effort}"
                )
                log.error(
                    "%s routing integrity mismatch %s:%s %s",
                    provider,
                    host,
                    session_name,
                    reason,
                )

            result = await self.store.apply_routing_integrity(
                host,
                session_name,
                provider=provider,
                requested_model=requested_model,
                requested_effort=requested_effort,
                effective_model=effective_model or None,
                effective_effort=effective_effort or None,
                integrity=integrity,
                reason=reason,
                observed_at=observed_at,
                expected_generation=generation,
            )
            if not result:
                return None
            persisted = result.get("session")
            episode = result.get("episode")
            if isinstance(persisted, dict):
                # The periodic sessions cache is retired.  Keep the live
                # inventory projection alert-only, sourced from the active
                # durable episode rather than from a sampled trust value.
                active_episode = episode if isinstance(episode, dict) else None
                self.sessions.apply_durable(
                    stream_id,
                    effective_model=persisted.get("effective_model"),
                    effective_effort=persisted.get("effective_effort"),
                    routing_integrity=("mismatch" if active_episode is not None else None),
                    routing_integrity_reason=(
                        active_episode.get("reason") if active_episode is not None else None
                    ),
                    routing_integrity_updated_at=(
                        active_episode.get("updated_at") if active_episode is not None else None
                    ),
                    routing_integrity_event_id=(
                        active_episode.get("episode_id") if active_episode is not None else None
                    ),
                )
            if integrity == "mismatch" and isinstance(episode, dict):
                if result.get("should_notify"):
                    await self._notify_episode_locked(
                        episode, reason=reason or "routing_drift_detected"
                    )
            return result

    async def refresh_for_report(self, stream_id: str) -> bool | None:
        """Refresh Claude's durable event source for a report-write check.

        ``None`` means this observer is not the source for the provider;
        ``False`` means no usable fresh event was available.
        """
        host, session_name = self.sessions.split(stream_id)
        row = await self.store.fetch_session(host, session_name)
        if not isinstance(row, dict):
            return None
        provider = str(row.get("provider") or "").lower()
        if provider != "claude":
            return None
        events = await self.store.fetch_session_event_tail(stream_id, limit=64)
        observed = False
        for payload in events:
            if not isinstance(payload, dict):
                continue
            raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
            has_tuple = (
                str(payload.get("provider") or "").lower() == "claude"
                and str(payload.get("kind") or "") in {"ASSIST_TEXT", "ASSIST"}
                and not raw.get("is_sidechain")
                and bool(_tuple_value(raw.get("model")))
                and bool(_tuple_value(raw.get("effort")))
            )
            if has_tuple:
                observed = True
            await self.observe_claude_event(payload)
        return observed

    async def report_write_snapshot(
        self,
        stream_id: str,
        *,
        fresh_source: bool | None = None,
    ) -> dict[str, Any] | None:
        """Return a tuple-derived integrity decision for one report write.

        The decision is intentionally recomputed from the requested/effective
        tuple. ``routing_integrity`` is a periodic observation and is never
        consulted as the authority for this snapshot.
        """
        host, session_name = self.sessions.split(stream_id)
        row = await self.store.fetch_session(host, session_name)
        if not isinstance(row, dict):
            return None
        provider = str(row.get("provider") or "unknown").lower()
        requested_model = _tuple_value(row.get("requested_model"))
        requested_effort = _tuple_value(row.get("requested_effort"))
        effective_model = _tuple_value(row.get("effective_model"))
        effective_effort = _tuple_value(row.get("effective_effort"))

        if not requested_model or not requested_effort:
            integrity = "unverified"
            reason = "requested_model_effort_missing"
        elif not effective_model or not effective_effort:
            integrity = "unverified"
            reason = "effective_model_effort_missing"
        elif (
            _canonical_or_self(provider, requested_model), requested_effort
        ) == (
            _canonical_or_self(provider, effective_model), effective_effort
        ):
            integrity = "verified"
            reason = None
        else:
            integrity = "mismatch"
            reason = (
                f"requested={requested_model}/{requested_effort};"
                f"effective={effective_model}/{effective_effort}"
            )
        return {
            "stream_id": stream_id,
            "provider": provider,
            "generation": str(row.get("created_at") or ""),
            "session_generation": str(row.get("session_generation") or ""),
            "qualified_spec_ids": normalize_spec_ids(row.get("qualified_spec_ids")),
            "requested_model": requested_model or None,
            "requested_effort": requested_effort or None,
            "effective_model": effective_model or None,
            "effective_effort": effective_effort or None,
            "routing_integrity": integrity,
            "reason": reason,
            "fresh_source": fresh_source,
        }

    async def observe_claude_event(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Lift v1's streamed Claude observer onto normalized v2 ingest events."""
        if str(payload.get("provider") or "").lower() != "claude":
            return None
        if str(payload.get("kind") or "") not in {"ASSIST_TEXT", "ASSIST"}:
            return None
        raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
        if raw.get("is_sidechain"):
            return None
        stream_id = str(payload.get("stream_id") or "")
        if not stream_id or ":" not in stream_id:
            return None
        host, session_name = stream_id.split(":", 1)
        row = await self.store.fetch_session(host, session_name)
        if not isinstance(row, dict):
            return None
        if str(row.get("status") or "open") != "open":
            # A closed generation is no longer a streamed evidence source. Drop
            # its duplicate-suppression entry so a name reopen cannot inherit it.
            self._claude_tuples.pop(stream_id, None)
            return None
        generation = str(row.get("created_at") or "")
        context = parse_claude_context(raw)
        context_result = None
        if context is not None:
            context_result = await self.observe_context(
                host,
                session_name,
                provider="claude",
                reading=context,
                observed_at=str(payload.get("timestamp") or "") or None,
                expected_generation=generation,
            )
        model = _tuple_value(raw.get("model"))
        effort = _tuple_value(raw.get("effort"))
        if not model or not effort:
            return context_result
        tuple_value = (model, effort)
        repeated = self._claude_tuples.get(stream_id) == (generation, tuple_value)
        if repeated:
            return None
        self._claude_tuples.pop(stream_id, None)
        self._claude_tuples[stream_id] = (generation, tuple_value)
        while len(self._claude_tuples) > MAX_CLAUDE_TUPLE_CACHE:
            self._claude_tuples.pop(next(iter(self._claude_tuples)))
        return await self.observe_tuple(
            host,
            session_name,
            provider="claude",
            effective=(model, effort),
            observed_at=str(payload.get("timestamp") or "") or None,
            expected_generation=generation,
        )

    async def _notify_episode(self, episode: dict[str, Any], *, reason: str) -> None:
        """Deliver one episode while serialized against its close/reopen."""
        stream_id = str(episode.get("stream_id") or "")
        async with self.store.routing_integrity_lifecycle_lock(stream_id):
            await self._notify_episode_locked(episode, reason=reason)

    async def _notify_episode_locked(self, episode: dict[str, Any], *, reason: str) -> None:
        stream_id = str(episode.get("stream_id") or "")
        expected_generation = str(episode.get("session_generation") or "")
        if expected_generation and ":" in stream_id:
            target_host, target_name = stream_id.split(":", 1)
            target_row = await self.store.fetch_session(target_host, target_name)
            if (
                not isinstance(target_row, dict)
                or str(target_row.get("created_at") or "") != expected_generation
                or str(target_row.get("status") or "") != "open"
            ):
                return
            current_episode = await self.store.routing_integrity_episode(stream_id)
            if (
                not isinstance(current_episode, dict)
                or str(current_episode.get("episode_id") or "")
                != str(episode.get("episode_id") or "")
            ):
                return
        event = {
            "type": "routing_integrity.alert",
            "routing_integrity_event_id": str(episode.get("episode_id") or ""),
            "routing_integrity_state": "mismatch",
            "stream_id": stream_id,
            "provider": str(episode.get("provider") or ""),
            "requested": {
                "model": str(episode.get("requested_model") or ""),
                "effort": str(episode.get("requested_effort") or ""),
            },
            "effective": {
                "model": str(episode.get("effective_model") or ""),
                "effort": str(episode.get("effective_effort") or ""),
            },
            "observed_at": str(episode.get("updated_at") or episode.get("first_observed_at") or ""),
            "reason": reason,
        }
        parent = str(episode.get("parent_stream_id") or "").strip()
        event["recipient_stream_id"] = parent or "operator"
        if self.notify is None:
            return
        try:
            await self.notify.create_internal_notification(
                producer="routing_integrity",
                title="Routing-integrity drift",
                body=json.dumps(event, sort_keys=True),
                severity="warning",
                dedup_key=f"routing_integrity:{event['routing_integrity_event_id']}",
            )
        except Exception:  # noqa: BLE001 - leave the durable episode retryable
            log.exception("routing-integrity alert failed stream=%s", stream_id)
            return
        marked = await self.store.mark_routing_integrity_notified(
            stream_id,
            str(episode.get("episode_id") or ""),
            expected_generation=expected_generation or None,
        )
        if marked and self.broadcast is not None:
            try:
                await self.broadcast(event)
            except Exception:  # noqa: BLE001 - durable card already exists
                log.exception("routing-integrity alert broadcast failed stream=%s", stream_id)
