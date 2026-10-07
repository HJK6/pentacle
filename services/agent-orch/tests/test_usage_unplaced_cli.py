"""`agent-orch usage --unplaced` reads v2_usage_unplaced read-only (AC10)."""
import json
import sqlite3

from agent_orch import cli


def _seed(data_dir):
    data_dir.mkdir()
    conn = sqlite3.connect(data_dir / "sessions.db")
    conn.execute("""CREATE TABLE v2_usage_unplaced (host TEXT, provider TEXT, native_session_id TEXT,
        record_key TEXT, stream_id TEXT, source_pane_pid TEXT, transcript_ts TEXT, reason TEXT,
        first_seen_at TEXT, detail TEXT)""")
    rows = [
        ("h", "claude", "n", "m2", "h:v2-a", "1", None, "outside_all_generations", "t", None),
        ("h", "claude", "n", "m3", "h:v2-a", "1", None, "boundary_uncertain", "t", None),
        ("h", "claude", "n", "loss:ab:1", None, None, None, "held_span_expired_ttl", "t",
         json.dumps({"records_lost": 4, "rev": 2})),
        ("h", "claude", "n", "loss:ab:2", None, None, None, "held_span_expired_ttl", "t",
         json.dumps({"records_lost": 3, "rev": 1})),
        ("h", "claude", "n", "loss_conflict:ab:1", None, None, None, "loss_conflict", "t",
         json.dumps({"stored": {"records_lost": 4}, "pending": {"records_lost": 9}})),
    ]
    conn.executemany("INSERT INTO v2_usage_unplaced VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def test_unplaced_readback_counts_losses_and_lists_conflicts_uncounted(tmp_path, capsys):
    _seed(tmp_path / "data")
    assert cli.main(["usage", "--unplaced", "--json", "--data-dir", str(tmp_path / "data")]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["streams"] == {"h:v2-a": {"count": 2, "reasons": {
        "boundary_uncertain": 1, "outside_all_generations": 1}}}
    assert summary["losses"] == {"held_span_expired_ttl": 7}
    assert summary["conflicts"] == [{"host": "h", "loss_id": "ab:1", "stored": {"records_lost": 4},
                                     "pending": {"records_lost": 9}}]
    assert cli.main(["usage", "--unplaced", "--data-dir", str(tmp_path / "data")]) == 0
    text = capsys.readouterr().out
    assert "unresolved loss conflicts: 1" in text and "no recovered count" in text


def test_unplaced_without_db_fails_cleanly(tmp_path, capsys):
    assert cli.main(["usage", "--unplaced", "--data-dir", str(tmp_path)]) == 1
    assert "no daemon database" in capsys.readouterr().err


def test_usage_without_host_or_unplaced_is_an_error(capsys):
    assert cli.main(["usage"]) == 2
