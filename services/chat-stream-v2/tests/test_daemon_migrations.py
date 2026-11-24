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
        sequence = conn.execute(
            "SELECT pair_id,archive_path FROM v2_send_receipt_sequence WHERE singleton=1"
        ).fetchone()
        conn.execute("UPDATE v2_send_receipt_sequence SET archive_ready=0 WHERE singleton=1")
        conn.execute(f"DROP TABLE {daemon_migrations.REGISTRY}")
    with sqlite3.connect(sequence[1]) as archive:
        archive.execute("DROP TABLE v2_receipt_route_archive")
    result = daemon_migrations.prepare(database)
    assert result["prepared"] is True
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT value FROM migration_fixture").fetchone()[0] == "preserve"
        assert conn.execute(
            "SELECT pair_id,archive_ready FROM v2_send_receipt_sequence WHERE singleton=1"
        ).fetchone() == (sequence[0], 1)


def test_inspect_rejects_main_marker_pair_identity_mismatch(tmp_path: Path) -> None:
    database = tmp_path / "wrong-main-marker.db"
    daemon_migrations.prepare(database)
    with sqlite3.connect(database) as conn:
        conn.execute(
            f"UPDATE {daemon_migrations.REGISTRY} SET pair_id='wrong-pair-id' WHERE step_id=?",
            (daemon_migrations.MAIN_STEP_ID,),
        )
    assert daemon_migrations.inspect(database)["prepared"] is False

    with sqlite3.connect(database) as conn:
        pair = conn.execute(
            "SELECT archive_path FROM v2_send_receipt_sequence WHERE singleton=1"
        ).fetchone()[0]
        conn.execute(
            f"UPDATE {daemon_migrations.REGISTRY} SET pair_id=(SELECT pair_id FROM "
            f"v2_send_receipt_sequence WHERE singleton=1), archive_path='wrong.db' "
            "WHERE step_id=?",
            (daemon_migrations.MAIN_STEP_ID,),
        )
    assert pair
    assert daemon_migrations.inspect(database)["prepared"] is False


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


def test_interruption_before_main_step_receipt_does_not_repeat_store_start(
    tmp_path: Path, monkeypatch,
) -> None:
    database = tmp_path / "main-commit-before-receipt.db"
    original_store = daemon_migrations.Store
    starts = 0

    class CountingStore(original_store):
        def start(self, *args, **kwargs) -> None:
            nonlocal starts
            starts += 1
            return super().start(*args, **kwargs)

    monkeypatch.setattr(daemon_migrations, "Store", CountingStore)

    def interrupt() -> None:
        raise RuntimeError("injected interruption before main step receipt")

    try:
        daemon_migrations.prepare(database, before_main_step_record=interrupt)
    except RuntimeError as exc:
        assert "before main step receipt" in str(exc)
    else:
        raise AssertionError("interruption was not injected")
    assert starts == 1
    assert daemon_migrations.inspect(database)["prepared"] is False

    assert daemon_migrations.prepare(database)["prepared"] is True
    assert starts == 1


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
