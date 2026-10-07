"""Daemon restart-continuity matrix: a real `main.py` process, killed mid-spawn.

Each cell runs one admitted spawn (or one client journey) against a disposable
daemon on `--port 0`, an isolated sqlite `--db`, a run-owned tmux socket and the
stub Claude provider (`restart_stub_claude.py`) launched through the real tuple
path. Stage triggers are read from durable state or stub behaviour, never
sleeps. The daemon is then signalled the way launchd does (SIGTERM with a 20 s
exit window, or SIGKILL), restarted on the same DB, and the spawn is measured
against the spec's Target State 1-3.

Classification: PASS, PRODUCT_FAIL (the daemon violated a target), HARNESS_ERROR
(the stage was not reached or the harness itself failed) or CLEANUP_FAIL.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from websockets.sync.client import connect

from tools.run_gate import _process_group_popen_kwargs
from tests.soak.harness import TmuxNamespace, LOCAL_HOST

SERVICE_DIR = Path(__file__).resolve().parents[2]
REPO_SERVICES = SERVICE_DIR.parent
STUB = Path(__file__).resolve().parent / "restart_stub_claude.py"
PROMPT_STAGE_ROOT = Path("/tmp/pentacle-prompt-stage")
LAUNCHD_EXIT_TIMEOUT_S = 20.0
SETTLE_DEADLINE_S = 100.0  # startup pass + one 60 s recurring pass + 20 s adoption window
PASS, PRODUCT_FAIL, HARNESS_ERROR, CLEANUP_FAIL = "PASS", "PRODUCT_FAIL", "HARNESS_ERROR", "CLEANUP_FAIL"


def slugify_cwd(cwd: str) -> str:
    return cwd.replace("/", "-").replace("_", "-").replace(".", "-")


def prompt_stage_path(text: str) -> Path:
    return PROMPT_STAGE_ROOT / f"pentacle-initial-prompt-{hashlib.sha256(text.encode()).hexdigest()}.txt"


def unique_prompt(tag: str) -> str:
    body = f"Restart continuity matrix brief {tag} {uuid.uuid4().hex}. "
    return (body * 8)[:400].strip()


# --------------------------------------------------------------------------- #
# disposable daemon
# --------------------------------------------------------------------------- #


class RestartDaemon:
    """`main.py` with the stub Claude on the tuple path, same DB across restarts."""

    def __init__(self, root: Path, namespace: TmuxNamespace, tmux_bin: Path) -> None:
        self.root, self.namespace, self.tmux_bin = root, namespace, tmux_bin
        self.db = str(root / "sessions.db")
        self.home = root / "home"
        self.home.mkdir(exist_ok=True)
        self.spawn_cwd = (root / "seats").resolve()
        self.spawn_cwd.mkdir(exist_ok=True)
        self.projects_root = root / ".claude" / "projects"
        self.projects_root.mkdir(parents=True, exist_ok=True)
        self.logpath = root / "daemon.log"
        self.proc: subprocess.Popen[str] | None = None
        self.port: int | None = None
        self.pids: list[int] = []
        self.ports: list[int] = []
        self.extra_env: dict[str, str] = {}

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def env(self) -> dict[str, str]:
        env = self.namespace.child_env()
        env.update({
            "HOME": str(self.home),
            "PYTHONUSERBASE": os.environ.get("PYTHONUSERBASE") or str(Path.home() / ".local"),
            "PYTHONUNBUFFERED": "1",
        })
        env.update(self.extra_env)
        return env

    def start(self) -> None:
        argv = [
            # First boot picks a free port; a restart rebinds the same one so a
            # client configured with the URL can reconnect (as on 7791).
            sys.executable, "main.py", "--port", str(self.port or 0), "--db", self.db,
            "--local-host", LOCAL_HOST, "--tmux-bin", str(self.tmux_bin),
            "--notifications-db", str(self.root / "notifications.db"),
            "--assets-db", str(self.root / "assets.db"), "--blob-root", str(self.root / "blobs"),
            "--disable-hosts", "--disable-usage-state-publisher",
            "--claude-bin", str(STUB), "--spawn-cwd", str(self.spawn_cwd),
            "--projects-root", str(self.projects_root),
        ]
        with open(self.logpath, "ab") as log:
            log.write(f"\n==== daemon start {time.time():.3f} ====\n".encode())
            offset = log.tell()
        logf = open(self.logpath, "ab", buffering=0)
        self.proc = subprocess.Popen(argv, cwd=str(SERVICE_DIR), env=self.env(), stdout=logf,
                                     stderr=subprocess.STDOUT, text=True, **_process_group_popen_kwargs())
        logf.close()
        self.pids.append(self.proc.pid)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"daemon exited before binding rc={self.proc.returncode}")
            with open(self.logpath, "r", errors="replace") as fh:
                fh.seek(offset)
                for line in fh:
                    if "listening on" in line:
                        self.port = int(line.rsplit(":", 1)[1].strip())
                        self.ports.append(self.port)
                        self.wait_spawn_ready()
                        return
            time.sleep(0.02)
        raise RuntimeError("daemon did not bind within 15 s")

    def wait_spawn_ready(self, timeout: float = 30.0) -> None:
        """The startup reconcile pass runs before spawn admission opens."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                reply = rpc(self.url, {"type": "spawn_status", "idempotency_key": "__probe__"}, timeout=5)
                if reply.get("error_code") != "daemon_starting":
                    return
            except Exception:  # noqa: BLE001 - still starting
                pass
            time.sleep(0.1)

    def stop(self, sig: signal.Signals, on_signal: Callable[[], Any] | None = None) -> dict[str, Any]:
        """launchd semantics (`kickstart -k`): `sig` to the job's PID, SIGKILL after
        ExitTimeOut (20 s), then the rest of the job's process group is killed
        (AbandonProcessGroup=false)."""
        assert self.proc is not None
        proc, started = self.proc, time.monotonic()
        pgid = os.getpgid(proc.pid)
        os.kill(proc.pid, sig)
        at_signal = on_signal() if on_signal is not None else None
        escalated = False
        try:
            proc.wait(timeout=LAUNCHD_EXIT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            escalated = True
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
        try:
            os.killpg(pgid, signal.SIGKILL)  # stragglers (blocked tmux wrappers)
        except ProcessLookupError:
            pass
        self.proc = None
        return {"signal": sig.name, "rc": proc.returncode, "exit_s": round(time.monotonic() - started, 3),
                "escalated_to_sigkill": escalated, "at_signal": at_signal}

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def kill_quiet(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                self.proc.wait(timeout=10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
        self.proc = None


def rpc(url: str, payload: dict[str, Any], *, timeout: float = 10.0) -> dict[str, Any]:
    payload = {"request_id": f"rm-{uuid.uuid4().hex[:12]}", **payload}
    with connect(url, open_timeout=5, max_size=None) as ws:
        ws.send(json.dumps(payload))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no reply for {payload['type']}")
            frame = json.loads(ws.recv(timeout=remaining))
            if frame.get("request_id") == payload["request_id"]:
                return frame


class PendingRpc:
    """A request whose reply may never arrive (the daemon dies under it)."""

    def __init__(self, url: str, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.reply: dict[str, Any] | None = None
        self.error: str | None = None
        self.sent = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(url,), daemon=True)
        self.thread.start()
        self.sent.wait(10)

    def _run(self, url: str) -> None:
        try:
            with connect(url, open_timeout=5, max_size=None) as ws:
                ws.send(json.dumps(self.payload))
                self.sent.set()
                while True:
                    frame = json.loads(ws.recv(timeout=400))
                    if frame.get("request_id") == self.payload["request_id"]:
                        self.reply = frame
                        return
        except Exception as exc:  # noqa: BLE001 - recorded as evidence
            self.error = f"{type(exc).__name__}: {exc}"
            self.sent.set()


def spawn_payload(name: str, prompt: str, key: str, request_id: str | None = None) -> dict[str, Any]:
    return {
        "type": "spawn", "objective": "Restart continuity matrix seat", "objective_supported": True,
        "provider": "claude", "model": "claude-opus-5-5", "effort": "high",
        "host": LOCAL_HOST, "session_name": name, "role": "worker", "visibility": "hidden",
        "initial_prompt": prompt, "idempotency_key": key,
        "request_id": request_id or f"spawn-{uuid.uuid4()}",
    }


# --------------------------------------------------------------------------- #
# per-cell environment
# --------------------------------------------------------------------------- #


@dataclass
class Cell:
    root: Path
    namespace: TmuxNamespace
    daemon: RestartDaemon
    control: Path
    transcript_dir: Path
    evidence: Path
    log: list[dict[str, Any]] = field(default_factory=list)

    def note(self, event: str, **data: Any) -> None:
        entry = {"t": round(time.time(), 3), "event": event, **data}
        self.log.append(entry)
        with open(self.evidence / "timeline.jsonl", "a") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")

    def set_mode(self, mode: str) -> None:
        (self.control / "mode").write_text(mode)

    def release(self) -> None:
        (self.control / "release").write_text("1")

    # durable reads ------------------------------------------------------- #

    def sql(self, query: str, args: tuple = ()) -> list[dict[str, Any]]:
        conn = sqlite3.connect(f"file:{self.daemon.db}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(query, args)]
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return []
            raise
        finally:
            conn.close()

    def reservations(self, name: str) -> list[dict[str, Any]]:
        return self.sql("SELECT * FROM v2_stream_reservations WHERE session_name=?", (name,))

    def outcomes(self, name: str) -> list[dict[str, Any]]:
        return self.sql("SELECT * FROM v2_spawn_outcomes WHERE session_name=?", (name,))

    def session_rows(self, name: str) -> list[dict[str, Any]]:
        return self.sql("SELECT session_name,status,bootstrap_state,created_at,closed_at,close_kind,pane_pid,"
                        "provider FROM sessions WHERE session_name=?", (name,))

    def panes(self, name: str) -> list[str]:
        return [n for n in self.namespace.session_names() if n == name]

    def inputs(self, name: str) -> list[dict[str, Any]]:
        path = self.control / f"{name}.inputs"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def snapshot(self, label: str, name: str) -> dict[str, Any]:
        snap = {
            "label": label,
            "reservations": self.reservations(name),
            "outcomes": self.outcomes(name),
            "sessions": self.session_rows(name),
            "panes": self.panes(name),
            "inputs": self.inputs(name),
        }
        for res in snap["reservations"]:
            if res.get("payload"):
                res["payload"] = f"<{len(res['payload'])} bytes>"
        (self.evidence / f"snapshot-{label}.json").write_text(json.dumps(snap, indent=1, default=str))
        return snap

    def capture(self, name: str, label: str) -> None:
        out = self.namespace.run("capture-pane", "-p", "-t", f"={name}:")
        (self.evidence / f"pane-{name}-{label}.txt").write_text(out.stdout or out.stderr)


def tmux_blocking_wrapper(root: Path, socket: str, real_tmux: str, control: Path) -> Path:
    """`--tmux-bin`: blocks `new-session` while `control/block_new_session` exists."""
    wrapper = root / "tmux-wrapper"
    wrapper.write_text(
        "#!/bin/sh\n"
        "case \" $* \" in\n"
        f"  *\" new-session \"*) if [ -e {shlex.quote(str(control / 'block_new_session'))} ]; then\n"
        f"      echo $$ > {shlex.quote(str(control / 'new_session_blocked'))}\n"
        f"      while [ -e {shlex.quote(str(control / 'block_new_session'))} ]; do sleep 0.05; done\n"
        "    fi ;;\n"
        "esac\n"
        f"exec {shlex.quote(real_tmux)} -L {shlex.quote(socket)} \"$@\"\n"
    )
    wrapper.chmod(0o755)
    return wrapper


@contextmanager
def cell_env(root: Path, evidence: Path) -> Iterator[Cell]:
    root.mkdir(parents=True, exist_ok=True)
    evidence.mkdir(parents=True, exist_ok=True)
    control = root / "control"
    control.mkdir(exist_ok=True)
    namespace = TmuxNamespace(root)
    daemon_holder: list[RestartDaemon] = []
    spawn_cwd = (root / "seats").resolve()
    transcript_dir = root / ".claude" / "projects" / slugify_cwd(str(spawn_cwd))
    namespace._env.update({
        "RESTART_STUB_CONTROL": str(control),
        "RESTART_STUB_TRANSCRIPT_DIR": str(transcript_dir),
    })
    namespace.start()
    wrapper = tmux_blocking_wrapper(root, namespace.socket, namespace.real_tmux, control)
    daemon = RestartDaemon(root, namespace, wrapper)
    daemon_holder.append(daemon)
    cell = Cell(root, namespace, daemon, control, transcript_dir, evidence)
    try:
        yield cell
    finally:
        try:
            (control / "block_new_session").unlink(missing_ok=True)
            cell.release()
            if daemon.alive():
                daemon.stop(signal.SIGTERM)
        finally:
            daemon.kill_quiet()
            try:
                import shutil
                if daemon.logpath.exists():
                    shutil.copy(daemon.logpath, evidence / "daemon.log")
            except OSError:
                pass
            namespace.kill_server()


def wait_for(predicate: Callable[[], Any], timeout: float, interval: float = 0.02) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


# --------------------------------------------------------------------------- #
# daemon cells S1-S4 x {SIGTERM, SIGKILL}
# --------------------------------------------------------------------------- #


def _deliveries(cell: Cell, name: str, prompt: str) -> int:
    staged = prompt_stage_path(prompt).name
    return sum(1 for item in cell.inputs(name) if staged in item["text"] or prompt[:60] in item["text"])


def _stage_reached(cell: Cell, stage: str, name: str, prompt: str) -> Callable[[], Any]:
    if stage == "S1":
        def s1() -> Any:
            if not (cell.control / "new_session_blocked").exists():
                return None
            rows = cell.reservations(name)
            return rows and rows[0].get("payload") and not rows[0].get("tmux_created")
        return s1
    if stage == "S2":
        def s2() -> Any:
            rows = cell.session_rows(name)
            return (cell.control / f"{name}.booting").exists() and rows and rows[0]["status"] == "open"
        return s2
    if stage == "S4":
        return lambda: (cell.control / f"{name}.pasted").exists()
    staged = prompt_stage_path(prompt)
    return staged.exists


def _settled(cell: Cell, name: str, request_id: str) -> dict[str, Any] | None:
    outcome = next((o for o in cell.outcomes(name) if o.get("request_id") == request_id), None)
    if outcome is None:
        return None
    reservations = [r for r in cell.reservations(name) if r.get("request_id") == request_id]
    rows = cell.session_rows(name)
    if outcome["state"] == "delivered" and rows and rows[0].get("bootstrap_state") in {"ready", "started"}:
        return outcome
    if outcome["state"] == "failed" and not reservations:
        return outcome
    return None


def measure_after_restart(cell: Cell, *, stage: str, name: str, prompt: str, key: str,
                          request_id: str, pane_before_restart: bool, restart_at: float) -> dict[str, Any]:
    url = cell.daemon.url
    settled = wait_for(lambda: _settled(cell, name, request_id), SETTLE_DEADLINE_S, interval=0.5)
    settle_s = round(time.monotonic() - restart_at, 1)
    cell.note("settled", outcome=settled and {k: settled.get(k) for k in ("state", "reason", "delivery_evidence")},
              settle_s=settle_s)
    after = cell.snapshot("after-settle", name)
    cell.capture(name, "after-settle") if after["panes"] else None
    status = rpc(url, {"type": "spawn_status", "idempotency_key": key})
    awaited = rpc(url, {"type": "await_spawn", "spawn_request_id": request_id})
    refire_payload = spawn_payload(name, prompt, key)
    try:
        refire = rpc(url, refire_payload, timeout=30)
    except Exception as exc:  # noqa: BLE001 - a hang is the finding
        refire = {"type": "harness.timeout", "error": f"{type(exc).__name__}: {exc}"}
    time.sleep(0)  # no-op; ordering marker for the timeline
    after_refire = cell.snapshot("after-refire", name)
    cell.note("reads", spawn_status=status, await_spawn=awaited, refire=refire)
    outcome = next((o for o in after["outcomes"] if o.get("request_id") == request_id), None)
    open_rows = [r for r in after_refire["sessions"] if r["status"] == "open"]
    deliveries = _deliveries(cell, name, prompt)
    checks: dict[str, bool] = {}
    pane_now = bool(after["panes"])
    success_path = pane_before_restart or pane_now
    if success_path:
        checks["one_pane"] = len(after_refire["panes"]) == 1
        checks["one_open_row"] = len(open_rows) == 1
        checks["prompt_delivered_once"] = deliveries == 1
        checks["outcome_delivered"] = bool(outcome) and outcome["state"] == "delivered"
        checks["bootstrap_ready_or_started"] = bool(after["sessions"]) and after["sessions"][0].get(
            "bootstrap_state") in {"ready", "started"}
        checks["settled_within_cadence"] = settled is not None
        checks["reservation_released"] = not [r for r in after["reservations"] if r.get("request_id") == request_id]
        checks["spawn_status_delivered"] = any(o.get("state") == "delivered" for o in status.get("outcomes") or [])
        checks["await_spawn_ready"] = awaited.get("type") == "await_spawn.ok" and awaited.get("state") == "ready"
        checks["refire_replays_same_seat"] = (refire.get("type") == "spawn.ok" and bool(refire.get("replayed"))
                                              and refire.get("stream_id") == f"{LOCAL_HOST}:{name}")
        fresh = None
    else:
        checks["no_pane"] = not after_refire["panes"]
        checks["no_open_row"] = not open_rows
        checks["outcome_failed_with_reason"] = bool(outcome) and outcome["state"] == "failed" and bool(outcome.get("reason"))
        checks["settled_within_cadence"] = settled is not None
        checks["reservation_released"] = not [r for r in after["reservations"] if r.get("request_id") == request_id]
        checks["spawn_status_failed_reason"] = any(o.get("state") == "failed" for o in status.get("outcomes") or [])
        recorded = str((outcome or {}).get("reason") or "").split(":", 1)[0]
        checks["refire_refused_with_recorded_reason"] = (
            refire.get("type") == "spawn.error" and bool(recorded) and refire.get("error_code") == recorded)
        cell.set_mode("normal")
        fresh_key = f"{key}-fresh"
        try:
            fresh = rpc(url, spawn_payload(name, prompt, fresh_key), timeout=60)
        except Exception as exc:  # noqa: BLE001
            fresh = {"type": "harness.timeout", "error": f"{type(exc).__name__}: {exc}"}
        final = cell.snapshot("after-fresh-key", name)
        checks["fresh_key_one_seat"] = (fresh.get("type") == "spawn.ok" and fresh.get("state") == "ready"
                                        and len(final["panes"]) == 1 and _deliveries(cell, name, prompt) == 1)
        cell.note("fresh_key", reply=fresh)
    return {
        "path": "success" if success_path else "durable_failure",
        "checks": checks,
        "outcome": outcome and {k: outcome.get(k) for k in ("state", "reason", "delivery_evidence")},
        "settle_s": settle_s,
        "deliveries": deliveries,
        "refire": {k: refire.get(k) for k in ("type", "error_code", "error", "state", "replayed", "stream_id")},
        "spawn_status": status.get("outcomes"),
        "await_spawn": {k: awaited.get(k) for k in ("type", "state", "error_code", "error", "pending_reconcile")},
        "fresh": fresh and {k: fresh.get(k) for k in ("type", "state", "error_code", "error")},
    }


def run_daemon_cell(stage: str, sig: signal.Signals, root: Path, evidence: Path, *,
                    hold_intent_write: bool = False) -> dict[str, Any]:
    """One stage x signal cell. Returns the classified record (never raises).

    `hold_intent_write` (S3 only): the instant the staged prompt file appears the
    harness takes the sqlite write lock (`BEGIN IMMEDIATE`), so the daemon's
    `record_spawn_intent` write waits behind it; the signal then lands
    deterministically before the intent. The lock is released right after the
    at-signal snapshot (well inside sqlite's 5 s busy timeout)."""
    record: dict[str, Any] = {"cell": f"{stage}-{sig.name}", "stage": stage, "signal": sig.name,
                              "hold_intent_write": hold_intent_write}
    name = f"seat-{stage.lower()}"
    prompt = unique_prompt(f"{stage}-{sig.name}")
    key = f"k-{stage.lower()}-{sig.name.lower()}-{uuid.uuid4().hex[:6]}"
    request_id = f"spawn-{uuid.uuid4()}"
    try:
        with cell_env(root, evidence) as cell:
            cell.set_mode({"S2": "hold_ready", "S4": "withhold_user"}.get(stage, "normal"))
            if stage == "S1":
                (cell.control / "block_new_session").write_text("1")
            cell.daemon.start()
            cell.note("daemon_started", pid=cell.daemon.proc.pid, port=cell.daemon.port)
            pending = PendingRpc(cell.daemon.url, spawn_payload(name, prompt, key, request_id))
            cell.note("spawn_sent", key=key, request_id=request_id)
            reached = wait_for(_stage_reached(cell, stage, name, prompt), 60, interval=0.001)
            holder = None
            if reached and hold_intent_write:
                holder = sqlite3.connect(cell.daemon.db, timeout=5, isolation_level=None)
                holder.execute("BEGIN IMMEDIATE")
            if not reached:
                cell.snapshot("stage-not-reached", name)
                record.update(classification=HARNESS_ERROR, detail=f"stage {stage} not reached in 60 s",
                              pending_reply=pending.reply, pending_error=pending.error)
                return record
            log_offset = cell.daemon.logpath.stat().st_size
            def at_signal() -> dict[str, Any]:
                rows = cell.reservations(name)
                snap = {"payload_set": bool(rows and rows[0].get("payload")),
                        "tmux_created": bool(rows and rows[0].get("tmux_created")),
                        "session_rows": len(cell.session_rows(name))}
                if holder is not None:
                    holder.execute("ROLLBACK")
                    holder.close()
                return snap
            stop = cell.daemon.stop(sig, on_signal=at_signal)
            cell.note("daemon_stopped", **stop)
            pre = cell.snapshot("pre-restart", name)
            with open(cell.daemon.logpath, "r", errors="replace") as fh:
                fh.seek(log_offset)
                shutdown_log = fh.read()
            store_stopped_errors = shutdown_log.count("store is not running")
            (cell.control / "block_new_session").unlink(missing_ok=True)
            cell.release()
            pane_before = bool(pre["panes"])
            if pane_before:
                cell.capture(name, "pre-restart")
            record["pre_restart"] = {
                "payload_set": bool(pre["reservations"] and pre["reservations"][0].get("payload")),
                "tmux_created": bool(pre["reservations"] and pre["reservations"][0].get("tmux_created")),
                "outcome": pre["outcomes"][0]["state"] if pre["outcomes"] else None,
                "outcome_reason": pre["outcomes"][0].get("reason") if pre["outcomes"] else None,
                "session": pre["sessions"][0]["status"] if pre["sessions"] else None,
                "pane": pane_before,
                "stop": stop,
                "client_reply": pending.reply and pending.reply.get("type"),
                "client_error": pending.error,
                "owner_instance_id": pre["reservations"][0].get("owner_instance_id") if pre["reservations"] else None,
                "store_stopped_errors": store_stopped_errors,
            }
            restart_at = time.monotonic()
            cell.daemon.start()
            cell.note("daemon_restarted", pid=cell.daemon.proc.pid, port=cell.daemon.port)
            result = measure_after_restart(cell, stage=stage, name=name, prompt=prompt, key=key,
                                           request_id=request_id, pane_before_restart=pane_before,
                                           restart_at=restart_at)
            record.update(result)
            if sig == signal.SIGTERM:
                # Target 4: a graceful stop reaches a durable handoff before the
                # store stops (no write lost to "store is not running", owner
                # released so the next instance may adopt without a TTL wait).
                result["checks"]["graceful_shutdown_handoff"] = (
                    store_stopped_errors == 0
                    and not record["pre_restart"]["owner_instance_id"]
                ) if pre["reservations"] else store_stopped_errors == 0
            failed = [k for k, ok in result["checks"].items() if not ok]
            record["failed_checks"] = failed
            record["classification"] = PRODUCT_FAIL if failed else PASS
        residue = subprocess.run(["pgrep", "-f", str(root)], capture_output=True, text=True).stdout.split()
        record["cleanup"] = "ok" if not residue else {"residual_pids": residue}
        if residue and record.get("classification") == PASS:
            record["classification"] = CLEANUP_FAIL
    except Exception as exc:  # noqa: BLE001 - classified, never hidden
        import traceback
        record.update(classification=HARNESS_ERROR, detail=f"{type(exc).__name__}: {exc}",
                      traceback=traceback.format_exc()[-3000:])
    (evidence / "record.json").write_text(json.dumps(record, indent=1, default=str))
    return record


def run_s3(sig: signal.Signals, root: Path, evidence: Path, *, max_attempts: int = 20) -> list[dict[str, Any]]:
    """S3: signal the instant the staged prompt file appears; classify the side of
    `record_spawn_intent` from the pre-restart sqlite snapshot. Repeat until both
    sub-windows are observed (or 20 attempts); a window never hit is a
    HARNESS_ERROR, never a PASS."""
    windows: dict[str, dict[str, Any]] = {}
    attempts: list[dict[str, Any]] = []
    for attempt in range(1, max_attempts + 1):
        # Natural timing first; once a natural attempt has shown post-intent,
        # alternate with the deterministic intent-write hold for pre-intent.
        hold = "pre_intent" not in windows and attempt % 2 == 0
        rec = run_daemon_cell("S3", sig, root / f"a{attempt}", evidence / f"attempt-{attempt:02d}",
                              hold_intent_write=hold)
        pre = ((rec.get("pre_restart") or {}).get("stop") or {}).get("at_signal") or {}
        if rec.get("classification") == HARNESS_ERROR and not pre:
            window = "harness_error"
        elif not pre.get("payload_set"):
            window = "pre_intent"
        elif not pre.get("tmux_created"):
            window = "post_intent_pre_pane"
        else:
            window = "overshoot_pane_created"
        rec["s3_window"] = window
        attempts.append({"attempt": attempt, "window": window, "hold_intent_write": hold,
                         "classification": rec.get("classification"),
                         "failed_checks": rec.get("failed_checks")})
        if window in {"pre_intent", "post_intent_pre_pane"} and window not in windows:
            windows[window] = {**rec, "cell": f"S3{'a' if window == 'pre_intent' else 'b'}-{sig.name}",
                               "attempt": attempt}
        if len(windows) == 2:
            break
    (evidence / "attempts.json").write_text(json.dumps(attempts, indent=1))
    out = []
    for window, suffix in (("pre_intent", "a"), ("post_intent_pre_pane", "b")):
        if window in windows:
            out.append({**windows[window], "attempt_log": attempts})
        else:
            out.append({"cell": f"S3{suffix}-{sig.name}", "stage": "S3", "signal": sig.name,
                        "s3_window": window, "classification": HARNESS_ERROR,
                        "detail": "window not hit", "attempt_log": attempts})
    return out


# --------------------------------------------------------------------------- #
# client cells C1-C3 (the real agent-orch CLI as a subprocess)
# --------------------------------------------------------------------------- #

AGENT_ORCH_DIR = REPO_SERVICES / "agent-orch"
REPORT_RESULT = json.dumps({
    "summary": "Restart continuity stub child finished.",
    "findings": [{"severity": "info", "where": "restart_matrix", "issue": "none", "suggested_fix": None}],
    "next_action": "lead_merge",
})


def token_file(cell: Cell, name: str) -> Path:
    key = hashlib.sha256(f"{LOCAL_HOST}:{name}".encode()).hexdigest()[:24]
    return cell.daemon.spawn_cwd / ".pentacle-stream-tokens" / f"{key}.token"


def cli_env(cell: Cell, as_name: str | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("AGENT_ORCH_", "PENTACLE_")) and k not in {"TMUX", "TMUX_PANE"}}
    env.update({
        "HOME": str(cell.daemon.home),
        "PYTHONUSERBASE": cell.daemon.env()["PYTHONUSERBASE"],
        "PYTHONPATH": str(AGENT_ORCH_DIR),
        "AGENT_ORCH_WS_URL": cell.daemon.url,
        "AGENT_ORCH_HOST_ID": LOCAL_HOST,
    })
    if as_name:
        sid = f"{LOCAL_HOST}:{as_name}"
        env.update({"AGENT_ORCH_STREAM_ID": sid, "PENTACLE_STREAM_ID": sid,
                    "AGENT_ORCH_STREAM_TOKEN_FILE": str(token_file(cell, as_name))})
    return env


def cli(cell: Cell, as_name: str | None, *args: str) -> subprocess.Popen[str]:
    # Evidence only: print the transport exception's traceback the CLI folds
    # into its typed error, so a classification can name the raising frame.
    code = ("import sys, traceback, agent_orch.cli as c\n"
            "orig = c._direct_rpc_transport_error\n"
            "def wrapped(prefix, rid, exc, **kw):\n"
            "    traceback.print_exception(exc, file=sys.stderr)\n"
            "    return orig(prefix, rid, exc, **kw)\n"
            "c._direct_rpc_transport_error = wrapped\n"
            "sys.exit(c.main())\n")
    argv = [sys.executable, "-c", code, *args]
    return subprocess.Popen(argv, env=cli_env(cell, as_name), cwd=str(cell.root), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


def finish(proc: subprocess.Popen[str], timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        return {"rc": "timeout", "stdout": out[-4000:], "stderr": err[-4000:]}
    return {"rc": proc.returncode, "stdout": out[-4000:], "stderr": err[-4000:],
            "waited_s": round(time.monotonic() - started, 2)}


def seed_seats(cell: Cell, *names: str, parent: str | None = None) -> dict[str, dict[str, Any]]:
    replies: dict[str, dict[str, Any]] = {}
    for name in names:
        payload = spawn_payload(name, "", f"seed-{name}")
        payload.pop("initial_prompt")
        payload["visibility"] = "default"
        if parent and name != parent:
            payload["parent_stream_id"] = f"{LOCAL_HOST}:{parent}"
        reply = rpc(cell.daemon.url, payload, timeout=60)
        if reply.get("type") != "spawn.ok":
            raise RuntimeError(f"seed spawn {name} failed: {reply}")
        if not token_file(cell, name).exists():
            raise RuntimeError(f"seed seat {name} has no token file")
        replies[name] = reply
    return replies


def run_c1(root: Path, evidence: Path, *, outage_s: float = 150.0) -> dict[str, Any]:
    """C1: `agent-orch await --from <child>` across a 150 s daemon outage; the stub
    child files its terminal report after the restart."""
    record: dict[str, Any] = {"cell": "C1", "outage_s": outage_s}
    try:
        with cell_env(root, evidence) as cell:
            cell.daemon.start()
            seed_seats(cell, "parent", "child", parent="parent")
            child = f"{LOCAL_HOST}:child"
            started = time.monotonic()
            waiter = cli(cell, "parent", "await", "--from", child, "--timeout", "400")
            registered = wait_for(lambda: cell.sql(
                "SELECT * FROM v2_awaiters WHERE stream_id=? AND outcome='pending'", (child,)), 30, 0.05)
            if not registered:
                record.update(classification=HARNESS_ERROR, detail="await never registered",
                              cli=finish(waiter, 5))
                return record
            cell.note("await_registered")
            stop = cell.daemon.stop(signal.SIGTERM)
            cell.note("daemon_stopped", **stop)
            client_exit = wait_for(lambda: waiter.poll() is not None, outage_s, 0.1)
            cell.note("outage_elapsed", client_exited_during_outage=bool(client_exit))
            cell.daemon.start()
            cell.note("daemon_restarted", port=cell.daemon.port)
            reporter = finish(cli(cell, "child", "report", "--status", "done", "--msg-id", "0",
                                  "--report-id", "c1-child-report", "--result", REPORT_RESULT), 60)
            cell.note("child_reported", **reporter)
            awaited = finish(waiter, 400 - (time.monotonic() - started))
            awaited["elapsed_s"] = round(time.monotonic() - started, 1)
            cell.note("await_finished", **awaited)
            reports = cell.sql("SELECT report_id, status FROM v2_reports WHERE from_stream_id=?", (child,))
            recovery = finish(cli(cell, "parent", "await", "--from", child, "--timeout", "30"), 60)
            record.update(stop=stop, await_cli=awaited, report_cli=reporter, reports=reports,
                          fresh_reawait=recovery)
            ok = awaited["rc"] == 0 and "c1-child-report" in awaited["stdout"]
            record["checks"] = {
                "await_survives_outage_returns_report": ok,
                "report_landed_once": [r["report_id"] for r in reports].count("c1-child-report") == 1,
            }
    except Exception as exc:  # noqa: BLE001
        import traceback
        record.update(classification=HARNESS_ERROR, detail=f"{type(exc).__name__}: {exc}",
                      traceback=traceback.format_exc()[-3000:])
    return _close_client_record(record, root, evidence)


def run_c2(root: Path, evidence: Path, *, variant: str = "pre_admission") -> dict[str, Any]:
    """C2: `agent-orch spawn --idempotency-key K`, restarted after K's durable claim
    and before the final `spawn.ok` reaches the client. `pre_admission` holds the
    spawn at `new-session` (no reply of any kind sent; the spec trigger);
    `post_admission` holds it after the paste, i.e. after the early
    `spawn.ok state=starting` admission reply but before `ready`."""
    record: dict[str, Any] = {"cell": f"C2-{variant}", "variant": variant}
    try:
        with cell_env(root, evidence) as cell:
            cell.daemon.start()
            seed_seats(cell, "parent")
            if variant == "pre_admission":
                (cell.control / "block_new_session").write_text("1")
            else:
                cell.set_mode("withhold_user")
            key = f"c2-key-{uuid.uuid4().hex[:8]}"
            prompt_file = cell.root / "c2-prompt.txt"
            prompt = unique_prompt("C2")
            prompt_file.write_text(prompt)

            def args_for(k: str) -> tuple[str, ...]:
                return ("spawn", "--provider", "claude", "--model", "claude-opus-5-5", "--effort", "high",
                        "--host", LOCAL_HOST, "--role", "worker", "--objective", "C2 restart client seat",
                        "--visibility", "hidden", "--parent", f"{LOCAL_HOST}:parent",
                        "--initial-prompt-file", str(prompt_file), "--idempotency-key", k, "--timeout", "185")

            spawner = cli(cell, "parent", *args_for(key))

            def claimed() -> Any:
                rows = cell.sql("SELECT * FROM v2_stream_reservations WHERE idempotency_key=?", (key,))
                if not rows or not rows[0].get("payload"):
                    return None
                if variant == "pre_admission":
                    return rows if (cell.control / "new_session_blocked").exists() else None
                return rows if (cell.control / f"{rows[0]['session_name']}.pasted").exists() else None

            rows = wait_for(claimed, 60, 0.02)
            if not rows:
                record.update(classification=HARNESS_ERROR, detail="claim not observed", cli=finish(spawner, 5))
                return record
            name, request_id = rows[0]["session_name"], rows[0]["request_id"]
            cell.note("claimed", name=name, request_id=request_id, key=key)
            stop = cell.daemon.stop(signal.SIGTERM)
            (cell.control / "block_new_session").unlink(missing_ok=True)
            cell.release()
            cell.daemon.start()
            first = finish(spawner, 200)
            cell.note("spawn_cli_finished", **first)
            settled = wait_for(lambda: _settled(cell, name, request_id), SETTLE_DEADLINE_S, 0.5)
            outcome = next((o for o in cell.outcomes(name) if o.get("request_id") == request_id), None)
            status = finish(cli(cell, "parent", "spawn-status", key), 60)
            awaited = finish(cli(cell, "parent", "await-spawn", "--request-id", request_id, "--timeout", "30"), 60)
            refire = finish(cli(cell, "parent", *args_for(key)), 200)
            snap = cell.snapshot("after-refire", name)
            record.update(stop=stop, first_cli=first, spawn_status_cli=status, await_spawn_cli=awaited,
                          refire_cli=refire, settled=bool(settled), name=name, request_id=request_id, key=key,
                          outcome=outcome and {k: outcome.get(k) for k in ("state", "reason", "delivery_evidence")})
            try:
                first_json = json.loads(first["stdout"].strip().splitlines()[-1])
            except (ValueError, IndexError):
                first_json = {}
            typed = json.dumps(first_json)
            # The key is named in the typed reply or on the CLI's documented
            # pre-RPC `spawn key: K` stderr line (cli.py prints it for exactly
            # this interrupted-caller case); the request id must be in the reply.
            key_named = key in typed or f"spawn key: {key}" in first["stderr"]
            ready = (first_json.get("type") == "spawn.ok" and first_json.get("state") == "ready"
                     and first_json.get("stream_id") == f"{LOCAL_HOST}:{name}")
            recorded_code = str((outcome or {}).get("reason") or "").split(":", 1)[0]
            checks = {
                "typed_reply_names_key_and_request": ready or (
                    first_json.get("type") in {"spawn.error", "spawn.indeterminate"}
                    and key_named and request_id in typed),
                # Target 5: a retry-eligible verb survives an outage shorter than
                # its deadline and returns the terminal state (ready, or the
                # recorded failure) rather than a transport error.
                "client_returns_terminal_state": ready or (
                    first_json.get("type") == "spawn.error" and bool(recorded_code)
                    and first_json.get("error_code") == recorded_code),
                "no_idempotency_key_conflict": "idempotency_key_conflict" not in (refire["stdout"] + refire["stderr"]),
            }
            if outcome and outcome["state"] == "delivered":
                checks.update({
                    "spawn_status_truthful": status["rc"] == 0 and '"delivered"' in status["stdout"],
                    "await_spawn_truthful": awaited["rc"] == 0 and '"ready"' in awaited["stdout"],
                    "refire_replays_one_seat": refire["rc"] == 0 and '"replayed":true' in refire["stdout"]
                        and f"{LOCAL_HOST}:{name}" in refire["stdout"],
                    "one_pane": len(snap["panes"]) == 1,
                    "prompt_delivered_once": _deliveries(cell, name, prompt) == 1,
                })
            else:
                recorded = str((outcome or {}).get("reason") or "").split(":", 1)[0]
                fresh = finish(cli(cell, "parent", *args_for(f"{key}-fresh")), 200)
                record["fresh_cli"] = fresh
                final = cell.snapshot("after-fresh", name)
                checks.update({
                    "outcome_failed_durably": bool(outcome) and outcome["state"] == "failed" and settled is not None,
                    "spawn_status_truthful": status["rc"] == 0 and '"failed"' in status["stdout"],
                    "await_spawn_truthful": '"spawn_failed"' in awaited["stdout"] and recorded in awaited["stdout"],
                    "refire_refused_with_recorded_reason": refire["rc"] != 0 and bool(recorded)
                        and f'"error_code":"{recorded}"' in refire["stdout"],
                    "no_pane_for_failed_request": not snap["panes"],
                    "fresh_key_one_seat": fresh["rc"] == 0 and '"state":"ready"' in fresh["stdout"]
                        and len([n for n in cell.namespace.session_names() if n.startswith("v2-")]) == 1,
                })
                del final
            record["checks"] = checks
    except Exception as exc:  # noqa: BLE001
        import traceback
        record.update(classification=HARNESS_ERROR, detail=f"{type(exc).__name__}: {exc}",
                      traceback=traceback.format_exc()[-3000:])
    return _close_client_record(record, root, evidence)


def run_c3(root: Path, evidence: Path) -> dict[str, Any]:
    """C3: `agent-orch report` issued while a launchd restart is in progress."""
    record: dict[str, Any] = {"cell": "C3"}
    try:
        with cell_env(root, evidence) as cell:
            cell.daemon.start()
            seed_seats(cell, "parent", "child", parent="parent")
            child = f"{LOCAL_HOST}:child"
            os.kill(cell.daemon.proc.pid, signal.SIGTERM)
            reporter = cli(cell, "child", "report", "--status", "done", "--msg-id", "0",
                           "--report-id", "c3-child-report", "--result", REPORT_RESULT)
            stop = cell.daemon.stop(signal.SIGTERM) if cell.daemon.alive() else {"signal": "SIGTERM"}
            cell.daemon.proc = None
            cell.daemon.start()
            result = finish(reporter, 120)
            reports = cell.sql("SELECT report_id FROM v2_reports WHERE from_stream_id=?", (child,))
            record.update(stop=stop, report_cli=result, reports=reports)
            record["checks"] = {
                "report_cli_succeeds": result["rc"] == 0,
                "report_landed_once": [r["report_id"] for r in reports].count("c3-child-report") == 1,
            }
    except Exception as exc:  # noqa: BLE001
        import traceback
        record.update(classification=HARNESS_ERROR, detail=f"{type(exc).__name__}: {exc}",
                      traceback=traceback.format_exc()[-3000:])
    return _close_client_record(record, root, evidence)


def _close_client_record(record: dict[str, Any], root: Path, evidence: Path) -> dict[str, Any]:
    if "classification" not in record:
        failed = [k for k, ok in record.get("checks", {}).items() if not ok]
        record["failed_checks"] = failed
        record["classification"] = PRODUCT_FAIL if failed else PASS
    residue = subprocess.run(["pgrep", "-f", str(root)], capture_output=True, text=True).stdout.split()
    record["cleanup"] = "ok" if not residue else {"residual_pids": residue}
    if residue and record["classification"] == PASS:
        record["classification"] = CLEANUP_FAIL
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "record.json").write_text(json.dumps(record, indent=1, default=str))
    return record


# --------------------------------------------------------------------------- #
# C4 / C4b: the daily retro `run --on-demand` path across a restart
# --------------------------------------------------------------------------- #

RETRO_TOOL = SERVICE_DIR / "tools" / "daily_retro.py"


def _seed_codex_rows(cell: Cell, names: tuple[str, ...]) -> dict[str, dict[str, str]]:
    """Daemon stopped: open hidden Codex worker rows (the retro's Sol/Astra seats)
    with a granted token and a live pane in the run-owned tmux namespace."""
    import asyncio
    sys.path.insert(0, str(REPO_SERVICES))
    from store import Store, STREAM_TOKEN_HASH_VERSION  # noqa: E402

    seeded: dict[str, dict[str, str]] = {}

    async def go() -> None:
        store = Store(cell.daemon.db)
        store.start()
        try:
            for name in names:
                model = "gpt-6.1-sol" if name == "sol" else "gpt-6-astra"
                effort = "medium" if name == "sol" else "high"
                cell.namespace.run("new-session", "-d", "-s", name, "sleep", "3600")
                pid = cell.namespace.run("display-message", "-p", "-t", f"={name}:", "#{pane_pid}").stdout.strip()
                row = await store.open_session(
                    LOCAL_HOST, name, provider="codex", role="worker", visibility="hidden",
                    pane_status="pane_alive", pane_pid=pid, effective_model=model, effective_effort=effort,
                    requested_model=model, requested_effort=effort, self_close_on_completion=True,
                    objective=f"daily retro {name} stage")
                token = f"restart-matrix-{name}-{uuid.uuid4().hex}"
                await store.grant_stream_token(LOCAL_HOST, name, hashlib.sha256(token.encode()).hexdigest(),
                                               STREAM_TOKEN_HASH_VERSION)
                path = cell.root / f"{name}.token"
                path.write_text(token)
                seeded[name] = {"generation": row["session_generation"], "token_file": str(path)}
        finally:
            store.stop()

    asyncio.run(go())
    return seeded


def _retro_env(cell: Cell) -> dict[str, str]:
    env = cli_env(cell, None)
    env["PYTHONPATH"] = os.pathsep.join([str(AGENT_ORCH_DIR), str(REPO_SERVICES), str(SERVICE_DIR)])
    return env


def _retro(cell: Cell, cfg: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.Popen[str]:
    return subprocess.Popen([sys.executable, str(RETRO_TOOL), *args, "--config", str(cfg)], cwd=str(SERVICE_DIR),
                            env={**_retro_env(cell), **(env or {})}, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


def _bart_texts(cell: Cell, marker: str) -> int:
    return sum(1 for item in cell.inputs("bart") if marker in item["text"])


def run_c4(root: Path, evidence: Path, *, stay_down: bool = False) -> dict[str, Any]:
    """C4: restart during the Astra report await (stub Astra reports after the
    restart). C4b (`stay_down`): the daemon stays down until the pass gives up."""
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    record: dict[str, Any] = {"cell": "C4b" if stay_down else "C4"}
    try:
        sys.path.insert(0, str(REPO_SERVICES))
        from _shared.operator_auth import OperatorCredentialRegistry  # noqa: E402
        from tools import daily_retro as retro  # noqa: E402
        with cell_env(root, evidence) as cell:
            registry = OperatorCredentialRegistry(cell.daemon.home / ".config/pentacle-stream/operator-credentials.json")
            registry.initialize()
            _, envelope = registry.issue("pentacle", label="restart matrix disposable operator")
            token_path = cell.root / "operator-token"
            token_path.write_text(envelope)
            cell.daemon.start()
            bart = seed_seats(cell, "bart")["bart"]
            bart_generation = (bart.get("session") or {}).get("session_generation")
            cell.daemon.stop(signal.SIGTERM)
            seats = _seed_codex_rows(cell, ("sol", "astra"))
            cell.daemon.extra_env.update({
                "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
                "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": "soakchat:assistant",
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": f"{LOCAL_HOST}:bart",
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": str(bart_generation),
            })
            cell.daemon.start()
            # Shared memory + retained run state shaped like the 2026-10-07 incident:
            # Sol finished (packet retained), Astra admitted and awaited.
            memory, state = cell.root / "memory", cell.root / "state"
            for folder in ("completed", "deprecated"):
                (memory / "work" / folder).mkdir(parents=True, exist_ok=True)
            now = datetime.now(timezone.utc)
            yesterday = (now.astimezone(ZoneInfo("America/Chicago")) - timedelta(days=1)).date().isoformat()
            spec = memory / "work/completed/restart-fixture/spec.md"
            spec.parent.mkdir(parents=True, exist_ok=True)
            spec.write_text(f"---\nid: spec_restart_fixture\ntype: spec\nstatus: completed\ncompleted_at: '{yesterday}'\n"
                            "---\n\n## Retro\nRestart fixture lesson.\n")
            cfg = cell.root / "retro.json"
            cfg.write_text(json.dumps({"timezone": "America/Chicago", "memory_root": str(memory), "state_root": str(state),
                                       "ws_url": cell.daemon.url, "token_path": str(token_path), "host": LOCAL_HOST,
                                       "isolated": True}))
            collected = finish(_retro(cell, cfg, "collect", "--now", now.isoformat()), 60)
            settings = retro.Settings.load(cfg)
            manifest_path = next((state / "runs").glob("*/collection.json"))
            manifest = retro.read(manifest_path)
            run_id, run_root = manifest["run_id"], manifest_path.parent
            packet = {"run_id": run_id, "candidates": [], "dispositions": [
                {"id": src["id"], "fingerprint": src["fingerprint"], "reason": "explicit no useful change"}
                for src in manifest["sources"]]}
            retro.validate_packet(packet, manifest)
            sol_packet = {**packet, "collection": retro.worker_collection(manifest, manifest_path)}
            ns = settings.namespace
            retro.atomic(run_root / "sol.json", {
                "attempt": 1, "created_at": retro.now_iso(), "report_id": f"daily-retro-{ns}-sol-{run_id}-1",
                "payload": {"request_id": f"daily-retro-{ns}-{run_id}-sol-1"}, "stream_id": f"{LOCAL_HOST}:sol",
                "generation": seats["sol"]["generation"], "packet": sol_packet, "packet_hash": retro.digest(sol_packet)})
            astra_report_id = f"daily-retro-{ns}-astra-{run_id}-1"
            astra_key = f"daily-retro-{ns}-{run_id}-astra-1"
            retro.atomic(run_root / "astra.json", {
                "attempt": 1, "created_at": retro.now_iso(), "report_id": astra_report_id,
                "payload": {"request_id": astra_key, "idempotency_key": astra_key}, "stream_id": f"{LOCAL_HOST}:astra",
                "generation": seats["astra"]["generation"]})
            sol_before = (run_root / "sol.json").read_text()
            cell.note("retro_state_seeded", run_id=run_id, collect=collected)
            astra = f"{LOCAL_HOST}:astra"
            # C4b bounds the reconnect window with the retained client env
            # override, so "down past the reconnect window" is reachable.
            pass1 = _retro(cell, cfg, "run", "--on-demand",
                           env={"AGENT_ORCH_RPC_RETRY_DEADLINE_S": "20"} if stay_down else None)
            awaiting = wait_for(lambda: cell.sql(
                "SELECT * FROM v2_awaiters WHERE stream_id=? AND outcome='pending'", (astra,)), 60, 0.05)
            if not awaiting:
                record.update(classification=HARNESS_ERROR, detail="astra await never registered",
                              pass1=finish(pass1, 10), collect=collected)
                return record
            cell.note("astra_await_registered")
            stop = cell.daemon.stop(signal.SIGTERM)
            if stay_down:
                gave_up = wait_for(lambda: pass1.poll() is not None, 600, 0.2)
                cell.note("pass1_exit_while_down", exited=bool(gave_up))
            cell.daemon.start()
            report_env = cli_env(cell, None)
            report_env.update({"AGENT_ORCH_STREAM_ID": astra, "PENTACLE_STREAM_ID": astra,
                               "AGENT_ORCH_STREAM_TOKEN_FILE": seats["astra"]["token_file"]})
            result = json.loads(REPORT_RESULT)
            result["extras"] = {"daily_retro": packet}
            code = "import sys; from agent_orch.cli import main; sys.exit(main())"
            astra_report = finish(subprocess.Popen(
                [sys.executable, "-c", code, "report", "--status", "done", "--msg-id", "0", "--report-id",
                 astra_report_id, "--result", json.dumps(result)], env=report_env, cwd=str(cell.root),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True), 60)
            cell.note("astra_reported", rc=astra_report["rc"], stderr=astra_report["stderr"][-500:])
            first = finish(pass1, 600)
            state_after_1 = {n: retro.read(run_root / n, None) for n in (
                "failure.json", "failure-delivery.json", "failure-notice-error.json", "delivery.json")}
            report_after_1 = _bart_texts(cell, "REPORT daily-retro ready")
            notice_after_1 = _bart_texts(cell, "REPORT daily-retro failure")
            second = finish(_retro(cell, cfg, "run", "--on-demand"), 600)
            state_after_2 = {n: retro.read(run_root / n, None) for n in (
                "failure.json", "failure-delivery.json", "failure-notice-error.json", "delivery.json")}
            report_total = _bart_texts(cell, "REPORT daily-retro ready")
            notice_total = _bart_texts(cell, "REPORT daily-retro failure")
            spawns = cell.sql("SELECT session_name, idempotency_key FROM v2_stream_reservations "
                              "UNION ALL SELECT session_name, idempotency_key FROM v2_spawn_outcomes")
            retro_spawns = [r for r in spawns if str(r.get("idempotency_key") or "").startswith("daily-retro-")]
            record.update(stop=stop, pass1=first, pass2=second, astra_report=astra_report,
                          state_after_pass1=state_after_1, state_after_pass2=state_after_2,
                          bart_report_after_pass1=report_after_1, bart_notice_after_pass1=notice_after_1,
                          bart_report_total=report_total, bart_notice_total=notice_total,
                          retro_spawns=retro_spawns, run_id=run_id)
            failure = state_after_1["failure.json"] or {}
            primary_masked = bool(first["rc"] not in (0,) and "ConnectionRefused" in first["stderr"].strip().splitlines()[-1:][0]
                                  if first["stderr"].strip() else False) and "ConnectionRefused" not in str(failure.get("error"))
            checks: dict[str, bool] = {
                "no_respawn": not retro_spawns,
                "no_sol_rerun": (run_root / "sol.json").read_text().count('"packet"') == sol_before.count('"packet"')
                    and json.loads((run_root / "sol.json").read_text()).get("packet_hash")
                    == json.loads(sol_before).get("packet_hash"),
                "report_delivered_once": report_total == 1,
                "cleanup_never_masks_primary": not primary_masked,
            }
            if stay_down:
                pending = (state_after_1["failure-delivery.json"] or {})
                checks.update({
                    "failure_persists_run_stage_error": all(k in failure for k in ("run_id", "stage", "error")),
                    "pending_notice_persisted_while_down": bool(pending.get("attempts")) or failure.get("notice") in {"pending", "queued"},
                    "notice_delivered_once_next_pass": notice_after_1 == 0 and notice_total == 1,
                })
            else:
                checks.update({
                    "pass_survives_outage": first["rc"] == 0 and report_after_1 == 1,
                    "no_failure_notice": notice_total == 0,
                })
            record["checks"] = checks
    except Exception as exc:  # noqa: BLE001
        import traceback
        record.update(classification=HARNESS_ERROR, detail=f"{type(exc).__name__}: {exc}",
                      traceback=traceback.format_exc()[-3000:])
    return _close_client_record(record, root, evidence)
