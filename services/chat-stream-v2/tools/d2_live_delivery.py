"""D2 live delivery proof using the supported authenticated LiveWindow API only."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import plistlib
import sqlite3
import subprocess
import sys
import time
import traceback
import uuid


WORKTREE = Path(__file__).resolve().parents[3]
SERVICE = WORKTREE / "services" / "chat-stream-v2"
for import_root in (WORKTREE / "services", WORKTREE / "services" / "agent-orch", SERVICE, SERVICE / "tools"):
    sys.path.insert(0, str(import_root))

from agent_orch.config import load_config
from agent_orch.wsclient import _one_shot_rpc, _stream_token_from_env
from live_window import LiveWindow, authenticated_operator_connection, write_receipt
from machines import load_machines


LIVE_DB = Path(os.environ.get("PENTACLE_SESSION_DB", str(Path.home() / ".local/share/pentacle-stream/sessions.db"))).expanduser()
LIVE_HOST = os.environ.get("PENTACLE_HOST_ID", "hosta")
LIVE_TOKEN = Path.home() / ".config" / "pentacle-stream" / "token"
LIVE_TMUX = "/opt/homebrew/bin/tmux"


def _redact(value):
    if isinstance(value, dict):
        return {key: "<redacted>" if key in {"token", "stream_token", "auth_v2", "push_secret"} else _redact(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _utc(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class D2Journey:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.run_id = f"d2-live-{uuid.uuid4().hex[:12]}"
        self.out = Path(args.artifact_root) / self.run_id
        self.out.mkdir(mode=0o700)
        self.cfg = load_config()
        self.owned = {}
        self.tokens = {}
        self.busy_observations = {}
        self.foreign_inventory_before = None
        self.sequence = 0
        self.limit = time.monotonic() + args.deadline_seconds
        self.result = {
            "run_id": self.run_id,
            "verdict": "RUNNING",
            "started_at": time.time(),
            "candidate_sha": args.candidate_sha,
            "helper_sha": args.candidate_sha,
            "runtime": {"expected_sha": getattr(args, "runtime_sha", None) or args.candidate_sha, "pid": args.pid},
            "preconditions": {
                "gate_tell": args.gate_tell,
                "gate_ledger_row": args.gate_ledger_row,
                "ci_run": args.ci_run,
                "qa_report": args.qa_report,
                "qa_digest": args.qa_digest,
                "legacy_mode": False,
                "close_mode": "generation-CAS",
            },
            "checks": {},
            "failures": [],
            "mutation_targets": [],
            "actors": self.owned,
        }
        self._save_progress()

    def _save_progress(self) -> None:
        (self.out / "progress.json").write_text(json.dumps(_redact(self.result), indent=2, sort_keys=True) + "\n")

    def _event(self, event: str, value) -> None:
        with (self.out / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": time.time(), "event": event, "value": _redact(value)}, sort_keys=True, default=str) + "\n")

    def _check(self, name: str, value=True) -> None:
        self.result["checks"][name] = value
        self._event("check", {name: value})
        self._save_progress()

    @staticmethod
    def _foreign_inventory_projection(rows: list[dict]) -> list[dict]:
        """Keep a durable, privacy-sanitized identity projection, not volatile state."""
        projection = []
        for row in rows:
            identity = "|".join(str(row.get(key) or "") for key in ("stream_id", "host", "session_name", "session_generation"))
            projection.append({"identity_sha256": hashlib.sha256(identity.encode()).hexdigest()})
        return sorted(projection, key=lambda item: item["identity_sha256"])

    def _sql(self, query: str, args=()):
        with closing(sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(query, args)]

    def _operator(self, payload: dict, timeout: float = 90):
        if payload["type"] not in {"list_sessions", "spawn_catalog_get"}:
            target = payload.get("to_stream_id")
            if payload["type"] != "tell" or target not in self.owned:
                raise RuntimeError("refusing foreign or unsupported operator mutation")
            self.result["mutation_targets"].append(target)
            self._event("mutation", {"type": payload["type"], "target": target})
        request = {**payload, "request_id": str(payload.get("request_id") or uuid.uuid4())}
        with authenticated_operator_connection(self.args.url, LIVE_TOKEN, timeout) as operator:
            snapshot = operator.snapshot
            reply = operator.rpc(request)
        self._event("operator", {"request_type": request["type"], "reply_type": reply.get("type")})
        return reply, snapshot

    def _wait(self, label: str, predicate, seconds: float):
        end = min(time.monotonic() + seconds, self.limit)
        while time.monotonic() < end:
            value = predicate()
            if value:
                return value
            time.sleep(2)
        raise TimeoutError(label)

    def _token_path(self, stream_id: str) -> Path:
        if stream_id in self.tokens:
            return self.tokens[stream_id]
        env = plistlib.loads((Path.home() / "Library/LaunchAgents/com.pentacle.chat-streamd-v2.plist").read_bytes()).get("EnvironmentVariables", {})
        machines = load_machines(env)
        cwd = next((Path(machine.cwd) for machine in machines if machine.name == LIVE_HOST), Path.home() / "agent-workspace")
        path = cwd / ".pentacle-stream-tokens" / (hashlib.sha256(stream_id.encode()).hexdigest()[:24] + ".token")
        row = self._row(stream_id)
        if not path.is_file() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
            raise RuntimeError(f"owned actor token is not private: {stream_id}")
        if not row or hashlib.sha256(path.read_text().strip().encode()).hexdigest() != row["token_hash"]:
            raise RuntimeError(f"owned actor token hash does not bind live row: {stream_id}")
        self.tokens[stream_id] = path
        return path

    def _actor(self, stream_id: str, payload: dict, timeout: float = 90):
        if stream_id not in self.owned:
            raise RuntimeError(f"refusing action for unowned actor: {stream_id}")
        self.result["mutation_targets"].append(stream_id)
        self._event("actor_request", {"type": payload["type"], "target": stream_id})
        old_token = os.environ.get("AGENT_ORCH_STREAM_TOKEN_FILE")
        try:
            os.environ["AGENT_ORCH_STREAM_TOKEN_FILE"] = str(self._token_path(stream_id))
            body = {**payload, "from_stream_id": stream_id, "request_id": str(payload.get("request_id") or uuid.uuid4())}
            body["stream_token"] = _stream_token_from_env()
            reply = asyncio.run(_one_shot_rpc(self.cfg, body, prefix=body["type"].split(".")[0], from_stream_id=stream_id, timeout=timeout))
            if not str(reply.get("type", "")).endswith(".ok"):
                raise RuntimeError(f"owned actor operation rejected: {_redact(reply)!r}")
            self._event("actor", {"actor": stream_id, "request_type": body["type"], "reply_type": reply.get("type")})
            return reply
        finally:
            if old_token is None:
                os.environ.pop("AGENT_ORCH_STREAM_TOKEN_FILE", None)
            else:
                os.environ["AGENT_ORCH_STREAM_TOKEN_FILE"] = old_token

    def _row(self, stream_id: str):
        host, name = stream_id.split(":", 1)
        rows = self._sql("SELECT s.*, g.generation AS session_generation FROM sessions s JOIN v2_session_generations g USING(host, session_name) WHERE host=? AND session_name=?", (host, name))
        return rows[0] if rows else None

    def _spawn(self, window: LiveWindow, catalog: dict, label: str, provider: str, parent: str | None = None):
        model, effort = catalog["profiles"]["desktop_manual"][provider]
        idempotency_key = uuid.uuid4().hex
        name = f"d2-live-{label}-{idempotency_key[:12]}"
        payload = {
            "type": "spawn", "host": LIVE_HOST, "session_name": name, "provider": provider,
            "model": model, "effort": effort, "visibility": "hidden", "schema": "SpawnRequestV2",
            "spawn_profile": "desktop_manual", "catalog_version": catalog["catalog_version"],
            "resolution_source": "explicit_override", "request_id": str(uuid.uuid4()),
            "idempotency_key": idempotency_key, "objective_supported": True,
            "objective": f"D2 live delivery actor {label}", "spec_id": os.environ.get("PENTACLE_SPEC_ID", "spec_pentacle__example"),
            "parent_stream_id": parent or self.args.parent_stream_id, "no_watch": parent is None,
            "self_close_on_completion": False,
            "initial_prompt": (
                "You are a bounded D2 acceptance actor. Reply READY once, then wait quietly. "
                "Do not close yourself, spawn children, send reports, or modify projects. "
                "On a D2 BUSY instruction run only its exact bounded command and reply DONE after it finishes."
            ),
        }
        owned = window.spawn(payload)
        self.owned[owned.stream_id] = {
            "label": label, "provider": provider, "request_id": owned.request_id,
            "idempotency_key": owned.idempotency_key, "stream_id": owned.stream_id,
            "generation": owned.session_generation, "legacy_unique_name": owned.legacy_unique_name,
        }
        self._event("spawn", self.owned[owned.stream_id])
        self._save_progress()
        return owned

    def _default(self, parent: str, child: str):
        watches = self._actor(parent, {"type": "watch.list"})["watches"]
        rows = [row for row in watches if row.get("default") and row.get("child_stream_id") == child]
        if len(rows) != 1 or set(rows[0].get("triggers", [])) != {"end", "blocker", "idle"}:
            raise AssertionError(f"default watch mismatch for {parent}/{child}: {rows!r}")
        return rows[0]

    def _subscription(self, key: str):
        rows = self._sql("SELECT * FROM v2_watch_wake WHERE id=?", (key,))
        if not rows:
            return None
        rows[0]["data"] = json.loads(rows[0]["data"])
        return rows[0]

    def _notice(self, key: str):
        rows = self._sql("SELECT * FROM v2_outbound_notices WHERE notice_id=?", (key,))
        return rows[0] if rows else None

    def _prove_notice(self, key: str, label: str, seconds: float = 180):
        def delivered():
            row = self._notice(key)
            if row and row["recipient_stream_id"] in self.busy_observations:
                self._observe_busy(row["recipient_stream_id"])
            if row and row.get("terminal_at"):
                raise AssertionError(f"terminal notice: {key}")
            return row if row and row.get("delivered_at") else None
        row = self._wait(label, delivered, seconds)
        backoff_until = _utc(row["delivered_at"]) + self.args.proof_backoff_seconds
        if time.time() < backoff_until:
            time.sleep(backoff_until - time.time())
        durable = self._notice(key)
        if not durable or not durable.get("delivered_at"):
            raise AssertionError(f"delivery did not survive proof backoff: {key}")
        value = {"notice_id": key, "kind": durable["kind"], "enqueued_at": _utc(durable["created_at"]), "proof_at": time.time(), "proof_backoff_s": self.args.proof_backoff_seconds}
        self._check(label, value)
        return durable

    def _prove_consumed(
        self, subscription_id: str, trigger: str, label: str, *, delivery_seconds: float = 180,
    ):
        self._wait(label + " subscription", lambda: self._subscription(subscription_id), 180)
        consumed = self._wait(label + " trigger", lambda: (self._subscription(subscription_id) or {"data": {}})["data"].get("consumed", {}).get(trigger), 180)
        return self._prove_notice(consumed["notice_id"], label, delivery_seconds)

    def _wake(self, stream_id: str, urgent: bool = False):
        request = f"{self.run_id}-wake-{uuid.uuid4().hex[:8]}"
        body = {"type": "wake.register", "request_id": request, "in": "5s", "note": request, "urgent": urgent}
        first = self._actor(stream_id, body)["wake"]
        second = self._actor(stream_id, body)["wake"]
        if first["id"] != second["id"]:
            raise AssertionError("wake idempotency failed")
        return first

    def _report(self, stream_id: str, status: str):
        self.sequence += 1
        report_id = f"{self.run_id}-{status}-{self.sequence}"
        body = {"type": "report", "report_id": report_id, "msg_id": self.sequence, "status": status,
                "summary": "D2 owned live report", "findings": [], "next_action": "wait for cleanup", "reason": "controlled D2 proof"}
        self._actor(stream_id, body)
        self._actor(stream_id, body)
        if status == "progress":
            return None
        return self._observe(f"report_{status}_{stream_id}", lambda: self._report_proof(report_id, status, stream_id))

    def _report_proof(self, report_id, status, stream_id):
        rows = self._wait("report notice", lambda: self._sql("SELECT * FROM v2_outbound_notices WHERE json_extract(metadata, '$.report_id')=?", (report_id,)), 90)
        if len(rows) != 1:
            raise AssertionError(f"report did not coalesce: {report_id}")
        return self._prove_notice(rows[0]["notice_id"], f"report_{status}_{stream_id}")

    def _busy(self, stream_id: str, label: str):
        start, end = self.out / f"{label}.start", self.out / f"{label}.end"
        command = f"import time; from pathlib import Path; Path({str(start)!r}).write_text(str(time.time())); time.sleep(180); Path({str(end)!r}).write_text(str(time.time()))"
        message = "D2 BUSY: run this exact bounded command once with your shell tool, then reply DONE only after it finishes. A notice must not abandon the pending command. " + shlex_join([sys.executable, "-c", command])
        reply, _ = self._operator({"type": "tell", "stream_id": stream_id, "to_stream_id": stream_id, "tell_id": f"{self.run_id}-{label}", "message": message}, 120)
        if reply.get("type") != "tell.ok":
            raise RuntimeError(f"busy tell rejected: {_redact(reply)!r}")
        self._observe("busy_start_" + label, lambda: self._wait("busy start " + label, start.exists, 120))
        def working():
            rows, _ = self._operator({"type": "list_sessions"})
            row = next((item for item in rows.get("active", []) if item.get("stream_id") == stream_id), None)
            if row:
                self._event("busy_poll", {"stream_id": stream_id, "working": row.get("working")})
            return row if row and row.get("working") is True and not end.exists() else None
        self._observe("busy_observed_" + label, lambda: self._wait("busy observed " + label, working, 45))
        self.busy_observations[stream_id] = []
        self._observe("initial_busy_" + label, lambda: self._observe_busy(stream_id))
        return start, end

    def _observe_busy(self, stream_id: str):
        rows, _ = self._operator({"type": "list_sessions"})
        row = next(item for item in rows.get("active", []) if item.get("stream_id") == stream_id)
        observation = {"at": time.time(), "working": row.get("working"), "genuine_activity_at": row.get("genuine_activity_at"), "observer_source": row.get("observer_source")}
        self.busy_observations[stream_id].append(observation)
        return observation

    def _prove_busy_delivery(
        self, stream_id: str, start: Path, end: Path, notice: dict, label: str,
        *, require_queue_drain: bool = False, require_during_busy: bool = False,
    ):
        overlap = [item for item in self.busy_observations[stream_id] if item["working"] and _utc(notice["created_at"]) <= item["at"] <= time.time()]
        if not overlap:
            raise AssertionError(f"delivery polling did not observe the controlled busy actor: {stream_id}")
        started_at = float(start.read_text())
        ended_at = float(end.read_text()) if end.exists() else None
        delivered_at = _utc(notice["delivered_at"])
        if delivered_at < started_at:
            raise AssertionError("notice delivered before controlled work")
        if require_queue_drain and (ended_at is None or delivered_at < ended_at):
            raise AssertionError(f"nonurgent notice did not remain queued through controlled work: {stream_id}")
        if require_during_busy and ended_at is not None and delivered_at >= ended_at:
            raise AssertionError(f"urgent notice did not deliver during controlled work: {stream_id}")
        self._check(label, {"queued_nonurgent": notice["kind"] == "wake", "busy_overlap": overlap, "delivery_polled": True, "delivery_after_busy": ended_at is not None and delivered_at >= ended_at, "started_at": started_at, "ended_at": ended_at, "delivered_at": delivered_at, "proof_at": time.time()})

    def _runtime_identity(self, phase: str):
        command = subprocess.check_output(["ps", "-p", str(self.args.pid), "-o", "command="], text=True).strip()
        live_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.args.runtime_checkout, text=True).strip()
        self._check("runtime_" + phase, {"pid": self.args.pid, "command": command, "head": live_head})
        expected_sha = getattr(self.args, "runtime_sha", None) or self.args.candidate_sha
        if expected_sha != live_head or "/services/chat-stream-v2/main.py" not in command:
            raise RuntimeError(f"live identity drift at {phase}")

    def run(self):
        try:
            if subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=WORKTREE, text=True).strip() != self.args.candidate_sha:
                raise RuntimeError("candidate worktree identity drift")
            self._runtime_identity("before")
            inventory, snapshot = self._operator({"type": "list_sessions"})
            if snapshot is None or snapshot.get("type") != "snapshot":
                raise RuntimeError("nonce-proof authenticated snapshot missing")
            self.foreign_inventory_before = self._foreign_inventory_projection(inventory.get("active", []))
            baseline = json.dumps(self.foreign_inventory_before, sort_keys=True, separators=(",", ":"))
            self._check("pre_d2_manifest", {"nonce_proof": "token-file nonce handshake", "snapshot_capability": snapshot.get("capabilities"), "foreign_inventory_sha256": hashlib.sha256(baseline.encode()).hexdigest(), "foreign_inventory_projection": self.foreign_inventory_before, "environment": {"PATH": os.environ.get("PATH"), "PYTHONNOUSERSITE": os.environ.get("PYTHONNOUSERSITE"), "executable": sys.executable, "cwd": os.getcwd()}})
            catalog, _ = self._operator({"type": "spawn_catalog_get"})
            if catalog.get("type") != "spawn_catalog_get.ok":
                raise RuntimeError(f"catalog unavailable: {_redact(catalog)!r}")
            receipt_path = self.out / "receipt.json"
            with LiveWindow(url=self.args.url, token_path=LIVE_TOKEN, timeout=90, state_path=self.out / "owned.json", receipt_path=receipt_path, session_db=LIVE_DB, tmux_bin=LIVE_TMUX, candidate_sha=self.args.candidate_sha, runtime_pid=self.args.pid, helper_path=Path(__file__), evidence_paths=[str(self.out / "events.jsonl"), str(self.out / "progress.json")], allow_legacy_close=False) as window:
                p1 = self._spawn(window, catalog, "p-codex", "codex")
                c1 = self._spawn(window, catalog, "c-claude", "claude", p1.stream_id)
                p2 = self._spawn(window, catalog, "p-claude", "claude")
                c2 = self._spawn(window, catalog, "c-codex", "codex", p2.stream_id)
                d1 = self._observe("default_watch_one", lambda: self._default(p1.stream_id, c1.stream_id))
                d2 = self._observe("default_watch_two", lambda: self._default(p2.stream_id, c2.stream_id))
                self._check("default_watches", {"pair_one": d1, "pair_two": d2})
                start, end = self._busy(p1.stream_id, "codex-nonurgent")
                codex_wake = self._wake(p1.stream_id)
                nonurgent = self._observe("codex_nonurgent", lambda: self._prove_consumed(codex_wake["id"], "wake", "codex_nonurgent", delivery_seconds=self.args.nonurgent_delivery_seconds))
                self._observe('self._prove_busy_delivery', lambda: self._prove_busy_delivery(p1.stream_id, start, end, nonurgent, "codex_busy_delivery"))
                self._observe('self._wait', lambda: self._wait("codex busy end", end.exists, 220))
                start, end = self._busy(p2.stream_id, "claude-nonurgent")
                claude_wake = self._wake(p2.stream_id)
                nonurgent = self._observe("claude_nonurgent", lambda: self._prove_consumed(claude_wake["id"], "wake", "claude_nonurgent", delivery_seconds=self.args.nonurgent_delivery_seconds))
                self._observe('self._prove_busy_delivery', lambda: self._prove_busy_delivery(p2.stream_id, start, end, nonurgent, "claude_busy_delivery", require_queue_drain=True))
                self._report(c2.stream_id, "error")
                self._observe('self._wait', lambda: self._wait("claude busy end", end.exists, 220))
                self._report(c2.stream_id, "progress")
                self._report(c2.stream_id, "done")
                urgent_start, _urgent_end = self._busy(p2.stream_id, "claude-urgent")
                urgent_wake = self._wake(p2.stream_id, urgent=True)
                urgent = self._observe("urgent_wake", lambda: self._prove_consumed(urgent_wake["id"], "wake", "urgent_wake"))
                self._observe("urgent_kind", lambda: self._require(urgent and urgent["kind"] == "wake_urgent", "urgent wake was not delivered as urgent"))
                self._observe('self._prove_busy_delivery', lambda: self._prove_busy_delivery(p2.stream_id, urgent_start, _urgent_end, urgent, "urgent_busy_delivery", require_during_busy=True))
                idle_fact = self._observe("default_idle_wait", lambda: self._wait("default idle >900", lambda: (self._subscription(d1["id"]) or {"data": {}})["data"].get("consumed", {}).get("idle"), 1100))
                idle_subscription = self._observe("idle_subscription", lambda: self._subscription(d1["id"]))
                self._observe("default_idle_threshold", lambda: self._require(idle_subscription and time.time() > idle_subscription["data"]["idle_since"] + 900, "default idle threshold was not continuous beyond 900 seconds"))
                self._observe("default_idle", lambda: self._prove_notice(idle_fact["notice_id"], "default_idle"))
                self._report(c1.stream_id, "done")
                for watch in filter(None, (d1, d2)):
                    self._observe("default_watch_consumed_" + watch["id"], lambda: self._require(self._subscription(watch["id"])["state"] == "consumed", "default watch not consumed"))
                self.result["terminal_notice_ids_before_teardown"] = [row["notice_id"] for row in self._sql("SELECT notice_id FROM v2_outbound_notices WHERE source_stream_id IN (?, ?) AND kind IN ('report', 'reconciler') ORDER BY notice_id", (c1.stream_id, c2.stream_id))]
                self._check("report_end_urgent_coalescence", {"reports": self.result["terminal_notice_ids_before_teardown"], "urgent_kind": (urgent or {}).get("kind")})
        except BaseException as exc:
            self._failure("journey", exc)
        finally:
            # Every independent readback runs even when admission, delivery or cleanup failed.
            self._observe("runtime_after", lambda: self._runtime_identity("after"))
            base = self._observe("library_receipt", self._read_library_receipt) or {}
            self._observe("terminal_episode", self._check_terminal_episode)
            self._observe("teardown_closed_expected", lambda: self._check_teardown(base))
            self._observe("generation_cas", lambda: self._check_cas(base))
            self._observe("foreign_inventory_unmutated_by_lane", lambda: self._check_foreign_inventory(base))
            self.result["verdict"] = "FAIL" if self.result["failures"] else "PASS"
            self.result["finished_at"] = time.time()
            self._save_progress()
            receipt = self.out / "receipt.json"
            write_receipt(receipt, candidate_sha=self.args.candidate_sha, runtime_pid=self.args.pid, helper_path=Path(__file__), evidence_paths=[str(self.out / "events.jsonl"), str(self.out / "progress.json")], payload={**base, "d2": _redact(self.result)})
            print(json.dumps({"verdict": self.result["verdict"], "receipt": str(receipt), "failures": self.result["failures"]}), flush=True)
        if self.result["failures"]:
            raise RuntimeError("D2 acceptance failed: " + ", ".join(item["check"] for item in self.result["failures"]))

    @staticmethod
    def _require(value, message):
        if not value:
            raise AssertionError(message)

    def _failure(self, name, exc):
        self.result["failures"].append({"check": name, "error": repr(exc), "traceback": traceback.format_exc()})
        self._save_progress()

    def _observe(self, name, predicate):
        try:
            return predicate()
        except Exception as exc:
            self._failure(name, exc)
            return None

    def _read_library_receipt(self):
        return json.loads((self.out / "receipt.json").read_text())

    def _check_terminal_episode(self):
        children = tuple(sid for sid, actor in self.owned.items() if actor["label"] in {"c-claude", "c-codex"})
        if len(children) != 2 or "terminal_notice_ids_before_teardown" not in self.result:
            raise AssertionError("terminal episode baseline unavailable")
        after = [row["notice_id"] for row in self._sql("SELECT notice_id FROM v2_outbound_notices WHERE source_stream_id IN (?, ?) AND kind IN ('report', 'reconciler') ORDER BY notice_id", children)]
        if after != self.result["terminal_notice_ids_before_teardown"]:
            raise AssertionError("library teardown duplicated terminal episode")
        self._check("terminal_episode", {"before": self.result["terminal_notice_ids_before_teardown"], "after": after})

    def _check_teardown(self, base):
        teardown = base.get("teardown", {})
        self._check("teardown_closed_expected", teardown)
        if teardown.get("closed") != 4 or teardown.get("expected") != 4 or teardown.get("failures"):
            raise AssertionError(f"library teardown did not prove 4/4: {teardown!r}")

    def _check_cas(self, base):
        proof = {"runtime_generation_close_supported": base.get("runtime_generation_close_supported"), "legacy_close_residual_race": base.get("legacy_close_residual_race")}
        self._check("generation_cas", proof)
        if proof["runtime_generation_close_supported"] is not True or proof["legacy_close_residual_race"] is not False:
            raise AssertionError("generation-CAS cleanup proof missing")

    def _check_foreign_inventory(self, base):
        after, _ = self._operator({"type": "list_sessions"})
        foreign = [row for row in after.get("active", []) if row.get("stream_id") not in self.owned]
        projection = self._foreign_inventory_projection(foreign)
        before = self.foreign_inventory_before
        targets = self.result["mutation_targets"] + [row["stream_id"] for row in base.get("sessions", [])]
        violations = sorted(set(targets) - set(self.owned))
        digest = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self._check("foreign_inventory_unmutated_by_lane", {
            "before": before, "after": projection,
            "before_sha256": digest(before), "after_sha256": digest(projection),
            "inventory_equal": before == projection, "passed": before is not None and not violations,
            "foreign_mutation_targets": violations,
            "provenance": "All helper mutations are ownership-guarded and logged; library close is registry/generation fenced. Inventory changes outside those targets are concurrent foreign activity.",
        })
        if before is None or violations:
            raise AssertionError(f"foreign inventory ownership proof failed: {violations}")


def shlex_join(parts):
    import shlex
    return shlex.join(parts)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--runtime-checkout", type=Path, default=Path.home() / "repos/pentacle-v2")
    parser.add_argument("--parent-stream-id", default=os.environ.get("AGENT_ORCH_STREAM_ID"))
    parser.add_argument("--url", default="ws://127.0.0.1:7791")
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--candidate-sha", required=True, help="Exact helper checkout SHA")
    parser.add_argument("--runtime-sha", help="Held runtime SHA; defaults to candidate SHA")
    parser.add_argument("--gate-tell", required=True)
    parser.add_argument("--gate-ledger-row", required=True)
    parser.add_argument("--ci-run", required=True)
    parser.add_argument("--qa-report", required=True)
    parser.add_argument("--qa-digest", required=True)
    parser.add_argument("--deadline-seconds", type=int, default=1800)
    parser.add_argument("--proof-backoff-seconds", type=int, default=60)
    parser.add_argument("--nonurgent-delivery-seconds", type=int, default=330)
    args = parser.parse_args()
    if not args.parent_stream_id:
        parser.error("--parent-stream-id is required")
    if args.proof_backoff_seconds < 60:
        parser.error("proof backoff must be at least 60 seconds")
    D2Journey(args).run()


if __name__ == "__main__":
    main()
