"""Content-free voice milestones in sessions.db; no message or audio queue."""

from __future__ import annotations
import hashlib
import json
import time
from datetime import datetime, timezone

DDL = """CREATE TABLE IF NOT EXISTS voice_operations (
 principal TEXT NOT NULL, operation_id TEXT NOT NULL,
 origin_stream_id TEXT NOT NULL, origin_generation TEXT NOT NULL,
 operation_kind TEXT NOT NULL, client_build TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, projected_revision INTEGER NOT NULL DEFAULT 0,
 highest_sequence INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 data TEXT NOT NULL DEFAULT '{}', events TEXT NOT NULL DEFAULT '{}',
 PRIMARY KEY(principal,operation_id))"""


def iso(epoch=None):
    return (
        datetime.fromtimestamp(time.time() if epoch is None else epoch, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def decode(row):
    if row is None:
        return None
    row = dict(row)
    for key in ("data", "events"):
        row[key] = json.loads(row[key])
    return row


def _register(conn, principal, intent, now):
    row = conn.execute(
        "SELECT * FROM voice_operations WHERE principal=? AND operation_id=?",
        (principal, intent["operation_id"]),
    ).fetchone()
    if row:
        for k in ("origin_stream_id", "origin_generation", "operation_kind"):
            if row[k] != intent[k]:
                raise ValueError("origin_mismatch")
        return decode(row)
    host, sep, name = intent["origin_stream_id"].partition(":")
    session = (
        conn.execute(
            "SELECT s.*,g.generation AS session_generation FROM sessions s LEFT JOIN v2_session_generations g ON g.host=s.host AND g.session_name=s.session_name WHERE s.host=? AND s.session_name=?",
            (host, name),
        ).fetchone()
        if sep
        else None
    )
    if (
        not session
        or session["status"] != "open"
        or session["session_generation"] != intent["origin_generation"]
    ):
        raise ValueError("origin_mismatch")
    conn.execute(
        "INSERT INTO voice_operations(principal,operation_id,origin_stream_id,origin_generation,operation_kind,client_build,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            principal,
            intent["operation_id"],
            intent["origin_stream_id"],
            intent["origin_generation"],
            intent["operation_kind"],
            intent["client_build"],
            now,
            now,
        ),
    )
    return decode(
        conn.execute(
            "SELECT * FROM voice_operations WHERE principal=? AND operation_id=?",
            (principal, intent["operation_id"]),
        ).fetchone()
    )


def _save(conn, row, now):
    conn.execute(
        "UPDATE voice_operations SET data=?,events=?,revision=?,highest_sequence=?,updated_at=? WHERE principal=? AND operation_id=?",
        (
            json.dumps(row["data"], sort_keys=True),
            json.dumps(row["events"], sort_keys=True),
            row["revision"],
            row["highest_sequence"],
            now,
            row["principal"],
            row["operation_id"],
        ),
    )


class VoiceOperationsStoreMixin:
    async def voice_register(
        self, principal, intent, *, upload_request_id=None, now=None
    ):
        now = now or iso()

        def op(conn):
            with conn:
                row = _register(conn, principal, intent, now)
                if upload_request_id:
                    prior = row["data"].get("upload_request_id")
                    if prior and prior != upload_request_id:
                        raise ValueError("event_conflict")
                    conflict = conn.execute(
                        "SELECT principal,operation_id FROM voice_operations WHERE json_extract(data,'$.upload_request_id')=?",
                        (upload_request_id,),
                    ).fetchone()
                    if conflict and (
                        conflict["principal"],
                        conflict["operation_id"],
                    ) != (principal, intent["operation_id"]):
                        raise ValueError("operation_forbidden")
                    if not prior:
                        row["data"]["upload_request_id"] = upload_request_id
                        row["revision"] += 1
                        _save(conn, row, now)
                return row

        return await self.submit(op)

    async def voice_get(self, principal, operation_id):
        return await self.submit(
            lambda conn: decode(
                conn.execute(
                    "SELECT * FROM voice_operations WHERE principal=? AND operation_id=?",
                    (principal, operation_id),
                ).fetchone()
            )
        )

    async def voice_list(self):
        return await self.submit(
            lambda conn: [
                decode(r)
                for r in conn.execute(
                    "SELECT * FROM voice_operations ORDER BY created_at,operation_id"
                )
            ]
        )

    async def voice_milestone(
        self,
        principal,
        operation_id,
        stage,
        *,
        blob_sha=None,
        request_id=None,
        code=None,
        now=None,
    ):
        if stage not in (
            "upload_committed",
            "transcribe_started",
            "transcribe_result",
            "transcribe_failed",
            "send_committed",
            "send_proved",
        ):
            raise ValueError("invalid_request")
        now = now or iso()

        def op(conn):
            with conn:
                row = decode(
                    conn.execute(
                        "SELECT * FROM voice_operations WHERE principal=? AND operation_id=?",
                        (principal, operation_id),
                    ).fetchone()
                )
                if row is None:
                    raise ValueError("operation_forbidden")
                data = row["data"]
                if blob_sha:
                    if data.get("blob_sha") and data["blob_sha"] != blob_sha:
                        raise ValueError("blob_forbidden")
                    if stage != "upload_committed" and (
                        not data.get("upload_committed")
                        or data.get("blob_sha") != blob_sha
                    ):
                        raise ValueError("blob_forbidden")
                    data["blob_sha"] = blob_sha
                if stage.startswith("transcribe") and request_id:
                    if (
                        data.get("transcribe_request_id")
                        and data["transcribe_request_id"] != request_id
                    ):
                        raise ValueError("event_conflict")
                    data["transcribe_request_id"] = request_id
                if stage.startswith("send") and request_id:
                    if request_id not in data.get(
                        "server_send_request_ids", [data.get("server_send_request_id")]
                    ):
                        raise ValueError("operation_forbidden")
                    data["send_request_id"] = request_id
                if stage not in data:
                    data[stage] = now
                    if code:
                        data[stage + "_code"] = code
                    row["revision"] += 1
                    _save(conn, row, now)
                return row

        return await self.submit(op)

    async def voice_report(self, principal, frame, *, now_epoch=None):
        epoch = time.time() if now_epoch is None else now_epoch
        now = iso(epoch)
        payload = {
            k: v
            for k, v in frame.items()
            if k not in ("request_id", "type") and not k.startswith("_")
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        def op(conn):
            with conn:
                # Event identity is principal-wide, including operation reassignment.
                prior = conn.execute(
                    "SELECT operation_id, json_extract(events, ?) AS event FROM voice_operations WHERE principal=? AND json_type(events, ?) IS NOT NULL",
                    (
                        '$."' + frame["event_id"] + '"',
                        principal,
                        '$."' + frame["event_id"] + '"',
                    ),
                ).fetchone()
                if prior:
                    event = json.loads(prior["event"])
                    if (
                        event["digest"] != digest
                        or prior["operation_id"] != frame["operation_id"]
                    ):
                        raise ValueError("event_conflict")
                    return {
                        "event_id": frame["event_id"],
                        "operation_id": frame["operation_id"],
                        "sequence": event["sequence"],
                        "duplicate": True,
                        "accepted_at": event["at"],
                    }
                count = conn.execute(
                    "SELECT COUNT(*) FROM voice_operations v,json_each(v.events) e WHERE v.principal=? AND json_extract(e.value,'$.epoch')>?",
                    (principal, epoch - 60),
                ).fetchone()[0]
                if count >= 60:
                    raise ValueError("rate_limited")
                row = decode(
                    conn.execute(
                        "SELECT * FROM voice_operations WHERE principal=? AND operation_id=?",
                        (principal, frame["operation_id"]),
                    ).fetchone()
                )
                if row is None:
                    intent = frame.get("voice_operation")
                    if (
                        not intent
                        or intent["operation_id"] != frame["operation_id"]
                        or frame["code"] not in ("upload_read_failed", "report_gap")
                    ):
                        raise ValueError("operation_forbidden")
                    row = _register(conn, principal, intent, now)
                elif frame.get("voice_operation"):
                    if (
                        frame["voice_operation"]["operation_id"]
                        != frame["operation_id"]
                    ):
                        raise ValueError("operation_forbidden")
                    _register(conn, principal, frame["voice_operation"], now)
                if frame["sequence"] <= row["highest_sequence"]:
                    raise ValueError("stale_sequence")
                if frame.get("send_request_id"):
                    # Client correlation is evidence only, never a server receipt binding.
                    row["data"]["client_send_request_id"] = frame["send_request_id"]
                row["events"][frame["event_id"]] = {
                    "digest": digest,
                    "at": now,
                    "epoch": epoch,
                    "sequence": frame["sequence"],
                }
                row["highest_sequence"] = frame["sequence"]
                row["revision"] += 1
                data = row["data"]
                previous = data.get("client_event", {})
                # A later client failure cannot reopen a terminal take. A server
                # receipt still wins over cancellation in the projection.
                if frame["code"] != "report_gap" and (
                    previous.get("outcome") not in ("cancelled", "recovered")
                    or frame["outcome"] in ("cancelled", "recovered")
                ):
                    data["client_event"] = {
                        k: frame[k]
                        for k in ("stage", "outcome", "code", "retry_state", "sequence")
                    }
                    data["client_event"]["at"] = now
                if frame["code"] == "report_gap":
                    data["report_gap_count"] = data.get(
                        "report_gap_count", 0
                    ) + frame.get("dropped_count", 1)
                _save(conn, row, now)
                return {
                    "event_id": frame["event_id"],
                    "operation_id": frame["operation_id"],
                    "sequence": frame["sequence"],
                    "duplicate": False,
                    "accepted_at": now,
                }

        return await self.submit(op)

    async def voice_bind_send(self, principal, operation_id, request_id):
        def op(conn):
            with conn:
                row = decode(
                    conn.execute(
                        "SELECT * FROM voice_operations WHERE principal=? AND operation_id=?",
                        (principal, operation_id),
                    ).fetchone()
                )
                if not row:
                    raise ValueError("operation_forbidden")
                existing = row["data"].get("server_send_request_ids") or (
                    [row["data"]["server_send_request_id"]]
                    if row["data"].get("server_send_request_id")
                    else []
                )
                if request_id not in existing:
                    # Bind before submission so a lost ACK is recoverable from
                    # the ordinary send receipt. Client reports cannot set this.
                    row["data"]["server_send_request_ids"] = [*existing, request_id]
                    row["revision"] += 1
                    _save(conn, row, iso())

        return await self.submit(op)

    async def voice_prune(self, retained, now_epoch):
        """Preserve every retained alert's source evidence, including overdue rows."""

        def op(conn):
            with conn:
                removed = 0
                for row in conn.execute(
                    "SELECT principal,operation_id,updated_at,projected_revision,data FROM voice_operations"
                ).fetchall():
                    if (row["principal"], row["operation_id"]) in retained:
                        continue
                    days = 30 if row["projected_revision"] else 7
                    if row["updated_at"] >= iso(now_epoch - days * 86400):
                        continue
                    data = json.loads(row["data"])
                    terminal = (
                        data.get("send_proved")
                        or data.get("client_event", {}).get("outcome")
                        in ("cancelled", "recovered")
                        or not data.get("upload_committed")
                    )
                    if not terminal:
                        continue
                    conn.execute(
                        "DELETE FROM voice_operations WHERE principal=? AND operation_id=?",
                        (row["principal"], row["operation_id"]),
                    )
                    removed += 1
                return removed

        return await self.submit(op)

    async def voice_projected(self, principal, operation_id, revision):
        def op(conn):
            with conn:
                conn.execute(
                    "UPDATE voice_operations SET projected_revision=MAX(projected_revision,?) WHERE principal=? AND operation_id=?",
                    (revision, principal, operation_id),
                )

        return await self.submit(op)
