"""G2: real close CLI, owned processes and durable ownership across restart.

Reuse the isolated restart-matrix daemon and stub-provider fixtures. This is
fixture evidence, not installed-service or real-provider acceptance.
"""
from __future__ import annotations

import asyncio
import json
import signal
import subprocess
import sys

import prockill
from tests.soak import restart_matrix as rm
from tools.gate_owner_manifest import process_start_identity


def _record_termination(cell):
    """Observe the actual daemon-to-tmux boundary without replacing it."""
    original = cell.daemon.tmux_bin
    events = cell.root / "close-events.jsonl"
    wrapper = cell.root / "record-close"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"original = {str(original)!r}\n"
        f"events = {str(events)!r}\n"
        "if sys.argv[1:2] == ['kill-session']:\n"
        "    with open(events, 'a') as output:\n"
        "        output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "os.execv(original, [original, *sys.argv[1:]])\n"
    )
    wrapper.chmod(0o755)
    cell.daemon.tmux_bin = wrapper
    return events


def _kills(events, name):
    if not events.exists():
        return []
    return [args for line in events.read_text().splitlines()
            if (args := json.loads(line)) == ["kill-session", "-t", f"={name}:"]]


def _row(cell, name):
    rows = cell.sql(
        "SELECT s.*, g.generation AS session_generation FROM sessions AS s "
        "JOIN v2_session_generations AS g ON g.host=s.host AND g.session_name=s.session_name "
        "WHERE s.host=? AND s.session_name=?", (rm.LOCAL_HOST, name),
    )
    assert len(rows) == 1, rows
    return rows[0]


def _pane(cell, name):
    result = cell.namespace.run("list-panes", "-t", f"={name}", "-F", "#{pane_id}|#{pane_pid}")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 1, lines
    pane, pid = lines[0].split("|")
    identity = process_start_identity(int(pid))
    assert identity, "live pane has no process identity"
    return pane, pid, identity


def _close(cell, caller, target, generation):
    # The actual CLI subprocess loads the fixture-issued caller credential and
    # sends close over the real daemon transport, as in test_close_progeny_cli.
    env = rm.cli_env(cell, caller)
    result = subprocess.run(
        [sys.executable, "-m", "agent_orch.cli", "close", "--operator-confirm",
         "--expected-generation", generation, "--timeout", "10",
         f"{rm.LOCAL_HOST}:{target}"],
        env=env, cwd=cell.root, capture_output=True, text=True, timeout=15,
    )
    assert result.stdout.strip(), (result.returncode, result.stderr)
    return result.returncode, json.loads(result.stdout)


def _closed_audits(cell, name):
    return cell.sql("SELECT * FROM v2_close_audit WHERE session_name=? AND disposition='closed'", (name,))


def test_owned_close_refuses_foreign_identity_and_replays_across_restart(tmp_path):
    owned, foreign = "g2-owned", "g2-foreign"
    with rm.cell_env(tmp_path / "cell", tmp_path / "evidence") as cell:
        events = _record_termination(cell)
        cell.daemon.start()
        rm.seed_seats(cell, owned, foreign)
        before = {name: (_row(cell, name), _pane(cell, name)) for name in (owned, foreign)}
        assert not _kills(events, owned)
        assert not _closed_audits(cell, owned)

        # Crash the real daemon process, not just its Store. The tmux namespace
        # and both live seats survive; boot reconciles the same durable DB.
        cell.daemon.stop(signal.SIGKILL)
        cell.daemon.start()
        for name in (owned, foreign):
            row, pane = before[name]
            assert _row(cell, name)["status"] == "open"
            assert _row(cell, name)["session_generation"] == row["session_generation"]
            assert _pane(cell, name) == pane, "restart replaced or duplicated seat ownership"
            assert cell.panes(name) == [name]
            assert not cell.reservations(name)

        generation = before[owned][0]["session_generation"]
        rc, refused = _close(cell, foreign, owned, generation)
        assert rc == 1, refused
        assert refused["type"] == "close.error", refused
        assert refused["error_code"] == "close_unauthorized", refused
        assert _row(cell, owned)["status"] == "open"
        assert _pane(cell, owned) == before[owned][1]
        assert not _kills(events, owned), "foreign identity reached termination"
        assert not _closed_audits(cell, owned)

        # Inventory immediately before close, after restart. process_tree's
        # root-only fallback on a failed probe cannot prove descendant cleanup.
        records = asyncio.run(prockill.process_records())
        root_pid = int(_pane(cell, owned)[1])
        assert root_pid in records, "owned process inventory probe failed"
        tracked = {root_pid}
        while children := {pid for pid, record in records.items()
                           if record["ppid"] in tracked} - tracked:
            tracked.update(children)
        identities = {pid: process_start_identity(pid) for pid in tracked}
        assert all(identities.values()), "owned process identity probe failed"

        rc, closed = _close(cell, owned, owned, generation)
        assert rc == 0 and closed["type"] == "close.ok", closed
        rm.wait_for(lambda: not cell.panes(owned), timeout=5)
        rm.wait_for(lambda: all(process_start_identity(int(pid)) != identity
                               for pid, identity in identities.items()), timeout=5)
        assert _row(cell, owned)["status"] == "closed"
        assert _row(cell, owned)["session_generation"] == generation
        assert len(_kills(events, owned)) == 1, "owned termination must execute exactly once"
        audits = _closed_audits(cell, owned)
        assert len(audits) == 1, audits
        assert audits[0]["closed_by"] == f"{rm.LOCAL_HOST}:{owned}"
        assert _pane(cell, foreign) == before[foreign][1]
        assert not _kills(events, foreign)

        # Reopen the durable terminal state in another daemon process. The old
        # self credential can read the idempotent close result but not re-own it.
        cell.daemon.stop(signal.SIGKILL)
        cell.daemon.start()
        for _ in range(2):
            rc, repeated = _close(cell, owned, owned, generation)
            assert rc == 0 and repeated["type"] == "close.already_closed", repeated
        assert _row(cell, owned)["status"] == "closed"
        assert _row(cell, owned)["session_generation"] == generation
        assert not cell.panes(owned)
        assert not cell.reservations(owned)
        assert len(_kills(events, owned)) == 1, "replayed close terminated the seat again"
        assert _closed_audits(cell, owned) == audits
        assert _pane(cell, foreign) == before[foreign][1]
        assert _row(cell, foreign)["status"] == "open"
        assert not _kills(events, foreign)
