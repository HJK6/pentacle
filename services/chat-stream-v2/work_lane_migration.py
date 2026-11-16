"""Transactional CHECK migration and data-preserving reverse migration."""
from __future__ import annotations

TABLE = "v2_work_lane_events"
ARCHIVE = "v2_work_lane_events_progress_rollback"


def replace_events_table(conn, ddl: str) -> None:
    """Rebuild only the CHECK; retain row order and all explicit indexes/triggers."""
    indexes = [row[0] for row in conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL",
        (TABLE,))]
    columns = ",".join('"' + row[1] + '"' for row in conn.execute(f"PRAGMA table_info({TABLE})"))
    temp = TABLE + "_replacement"
    conn.execute(ddl.replace("CREATE TABLE IF NOT EXISTS " + TABLE, "CREATE TABLE " + temp))
    conn.execute(f"INSERT INTO {temp} ({columns}) SELECT {columns} FROM {TABLE} ORDER BY rowid")
    conn.execute(f"DROP TABLE {TABLE}")
    conn.execute(f"ALTER TABLE {temp} RENAME TO {TABLE}")
    for statement in indexes:
        conn.execute(statement)


def upgrade_events(conn, ddl: str) -> None:
    existing = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()
    if existing and ("'set_members'" not in existing[0] or "'item_change'" not in existing[0]):
        replace_events_table(conn, ddl)
    else:
        conn.execute(ddl)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (ARCHIVE,)).fetchone():
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({TABLE})")]
        for row in conn.execute(f"SELECT * FROM {ARCHIVE} ORDER BY rowid").fetchall():
            prior = conn.execute(f"SELECT * FROM {TABLE} WHERE event_id=?", (row[0],)).fetchone()
            if prior is not None and tuple(prior) != tuple(row):
                raise ValueError("work_lane_rollback_receipt_conflict")
            if prior is None:
                conn.execute(f"INSERT INTO {TABLE} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", tuple(row))
        conn.execute(f"DROP TABLE {ARCHIVE}")


def rollback_progress(conn, previous_ddl: str) -> int:
    """Caller owns the transaction. Preserve new receipts in this DB for re-upgrade."""
    conn.execute(f"CREATE TABLE IF NOT EXISTS {ARCHIVE} AS SELECT * FROM {TABLE} WHERE 0")
    count = conn.execute(f"SELECT count(*) FROM {TABLE} WHERE operation IN ('set_members','item_change')").fetchone()[0]
    conn.execute(f"INSERT INTO {ARCHIVE} SELECT * FROM {TABLE} WHERE operation IN ('set_members','item_change')")
    conn.execute(f"DELETE FROM {TABLE} WHERE operation IN ('set_members','item_change')")
    replace_events_table(conn, previous_ddl)
    return count
