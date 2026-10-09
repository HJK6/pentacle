"""Additive structured error facts and bounded settings in notifications.db.

Transport state is never stored here. The delivery owner passes settled IDs to
retention after inspecting the existing outbox and linked digest receipts.
"""

from __future__ import annotations
import json
import uuid
from datetime import datetime, timezone, timedelta

FAMILIES = (
    "voice_operation.v1",
    "session_lifecycle",
    "integrity",
    "native_notice",
    "legacy_notification",
    "system_notification",
    "consent_security",
)


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
    conn.execute(
        "CREATE TABLE IF NOT EXISTS error_producer_settings (family TEXT PRIMARY KEY, revision INTEGER NOT NULL, delivery_mode TEXT NOT NULL, mute_until TEXT, updated_at TEXT, updated_by TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS error_settings_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, family TEXT NOT NULL, revision INTEGER NOT NULL, actor TEXT NOT NULL, at TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO error_producer_settings VALUES ('voice_operation.v1',0,'immediate',NULL,NULL,NULL)"
    )


class ErrorNotificationMixin:
    def error_rows(self):
        with self._lock:
            self._require_open()
            rows = self._conn.execute(
                "SELECT n.* FROM notifications n WHERE n.error_context IS NOT NULL OR (n.severity IN ('warning','critical') AND n.producer != 'agent_question.v1' AND n.producer NOT LIKE 'consent.%' AND NOT EXISTS (SELECT 1 FROM agent_questions q WHERE q.notification_id=n.notification_id))"
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
                    "INSERT INTO notifications(notification_id,created_at,updated_at,producer,severity,title,state,actions,ttl_seconds,first_fired_at,last_fired_at,firing_count,error_key,error_context) VALUES(?,?,?,'voice_operation.v1','warning',?,'open','[]',0,?,?,1,?,?)",
                    (
                        nid,
                        now,
                        now,
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

    def error_settings(self, *, now=None):
        now = now or stamp()
        with self._lock:
            self._require_open()
            row = dict(
                self._conn.execute(
                    "SELECT * FROM error_producer_settings WHERE family='voice_operation.v1'"
                ).fetchone()
            )
        return [self._setting(row, now)] + [
            self._setting({"family": f}, now) for f in FAMILIES[1:]
        ]

    @staticmethod
    def _setting(row, now):
        editable = row["family"] == "voice_operation.v1"
        mode = row.get("delivery_mode", "source_managed")
        until = row.get("mute_until")
        return {
            "family": row["family"],
            "editable": editable,
            "revision": row.get("revision", 0),
            "delivery_mode": mode,
            "effective_delivery_mode": "muted" if until and until > now else mode,
            "mute_until": until,
            "updated_at": row.get("updated_at"),
            "updated_by": row.get("updated_by"),
            "policy_description": (
                "Fixed voice milestones; current front desk binding"
                if editable
                else "Existing source controls delivery; this view does not change it"
            ),
        }

    def error_settings_set(
        self,
        *,
        actor,
        expected_revision,
        delivery_mode=None,
        mute_for_s=None,
        restore_defaults=False,
        now=None,
    ):
        now = now or stamp()
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("invalid_request")
        if delivery_mode not in (None, "immediate", "digest", "muted") or (
            mute_for_s is not None
            and (type(mute_for_s) is not int or mute_for_s not in (0, 900, 3600, 86400))
        ):
            raise ValueError("invalid_request")
        if restore_defaults and (delivery_mode is not None or mute_for_s is not None):
            raise ValueError("invalid_request")
        if not restore_defaults and delivery_mode is None and mute_for_s is None:
            raise ValueError("invalid_request")
        with self._lock, self._conn:
            self._require_open()
            before = dict(
                self._conn.execute(
                    "SELECT * FROM error_producer_settings WHERE family='voice_operation.v1'"
                ).fetchone()
            )
            if before["revision"] != expected_revision:
                raise ValueError("revision_conflict")
            after = {
                **before,
                "revision": expected_revision + 1,
                "updated_by": actor,
                "updated_at": now,
            }
            if restore_defaults:
                after.update(delivery_mode="immediate", mute_until=None)
            if delivery_mode is not None:
                after["delivery_mode"] = delivery_mode
            if mute_for_s is not None:
                after["mute_until"] = (
                    (
                        datetime.fromisoformat(now.replace("Z", "+00:00"))
                        + timedelta(seconds=mute_for_s)
                    )
                    .isoformat()
                    .replace("+00:00", "Z")
                    if mute_for_s
                    else None
                )
            self._conn.execute(
                "UPDATE error_producer_settings SET revision=?,delivery_mode=?,mute_until=?,updated_at=?,updated_by=? WHERE family=?",
                (
                    after["revision"],
                    after["delivery_mode"],
                    after["mute_until"],
                    now,
                    actor,
                    after["family"],
                ),
            )
            self._conn.execute(
                "INSERT INTO error_settings_audit(family,revision,actor,at,before_json,after_json) VALUES(?,?,?,?,?,?)",
                (
                    after["family"],
                    after["revision"],
                    actor,
                    now,
                    json.dumps(self._setting(before, now)),
                    json.dumps(self._setting(after, now)),
                ),
            )
            return self._setting(after, now)

    def error_settings_audit(self, *, before=None, limit=20):
        with self._lock:
            self._require_open()
            rows = self._conn.execute(
                "SELECT * FROM error_settings_audit WHERE (? IS NULL OR id<?) ORDER BY id DESC LIMIT ?",
                (before, before, limit + 1),
            ).fetchall()
        items = [
            {
                "id": str(r["id"]),
                "family": r["family"],
                "revision": r["revision"],
                "actor": r["actor"],
                "at": r["at"],
                "before": json.loads(r["before_json"]),
                "after": json.loads(r["after_json"]),
            }
            for r in rows[:limit]
        ]
        return items, str(rows[limit - 1]["id"]) if len(rows) > limit else None

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
