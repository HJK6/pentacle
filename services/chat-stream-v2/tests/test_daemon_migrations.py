from __future__ import annotations

import sqlite3
from pathlib import Path

import daemon_migrations
import store as store_mod
from store import Store


def test_fresh_pair_prepare_is_versioned_and_inspect_is_read_only(tmp_path: Path) -> None:
    database = tmp_path / "fresh.db"
    result = daemon_migrations.prepare(database)
    assert result["prepared"] is True
    assert result["step_id"] == daemon_migrations.PAIR_STEP_ID
    assert daemon_migrations.inspect(database) == result

    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE inspect_sentinel(value TEXT)")
        conn.execute("INSERT INTO inspect_sentinel VALUES('untouched')")
    assert daemon_migrations.inspect(database) == result
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT value FROM inspect_sentinel").fetchone()[0] == "untouched"


def test_mixed_v2_pair_converges_without_replacing_rows(tmp_path: Path) -> None:
    database = tmp_path / "mixed.db"
    daemon_migrations.prepare(database)
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE migration_fixture(value TEXT)")
        conn.execute("INSERT INTO migration_fixture VALUES('preserve')")
        conn.execute("PRAGMA user_version=0")
        conn.execute(f"DROP TABLE {daemon_migrations.REGISTRY}")
    result = daemon_migrations.prepare(database)
    assert result["prepared"] is True
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT value FROM migration_fixture").fetchone()[0] == "preserve"


def test_interruption_before_main_commit_rolls_back_and_resumes(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "before-main-commit.db"
    original = store_mod.prepare_history_schema

    def interrupt(*args, **kwargs):
        raise RuntimeError("injected interruption before main commit")

    monkeypatch.setattr(store_mod, "prepare_history_schema", interrupt)
    try:
        daemon_migrations.prepare(database)
    except RuntimeError as exc:
        assert "before main commit" in str(exc)
    else:
        raise AssertionError("interruption was not injected")
    assert daemon_migrations.inspect(database)["prepared"] is False
    monkeypatch.setattr(store_mod, "prepare_history_schema", original)
    assert daemon_migrations.prepare(database)["prepared"] is True


def test_interruption_after_main_commit_resumes(tmp_path: Path) -> None:
    database = tmp_path / "before-archive.db"

    def interrupt() -> None:
        raise RuntimeError("injected interruption after main commit")

    try:
        daemon_migrations.prepare(database, after_main_commit=interrupt)
    except RuntimeError as exc:
        assert "after main commit" in str(exc)
    else:
        raise AssertionError("interruption was not injected")
    assert daemon_migrations.inspect(database)["prepared"] is False
    assert daemon_migrations.prepare(database)["prepared"] is True


def test_interruption_between_main_and_archive_commits_resumes(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "between-commits.db"

    def interrupt() -> None:
        raise RuntimeError("injected interruption after archive commit")

    try:
        daemon_migrations.prepare(database, after_archive_commit=interrupt)
    except RuntimeError as exc:
        assert "after archive commit" in str(exc)
    else:
        raise AssertionError("interruption was not injected")
    assert daemon_migrations.inspect(database)["prepared"] is False
    monkeypatch.setattr(daemon_migrations.Store, "start", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("completed main step was rerun")
    ))
    assert daemon_migrations.prepare(database)["prepared"] is True
