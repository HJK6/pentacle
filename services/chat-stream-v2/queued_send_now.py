"""Receipt-bound operator Send now; native shortcut or one interrupted-turn resume.

Control state uses the existing durable kv store. Original send receipts are
immutable, and continuation input uses Comms' permanent request-ID dedupe.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from datetime import datetime

from boot_ready import claude_composer_empty, prompt_in_native_queue
from sessions import VerbError

log = logging.getLogger("chat_streamd_v2.queued_send_now")

PREFIX = "queued-send-now.operation."
CONTINUE_RETRY_DELAY_S = 5.0
PINS = {
    "claude": ("2.1.312 (Claude Code)", "6b5ec691195e551b02c41df9f7d723f4ea61eaa03c2df90acefe3736f1aeb620"),
    "codex": ("codex-cli 0.160.0", "112fae7a5a1223e673c8a1791d32338f37df8b527ff1159bb8adac6c4dbf1b4b"),
}
TERMINAL = {"completed", "cancelled", "indeterminate"}


def route_for(row, local_host, peers=None):
    binding = row.get("account_binding") or {}
    provider = row.get("provider")
    remote = row.get("host") != local_host
    if remote:
        machine = (peers or {}).get(row.get("host"))
        if machine is None or machine.name != row.get("host") or machine.is_local:
            return None
        pins = {name: (version, digest)
                for name, version, digest in machine.queued_send_now_qualifications}
        pin = pins.get(provider)
    else:
        pin = PINS.get(provider)
    if (not isinstance(binding, dict) or pin is None
            or (binding.get("version"), binding.get("executable_sha256")) != pin
            or (remote and (binding.get("host") != row.get("host")
                            or binding.get("provider") != provider))):
        return None
    return {"claude": "claude-native", "codex": "codex-continue"}.get(provider)


def native_turn_signal(event):
    raw = event.get("raw") or {}
    terminal_error = (event.get("kind") == "ERROR" and raw.get("subtype") == "provider-turn-error"
        and raw.get("codex_payload_type") == "task_complete")
    state = "task_complete" if terminal_error else raw.get("native_turn_state")
    control = event.get("kind") == "SYSTEM" and raw.get("subtype") == "provider-turn-control"
    if (not (control or terminal_error) or event.get("provider") != "codex"
            or raw.get("source") != "structured" or raw.get("transport") != "codex-rollout"
            or raw.get("codex_record_type") != "event_msg"
            or raw.get("codex_payload_type") != state
            or state not in {"task_started", "task_complete", "turn_aborted"}
            or not raw.get("turn_id") or not raw.get("source_session_identity")):
        return None
    return {"state": state, "turn_id": raw["turn_id"],
            "source_session_id": raw["source_session_identity"],
            "reason": raw.get("interrupt_reason"), "event_id": event["daemon_seq"],
            "source_ordinal": raw.get("ordinal")}


def claude_queue_composer_empty(styled_pane):
    """Distinguish the pinned native dim placeholder from identical typed text.

    Only the observed queue placeholder is admitted, and only when every text
    character is SGR-dim. Unknown placeholder/chrome remains fail-closed.
    """
    sgr = re.compile(r"\x1b\[([0-9;]*)m")
    lines, dim_lines = [], []
    dim = False
    for line in styled_pane.splitlines():
        text, mask, start = "", [], 0
        for match in sgr.finditer(line):
            chunk = line[start:match.start()]
            text += chunk; mask.extend([dim] * len(chunk))
            codes = [int(c) if c else 0 for c in match.group(1).split(";")]
            # Skip extended foreground/background operands (a color index of
            # 2 is a palette value, never the dim attribute).
            i = 0
            while i < len(codes):
                code = codes[i]
                if code in {38, 48, 58} and i + 1 < len(codes):
                    i += 3 if codes[i + 1] == 5 else 5
                    continue
                if code in {0, 22}: dim = False
                elif code == 2: dim = True
                i += 1
            start = match.end()
        chunk = line[start:]; text += chunk; mask.extend([dim] * len(chunk))
        if "\x1b" in text: return False
        lines.append(text); dim_lines.append(mask)
    caret = next((i for i in range(len(lines)-1, -1, -1) if lines[i].strip().startswith("❯")), None)
    if caret is None: return False
    line = lines[caret]; content = line.strip().removeprefix("❯").strip()
    if content:
        if content != "Press up to edit queued messages": return False
        offset = line.index(content)
        if not all(dim_lines[caret][j] for j in range(offset, len(line)) if not line[j].isspace()): return False
        lines[caret] = "❯"
    # Native paste-expansion hint replaces the permission footer briefly. It
    # is chrome only below the composer box's lower divider.
    below_divider = False
    for i in range(caret + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped and set(stripped) == {"─"}: below_divider = True
        elif below_divider and (stripped in {"paste again to expand", "Ctrl+Y to paste deleted text"}
                or re.fullmatch(r"paste again to expand\s+Ctrl\+Y to paste deleted text", stripped)):
            lines[i] = ""
    return claude_composer_empty("\n".join(lines))


def possible_native_intake(receipt, candidates):
    """A possible duplicate is a refusal, never proof assigning a USER row."""
    def epoch(value):
        try: return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, OverflowError): return None
    admission = epoch(receipt.get("first_created_at"))
    for candidate in candidates:
        observed = epoch(candidate.get("timestamp"))
        if ((admission is None or observed is None or observed >= admission)
                and candidate.get("wire_digest") in {None, receipt.get("wire_digest")}):
            return True
    return False



class QueuedSendNow:
    def __init__(self, store, sessions, comms, tmux, *, owner=None, verify_executable=None, source_snapshot=None, clock=None):
        self.store, self.sessions, self.comms, self.tmux = store, sessions, comms, tmux
        self.owner = owner or str(uuid.uuid4())
        self.clock = clock or time.time
        self.verify_executable = verify_executable or self._verify_executable
        self.source_snapshot = source_snapshot or self._source_snapshot
        self.jobs = {}
        self.reconcile_task = None
        self.dirty = False
        self.closed = False
        self.tick_lock = asyncio.Lock()
        self.scan_offset = 0

    async def _peer_tmux(self, row):
        if row["host"] == self.sessions.local_host:
            return self.tmux
        hosts = getattr(self.comms, "hosts", None)
        if hosts is None or row["host"] not in hosts.peers or hosts.is_local(row["host"]):
            raise VerbError("unsupported_host", "Selected remote host is not configured")
        await hosts.ensure_reachable(row["host"], "queued_send_now")
        return hosts.tmux_for(row["host"])

    async def _remote_snapshot(self, row):
        await self._peer_tmux(row)  # Exclude Hosts' unknown-peer local fallback.
        from queued_send_now_probe import program
        fields = ("host", "session_name", "provider", "status", "session_generation",
                  "pane_pid", "account_binding", "observer_binding", "claude_session_id")
        expected = {key: row.get(key) for key in fields}
        try:
            rc, output = await self.comms.hosts.run_command(row["host"], "python3", "-c",
                program(row["provider"]), json.dumps(expected), timeout=12.0)
            if rc or len(output) > 262144:
                return None
            result = json.loads(output)
            return result if isinstance(result, dict) and result.get("ok") is True else None
        except (OSError, ValueError, TypeError, asyncio.TimeoutError):
            return None

    async def _verify_executable(self, row):
        if row["host"] != self.sessions.local_host:
            return await self._remote_snapshot(row) is not None
        binding = row.get("account_binding") or {}
        def verify():
            try:
                with Path(binding["executable"]).open("rb") as source:
                    return hashlib.file_digest(source, "sha256").hexdigest() == binding["executable_sha256"]
            except (OSError, KeyError):
                return False
        return await asyncio.to_thread(verify)

    async def _source_snapshot(self, row):
        if row["host"] != self.sessions.local_host:
            return await self._remote_snapshot(row)
        # Reuse the installed ingress proof: exact pane PID/start/executable,
        # and for Codex its unique writable launch-bound native descriptor.
        from ingest import Ingest, _open_writable_transcripts
        async def discard(_frame): pass
        async def observe():
            observer = Ingest(self.store, self.sessions, self.tmux, discard,
                local_host=self.sessions.local_host, recent_limit=500)
            root = await observer._provider_root(row, row["session_name"])
            if root is None: return None
            if row["provider"] != "codex": return {"pane_identity": list(root)}
            transcript = (row.get("observer_binding") or {}).get("transcript") or {}
            paths = await _open_writable_transcripts(root[0], row.get("account_binding"))
            if (len(paths) != 1 or paths[0][0] != transcript.get("path")
                    or list(paths[0][1]) != transcript.get("file_id")):
                return None
            def read_prefix():
                fd = os.open(paths[0][0], os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as source:
                    info = os.fstat(source.fileno())
                    if (info.st_dev, info.st_ino) != paths[0][1]: return None
                    header = json.loads(source.readline(1 << 20))
                    if (header.get("type") != "session_meta" or
                            (header.get("payload") or {}).get("id") != transcript.get("session_id")):
                        return None
                    end = info.st_size
                    start = max(0, end - (1 << 20))
                    source.seek(start)
                    if start: source.readline()  # discard one partial boundary record
                    data = source.read(end - source.tell())
                    if not data.endswith(b"\n"): return None
                    records = [json.loads(line) for line in data.splitlines() if line.strip()]
                    ordinals = [r.get("ordinal") for r in records]
                    if (not ordinals or any(type(n) is not int or n < 0 for n in ordinals)
                            or any(a >= b for a, b in zip(ordinals, ordinals[1:]))): return None
                    controls = [r["payload"] for r in records if r.get("type") == "event_msg"
                        and (r.get("payload") or {}).get("type") in {"task_started", "task_complete", "turn_aborted"}]
                    if not controls: return None
                    from codex_rollout_norm import normalize_codex_rollout_record
                    from store import _send_wire_digest
                    def incomplete(value):
                        if isinstance(value, dict):
                            retained = value.get("retained_source")
                            return (isinstance(retained, dict) and retained.get("complete") is False) or any(incomplete(v) for v in value.values())
                        return isinstance(value, list) and any(incomplete(v) for v in value)
                    candidates = []
                    for record in records:
                        for event in normalize_codex_rollout_record(record, host=row["host"],
                                session_name=row["session_name"], session_id=transcript["session_id"]):
                            if event["kind"] in {"USER", "TELL"}:
                                candidates.append({"wire_digest": None if incomplete(record) else _send_wire_digest(event["text"]),
                                    "timestamp": record.get("timestamp")})
                    last = controls[-1]
                    return {"input_candidates": candidates, "source_session_id": transcript["session_id"], "file_id": list(paths[0][1]),
                        "watermark": ordinals[-1],
                        "active_turn_id": last.get("turn_id") if last["type"] == "task_started" else None}
            try: return await asyncio.to_thread(read_prefix)
            except (OSError, ValueError, TypeError, KeyError): return None
        try: return await asyncio.wait_for(observe(), timeout=4.0)
        except (asyncio.TimeoutError, OSError): return None

    async def _record(self, key, create=None, *, expected=None, update=None):
        def operation(conn):
            with conn:
                row = conn.execute("SELECT v FROM kv WHERE k=?", (PREFIX + key,)).fetchone()
                value = json.loads(row[0]) if row else None
                if value is None and create is not None:
                    conn.execute("INSERT INTO kv(k,v) VALUES(?,?)", (PREFIX + key, json.dumps(create)))
                    return create, True
                if value is not None and update is not None and (expected is None or value["state"] in expected):
                    value.update(update)
                    conn.execute("UPDATE kv SET v=? WHERE k=?", (json.dumps(value), PREFIX + key))
                    return value, True
                return value, False
        return await self.store.submit(operation)

    async def _events(self, sid):
        # Query retained authoritative history, not a bounded arrival-order tail.
        def operation(conn):
            host, name = self.sessions.split(sid)
            row = conn.execute("SELECT created_at FROM sessions WHERE host=? AND session_name=? AND status='open'", (host, name)).fetchone()
            if not row:
                return []
            rows = conn.execute("SELECT event_id,event_json FROM session_event_tail WHERE stream_id=? AND session_created_at=? AND (json_extract(event_json,'$.kind') IN ('USER','TELL') OR json_extract(event_json,'$.raw.subtype') IN ('provider-turn-control','provider-turn-error')) ORDER BY event_id", (sid, row[0])).fetchall()
            return [{**json.loads(row[1]), "daemon_seq": row[0]} for row in rows]
        return await self.store.submit(operation)

    @staticmethod
    def _intake(events, original_request_id):
        return next((e for e in events if e.get("kind") in {"USER", "TELL"}
                     and e.get("client_origin") is not True and e.get("request_id") == original_request_id), None)

    @staticmethod
    def _active_turn(events):
        signals = [signal for event in events if (signal := native_turn_signal(event))]
        terminal = {(s["source_session_id"], s["turn_id"]) for s in signals if s["state"] != "task_started"}
        active = {(s["source_session_id"], s["turn_id"]): s for s in signals
                  if s["state"] == "task_started" and (s["source_session_id"], s["turn_id"]) not in terminal}
        return next(iter(active.values())) if len(active) == 1 else None

    @staticmethod
    def _reply(msg, operation=None, *, reason=None):
        if (operation or {}).get("state") == "indeterminate":
            return {"type": "send.interrupt.error", "request_id": msg.get("request_id"),
                "error_code": "queued_send_now_indeterminate", "error": "Send now is awaiting native confirmation"}
        return {"type": "send.interrupt.ok", "request_id": msg.get("request_id"),
                "interrupted": False, "landed": False,
                "confirm": "coalesced" if reason else "interrupt_unconfirmed",
                "coalesced": bool(reason), "queued_now_state": (operation or {}).get("state", reason),
                "queued_now_route": (operation or {}).get("route")}

    @staticmethod
    def operation_key(sid, generation, request):
        return hashlib.sha256(json.dumps([sid, generation, request]).encode()).hexdigest()

    async def bind_receipt_locked(self, sid, generation, request, receipt_id):
        # Called only after Comms observes native queue ownership while holding
        # the existing lifecycle and input locks. Seconds-resolution timestamps
        # cannot bind a receipt across close/reopen of the same pane name.
        key = self.operation_key(sid, generation, request)
        return await self._record(key, {"key": key, "stream_id": sid,
            "generation": generation, "original_request_id": request,
            "receipt_id": receipt_id, "state": "queued", "created_at": time.time()})

    async def send(self, msg):
        host, name = str(msg.get("host") or ""), str(msg.get("session_name") or "")
        sid = f"{host}:{name}"
        descriptor = msg.get("queued_send_now")
        auth = msg.get("_auth_context") or {}
        if not auth.get("operator_authenticated") or not isinstance(descriptor, dict):
            raise VerbError("queued_send_now_denied", "Operator queued action required")
        request = str(descriptor.get("original_request_id") or "")
        optimistic = str(descriptor.get("optimistic_id") or "")
        generation = str(msg.get("expected_session_generation") or "")
        if not all([host, name, request, optimistic, generation]):
            raise VerbError("queued_send_now_identity_required", "Selected queued identity required")
        async with self.sessions._lifecycle_lock(host, name), self.comms._pane_input_lock(sid):
            row = await self.store.fetch_session(host, name) or {}
            if row.get("status") != "open" or row.get("session_generation") != generation:
                raise VerbError("stale_session_generation", "Selected generation changed")
            route = route_for(row, self.sessions.local_host,
                getattr(getattr(self.comms, "hosts", None), "peers", None))
            if not route:
                raise VerbError("queued_send_now_unsupported", "Provider executable is not qualified for Send now")
            tmux = await self._peer_tmux(row)
            if not await self.verify_executable(row):
                raise VerbError("queued_send_now_unsupported", "Provider executable is not qualified for Send now")
            account_runtime = getattr(self.comms, "account_runtime", None)
            if account_runtime is not None:
                await account_runtime.before_input(row)
            receipt = await self.store.get_send_receipt(sid, request, include_display_text=True, include_submission_identity=True)
            actor = self.comms._send_provenance(msg)[1]
            if (not receipt or receipt.get("optimistic_id") != optimistic
                    or not receipt.get("actor_trusted") or receipt.get("actor_stream_id") != actor):
                raise VerbError("queued_send_now_receipt_mismatch", "Selected provider-owned receipt required")
            key = self.operation_key(sid, generation, request)
            previous, _ = await self._record(key)
            if previous and previous["state"] != "queued":
                return self._reply(msg, previous, reason="operation_replay")
            if not previous:
                raise VerbError("queued_send_now_generation_unproven", "Selected queued generation is not confirmed")
            events = await self._events(sid)
            if self._intake(events, request):
                return self._reply(msg, reason="already_consumed")
            if not str(receipt.get("reason") or "").endswith("provider_queued"):
                raise VerbError("queued_send_now_pending", "Selected native intake is not confirmed")
            pane = await tmux.capture(name)
            composer_empty = route != "claude-native" or claude_composer_empty(pane)
            if route == "claude-native" and not composer_empty:
                rc, styled = await tmux.run("capture-pane", "-p", "-e", "-t", f"={name}:")
                composer_empty = rc == 0 and claude_queue_composer_empty(styled)
                pane = re.sub(r"\x1b\[[0-9;]*m", "", styled) if rc == 0 else ""
            if (not prompt_in_native_queue(pane, receipt.get("display_text") or "", row["provider"])
                    or not composer_empty):
                raise VerbError("queued_send_now_pending", "Native queue ownership or empty draft is not confirmed")
            active = self._active_turn(events) if route == "codex-continue" else None
            source = await self.source_snapshot(row)
            if not source:
                raise VerbError("queued_send_now_source_unproven", "Native pane identity is not confirmed")
            # A native input can precede daemon ingestion while the pane still
            # paints old queue chrome. Digest candidates are not attribution:
            # any possible selected intake since admission refuses keys until
            # normal authoritative reconciliation resolves it.
            if possible_native_intake(receipt, source.get("input_candidates", [])):
                raise VerbError("queued_send_now_pending", "Native intake is ahead of reconciliation")
            if route == "codex-continue" and (not active or active["turn_id"] != source.get("active_turn_id")
                    or active["source_session_id"] != source.get("source_session_id")):
                raise VerbError("queued_send_now_turn_unproven", "Active native turn is not confirmed")
            operation = {"key": key, "stream_id": sid, "generation": generation,
                         "original_request_id": request, "optimistic_id": optimistic,
                         "receipt_id": receipt["receipt_id"], "route": route, "owner": self.owner,
                         "provider": row["provider"], "executable_sha256": row["account_binding"]["executable_sha256"],
                         "native_turn_id": (active or {}).get("turn_id"),
                         "source_session_id": (active or {}).get("source_session_id"),
                         "source_file_id": source.get("file_id"), "source_watermark": source.get("watermark"),
                         "native_executable_sha256": source.get("native_executable_sha256"),
                         "native_executable_file_id": source.get("native_executable_file_id"),
                         "watermark": max((e["daemon_seq"] for e in events), default=0),
                         "state": "dispatch_claimed", "created_at": time.time()}
            operation, claimed = await self._record(key, expected={"queued"}, update=operation)
            if not claimed:
                return self._reply(msg, operation, reason="operation_replay")
            current = await self.store.fetch_session(host, name) or {}
            if any(current.get(key) != row.get(key) for key in (
                    "status", "session_generation", "pane_pid", "observer_binding", "account_binding")):
                await self._record(key, expected={"dispatch_claimed"},
                                   update={"state": "cancelled", "reason": "binding_changed"})
                raise VerbError("stale_session_generation", "Selected source binding changed")
            try:
                keys = ("C-x", "C-s") if route == "claude-native" else ("Escape",)
                result = await tmux.run("send-keys", "-t", f"={name}:", *keys)
                if result and result[0] != 0:
                    raise VerbError("queued_send_now_key_uncertain", "Native control dispatch is unconfirmed")
            except BaseException:
                await self._record(key, expected={"dispatch_claimed"}, update={"state": "indeterminate", "reason": "key_dispatch_uncertain"})
                raise
            operation, _ = await self._record(key, expected={"dispatch_claimed"}, update={"state": "completed" if route == "claude-native" else "wait_interrupt"})
            self.request_tick()
            return self._reply(msg, operation)

    async def cancel_locked(self, sid, generation):
        def operation(conn):
            with conn:
                for row in conn.execute("SELECT k,v FROM kv WHERE k LIKE ?", (PREFIX + "%",)).fetchall():
                    value = json.loads(row[1])
                    if value["stream_id"] == sid and value["generation"] == generation and value["state"] not in {"completed", "cancelled"}:
                        value.update(state="cancelled", reason="explicit_stop")
                        conn.execute("UPDATE kv SET v=? WHERE k=?", (json.dumps(value), row[0]))
        await self.store.submit(operation)

    async def validate_continue_locked(self, key, sid, generation, request_id):
        operation, _ = await self._record(key)
        host, name = self.sessions.split(sid)
        row = await self.store.fetch_session(host, name) or {}
        qualified = route_for(row, self.sessions.local_host,
            getattr(getattr(self.comms, "hosts", None), "peers", None)) == "codex-continue" and await self.verify_executable(row)
        source = await self.source_snapshot(row) if qualified else None
        source_matches = bool(source and operation and source.get("source_session_id") == operation.get("source_session_id")
            and source.get("file_id") == operation.get("source_file_id"))
        return bool(qualified and source_matches and row.get("status") == "open" and row.get("session_generation") == generation
                    and operation and operation["state"] == "continue_claimed"
                    and operation["stream_id"] == sid and operation["generation"] == generation
                    and operation.get("continue_request_id") == request_id)

    async def _continue(self, operation):
        key, sid = operation["key"], operation["stream_id"]
        host, name = self.sessions.split(sid)
        try:
            result = await self.comms.send({"host": host, "session_name": name,
                "text": "continue", "request_id": operation["continue_request_id"],
                "_queued_send_now_operation": key,
                "_auth_context": {"service_authenticated": True, "service_actor": "daemon:send-now"}})
            if result.get("delivery") != "not_landed":
                update = {"state": "continue_pending_proof", "reason": None}
            elif result.get("reason") == "queued_continue_cancelled":
                # Comms refused before native input. Preserve the durable ID and
                # retry validation on the bounded cadence, never on every event.
                update = {"state": "continue_retry_wait", "reason": "continue_validation_pending",
                          "next_retry_at": self.clock() + CONTINUE_RETRY_DELAY_S}
            else:
                update = {"state": "indeterminate", "reason": "continue_not_landed"}
            _saved, changed = await self._record(key, expected={"continue_claimed"}, update=update)
            if changed and update["state"] == "indeterminate":
                log.error("queued Send now continuation indeterminate stream=%s operation=%s reason=%s",
                          sid, key, result.get("reason") or "unknown")
        except asyncio.CancelledError:
            raise
        except Exception:
            # A claimed/possibly transmitted send remains reconciliable by the
            # exact same request ID; never rotate it or replay the original.
            return

    def request_tick(self):
        """Coalesce native-event notifications; never block the ingest pump."""
        if self.closed:
            return
        self.dirty = True
        if self.reconcile_task is not None and not self.reconcile_task.done():
            return
        async def reconcile():
            try:
                while self.dirty and not self.closed:
                    self.dirty = False
                    await self.tick()
            except Exception as exc:
                log.warning("queued Send now reconcile failed: %s", type(exc).__name__)
        self.reconcile_task = asyncio.create_task(reconcile())

    async def tick(self):
        async with self.tick_lock:
            if not self.closed:
                await self._tick()

    async def _tick(self):
        def pending(conn):
            values = [json.loads(row[0]) for row in conn.execute("SELECT v FROM kv WHERE k LIKE ?", (PREFIX + "%",))]
            return [v for v in values if v["state"] not in TERMINAL | {"queued"}
                    or (v["state"] == "indeterminate" and v.get("reason") in {"key_dispatch_uncertain", "key_dispatch_crash"})]
        waiting = await self.store.submit(pending)
        start = self.scan_offset % len(waiting) if waiting else 0
        selected = (waiting[start:] + waiting[:start])[:8]
        self.scan_offset = start + len(selected)
        for operation in selected:
            if self.closed: return
            key, sid = operation["key"], operation["stream_id"]
            if key in self.jobs:
                continue
            host, name = self.sessions.split(sid)
            async with self.sessions._lifecycle_lock(host, name), self.comms._pane_input_lock(sid):
                current, _ = await self._record(key)
                if not current or current["state"] in {"completed", "cancelled"}:
                    continue
                row = await self.store.fetch_session(host, name) or {}
                if row.get("status") != "open" or row.get("session_generation") != current["generation"]:
                    await self._record(key, update={"state": "cancelled", "reason": "generation_closed_or_replaced"})
                    continue
                if current["state"] == "dispatch_claimed":
                    if current["owner"] == self.owner: continue
                    current, _ = await self._record(key, update={"state": "indeterminate", "reason": "key_dispatch_crash"})
                events = await self._events(sid)
                if current["state"] == "indeterminate" and current["route"] == "claude-native":
                    if self._intake(events, current["original_request_id"]):
                        await self._record(key, expected={"indeterminate"}, update={"state": "completed", "reason": "native_intake_observed"})
                    continue
                if current["state"] in {"wait_interrupt", "indeterminate"}:
                    abort = next((signal for event in events if (signal := native_turn_signal(event))
                        and signal["state"] == "turn_aborted" and signal["reason"] == "interrupted"
                        and signal["turn_id"] == current["native_turn_id"]
                        and signal["source_session_id"] == current["source_session_id"]
                        and signal["event_id"] > current["watermark"]
                        and type(signal["source_ordinal"]) is int
                        and type(current.get("source_watermark")) is int
                        and signal["source_ordinal"] > current["source_watermark"]), None)
                    if not abort or not self._intake(events, current["original_request_id"]):
                        continue
                    request_id = "send-now-continue-" + hashlib.sha256(json.dumps([key, abort["event_id"]]).encode()).hexdigest()
                    current, _ = await self._record(key, expected={"wait_interrupt", "indeterminate"}, update={"state": "continue_claimed", "continue_request_id": request_id, "interrupt_event_id": abort["event_id"]})
                if current["state"] == "continue_retry_wait":
                    if self.clock() < current["next_retry_at"]:
                        continue
                    current, _ = await self._record(key, expected={"continue_retry_wait"},
                        update={"state": "continue_claimed", "reason": None})
                if current["state"] == "continue_pending_proof":
                    if self._intake(events, current["continue_request_id"]):
                        await self._record(key, expected={"continue_pending_proof"}, update={"state": "completed"})
                    continue
                if current["state"] == "continue_claimed":
                    task = asyncio.create_task(self._continue(current))
                    self.jobs[key] = task
                    def finished(_task, key=key):
                        self.jobs.pop(key, None)
                        # Retry an uncertain send only on a later native notification
                        # or the existing bounded reconciliation cadence.
                    task.add_done_callback(finished)

    async def close(self):
        self.closed = True
        tasks = list(self.jobs.values())
        if self.reconcile_task is not None:
            tasks.append(self.reconcile_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
