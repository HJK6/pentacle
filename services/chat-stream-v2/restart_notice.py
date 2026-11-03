"""One front-desk wake notice per daemon start, listing in-progress work.

spec_pentacle__fd_restart_resume_notice_2026_11. After the startup sequence the
hook captures the open-seat snapshot and schedules `RestartNotice.run_once` as a
daemon-owned task; nothing on the startup path awaits a probe or a delivery.
`run_once` classifies the start against the durable kv marker, probes each
captured seat once, composes a capped plain-text notice and hands it to the
existing composite-tell delivery (queued while the front desk is unbound or
dead, flushed by the existing restore/rebind paths). The first line starts with
`BLOCKER` so the front-desk digest admits it as a wake.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger("chat_streamd_v2.restart_notice")

MARKER_KEY = "daemon.restart_notice.last"
FROM_STREAM_ID = "daemon:restart-notice"
HEADER_PREFIX = "BLOCKER restart from daemon: "
CAP_BYTES = 6000
PROBE_TIMEOUT_S = 5.0
PROBE_CONCURRENCY = 8
SEAT_LINES = 20
LIST_LINES = 8
TITLE_CHARS = 40
FIELD_CHARS = 300
BOOT_TIME_TOLERANCE_S = 2  # Linux btime can move by a second between reads.
DEFAULT_PRINTER_CMD = (
    "agent-orch ssh vamsh@100.104.128.92 "
    "'/home/vamsh/repos/amaterasu-triforce/bin/printer status'"
)
DEFAULT_CHECKPOINT = "~/agent-workspace/agent-outputs/fd-checkpoint/checkpoint-current.md"
QUESTION_HELD_STATES = ("pending", "approved", "bypassed", "unruled_disabled")
PRINT_SPEC = re.compile(r"^spec_printer__")
KIND_NOTES = {
    "first_instrumented_start": "no earlier start marker; restart cause unknown",
    "host_boot": "host boot time changed since the previous daemon start",
    "deploy": "daemon SHA changed with the same host boot",
    "daemon_restart": "same boot and SHA: crash, stall-checker kickstart or manual restart (not distinguishable)",
}
SEATS_FOOTER = ("  gone = pane absent at probe time (provisional); the reconciler confirms "
                "after 2 observations (~2 min) and closes them; respawn is the owner's call")
RESUME_LINE = ("Resume: agent-orch list; agent-orch prompt list --open; "
               "agent-orch assistant binding; agent-orch inspect <stream>")

# Process start, read once at import (main imports this module at startup).
PROCESS_STARTED_AT = time.time()


def parse_flag(env: Any) -> bool:
    """`PENTACLE_RESTART_NOTICE`: `0`/`1`, default `1`; anything else fails startup."""
    value = str(env.get("PENTACLE_RESTART_NOTICE", "1")).strip()
    if value not in ("0", "1"):
        raise ValueError("PENTACLE_RESTART_NOTICE must be 0 or 1")
    return value == "1"


def host_boot_time() -> int | None:
    """Host boot epoch: `sysctl -n kern.boottime` (as machine_stats), else Linux btime."""
    if platform.system() == "Darwin":
        try:
            raw = subprocess.check_output(["sysctl", "-n", "kern.boottime"], text=True,
                                          stderr=subprocess.PIPE, timeout=2)
            return int(raw.split("sec =", 1)[1].split(",", 1)[0].strip())
        except (IndexError, ValueError, OSError, subprocess.SubprocessError):
            return None
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def classify(previous: dict | None, current: dict) -> str:
    """First instrumented start, host boot, deploy, else daemon restart (in that order)."""
    if not previous:
        return "first_instrumented_start"
    prev_boot, boot = previous.get("boot_time"), current.get("boot_time")
    if (isinstance(prev_boot, (int, float)) and isinstance(boot, (int, float))
            and abs(prev_boot - boot) > BOOT_TIME_TOLERANCE_S):
        return "host_boot"
    prev_sha, sha = str(previous.get("daemon_sha") or ""), str(current.get("daemon_sha") or "")
    if prev_sha and sha and prev_sha != sha:
        return "deploy"
    return "daemon_restart"


def _utc(epoch: float | int | None) -> str:
    if not isinstance(epoch, (int, float)):
        return "unknown"
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _epoch(value: Any) -> float | None:
    """Epoch from a REAL column or an ISO-8601 UTC string."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _sha7(sha: Any) -> str:
    return str(sha or "")[:7] or "unknown"


def _clip(value: Any, limit: int = FIELD_CHARS) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def header(kind: str, current: dict, previous: dict | None) -> str:
    line = (f"{HEADER_PREFIX}{kind} — host booted {_utc(current.get('boot_time'))}; "
            f"daemon started {_utc(current.get('started_epoch'))} sha {_sha7(current.get('daemon_sha'))}")
    if not previous:
        return line + "; no previous marker"
    prev_epoch = _epoch(previous.get("started_at"))
    gap = (f"{int(current['started_epoch'] - prev_epoch)} s"
           if prev_epoch is not None and isinstance(current.get("started_epoch"), (int, float)) else "unknown")
    return (line + f"; previous daemon started {_utc(prev_epoch)} ({gap} before this start) "
            f"sha {_sha7(previous.get('daemon_sha'))}")


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    try:
        parsed = json.loads(value) if value else []
    except (TypeError, ValueError):
        return []
    return [str(v) for v in parsed] if isinstance(parsed, list) else []


def _seat_line(row: dict, fd_stream: str) -> str:
    parent = str(row.get("parent_stream_id") or "")
    parent_label = "FD" if parent and parent == fd_stream else (parent or "top-level")
    title = _clip(row.get("title") or row.get("objective") or "", TITLE_CHARS).replace('"', "'")
    return (f"  {row['stream_id']} {row.get('role') or '-'} {row.get('spec_id') or '-'} "
            f'"{title}" {row.get("probe", "unknown")} parent={parent_label}')


class _List:
    """One capped list section: fixed head, item lines, overflow, droppable."""

    def __init__(self, head: str, lines: list[str], overflow: str, tail: list[str] | None = None,
                 count: int = 0):
        self.head, self.lines, self.overflow, self.tail = head, lines, overflow, tail or []
        self.count = count
        self.dropped = False

    def render(self) -> list[str]:
        if self.dropped and self.lines:
            body = [f"  ({self.count} listed in the count above, omitted for size: {self.overflow})"]
        else:
            body = self.lines
        return [self.head, *body, *self.tail]


def compose(facts: dict) -> str:
    """Eleven fixed sections, per-list caps, 6000-byte total (header never cut)."""
    fd = facts["front_desk"]
    fd_stream = fd.get("stream_id") or ""
    head = [facts["header"], f"  kind {facts['kind']}: {KIND_NOTES.get(facts['kind'], '')}"]

    if fd_stream:
        state = fd.get("probe", "unknown")
        if state == "gone":
            restore = ("auto-restore episode expected (PENTACLE_ASSISTANT_AUTO_RESTORE=1)"
                       if fd.get("auto_restore") else
                       "no auto-restore (PENTACLE_ASSISTANT_AUTO_RESTORE=0): bind a fresh seat")
        elif state == "alive":
            restore = "no restore needed"
        else:
            restore = "pane state unknown"
        fd_line = (f"Front desk: {fd_stream} generation {_clip(fd.get('generation') or '-', 40)} "
                   f"pane {state}; {restore}")
    else:
        fd_line = "Front desk: unbound; this notice stays queued until a seat is bound"
    adv = facts["advisor"]
    adv_line = (f"Advisor: {adv['stream_id']} pane {adv.get('probe', 'unknown')}"
                if adv.get("stream_id") else "Advisor: none configured")

    seats = facts["seats"]
    order = {"gone": 0, "unknown": 1, "alive": 2}
    ranked = sorted(seats, key=lambda r: (order.get(r.get("probe"), 1), str(r.get("created_at") or "")))
    counts = {k: sum(1 for r in seats if r.get("probe") == k) for k in ("alive", "gone")}
    unknown = len(seats) - counts["alive"] - counts["gone"]
    seat_lines = [_seat_line(r, fd_stream) for r in ranked[:SEAT_LINES]]
    if len(ranked) > SEAT_LINES:
        seat_lines.append(f"  +{len(ranked) - SEAT_LINES} more: agent-orch list")
    seat_section = _List(
        f"Seats open at start: {len(seats)} open: {counts['alive']} alive, {counts['gone']} gone, "
        f"{unknown} unknown (probed {_utc(facts.get('probed_at'))})",
        seat_lines, "agent-orch list", [SEATS_FOOTER] if seats else [], count=len(seats))
    if not seats:
        seat_section.head = "Seats open at start: none"

    prints = [r for r in ranked if PRINT_SPEC.match(str(r.get("spec_id") or ""))
              or any(PRINT_SPEC.match(s) for s in _json_list(r.get("qualified_spec_ids")))]
    print_lines = [_seat_line(r, fd_stream) for r in prints[:LIST_LINES]]
    if len(prints) > LIST_LINES:
        print_lines.append(f"  +{len(prints) - LIST_LINES} more: agent-orch list")
    printer = ("  printer state is not probed by the daemon; check: "
               + _clip(facts.get("printer_cmd") or DEFAULT_PRINTER_CMD))
    print_section = _List(f"Print work: {len(prints)} seat(s)" if prints else "Print work: none",
                          print_lines, "agent-orch list", [printer], count=len(prints))

    window_lines: list[str] = []
    window_total = 0
    for kind, rows, render, overflow in (
        ("admission", facts["windows"]["admission"],
         lambda r: f"  admission freeze {r['host']} until {_utc(r.get('held_until'))} reason \"{_clip(r.get('reason') or '-', 80)}\"",
         "agent-orch spawn freeze --help"),
        ("hold", facts["windows"]["holds"],
         lambda r: f"  hold {_clip(r['resource'], 80)} owner {r.get('owner_stream') or '-'} until {_utc(_epoch(r.get('expires_at'))) if r.get('expires_at') else 'no expiry'}",
         "agent-orch hold list"),
        ("screen", facts["windows"]["screen"],
         lambda r: f"  screen lease {r['host']} holder {r.get('holder_stream_id') or '-'} until {_utc(r.get('expires_at'))}",
         "agent-orch screen status"),
    ):
        window_total += len(rows)
        window_lines.extend(render(r) for r in rows[:LIST_LINES])
        if len(rows) > LIST_LINES:
            window_lines.append(f"  +{len(rows) - LIST_LINES} more {kind}: {overflow}")
    window_section = _List(f"Windows: {window_total} active" if window_total else "Windows: none",
                           window_lines, "agent-orch hold list; agent-orch screen status; agent-orch spawn freeze --help",
                           count=window_total)

    now = facts.get("probed_at") or time.time()
    rulings = facts["rulings"]
    pending = rulings["pending"]
    ruling_lines = [
        f"  {r['ruling_request_id']} {r['action']} requester={r.get('requester_stream_id') or '-'} "
        f"target={r.get('target_stream_id') or '-'} age {max(0, int(now - (r.get('created_at') or now)))}s"
        for r in pending[:LIST_LINES]]
    if len(pending) > LIST_LINES:
        ruling_lines.append(f"  +{len(pending) - LIST_LINES} more pending")
    others = rulings["other_counts"]
    ruling_tail = (["  other non-done: " + ", ".join(f"{k} {v}" for k, v in sorted(others.items()))]
                   if others else [])
    ruling_section = _List(
        f"Advisor rulings: {len(pending)} pending" if (pending or others) else "Advisor rulings: none",
        ruling_lines, "pending rulings", ruling_tail, count=len(pending))

    questions = facts["questions"]
    question_lines = [
        f"  {q['question_id']} {q.get('producer') or '-'} {q.get('spec_id') or '-'} "
        f"age {max(0, int(now - _epoch(q.get('created_at')))) if _epoch(q.get('created_at')) else '?'}s {q['source']}"
        for q in questions[:LIST_LINES]]
    if len(questions) > LIST_LINES:
        question_lines.append(f"  +{len(questions) - LIST_LINES} more: agent-orch prompt list --open")
    question_section = _List(
        f"Operator questions: {len(questions)} open" if questions else "Operator questions: none",
        question_lines, "agent-orch prompt list --open", count=len(questions))

    undelivered = facts["undelivered"]
    total = sum(undelivered["by_kind"].values())
    detail = ", ".join(f"{k} {v}" for k, v in sorted(undelivered["by_kind"].items()))
    undelivered_head = (f"Undelivered FD traffic: {total} notice(s); composite queue depth "
                        f"{undelivered['queue_depth']} before this notice")
    undelivered_section = _List(undelivered_head, [f"  by kind: {detail}"] if detail else [],
                                "per-kind counts", count=total)

    cp = facts["checkpoint"]
    cp_line = (f"Checkpoint: {_clip(cp['path'])} exists {cp['size']} bytes modified {_utc(cp['mtime'])}"
               if cp.get("exists") else f"Checkpoint: {_clip(cp['path'])} missing")

    sections: list[Any] = [head, [fd_line], [adv_line], seat_section, print_section, window_section,
                           ruling_section, question_section, undelivered_section, [cp_line], [RESUME_LINE]]

    def render() -> str:
        out: list[str] = []
        for section in sections:
            out.extend(section.render() if isinstance(section, _List) else section)
        return "\n".join(out)

    text = render()
    for section in (seat_section, question_section, ruling_section, window_section,
                    print_section, undelivered_section):
        if len(text.encode()) <= CAP_BYTES:
            break
        section.dropped = True
        text = render()
    if len(text.encode()) > CAP_BYTES:
        # Unreachable with the fixed caps; never cut the header line.
        first, _, rest = text.partition("\n")
        room = max(0, CAP_BYTES - len(first.encode()) - 1)
        text = first + "\n" + rest.encode()[:room].decode(errors="ignore")
    return text


Probe = Callable[[str, str], Awaitable[str]]
Deliver = Callable[..., Awaitable[str]]


class RestartNotice:
    """Single-shot per process: capture at the hook, run once in the background."""

    def __init__(self, *, store: Any, composite: Any, deliver: Deliver, probe: Probe,
                 notify: Any = None, instance_id: str, daemon_sha: str = "",
                 pid: int | None = None, started_at: float | None = None,
                 env: Any = None, boot_time: Callable[[], int | None] = host_boot_time,
                 probe_timeout_s: float = PROBE_TIMEOUT_S,
                 compose_fn: Callable[[dict], str] = compose,
                 clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.composite = composite
        self.deliver = deliver
        self.probe = probe
        self.notify = notify
        self.instance_id = instance_id
        self.daemon_sha = daemon_sha or ""
        self.pid = os.getpid() if pid is None else pid
        self.started_at = PROCESS_STARTED_AT if started_at is None else started_at
        self.env = os.environ if env is None else env
        self.boot_time = boot_time
        self.probe_timeout_s = probe_timeout_s
        self.compose_fn = compose_fn
        self.clock = clock
        self._attempted = False
        self.task: asyncio.Task | None = None

    @property
    def tell_id(self) -> str:
        return f"restart-notice:{self.instance_id}"

    async def capture(self) -> dict:
        """Startup snapshot: open rows, binding and authority. DB reads only."""
        binding = await self.composite.binding()
        rows = await self.store.submit(lambda conn: [dict(r) for r in conn.execute(
            "SELECT host, session_name, parent_stream_id, role, spec_id, qualified_spec_ids, title, "
            "objective, created_at, pane_status, presumed_dead_at FROM sessions WHERE status='open' "
            # Composite projections are synthetic chat rows with no pane.
            "AND COALESCE(role, '') != 'assistant_composite' ORDER BY created_at")])
        shape_raw = await self.store.get("assistant.authority.shape")
        try:
            shape = json.loads(shape_raw) if shape_raw else {}
        except ValueError:
            shape = {}
        return {
            "binding": {"stream_id": str(binding.get("stream_id") or ""),
                        "generation": str(binding.get("generation") or "")},
            "authority_stream_id": str((shape or {}).get("stream_id") or ""),
            "rows": rows,
            "captured_at": self.clock(),
        }

    def schedule(self, snapshot: dict | None) -> asyncio.Task:
        self.task = asyncio.create_task(self.run_once(snapshot), name="restart-notice")
        return self.task

    async def _state(self, stream_id: str) -> str:
        host, _, name = stream_id.partition(":")
        if not host or not name:
            return "unknown"
        try:
            state = await asyncio.wait_for(self.probe(host, name), self.probe_timeout_s)
        except Exception:  # noqa: BLE001 - timeout, transport or verb failure
            return "unknown"
        return state if state in ("alive", "gone") else "unknown"

    async def _probe_all(self, streams: list[str]) -> dict[str, str]:
        gate = asyncio.Semaphore(PROBE_CONCURRENCY)

        async def one(stream: str) -> tuple[str, str]:
            async with gate:
                return stream, await self._state(stream)

        return dict(await asyncio.gather(*(one(s) for s in streams)))

    async def _rows(self, sql: str, args: tuple = ()) -> list[dict]:
        def op(conn: sqlite3.Connection) -> list[dict]:
            try:
                return [dict(r) for r in conn.execute(sql, args)]
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    return []
                raise
        return await self.store.submit(op)

    async def _questions(self) -> list[dict]:
        found: dict[str, dict] = {}
        if self.notify is not None:
            for q in await self.notify._db.call("list_agent_questions", open_only=True):
                # The existing stale-question policy, read-only: a question whose
                # producer is no longer current is excluded, never expired here.
                if not await self.notify._producer_is_current(q):
                    continue
                qid = str(q.get("question_id") or "")
                if qid:
                    found[qid] = {"question_id": qid, "producer": q.get("producer_stream_id"),
                                  "spec_id": q.get("spec_id"), "created_at": q.get("created_at"),
                                  "source": "store"}
        marks = ",".join("?" for _ in QUESTION_HELD_STATES)
        for row in await self._rows(
                f"SELECT requester_stream_id, intent_json, created_at FROM v2_assistant_lane_rulings "
                f"WHERE action='question' AND state IN ({marks})", QUESTION_HELD_STATES):
            try:
                envelope = json.loads(row["intent_json"]).get("envelope") or {}
            except (TypeError, ValueError, AttributeError):
                envelope = {}
            qid = str(envelope.get("question_id") or "")
            if not qid or qid in found:
                continue
            found[qid] = {"question_id": qid,
                          "producer": envelope.get("producer_stream_id") or row["requester_stream_id"],
                          "spec_id": envelope.get("spec_id"),
                          "created_at": envelope.get("created_at") or row["created_at"], "source": "held"}
        return sorted(found.values(), key=lambda q: _epoch(q.get("created_at")) or 0.0)

    async def gather(self, kind: str, current: dict, previous: dict | None, snapshot: dict) -> dict:
        fd_stream = snapshot["binding"]["stream_id"]
        adv_stream = snapshot["authority_stream_id"]
        rows = []
        for r in snapshot["rows"]:
            stream = f"{r['host']}:{r['session_name']}"
            if stream in (fd_stream, adv_stream):
                continue
            rows.append({**r, "stream_id": stream})
        streams = [r["stream_id"] for r in rows] + [s for s in (fd_stream, adv_stream) if s]
        states = await self._probe_all(streams)
        probed_at = self.clock()
        for r in rows:
            r["probe"] = states.get(r["stream_id"], "unknown")
        now = self.clock()
        admission = await self._rows(
            "SELECT host, held_until, reason FROM v2_spawn_admission_hold WHERE held_until > ? ORDER BY held_until",
            (now,))
        # holds.expires_at is ISO text; compare as epochs, not strings.
        holds = [r for r in await self._rows(
            "SELECT resource, owner_stream, expires_at FROM holds WHERE released_at IS NULL "
            "ORDER BY acquired_at")
            if not r.get("expires_at") or (_epoch(r["expires_at"]) or 0.0) > now]
        screen = await self._rows(
            "SELECT host, holder_stream_id, expires_at FROM v2_screen_leases WHERE released_at IS NULL "
            "ORDER BY acquired_at")
        pending = await self._rows(
            "SELECT ruling_request_id, action, requester_stream_id, target_stream_id, created_at "
            "FROM v2_assistant_lane_rulings WHERE state='pending' ORDER BY created_at")
        other_counts = {r["state"]: r["n"] for r in await self._rows(
            "SELECT state, COUNT(*) AS n FROM v2_assistant_lane_rulings "
            "WHERE state NOT IN ('done','pending') GROUP BY state")}
        by_kind = {r["kind"]: r["n"] for r in await self._rows(
            "SELECT kind, COUNT(*) AS n FROM v2_outbound_notices WHERE recipient_stream_id=? "
            "AND delivered_at IS NULL AND terminal_at IS NULL GROUP BY kind", (fd_stream,))} if fd_stream else {}
        depth = (await self._rows(
            "SELECT COUNT(*) AS n FROM v2_assistant_composite_tell_queue WHERE name=?",
            (self.composite.config.name,)) or [{"n": 0}])[0]["n"]
        cp_path = os.path.expanduser(str(self.env.get("PENTACLE_FD_CHECKPOINT_PATH") or DEFAULT_CHECKPOINT))
        try:
            st = os.stat(cp_path)
            checkpoint = {"path": cp_path, "exists": True, "size": st.st_size, "mtime": st.st_mtime}
        except OSError:
            checkpoint = {"path": cp_path, "exists": False}
        return {
            "kind": kind,
            "header": header(kind, current, previous),
            "front_desk": {"stream_id": fd_stream, "generation": snapshot["binding"]["generation"],
                           "probe": states.get(fd_stream, "unknown"),
                           "auto_restore": str(self.env.get("PENTACLE_ASSISTANT_AUTO_RESTORE", "")).strip() == "1"},
            "advisor": {"stream_id": adv_stream, "probe": states.get(adv_stream, "unknown")},
            "seats": rows,
            "probed_at": probed_at,
            "printer_cmd": self.env.get("PENTACLE_RESTART_NOTICE_PRINTER_CMD") or DEFAULT_PRINTER_CMD,
            "windows": {"admission": admission, "holds": holds, "screen": screen},
            "rulings": {"pending": pending, "other_counts": other_counts},
            "questions": await self._questions(),
            "undelivered": {"by_kind": by_kind, "queue_depth": depth},
            "checkpoint": checkpoint,
        }

    async def run_once(self, snapshot: dict | None = None) -> dict | None:
        """Classify, compose, deliver once, then write the marker. No retries."""
        if self._attempted:
            return None
        self._attempted = True
        try:
            raw = await self.store.get(MARKER_KEY)
            previous = json.loads(raw) if raw else None
        except Exception:  # noqa: BLE001 - an unreadable marker reads as none
            log.exception("restart notice marker read failed", extra={"subsystem": "restart_notice"})
            previous = None
        if isinstance(previous, dict) and previous.get("instance_id") == self.instance_id:
            return None  # This process already attempted (e.g. a re-entered hook).
        if not isinstance(previous, dict):
            previous = None
        boot = await asyncio.to_thread(self.boot_time)
        current = {"boot_time": boot, "daemon_sha": self.daemon_sha, "started_epoch": self.started_at}
        kind = classify(previous, current)
        try:
            text = header(kind, current, previous)
        except Exception:  # noqa: BLE001 - the header must still be sent
            text = f"{HEADER_PREFIX}{kind}"
        try:
            if snapshot is None:
                raise RuntimeError("startup snapshot unavailable")
            facts = await self.gather(kind, current, previous, snapshot)
            text = self.compose_fn(facts)
        except Exception:  # noqa: BLE001 - compose failure sends the header only
            log.exception("restart notice compose failed; sending the header only",
                          extra={"subsystem": "restart_notice"})
        marker: dict[str, Any] = {
            "instance_id": self.instance_id, "pid": self.pid, "started_at": _utc(self.started_at),
            "boot_time": boot, "daemon_sha": self.daemon_sha, "notice_tell_id": self.tell_id, "kind": kind,
        }
        try:
            outcome = await self.deliver(self.composite, text, self.tell_id, from_stream_id=FROM_STREAM_ID)
            if outcome not in ("queued", "committed"):
                # A handoff without a committed or durably held reply is not
                # receipt: queue it so the restore/rebind flush delivers it.
                await self.store.enqueue_composite_tell(
                    name=self.composite.config.name, tell_id=self.tell_id,
                    from_stream_id=FROM_STREAM_ID, body=text, request_id="")
        except Exception as exc:  # noqa: BLE001 - logged; the marker records the attempt
            log.exception("restart notice delivery failed", extra={"subsystem": "restart_notice"})
            marker["error"] = f"{type(exc).__name__}: {_clip(exc, 200)}"
        try:
            await self.store.put(MARKER_KEY, json.dumps(marker, sort_keys=True, separators=(",", ":")))
        except Exception:  # noqa: BLE001 - best effort; startup never fails here
            log.exception("restart notice marker write failed", extra={"subsystem": "restart_notice"})
        log.info("restart notice kind=%s tell_id=%s error=%s", kind, self.tell_id, marker.get("error"),
                 extra={"subsystem": "restart_notice"})
        return marker
