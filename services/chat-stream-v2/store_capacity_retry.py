"""Transactional capacity-episode policy; called only on the Store worker."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

PREFIX = "codex.capacity."
GLOBAL_KEY = PREFIX + "hold"
MAX_ATTEMPTS = 3
WAIT_S = 60


def load(conn, sid):
    row = conn.execute("SELECT v FROM kv WHERE k=?", (PREFIX + sid,)).fetchone()
    return json.loads(row[0]) if row else None


def save(conn, sid, state):
    conn.execute("INSERT OR REPLACE INTO kv(k,v) VALUES (?,?)",
                 (PREFIX + sid, json.dumps(state, separators=(",", ":"))))


def globally_held(conn):
    row = conn.execute("SELECT v FROM kv WHERE k=?", (GLOBAL_KEY,)).fetchone()
    return bool(row and json.loads(row[0]).get("held"))


def current(conn, sid):
    host, _, name = sid.partition(":")
    row = conn.execute("SELECT s.*,g.generation FROM sessions s JOIN v2_session_generations g "
                       "ON s.host=g.host AND s.session_name=g.session_name "
                       "WHERE s.host=? AND s.session_name=?", (host, name)).fetchone()
    return dict(row) if row else None


def new_state(row):
    return {"generation": row["generation"], "pane_pid": row.get("pane_pid"),
            "state": "idle", "held": False, "cancelled": False, "attempts_used": 0,
            "next_retry_at": None, "frontier": "", "consumed_turns": [],
            "revision": 0, "episode_id": None, "attempt_id": None,
            "proof_event_id": None, "echo_identity": None}


def cancel(conn, sid, *, hold=False, reason="input", generation=None):
    row = current(conn, sid)
    if not row or (generation and row["generation"] != generation):
        return False
    state = load(conn, sid)
    if not state or state["generation"] != row["generation"]:
        if not hold:
            return False
        state = new_state(row)
    state.update(state="held" if hold or state.get("held") else "idle",
                 held=bool(hold or state.get("held")), next_retry_at=None,
                 cancelled=bool(state.get("episode_id") or state.get("cancelled")),
                 reason=reason, revision=state["revision"] + 1)
    save(conn, sid, state)
    return True


def _stamp(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _consume(state, turn):
    state["consumed_turns"] = [*state.get("consumed_turns", [])[-63:], turn]


def observe(conn, sid, event, event_id, lifecycle, now):
    """Reduce only new, source/lifecycle-admitted records in insertion transaction."""
    if lifecycle is None or event.get("provider") != "codex":
        return
    row = current(conn, sid)
    if not row or row["status"] != "open" or row["generation"] != lifecycle["generation"]:
        return
    raw = event.get("raw") or {}
    if raw.get("transport") != "codex-rollout" or raw.get("source") != "structured":
        return
    state = load(conn, sid)
    if not state or state["generation"] != row["generation"]:
        state = new_state(row)
    stamp = _stamp(event.get("timestamp"))
    previous = _stamp(state.get("frontier"))
    # Native source chronology plus final frontier proof, never arrival order alone.
    if stamp is None or (previous is not None and stamp < previous):
        return
    state["frontier"] = event["timestamp"]
    kind = event.get("kind")
    attempt = state.get("attempt_id")
    if kind in {"USER", "TELL"}:
        receipt = conn.execute("SELECT reply FROM v2_tell_deliveries WHERE tell_id=?",
                               (attempt,)).fetchone() if attempt else None
        delivery = json.loads(receipt[0]).get("delivery", {}) if receipt else {}
        # An own echo is recognized through a committed pre-input receipt and watermark.
        own_echo = (not state.get("echo_identity") and not state.get("proof_event_id")
                    and delivery.get("notification_answer_generation") == row["generation"]
                    and event_id > int(delivery.get("proof_watermark") or 0)
                    and event.get("text", "").strip() == delivery.get("text", "").strip())
        if own_echo:
            state["proof_event_id"] = event_id
            state["echo_identity"] = {"event_id": event_id, "native_session_id": raw.get("source_session_identity"),
                                      "uuid": raw.get("uuid"), "timestamp": event["timestamp"]}
            if state["state"] in {"reserved", "uncertain"}:
                state["state"] = "submitted"
        else:
            state.update(state="held" if state["held"] else "idle", next_retry_at=None,
                         cancelled=bool(state.get("episode_id") or state.get("cancelled")),
                         revision=state["revision"] + 1, reason="native_input")
    elif kind in {"ASSIST_TEXT", "TOOL_USE", "TOOL_RESULT"}:
        # Verified provider work ends the unresolved episode, but cannot lift a hold.
        state.update(state="held" if state["held"] else "idle", next_retry_at=None,
                     attempts_used=0, episode_id=None, attempt_id=None, proof_event_id=None,
                     cancelled=False, echo_identity=None,
                     revision=state["revision"] + 1, reason="provider_progress")
    elif kind == "ERROR" and raw.get("subtype") == "provider-turn-error":
        turn = raw.get("turn_id")
        if not isinstance(turn, str) or not turn or turn in state["consumed_turns"]:
            return
        _consume(state, turn)
        if raw.get("provider_error_code") != "server_overloaded":
            state.update(state="held" if state["held"] else "idle", next_retry_at=None, reason="other_error")
        elif state["held"] or globally_held(conn):
            state.update(state="held", next_retry_at=None, reason="inhibited")
        elif state.get("cancelled"):
            state.update(state="idle", next_retry_at=None, reason="cancelled_until_progress")
        elif state["attempts_used"] and (
            not state.get("proof_event_id") or event_id <= state["proof_event_id"]
        ):
            state.update(state="uncertain", next_retry_at=None, reason="prior_submission_unproved")
        elif state["attempts_used"] >= MAX_ATTEMPTS:
            state.update(state="exhausted", next_retry_at=None, reason="capacity_budget_exhausted")
        else:
            episode = state.get("episode_id") or hashlib.sha256(
                (sid + row["generation"] + turn).encode()).hexdigest()[:32]
            binding = row.get("observer_binding")
            binding = json.loads(binding) if isinstance(binding, str) else binding
            state.update(state="waiting", next_retry_at=now + WAIT_S, episode_id=episode,
                         error_turn_id=turn, error_event_id=event_id,
                         native_session_id=raw.get("source_session_identity"),
                         binding=binding, pane_pid=row.get("pane_pid"),
                         proof_event_id=None, revision=state["revision"] + 1,
                         reason="capacity")
    else:
        return
    save(conn, sid, state)


def projection(conn, row):
    sid = f"{row['host']}:{row['session_name']}"
    state = load(conn, sid)
    if not state or state["generation"] != row.get("session_generation"):
        return None
    if row.get("status") != "open" or state["state"] == "idle":
        return None
    return {k: state.get(k) for k in ("state", "next_retry_at", "attempts_used", "reason")}


async def refresh_projection(store, sessions, sid):
    host, _, name = sid.partition(":")
    row = await store.fetch_session(host, name)
    if row and hasattr(sessions, "apply_durable"):
        sessions.apply_durable(sid, expected_generation=row["session_generation"],
                               capacity_retry=row.get("capacity_retry"))
