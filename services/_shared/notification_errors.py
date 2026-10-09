"""Additive structured error facts in notifications.db.

Transport state is never stored here. The delivery owner passes settled IDs to
retention after inspecting the existing outbox and linked digest receipts.
"""

from __future__ import annotations
import json
import uuid
from datetime import datetime, timezone

def stamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def init_schema(conn):
    columns = {r[1] for r in conn.execute("PRAGMA table_info(notifications)")}
    for name in ("error_key", "error_context"):
        if name not in columns:
            conn.execute(f"ALTER TABLE notifications ADD COLUMN {name} TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS notifications_error_key ON notifications(error_key) WHERE error_key IS NOT NULL"
    )


class ErrorNotificationMixin:
    def error_rows(self):
        with self._lock:
            self._require_open()
            rows = self._conn.execute(
                "SELECT * FROM notifications WHERE error_context IS NOT NULL"
            ).fetchall()
            return [self._error_row(r) for r in rows]

    @staticmethod
    def _error_row(row):
        if row is None:
            return None
        data = dict(row)
        data["error_context"] = (
            json.loads(data["error_context"]) if data.get("error_context") else None
        )
        # Original body/actions stay at their original authorized source.
        data.pop("body", None)
        data.pop("actions", None)
        data.pop("resolution", None)
        return data

    def error_get(self, notification_id):
        with self._lock:
            self._require_open()
            return self._error_row(
                self._conn.execute(
                    "SELECT * FROM notifications WHERE notification_id=?",
                    (notification_id,),
                ).fetchone()
            )

    def error_upsert(self, *, error_key, context, title, now=None):
        now = now or stamp()
        with self._lock, self._conn:
            self._require_open()
            old = self._conn.execute(
                "SELECT * FROM notifications WHERE error_key=?", (error_key,)
            ).fetchone()
            if old:
                prior = json.loads(old["error_context"])
                if prior["operation_revision"] >= context["operation_revision"]:
                    return self._error_row(old)
                # Preserve attention and transport intent across newer evidence.
                merged = {**prior, **context}
                for key in (
                    "seen_at",
                    "ack_at",
                    "desired_notice_revision",
                    "enqueued_notice_revision",
                    "notice_ids",
                ):
                    merged[key] = prior.get(key)
                resolved = merged["condition"] in ("recovered", "cancelled")
                self._conn.execute(
                    "UPDATE notifications SET error_context=?,title=?,updated_at=?,last_fired_at=?,firing_count=firing_count+1,state=?,resolved_at=? WHERE notification_id=?",
                    (
                        json.dumps(merged, sort_keys=True),
                        title,
                        now,
                        now,
                        "resolved" if resolved else old["state"],
                        now if resolved else old["resolved_at"],
                        old["notification_id"],
                    ),
                )
                nid = old["notification_id"]
            else:
                nid = str(uuid.uuid5(uuid.NAMESPACE_URL, "error-alert:" + error_key))
                context = {
                    **context,
                    "seen_at": None,
                    "ack_at": None,
                    "desired_notice_revision": 0,
                    "enqueued_notice_revision": 0,
                    "notice_ids": [],
                }
                self._conn.execute(
                    "INSERT INTO notifications(notification_id,created_at,updated_at,producer,severity,title,state,actions,ttl_seconds,first_fired_at,last_fired_at,firing_count,error_key,error_context) VALUES(?,?,?,?,'warning',?,'open','[]',0,?,?,1,?,?)",
                    (
                        nid,
                        now,
                        now,
                        context["family"],
                        title,
                        now,
                        now,
                        error_key,
                        json.dumps(context, sort_keys=True),
                    ),
                )
            return self._error_row(
                self._conn.execute(
                    "SELECT * FROM notifications WHERE notification_id=?", (nid,)
                ).fetchone()
            )

    def error_patch(self, notification_id, patch, *, expected_operation_revision=None):
        allowed = {
            "seen_at",
            "ack_at",
            "desired_notice_revision",
            "enqueued_notice_revision",
            "notice_ids",
            "notice_intent",
            "disposition",
        }
        if set(patch) - allowed:
            raise ValueError("invalid_request")
        with self._lock, self._conn:
            self._require_open()
            row = self._conn.execute(
                "SELECT error_context FROM notifications WHERE notification_id=?",
                (notification_id,),
            ).fetchone()
            if not row:
                raise ValueError("not_found")
            if not row[0]:
                raise ValueError("read_only")
            ctx = json.loads(row[0])
            if (
                expected_operation_revision is not None
                and ctx["operation_revision"] != expected_operation_revision
            ):
                return False
            ctx.update(patch)
            self._conn.execute(
                "UPDATE notifications SET error_context=? WHERE notification_id=?",
                (json.dumps(ctx, sort_keys=True), notification_id),
            )
            return True

    def error_cutover(self):
        with self._lock, self._conn:
            self._require_open()
            self._conn.execute(
                "INSERT OR IGNORE INTO notification_markers(marker,created_at) VALUES ('error_alerts_v1',?)",
                (stamp(),),
            )
            return self._conn.execute(
                "SELECT created_at FROM notification_markers WHERE marker='error_alerts_v1'"
            ).fetchone()[0]
