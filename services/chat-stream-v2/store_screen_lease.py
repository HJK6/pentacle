"""Atomic screen exclusion and durable notices on the existing Store worker."""

import hashlib
import json
import uuid

from store_assistant_binding import _binding_conn
from store_routing import _assistant_actor_conn, _insert_outbound_notice_conn

SCREEN_LEASE_DDL = (
    """CREATE TABLE IF NOT EXISTS v2_screen_leases (
    lease_id TEXT PRIMARY KEY, host TEXT NOT NULL, holder_stream_id TEXT NOT NULL,
    holder_label TEXT NOT NULL, reason TEXT NOT NULL, acquired_at REAL NOT NULL,
    expires_at REAL NOT NULL, renewed_at REAL, released_at REAL,
    release_reason TEXT CHECK(release_reason IN ('released','expired','forced','holder_closed')),
    released_by TEXT)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS v2_screen_one_active ON v2_screen_leases(host) WHERE released_at IS NULL",
    """CREATE TABLE IF NOT EXISTS v2_screen_lease_notices (
    lease_id TEXT NOT NULL, requester_stream_id TEXT NOT NULL, notified_at REAL NOT NULL,
    PRIMARY KEY(lease_id,requester_stream_id))""",
)


def release_holder_conn(conn, stream_id, now):
    conn.execute(
        "UPDATE v2_screen_leases SET released_at=?,release_reason=CASE WHEN expires_at<=? THEN 'expired' ELSE 'holder_closed' END "
        "WHERE holder_stream_id=? AND released_at IS NULL",
        (now, now, stream_id),
    )


def _notice(conn, kind, recipient, identity, data):
    nid = "screen:" + hashlib.sha256(identity.encode()).hexdigest()
    _insert_outbound_notice_conn(
        conn,
        notice_id=nid,
        tell_id=nid,
        kind=kind,
        dedupe_key=nid,
        recipient_stream_id=recipient,
        body="[" + kind + "] " + json.dumps(data, sort_keys=True),
        metadata=data,
    )


class ScreenLeaseStoreMixin:
    async def screen_lease_call(
        self, verb, *, hosts, message, actor=None, env_binding=None, now
    ):
        def body(conn):
            def error(code, **fields):
                return {"error_code": code, **fields}

            host = message.get("host")
            if host is not None:
                host = host.strip().lower() if isinstance(host, str) else ""
                if host not in hosts:
                    return error("screen_host_invalid")
            if verb == "status":
                result = []
                for name in ([host] if host is not None else hosts):
                    row = conn.execute(
                        "SELECT * FROM v2_screen_leases WHERE host=? AND released_at IS NULL AND expires_at>?",
                        (name, now),
                    ).fetchone()
                    fields = (
                        "lease_id",
                        "holder_stream_id",
                        "holder_label",
                        "reason",
                        "acquired_at",
                        "renewed_at",
                        "expires_at",
                    )
                    result.append(
                        {
                            "host": name,
                            "state": "held" if row else "free",
                            "lease": {k: row[k] for k in fields} if row else None,
                        }
                    )
                return {"hosts": result}
            # Check the current seat again inside the same write transaction as acquisition/close.
            ah, _, an = str(actor or "").partition(":")
            seat = conn.execute(
                "SELECT * FROM sessions WHERE host=? AND session_name=?", (ah, an)
            ).fetchone()
            token = message.get("stream_token")
            if (
                not seat
                or seat["status"] != "open"
                or not isinstance(token, str)
                or hashlib.sha256(token.encode()).hexdigest() != seat["token_hash"]
            ):
                return error("not_authenticated")
            binding = _binding_conn(conn, env_binding or {}, include_target=False)
            force = verb == "release" and bool(message.get("force"))
            if verb != "release" and message.get("force"):
                return error("invalid_request")
            if verb == "renew" and not isinstance(message.get("lease_id"), str):
                return error("invalid_request")
            if force:
                try:
                    if binding["stream_id"] != actor:
                        raise ValueError
                    _assistant_actor_conn(conn, actor, binding["generation"])
                except ValueError:
                    return error("screen_force_unauthorized")
            if verb in ("acquire", "renew"):
                ttl = message.get("ttl", 900)
                if type(ttl) is not int or not 1 <= ttl <= 3600:
                    return error("screen_ttl_invalid")
            if verb == "acquire" and (
                host is None
                or not isinstance(message.get("reason"), str)
                or not message["reason"].strip()
            ):
                return error("invalid_request")
            if verb == "release" and bool(message.get("lease_id")) == (
                host is not None
            ):
                return error("invalid_request")
            # Expired ids retain their terminal state after finalization.
            conn.execute(
                "UPDATE v2_screen_leases SET released_at=?,release_reason='expired' WHERE released_at IS NULL AND expires_at<=?",
                (now, now),
            )
            if verb == "acquire":
                row = conn.execute(
                    "SELECT * FROM v2_screen_leases WHERE host=? AND released_at IS NULL",
                    (host,),
                ).fetchone()
                if row and row["holder_stream_id"] != actor:
                    inserted = conn.execute(
                        "INSERT OR IGNORE INTO v2_screen_lease_notices VALUES (?,?,?)",
                        (row["lease_id"], actor, now),
                    ).rowcount
                    if inserted:
                        data = {
                            "host": host,
                            "holder_stream_id": row["holder_stream_id"],
                            "holder_label": row["holder_label"],
                            "holder_reason": row["reason"],
                            "requester_stream_id": actor,
                            "requester_reason": message["reason"],
                            "lease_id": row["lease_id"],
                            "expires_at": row["expires_at"],
                        }
                        if not binding["stream_id"]:
                            raise ValueError("screen_front_desk_unavailable")
                        _notice(
                            conn,
                            "screen_lease_contended",
                            binding["stream_id"],
                            row["lease_id"] + ":" + actor,
                            data,
                        )
                    return error(
                        "screen_held",
                        held_by=row["holder_stream_id"],
                        **{
                            k: row[k]
                            for k in (
                                "holder_label",
                                "reason",
                                "lease_id",
                                "expires_at",
                            )
                        }
                    )
                if row:
                    conn.execute(
                        "UPDATE v2_screen_leases SET renewed_at=?,expires_at=? WHERE lease_id=?",
                        (now, now + ttl, row["lease_id"]),
                    )
                    return {
                        **{
                            k: row[k]
                            for k in (
                                "lease_id",
                                "host",
                                "holder_stream_id",
                                "holder_label",
                                "reason",
                                "acquired_at",
                            )
                        },
                        "expires_at": now + ttl,
                        "renewed": True,
                    }
                lid = "lease-" + uuid.uuid4().hex
                label = seat["title"] or actor
                conn.execute(
                    "INSERT INTO v2_screen_leases (lease_id,host,holder_stream_id,holder_label,reason,acquired_at,expires_at) VALUES (?,?,?,?,?,?,?)",
                    (lid, host, actor, label, message["reason"], now, now + ttl),
                )
                return dict(
                    lease_id=lid,
                    host=host,
                    holder_stream_id=actor,
                    holder_label=label,
                    reason=message["reason"],
                    acquired_at=now,
                    expires_at=now + ttl,
                )
            lid = message.get("lease_id")
            row = (
                conn.execute(
                    "SELECT * FROM v2_screen_leases WHERE lease_id=?", (lid,)
                ).fetchone()
                if lid
                else conn.execute(
                    "SELECT * FROM v2_screen_leases WHERE host=? AND released_at IS NULL",
                    (host,),
                ).fetchone()
            )
            if not row or row["released_at"] is not None:
                state = (
                    ("expired" if row["release_reason"] == "expired" else "released")
                    if row
                    else ("unknown" if lid else "free")
                )
                return error(
                    "screen_lease_not_found",
                    **({"lease_id": lid} if lid else {"host": host}),
                    state=state
                )
            if row["holder_stream_id"] != actor and not force:
                return error(
                    "screen_lease_not_held",
                    lease_id=row["lease_id"],
                    held_by=row["holder_stream_id"],
                )
            lid, host = row["lease_id"], row["host"]
            if verb == "renew":
                conn.execute(
                    "UPDATE v2_screen_leases SET renewed_at=?,expires_at=? WHERE lease_id=?",
                    (now, now + ttl, lid),
                )
                return dict(
                    lease_id=lid,
                    host=host,
                    holder_stream_id=actor,
                    renewed_at=now,
                    expires_at=now + ttl,
                )
            reason = "forced" if force else "released"
            conn.execute(
                "UPDATE v2_screen_leases SET released_at=?,release_reason=?,released_by=? WHERE lease_id=?",
                (now, reason, actor, lid),
            )
            if reason == "forced":
                _notice(
                    conn,
                    "screen_lease_revoked",
                    row["holder_stream_id"],
                    lid + ":revoked",
                    dict(lease_id=lid, host=host, released_by=actor),
                )
            return dict(
                lease_id=lid,
                host=host,
                released_at=now,
                release_reason=reason,
                released_by=actor,
            )

        def op(conn):
            conn.execute("BEGIN IMMEDIATE" if verb != "status" else "BEGIN")
            try:
                result = body(conn)
                conn.commit()
                return result
            except BaseException:
                conn.rollback()
                raise

        return await self.submit(op)
