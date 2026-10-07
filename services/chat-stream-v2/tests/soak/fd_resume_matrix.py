"""Front-desk routing recovery after seat resume (Addendum A of
spec_pentacle__daemon_restart_continuity_2026_10).

A real `main.py` (restart_matrix's disposable daemon and stub Claude) with a
direct-primary `bart` composite bound to a front-desk (FD) seat. Each journey
kills the FD pane and restarts the daemon (a host logout), resumes the seat
through the real `agent-orch spawn --resume`, and drives the rebind through the
real CLI with the resumed generation's own stream token. Handoffs use the real
`agent-orch spawn --handoff`. Nothing writes the binding or sessions tables.

Every fixture records its `PENTACLE_ASSISTANT_ROLE` configuration: `"lead"`
(equal to the FD row role, so the row is protected) or unset.
"""
from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from tests.soak import restart_matrix as rm
from tests.soak.harness import LOCAL_HOST

RECOVERY_SPEC = "spec_pentacle__bart_fd_routing_recovery_2026_10"
RECOVERY_TOPIC = "pentacle__bart_fd_routing_recovery_2026_10"
COMPOSITE = "soakchat:assistant"
DAFF_COMPOSITE = "soakchat:daff"
FD_ROLE = "lead"
DAFF_ROLE = "daff-assistant"
PASS, PRODUCT_FAIL, HARNESS_ERROR = rm.PASS, rm.PRODUCT_FAIL, rm.HARNESS_ERROR


def sid(name: str) -> str:
    return f"{LOCAL_HOST}:{name}"


def write_memory(root: Path) -> Path:
    """Minimal live work tree so `--spec-id` resolves to the recovery spec."""
    memory = root / "memory"
    work = memory / "work"
    folder = work / "backlog" / RECOVERY_TOPIC
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "spec.md").write_text(
        f"---\nid: {RECOVERY_SPEC}\ntitle: FD routing recovery\nstatus: backlog\n---\n", encoding="utf-8")
    statuses = [{"name": n, "order": i, "display_label": n, "is_terminal": n in {"completed", "deprecated"}}
                for i, n in enumerate(("backlog", "ready_for_dev", "in_progress", "completed", "deprecated"), 1)]
    (work / "statuses.json").write_text(json.dumps({"version": 2, "statuses": statuses}), encoding="utf-8")
    return memory


class FdCell:
    """One disposable daemon plus operator credential and memory root."""

    def __init__(self, cell: rm.Cell, *, assistant_role: str | None) -> None:
        self.cell = cell
        self.assistant_role = assistant_role
        sys.path.insert(0, str(rm.REPO_SERVICES))
        from _shared.operator_auth import OperatorCredentialRegistry  # noqa: E402
        registry = OperatorCredentialRegistry(cell.daemon.home / ".config/pentacle-stream/operator-credentials.json")
        registry.initialize()
        _, envelope = registry.issue("pentacle", label="fd resume matrix disposable operator")
        self.operator_token = cell.root / "operator-token"
        self.operator_token.write_text(envelope)
        # Seats are seeded before the protected role is configured: the daemon
        # refuses a parented seat in a protected role, so the actual FD shape
        # (lead, hidden, parented) can only be protected by a role configured
        # after its spawn. `configure_composites` applies the role.
        cell.daemon.extra_env.update({"PENTACLE_MEMORY_ROOT": str(write_memory(cell.root))})

    # transport ------------------------------------------------------------ #

    def op(self, payload: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
        from tools.live_window.core import authenticated_operator_connection
        payload = {"request_id": f"op-{uuid.uuid4().hex[:12]}", **payload}
        with authenticated_operator_connection(self.cell.daemon.url, self.operator_token, timeout) as conn:
            return conn.rpc(payload)

    def cli(self, as_name: str | None, *args: str, timeout: float = 200) -> dict[str, Any]:
        proc = rm.cli(self.cell, as_name, *args)
        try:
            out, err = proc.communicate(timeout=timeout)
            result: dict[str, Any] = {"rc": proc.returncode, "stdout": out, "stderr": err}
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            result = {"rc": "timeout", "stdout": out, "stderr": err}
        result["json"] = {}
        # A spawn reply is one JSON line that can exceed rm.finish's 4 KB tail.
        for line in reversed(result["stdout"].strip().splitlines()):
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                result["json"] = parsed
                break
        self.cell.note("cli", as_name=as_name, args=list(args), rc=result["rc"], json=result["json"],
                       stdout=result["stdout"][-600:], stderr=result["stderr"][-600:])
        return result

    # durable reads -------------------------------------------------------- #

    def row(self, name: str) -> dict[str, Any] | None:
        rows = self.cell.sql(
            "SELECT s.*, g.generation AS live_generation FROM sessions s LEFT JOIN v2_session_generations g "
            "ON g.host=s.host AND g.session_name=s.session_name WHERE s.host=? AND s.session_name=?",
            (LOCAL_HOST, name))
        return rows[0] if rows else None

    def generation(self, name: str) -> str:
        row = self.row(name) or {}
        return str(row.get("session_generation") or row.get("live_generation") or "")

    def binding_rows(self) -> list[dict[str, Any]]:
        return self.cell.sql("SELECT name, stream_id, generation, revision FROM v2_assistant_direct_binding")

    def binding(self) -> dict[str, Any]:
        return self.op({"type": "assistant.binding"})

    def audit(self) -> list[dict[str, Any]]:
        return self.cell.sql("SELECT request_id, actor_stream_id, actor_generation, outcome "
                             "FROM v2_assistant_rebind_audit ORDER BY audit_id")

    def lifecycle(self) -> list[dict[str, Any]]:
        return self.cell.sql("SELECT * FROM v2_lifecycle_manager")

    def tell_queue(self) -> list[dict[str, Any]]:
        return self.cell.sql("SELECT * FROM v2_assistant_composite_tell_queue")

    def held(self, text: str) -> list[dict[str, Any]]:
        """Front-desk digest holds (the live delivery of a peer tell to bart)."""
        return self.cell.sql("SELECT notice_id, kind, recipient_stream_id, created_at, delivered_at, terminal_at "
                             "FROM v2_outbound_notices WHERE body LIKE ?", (f"%{text}%",))

    def keep_db(self) -> None:
        """Copy the daemon DB into the evidence directory (sqlite online backup)."""
        import sqlite3
        src = sqlite3.connect(f"file:{self.cell.daemon.db}?mode=ro", uri=True, timeout=5)
        dst = sqlite3.connect(str(self.cell.evidence / "sessions.db"))
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()

    def routes(self) -> list[dict[str, Any]]:
        return self.cell.sql("SELECT input_identity, routing_state, route_target, dispatch_id "
                             "FROM v2_assistant_composite_routes ORDER BY rowid")

    # journey steps --------------------------------------------------------- #

    def seed(self, name: str, *, role: str = "worker", visibility: str = "default", parent: str | None = None,
             spec: str | None = None, prompt: bool = True) -> dict[str, Any]:
        """Spawn through the daemon (tuple path, stub Claude). A prompt makes the
        stub write the native transcript that `--resume` authenticates."""
        text = rm.unique_prompt(f"seed-{name}") if prompt else ""
        payload = rm.spawn_payload(name, text, f"seed-{name}-{uuid.uuid4().hex[:6]}")
        if not prompt:
            payload.pop("initial_prompt")
        payload.update(role=role, visibility=visibility)
        if parent:
            payload["parent_stream_id"] = sid(parent)
        if spec:
            payload["spec_id"] = spec
        reply = self.op(payload, timeout=90)
        self.cell.note("seed", name=name, reply_type=reply.get("type"), error=reply.get("error_code"))
        if reply.get("type") != "spawn.ok":
            raise RuntimeError(f"seed {name} failed: {json.dumps(reply)[:600]}")
        if prompt and not rm.wait_for(lambda: self.transcript(name), 30, 0.1):
            raise RuntimeError(f"seed {name}: no native transcript")
        return reply

    def transcript(self, name: str) -> Path | None:
        csid = str((self.row(name) or {}).get("claude_session_id") or "")
        path = self.cell.transcript_dir / f"{csid}.jsonl"
        return path if csid and path.exists() else None

    def configure_composites(self, fd: str, daff: str) -> None:
        """Bind bart to the FD and daff to its seat (env pairs), then restart."""
        self.cell.daemon.stop(signal.SIGTERM)
        if self.assistant_role:
            self.cell.daemon.extra_env["PENTACLE_ASSISTANT_ROLE"] = self.assistant_role
        self.cell.daemon.extra_env.update({
            "PENTACLE_ASSISTANT_DAFF_ROLE": DAFF_ROLE,
            "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
            "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": COMPOSITE,
            "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": sid(fd),
            "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": self.generation(fd),
            "PENTACLE_ASSISTANT_REBIND_AUTHORIZED_SPEC_IDS": json.dumps([RECOVERY_SPEC]),
            "PENTACLE_ASSISTANT_DAFF_COMPOSITE_ENABLED": "1",
            "PENTACLE_ASSISTANT_DAFF_COMPOSITE_STREAM_ID": DAFF_COMPOSITE,
            "PENTACLE_ASSISTANT_DAFF_DIRECT_PRIMARY_STREAM_ID": sid(daff),
            "PENTACLE_ASSISTANT_DAFF_DIRECT_PRIMARY_GENERATION": self.generation(daff),
        })
        self.cell.daemon.start()

    def logout(self, name: str, *, observed: Any = None, settle_s: float = 240) -> dict[str, Any]:
        """Host logout: the FD pane dies and the daemon restarts. Two dead-window
        phases follow: the pane is observed dead (`pane_status=pane_dead`), then
        the reconciler records the death (closed, or preserved-dead with
        `presumed_dead_at` when protected). `observed()` runs in the first phase."""
        self.cell.namespace.run("kill-session", "-t", f"={name}")
        gone = rm.wait_for(lambda: name not in self.cell.namespace.session_names(), 10, 0.05)
        stop = self.cell.daemon.stop(signal.SIGTERM)
        started = time.monotonic()
        self.cell.daemon.start()

        def state(row: dict[str, Any]) -> dict[str, Any]:
            return {k: row.get(k) for k in ("status", "pane_status", "presumed_dead_at", "closed_at", "close_kind")}

        seen = rm.wait_for(lambda: (self.row(name) or {}).get("pane_status") == "pane_dead"
                           or (self.row(name) or {}).get("closed_at"), 60, 0.1)
        observed_row = state(self.row(name) or {})
        observed_result = observed() if (observed is not None and seen) else None
        row = rm.wait_for(lambda: (lambda r: r if (r.get("presumed_dead_at") or r.get("closed_at")) else None)(
            self.row(name) or {}), settle_s, 0.25)
        record = {"pane_gone": bool(gone), "observed_dead": bool(seen), "observed_row": observed_row,
                  "death_recorded": row is not None, "recorded_row": state(row or {}),
                  "reconciled_after_s": round(time.monotonic() - started, 1) if row else None,
                  "observed_result": observed_result}
        self.cell.note("logout", name=name, stop=stop, **record)
        return record

    def resume(self, name: str) -> dict[str, Any]:
        csid = str((self.row(name) or {}).get("claude_session_id") or "")
        return self.cli(None, "spawn", "--provider", "claude", "--host", LOCAL_HOST, "--resume", csid,
                        "--timeout", "120")

    def rebind(self, as_name: str, target: str, generation: str, revision: int, request_id: str) -> dict[str, Any]:
        return self.cli(as_name, "assistant", "rebind", "--target", sid(target), "--generation", generation,
                        "--expected-revision", str(revision), "--request-id", request_id, timeout=60)

    def tell_composite(self, text: str) -> dict[str, Any]:
        return self.cli("peer", "tell", COMPOSITE, text, timeout=60)

    def composite_input(self, text: str) -> dict[str, Any]:
        host, name = COMPOSITE.split(":", 1)
        optimistic = f"in-{uuid.uuid4().hex[:10]}"
        reply = self.op({"type": "send", "host": host, "session_name": name, "text": text,
                         "optimistic_id": optimistic})
        self.cell.note("composite_input", text=text, optimistic_id=optimistic, reply=reply)
        return {"optimistic_id": optimistic, "reply": reply}

    def delivered(self, name: str, text: str) -> int:
        return sum(1 for item in self.cell.inputs(name) if text in item["text"])


def dead_window_traffic(fd: FdCell, phase: str) -> dict[str, Any]:
    """One composite tell and one composite input sent while the seat is dead."""
    tag = uuid.uuid4().hex[:6]
    tell = fd.tell_composite(f"dead-{phase} tell {tag}")
    sent = fd.composite_input(f"dead-{phase} input {tag}")
    accepted = (sent["reply"].get("assistant_composite") or {})
    return {"phase": phase, "tell_text": f"dead-{phase} tell {tag}", "input_text": f"dead-{phase} input {tag}",
            "tell": tell["json"], "tell_rc": tell["rc"],
            "input": {"optimistic_id": sent["optimistic_id"], "type": sent["reply"].get("type"),
                      "error_code": sent["reply"].get("error_code"),
                      "routing_state": accepted.get("routing_state"), "route_id": accepted.get("route_id")}}


def configured_spec_authorized(row: dict[str, Any]) -> bool:
    """The daemon's own authority predicate, evaluated on a persisted row."""
    sys.path.insert(0, str(rm.SERVICE_DIR))
    from store_assistant_binding import _configured_spec_authorized
    return _configured_spec_authorized(row, frozenset({RECOVERY_SPEC}))


def classify(record: dict[str, Any]) -> dict[str, Any]:
    if record.get("classification") == HARNESS_ERROR:
        return record
    checks = record.get("checks") or {}
    failed = [k for k, v in checks.items() if not v]
    record["failed_checks"] = failed
    record["classification"] = PASS if checks and not failed else PRODUCT_FAIL
    return record


def write_record(evidence: Path, records: list[dict[str, Any]], fd: "FdCell | None" = None) -> None:
    (evidence / "records.json").write_text(json.dumps(records, indent=1, default=str))
    if fd is not None:
        fd.keep_db()


def _err(result: dict[str, Any]) -> str:
    data = result.get("json") or {}
    return str(data.get("error_code") or data.get("code") or "") + " " + result.get("stderr", "")[-400:]


# --------------------------------------------------------------------------- #
# Journey 1: the bound FD itself is resumed (A0 / A1, then A2, A3)
# --------------------------------------------------------------------------- #


def run_self_resume(root: Path, evidence: Path, *, assistant_role: str | None) -> list[dict[str, Any]]:
    """A0 (role unset) or A1 (role == FD row role): top-level default FD bound
    directly, no recovery spec; logout; `spawn --resume`. Then A2 (unbound
    window) and A3 (self-rebind without the spec) on the resumed seat."""
    label = "A1" if assistant_role == FD_ROLE else "A0"
    records: list[dict[str, Any]] = []
    with rm.cell_env(root, evidence) as cell:
        fd = FdCell(cell, assistant_role=assistant_role)
        cell.daemon.start()
        fd.seed("fd", role=FD_ROLE)
        fd.seed("daff", role=DAFF_ROLE)
        fd.seed("peer", prompt=False)
        fd.configure_composites("fd", "daff")
        before = fd.binding()
        old_gen = fd.generation("fd")
        daff_before = [r for r in fd.binding_rows() if r["name"] == "daff"]
        out = fd.logout("fd", observed=lambda: dead_window_traffic(fd, "observed"))
        dead = dead_window_traffic(fd, "reconciled")
        queue_dead = fd.tell_queue()
        resumed = fd.resume("fd")
        row = fd.row("fd") or {}
        new_gen = fd.generation("fd")
        panes = [n for n in cell.namespace.session_names() if n == "fd"]
        resume_ok = resumed["rc"] == 0 and resumed["json"].get("type") == "spawn.ok"
        rec: dict[str, Any] = {
            "cell": label, "assistant_role": assistant_role or "<unset>", "fd_row_role": FD_ROLE,
            "binding_before": before, "old_generation": old_gen, "logout": out,
            "resume": {k: resumed[k] for k in ("rc", "json")}, "resume_stderr": resumed["stderr"][-600:],
            "row_after": {k: row.get(k) for k in ("status", "pane_status", "presumed_dead_at", "closed_at",
                                                    "parent_stream_id", "visibility", "role")},
            "new_generation": new_gen, "panes": panes,
        }
        rec["checks"] = {
            "death_recorded": out["death_recorded"],
            "resume_ok": resume_ok,
            "same_stream_id": resume_ok and resumed["json"].get("stream_id") == sid("fd"),
            "new_generation": bool(new_gen) and new_gen != old_gen,
            "one_open_row": row.get("status") == "open" and not row.get("closed_at"),
            "one_pane": len(panes) == 1,
        }
        if label == "A1":
            # Protected-row control: the actual FD is unprotected (Thoth plist has
            # no PENTACLE_ASSISTANT_ROLE), so the refusal is recorded, not fixed.
            rec["control"] = "protected_row_resume_refused"
            rec["checks"] = {
                "death_recorded": out["death_recorded"],
                "resume_refused_already_live":
                    resumed["json"].get("error_code") == "resume_session_already_live",
                "row_preserved_open": row.get("status") == "open" and bool(row.get("presumed_dead_at")),
                "generation_unchanged": new_gen == old_gen,
                "no_pane": not panes,
            }
            records.append(classify(rec))
            write_record(evidence, records, fd)
            return records
        records.append(classify(rec))
        if not resume_ok:
            write_record(evidence, records, fd)
            return records

        # A2: resumed but unbound; visible refusals, dead-window traffic queued.
        live_tell = fd.tell_composite(f"resumed-unbound tell {uuid.uuid4().hex[:6]}")
        live_input = fd.composite_input(f"resumed-unbound input {uuid.uuid4().hex[:6]}")
        time.sleep(1.0)
        a2 = {"cell": "A2", "assistant_role": assistant_role or "<unset>",
              "observed_phase": out["observed_result"], "reconciled_phase": dead,
              "tell_queue_dead_window": queue_dead,
              "live_tell": {k: live_tell[k] for k in ("rc", "json")}, "live_tell_stderr": live_tell["stderr"][-400:],
              "live_input": live_input["reply"], "routes": fd.routes()}
        a2["checks"] = {
            "reconciled_dead_tell_queued": dead["tell"].get("delivery_status") == "queued_unbound"
                and any(q["tell_id"] == dead["tell"].get("tell_id") for q in queue_dead),
            "reconciled_dead_input_queued": dead["input"].get("routing_state") == "queued",
            "resumed_unbound_tell_refused_visibly": live_tell["rc"] != 0
                and "assistant_direct_generation_conflict" in (live_tell["stdout"] + live_tell["stderr"]),
            "resumed_unbound_input_refused_visibly":
                live_input["reply"].get("error_code") == "assistant_direct_generation_conflict",
            "nothing_delivered_to_resumed_pane_unbound": fd.delivered("fd", "resumed-unbound") == 0,
        }
        records.append(classify(a2))

        # A3: the resumed FD has no recovery spec: self-rebind is refused.
        revision = int(fd.binding().get("revision") or 0)
        a3_try = fd.rebind("fd", "fd", new_gen, revision, f"bart-fd-recovery-fd-{new_gen}")
        a3 = {"cell": "A3", "assistant_role": assistant_role or "<unset>",
              "rebind": {k: a3_try[k] for k in ("rc", "json")}, "stderr": a3_try["stderr"][-400:],
              "binding_after": fd.binding()}
        a3["checks"] = {
            "self_rebind_unauthorized": a3_try["rc"] != 0
                and "assistant_rebind_unauthorized" in (a3_try["stdout"] + a3_try["stderr"]),
            "binding_unchanged": a3["binding_after"].get("generation") == old_gen,
            "daff_binding_unchanged": [r for r in fd.binding_rows() if r["name"] == "daff"] == daff_before,
        }
        records.append(classify(a3))
    write_record(evidence, records, fd)
    return records


# --------------------------------------------------------------------------- #
# A1b: the actual FD shape (role=lead, hidden, parented)
# --------------------------------------------------------------------------- #


def run_actual_fd_shape(root: Path, evidence: Path, *, assistant_role: str | None) -> list[dict[str, Any]]:
    """A1b: FD shaped like the readback (role=lead, hidden, parented), holding the
    recovery spec `spawn_explicit` so topology is the only disqualifier. Records
    the resume outcome and applies the per-branch predicate: on refusal, the
    preserved row fails configured-spec authorization (no resumed seat is
    asserted); on success, the resumed seat keeps that topology and its
    self-rebind stays unauthorized."""
    rec: dict[str, Any] = {"cell": "A1b", "assistant_role": assistant_role or "<unset>", "fd_row_role": FD_ROLE}
    with rm.cell_env(root, evidence) as cell:
        fd = FdCell(cell, assistant_role=assistant_role)
        cell.daemon.start()
        fd.seed("planner", prompt=False)
        fd.seed("fd", role=FD_ROLE, visibility="hidden", parent="planner", spec=RECOVERY_TOPIC)
        fd.seed("daff", role=DAFF_ROLE)
        fd.seed("peer", prompt=False)
        fd.configure_composites("fd", "daff")
        old_gen = fd.generation("fd")
        seeded = fd.row("fd") or {}
        rec["seeded_row"] = {k: seeded.get(k) for k in ("role", "visibility", "parent_stream_id",
                                                         "qualified_spec_ids", "spec_binding_provenance")}
        rec["logout"] = fd.logout("fd")
        resumed = fd.resume("fd")
        row = fd.row("fd") or {}
        rec["resume"] = {k: resumed[k] for k in ("rc", "json")}
        rec["resume_stderr"] = resumed["stderr"][-600:]
        refused = not (resumed["rc"] == 0 and resumed["json"].get("type") == "spawn.ok")
        rec["branch"] = "refusal" if refused else "success"
        rec["row_after"] = {k: row.get(k) for k in ("status", "pane_status", "presumed_dead_at", "closed_at",
                                                     "parent_stream_id", "visibility", "role", "session_generation")}
        authorized = configured_spec_authorized(row)
        rec["configured_spec_authorized"] = authorized
        if refused:
            rec["checks"] = {
                "outcome_recorded": bool(rec["resume"]["json"].get("error_code") or resumed["stderr"]),
                "preserved_row_topology_parented_hidden": bool(row.get("parent_stream_id"))
                    and row.get("visibility") == "hidden",
                "preserved_row_fails_configured_spec_authorization": authorized is False,
            }
        else:
            new_gen = fd.generation("fd")
            revision = int(fd.binding().get("revision") or 0)
            attempt = fd.rebind("fd", "fd", new_gen, revision, f"bart-fd-recovery-fd-{new_gen}")
            rec["rebind"] = {k: attempt[k] for k in ("rc", "json")}
            rec["checks"] = {
                "outcome_recorded": True,
                "resumed_seat_new_generation": bool(new_gen) and new_gen != old_gen,
                "resumed_seat_keeps_parented_hidden": row.get("parent_stream_id") == sid("planner")
                    and row.get("visibility") == "hidden",
                "resumed_seat_fails_configured_spec_authorization": authorized is False,
                "resumed_self_rebind_unauthorized": attempt["rc"] != 0
                    and "assistant_rebind_unauthorized" in (attempt["stdout"] + attempt["stderr"]),
            }
    records = [classify(rec)]
    write_record(evidence, records, fd)
    return records


# --------------------------------------------------------------------------- #
# Journey 2: D5 successor, logout, resume, configured-spec self-rebind (A4-A8)
# --------------------------------------------------------------------------- #

BOUNDARY = {
    # name: (seed kwargs, how the recovery spec reaches it)
    "ordinary": ({}, None),
    "child": ({"parent": "planner", "visibility": "hidden", "spec": RECOVERY_TOPIC}, "spawn_explicit"),
    "hiddenseat": ({"visibility": "hidden", "spec": RECOVERY_TOPIC}, "spawn_explicit"),
    "opv2": ({}, "operator_v2"),
}


def run_handoff_recovery(root: Path, evidence: Path, *, assistant_role: str | None,
                         variant: str = "spawn_explicit") -> list[dict[str, Any]]:
    """A4 with A2, A5, A6, A7 and A8. Start from a bound FD shaped like the
    actual one (lead, hidden, parented); protected handoff (`spawn --handoff
    --visibility default`, recovery spec `spawn_explicit` or `handoff_inherited`
    from a predecessor holding it); the successor binds with its handoff proof;
    logout; `spawn --resume`; self-rebind by configured spec with CAS."""
    role_label = assistant_role or "<unset>"
    records: list[dict[str, Any]] = []
    base = {"assistant_role": role_label, "variant": variant}
    with rm.cell_env(root, evidence) as cell:
        fd = FdCell(cell, assistant_role=assistant_role)
        cell.daemon.start()
        fd.seed("planner", prompt=False)
        fd.seed("fd0", role=FD_ROLE, visibility="hidden", parent="planner",
                spec=RECOVERY_TOPIC if variant == "handoff_inherited" else None)
        fd.seed("daff", role=DAFF_ROLE)
        fd.seed("peer", prompt=False)
        for name, (kwargs, _how) in BOUNDARY.items():
            fd.seed(name, prompt=False, **kwargs)
        attach = fd.op({"type": "session.spec_update", "action": "attach", "host": LOCAL_HOST,
                        "session_name": "opv2", "spec_id": RECOVERY_TOPIC})
        cell.note("operator_attach", reply=attach)
        fd.configure_composites("fd0", "daff")
        daff_before = [r for r in fd.binding_rows() if r["name"] == "daff"]

        # Pre-logout protected handoff to the D5 successor.
        brief = cell.root / "successor-brief.txt"
        brief.write_text(rm.unique_prompt("successor"))
        args = ["spawn", "--handoff", "--visibility", "default", "--initial-prompt-file", str(brief),
                "--timeout", "120"]
        if variant == "spawn_explicit":
            args[2:2] = ["--spec-id", RECOVERY_TOPIC]
        handoff = fd.cli("fd0", *args)
        succ_sid = str(handoff["json"].get("stream_id") or "")
        succ = succ_sid.partition(":")[2]
        srow = fd.row(succ) or {} if succ else {}
        provenance = json.loads(srow.get("spec_binding_provenance") or "[]") if srow else []
        qualified = json.loads(srow.get("qualified_spec_ids") or "[]") if srow else []
        prov = next((p for p in provenance if p.get("spec_id") == RECOVERY_SPEC), {})
        handoff_rec = {"cell": "A4-handoff", **base, "handoff": {k: handoff[k] for k in ("rc", "json")},
                       "handoff_stderr": handoff["stderr"][-600:], "successor_row": {
                           k: srow.get(k) for k in ("parent_stream_id", "visibility", "role",
                                                     "handoff_from_stream_id", "qualified_spec_ids",
                                                     "spec_binding_provenance")}}
        ok_handoff = handoff["rc"] == 0 and bool(succ) and bool(srow)
        handoff_rec["checks"] = {
            "handoff_ok": ok_handoff,
            "successor_top_level": ok_handoff and not srow.get("parent_stream_id"),
            "successor_visibility_default": srow.get("visibility") == "default",
            "successor_holds_recovery_spec": RECOVERY_SPEC in qualified,
            "provenance_expected": prov.get("provenance") == variant,
            "granting_principal_present": bool(str(prov.get("granting_principal") or "").strip()),
        }
        if ok_handoff:
            rev0 = int(fd.binding().get("revision") or 0)
            gen1 = fd.generation(succ)
            proof = fd.rebind(succ, succ, gen1, rev0, f"bart-fd-handoff-{succ_sid}-{gen1}")
            handoff_rec["proof_rebind"] = {k: proof[k] for k in ("rc", "json")}
            handoff_rec["checks"]["binding_moved_by_handoff_proof"] = proof["rc"] == 0 and \
                (fd.binding().get("stream_id") == succ_sid)
        records.append(classify(handoff_rec))
        if handoff_rec["classification"] != PASS:
            write_record(evidence, records, fd)
            return records

        lifecycle_before = fd.lifecycle()
        out = fd.logout(succ, observed=lambda: dead_window_traffic(fd, "observed"))
        dead = dead_window_traffic(fd, "reconciled")
        resumed = fd.resume(succ)
        gen2 = fd.generation(succ)
        resume_ok = resumed["rc"] == 0 and resumed["json"].get("type") == "spawn.ok"
        resume_rec = {"cell": "A4-resume", **base, "logout": out, "resume": {k: resumed[k] for k in ("rc", "json")},
                      "resume_stderr": resumed["stderr"][-600:], "generation_before": gen1, "generation_after": gen2}
        resume_rec["checks"] = {
            "death_recorded": out["death_recorded"],
            "resume_ok": resume_ok,
            "same_stream_id": resume_ok and resumed["json"].get("stream_id") == succ_sid,
            "new_generation": bool(gen2) and gen2 != gen1,
        }
        records.append(classify(resume_rec))
        if not resume_ok:
            write_record(evidence, records, fd)
            return records

        # A2 on this fixture: the resumed-unbound window refuses visibly.
        live_tell = fd.tell_composite(f"resumed-unbound tell {uuid.uuid4().hex[:6]}")
        live_input = fd.composite_input(f"resumed-unbound input {uuid.uuid4().hex[:6]}")
        a2 = {"cell": "A2", **base, "observed_phase": out["observed_result"], "reconciled_phase": dead,
              "live_tell": {k: live_tell[k] for k in ("rc", "json")}, "live_input": live_input["reply"]}
        a2["checks"] = {
            "reconciled_dead_tell_queued": dead["tell"].get("delivery_status") == "queued_unbound",
            "reconciled_dead_input_queued": dead["input"].get("routing_state") == "queued",
            "resumed_unbound_tell_refused_visibly": live_tell["rc"] != 0
                and "assistant_direct_generation_conflict" in (live_tell["stdout"] + live_tell["stderr"]),
            "resumed_unbound_input_refused_visibly":
                live_input["reply"].get("error_code") == "assistant_direct_generation_conflict",
        }
        records.append(classify(a2))

        # A4: runbook step 3 (read binding, CAS rebind to own stream+generation).
        before = fd.binding()
        rev = int(before.get("revision") or 0)
        rid = f"bart-fd-recovery-{succ_sid}-{gen2}"
        first = fd.rebind(succ, succ, gen2, rev, rid)
        after = fd.binding()
        audit = [a for a in fd.audit() if a["request_id"] == rid]
        a4 = {"cell": "A4", **base, "binding_before": before, "rebind": {k: first[k] for k in ("rc", "json")},
              "binding_after": after, "audit": audit}
        a4["checks"] = {
            "rebind_ok": first["rc"] == 0 and first["json"].get("type") == "assistant.rebind.ok",
            "revision_plus_one": int(after.get("revision") or -1) == rev + 1,
            "binding_reads_new_generation": after.get("stream_id") == succ_sid and after.get("generation") == gen2,
            "audit_row": len(audit) == 1 and audit[0]["outcome"] == "ok" and audit[0]["actor_generation"] == gen2,
        }
        records.append(classify(a4))

        # A5: the exact request again (a lost reply) replays the receipt.
        again = fd.rebind(succ, succ, gen2, rev, rid)
        a5 = {"cell": "A5", **base, "replay": {k: again[k] for k in ("rc", "json")}, "binding_after": fd.binding()}
        a5["checks"] = {
            "replay_ok_duplicate": again["rc"] == 0 and again["json"].get("duplicate") is True,
            "revision_unchanged": int(a5["binding_after"].get("revision") or -1) == rev + 1,
        }
        records.append(classify(a5))

        # A6: an older expected revision is refused and changes nothing.
        stale = fd.rebind(succ, succ, gen2, rev, f"{rid}-stale")
        a6 = {"cell": "A6", **base, "stale": {k: stale[k] for k in ("rc", "json")}, "binding_after": fd.binding()}
        a6["checks"] = {
            "stale_revision_refused": stale["rc"] != 0
                and "assistant_rebind_stale_revision" in (stale["stdout"] + stale["stderr"]),
            "binding_unchanged": a6["binding_after"].get("revision") == a5["binding_after"].get("revision"),
        }
        records.append(classify(a6))

        # A7: everything the dead window accepted delivers once after the rebind.
        # A live peer tell to bart is delivered into the front-desk digest hold
        # (persisted, no paste; see the live control), so a tell counts as
        # delivered once when exactly one hold for the successor carries it. A
        # composite input is pasted as a direct dispatch to the successor pane.
        phases = [p for p in (out["observed_result"], dead) if p]
        tells = [p["tell_text"] for p in phases if p["tell_rc"] == 0]
        inputs = [dead["input_text"]] if dead["input"].get("type") == "send.result" else []
        observed = out["observed_result"] or {}
        pre_reconcile = observed.get("input") or {}
        rm.wait_for(lambda: all(fd.delivered(succ, t) for t in inputs) and not fd.tell_queue(), 60, 0.25)
        holds = {t: [h for h in fd.held(t) if h["recipient_stream_id"] == succ_sid] for t in tells}
        routes = fd.routes()
        dispatch_ids = [r["dispatch_id"] for r in routes if r.get("dispatch_id")]
        a7 = {"cell": "A7", **base, "accepted_tells": tells, "accepted_inputs": inputs,
              "tell_holds": holds, "input_deliveries": {t: fd.delivered(succ, t) for t in inputs},
              "tell_queue_after": fd.tell_queue(), "routes": routes,
              "refused_in_dead_window": [p["input_text"] for p in phases
                                         if p["input"].get("type") != "send.result"]}
        # Documented residual (planner ruling 6493e2bb): an input dispatched in
        # the pre-reconcile window to the dead generation fails visibly
        # (pinned target_closed contract); it must never vanish silently.
        pre_route = next((r for r in fd.cell.sql(
            "SELECT routing_state, delivery_state, error_code FROM v2_assistant_composite_routes "
            "WHERE input_identity=?", (pre_reconcile.get("optimistic_id") or "",))), {})
        a7["pre_reconcile_input"] = {"text": observed.get("input_text"), "route": pre_route,
                                     "delivered": fd.delivered(succ, observed.get("input_text") or "\0")}
        a7["checks"] = {
            "accepted_tells_held_once_for_successor": all(len(h) == 1 for h in holds.values()),
            "reconciled_window_input_accepted": bool(inputs),
            "accepted_inputs_delivered_once": all(fd.delivered(succ, t) == 1 for t in inputs),
            "pre_reconcile_input_never_silent": not pre_reconcile or a7["pre_reconcile_input"]["delivered"] == 1
                or (pre_route.get("delivery_state") == "failed" and bool(pre_route.get("error_code"))),
            "tell_queue_drained": not a7["tell_queue_after"],
            "dispatch_ids_unique": len(dispatch_ids) == len(set(dispatch_ids)),
            "unbound_refusals_never_delivered": fd.delivered(succ, "resumed-unbound") == 0,
        }
        records.append(classify(a7))

        # A8: authority boundaries; Daff and the lifecycle grant untouched.
        rev_now = int(fd.binding().get("revision") or 0)
        boundary: dict[str, Any] = {}
        for name in BOUNDARY:
            gen = fd.generation(name)
            res = fd.rebind(name, name, gen, rev_now, f"a8-{name}-{gen}")
            row = fd.row(name) or {}
            boundary[name] = {"rc": res["rc"], "json": res["json"],
                              "row": {k: row.get(k) for k in ("parent_stream_id", "visibility",
                                                               "spec_binding_provenance")}}
        lifecycle_after = fd.lifecycle()
        a8 = {"cell": "A8", **base, "boundary": boundary, "lifecycle_before": lifecycle_before,
              "lifecycle_after": lifecycle_after, "daff_before": daff_before,
              "daff_after": [r for r in fd.binding_rows() if r["name"] == "daff"]}
        a8["checks"] = {
            **{f"{name}_refused": boundary[name]["rc"] != 0
               and "assistant_rebind_unauthorized" in json.dumps(boundary[name]["json"]) for name in BOUNDARY},
            "opv2_holds_operator_v2": "operator_v2" in str(boundary["opv2"]["row"]["spec_binding_provenance"]),
            "binding_still_successor": fd.binding().get("generation") == gen2,
            "daff_binding_unchanged": a8["daff_after"] == daff_before,
            "lifecycle_not_restored": not any(gen2 in json.dumps(r, default=str) for r in lifecycle_after),
        }
        records.append(classify(a8))
    write_record(evidence, records, fd)
    return records
