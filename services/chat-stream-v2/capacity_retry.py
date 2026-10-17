"""Finite continuation of proven Codex capacity stops on the existing reconcile pass."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import logging
import time

from boot_ready import codex_composer_empty
from codex_capacity_probe import program
from sessions import VerbError
from outbound_notices import NoticeDecision
import store_capacity_retry as policy
import store_lifecycle_authority as authority

log = logging.getLogger(__name__)


class CapacityRetry:
    def __init__(self, store, sessions, comms, hosts, *, outbound=None, clock=time.time, probe=None):
        self.store, self.sessions, self.comms, self.hosts = store, sessions, comms, hosts
        self.outbound, self.clock, self.probe = outbound, clock, probe or self._native_probe
        self.token = object()
        self._tick_lock = asyncio.Lock()
        comms.capacity_retry = self
        if outbound is not None:
            outbound.register_kind("codex_capacity_exhausted", guard=self.notice_guard,
                                   lock_factory=self.notice_locks)

    def notice_locks(self, notice):
        return self.locks(notice["source_stream_id"])

    async def notice_guard(self, notice):
        def valid(conn):
            sid = notice["source_stream_id"]
            state, row = policy.load(conn, sid), policy.current(conn, sid)
            return (not policy.globally_held(conn) and row and row["status"] == "open"
                    and state and state["generation"] == row["generation"]
                    and state["state"] == "exhausted" and not state["held"]
                    and state["episode_id"] == notice.get("episode_id"))
        if not await self.store.submit(valid):
            return NoticeDecision.terminal("capacity_episode_retired", "inspect current seat status")
        return None

    async def _transaction(self, fn):
        def run(conn):
            conn.execute("BEGIN")
            try:
                result = fn(conn)
                conn.commit()
                return result
            except BaseException:
                conn.rollback()
                raise
        return await self.store.submit(run)

    @asynccontextmanager
    async def locks(self, sid):
        host, name = self.sessions.split(sid)
        async with self.sessions._lifecycle_lock(host, name), self.comms._pane_input_lock(sid):
            yield

    async def refresh(self, sid):
        host, name = self.sessions.split(sid)
        row = await self.store.fetch_session(host, name)
        if row:
            self.sessions.apply_durable(sid, expected_generation=row["session_generation"],
                                        capacity_retry=row.get("capacity_retry"))
        emitter = getattr(self.sessions, "_inventory_emitter", None)
        if emitter is not None:
            await emitter.emit_if_changed()

    async def cancel_input(self, sid):
        """Called under Comms' input lock before any ordinary paste/Escape."""
        await self._transaction(lambda conn: policy.cancel(conn, sid))
        await self.refresh(sid)

    async def interrupt(self, sid, generation):
        # Called under lifecycle then input lock; a hold survives uncertain Escape.
        await self._transaction(lambda conn: policy.cancel(conn, sid, hold=True,
                                                           reason="interrupt", generation=generation))
        await self.refresh(sid)

    async def control(self, msg):
        auth = msg.get("_auth_context") or {}
        action = msg.get("action")
        sid = str(msg.get("target_stream_id") or "")
        all_seats = msg.get("all") is True
        generation = str(msg.get("expected_session_generation") or "")
        if action not in {"hold", "resume", "status"} or (all_seats == bool(sid)):
            raise VerbError("bad_request", "choose action and exactly one target or all")
        if not all_seats and (":" not in sid or not generation):
            raise VerbError("generation_required", "selected generation required")
        if not msg.get("request_id"):
            raise VerbError("bad_request", "request_id required")
        request_id = str(msg["request_id"])
        if len(request_id) > 200:
            raise VerbError("bad_request", "request_id too long")
        receipt_key = policy.PREFIX + "control." + hashlib.sha256(request_id.encode()).hexdigest()
        identity = json.dumps([action, sid, all_seats, generation], separators=(",", ":"))

        def mutate(conn):
            allowed = auth.get("operator_authenticated") is True or (
                auth.get("token_verified") is True and authority.holder_is(
                    conn, str(auth.get("stream_id") or ""), auth.get("session_generation")))
            if not allowed:
                raise VerbError("authority_holder_required", "operator or current lifecycle manager required")
            prior = conn.execute("SELECT v FROM kv WHERE k=?", (receipt_key,)).fetchone()
            if prior:
                receipt = json.loads(prior[0])
                if receipt["identity"] != identity:
                    raise VerbError("request_id_conflict", "capacity control request_id already used")
                return {**receipt["result"], "duplicate": True}
            def finish(result):
                conn.execute("INSERT INTO kv(k,v) VALUES (?,?)", (receipt_key,
                    json.dumps({"identity": identity, "result": result}, separators=(",", ":"))))
                return result
            if all_seats:
                if action != "status":
                    conn.execute("INSERT OR REPLACE INTO kv(k,v) VALUES (?,?)",
                                 (policy.GLOBAL_KEY, json.dumps({"held": action == "hold"})))
                    # Every current error is consumed on both edges; resume cannot
                    # revive the error that existed before the control action.
                    for row in conn.execute("SELECT host,session_name FROM sessions WHERE status='open'"):
                        target = f"{row['host']}:{row['session_name']}"
                        policy.cancel(conn, target, reason="global_" + action)
                        if action == "resume":
                            state = policy.load(conn, target)
                            if state:
                                state["cancelled"] = False
                                policy.save(conn, target, state)
                return finish({"held": policy.globally_held(conn)})
            row = policy.current(conn, sid)
            if not row or row["status"] != "open" or row["generation"] != generation:
                raise VerbError("stale_session_generation", "selected seat generation changed")
            state = policy.load(conn, sid)
            if not state or state["generation"] != generation:
                state = policy.new_state(row)
            if action != "status":
                state.update(held=action == "hold", state="held" if action == "hold" else "idle",
                             cancelled=action == "hold" and bool(state.get("episode_id")),
                             next_retry_at=None, revision=state["revision"] + 1, reason="explicit_" + action)
                policy.save(conn, sid, state)
            return finish({"held": state["held"], "capacity_retry": policy.projection(
                conn, {**row, "session_generation": generation})})

        if all_seats:
            result = await self._transaction(mutate)
            # Inhibit before draining. No newly admitted attempt can pass its
            # final reservation; an already-started input finishes before ACK.
            for row in sorted(self.sessions.list_open(), key=lambda r: r["stream_id"]):
                async with self.locks(row["stream_id"]):
                    pass
                await self.refresh(row["stream_id"])
        else:
            async with self.locks(sid):
                result = await self._transaction(mutate)
            await self.refresh(sid)
        return {"type": "capacity_retry.control.ok", "request_id": msg["request_id"], **result}

    async def _native_probe(self, row, state):
        rc, text = await self.hosts.run_command(row["host"], "python3", "-c", program(),
                                               json.dumps(state), timeout=25)
        if rc != 0 or len(text) > 4096:
            return False
        try:
            result = json.loads(text)
            if result.get("ok") is not True:
                log.info("capacity native proof deferred stream=%s reason=%s", row["stream_id"],
                         result.get("reason") if result.get("reason") in {
                             "missing_binding", "provider_identity_changed", "ambiguous_process_tree",
                             "ambiguous_rollout", "unbounded_header", "native_session_changed",
                             "rollout_identity_changed", "rollout_not_stable", "newer_native_record",
                             "native_proof_unavailable"} else "unavailable")
            return result.get("ok") is True
        except (ValueError, AttributeError):
            return False

    async def before_input(self, msg, route, watermark):
        """Last guard/reservation, called INSIDE lifecycle then pane input locks."""
        sid = str(route["final_target"])
        expected = msg["_capacity_attempt"]
        host, name = self.sessions.split(sid)
        row = await self.store.fetch_session(host, name) or {}
        live = self.sessions.get(sid) or {}
        prior_binding = expected.get("binding") or {}
        current_binding = row.get("observer_binding") or {}
        # A satellite's first usage span anchors the transcript after its
        # event batch. Permit only that monotonic first anchor, for the SAME
        # original process/native session; an existing anchor is immutable.
        if (not prior_binding.get("transcript") and current_binding.get("transcript")
                and {k: v for k, v in current_binding.items() if k != "transcript"} == prior_binding
                and current_binding["transcript"].get("native_session_id") == expected.get("native_session_id")):
            expected = {**expected, "binding": current_binding}
        generation = expected["generation"]
        observed = (
            ((live.get("local_mirror") is True or live.get("mirror_local") is True)
             and (live.get("mirror_generation") or live.get("local_mirror_generation")) == generation
             and live.get("capture_liveness") in (None, "idle"))
            or (live.get("capture_generation") == generation and live.get("capture_liveness") == "idle")
        )
        if (sid != str(route["original_target"]) or row.get("status") != "open"
                or row.get("session_generation") != expected["generation"]
                or row.get("provider") != "codex" or row.get("pane_pid") != expected.get("pane_pid")
                or row.get("observer_binding") != expected.get("binding")
                or live.get("session_generation") != expected["generation"]
                or live.get("pane_status") != "pane_alive" or live.get("working") is not False
                or live.get("online") is not True or live.get("host_status") == "offline"
                or live.get("routing_integrity") == "mismatch"
                or live.get("bootstrap_state") not in (None, "ready")
                or not observed):
            raise VerbError("capacity_retry_deferred", "current idle seat not proven", phase="not_started")
        tmux = self.hosts.tmux_for(host)
        if not await tmux.input_mode_clear(name):
            raise VerbError("capacity_retry_deferred", "pane input mode blocked", phase="not_started")
        # Preserve SGR: dim placeholder text is not an editable user draft.
        rc, pane = await tmux.run("capture-pane", "-p", "-e", "-J", "-t", f"={name}:")
        if rc != 0 or not codex_composer_empty(pane):
            raise VerbError("capacity_retry_deferred", "composer not proven empty", phase="not_started")
        if not await self.probe(row, expected):
            raise VerbError("capacity_retry_deferred", "native terminal frontier not proven", phase="not_started")
        if watermark.state != "reachable":
            raise VerbError("capacity_retry_deferred", "submission watermark unavailable", phase="not_started")

        def reserve(conn):
            state = policy.load(conn, sid)
            current = policy.current(conn, sid)
            if (not state or not current or current["status"] != "open"
                    or current["generation"] != expected["generation"]
                    or str(current.get("pane_pid")) != str(expected.get("pane_pid"))
                    or json.loads(current.get("observer_binding") or "null") != expected.get("binding")
                    or state["revision"] != expected["revision"] or state["state"] != "waiting"
                    or state["held"] or policy.globally_held(conn)
                    or self.clock() < state["next_retry_at"] or state["attempts_used"] >= policy.MAX_ATTEMPTS):
                raise VerbError("capacity_retry_deferred", "episode cancelled or changed", phase="not_started")
            state.update(state="reserved", attempts_used=state["attempts_used"] + 1,
                         attempt_id=msg["tell_id"], echo_identity=None, next_retry_at=None,
                         revision=state["revision"] + 1)
            policy.save(conn, sid, state)
        await self._transaction(reserve)

    async def tick(self):
        async with self._tick_lock:
            for row in self.sessions.list_open():
                if row.get("provider") != "codex":
                    continue
                sid = row["stream_id"]
                state = await self.store.submit(lambda conn: policy.load(conn, sid))
                if not state or state["generation"] != row.get("session_generation"):
                    continue
                if state["state"] == "waiting" and self.clock() >= state["next_retry_at"]:
                    attempt = f"codex-capacity:{state['episode_id']}:{state['attempts_used'] + 1}"
                    host, name = self.sessions.split(sid)
                    try:
                        async with self.sessions._lifecycle_lock(host, name):
                            # Plain nonurgent continue; existing notification-answer
                            # ownership records a possible input before the paste.
                            await self.comms.deliver_capacity_continue(sid, state, attempt)
                    except VerbError as exc:
                        log.info("capacity retry deferred stream=%s code=%s reason=%s", sid, exc.code, str(exc))
                        if exc.code != "capacity_retry_deferred":
                            await self._uncertain(sid, attempt)
                    except Exception:
                        await self._uncertain(sid, attempt)
                elif state["state"] == "reserved":
                    # Restart recovery is evidence-only, including a crash before
                    # any receipt was stored. Never call the send path again.
                    await self.comms.reconcile_notification_answer(
                        state["attempt_id"], sid, state["generation"])
                    await self._uncertain(sid, state["attempt_id"])
                elif state["state"] == "exhausted" and self.outbound is not None:
                    parent = row.get("parent_stream_id")
                    if parent:
                        notice = f"codex-capacity-exhausted:{state['episode_id']}"
                        await self.outbound.enqueue(kind="codex_capacity_exhausted", dedupe_key=notice,
                            recipient_stream_id=parent, source_stream_id=sid, tell_id=notice,
                            episode_id=state["episode_id"], body=f"{sid}: capacity continuation exhausted (3/3); manual action needed.")
                await self.refresh(sid)

    async def _uncertain(self, sid, attempt):
        def change(conn):
            state = policy.load(conn, sid)
            if state and state.get("attempt_id") == attempt and state["state"] == "reserved":
                state.update(state="uncertain", next_retry_at=None, reason="submission_evidence_pending")
                policy.save(conn, sid, state)
        await self._transaction(change)
