import sqlite3

import pytest

from tests.soak.harness import assert_quiescent_population


def test_population_census_rejects_open_churn_and_counts_offline_fixtures(tmp_path):
    path = tmp_path / "soak.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE sessions (host TEXT, session_name TEXT, status TEXT)")
        db.executemany("INSERT INTO sessions VALUES (?, ?, ?)", [
            ("soakhost", "soak-core-0", "open"),
            ("offline-peer", "offline-0", "open"),
            ("soakhost", "soak-churn-0-1", "open"),
        ])
    expected = {"soakhost:soak-core-0", "offline-peer:offline-0"}
    with pytest.raises(AssertionError, match="population mismatch"):
        assert_quiescent_population(str(path), expected)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE sessions SET status='closed' WHERE session_name LIKE 'soak-churn-%'")
    assert assert_quiescent_population(str(path), expected) == {
        "open": 2, "expected": 2, "open_churn": 0,
    }
    with pytest.raises(AssertionError, match="missing="):
        assert_quiescent_population(str(path), expected | {"soakhost:soak-core-1"})
