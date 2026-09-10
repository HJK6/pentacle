#!/usr/bin/env python3
"""Exercise every Desktop-wire spawn cell and surface one red per failed cell."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid

SERVICE_DIR = Path(__file__).resolve().parents[1]
SERVICES_DIR = SERVICE_DIR.parent
for _path in (SERVICE_DIR, SERVICES_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from boot_ready import codex_reset_interstitial_visible  # noqa: E402
from machines import configured_host_names  # noqa: E402
from client_contract_probe import (  # noqa: E402
    CONTROL_PLANE_P95_LIMIT_MS,
    CONVERGENCE_QUIET_LIMIT_MS,
    HEARTBEAT_DEADLINE_MS,
    HISTORY_HEARTBEAT_DEADLINE_MS,
    _PUSH_TYPES,
    audit_control_plane_latency,
    audit_disconnects,
    audit_event_completeness,
    audit_final_state_convergence,
    audit_reconnect_recovery,
    desktop_handshake_fetch_reply,
    LINK_FLOOR_BYTES_PER_SECOND,
    MAX_QUEUED_AGE_MS,
    MAX_QUEUED_BYTES,
    RECONNECT_SPIRAL_WINDOW_SECONDS,
)
from tools.live_window import (  # noqa: E402
    OwnedSession,
    OwnedSessionRegistry,
    authenticated_operator_connection,
)


HOSTS = configured_host_names("PENTACLE_SMOKE_HOSTS")
PROVIDERS = ("claude", "codex")
PROMPT_MODES = ("prompted", "promptless")
ASSISTANT_KINDS = frozenset({"ASSIST", "ASSIST_TEXT"})

DEFAULT_URL = "ws://127.0.0.1:7791"
DEFAULT_TOKEN_PATH = Path.home() / ".config/pentacle-stream/token"
DEFAULT_TIMEOUT = 180.0
# Teardown is the one stage with a known-fast happy path (a clean remote
# close lands in ~5s), so it gets its own bounded budget instead of the
# full stage timeout: a leaked pane must be escalated quickly, and a
# failing cell must not burn two full stage timeouts on the way out.
TEARDOWN_TIMEOUT = 30.0
EXIT_UNTESTED = 2

# A Codex account over its usage limit renders a TUI banner in place of any
# assistant turn ("■ You've hit your usage limit … try again at <t>."), so the
# cell reaches readiness but produces zero ASSIST events and the smoke's event
# predicate times out — indistinguishable, in the event stream, from a genuine
# daemon/wire miss. The banner is a TUI render ONLY (never a transcript/tail
# event; see provider reset diagnostics), so the only
# surface that carries it is the pane. A quota condition is environmental and
# self-resolving at reset; it must not read as a `critical` daemon regression.
_CODEX_USAGE_LIMIT_RE = re.compile(r"hit your usage limit", re.IGNORECASE)
_CODEX_RESET_AT_RE = re.compile(r"try again at\s+(.+?)\s*\.", re.IGNORECASE)


class CodexQuotaExhausted(RuntimeError):
    """A codex cell that failed because its account is over its usage limit.

    Carries the host and the parsed reset time (when the banner named one) so
    `run_matrix` can report it as an `UNTESTED` environmental outcome instead of
    a `critical` daemon red.
    """

    def __init__(self, host: str, reset_at: str | None) -> None:
        self.host = host
        self.reset_at = reset_at
        detail = f"codex usage limit on {host}"
        if reset_at:
            detail += f"; resets {reset_at}"
        super().__init__(f"quota_exhausted: {detail}")


def _codex_quota_reset(pane_text: str) -> tuple[bool, str | None]:
    """Is `pane_text` a Codex usage-limit surface, and if so its reset time?

    Matches both the plain "hit your usage limit … try again at <t>." banner and
    the daemon's own `/usage` reset menu/confirm surface (reusing
    `boot_ready.codex_reset_interstitial_visible`, the owner's detector).
    """
    text = pane_text or ""
    if not (_CODEX_USAGE_LIMIT_RE.search(text) or codex_reset_interstitial_visible(text)):
        return False, None
    match = _CODEX_RESET_AT_RE.search(text)
    return True, (match.group(1).strip() if match else None)


def _capture_cell_pane(host: str, session_name: str) -> str:
    """Best-effort capture of one cell's tmux pane via the daemon's OWN per-host
    transport — local `tmux capture-pane` for this host, the same over SSH for a
    peer (`hosts.Hosts.tmux_for`). Reuses the shipped capture path rather than
    adding a client-facing daemon RPC. Returns "" on any error: a failed capture
    must fall back to the normal red, never crash the run or masquerade as quota.
    """
    try:
        from machines import get_local_machine_name, load_machines
        from hosts import Hosts, HostsConfig

        machines = load_machines(os.environ)
        local = get_local_machine_name(machines) or ""
        peers = {m.name: m for m in machines if not m.is_local and m.name != local}
        hosts = Hosts(local_host=local, peers=peers, config=HostsConfig.from_env())
        return asyncio.run(hosts.tmux_for(host).capture(session_name))
    except Exception:
        return ""


def run_cell(
    host: str,
    provider: str,
    prompt_mode: str,
    *,
    rpc,
    wait_ready,
    wait_event,
    verify_teardown,
    register_owned=None,
    close_owned=None,
    prepare_owned_spawn=None,
    rescue_teardown=None,
    inject_stage: str | None = None,
    capture_pane=None,
    validate_session=None,
) -> dict[str, object]:
    """Run one matrix cell; a created stream is always closed and verified."""
    stream_id = ""
    stage = "catalog"
    cell_error: Exception | None = None
    session_metrics: dict | None = None
    try:
        if inject_stage == stage:
            raise RuntimeError("forced failure")
        catalog = rpc({"type": "spawn_catalog_get"}, "spawn_catalog_get")
        if catalog.get("type") != "spawn_catalog_get.ok":
            raise RuntimeError(str(catalog))
        model, effort = catalog["profiles"]["desktop_manual"][provider]
        marker = f"PENTACLE_FLEET_SMOKE_{uuid.uuid4().hex}"
        # The legacy runtime fallback is allowed only for names derived from
        # this activation's idempotency UUID.  Keep this construction adjacent
        # to the immutable spawn payload so a caller cannot accidentally reuse
        # a friendly/static fleet-smoke name after a reconnect.
        idempotency_key = uuid.uuid4().hex
        name = f"v2-fleet-smoke-{provider}-{idempotency_key[:12]}"
        spawn = {"objective": "Exercise the existing spawn contract",
            "type": "spawn",
            "host": host,
            "session_name": name,
            "provider": provider,
            "visibility": "hidden",
            "schema": "SpawnRequestV2",
            "spawn_profile": "desktop_manual",
            "model": model,
            "effort": effort,
            "catalog_version": catalog["catalog_version"],
            "resolution_source": "explicit_override",
            "request_id": str(uuid.uuid4()),
            "idempotency_key": idempotency_key,
        }
        if prompt_mode == "prompted":
            spawn["initial_prompt"] = f"Reply exactly {marker} and do nothing else."
        if prepare_owned_spawn is not None:
            prepare_owned_spawn(spawn)
        result = rpc(spawn, "spawn")
        stream_id = str(result.get("stream_id") or (result.get("session") or {}).get("stream_id") or "")
        if not stream_id:
            raise RuntimeError(f"spawn returned no stream_id: {result}")
        if register_owned is None:
            raise RuntimeError("spawn: live-window ownership adapter is required")
        register_owned(spawn, result)

        stage = "readiness"
        if inject_stage == stage:
            raise RuntimeError("forced failure")
        wait_ready(stream_id)

        stage = "event"
        if inject_stage == stage:
            raise RuntimeError("forced failure")
        if prompt_mode == "promptless":
            rpc({
                "type": "send",
                "host": host,
                "session_name": stream_id.partition(":")[2],
                "text": f"Reply exactly {marker} and do nothing else.",
                "optimistic_id": str(uuid.uuid4()),
            }, "send")
        wait_event(stream_id, marker)
        if validate_session is not None:
            session_metrics = validate_session(stream_id, marker)
            for gate_name in ("event", "recovery", "convergence", "control_plane", "disconnect", "terminal_idle"):
                gate = session_metrics.get(gate_name) if isinstance(session_metrics, dict) else None
                if isinstance(gate, dict) and not gate.get("passed", True):
                    stage = gate_name
                    raise RuntimeError(f"{gate_name}: {json.dumps(gate, sort_keys=True)}")
    except Exception as exc:
        # A codex cell that reached readiness but produced no assistant turn may
        # be over its usage limit rather than hitting a daemon/wire fault. The
        # banner lives only on the pane, and the pane is still alive here (the
        # teardown below runs after this block), so capture it now and, on a
        # match, remember the non-code quota class instead of a stage red.
        # Best-effort: a capture/classify error must never skip teardown or mask
        # the red. Forced test injections (`inject_stage`) are never classified.
        try:
            if inject_stage is None and provider == "codex" and stage in ("event", "readiness") and stream_id:
                pane = (capture_pane or _capture_cell_pane)(host, stream_id.partition(":")[2])
                is_quota, reset_at = _codex_quota_reset(pane)
                if is_quota:
                    cell_error = CodexQuotaExhausted(host, reset_at)
        except Exception:
            pass
        if cell_error is None:
            cell_error = RuntimeError(f"{stage}: {exc}")

    # Teardown ALWAYS runs (success or failure), but its own failure must never
    # mask a `quota_exhausted` verdict: an over-quota cell is exactly the case
    # whose stuck pane can also make teardown hang, and reporting it as a
    # `critical` teardown red would violate AC1 (a quota cell raises no daemon
    # red). So a quota verdict wins; otherwise a teardown failure supersedes a
    # stage red as before (the closed-row/live-pane leak is the worse fault).
    teardown_error: Exception | None = None
    if stream_id:
        try:
            if close_owned is None:
                raise RuntimeError("teardown: live-window ownership adapter is required")
            close_owned(stream_id)
        except Exception as exc:
            if rescue_teardown is not None and _operator_socket_failure(exc):
                try:
                    rescue_teardown(stream_id)
                except Exception as rescue_exc:
                    teardown_error = RuntimeError(
                        f"teardown: {exc}; rescue failed: {rescue_exc}"
                    )
                else:
                    teardown_error = RuntimeError(
                        f"teardown: {exc}; rescue succeeded"
                    )
            else:
                teardown_error = RuntimeError(f"teardown: {exc}")

    if isinstance(cell_error, CodexQuotaExhausted):
        raise cell_error
    if cell_error is not None:
        raise teardown_error or cell_error
    if teardown_error is not None:
        raise teardown_error
    if inject_stage == "teardown":
        raise RuntimeError("teardown: forced failure")
    result = {"host": host, "provider": provider, "prompt_mode": prompt_mode, "stream_id": stream_id}
    if session_metrics is not None:
        result["metrics"] = session_metrics
    return result


def _stage_from_error(error: Exception) -> str:
    return str(error).partition(":")[0] or "unknown"


_DEAD_OPERATOR_SOCKET_MARKERS = (
    "broken pipe",
    "connection reset",
    "connection closed",
    "connection lost",
    "disconnect:",
    "no close frame",
    "websocket",
    "1011",
    "4000",
)


def _operator_socket_failure(error: BaseException) -> bool:
    """Return whether a teardown failure warrants a fresh socket rescue."""
    if isinstance(error, TimeoutError):
        return False
    if isinstance(error, (ConnectionError, OSError)):
        return True
    detail = str(error).lower()
    return any(marker in detail for marker in _DEAD_OPERATOR_SOCKET_MARKERS)


_UNAVAILABLE_MARKERS = (
    "host unavailable", "host offline", "host_offline", "connection refused",
    "no route to host", "unreachable", "machine unavailable",
)


def classify_cell_outcome(error: Exception) -> dict[str, object]:
    """Classify environmental cells without turning them into a daemon red."""
    if isinstance(error, CodexQuotaExhausted):
        return {"class": "untested", "reason": "quota_exhausted", "reset_at": error.reset_at}
    detail = str(error).lower()
    if any(marker in detail for marker in _UNAVAILABLE_MARKERS):
        return {"class": "untested", "reason": "host_unavailable"}
    return {"class": "failure", "reason": _stage_from_error(error)}


@contextmanager
def _operator_connection(
    url: str, token_path: Path, timeout: float, registry: OwnedSessionRegistry | None = None,
):
    """Yield one cell's four wire closures over a private operator connection.

    Every matrix cell opens its own short-lived connection. This operator
    subscribes to the whole fleet (`include_subagents`), so a single shared
    connection let dozens of live sessions' `chat.event` frames pile up in the
    socket and in `pushes`; each later cell's readiness/event round-trips then
    starved draining that ever-growing backlog and timed out — 11/12 cells
    failed serially while every cell passed in isolation
    (spec_example_2026_01, deploy
    record 2026-09-01). A fresh connection per cell bounds the backlog to one
    cell's lifetime, which is the simplest correct fix (no new knobs).
    """
    if registry is None:
        registry = OwnedSessionRegistry(
            Path(tempfile.gettempdir()) / f"pentacle-live-window-test-{uuid.uuid4().hex}.json",
        )
    with authenticated_operator_connection(url, token_path, timeout) as operator:
        registry.configure_runtime(operator.snapshot)
        ws = operator.socket
        pushes: list[dict] = []
        state_frames: list[list[dict]] = []
        control_latencies_ms: list[float | None] = []
        history_latencies_ms: list[float] = []
        disconnects: list[dict] = []
        last_push_at = [time.monotonic()]
        pressure_clear = [None]
        reconnect_attempts: list[dict] = []

        def record_push(frame: dict) -> None:
            last_push_at[0] = time.monotonic()
            if frame.get("type") == "snapshot" and isinstance(frame.get("sessions"), list):
                state_frames.append(frame["sessions"])
            if frame.get("type") == "session.inventory" and isinstance(frame.get("sessions"), list):
                state_frames.append(frame["sessions"])
            if frame.get("type") in _PUSH_TYPES or frame.get("type") in {"snapshot", "session.inventory"}:
                pushes.append(frame)

        if operator.snapshot is not None:
            record_push(operator.snapshot)

        def receive(receive_timeout: float) -> dict:
            try:
                return json.loads(ws.recv(timeout=receive_timeout))
            except Exception as exc:
                detail = str(exc)
                if (isinstance(exc, (ConnectionError, OSError)) and not isinstance(exc, TimeoutError)) or any(
                    word in detail.lower() for word in (
                        "closed", "close", "websocket", "connection", "1011", "4000",
                    )
                ):
                    disconnects.append({
                        "detail": detail,
                        "at_monotonic": time.monotonic(),
                        **({"code": 1011} if "1011" in detail else {}),
                        **({"code": 4000} if "4000" in detail else {}),
                    })
                raise

        def drain_pushes_after_marker() -> None:
            """Drain already-readable broadcast frames before marking pressure clear."""
            while True:
                try:
                    frame = receive(0)
                except TimeoutError:
                    break
                record_push(frame)
            pressure_clear[0] = {
                "at_monotonic": time.monotonic(),
                "reason": "subscription_push_queue_drained",
            }

        def rpc(
            payload: dict, prefix: str, *, deadline: float | None = None
        ) -> dict:
            request_id = str(uuid.uuid4())
            started = time.monotonic()
            stream_chunks: list[dict] = []
            if deadline is None and prefix in {"spawn_catalog_get", "asset.list", "ping"}:
                deadline = time.monotonic() + CONTROL_PLANE_P95_LIMIT_MS / 1000
            try:
                ws.send(json.dumps({**payload, "request_id": request_id}))
            except Exception as exc:
                disconnects.append({"detail": str(exc), "at_monotonic": time.monotonic()})
                if prefix in {"spawn_catalog_get", "asset.list", "ping"}:
                    control_latencies_ms.append(None)
                raise RuntimeError(f"disconnect: {exc}") from exc
            while True:
                receive_timeout = timeout
                if deadline is not None:
                    receive_timeout = deadline - time.monotonic()
                    if receive_timeout <= 0:
                        if prefix in {"spawn_catalog_get", "asset.list", "ping"}:
                            control_latencies_ms.append(None)
                        raise RuntimeError(f"{prefix} timed out")
                frame = receive(receive_timeout)
                if frame.get("type") in _PUSH_TYPES:
                    record_push(frame)
                    continue
                if frame.get("request_id") != request_id:
                    continue
                if prefix == "request_stream_events" and frame.get("type") == "request_stream_events.chunk":
                    stream_chunks.extend(item for item in frame.get("events", []) if isinstance(item, dict))
                    continue
                if frame.get("type") == f"{prefix}.error":
                    raise RuntimeError(str(frame))
                if prefix == "request_stream_events" and frame.get("type") == "request_stream_events.ok":
                    frame = {**frame, "events": stream_chunks + [
                        item for item in frame.get("events", []) if isinstance(item, dict)
                    ]}
                if prefix == "request_stream_events":
                    history_latencies_ms.append((time.monotonic() - started) * 1000)
                if prefix in {"spawn_catalog_get", "asset.list", "ping"}:
                    control_latencies_ms.append((time.monotonic() - started) * 1000)
                return frame

        def wait_ready(stream_id: str) -> None:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                reply = rpc({"type": "list_sessions"}, "list_sessions")
                row = next((item for item in reply.get("active", []) if item.get("stream_id") == stream_id), None)
                if row and row.get("bootstrap_state") == "ready":
                    return
                time.sleep(0.5)
            raise RuntimeError("timed out waiting for ready inventory row")

        def wait_event(stream_id: str, marker: str) -> None:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                for frame in pushes:
                    event = frame.get("event") or {}
                    if event.get("stream_id") == stream_id and event.get("kind") in ASSISTANT_KINDS and marker in str(event.get("text") or ""):
                        drain_pushes_after_marker()
                        return
                frame = receive(max(0.1, deadline - time.monotonic()))
                record_push(frame)
            raise RuntimeError("timed out waiting for assistant event")

        def validate_session(stream_id: str, _marker: str) -> dict:
            replay = rpc({
                "type": "request_stream_events", "stream_id": stream_id, "limit": 500,
            }, "request_stream_events", deadline=time.monotonic() + HISTORY_HEARTBEAT_DEADLINE_MS / 1000)
            expected = replay.get("events")
            if not isinstance(expected, list):
                raise RuntimeError(f"event: authoritative replay is malformed: {replay}")
            expected = sorted(
                (event for event in expected if event.get("stream_id") == stream_id),
                key=lambda event: int(event.get("daemon_seq", -1)),
            )
            if not expected:
                raise RuntimeError("event: authoritative stream set is empty")
            final_event = expected[-1]
            received_events = [
                frame.get("event") for frame in pushes
                if frame.get("type") == "chat.event"
                and isinstance(frame.get("event"), dict)
            ]
            initial_event_gate = audit_event_completeness(
                expected, received_events, stream_id=stream_id, final_event=final_event,
            )
            reconnect_attempt = {
                "at_monotonic": time.monotonic(), "kind": "new_connection", "outcome": "started",
            }
            reconnect_attempts.append(reconnect_attempt)
            reconnect: dict = {}
            try:
                reconnect = desktop_handshake_fetch_reply(
                    url, stream_id, limit=500, include_subagents=True,
                    events_mode="summary", timeout=min(timeout, HISTORY_HEARTBEAT_DEADLINE_MS / 1000),
                )
                reconnect_attempt.update({
                    "outcome": "passed",
                    "duration_ms": (time.monotonic() - reconnect_attempt["at_monotonic"]) * 1000,
                })
            except Exception as exc:
                reconnect_attempt.update({
                    "outcome": "failed", "error": str(exc),
                    "duration_ms": (time.monotonic() - reconnect_attempt["at_monotonic"]) * 1000,
                })
            replay_events = reconnect.get("events") or reconnect.get("received_events") or []
            recovery_gate = audit_reconnect_recovery(
                expected, received_events, replay_events,
                stream_id=stream_id, final_event=final_event,
            )
            event_gate = {**recovery_gate, "initial": initial_event_gate}

            # Replay/reconnect may have queued more subscription frames. Drain
            # those once, then give the normal coalesced inventory its existing
            # two-second convergence bound. Never reset this deadline on churn.
            drain_pushes_after_marker()
            convergence_start = pressure_clear[0]["at_monotonic"]
            convergence_deadline = convergence_start + min(timeout, CONVERGENCE_QUIET_LIMIT_MS / 1000)
            convergence_gate = {"passed": False}

            def compare_inventory(authoritative_rows: list[dict]) -> dict:
                rendered_rows = state_frames[-1] if state_frames else []
                gate = audit_final_state_convergence(
                    authoritative_rows, rendered_rows,
                    quiet_ms=(time.monotonic() - convergence_start) * 1000,
                )
                return gate

            while time.monotonic() < convergence_deadline:
                inventory = rpc({"type": "list_sessions"}, "list_sessions", deadline=convergence_deadline)
                authoritative_rows = inventory.get("active")
                if not isinstance(authoritative_rows, list):
                    raise RuntimeError(f"convergence: invalid authoritative roster: {inventory}")
                convergence_gate = compare_inventory(authoritative_rows)
                if convergence_gate["passed"]:
                    break
                try:
                    frame = receive(max(0.0, convergence_deadline - time.monotonic()))
                except TimeoutError:
                    break
                record_push(frame)
                # A final frame arriving exactly at the bound is still valid;
                # compare it before attempting another control-plane roundtrip.
                convergence_gate = compare_inventory(authoritative_rows)
                if convergence_gate["passed"]:
                    break
            convergence_gate["wait_ms"] = (time.monotonic() - convergence_start) * 1000
            convergence_gate["pressure_cleared"] = pressure_clear[0] is not None
            convergence_gate["pressure_clear_marker"] = pressure_clear[0]

            assets = rpc({"type": "asset.list", "stream_id": stream_id}, "asset.list", deadline=time.monotonic() + HEARTBEAT_DEADLINE_MS / 1000)
            del assets  # The latency sample is the contract; the payload is not this gate.
            heartbeat = rpc({"type": "ping"}, "ping", deadline=time.monotonic() + HEARTBEAT_DEADLINE_MS / 1000)
            if heartbeat.get("type") != "pong":
                raise RuntimeError(f"control_plane: heartbeat reply was {heartbeat}")
            control_gate = audit_control_plane_latency(
                control_latencies_ms, p95_limit_ms=CONTROL_PLANE_P95_LIMIT_MS,
            )
            history_ms = max(history_latencies_ms, default=0.0)
            control_gate["history_ms"] = history_ms
            control_gate["history_limit_ms"] = HISTORY_HEARTBEAT_DEADLINE_MS
            control_gate["heartbeat_limit_ms"] = HEARTBEAT_DEADLINE_MS
            control_gate["passed"] = control_gate["passed"] and history_ms <= HISTORY_HEARTBEAT_DEADLINE_MS
            disconnect_gate = audit_disconnects(
                disconnects,
                reconnect_spiral_count=max(0, len(reconnect_attempts) - 1),
                reconnect_attempts=reconnect_attempts,
            )
            disconnect_gate["window_seconds"] = RECONNECT_SPIRAL_WINDOW_SECONDS
            # Provider turn completion is independent of client convergence.
            # A marker can arrive while both inventories correctly say working.
            idle_start = time.monotonic()
            idle_deadline = idle_start + timeout
            terminal_idle_gate = {"passed": False, "timeout_seconds": timeout}
            try:
                while time.monotonic() < idle_deadline:
                    reply = rpc({"type": "list_sessions"}, "list_sessions", deadline=idle_deadline)
                    target = next((row for row in reply.get("active", []) if row.get("stream_id") == stream_id), {})
                    if target.get("working") is False:
                        terminal_idle_gate["passed"] = True
                        break
                    time.sleep(min(0.25, max(0, idle_deadline - time.monotonic())))
            except Exception as exc:
                terminal_idle_gate["error"] = str(exc)
            terminal_idle_gate["wait_ms"] = (time.monotonic() - idle_start) * 1000
            bootstrap = {
                key: reconnect.get(key)
                for key in (
                    "bootstrap_total_bytes", "bootstrap_peak_bytes", "bootstrap_elapsed_ms",
                    "bootstrap_rate_bytes_per_second", "full_hello_reference_bytes",
                )
                if key in reconnect
            }
            return {
                "event": event_gate,
                "recovery": recovery_gate,
                "convergence": convergence_gate,
                "terminal_idle": terminal_idle_gate,
                "control_plane": control_gate,
                "disconnect": disconnect_gate,
                "bootstrap": bootstrap,
                "locked_pressure_envelope": {
                    "link_floor_bytes_per_second": LINK_FLOOR_BYTES_PER_SECOND,
                    "max_queued_bytes": MAX_QUEUED_BYTES,
                    "max_queued_age_ms": MAX_QUEUED_AGE_MS,
                },
            }

        def inventory(deadline: float | None = None) -> list[dict[str, object]]:
            reply = rpc({"type": "list_sessions"}, "list_sessions", deadline=deadline)
            active = (
                reply.get("active")
                if isinstance(reply, dict)
                and reply.get("type") == "list_sessions.ok"
                else None
            )
            if not isinstance(active, list) or any(
                not isinstance(item, dict)
                or not isinstance(item.get("stream_id"), str)
                or not item["stream_id"]
                for item in active
            ):
                raise RuntimeError(f"list_sessions returned invalid inventory: {reply!r}")
            return active

        def wait_for_gone(owned: OwnedSession, deadline: float) -> bool:
            """Confirm the row and pane are gone, escalating rather than leaking.

            Teardown is asynchronous: `close` returns once the daemon accepts
            it, while the remote pane kill and the row close land over the
            peer's own tmux. Checking the inventory exactly once raced that
            propagation into a false red AND walked away from a pane that was
            still running. A pane that outlives its cell is not cosmetic: once
            its row closes, every `event.push` batch from that host is rejected
            and the whole peer stops ingesting events
            (spec_example_2026_01), which is
            what made later remote cells fail at the `event` stage. So poll
            like every other stage, then escalate with one more close before
            reporting the leak.
            """
            while time.monotonic() < deadline:
                try:
                    if not any(
                        item.get("stream_id") == owned.stream_id for item in inventory(deadline)
                    ):
                        return True
                except RuntimeError as error:
                    if str(error) != "list_sessions timed out":
                        raise
                    return False
                time.sleep(0.5)
            return False

        def register_owned(spawn: dict[str, object], result: dict[str, object]) -> None:
            deadline = time.monotonic() + timeout
            stream_id = str(result.get("stream_id") or "")
            while time.monotonic() < deadline:
                row = next(
                    (item for item in inventory(deadline) if item.get("stream_id") == stream_id), None,
                )
                if row is not None:
                    registry.register_spawn(spawn, result, row)
                    return
                time.sleep(0.1)
            raise RuntimeError(f"spawn: missing authoritative ownership row for {stream_id}")

        def prepare_owned_spawn(spawn: dict[str, object]) -> None:
            registry.prepare_spawn(spawn)

        # Keep the existing five-closure public seam intact for focused caller
        # tests while binding pre-spawn admission to the same registry object.
        register_owned.prepare_owned_spawn = prepare_owned_spawn  # type: ignore[attr-defined]

        def close_owned(stream_id: str) -> None:
            registry.close(
                stream_id,
                inventory=inventory,
                send_close=lambda payload: rpc(payload, "close"),
                wait_gone=wait_for_gone,
                timeout=min(timeout, TEARDOWN_TIMEOUT),
            )

        close_owned.validate = validate_session
        yield rpc, wait_ready, wait_event, register_owned, close_owned


def _rescue_teardown(
    url: str, token_path: Path, timeout: float, stream_id: str, registry: OwnedSessionRegistry,
) -> None:
    """Close and verify a cell through a fresh operator connection.

    The primary cell socket may die after spawn but before its close/verify
    exchange. A fresh connection is deliberately limited to the teardown
    operation; it repairs the harness leak without hiding the original cell
    failure from the matrix result.
    """
    with _operator_connection(url, token_path, timeout, registry) as (
        _rpc, _wait_ready, _wait_event, _register_owned, close_owned,
    ):
        close_owned(stream_id)


def run_matrix(
    url: str,
    token_path: Path,
    timeout: float,
    cells: tuple[tuple[str, str, str], ...] | None = None,
    *,
    evidence: list[dict[str, object]] | None = None,
    allow_legacy_close: bool = False,
) -> list[dict[str, object]]:
    if cells is None:
        cells = tuple(
            (host, provider, prompt_mode)
            for host in HOSTS for provider in PROVIDERS for prompt_mode in PROMPT_MODES
        )
    if not cells:
        raise ValueError("no smoke host cells configured")
    failures: list[dict[str, str]] = []
    for host, provider, prompt_mode in cells:
        state_path = Path(tempfile.gettempdir()) / f"pentacle-live-window-{uuid.uuid4().hex}.json"
        registry = OwnedSessionRegistry(state_path, allow_legacy_close=allow_legacy_close)

        def rescue_cell_teardown(stream_id: str) -> None:
            _rescue_teardown(url, token_path, timeout, stream_id, registry)

        try:
            with _operator_connection(url, token_path, timeout, registry) as (
                rpc, wait_ready, wait_event, register_owned, close_owned,
            ):
                try:
                    result = run_cell(
                        host, provider, prompt_mode, rpc=rpc, wait_ready=wait_ready,
                        wait_event=wait_event, verify_teardown=close_owned,
                        register_owned=register_owned, close_owned=close_owned,
                        prepare_owned_spawn=getattr(register_owned, "prepare_owned_spawn", None),
                        rescue_teardown=rescue_cell_teardown,
                        validate_session=getattr(close_owned, "validate", None),
                    )
                    if evidence is not None and isinstance(result, dict):
                        evidence.append({"outcome": "passed", **result})
                    continue
                except CodexQuotaExhausted as exc:
                    # Environmental, not a daemon regression: record the cell as
                    # the non-code `UNTESTED` class (kept out of daemon
                    # regression failures by `main`) and surface ONE operator
                    # `warning` line per host — never a `critical` daemon red.
                    row = {
                        "host": host, "provider": provider, "prompt_mode": prompt_mode,
                        "stage": "event", "class": "untested", "reason": "quota_exhausted",
                        "reset_at": exc.reset_at,
                    }
                    failures.append(row)
                    if evidence is not None:
                        evidence.append({"outcome": "UNTESTED", **row})
                    try:
                        reset = f"; try again at {exc.reset_at}" if exc.reset_at else ""
                        rpc({
                            "type": "notification.create",
                            "producer": "spawn-fleet-smoke",
                            "severity": "warning",
                            "title": f"Codex quota exhausted: {host}",
                            "body": f"{host} Codex account is over its usage limit{reset}. "
                                    "Restore quota (/usage reset or top-up); not a daemon fault.",
                            "dedup_key": f"spawn-fleet-smoke-quota:{host}",
                        }, "notification.create")
                    except Exception:
                        pass
                    continue
                except Exception as exc:
                    stage = _stage_from_error(exc)
                    classification = classify_cell_outcome(exc)
                    row = {
                        "host": host, "provider": provider, "prompt_mode": prompt_mode,
                        "stage": stage, "class": classification["class"],
                        **({"reason": classification["reason"]} if classification.get("reason") else {}),
                    }
                    failures.append(row)
                    if evidence is not None and classification["class"] == "untested":
                        evidence.append({"outcome": "UNTESTED", **row})
                    if classification["class"] == "untested":
                        try:
                            rpc({
                                "type": "notification.create",
                                "producer": "spawn-fleet-smoke",
                                "severity": "warning",
                                "title": f"Spawn smoke untested: {host}/{provider}",
                                "body": f"prompt_mode={prompt_mode}; {exc}",
                                "dedup_key": f"spawn-fleet-smoke-untested:{host}:{provider}:{prompt_mode}",
                            }, "notification.create")
                        except Exception:
                            pass
                        continue
                    try:
                        rpc({
                            "type": "notification.create",
                            "producer": "spawn-fleet-smoke",
                            "severity": "critical",
                            "title": f"Spawn smoke failed: {host}/{provider}/{stage}",
                            "body": f"prompt_mode={prompt_mode}; {exc}",
                            "dedup_key": f"spawn-fleet-smoke:{host}:{provider}:{prompt_mode}:{stage}",
                        }, "notification.create")
                    except Exception:
                        # Connection unusable after the cell failure; the red is
                        # still recorded and returned to the caller.
                        pass
        except Exception as exc:
            # Connection or handshake failure: the cell still counts as a red,
            # but no live notification could be emitted on this connection.
            classification = classify_cell_outcome(exc)
            row = {
                "host": host, "provider": provider, "prompt_mode": prompt_mode,
                "stage": _stage_from_error(exc), "class": classification["class"],
                **({"reason": classification["reason"]} if classification.get("reason") else {}),
            }
            failures.append(row)
            if evidence is not None and classification["class"] == "untested":
                evidence.append({"outcome": "UNTESTED", **row})
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Exercise Desktop-wire spawn cells and raise one operator red per failed cell. "
            "With no arguments, runs the full host x provider x prompt-mode matrix; "
            "with URL and HOST, runs only the single post-deploy canary cell."
        ),
    )
    parser.add_argument(
        "url", nargs="?", default=None,
        help=f"chat-stream websocket URL; requires HOST (default full-matrix URL: {DEFAULT_URL})",
    )
    parser.add_argument(
        "--allow-legacy-close",
        action="store_true",
        help=(
            "permit the documented UUID-name fallback only when the authenticated "
            "runtime does not advertise generation-fenced close"
        ),
    )
    parser.add_argument(
        "host", nargs="?", default=None,
        help="with URL, run only the post-deploy canary cell (<host>, codex, "
             "promptless); the full matrix runs only when NO arguments are given",
    )
    args = parser.parse_args(argv)
    if args.host is not None:
        # Both positionals present: single post-deploy canary cell.
        url, cells = args.url, ((args.host, "codex", "promptless"),)
    elif args.url is not None:
        # A lone URL is ambiguous and must never silently launch 12 spawns.
        parser.error("HOST is required alongside URL for the single canary cell; "
                     "pass no arguments to run the full matrix")
    else:
        url, cells = DEFAULT_URL, None
    evidence: list[dict[str, object]] = []
    results = run_matrix(
        url, DEFAULT_TOKEN_PATH, DEFAULT_TIMEOUT, cells, evidence=evidence,
        allow_legacy_close=args.allow_legacy_close,
    )
    untested = [row for row in results if row.get("class") == "untested"]
    failures = [row for row in results if row.get("class") != "untested"]
    status = "FAIL" if failures else ("UNTESTED" if untested else "PASS")
    print(json.dumps(
        {
            "ok": not failures and not untested,
            "status": status,
            "failures": failures,
            "untested": untested,
            "cells": evidence,
        },
        sort_keys=True,
    ))
    return 1 if failures else (EXIT_UNTESTED if untested else 0)


if __name__ == "__main__":
    raise SystemExit(main())
