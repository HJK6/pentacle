"""Bounded, isolated recovery proof. Never point this at live daemon state.

The caller supplies a disposable private copy. No provider, tmux, fleet roster,
producer or push listener is started. Only sanitized predicates leave the copy.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager, closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "services" / "chat-stream-v2"), str(ROOT / "services")]

PRODUCER_KEYS = (
    "PENTACLE_SYSTEM_PRODUCER_STREAM_TOKEN", "PENTACLE_SYSTEM_PRODUCER_STREAM_TOKEN_FILE",
    "PENTACLE_SYSTEM_PRODUCER_STREAM_ID", "PENTACLE_BOT_MESSAGING_STREAM_TOKEN_FILE",
    "PENTACLE_WMI_BACKUP_STREAM_TOKEN_FILE", "PENTACLE_REPORT_PRODUCER_CONFIG",
    "PENTACLE_DOT_TLS_PORT", "PENTACLE_DOT_TLS_CERT", "PENTACLE_DOT_TLS_KEY", "PENTACLE_DOT_TLS_BINDS",
)
DRILL_HOST, DRILL_SESSION = "drill-fd", "assistant"
DRILL_STREAM = f"{DRILL_HOST}:{DRILL_SESSION}"
HISTORY_LIMIT = 8  # One supported page; pagination overfetch is tracked separately.
REPORT_KEYS = ("report_id", "status", "from_stream_id", "provenance_qualified_spec_ids", "created_at")


class DrillRefused(RuntimeError):
    pass


class DrillFailed(RuntimeError):
    """A named predicate failed; never include a private RPC frame in errors."""


def require(predicate, name):
    if not predicate:
        raise DrillFailed(name)


def preflight(env, roster) -> None:
    if any(env.get(key) for key in PRODUCER_KEYS):
        raise DrillRefused("producer_or_push_key_present")
    if roster:
        raise DrillRefused("real_host_roster_present")


def _rows(db, sql, args=()):
    with closing(sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, args)]


def _direct(db, sql, args=()):
    rows = _rows(db, sql, args)
    return tuple(rows[0].values()) if rows else None


def validate_copy(state_dir: Path) -> dict:
    """Full checks before Store startup/migration or any fixture credential write.

    Named references are mandatory live pointers, not historical session identities:
    history/reports can legitimately outlive their original seat. Cross-DB atomicity
    is not asserted. An absent component, corrupt bytes or missing pointer fails.
    """
    root = state_dir.absolute()
    require(root == root.resolve() and root.is_dir() and root.stat().st_mode & 0o077 == 0,
            "copy:private_scratch_path")
    result = {}
    for name in ("sessions", "notifications", "assets", "schedules"):
        db = state_dir / f"{name}.db"
        require(db.is_file() and not db.is_symlink(), f"{name}:missing_component")
        require(db.stat().st_nlink == 1, f"{name}:copy_alias")
        with closing(sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchall()
            foreign = conn.execute("PRAGMA foreign_key_check").fetchall()
        result[name] = {"integrity_check": integrity == [("ok",)], "foreign_key_violations": len(foreign)}
        require(result[name]["integrity_check"] and not foreign, f"{name}:integrity_or_foreign_key")
    db = state_dir / "sessions.db"
    queries = {
        "binding_generation": "SELECT COUNT(*) FROM v2_assistant_direct_binding b LEFT JOIN v2_session_generations s "
            "ON b.stream_id=s.host||':'||s.session_name AND b.generation=s.generation WHERE s.host IS NULL",
        "lane_events": "SELECT COUNT(*) FROM v2_work_lane_events e LEFT JOIN v2_assistant_composite_lanes l "
            "ON e.lane_id=l.lane_id WHERE l.lane_id IS NULL",
        "attachment_receipts": "SELECT COUNT(*) FROM v2_attachment_refs r LEFT JOIN v2_attachment_uploads u "
            "ON r.upload_id=u.upload_id AND r.blob_sha=u.blob_sha AND u.state='ready' WHERE u.upload_id IS NULL",
    }
    result["references"] = {}
    for name, sql in queries.items():
        missing = _direct(db, sql)[0]
        result["references"][name] = {"missing": missing}
        require(missing == 0, f"reference:{name}")
    # Assets have a compound key rather than a declared FK.
    missing = _direct(state_dir / "assets.db", "SELECT COUNT(*) FROM asset_comments c LEFT JOIN assets a "
        "ON c.host=a.host AND c.session_name=a.session_name AND c.asset_id=a.asset_id WHERE a.asset_id IS NULL")[0]
    result["references"]["asset_comments"] = {"missing": missing}
    require(missing == 0, "reference:asset_comments")
    pointers = [("blobs", row["blob_sha"], row["size_bytes"]) for row in _rows(db,
        "SELECT DISTINCT blob_sha,size_bytes FROM v2_attachment_uploads WHERE state='ready'")]
    # Legacy and managed transcript attachments both use content-addressed keys.
    for row in _rows(db, "SELECT DISTINCT json_extract(a.value,'$.key') AS sha, "
            "json_extract(a.value,'$.size') AS size FROM session_event_tail e, "
            "json_each(e.event_json,'$.attachments') a WHERE json_extract(a.value,'$.key') IS NOT NULL"):
        pointers.append(("blobs", row["sha"], row["size"]))
    for row in _rows(db, "SELECT DISTINCT prompt_blob_id FROM v2_schedules WHERE prompt_blob_id IS NOT NULL"):
        pointers.append(("blobs", row["prompt_blob_id"], None))
    for directory, sha, size in pointers:
        require(isinstance(sha, str) and re.fullmatch(r"[a-f0-9]{64}", sha), "reference:invalid_content_key")
        path = state_dir / directory / sha[:2] / sha
        require(path.is_file() and not path.is_symlink() and not path.parent.is_symlink(), "reference:missing_content")
        require(size is None or path.stat().st_size == size, "reference:content_size")
        with path.open("rb") as source:
            require(hashlib.file_digest(source, "sha256").hexdigest() == sha, "reference:content_digest")
    result["references"]["content_addressed_files"] = {"checked": len(pointers), "missing": 0}
    return result


def prepare_archive(state_dir: Path, source_archive_path: Path | None = None, *, source_absent: bool = False) -> dict:
    """Validate only copied bytes, then rewrite one working-copy column pre-Store.

    The FD-supplied source literal is an equality check, never an open target.
    Fresh disk fixtures have a local path already; ready=0/missing means only
    local bootstrap is permitted. A required captured archive is never invented.
    """
    from retention import _archive_schema, history_union_sql, HISTORY_TABLES, RetentionError
    root = state_dir.absolute()
    require(root == root.resolve() and root.is_dir(), "archive:scratch_path_escape")
    require(root.stat().st_mode & 0o077 == 0, "archive:scratch_not_private")
    db, archive = root / "sessions.db", root / "sessions_archive.db"
    require(db.stat().st_nlink == 1, "archive:sessions_alias")
    sequence = _rows(db, "SELECT * FROM v2_send_receipt_sequence WHERE singleton=1")
    require(len(sequence) == 1, "archive:sequence_required")
    original = sequence[0]
    stored = original["archive_path"]
    require(isinstance(stored, str) and bool(stored), "archive:disk_store_path_required")
    claimed = Path(stored)
    require(claimed.is_absolute() and ".." not in claimed.parts, "archive:source_path_escape")
    if source_archive_path is not None:
        approved = Path(source_archive_path)
        require(approved.is_absolute() and ".." not in approved.parts and claimed == approved,
                "archive:source_path_not_approved")
        require(approved != archive, "archive:source_copy_alias")
    else:
        require(claimed == archive, "archive:source_path_not_approved")
    require(not archive.is_symlink(), "archive:copy_path_escape")
    present = archive.is_file()
    require(present or not original["archive_ready"], "archive:required_copy_missing")
    require(present or source_archive_path is None or source_absent, "archive:absence_not_qualified")
    require(not source_absent or not present, "archive:source_absence_contradiction")
    receipt = {"mode": "paired" if present else "rewrite_only", "integrity_check": None,
        "foreign_key_violations": 0, "rows": {}, "only_archive_path_changed": True}
    if present:
        require(archive.stat().st_nlink == 1 and not os.path.samefile(db, archive), "archive:copy_alias")
        with closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)) as hot, \
                closing(sqlite3.connect(f"{archive.as_uri()}?mode=ro", uri=True)) as cold:
            hot.row_factory = cold.row_factory = sqlite3.Row
            receipt["integrity_check"] = [tuple(row) for row in cold.execute("PRAGMA integrity_check")] == [("ok",)]
            foreign = cold.execute("PRAGMA foreign_key_check").fetchall()
            receipt["foreign_key_violations"] = len(foreign)
            require(receipt["integrity_check"] and not foreign, "archive:integrity_or_foreign_key")
            try:
                _archive_schema(hot, cold, initialize=False, pair_id=original["pair_id"])
                hot.create_function("history_identity_conflict", 0,
                    lambda: (_ for _ in ()).throw(ValueError("archive_identity_conflict")))
                hot.execute("ATTACH DATABASE ? AS receipt_archive", (archive.as_uri() + "?mode=ro",))
                for table in HISTORY_TABLES:
                    # Existing union checks BOTH arms for conflicting durable identities.
                    union = history_union_sql(hot, table, "receipt_archive")
                    receipt["rows"][table] = hot.execute(f"SELECT COUNT(*) FROM ({union})").fetchone()[0]
                hot_max = hot.execute("SELECT COALESCE(MAX(rowid),0) FROM v2_send_receipts").fetchone()[0]
                cold_max = cold.execute("SELECT COALESCE(MAX(receipt_rowid),0) FROM v2_send_receipts").fetchone()[0]
                require(max(original["high_water"], hot_max) >= cold_max, "archive:receipt_high_water_gap")
            except (sqlite3.Error, ValueError, RuntimeError, RetentionError) as exc:
                raise DrillFailed("archive:pair_schema_or_identity") from exc
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        current = dict(conn.execute("SELECT * FROM v2_send_receipt_sequence WHERE singleton=1").fetchone())
        require(current == original, "archive:sequence_changed")
        conn.execute("UPDATE v2_send_receipt_sequence SET archive_path=? WHERE singleton=1", (str(archive),))
        rewritten = dict(conn.execute("SELECT * FROM v2_send_receipt_sequence WHERE singleton=1").fetchone())
        require(rewritten == {**original, "archive_path": str(archive)}, "archive:unexpected_column_change")
        conn.commit()
    return receipt


@asynccontextmanager
async def _connection(port, stream, token, operator=None):
    import websockets
    async with websockets.connect(f"ws://127.0.0.1:{port}", max_size=None) as socket:
        welcome = json.loads(await socket.recv())
        require(welcome.get("type") == "welcome", "socket:welcome")
        async def rpc(kind, request_id, **fields):
            await socket.send(json.dumps({"type": kind, "request_id": request_id, **fields}))
            events = []
            async with asyncio.timeout(10):
                while True:
                    frame = json.loads(await socket.recv())
                    if frame.get("request_id") != request_id:
                        continue
                    if kind == "request_stream_events":
                        page = frame.get("events", [])
                        page_ids = [event.get("daemon_seq") for event in page]
                        require(all(isinstance(seq, int) for seq in page_ids) and page_ids == sorted(page_ids),
                                "rpc:history_frame_order")
                        events.extend(page)
                        if frame.get("type") == "request_stream_events.chunk":
                            continue
                        require(len({event["daemon_seq"] for event in events}) == len(events), "rpc:history_duplicate")
                        frame["events"] = sorted(events, key=lambda event: event["daemon_seq"])
                    return frame
        proof = {}
        if operator is not None:
            from _shared import operator_auth
            proof["auth_v2"] = {"scheme": operator_auth.AUTH_SCHEME, "credential_id": operator["credential_id"],
                "proof": operator_auth.make_proof(operator["proof_key"], welcome["auth"]["operator"]["nonce"],
                    operator["credential_id"], operator["client_kind"])}
        hello = await rpc("hello", "hello", client="pentacle" if operator else "recovery-drill",
            stream_token=token, from_stream_id=stream, **proof,
            capabilities={"work_lanes_v1": True, "assistant_composite_v1": True},
            subscribe={"snapshot": False, "mode": "rpc", "include_subagents": True})
        require(hello.get("type") in {"hello", "ready"}, "socket:hello")
        yield rpc


async def _projections(rpc, expected, composite_stream):
    history = await rpc("request_stream_events", "history", stream_id=composite_stream, limit=HISTORY_LIMIT)
    require(history.get("type") == "request_stream_events.ok", "rpc:history_type")
    ids = [event.get("daemon_seq") for event in history["events"]]
    require(ids == expected["history"], "rpc:history_identity_order")
    for report in expected["reports"]:
        frame = await rpc("work.report.get", "report:" + report["report_id"], report_id=report["report_id"])
        require(frame.get("type") == "work.report.get.ok", "rpc:report_type")
        require({key: frame.get(key) for key in REPORT_KEYS} == report, "rpc:report_projection")
        require(set(frame) <= {*REPORT_KEYS, "type", "request_id"}, "rpc:report_payload_leak")
    lanes = []
    cursor = {}
    while True:
        frame = await rpc("work_lanes.list", "lanes", include_done=True, limit=200, **cursor)
        require(frame.get("type") == "work_lanes.list.ok", "rpc:lanes_type")
        lanes.extend(frame["lanes"])
        if not frame.get("next_before_lane_id"):
            break
        cursor = {"before_lane_id": frame["next_before_lane_id"], "before_updated_at": frame["next_before_updated_at"]}
    require({lane["lane_id"] for lane in lanes} == {lane["lane_id"] for lane in expected["lanes"]}, "rpc:lane_inventory")
    for lane in expected["lanes"]:
        frame = await rpc("work_lanes.show", "lane:" + lane["lane_id"], lane_id=lane["lane_id"])
        require(frame.get("type") == "work_lanes.show.ok", "rpc:lane_type")
        require(all(frame["lane"].get(key) == lane[key] for key in ("lane_id", "work_state", "members")),
                "rpc:lane_members_state")
        projection = frame.get("projection")
        require(isinstance(projection, dict) and projection.get("lane_id") == lane["lane_id"], "rpc:lane_projection")
    return {"history_events": len(ids), "reports": len(expected["reports"]), "lanes": len(lanes)}


def _expectations(db, composite_stream):
    # Read the untouched copied source before Store startup. Do not derive an
    # expected projection from the same cache/consumer being checked.
    reports = _rows(db, "SELECT * FROM v2_reports ORDER BY ledger_row_id DESC LIMIT 1")
    for report in reports:
        raw = report.get("provenance_qualified_spec_ids")
        report["provenance_qualified_spec_ids"] = json.loads(raw) if raw else None
    lanes = _rows(db, "SELECT lane_id,work_state,members_json FROM v2_assistant_composite_lanes "
        "WHERE work_state IN ('active','paused','blocked','done')")
    for lane in lanes:
        lane["members"] = json.loads(lane.pop("members_json") or "[]")
    return {
        "history": [row["event_id"] for row in _rows(db,
            "SELECT event_id FROM (SELECT t.event_id FROM session_event_tail t JOIN sessions s "
            "ON t.stream_id=s.host||':'||s.session_name AND t.session_created_at=s.created_at "
            "WHERE t.stream_id=? ORDER BY t.event_id DESC LIMIT 8) ORDER BY event_id", (composite_stream,))],
        "reports": [{key: report.get(key) for key in REPORT_KEYS} for report in reports],
        "lanes": lanes,
    }


async def run_drill(state_dir: Path, *, composite_stream: str, binding_name: str, env=None, roster=(),
                    remove_state_dir: bool = True, source_archive_path: Path | None = None, archive_source_absent: bool = False) -> dict:
    preflight(os.environ if env is None else env, roster)
    from assistant_composite import AssistantComposite, AssistantCompositeConfig
    from _shared import operator_auth
    from server import Server
    from ledger import Ledger
    from sessions import Sessions
    from store import STREAM_TOKEN_HASH_VERSION, Store
    from work_lanes_projection import WorkLanesInventory

    started = time.monotonic()
    db = state_dir / "sessions.db"
    result = {"composite_stream": composite_stream}
    store = composite = server = None
    ports = []
    primary_error = None
    async def cleanup():
        try:
            if composite is not None:
                await composite.stop()
        finally:
            try:
                if server is not None:
                    await server.close()
            finally:
                if store is not None:
                    require(store.stop(), "cleanup:store_worker")
                result["port_closed"] = True
                for port in ports:
                    try:
                        _, writer = await asyncio.open_connection("127.0.0.1", port)
                    except OSError:
                        continue
                    writer.close()
                    await writer.wait_closed()
                    result["port_closed"] = False
                if remove_state_dir:
                    shutil.rmtree(state_dir)
                    result["state_dir_removed"] = not state_dir.exists()
                require(result["port_closed"], "cleanup:listener")
    try:
        result["integrity"] = validate_copy(state_dir)
        captured_expected = _expectations(db, composite_stream)
        result["archive"] = prepare_archive(state_dir, source_archive_path, source_absent=archive_source_absent)
        result["direct_reports"] = _direct(db, "SELECT COUNT(*) FROM v2_reports")[0]
        result["direct_lane_events"] = _direct(db, "SELECT COUNT(*) FROM v2_work_lane_events")[0]
        bound = _direct(db, "SELECT stream_id,generation FROM v2_assistant_direct_binding WHERE name=?", (binding_name,))
        require(bound, "binding:missing_current_holder")
        holder, generation = bound
        token, stale_token, drill_token = (os.urandom(32).hex() for _ in range(3))
        store = Store(str(db), archive_path=str(state_dir.absolute() / "sessions_archive.db"))
        store.start()
        changed = await store.submit(lambda conn: conn.execute(
            "UPDATE sessions SET token_hash=?,token_hash_version=? WHERE host=? AND session_name=? AND status='open'",
            (hashlib.sha256(token.encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION, *holder.split(":", 1))).rowcount)
        await store.submit(lambda conn: conn.commit())
        require(changed == 1, "fixture:holder_token")
        result["holder_token_replaced_in_copy"] = True
        drill = await store.open_session(DRILL_HOST, DRILL_SESSION, provider="codex", role="assistant",
            effective_model="gpt-6-sol", effective_effort="high", pane_status="pane_alive")
        require(await store.grant_stream_token(DRILL_HOST, DRILL_SESSION, hashlib.sha256(stale_token.encode()).hexdigest(),
            STREAM_TOKEN_HASH_VERSION) == "ok", "fixture:old_token")
        await store.submit(lambda conn: conn.execute(
            "UPDATE sessions SET token_hash=? WHERE host=? AND session_name=?",
            (hashlib.sha256(drill_token.encode()).hexdigest(), DRILL_HOST, DRILL_SESSION)))
        await store.submit(lambda conn: conn.commit())
        settings = {"PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1", "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": composite_stream,
            "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": holder, "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": generation}
        dispatched = []
        async def dispatch(route):
            dispatched.append(route["dispatch_id"])
            return {"delivery": "landed"}
        registry = operator_auth.OperatorCredentialRegistry(state_dir / "fixture-operator.json")
        registry.initialize()
        _, envelope = registry.issue("pentacle", label="Isolated recovery fixture")
        operator = operator_auth.decode_envelope(envelope)
        config = AssistantCompositeConfig.from_env(settings, name=binding_name)
        async def start_server():
            nonlocal composite, server
            composite = AssistantComposite(store, config=config, dispatch=dispatch)
            await composite.load_binding()
            await composite.ensure_projection()
            sessions = Sessions(store, local_host=composite_stream.split(":", 1)[0])
            await sessions.refresh()
            server = Server(host="127.0.0.1", port=0, store=store, sessions=sessions,
                local_host=composite_stream.split(":", 1)[0], dot_tls_port=0)
            server.assistant_composite = composite
            server.operator_credential_registry = registry
            server.work_lanes = WorkLanesInventory(store, sessions, server.broadcast)
            ports.append(await server.bind())
            return ports[-1]
        port = await start_server()
        expected = _expectations(db, composite_stream)
        result["captured_rpc"] = None
        async with _connection(port, holder, token) as rpc:
            result["captured_rpc"] = await _projections(rpc, captured_expected, composite_stream)
        # Missing production tuples get copy-only deterministic controls, labelled separately.
        seeded = []
        if not expected["reports"]:
            await store.put_report({"report_id": "drill-fixture-report", "from_stream_id": DRILL_STREAM,
                "to_stream_id": holder, "msg_id": 0, "status": "done", "summary": "Fixture",
                "findings": [], "next_action": "Fixture", "ingested_at": "2026-01-01T00:00:00Z"},
                routing_snapshot=await Ledger(store, sessions=server.sessions)._report_routing_snapshot(DRILL_STREAM),
                announce=False)
            seeded.append("report")
        if not expected["lanes"]:
            await composite.operation({"composite_stream_id": composite_stream, "operation": "work_lane.adopt", "request_id": "drill-fixture-lane",
                "payload": {"adoption_key": "request:drill-fixture", "title": "Recovery fixture", "summary": "Fixture",
                    "work_state": "paused", "owner_kind": "operator", "visible_chat": {"stream_id": composite_stream},
                    "no_spec_reason": "Isolated recovery fixture"},
                "_auth_context": {"token_verified": True, "stream_id": holder, "session_generation": generation}},
                actor_stream_id=holder)
            seeded.append("lane")
        expected = _expectations(db, composite_stream)
        result["seeded_controls"] = seeded
        async with _connection(port, holder, token) as rpc:
            result["rpc"] = await _projections(rpc, expected, composite_stream)
            before = await rpc("assistant.binding", "binding")
            for foreign, identity, credential in (("foreign", DRILL_STREAM, drill_token), ("stale", DRILL_STREAM, stale_token)):
                async with _connection(port, identity, credential) as refused_rpc:
                    refused = await refused_rpc("assistant.rebind", foreign, expected_revision=before.get("revision") or 0,
                        target_stream_id=DRILL_STREAM, target_generation=drill["session_generation"])
                    require(refused.get("type") != "assistant.rebind.ok" and bool(refused.get("error_code")),
                            "auth:" + foreign + "_rebind_refused")
            require((await rpc("assistant.binding", "unchanged")).get("revision") == before.get("revision"),
                    "auth:zero_binding_mutations")
            moved = await rpc("assistant.rebind", "rebind", expected_revision=before.get("revision") or 0,
                target_stream_id=DRILL_STREAM, target_generation=drill["session_generation"])
            require(moved.get("type") == "assistant.rebind.ok", "rebind:current_holder")
            result["binding_after"] = moved["new_binding"]
            result["rebind_caller"] = holder
            # Ordinary public send on the isolated loopback client; recording dispatcher only.
            async with _connection(port, holder, token, operator=operator) as operator_rpc:
                sent = await operator_rpc("send", "send", stream_id=composite_stream, text="recovery drill ping",
                    optimistic_id="drill-input")
                require(not sent.get("error_code"), "rpc:send")
        route = None
        async with asyncio.timeout(5):
            while not route or route["routing_state"] != "resolved":
                route = await store.get_assistant_composite_route(stream_id=composite_stream, input_identity="drill-input")
                await asyncio.sleep(.01)
        require(route["route_target"] == DRILL_STREAM and len(dispatched) == 1, "route:single_fixture_dispatch")
        async with _connection(port, DRILL_STREAM, drill_token) as rpc:
            fields = dict(composite_stream_id=composite_stream, dispatch_id=route["dispatch_id"],
                reply_to_message_id="drill-input", publish_kind="prose", response_state="final", message="recovery drill reply",
                attachment_ids=[], evidence_refs=[])
            for _ in range(2):
                published = await rpc("assistant.publish", "publish:" + route["dispatch_id"], **fields)
                require(published.get("type") == "assistant.publish.ok", "publish:correlated_retry")
        result["time_to_first_reply_s"] = round(time.monotonic() - started, 3)
        tail = await store.fetch_session_event_tail(composite_stream, limit=10)
        require(sum(event.get("text") == "recovery drill reply" for event in tail) == 1, "publish:exactly_one_final")
        expected = _expectations(db, composite_stream)
        # Real persisted Store and Server restart, then a new authenticated connection.
        await composite.stop()
        await server.close()
        require(store.stop(), "restart:store_worker")
        store = Store(str(db), archive_path=str(state_dir.absolute() / "sessions_archive.db"))
        store.start()
        port = await start_server()
        async with _connection(port, DRILL_STREAM, drill_token) as rpc:
            result["restart_rpc"] = await _projections(rpc, expected, composite_stream)
            binding = await rpc("assistant.binding", "restart-binding")
            require(binding.get("stream_id") == DRILL_STREAM, "restart:binding")
        result["foreign_stale_refused"] = True
        result["final_replies"] = 1
        result["external_dispatches"] = 0
        result["lanes_reports_nonempty"] = bool(result["rpc"]["reports"] and result["rpc"]["lanes"])
        return result
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        task = asyncio.create_task(cleanup())
        cancelled_during_cleanup = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled_during_cleanup = True
            except BaseException:
                break
        try:
            task.result()
        except BaseException as cleanup_error:
            if primary_error is not None:
                raise BaseExceptionGroup("drill and cleanup failed", [primary_error, cleanup_error])
            raise
        if cancelled_during_cleanup:
            if primary_error is not None:
                raise BaseExceptionGroup("drill failed and cleanup was cancelled", [primary_error, asyncio.CancelledError()])
            raise asyncio.CancelledError()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state_dir", type=Path)
    parser.add_argument("--composite-stream", required=True)
    parser.add_argument("--binding-name", required=True)
    parser.add_argument("--source-archive-path", type=Path, help="FD-approved source literal; equality check only, never opened")
    parser.add_argument("--archive-source-absent", action="store_true",
        help="capture manifest proves no source archive file; ready=0 copy bootstrap only")
    args = parser.parse_args()
    try:
        result = asyncio.run(run_drill(args.state_dir, composite_stream=args.composite_stream, binding_name=args.binding_name,
            source_archive_path=args.source_archive_path, archive_source_absent=args.archive_source_absent))
    except (DrillFailed, DrillRefused) as exc:
        print(json.dumps({"passed": False, "predicate": str(exc)}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
