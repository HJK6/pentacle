"""Unit tests for the external residue reaper.

Covers every invariant and rule for the residue classes (default-server tmux
shell sessions, harness tmux server sockets, scratch daemons), the two-phase
(trash-then-purge) ledger mechanics, the act-layer outcome accounting, and the
dedup key. Hidden-seat closing is NOT part of this tool (daemon self-close lane
owns it), so there are no seat tests. All tests drive the PURE classifier
(`plan_cycle`) with hand-built inventories and a fixed `now`; nothing here
touches the live daemon, tmux, or ps.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tools import orphan_reaper as orr

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
H = 3600


def ago(seconds: float) -> datetime:
    return NOW - timedelta(seconds=seconds)


def session(**kw) -> orr.TmuxSession:
    base = dict(server="default", socket_path=None, name="deploy-x",
                attached=False, created_at=ago(10 * H), has_live_child=False)
    base.update(kw)
    return orr.TmuxSession(**base)


def server(**kw) -> orr.TmuxServer:
    base = dict(socket="v2cap-abc", socket_path="/tmp/tmux-501/v2cap-abc",
                session_count=0, any_live_pane=False)
    base.update(kw)
    return orr.TmuxServer(**base)


def daemon(**kw) -> orr.ScratchDaemon:
    base = dict(pid=4242, cmdline="python main.py --port 0 --db /tmp/x.db",
                client_count=0, started_at=ago(5 * H))
    base.update(kw)
    return orr.ScratchDaemon(**base)


def plan_of(*, sessions=(), servers=(), daemons=(), ledger=None):
    inv = orr.Inventory(tmux_sessions=tuple(sessions), tmux_servers=tuple(servers),
                        scratch_daemons=tuple(daemons))
    return orr.plan_cycle(inv, ledger or {}, NOW)


def skip_reasons(plan) -> dict[str, str]:
    return {s.target_desc: s.reason for s in plan.skips}


# --------------------------------------------------------------------------- #
# Default-server tmux sessions.
# --------------------------------------------------------------------------- #
def test_v2_session_never_killed():
    plan = plan_of(sessions=[session(name="v2-abc", created_at=ago(50 * H))])
    assert not plan.recorded and not plan.purges
    assert any("daemon-owned" in r for r in skip_reasons(plan).values())


def test_attached_session_never_killed():
    plan = plan_of(sessions=[session(attached=True, created_at=ago(50 * H))])
    assert not plan.recorded and not plan.purges
    assert any("attached" in r for r in skip_reasons(plan).values())


def test_session_with_live_child_never_killed():
    plan = plan_of(sessions=[session(has_live_child=True, created_at=ago(50 * H))])
    assert not plan.recorded and not plan.purges
    assert any("running non-shell child" in r for r in skip_reasons(plan).values())


def test_idle_shell_session_over_6h_is_recorded_not_killed_first_cycle():
    plan = plan_of(sessions=[session(created_at=ago(10 * H))])
    assert len(plan.recorded) == 1
    assert not plan.purges  # deferred: never killed on first sight


def test_idle_shell_session_under_6h_skips():
    plan = plan_of(sessions=[session(created_at=ago(3 * H))])
    assert not plan.recorded
    assert any("not older than 6h" in r for r in skip_reasons(plan).values())


def test_idle_shell_session_exactly_6h_skips_strict():
    plan = plan_of(sessions=[session(created_at=ago(6 * H))])   # exactly 6h -> not OLDER than
    assert not plan.recorded


def test_session_unknown_created_at_fails_closed():
    plan = plan_of(sessions=[session(created_at=None)])
    assert not plan.recorded
    assert any("fail closed" in r for r in skip_reasons(plan).values())


# --------------------------------------------------------------------------- #
# Harness server sockets.
# --------------------------------------------------------------------------- #
def test_empty_harness_server_recorded():
    plan = plan_of(servers=[server(session_count=0, any_live_pane=False)])
    assert len(plan.recorded) == 1
    assert not plan.purges


def test_harness_server_with_live_pane_never_killed():
    plan = plan_of(servers=[server(session_count=1, any_live_pane=True)])
    assert not plan.recorded and not plan.purges
    assert any("live pane" in r for r in skip_reasons(plan).values())


def test_harness_server_with_protected_session_never_killed():
    plan = plan_of(servers=[server(socket="v2cap-x", session_count=2,
                                   any_live_pane=False, has_protected_session=True)])
    assert not plan.recorded and not plan.purges
    assert any("v2-*/attached session" in r for r in skip_reasons(plan).values())


# --------------------------------------------------------------------------- #
# Scratch daemons.
# --------------------------------------------------------------------------- #
def test_scratch_daemon_with_client_never_killed():
    plan = plan_of(daemons=[daemon(client_count=1)])
    assert not plan.recorded and not plan.purges
    assert any("client connection" in r for r in skip_reasons(plan).values())


def test_scratch_daemon_no_client_over_2h_recorded():
    plan = plan_of(daemons=[daemon(client_count=0, started_at=ago(5 * H))])
    assert len(plan.recorded) == 1
    assert not plan.purges


def test_scratch_daemon_no_client_under_2h_skips():
    plan = plan_of(daemons=[daemon(client_count=0, started_at=ago(1 * H))])
    assert not plan.recorded
    assert any("not older than 2h" in r for r in skip_reasons(plan).values())


def test_scratch_daemon_unknown_start_fails_closed():
    plan = plan_of(daemons=[daemon(client_count=0, started_at=None)])
    assert not plan.recorded
    assert any("fail closed" in r for r in skip_reasons(plan).values())


def test_scratch_daemon_unknown_client_probe_fails_closed():
    plan = plan_of(daemons=[daemon(client_count=-1, started_at=ago(5 * H))])
    assert not plan.recorded and not plan.purges
    assert any("fail closed" in r for r in skip_reasons(plan).values())


# --------------------------------------------------------------------------- #
# Two-phase (trash-then-purge) mechanics.
# --------------------------------------------------------------------------- #
def _ledger_for(obj, tidfn, trashed_ago_h):
    tid = tidfn(obj)
    return tid, {"targets": {tid: {"kind": tid.split(":", 1)[0], "label": "x", "rule": "r",
                                   "action": "kill_session", "exec_ref": "deploy-x",
                                   "trashed_at": orr._iso(ago(trashed_ago_h * H))}}}


def test_session_purged_only_after_24h_window():
    s = session(created_at=ago(30 * H))
    tid, ledger = _ledger_for(s, orr._session_tid, 25)
    plan = plan_of(sessions=[s], ledger=ledger)
    assert len(plan.purges) == 1
    assert plan.purges[0].exec_ref == "deploy-x"


def test_session_pending_while_in_window():
    s = session(created_at=ago(30 * H))
    tid, ledger = _ledger_for(s, orr._session_tid, 10)
    plan = plan_of(sessions=[s], ledger=ledger)
    assert not plan.purges
    assert len(plan.pending) == 1


def test_came_back_to_life_is_dropped_never_purged():
    s = session(created_at=ago(30 * H), has_live_child=True)
    tid, ledger = _ledger_for(s, orr._session_tid, 25)
    plan = plan_of(sessions=[s], ledger=ledger)
    assert not plan.purges
    assert any(r["target_id"] == tid for r in plan.recovered)
    assert tid not in plan.next_ledger["targets"]


def test_vanished_target_is_dropped():
    tid = "tmux_server:v2cap-gone"
    ledger = {"targets": {tid: {"kind": "tmux_server", "label": "gone", "rule": "r",
                                "action": "kill_server", "exec_ref": "/tmp/x",
                                "trashed_at": orr._iso(ago(25 * H))}}}
    plan = plan_of(servers=[], ledger=ledger)
    assert not plan.purges
    assert any(r["target_id"] == tid for r in plan.recovered)


def test_reused_session_name_gets_fresh_clock():
    # A same-named session recreated (new created_at) must NOT inherit the dead
    # predecessor's 24h clock: old tid is treated as vanished, new one is recorded.
    old = session(created_at=ago(50 * H))
    new = session(created_at=ago(7 * H))
    old_tid = orr._session_tid(old)
    ledger = {"targets": {old_tid: {"kind": "tmux_session", "label": "x", "rule": "r",
                                    "action": "kill_session", "exec_ref": "deploy-x",
                                    "trashed_at": orr._iso(ago(30 * H))}}}
    plan = plan_of(sessions=[new], ledger=ledger)
    assert not plan.purges
    assert len(plan.recorded) == 1
    assert orr._session_tid(new) in plan.next_ledger["targets"]
    assert old_tid not in plan.next_ledger["targets"]


# --------------------------------------------------------------------------- #
# Mixed inventory, counters, dedup, card.
# --------------------------------------------------------------------------- #
def test_mixed_inventory_counts_and_card():
    sessions = [session(name="deploy-1", created_at=ago(20 * H)),   # record
                session(name="v2-live", created_at=ago(99 * H))]    # skip inv
    servers = [server(socket="v2cap-x", session_count=0)]           # record
    plan = plan_of(sessions=sessions, servers=servers)
    c = plan.counters()
    assert c["recorded"] == 2
    assert c["skipped_active"] >= 1
    card = orr.render_card(plan, "dry-run")
    assert "RECORDED" in card and "SKIPPED" in card
    for s in plan.skips:
        assert s.target_desc in card


def test_dedup_key_stable_for_same_decisions():
    s = session(created_at=ago(20 * H))
    k1 = plan_of(sessions=[s]).dedup_key("dry-run")
    k2 = plan_of(sessions=[s]).dedup_key("dry-run")
    assert k1 == k2


def test_dedup_key_changes_when_decisions_change():
    k1 = plan_of(sessions=[session(name="a", created_at=ago(20 * H))]).dedup_key("dry-run")
    k2 = plan_of(sessions=[session(name="b", created_at=ago(20 * H))]).dedup_key("dry-run")
    assert k1 != k2


def test_dedup_key_changes_when_recovered_differs():
    p1 = orr.Plan(now=NOW)
    p2 = orr.Plan(now=NOW, recovered=[{"target_id": "tmux_server:v2cap-z", "label": "z", "reason": "r"}])
    assert p1.dedup_key("act") != p2.dedup_key("act")


def test_empty_inventory_is_noop():
    plan = plan_of()
    assert not plan.purges and not plan.recorded and not plan.skips
    assert plan.next_ledger["targets"] == {}


# --------------------------------------------------------------------------- #
# Act layer: outcomes, rechecks, ledger maintenance.
# --------------------------------------------------------------------------- #
def test_prekill_recheck_blocks_revived_target(monkeypatch):
    cand = orr.Candidate("tmux_session", "tmux_session:default:x:1", "sess x",
                         orr.ACTION_KILL_SESSION, "idle >6h", "x")
    plan = orr.Plan(now=NOW, purges=[cand], next_ledger={"targets": {"tmux_session:default:x:1": {"trashed_at": "old"}}})
    killed = []
    monkeypatch.setattr(orr, "_kill_session", lambda name: (killed.append(name) or (True, "")))
    orr.execute_plan(plan, purge_recheck=lambda c: (False, "became live"))
    assert killed == []
    assert not plan.purged_ok
    assert any("recheck" in s.reason for s in plan.live_skips)
    assert plan.counters()["skipped_live"] == 1
    assert "tmux_session:default:x:1" not in plan.next_ledger["targets"]   # clock reset


def test_prekill_recheck_allows_dead_target(monkeypatch):
    cand = orr.Candidate("tmux_session", "tmux_session:default:x:1", "sess x",
                         orr.ACTION_KILL_SESSION, "idle >6h", "x")
    plan = orr.Plan(now=NOW, purges=[cand],
                    next_ledger={"targets": {"tmux_session:default:x:1": {"trashed_at": "t"}}})
    killed = []
    monkeypatch.setattr(orr, "_kill_session", lambda name: (killed.append(name) or (True, "")))
    orr.execute_plan(plan, purge_recheck=lambda c: (True, ""))
    assert killed == ["x"] and plan.purged_ok == [cand]
    assert plan.counters()["purged"] == 1
    assert "tmux_session:default:x:1" not in plan.next_ledger["targets"]   # purged: gone


def test_failed_kill_not_counted_as_purged(monkeypatch):
    cand = orr.Candidate("tmux_server", "tmux_server:v2cap-x", "srv x",
                         orr.ACTION_KILL_SERVER, "empty", "/tmp/x")
    plan = orr.Plan(now=NOW, purges=[cand],
                    next_ledger={"targets": {"tmux_server:v2cap-x": {"trashed_at": "t"}}})
    monkeypatch.setattr(orr, "_kill_server", lambda sp: (False, "no server on that socket"))
    orr.execute_plan(plan, purge_recheck=lambda c: (True, ""))
    assert plan.purged_ok == [] and len(plan.refused) == 1
    assert plan.counters()["purged"] == 0 and plan.counters()["refused"] == 1


def test_act_card_always_notifies_even_with_no_notify(monkeypatch):
    calls = []
    monkeypatch.setattr(orr, "_run", lambda cmd, timeout=30.0: (calls.append(cmd) or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()))
    plan = orr.Plan(now=NOW, acted=True)
    ok = orr.post_card(plan, "act", dry=True)   # dry=True (=--no-notify) must be ignored in act
    assert ok and calls and calls[0][0:2] == ["agent-orch", "notify"]


# --------------------------------------------------------------------------- #
# Parser / helper units (fixture strings, no live commands).
# --------------------------------------------------------------------------- #
def test_parse_default_sessions_attached_and_children():
    stdout = "deploy-1\t1\t1788500000\nidle-2\t0\t1788400000\n"
    live = {"deploy-1": True, "idle-2": False}
    out = orr._parse_default_sessions(stdout, lambda n: live[n])
    by = {s.name: s for s in out}
    assert by["deploy-1"].attached is True and by["deploy-1"].has_live_child is True
    assert by["idle-2"].attached is False and by["idle-2"].has_live_child is False


def test_harness_socket_re_matches_expected_and_not_default():
    assert orr.HARNESS_SOCKET_RE.match("v2cap-abc")
    assert orr.HARNESS_SOCKET_RE.match("v2s3")
    assert orr.HARNESS_SOCKET_RE.match("v2loop-1")
    assert orr.HARNESS_SOCKET_RE.match("ptr-x")
    assert orr.HARNESS_SOCKET_RE.match("authctx9")
    assert orr.HARNESS_SOCKET_RE.match("casgate-gate")
    assert not orr.HARNESS_SOCKET_RE.match("default")


def test_parse_ts_forms():
    assert orr._parse_ts("2026-09-05T12:00:00Z") == NOW
    assert orr._parse_ts(None) is None
    assert orr._parse_ts("") is None
    assert orr._parse_ts(1788600000) is not None


def test_ledger_roundtrip(tmp_path):
    p = tmp_path / "orphan_reaper.json"
    assert orr.load_ledger(p)["targets"] == {}
    orr.save_ledger(p, {"version": 1, "producer": "orphan_reaper", "targets": {"x": {"trashed_at": "t"}}})
    assert orr.load_ledger(p)["targets"]["x"]["trashed_at"] == "t"


def test_scratch_daemon_parse_matches_only_port_zero():
    # ps -eo pid=,lstart=,command= layout: pid + 5 lstart tokens + cmd.
    stdout = (
        "  6719 Thu Sep  3 01:39:22 2026 Python main.py --port 0 --db /tmp/x.db\n"
        " 50316 Fri Sep  4 10:00:00 2026 Python /repo/main.py --port 7791 --db /real/sessions.db\n"
        " 99999 Sat Sep  5 00:00:00 2026 zsh -lc echo hi\n"
    )
    out = orr._parse_scratch_daemons(stdout, client_count_fn=lambda pid: 0)
    assert {d.pid for d in out} == {6719}
    assert out[0].started_at is not None


def test_scratch_regex_ignores_nested_port_zero_on_real_daemon():
    real = "Python /repo/main.py --bind x --port 7791 --db /Users/example/.local/share/pentacle-stream/sessions.db --spawn --port 0"
    scratch = "Python main.py --port 0 --db /tmp/v2_smoke.db"
    assert orr._is_scratch_daemon_cmd(real) is False
    assert orr._is_scratch_daemon_cmd(scratch) is True


def test_scratch_regex_excludes_production_db():
    prod = "Python main.py --port 0 --db /Users/example/.local/share/pentacle-stream/sessions.db"
    assert orr._is_scratch_daemon_cmd(prod) is False


def test_scratch_requires_temp_db_identity():
    assert orr._is_scratch_daemon_cmd("Python main.py --port 0 --db /var/lib/other.db") is False
    assert orr._is_scratch_daemon_cmd("Python main.py --port 0 --db /private/var/folders/xx/v2.db") is True


def test_scratch_daemon_tid_is_generation_stamped():
    d1 = daemon(pid=100, started_at=ago(5 * H))
    d2 = daemon(pid=100, started_at=ago(1 * H))   # pid reuse, different start
    assert orr._daemon_tid(d1) != orr._daemon_tid(d2)


def test_pid_is_work_true_when_pane_process_is_the_worker(monkeypatch):
    monkeypatch.setattr(orr, "_process_command", lambda pid: "/usr/bin/python main.py --port 0")
    monkeypatch.setattr(orr, "_run", lambda cmd, timeout=30.0: type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})())
    assert orr._pid_is_work("123") is True


def test_pid_is_work_false_for_bare_shell(monkeypatch):
    monkeypatch.setattr(orr, "_process_command", lambda pid: "-zsh")
    monkeypatch.setattr(orr, "_run", lambda cmd, timeout=30.0: type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})())
    assert orr._pid_is_work("123") is False


def test_pid_is_work_fails_closed_when_probe_errors(monkeypatch):
    monkeypatch.setattr(orr, "_process_command", lambda pid: None)
    assert orr._pid_is_work("123") is True


def test_pid_is_work_fails_closed_when_pgrep_errors(monkeypatch):
    monkeypatch.setattr(orr, "_process_command", lambda pid: "-zsh")
    monkeypatch.setattr(orr, "_run", lambda cmd, timeout=30.0: type("R", (), {"returncode": 2, "stdout": "", "stderr": "pgrep: boom"})())
    assert orr._pid_is_work("123") is True
