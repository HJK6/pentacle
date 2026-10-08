"""Restore of the direct-primary assistant's bound seat after a host logout.

Real `main.py` on a disposable daemon (`fd_resume_matrix.FdCell`): own state
dir, port and tmux namespace, stub Claude. The bound front-desk pane is killed
and the daemon restarted; nothing but the daemon acts afterwards unless a
journey says so. The holder has the deployed shape in every journey: role
`lead`, hidden, parented, no protected role configured, durable binding.

Explicitly run, never part of the unit gate:

    PENTACLE_FORCE_LIVE_DAEMON=1 python3 -m pytest tests/soak/test_assistant_auto_restore.py -q

Set FD_RESUME_EVIDENCE=<dir> to keep each journey's record and daemon log.
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from tests.soak import fd_resume_matrix as fm
from tests.soak import restart_matrix as rm

pytestmark = [pytest.mark.live_daemon, pytest.mark.timeout(2400)]

RESTORE_FLAG = "PENTACLE_ASSISTANT_AUTO_RESTORE"
INHIBIT = "assistant-auto-restore.inhibit"
#: A short reconcile interval keeps the secondary journeys to minutes; the
#: primary journey (R0) and its control run the daemon's default cadence.
FAST = {"PENTACLE_RECONCILER_INTERVAL_S": "3"}
PASSES_S = 15.0          # several fast reconcile passes
NOTICE = "Seat restored after a host restart"


@pytest.fixture
def evidence(tmp_path, request):
    root = os.environ.get("FD_RESUME_EVIDENCE")
    path = Path(root) / request.node.name if root else tmp_path / "evidence"
    path.mkdir(parents=True, exist_ok=True)
    return path


class Journey:
    """One disposable daemon with the deployed holder shape bound durably."""

    def __init__(self, cell: rm.Cell, *, enabled: bool, fast: bool = True,
                 env: dict[str, str] | None = None, rebinder: bool = False) -> None:
        self.cell = cell
        self.fd = fm.FdCell(cell, assistant_role=None)
        cell.daemon.extra_env["PENTACLE_FORCE_LIVE_DAEMON"] = "1"
        if fast:
            cell.daemon.extra_env.update(FAST)
        cell.daemon.start()
        fd = self.fd
        fd.seed("planner", prompt=False)
        fd.seed("seedholder")
        fd.seed("fd", role=fm.FD_ROLE, visibility="hidden", parent="planner")
        fd.seed("daff", role=fm.DAFF_ROLE)
        fd.seed("peer", prompt=False)
        if rebinder:
            # A top-level seat the configured-spec exception lets rebind itself:
            # the stand-in for an operator moving the assistant elsewhere.
            fd.seed("other", role=fm.FD_ROLE, spec=fm.RECOVERY_TOPIC)
        if enabled:
            cell.daemon.extra_env[RESTORE_FLAG] = "1"
        cell.daemon.extra_env.update(env or {})
        fd.configure_composites("seedholder", "daff")
        moved = fd.rebind("seedholder", "fd", fd.generation("fd"), int(fd.binding().get("revision") or 0),
                          f"seed-durable-{uuid.uuid4().hex[:6]}")
        self.before = fd.binding()
        if moved["rc"] != 0 or self.before.get("source") != "durable" or self.before.get("stream_id") != fm.sid("fd"):
            raise RuntimeError(f"harness: durable seed binding failed: {moved['json']}")
        self.seeded = fd.row("fd") or {}
        self.old_generation = fd.generation("fd")
        self.lifecycle_before = fd.lifecycle()

    # -- actions ---------------------------------------------------------- #

    @property
    def inhibit_file(self) -> Path:
        return Path(self.cell.daemon.db).with_name(INHIBIT)

    def kill_pane(self, name: str = "fd") -> None:
        self.cell.namespace.run("kill-session", "-t", f"={name}")
        if not rm.wait_for(lambda: name not in self.cell.namespace.session_names(), 10, 0.05):
            raise RuntimeError("harness: pane did not die")

    def restart(self, *, set_env: dict[str, str] | None = None, drop_env: tuple[str, ...] = ()) -> None:
        daemon = self.cell.daemon
        if daemon.alive():
            daemon.stop(signal.SIGTERM)
        else:
            daemon.proc = None
        for key in drop_env:
            daemon.extra_env.pop(key, None)
        daemon.extra_env.update(set_env or {})
        daemon.start()

    def logout(self, **env: Any) -> None:
        """Host logout: the bound pane dies and the daemon restarts."""
        self.kill_pane()
        self.restart(**env)

    def wait_daemon_exit(self, code: int, timeout: float = 400) -> bool:
        proc = self.cell.daemon.proc
        return bool(rm.wait_for(lambda: proc is not None and proc.poll() == code, timeout, 0.2))

    def operator(self, action: str, request_id: str) -> dict[str, Any]:
        return self.fd.op({"type": "assistant.restore", "action": action, "request_id": request_id}, timeout=60)

    # -- reads ------------------------------------------------------------ #

    def restore(self) -> dict[str, Any]:
        return self.fd.binding().get("restore") or {}

    def episodes(self) -> list[dict[str, Any]]:
        return self.cell.sql("SELECT * FROM v2_assistant_restore_episode ORDER BY episode_id")

    def audit(self, event: str | None = None) -> list[dict[str, Any]]:
        rows = self.cell.sql("SELECT * FROM v2_assistant_restore_audit ORDER BY audit_id")
        return [r for r in rows if event is None or r["event"] == event]

    def restore_rebinds(self) -> list[dict[str, Any]]:
        return [r for r in self.fd.audit() if r["actor_stream_id"] == "daemon:assistant-restore"]

    def wait_state(self, *states: str, timeout: float = 300) -> dict[str, Any]:
        rm.wait_for(lambda: self.restore().get("state") in states, timeout, 0.5)
        return self.restore()

    def wait_episode(self, state: str, timeout: float = 300) -> dict[str, Any] | None:
        return rm.wait_for(lambda: next((e for e in self.episodes() if e["state"] == state), None), timeout, 0.25)

    def notices(self, name: str) -> int:
        """Restoration notices that reached `name`: pasted into its pane, or held
        for it by the front-desk digest (the live delivery of a tell to the
        bound seat; a held tell counts as delivered)."""
        held = [h for h in self.fd.held(NOTICE) if h.get("recipient_stream_id") == fm.sid(name)]
        return self.fd.delivered(name, NOTICE) + len(held)

    def restored_checks(self, *, revision_step: int = 1) -> dict[str, bool]:
        fd = self.fd
        row = fd.row("fd") or {}
        binding = fd.binding()
        generation = fd.generation("fd")
        episode = (self.episodes() or [{}])[-1]
        return {
            "seat_reopened_new_generation": row.get("status") == "open" and bool(generation)
                and generation != self.old_generation,
            "generation_is_the_attempts_own": generation == episode.get("attempt_generation"),
            "same_claude_session": bool(self.seeded.get("claude_session_id"))
                and row.get("claude_session_id") == self.seeded.get("claude_session_id"),
            "topology_preserved": row.get("visibility") == "hidden"
                and row.get("parent_stream_id") == fm.sid("planner") and row.get("role") == fm.FD_ROLE,
            "exactly_one_pane": self.cell.panes("fd") == ["fd"],
            "binding_same_stream_new_generation": binding.get("stream_id") == fm.sid("fd")
                and binding.get("generation") == generation,
            "binding_revision_advanced_once": int(binding.get("revision") or 0)
                == int(self.before.get("revision") or 0) + revision_step,
            "one_ok_restore_rebind_audit": [r["outcome"] for r in self.restore_rebinds()] == ["ok"],
            "restore_state_restored": (binding.get("restore") or {}).get("state") == "restored",
        }

    def record(self, rec: dict[str, Any]) -> dict[str, Any]:
        rec.update(episodes=self.episodes(), restore_audit=self.audit(), rebind_audit=self.fd.audit(),
                   binding_before=self.before, binding_after=self.fd.binding(),
                   row_after={k: (self.fd.row("fd") or {}).get(k) for k in (
                       "status", "pane_status", "presumed_dead_at", "closed_at", "close_kind",
                       "claude_session_id", "role", "visibility", "parent_stream_id")},
                   panes=self.cell.panes("fd"))
        return rec


def finish(journey: Journey | None, evidence: Path, rec: dict[str, Any]) -> dict[str, Any]:
    if journey is not None:
        journey.record(rec)
    fm.classify(rec)
    fm.write_record(evidence, [rec], journey.fd if journey else None)
    return rec


def assert_pass(rec: dict[str, Any]) -> None:
    assert rec.get("classification") == fm.PASS, json.dumps(
        {k: rec.get(k) for k in ("cell", "classification", "failed_checks", "row_after", "binding_after", "notes")},
        default=str)


def composite_input_reaches(journey: Journey, name: str) -> tuple[str, bool]:
    text = f"post-restore input {uuid.uuid4().hex[:6]}"
    journey.fd.composite_input(text)
    return text, bool(rm.wait_for(lambda: journey.fd.delivered(name, text) >= 1, 60, 0.25))


# --------------------------------------------------------------------------- #
# R0 / R0-off: the primary journey and its control (default reconcile cadence)
# --------------------------------------------------------------------------- #


def test_logout_restores_same_seat_and_binding(tmp_path, evidence):
    rec: dict[str, Any] = {"cell": "R0"}
    with rm.cell_env(tmp_path / "env", evidence) as cell:
        j = Journey(cell, enabled=True, fast=False)
        rec["logout"] = j.fd.logout("fd")
        j.wait_state("restored", timeout=240)
        _text, delivered = composite_input_reaches(j, "fd")
        rec["checks"] = {
            **j.restored_checks(),
            "composite_input_reaches_restored_seat": delivered,
            "lifecycle_manager_rows_unchanged": j.fd.lifecycle() == j.lifecycle_before,
        }
        finish(j, evidence, rec)
    assert_pass(rec)


def _seed_grant(journey: Journey, revision: int = 5) -> None:
    """Stand in for the operator's designation: the grant row plus its consented audit row.

    The designation ceremony needs an operator device; the journey under test
    starts from an existing grant, so the fixture writes that starting state.
    """
    conn = sqlite3.connect(journey.cell.daemon.db, timeout=30)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO v2_lifecycle_manager(id,stream_id,session_generation,revision,updated_at) "
            "VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET stream_id=excluded.stream_id,"
            "session_generation=excluded.session_generation,revision=excluded.revision",
            (fm.sid("fd"), journey.old_generation, revision, time.time()))
        conn.execute(
            "INSERT INTO v2_lifecycle_authority_audit(action,actor_kind,actor_identity,target_stream_id,"
            "target_generation,old_revision,new_revision,result,consent_id,created_at) "
            "VALUES('designate','operator','operator-fixture',?,?,?,?,'applied','consent-soak-1',?)",
            (fm.sid("fd"), journey.old_generation, revision - 1, revision, time.time()))
        conn.commit()
    finally:
        conn.close()


def _manager_reparent(journey: Journey, as_name: str) -> dict[str, Any]:
    """A fleet reparent of a seat the caller does not own: lifecycle-manager only."""
    fd = journey.fd
    return fd.cli(as_name, "reparent", fm.sid("peer"), "--to", fm.sid("planner"),
                  "--expected-generation", fd.generation("peer"), "--reason", "restore grant journey",
                  timeout=60)


def test_logout_carries_the_lifecycle_grant_with_the_seat(tmp_path, evidence):
    """R11: the grant on the dead bound seat follows the automatic restore."""
    rec: dict[str, Any] = {"cell": "R11"}
    with rm.cell_env(tmp_path / "env", evidence) as cell:
        j = Journey(cell, enabled=True, fast=False)
        _seed_grant(j)
        before = j.fd.lifecycle()
        refused = _manager_reparent(j, "seedholder")
        rec["logout"] = j.fd.logout("fd")
        j.wait_state("restored", timeout=240)
        after = j.fd.lifecycle()
        generation = j.fd.generation("fd")
        episode = (j.episodes() or [{}])[-1]
        continuity = j.cell.sql(
            "SELECT actor_identity,actor_generation,target_generation,old_revision,new_revision,result,"
            "consent_id,request_id FROM v2_lifecycle_authority_audit WHERE action='restore_continuity'")
        allowed = _manager_reparent(j, "fd")
        rec["grant"] = {"before": before, "after": after, "continuity": continuity,
                        "refused": refused["json"], "allowed": allowed["json"]}
        rec["checks"] = {
            **j.restored_checks(),
            "seat_without_the_grant_is_refused": refused["rc"] != 0,
            "grant_moved_to_the_resumed_generation": len(after) == 1
                and after[0]["stream_id"] == fm.sid("fd")
                and after[0]["session_generation"] == generation != j.old_generation
                and int(after[0]["revision"]) == int(before[0]["revision"]) + 1,
            "one_continuity_audit_row_with_the_consent": [
                (r["actor_identity"], r["actor_generation"], r["target_generation"], r["old_revision"],
                 r["new_revision"], r["result"], r["consent_id"]) for r in continuity
            ] == [("daemon:assistant-restore", j.old_generation, generation, 5, 6, "applied", "consent-soak-1")],
            "episode_records_the_carry": episode.get("grant_carry") == "applied"
                and episode.get("grant_generation") == j.old_generation,
            "restored_seat_performs_a_manager_only_action": allowed["rc"] == 0,
        }
        finish(j, evidence, rec)
    assert_pass(rec)


def test_flag_off_keeps_manual_recovery(tmp_path, evidence):
    rec: dict[str, Any] = {"cell": "R0-off"}
    with rm.cell_env(tmp_path / "env", evidence) as cell:
        j = Journey(cell, enabled=False, fast=False)
        rec["logout"] = j.fd.logout("fd")
        time.sleep(30)
        after = j.fd.binding()
        rec["checks"] = {
            "no_pane": cell.panes("fd") == [],
            "binding_unchanged": after.get("generation") == j.before.get("generation")
                and after.get("revision") == j.before.get("revision"),
            "restore_state_disabled": (after.get("restore") or {}).get("state") == "disabled",
            "zero_episodes": j.episodes() == [],
        }
        finish(j, evidence, rec)
    assert_pass(rec)


# --------------------------------------------------------------------------- #
# R1, R2: no death proof, no spawn
# --------------------------------------------------------------------------- #


def test_daemon_only_restart_spawns_nothing(tmp_path, evidence):
    rec: dict[str, Any] = {"cell": "R1"}
    with rm.cell_env(tmp_path / "env", evidence) as cell:
        j = Journey(cell, enabled=True)
        j.restart()
        time.sleep(PASSES_S)
        after = j.fd.binding()
        rec["checks"] = {
            "zero_episodes": j.episodes() == [],
            "generation_unchanged": j.fd.generation("fd") == j.old_generation,
            "binding_unchanged": after.get("generation") == j.before.get("generation")
                and after.get("revision") == j.before.get("revision"),
            "one_pane": cell.panes("fd") == ["fd"],
            "restore_state_healthy": (after.get("restore") or {}).get("state") == "healthy",
        }
        finish(j, evidence, rec)
    assert_pass(rec)


def _fake_ps(root: Path, body: str) -> str:
    path = root / "fake-ps"
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)
    return str(path)


def _has_session_unreachable(cell: rm.Cell) -> None:
    """Make the daemon's `tmux has-session` fail like a lost transport (rc 2)."""
    wrapper = cell.daemon.tmux_bin
    script = wrapper.read_text().replace(
        "case \" $* \" in\n", "case \" $* \" in\n  *\" has-session \"*) exit 2 ;;\n", 1)
    wrapper.write_text(script)


@pytest.mark.parametrize("probe,reason", [
    ("tmux_unreachable", "pane_probe_unknown"),
    ("ps_exit_2", "process_probe_unknown"),
    ("ps_timeout", "process_probe_unknown"),
], ids=["R2a", "R2b", "R2c"])
def test_unknown_probe_spawns_nothing(probe, reason, tmp_path, evidence):
    rec: dict[str, Any] = {"cell": {"tmux_unreachable": "R2a", "ps_exit_2": "R2b", "ps_timeout": "R2c"}[probe]}
    with rm.cell_env(tmp_path / "env", evidence) as cell:
        j = Journey(cell, enabled=True)
        env = {}
        if probe == "ps_exit_2":
            env["PENTACLE_TEST_RESTORE_PS_BIN"] = _fake_ps(cell.root, "exit 2")
        elif probe == "ps_timeout":
            env["PENTACLE_TEST_RESTORE_PS_BIN"] = _fake_ps(cell.root, "sleep 30")
        j.kill_pane()
        if probe == "tmux_unreachable":
            _has_session_unreachable(cell)
        j.restart(set_env=env)
        status = j.wait_state("waiting_evidence", timeout=60)
        time.sleep(PASSES_S)
        rec["status"] = status
        rec["checks"] = {
            "waiting_evidence": status.get("state") == "waiting_evidence",
            "reason_names_the_probe": status.get("reason") == reason,
            "zero_episodes": j.episodes() == [],
            "no_pane": cell.panes("fd") == [],
            "binding_unchanged": j.fd.binding().get("generation") == j.before.get("generation"),
        }
        finish(j, evidence, rec)
    assert_pass(rec)


def test_reconciler_closed_row_with_a_revived_pane_spawns_nothing(tmp_path, evidence):
    rec: dict[str, Any] = {"cell": "R2d"}
    with rm.cell_env(tmp_path / "env", evidence) as cell:
        j = Journey(cell, enabled=True)
        j.inhibit_file.write_text("hold the restore until the reconciler has closed the row")
        j.logout()
        closed = rm.wait_for(lambda: (j.fd.row("fd") or {}).get("status") == "closed", 120, 0.25)
        cell.namespace.run("new-session", "-d", "-s", "fd", "sleep 600")
        revived = rm.wait_for(lambda: "fd" in cell.namespace.session_names(), 10, 0.05)
        j.inhibit_file.unlink()
        status = j.wait_state("ineligible", timeout=60)
        time.sleep(PASSES_S)
        rec["status"] = status
        rec["checks"] = {
            "row_was_reconciler_closed": bool(closed),
            "pane_revived": bool(revived),
            "state_ineligible": status.get("state") == "ineligible",
            "reason_holder_revived": status.get("reason") == "holder_revived",
            "zero_episodes": j.episodes() == [],
            "binding_unchanged": j.fd.binding().get("generation") == j.before.get("generation"),
        }
        finish(j, evidence, rec)
    assert_pass(rec)


def test_reused_pid_counts_as_gone(tmp_path, evidence):
    rec: dict[str, Any] = {"cell": "R2e"}
    with rm.cell_env(tmp_path / "env", evidence) as cell:
        j = Journey(cell, enabled=True)
        j.kill_pane()
        cell.daemon.stop(signal.SIGTERM)
        # The recorded pane pid now belongs to another live process (this test
        # run), which started at a different time.
        conn = sqlite3.connect(cell.daemon.db, timeout=5)
        try:
            raw = conn.execute("SELECT observer_binding FROM sessions WHERE session_name='fd'").fetchone()[0]
            observer = json.loads(raw)
            rec["recorded_observer"] = dict(observer)
            observer["pane_pid"] = str(os.getpid())
            conn.execute("UPDATE sessions SET observer_binding=? WHERE session_name='fd'", (json.dumps(observer),))
            conn.commit()
        finally:
            conn.close()
        j.restart()
        j.wait_state("restored", timeout=240)
        rec["checks"] = {"harness_pid_is_alive": os.getpid() > 0, **j.restored_checks()}
        finish(j, evidence, rec)
    assert_pass(rec)


# --------------------------------------------------------------------------- #
# R3a-d: a daemon crash at each effect boundary continues the same episode
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("point", ["before_spawn", "after_spawn", "after_bind", "during_routing"],
                         ids=["R3a", "R3b", "R3c", "R3d"])
def test_crash_continues_the_same_episode(point, tmp_path, evidence):
    rec: dict[str, Any] = {"cell": f"R3-{point}"}
    with rm.cell_env(tmp_path / "env", evidence) as cell:
        j = Journey(cell, enabled=True)
        try:
            j.logout(set_env={"PENTACLE_TEST_RESTORE_CRASH_AT": point})
        except RuntimeError as exc:
            # The restore can reach the crash point before the harness has
            # finished its own startup wait; that exit is the journey's subject.
            if "rc=86" not in str(exc):
                raise
        crashed = j.wait_daemon_exit(86)
        at_crash = (j.episodes() or [{}])[-1]
        panes_at_crash = cell.panes("fd")
        rec["at_crash"] = {k: at_crash.get(k) for k in ("state", "attempt_seq", "attempt_generation", "spawn_key")}
        j.restart(drop_env=("PENTACLE_TEST_RESTORE_CRASH_AT",))
        j.wait_state("restored", timeout=300)
        episode = (j.episodes() or [{}])[-1]
        rec["checks"] = {
            "daemon_crashed_at_the_hook": bool(crashed),
            **j.restored_checks(),
            "attempt_generation_recorded_before_the_crash": bool(at_crash.get("attempt_generation"))
                and j.fd.generation("fd") == at_crash.get("attempt_generation"),
            "one_attempt": episode.get("attempt_seq") == 1 and len(j.audit("attempt_started")) == 1,
            "one_bound_audit": len(j.audit("bound")) == 1,
            "notice_delivered_once": j.notices("fd") == 1,
            "one_episode": len(j.episodes()) == 1,
        }
        if point == "before_spawn":
            rec["checks"]["no_pane_existed_at_the_crash"] = panes_at_crash == []
        else:
            rec["checks"]["pane_from_before_the_crash_survives"] = panes_at_crash == ["fd"]
        finish(j, evidence, rec)
    assert_pass(rec)


# --------------------------------------------------------------------------- #
# R4, R3e: an operator rebind is never overwritten
# --------------------------------------------------------------------------- #


def test_operator_rebind_before_the_bind_wins(tmp_path, evidence):
    rec: dict[str, Any] = {"cell": "R4"}
    with rm.cell_env(tmp_path / "env", evidence) as cell:
        j = Journey(cell, enabled=True, rebinder=True)
        gate = cell.root / "release-before-spawn"
        j.logout(set_env={"PENTACLE_TEST_RESTORE_PAUSE_AT": "before_spawn",
                          "PENTACLE_TEST_RESTORE_PAUSE_FILE": str(gate)})
        paused = j.wait_episode("spawning", timeout=120)
        moved = j.fd.rebind("other", "other", j.fd.generation("other"),
                            int(j.fd.binding().get("revision") or 0), f"operator-move-{uuid.uuid4().hex[:6]}")
        operator_binding = j.fd.binding()
        gate.write_text("go")
        ended = j.wait_episode("superseded", timeout=300)
        after = j.fd.binding()
        row = j.fd.row("fd") or {}
        rec["operator_rebind"] = {k: moved[k] for k in ("rc", "json")}
        rec["checks"] = {
            "paused_after_the_pre_spawn_check": bool(paused),
            "operator_rebind_ok": moved["rc"] == 0 and operator_binding.get("stream_id") == fm.sid("other"),
            "binding_stays_on_the_operators_seat": after.get("stream_id") == fm.sid("other")
                and after.get("revision") == operator_binding.get("revision")
                and after.get("generation") == operator_binding.get("generation"),
            "episode_superseded_binding_moved": bool(ended) and ended.get("reason") == "binding_moved",
            "one_superseded_rebind_audit": [r["outcome"] for r in j.restore_rebinds()]
                == ["assistant_restore_superseded"],
            "resumed_seat_open_and_unbound": row.get("status") == "open" and cell.panes("fd") == ["fd"]
                and after.get("stream_id") != fm.sid("fd"),
        }
        finish(j, evidence, rec)
    assert_pass(rec)


def test_operator_rebind_after_the_bind_is_an_ordinary_rebind(tmp_path, evidence):
    rec: dict[str, Any] = {"cell": "R3e"}
    with rm.cell_env(tmp_path / "env", evidence) as cell:
        j = Journey(cell, enabled=True, rebinder=True)
        gate = cell.root / "release-after-bind"
        j.logout(set_env={"PENTACLE_TEST_RESTORE_PAUSE_AT": "after_bind",
                          "PENTACLE_TEST_RESTORE_PAUSE_FILE": str(gate)})
        bound = j.wait_episode("bound", timeout=300)
        restore_binding = j.fd.binding()
        # The restored front desk, now the bound owner, hands the assistant to N.
        moved = j.fd.rebind("fd", "other", j.fd.generation("other"),
                            int(restore_binding.get("revision") or 0), f"operator-move-{uuid.uuid4().hex[:6]}")
        text = f"queued input {uuid.uuid4().hex[:6]}"
        j.fd.composite_input(text)
        gate.write_text("go")
        ended = j.wait_episode("restored", timeout=120)
        input_on_other = bool(rm.wait_for(lambda: j.fd.delivered("other", text) >= 1, 60, 0.25))
        rm.wait_for(lambda: j.notices("other") >= 1, 30, 0.25)
        time.sleep(PASSES_S)
        after = j.fd.binding()
        row = j.fd.row("fd") or {}
        closes = [r for r in cell.sql("SELECT close_kind,closed_at FROM sessions WHERE session_name='fd'")
                  if r.get("closed_at")]
        rec["operator_rebind"] = {k: moved[k] for k in ("rc", "json")}
        rec["checks"] = {
            "paused_after_the_bind": bool(bound) and restore_binding.get("stream_id") == fm.sid("fd")
                and restore_binding.get("revision") == int(j.before.get("revision") or 0) + 1,
            "operator_rebind_ok": moved["rc"] == 0,
            "binding_stays_on_n_at_r_plus_2": after.get("stream_id") == fm.sid("other")
                and after.get("revision") == int(j.before.get("revision") or 0) + 2,
            "episode_restored": bool(ended),
            "notice_reaches_n_exactly_once": j.notices("other") == 1 and j.notices("fd") == 0,
            "queued_input_reaches_n_exactly_once": input_on_other and j.fd.delivered("other", text) == 1
                and j.fd.delivered("fd", text) == 0,
            "resumed_seat_open_and_unbound": row.get("status") == "open" and cell.panes("fd") == ["fd"],
            "no_restore_initiated_close": closes == [],
        }
        finish(j, evidence, rec)
    assert_pass(rec)


# --------------------------------------------------------------------------- #
# R5 + R9: the attempt budget survives restarts; one operator retry reopens it
# --------------------------------------------------------------------------- #


def test_budget_survives_restarts_then_one_retry_restores(tmp_path, evidence):
    rec: dict[str, Any] = {"cell": "R5+R9"}
    with rm.cell_env(tmp_path / "env", evidence) as cell:
        j = Journey(cell, enabled=True)
        cell.set_mode("exit_at_launch")
        j.logout()
        starts: list[int] = []
        for attempt in (1, 2, 3, 4):
            reached = rm.wait_for(
                lambda: len(j.audit("attempt_started")) >= attempt
                and (j.episodes() or [{}])[-1].get("state") in {"pending", "degraded"}, 900, 0.5)
            starts.append(len(j.audit("attempt_started")))
            if not reached:
                break
            j.restart()                      # a daemon restart between attempts
        degraded = j.wait_state("degraded", timeout=60)
        for _ in range(2):                   # two more restarts never add a fifth
            j.restart()
            time.sleep(PASSES_S)
        started = j.audit("attempt_started")
        generations = [json.loads(r["detail_json"])["attempt_generation"] for r in started]
        episode = (j.episodes() or [{}])[-1]
        r5 = {
            "four_attempts_across_restarts": starts == [1, 2, 3, 4] and len(started) == 4,
            "distinct_attempt_generations": len(set(generations)) == 4,
            "later_attempts_started_from_a_rollback_row": (j.fd.row("fd") or {}).get("close_kind") == "spawn_rollback",
            "degraded": degraded.get("state") == "degraded" and episode.get("state") == "degraded",
            "exhausted_audited_once": len(j.audit("exhausted")) == 1,
            "no_pane": cell.panes("fd") == [],
            "binding_unchanged": j.fd.binding().get("generation") == j.before.get("generation"),
        }
        # R9: the provider works again; one operator retry, sent twice.
        cell.set_mode("normal")
        request_id = f"retry-{uuid.uuid4().hex[:8]}"
        first = j.operator("retry", request_id)
        second = j.operator("retry", request_id)
        j.wait_state("restored", timeout=300)
        episode = (j.episodes() or [{}])[-1]
        rec["retry_replies"] = [first, second]
        rec["checks"] = {
            **r5,
            **j.restored_checks(),
            "first_retry_ok": first.get("type") == "assistant.restore.ok" and first.get("duplicate") is False,
            "second_retry_is_a_duplicate": second.get("type") == "assistant.restore.ok"
                and second.get("duplicate") is True,
            "budget_epoch_two": episode.get("budget_epoch") == 2,
            "attempt_seq_five": episode.get("attempt_seq") == 5 and len(j.audit("attempt_started")) == 5,
            "one_budget_reset": len(j.audit("budget_reset")) == 1,
        }
        finish(j, evidence, rec)
    assert_pass(rec)


def test_stalled_bootstrap_fails_one_bounded_attempt(tmp_path, evidence):
    rec: dict[str, Any] = {"cell": "R6"}
    with rm.cell_env(tmp_path / "env", evidence) as cell:
        j = Journey(cell, enabled=True)
        cell.set_mode("stall")
        j.logout()
        failed = rm.wait_for(lambda: next((e for e in j.episodes() if e["state"] == "pending"
                                           and e["attempt_seq"] == 1), None), 400, 0.5)
        episode = failed or (j.episodes() or [{}])[-1]
        outcome = [r for r in j.audit("attempt_outcome") if r["outcome"] != "uncertain"]
        rec["attempt_outcome"] = outcome
        rec["checks"] = {
            "first_attempt_failed_with_the_boot_readiness_code": bool(failed) and bool(outcome)
                and outcome[-1]["outcome"] == "boot_not_ready",
            "budget_used_one": episode.get("budget_used") == 1,
            "state_pending_with_a_future_retry": episode.get("state") == "pending"
                and str(episode.get("next_attempt_at") or "") > str(episode.get("attempt_started_at") or ""),
            "no_second_attempt_yet": len(j.audit("attempt_started")) == 1,
            "binding_unchanged": j.fd.binding().get("generation") == j.before.get("generation"),
        }
        finish(j, evidence, rec)
    assert_pass(rec)


# --------------------------------------------------------------------------- #
# R7: maintenance inhibit
# --------------------------------------------------------------------------- #


def test_inhibit_file_suspends_the_automatic_trigger(tmp_path, evidence):
    rec: dict[str, Any] = {"cell": "R7"}
    with rm.cell_env(tmp_path / "env", evidence) as cell:
        j = Journey(cell, enabled=True)
        j.inhibit_file.write_text("maintenance")
        j.logout()
        time.sleep(PASSES_S)
        held = j.restore()
        held_checks = {
            "suspended_while_present": held.get("state") == "suspended",
            "zero_episodes_while_present": j.episodes() == [],
            "no_pane_while_present": cell.panes("fd") == [],
        }
        j.inhibit_file.unlink()
        j.wait_state("restored", timeout=240)
        rec["checks"] = {**held_checks, **j.restored_checks()}
        finish(j, evidence, rec)
    assert_pass(rec)


def test_inhibit_appearing_later_stops_the_next_attempt(tmp_path, evidence):
    rec: dict[str, Any] = {"cell": "R7b"}
    with rm.cell_env(tmp_path / "env", evidence) as cell:
        j = Journey(cell, enabled=True)
        cell.set_mode("exit_at_launch")
        j.logout()
        pending = rm.wait_for(lambda: next((e for e in j.episodes() if e["state"] == "pending"
                                            and e["attempt_seq"] == 1), None), 300, 0.5)
        j.inhibit_file.write_text("maintenance")
        cell.set_mode("normal")
        time.sleep(45)                        # past the 30 s retry delay
        held = j.restore()
        held_checks = {
            "first_attempt_failed": bool(pending),
            "suspended_past_the_retry_time": held.get("state") == "suspended",
            "still_one_attempt": len(j.audit("attempt_started")) == 1,
        }
        j.inhibit_file.unlink()
        j.wait_state("restored", timeout=240)
        rec["checks"] = {**held_checks, **j.restored_checks(),
                         "second_attempt_restored": len(j.audit("attempt_started")) == 2}
        finish(j, evidence, rec)
    assert_pass(rec)


# --------------------------------------------------------------------------- #
# R8: the operator verb with the automatic trigger off
# --------------------------------------------------------------------------- #


def test_manual_restore_with_the_flag_off(tmp_path, evidence):
    rec: dict[str, Any] = {"cell": "R8"}
    with rm.cell_env(tmp_path / "env", evidence) as cell:
        j = Journey(cell, enabled=False)
        j.logout()
        time.sleep(PASSES_S)
        idle = {"nothing_happens_without_a_request": j.episodes() == [] and cell.panes("fd") == []}
        replies: list[dict[str, Any]] = []

        def send(tag: str) -> None:
            replies.append(j.operator("restore", f"manual-{tag}-{uuid.uuid4().hex[:6]}"))

        threads = [threading.Thread(target=send, args=(t,)) for t in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)
        j.wait_state("restored", timeout=300)
        rec["replies"] = replies
        rec["checks"] = {
            **idle, **j.restored_checks(),
            "both_replies_ok": len(replies) == 2
                and all(r.get("type") == "assistant.restore.ok" for r in replies),
            "one_manual_episode": len(j.episodes()) == 1 and j.episodes()[0]["trigger"] == "manual",
        }
        finish(j, evidence, rec)
    assert_pass(rec)


# --------------------------------------------------------------------------- #
# R10: traffic sent while the seat was dead arrives once
# --------------------------------------------------------------------------- #


def test_dead_window_traffic_is_delivered_once(tmp_path, evidence):
    rec: dict[str, Any] = {"cell": "R10"}
    with rm.cell_env(tmp_path / "env", evidence) as cell:
        j = Journey(cell, enabled=True)
        j.inhibit_file.write_text("hold the restore until the death is recorded")
        j.logout()
        recorded = rm.wait_for(lambda: (j.fd.row("fd") or {}).get("status") == "closed", 120, 0.25)
        traffic = fm.dead_window_traffic(j.fd, "reconciled")
        j.inhibit_file.unlink()
        j.wait_state("restored", timeout=240)
        def tell_count() -> int:
            # A tell to the bound front desk is held by its digest; a hold counts
            # as the delivery (docs/assistant.md, "Rebinding after a seat resume").
            held = [h for h in j.fd.held(traffic["tell_text"]) if h.get("recipient_stream_id") == fm.sid("fd")]
            return j.fd.delivered("fd", traffic["tell_text"]) + len(held)

        tell_once = rm.wait_for(lambda: tell_count() >= 1, 60, 0.25)
        input_once = rm.wait_for(lambda: j.fd.delivered("fd", traffic["input_text"]) >= 1, 60, 0.25)
        time.sleep(PASSES_S)
        rec["traffic"] = traffic
        rec["checks"] = {
            "death_recorded_before_the_traffic": bool(recorded),
            **j.restored_checks(),
            "tell_delivered_exactly_once": bool(tell_once) and tell_count() == 1
                and j.fd.tell_queue() == [],
            "input_delivered_exactly_once": bool(input_once) and j.fd.delivered("fd", traffic["input_text"]) == 1,
        }
        finish(j, evidence, rec)
    assert_pass(rec)
