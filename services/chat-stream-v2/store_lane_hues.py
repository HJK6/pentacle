"""Hue allocation on existing lane rows and the existing SQLite writer."""
from contextlib import contextmanager
from datetime import datetime, timezone
import logging

from _shared.lane_hues import LANE_HUE_KEYS

TABLE = 'v2_assistant_composite_lanes'
OPEN = ('active', 'paused', 'blocked')
log = logging.getLogger(__name__)


@contextmanager
def hue_transaction(conn):
    owned = not conn.in_transaction
    if owned:
        conn.execute('BEGIN IMMEDIATE')
    try:
        yield
        if owned:
            conn.commit()
    except BaseException:
        if owned:
            conn.rollback()
        raise


def _epoch(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, OverflowError):
        raise ValueError('work_lane_hue_history_invalid') from None


def ensure_lane_hues_schema(conn):
    # Recheck under the write boundary: two Store startups cannot both migrate.
    with hue_transaction(conn):
        present = {row[1] for row in conn.execute('PRAGMA table_info(' + TABLE + ')')}
        if 'hue_open_order' in present:
            if 'hue' not in present:
                raise ValueError('work_lane_hue_schema_invalid')
            return
        if 'hue' not in present:
            conn.execute('ALTER TABLE ' + TABLE + ' ADD COLUMN hue TEXT')
        conn.execute('ALTER TABLE ' + TABLE + ' ADD COLUMN hue_open_order INTEGER')
        history = []
        for raw in conn.execute('SELECT lane_id,first_admitted_at,created_at FROM ' + TABLE + ' WHERE work_state IS NOT NULL'):
            lane = dict(raw)
            entries = [row[0] for row in conn.execute(
                "SELECT created_at FROM v2_work_lane_events WHERE lane_id=? AND prior_state='done' "
                "AND next_state IN ('active','paused','blocked')", (lane['lane_id'],))]
            entry = max(map(_epoch, entries)) if entries else _epoch(lane['first_admitted_at'] or lane['created_at'])
            history.append((entry, lane['lane_id']))
        for order, (_, identity) in enumerate(sorted(history), 1):
            conn.execute('UPDATE ' + TABLE + ' SET hue_open_order=? WHERE lane_id=?', (order, identity))


def enter_open_conn(conn, lane_id):
    assert conn.in_transaction
    order = conn.execute('SELECT COALESCE(MAX(hue_open_order),0)+1 FROM ' + TABLE).fetchone()[0]
    conn.execute('UPDATE ' + TABLE + ' SET hue_open_order=? WHERE lane_id=?', (order, lane_id))


def _open_rows(conn):
    return [dict(row) for row in conn.execute('SELECT lane_id,hue,hue_open_order FROM ' + TABLE +
            " WHERE work_state IN ('active','paused','blocked')")]


def _counts(rows):
    return {key: sum(row['hue'] == key for row in rows) for key in LANE_HUE_KEYS}


def repair_hues_conn(conn):
    assert conn.in_transaction
    rows = _open_rows(conn)
    for _ in range(16):
        counts = _counts(rows)
        if min(counts.values()) or max(counts.values()) <= 1:
            return
        colliders = [row for row in rows if row['hue'] in counts and counts[row['hue']] > 1]
        if any(type(row['hue_open_order']) is not int or row['hue_open_order'] < 1 for row in colliders):
            raise ValueError('work_lane_hue_order_missing')
        mover = min(colliders, key=lambda row: (-row['hue_open_order'], row['lane_id']))
        chosen = min(LANE_HUE_KEYS, key=counts.__getitem__)
        conn.execute('UPDATE ' + TABLE + ' SET hue=? WHERE lane_id=?', (chosen, mover['lane_id']))
        mover['hue'] = chosen
    raise ValueError('work_lane_hue_repair_unbounded')


def prepare_hues_conn(conn, lane_ids):
    with hue_transaction(conn):
        counts = _counts(_open_rows(conn))
        changed = False
        for identity in sorted(set(lane_ids)):
            raw = conn.execute('SELECT hue,hue_open_order,work_state FROM ' + TABLE + ' WHERE lane_id=?', (identity,)).fetchone()
            if raw is None or raw[0] is not None or raw[2] is None:
                continue
            if type(raw[1]) is not int or raw[1] < 1:
                # Malformed post-migration rows fail closed; a read is never an entry.
                log.warning('work_lane_hue_order_missing: %s', identity)
                continue
            chosen = min(LANE_HUE_KEYS, key=counts.__getitem__)
            cursor = conn.execute('UPDATE ' + TABLE + ' SET hue=? WHERE lane_id=? AND hue IS NULL', (chosen, identity))
            if cursor.rowcount:
                changed = True
                if raw[2] in OPEN:
                    counts[chosen] += 1
        if changed:
            repair_hues_conn(conn)
