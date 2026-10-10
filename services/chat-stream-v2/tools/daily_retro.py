#!/usr/bin/env python3
"""A bounded daily retro producer using the existing worker/report transport."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta, timezone
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from time import monotonic
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

SERVICES = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SERVICES / "agent-orch"))
sys.path.insert(0, str(SERVICES))
sys.path.insert(0, str(SERVICES / "chat-stream-v2"))
from agent_orch.config import Config  # noqa: E402
from agent_orch.triage import parse_frontmatter  # noqa: E402
from agent_orch import prompt_protocol, wsclient  # noqa: E402
from tools.live_window import authenticated_operator_connection  # noqa: E402
from websockets.exceptions import ConnectionClosed  # noqa: E402
from message_envelopes import match_message_envelope  # noqa: E402

ZONE = ZoneInfo("America/Chicago")
DISPOSITIONS = {"resolved", "duplicate", "no_change", "investigate", "authorized", "propose", "defer"}
STAGES = ("sol", "astra", "final-astra")
ATTENTION = {"no_owning_work", "new_grant", "recurrence", "changed_evidence", "ownership_gap", "revised_action", "uncertain"}
CHANGED = {"recurrence", "changed_evidence", "ownership_gap", "revised_action"}
PROPOSAL_START = "<!-- daily-retro-proposals -->"
PROPOSAL_END = "<!-- /daily-retro-proposals -->"
ACTIVE_STATUSES = ("backlog", "analysis", "ready_for_dev", "in_progress", "needs_qa", "blocked")
RECOMMENDATIONS = {"no_change", "resolved", "duplicate", "future_work", "immediate_work", "investigate"}


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".retro-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(value if isinstance(value, bytes) else encoded(value) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        folder = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


@dataclass(frozen=True)
class Settings:
    memory_root: Path
    state_root: Path
    ws_url: str
    token_path: Path
    host: str
    isolated: bool = False
    config_path: Path | None = None
    primary_store: Path | None = None
    primary_archive: Path | None = None
    primary_composite: str = "bart:assistant"
    self_assignment_exclusions: list[str] = field(default_factory=list)
    producers: list[str] = field(default_factory=lambda: ["sol"])
    usage_spec_id: str | None = None

    @classmethod
    def load(cls, path):
        path = Path(path).resolve()
        data = read(path)
        if data.get("timezone") != ZONE.key:
            raise ValueError("timezone must be America/Chicago")
        for key in ("memory_root", "state_root", "token_path"):
            if not Path(data[key]).is_absolute():
                raise ValueError(f"{key} must be absolute")
        url = urlparse(data["ws_url"])
        if url.scheme not in {"ws", "wss"} or not url.hostname or not url.port:
            raise ValueError("explicit websocket endpoint required")
        if data.get("isolated") and (url.port == 7791 or url.hostname not in {"localhost", "127.0.0.1"}):
            raise ValueError("isolated endpoint must be an owned local test port")
        memory, state = Path(data["memory_root"]).resolve(), Path(data["state_root"]).resolve()
        if state == memory or memory in state.parents:
            raise ValueError("state must live outside shared memory")
        if data.get("sink"):
            raise ValueError("fixed sink bypass is unsupported; resolve the current binding")
        for key in ("primary_store", "primary_archive"):
            if data.get(key) and not Path(data[key]).is_absolute():
                raise ValueError(f"{key} must be absolute")
        exclusions = data.get("self_assignment_exclusions", [])
        if not isinstance(exclusions, list) or any(not valid_work_id(value) for value in exclusions):
            raise ValueError("self_assignment_exclusions must be a list of valid work IDs")
        producers = data.get("producers", ["sol"])
        if producers != ["sol"]:
            raise ValueError("producers must be exactly [sol]")
        usage_spec_id = data.get("usage_spec_id")
        if usage_spec_id is not None and not valid_work_id(usage_spec_id):
            raise ValueError("usage_spec_id must be one valid work ID or null")
        return cls(memory, state, data["ws_url"], Path(data["token_path"]), data["host"],
                   bool(data.get("isolated")), path,
                   Path(data["primary_store"]) if data.get("primary_store") else None,
                   Path(data["primary_archive"]) if data.get("primary_archive") else None,
                   data.get("primary_composite", cls.__dataclass_fields__["primary_composite"].default), exclusions, producers, usage_spec_id)

    def rpc(self):
        return Config(self.ws_url, self.token_path.read_text().strip(), self.host,
                      self.state_root, self.memory_root)

    @property
    def namespace(self):
        return digest([self.host, str(self.state_root)])[:16]


def timer_due(stamp):
    if stamp.utcoffset() is None:
        raise ValueError("offset-aware timestamp required")
    return stamp.astimezone(ZONE).time() >= time(5)


def aware(value):
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise ValueError("offset-aware timestamp required")
    return stamp


def continuous_tag(meta):
    tags = meta.get("tags", [])
    if isinstance(tags, str) and tags.startswith("[") and tags.endswith("]"):
        tags = [s.strip().strip("\"'") for s in tags[1:-1].split(",")]
    return isinstance(tags, list) and "continuous-retro" in tags


def active_retro(text):
    """Select one real Markdown section; fenced examples are not headings."""
    visible, fence = [], None
    for line in text.splitlines(keepends=True):
        if fence:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(fence[1]) + r",}\s*", line):
                fence = None
            visible.append(re.sub(r"[^\n]", " ", line))
            continue
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})([^\n]*)", line)
        if opening and not (opening[1][0] == "`" and "`" in opening[2]):
            fence = (opening[1][0], len(opening[1]))
            visible.append(re.sub(r"[^\n]", " ", line))
        else:
            visible.append(line)
    matches = list(re.finditer(r"^ {0,3}##\s+Retro\b[^\n]*\n?(.*?)(?=^ {0,3}#{1,2}\s|\Z)",
                              "".join(visible), re.MULTILINE | re.DOTALL | re.IGNORECASE))
    if len(matches) != 1:
        raise ValueError("active source requires exactly one Retro section")
    return text[matches[0].start():matches[0].end()].strip()


def scan(settings, stamp=None):
    sources, gaps, duplicate, seen = {}, [], set(), set()
    for folder in ("completed", "deprecated", *(ACTIVE_STATUSES if stamp else ())):
        active = folder in ACTIVE_STATUSES
        root = settings.memory_root / "work" / folder
        if not root.is_dir():
            if not active:
                gaps.append({"path": str(root), "reason": "terminal directory unavailable"})
            continue
        try:
            paths = sorted(root.rglob("spec.md"))
        except OSError as exc:
            gaps.append({"path": str(root), "reason": str(exc)})
            continue
        for path in paths:
            relative = str(path.relative_to(settings.memory_root))
            try:
                meta = parse_frontmatter(path)
                if active and not continuous_tag(meta):
                    continue
                identity = meta.get("id")
                if not identity or (meta.get("status") != folder if active else meta.get("status") not in {"completed", "deprecated"}):
                    raise ValueError("missing stable ID or nonterminal status")
                if identity in seen or identity in duplicate:
                    duplicate.add(identity)
                    sources.pop(identity, None)
                    raise ValueError(f"duplicate stable ID: {identity}")
                seen.add(identity)
                text = path.read_text().replace("\r\n", "\n")
                if active:
                    original = active_retro(text)
                else:
                    match = re.search(r"^##\s+Retro\b[^\n]*\n?(.*?)(?=^#{1,2}\s|\Z)", text,
                                      re.MULTILINE | re.DOTALL | re.IGNORECASE)
                    if not match:
                        raise ValueError("missing Retro")
                    original = match.group(0).strip()
                body = re.sub(r"^##\s+Retro\b\s*[:—-]?\s*", "", original, count=1, flags=re.IGNORECASE).strip()
                window = None
                if active:
                    markers = re.findall(r"<!-- continuous-retro-window (\{[^\n]*\}) -->", original)
                    if len(markers) != 1:
                        raise ValueError("active Retro requires one continuous-retro-window JSON marker")
                    window = json.loads(markers[0])
                    start, end = aware(window["start"]), aware(window["end"])
                    previous = datetime.combine(stamp.astimezone(ZONE).date() - timedelta(days=1), time(), ZONE)
                    if not start <= end <= stamp or end < previous or start > stamp:
                        raise ValueError("stale or future active Retro capture window")
                    body = re.sub(r"<!-- continuous-retro-window .*? -->", "", body).strip()
                if not body:
                    raise ValueError("empty Retro")
                raw_day = None if active else meta.get(f"{meta['status']}_at") or meta.get("closed_at") or meta.get("updated_at")
                day = None
                if raw_day:
                    terminal = datetime.fromisoformat(str(raw_day).replace("Z", "+00:00"))
                    day = (terminal.astimezone(ZONE) if terminal.utcoffset() is not None else terminal).date().isoformat()
                sources[identity] = {"id": identity, "path": relative, "status": meta["status"],
                                     "terminal_date": day, "fingerprint": digest(original), "original": original}
                if active:
                    sources[identity].update(intake="continuous-retro", capture_window=window)
            except (OSError, UnicodeError, ValueError, RuntimeError, KeyError, TypeError) as exc:
                gaps.append({"path": relative, "reason": str(exc)})
    return sources, gaps


def digest_action(lane, evaluated_at, *, plan_statuses=(), waiting=False):
    """Evidence class and required next action, never a progress claim from a badge."""
    eta = lane.get("eta_at")
    expired = bool(eta and aware(eta) <= aware(evaluated_at))
    if lane.get("open_terminal_reports", 0):
        return "terminal_report", "inspect_report"
    if lane.get("working") is True:
        return "live_tool", "inspect_expired_checkpoint_and_record_reason_and_next_checkpoint" if expired else None
    if waiting:
        return "intentional_hold", "inspect_expired_checkpoint_and_record_reason_and_next_checkpoint" if expired else None
    if plan_statuses and all(s == "done" for s in plan_statuses):
        return "terminal_plan", "inspect_terminal_report"
    if lane.get("role") == "planner":
        return "retained_planner", "inspect_expired_checkpoint_and_record_reason_and_next_checkpoint" if expired else None
    idle = lane.get("idle_age_s")
    if expired or (isinstance(idle, (int, float)) and not isinstance(idle, bool) and idle > 7200):
        return ("expired_checkpoint" if expired else "unexplained_idle"), "inspect_dependency_and_record_action_or_reason_and_checkpoint"
    return "unknown" if lane.get("working") is None else "below_checkpoint", None


def primary_evidence(settings, start, cutoff, *, max_rows=1000, max_bytes=24576):
    """One bounded, read-only metadata snapshot of the existing receipt stores."""
    packet = {"window": {"start": start.isoformat(), "end": cutoff.isoformat()},
              "snapshot_at": now_iso(), "store": str(settings.primary_store) if settings.primary_store else None,
              "coverage": {}, "gaps": [], "observations": [], "deferred": {"count": 0, "by_kind": {}}}
    if not settings.primary_store:
        packet["gaps"].append({"source": "primary_store", "reason": "not configured"})
        return packet
    connections = []
    observations = []
    try:
        def connect(path):
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
            connections.append(conn)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            return conn

        conn = connect(settings.primary_store)
        archive = None
        if settings.primary_archive:
            try:
                archive = connect(settings.primary_archive)
            except sqlite3.Error:
                packet["gaps"].append({"source": "primary_archive", "reason": "unavailable"})
        else:
            packet["gaps"].append({"source": "primary_archive", "reason": "not configured; retired recipients unknown"})

        def query(db, name, columns, table, where="1", args=(), order="1"):
            try:
                total = db.execute(f"SELECT count(*) FROM {table} WHERE {where}", args).fetchone()[0]
                packet["coverage"][name] = {"observed": total, "scanned": 0, "overflow": total}
                rows = [dict(r) for r in db.execute(
                    f"SELECT {columns} FROM {table} WHERE {where} ORDER BY {order} LIMIT ?", (*args, max_rows))]
                packet["coverage"][name] = {"observed": total, "scanned": len(rows), "overflow": total - len(rows)}
                return rows
            except sqlite3.Error:
                packet["gaps"].append({"source": name, "reason": "table or required projection unavailable"})
                return []

        bindings = query(conn, "binding", "name,stream_id,generation,revision,updated_at",
                         "v2_assistant_direct_binding", "name='bart'")
        roots = {r["stream_id"] for r in bindings if r["stream_id"] and aware(r["updated_at"]) <= cutoff}
        packet["binding"] = bindings
        rebinds = query(conn, "rebind_audit", "audit_id,outcome,created_at,"
            "json_extract(old_binding_json,'$.stream_id') AS old_stream_id,"
            "json_extract(old_binding_json,'$.generation') AS old_generation,"
            "json_extract(new_binding_json,'$.stream_id') AS new_stream_id,"
            "json_extract(new_binding_json,'$.generation') AS new_generation", "v2_assistant_rebind_audit",
            "outcome='ok' AND julianday(created_at)<=julianday(?)", (cutoff.isoformat(),), "created_at,audit_id")
        packet["rebind_provenance"] = rebinds
        for r in rebinds:
            for prefix in ("old", "new"):
                stream, generation = r[prefix + "_stream_id"], r[prefix + "_generation"]
                if isinstance(stream, str) and stream and isinstance(generation, str) and generation:
                    roots.add(stream)
                else:
                    packet["gaps"].append({"source": "rebind:" + str(r["audit_id"]), "reason": prefix + " binding provenance incomplete"})
        packet["roots"] = sorted(roots)
        packet["scope_limit"] = "Current/prior configured Bart roots and their retained descendants; no universal fleet or historical-state claim."
        if not roots:
            packet["gaps"].append({"source": "binding", "reason": "no scoped Bart root"})
            return packet
        placeholders = ",".join("?" for _ in roots)
        args = tuple(sorted(roots))
        nodes = "SELECT host||':'||session_name AS sid,parent_stream_id AS parent FROM sessions"
        archived_scope = False
        if archive:
            try:
                conn.execute("ATTACH DATABASE ? AS retro_archive", (settings.primary_archive.resolve().as_uri() + "?mode=ro",))
                conn.execute("SELECT host,session_name,parent_stream_id FROM retro_archive.sessions LIMIT 0")
                nodes += " UNION SELECT host||':'||session_name,parent_stream_id FROM retro_archive.sessions"
                archived_scope = True
            except sqlite3.Error:
                packet["gaps"].append({"source": "archive_membership", "reason": "retired scope unavailable"})
        seeds = "VALUES " + ",".join("(?)" for _ in roots)
        scope_sql = f"""WITH RECURSIVE nodes(sid,parent) AS ({nodes}), seed(sid) AS ({seeds}), scope(sid) AS (
            SELECT sid FROM seed UNION SELECT nodes.sid FROM nodes JOIN scope ON nodes.parent=scope.sid)
            SELECT sid FROM scope"""
        session_columns = "host||':'||session_name AS stream_id,status,closed_at,created_at,"
        session_columns += (
            "json_extract(CASE WHEN json_valid(status_card) THEN status_card ELSE '{}' END,'$.updated_at') AS plan_updated_at,"
            "(SELECT json_group_array(json_extract(p.value,'$.status')) FROM json_each("
            "CASE WHEN json_valid(status_card) THEN status_card ELSE '{}' END,'$.plan') p) AS plan_statuses")
        sessions = query(conn, "sessions", session_columns, "sessions", f"host||':'||session_name IN ({scope_sql})", args)
        archived_sessions = query(conn, "archived_sessions", session_columns, "retro_archive.sessions",
            f"host||':'||session_name IN ({scope_sql}) AND host||':'||session_name NOT IN (SELECT host||':'||session_name FROM sessions)",
            args) if archived_scope else []
        session_map = {r["stream_id"]: {**r, "plan_source": "primary_archive.sessions"} for r in archived_sessions}
        session_map.update({r["stream_id"]: {**r, "plan_source": "sessions"} for r in sessions})
        text_window = "julianday(created_at)>=julianday(?) AND julianday(created_at)<=julianday(?)"
        window_args = (start.isoformat(), cutoff.isoformat())
        reports = query(conn, "reports", "report_id,from_stream_id,to_stream_id,session_generation,status,created_at",
            "v2_reports", f"to_stream_id IN ({placeholders}) AND status IN ('done','error','aborted') AND created_at>=? AND created_at<=?",
            (*args, start.timestamp(), cutoff.timestamp()), "CASE WHEN status='done' THEN 1 ELSE 0 END,created_at,report_id")
        publications = query(conn, "publications", "publication_key,stream_id,publish_kind,event_id,created_at,evidence_refs_json",
            "v2_assistant_composite_publications", "stream_id=? AND " + text_window,
            (settings.primary_composite, *window_args), "created_at,publication_key")
        correlated = {}
        for pub in publications:
            try:
                refs = json.loads(pub.pop("evidence_refs_json"))
                if not isinstance(refs, list):
                    raise ValueError()
                for ref in refs:
                    if isinstance(ref, str) and any(r["report_id"] == ref for r in reports):
                        correlated.setdefault(ref, []).append(pub)
            except (ValueError, TypeError):
                packet["gaps"].append({"source": "publication:" + pub["publication_key"], "reason": "invalid evidence references"})
        correlated = {r["report_id"]: [p for p in correlated.get(r["report_id"], [])
            if aware(p["created_at"]).timestamp() >= r["created_at"]] for r in reports}
        correlated = {key: pubs for key, pubs in correlated.items() if pubs}
        packet["coverage"]["completion_correlation"] = {"observed_done": sum(r["status"] == "done" for r in reports),
            "explicitly_published": sum(r["status"] == "done" and r["report_id"] in correlated for r in reports),
            "unknown": sum(r["status"] == "done" and r["report_id"] not in correlated for r in reports)}
        for r in reports:
            pubs = [p for p in correlated.get(r["report_id"], []) if aware(p["created_at"]).timestamp() >= r["created_at"]]
            entry = {"kind": "completion" if r["status"] == "done" else "terminal_failure", "source": "v2_reports:" + r["report_id"],
                     **r, "publication": pubs, "delivery": "correlated" if pubs else "unknown"}
            if pubs:
                entry["completion_to_publication_seconds"] = min(aware(p["created_at"]).timestamp() for p in pubs) - r["created_at"]
            observations.append(entry)
        for pub in publications[:100]:
            observations.append({"kind": "publication", "source": "v2_assistant_composite_publications:" + pub["publication_key"], **pub})
        sends = query(conn, "send_receipts", "receipt_id,request_id,from_stream_id,to_stream_id,state,delivery,submission_confirmed,created_at",
            "v2_send_receipts r", f"(from_stream_id IN ({scope_sql}) OR to_stream_id IN ({placeholders})) AND {text_window} "
            "AND r.rowid=(SELECT max(latest.rowid) FROM v2_send_receipts latest WHERE latest.to_stream_id=r.to_stream_id "
            "AND latest.request_id=r.request_id AND julianday(latest.created_at)<=julianday(?))",
            (*args, *args, *window_args, cutoff.isoformat()), "CASE WHEN state='not_landed' THEN 0 ELSE 1 END,created_at,receipt_id")
        # Only the latest retained state per exact request matters; a transient
        # accepted row cannot turn a later landed receipt into a failure.
        latest = {}
        for send in sends:
            key = (send["to_stream_id"], send["request_id"])
            if key not in latest or send["created_at"] >= latest[key]["created_at"]:
                latest[key] = send
        destinations = {r["to_stream_id"] for r in latest.values()} - set(session_map)
        for db, name in ((conn, "recipient_sessions"), (archive, "archived_recipients")):
            if destinations and db:
                marks = ",".join("?" for _ in destinations)
                for r in query(db, name, "host||':'||session_name AS stream_id,status,closed_at,created_at", "sessions",
                    f"host||':'||session_name IN ({marks})", tuple(sorted(destinations))):
                    session_map[r["stream_id"]] = r
                destinations -= set(session_map)
        packet["coverage"]["recipient_state"] = {"unknown": len(destinations), "looked_up": len({r["to_stream_id"] for r in latest.values()})}
        for r in latest.values():
            target = session_map.get(r["to_stream_id"], {})
            closed = target.get("closed_at")
            after_close = bool(closed and aware(closed) <= aware(r["created_at"]))
            if r["state"] == "not_landed" or after_close:
                observations.append({"kind": "delivery_failure" if r["state"] == "not_landed" else "closed_recipient",
                    "source": "v2_send_receipts:" + r["receipt_id"], **r,
                    "recipient_closed_at": closed, "next_action": "inspect_delivery_or_closed_dependency_immediately"})
        waits = query(conn, "waiting_lanes", "lane_id,stream_id,bound_stream_id,bound_generation,phase,version,updated_at",
            "v2_assistant_composite_lanes", f"stream_id=? AND phase='waiting' AND bound_stream_id IN ({scope_sql}) "
            "AND julianday(updated_at)<=julianday(?)", (settings.primary_composite, *args, cutoff.isoformat()))
        notices = query(conn, "notices", "notice_id,kind,recipient_stream_id,source_stream_id,created_at,delivered_at,terminal_at,"
            "terminal_reason IS NOT NULL AS has_terminal_reason,attempts,last_error IS NOT NULL AS has_error,"
            "CASE WHEN terminal_reason IN ('persisted_suppressed','folded_into_digest') THEN terminal_reason ELSE NULL END AS suppression,"
            "CASE WHEN kind='lane_digest' THEN body ELSE NULL END AS digest_body", "v2_outbound_notices",
            f"(recipient_stream_id IN ({scope_sql}) OR source_stream_id IN ({scope_sql})) AND {text_window}",
            (*args, *args, *window_args),
            "CASE WHEN last_error IS NOT NULL AND coalesce(terminal_reason,'') NOT IN ('persisted_suppressed','folded_into_digest') THEN 0 ELSE 1 END,created_at,notice_id")
        packet["coverage"]["expected_notice_suppression"] = {"scanned": sum(bool(n["suppression"]) for n in notices),
            "limit": "Only explicit persisted_suppressed/folded_into_digest are routine suppression; other errors require inspection."}
        episodes = {}
        for n in sorted(notices, key=lambda r: (r["created_at"], r["notice_id"])):
            body = n.pop("digest_body")
            if n["has_error"] and not n["suppression"]:
                observations.append({**n, "kind": "delivery_failure", "notice_kind": n["kind"],
                    "source": "v2_outbound_notices:" + n["notice_id"], "next_action": "inspect_delivery_immediately"})
            if n["kind"] != "lane_digest":
                continue
            envelope = match_message_envelope(body)
            if not envelope or envelope["kind"] != "lane_digest":
                packet["gaps"].append({"source": "notice:" + n["notice_id"], "reason": "unreadable registered lane digest"})
                continue
            for raw in envelope["lanes"]:
                lane = {k: raw.get(k) for k in ("stream_id", "generation", "role", "working", "idle_age_s", "eta_at", "open_terminal_reports")}
                if not lane.get("stream_id"):
                    continue
                session = session_map.get(lane["stream_id"], {})
                statuses = json.loads(session.get("plan_statuses") or "[]") if session.get("plan_updated_at") and aware(session["plan_updated_at"]) <= aware(n["created_at"]) else []
                waiting_provenance = [w for w in waits if lane["generation"]
                    and w["bound_stream_id"] == lane["stream_id"] and w["bound_generation"] == lane["generation"]
                    and aware(w["updated_at"]) <= aware(n["created_at"])]
                waiting = bool(waiting_provenance)
                try:
                    classification, action = digest_action(lane, envelope["evaluated_at"], plan_statuses=statuses, waiting=waiting)
                except (ValueError, TypeError):
                    classification, action = "invalid_checkpoint", "inspect_dependency_and_record_action_or_reason_and_checkpoint"
                # Age increments are not a fresh episode. A live tool/hold or
                # terminal state changes classification and permits reevaluation.
                episode = (lane["stream_id"], lane["generation"], classification, action, lane["eta_at"], tuple(statuses),
                           tuple((w["lane_id"], w["version"], w["updated_at"]) for w in waiting_provenance))
                key = (lane["stream_id"], lane["generation"])
                if episodes.get(key) == episode:
                    continue
                episodes[key] = episode
                observations.append({"kind": "digest", "source": "v2_outbound_notices:" + n["notice_id"],
                    "created_at": n["created_at"], "lane": lane, "classification": classification, "next_action": action,
                    "plan_evidence": "known" if statuses else "unknown", "waiting_evidence": waiting,
                    "plan_provenance": {"source": session.get("plan_source"), "updated_at": session.get("plan_updated_at"),
                        "statuses": statuses}, "waiting_provenance": waiting_provenance,
                    "execution_evidence": "provider_working_flag; specific tool unknown" if lane["working"] is True else "no live-tool proof"})
    except (sqlite3.Error, OSError):
        packet["gaps"].append({"source": "primary_store", "reason": "unavailable"})
    finally:
        for db in connections:
            db.close()
    urgent = lambda r: bool(r.get("next_action") or r["kind"] == "terminal_failure")
    if len(packet["gaps"]) > 50:
        packet["coverage"]["gap_references"] = {"observed": len(packet["gaps"]), "overflow": len(packet["gaps"]) - 50}
        packet["gaps"] = packet["gaps"][:50]
    # Retained provenance references are samples, not the SQL membership set.
    # Keep room for observations even after many successful root rebindings.
    reference_counts = {name: len(packet.get(name, [])) for name in ("rebind_provenance", "roots", "binding")}
    while len(encoded(packet)) > max_bytes // 3:
        eligible = [name for name in reference_counts if packet.get(name)]
        if not eligible:
            break
        name = max(eligible, key=lambda key: len(encoded(packet[key])))
        rows = packet[name]
        keep = rows[len(rows) // 2 + 1:]
        if name == "roots":
            current = {r["stream_id"] for r in packet.get("binding", [])}
            keep = sorted(set(keep) | (set(rows) & current))
            if keep == rows:
                eligible.remove(name)
                if not eligible:
                    break
                name = max(eligible, key=lambda key: len(encoded(packet[key])))
                rows = packet[name]
                keep = rows[len(rows) // 2 + 1:]
        packet[name] = keep
        packet.setdefault("reference_coverage", {})[name] = {
            "observed": reference_counts[name], "retained": len(keep), "deferred": reference_counts[name] - len(keep)}
    observations.sort(key=lambda r: (not urgent(r), str(r.get("created_at", "")), r["source"]))
    packet["coverage"]["observations"] = {"observed": len(observations), "selected": 0,
        "urgent": sum(urgent(r) for r in observations)}
    routine = 0
    for row in observations:
        if (not urgent(row) and routine >= 100) or len(encoded({**packet, "observations": packet["observations"] + [row]})) > max_bytes - 1024:
            packet["deferred"]["count"] += 1
            counts = packet["deferred"]["by_kind"]
            counts[row["kind"]] = counts.get(row["kind"], 0) + 1
        else:
            packet["observations"].append(row)
            routine += int(not urgent(row))
    packet["coverage"]["observations"]["selected"] = len(packet["observations"])
    packet["coverage"]["observations"]["deferred_urgent"] = sum(urgent(r) for r in observations) - sum(urgent(r) for r in packet["observations"])
    return packet


def rebuild_index(settings):
    entries = {}
    manifests = sorted((settings.state_root / "runs").glob("*/collection.json"))
    for path in manifests:
        manifest = read(path)
        entries.update(manifest["baseline"])
        entries.update({s["id"]: {"fingerprint": s["fingerprint"], "reviewed": False,
                                 "enrolled_run": manifest["run_id"]} for s in manifest["sources"]})
    return {"initialized": bool(manifests), "entries": entries}


def collect(settings, stamp, *, max_sources=40, max_bytes=65536):
    if stamp.utcoffset() is None:
        raise ValueError("offset-aware cutoff required")
    local = stamp.astimezone(ZONE)
    run_id = local.date().isoformat()
    path = settings.state_root / "runs" / run_id / "collection.json"
    with locked(settings.state_root / "collect.lock"):
        index = rebuild_index(settings)
        if path.exists():
            atomic(settings.state_root / "index.json", index)
            return read(path)
        sources, gaps = scan(settings, stamp)
        start = datetime.combine(local.date() - timedelta(days=1), time(), ZONE)
        primary = primary_evidence(settings, start, stamp)
        if settings.primary_store:
            original = "## Retro\nPrimary Bart receipts (metadata only)\n" + encoded(primary).decode()
            identity = "primary:bart:" + run_id
            sources = {identity: {"id": identity, "path": str(settings.primary_store), "status": "primary",
                "intake": "primary", "terminal_date": local.date().isoformat(),
                "fingerprint": digest(original), "original": original}, **sources}
        baseline, selected, deferred, size = {}, [], [], 0
        previous = local.date() - timedelta(days=1)
        for identity, source in sorted(sources.items(), key=lambda item: (item[1].get("intake") != "primary", item[0])):
            prior = index["entries"].get(identity)
            if prior and prior["fingerprint"] == source["fingerprint"]:
                continue
            if not source.get("intake") and not index["initialized"] and (not source["terminal_date"] or source["terminal_date"] < previous.isoformat()):
                baseline[identity] = {"fingerprint": source["fingerprint"], "reviewed": False,
                                      "reason": "older or unknown-date initial history"}
                continue
            if source["terminal_date"] and source["terminal_date"] > local.date().isoformat():
                gaps.append({"path": source["path"], "reason": "future terminal date"})
                continue
            source_size = len(source["original"].encode())
            if len(selected) >= max_sources or size + source_size > max_bytes:
                deferred.append({k: source[k] for k in ("id", "path", "fingerprint")})
                continue
            selected.append(source)
            size += source_size
        start = datetime.combine(previous, time(), ZONE)
        end = datetime.combine(local.date(), time(), ZONE)
        manifest = {"schema_version": 2, "run_id": run_id, "timezone": ZONE.key, "cutoff": stamp.isoformat(), "collected_at": now_iso(),
                    "producers": list(settings.producers),
                    "window": {"start": start.isoformat(), "end": end.isoformat()},
                    "sources": selected, "baseline": baseline, "gaps": gaps, "deferred": deferred, "primary": primary,
                    "coverage": {"readable_retros": len(sources), "selected": len(selected),
                                 "baseline_not_reviewed": len(baseline), "gaps": len(gaps), "deferred": len(deferred)}}
        atomic(path, manifest)  # This commits enrollment; index is a rebuildable projection.
        atomic(settings.state_root / "index.json", rebuild_index(settings))
        return manifest


def history_baseline(settings, baseline_path):
    """Prove the namespace and enrollment using reads only, before creating a lock."""
    baseline_path = Path(baseline_path).resolve()
    if (baseline_path.name != "collection.json" or baseline_path.parent.parent.name != "runs"
            or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", baseline_path.parent.name)):
        raise ValueError("daily baseline collection path required")
    daily_state = baseline_path.parents[2]
    state = settings.state_root.resolve()
    if state == daily_state or daily_state in state.parents or state in daily_state.parents:
        raise ValueError("history requires a separate state namespace")
    # A copied baseline path must not disguise a real daily namespace as history.
    for parent in (state, *state.parents):
        if (parent / "index.json").exists() or any(
                re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", p.parent.name)
                for p in (parent / "runs").glob("*/collection.json")):
            raise ValueError("history requires a separate state namespace")
    baseline = read(baseline_path)
    if (baseline.get("run_id") != baseline_path.parent.name or baseline.get("phase") == "history"
            or read(daily_state / "index.json") != rebuild_index(replace(settings, state_root=daily_state))):
        raise ValueError("daily baseline enrollment proof required")
    return baseline_path, baseline


def freeze_history(settings, baseline_path):
    """Snapshot an explicit daily baseline, without enrolling it in daily state."""
    baseline_path, baseline = history_baseline(settings, baseline_path)
    baseline_sha = hashlib.sha256(baseline_path.read_bytes()).hexdigest()
    path = settings.state_root / "history.json"
    inventory = read(path)
    if inventory is not None:
        if inventory.get("baseline_sha256") != baseline_sha:
            raise ValueError("historical baseline conflict")
        if inventory.get("inventory_digest") != digest({k: v for k, v in inventory.items() if k != "inventory_digest"}):
            raise ValueError("historical inventory digest mismatch")
        return inventory
    entries = baseline.get("baseline")
    if not isinstance(entries, dict) or not entries or set(entries) & {s["id"] for s in baseline["sources"]}:
        raise ValueError("explicit nonempty historical baseline required")
    current, _ = scan(settings)
    originals, batches, batch, size = [], [], [], 0
    for identity, entry in sorted(entries.items()):
        source = current.get(identity)
        if not source or entry.get("reviewed") is not False or source["fingerprint"] != entry.get("fingerprint"):
            raise ValueError(f"missing, malformed or changed historical original: {identity}")
        source_size = len(source["original"].encode())
        if source_size > 65536:
            raise ValueError(f"oversized historical original: {identity}")
        if len(batch) == 40 or size + source_size > 65536:
            batches.append(batch)
            batch, size = [], 0
        originals.append(source)
        batch.append(identity)
        size += source_size
    if batch:
        batches.append(batch)
    inventory = {"phase": "history", "baseline_path": str(baseline_path), "baseline_sha256": baseline_sha,
                 "collected_at": now_iso(), "sources": originals, "batches": batches}
    inventory["inventory_digest"] = digest(inventory)
    atomic(path, inventory)
    return inventory


def history_collect(settings, baseline_path, batch):
    history_baseline(settings, baseline_path)  # Refusal must not create paths in daily state.
    with locked(settings.state_root / "collect.lock"):
        inventory = freeze_history(settings, baseline_path)
        if type(batch) is not int or not 1 <= batch <= len(inventory["batches"]):
            raise ValueError("historical batch out of range")
        inventory_digest = inventory["inventory_digest"]
        originals = {s["id"]: s for s in inventory["sources"]}
        selected = [originals[identity] for identity in inventory["batches"][batch - 1]]
        run_id = f"history-{inventory_digest[:16]}-{batch:04}"
        manifest = {"run_id": run_id, "phase": "history", "timezone": ZONE.key, "window": None,
                    "cutoff": inventory["collected_at"], "collected_at": inventory["collected_at"],
                    "sources": selected, "baseline": {}, "gaps": [], "deferred": [],
                    "history": {"inventory_path": str(settings.state_root / "history.json"),
                                "inventory_digest": inventory_digest, "batch": batch,
                                "batches": len(inventory["batches"]), "originals": len(originals)},
                    "coverage": {"readable_retros": len(originals), "selected": len(selected),
                                 "baseline_not_reviewed": 0, "gaps": 0, "deferred": 0}}
        path = settings.state_root / "runs" / run_id / "collection.json"
        if path.exists():
            if read(path) != manifest:
                raise ValueError("edited or stale historical manifest")
        else:
            atomic(path, manifest)
        return manifest


def checked(response, kind):
    if response.get("type") != kind or response.get("ok") is False:
        raise RuntimeError(f"{kind}: {response.get('error_code') or response.get('reason') or response.get('type')}")
    return response


def validate_packet(packet, manifest):
    packet = json.loads(json.dumps(packet))  # Preserve the immutable raw worker report.
    if packet.get("run_id") != manifest["run_id"]:
        raise ValueError("packet run identity mismatch")
    selected = {s["id"]: s for s in manifest["sources"]}
    rows = packet.get("dispositions", [])
    if len(rows) != len(selected) or {r.get("id") for r in rows} != set(selected):
        raise ValueError("one disposition required for every original, including no-action")
    for row in rows:
        if row.get("fingerprint") != selected[row["id"]]["fingerprint"] or not row.get("reason"):
            raise ValueError("disposition must cite the retained original and give a reason")
    required = {"id", "problem", "consequence", "citations", "prior_occurrences", "existing", "action",
                "benefit", "effort", "risk", "uncertainty", "owner", "decision"}
    candidates = packet.get("candidates")
    if not isinstance(candidates, list) or len({c.get("id") for c in candidates}) != len(candidates):
        raise ValueError("unique candidate IDs required")
    for candidate in candidates:
        if required - candidate.keys() or not candidate["id"] or not candidate["citations"]:
            raise ValueError("incomplete recommendation")
        if manifest.get("schema_version") == 2 and candidate.get("recommendation_kind") not in RECOMMENDATIONS:
            raise ValueError("schema2 candidate requires explicit recommendation_kind")
        citations = candidate["citations"]
        if not isinstance(citations, list) or any(not isinstance(ref, str) for ref in citations):
            raise ValueError("citation IDs must be strings")
        originals = [ref for ref in citations if ref in selected]
        supplemental = [ref for ref in citations if ref not in selected]
        if not originals or any(ref.startswith("spec_") and ref not in packet.get("evidence_sources", {}) for ref in supplemental):
            raise ValueError("missing or fabricated collection original ID")
        if supplemental:
            candidate["citations"] = originals
            candidate["evidence_citations"] = list(dict.fromkeys(candidate.get("evidence_citations", []) + supplemental))
            packet.setdefault("normalization_notes", []).append({"candidate_id": candidate["id"], "evidence_refs": supplemental,
                "note": "Supplemental references retained as evidence labels; they do not expand original coverage."})
    if manifest.get("schema_version") == 2:
        packet["schema_version"] = 2
    return normalize_daily_candidates(packet, selected) if daily_manifest(manifest) else packet


CANDIDATE_METADATA = {"id", "candidate_key", "version", "finding_key", "finding_version", "aliases", "prior_reviews", "candidate_version"}
CANDIDATE_TEXT = ("problem", "consequence", "action", "benefit", "effort", "risk", "uncertainty", "decision")


def daily_manifest(manifest):
    return not (manifest.get("phase", "").startswith("history") or manifest.get("consolidation")
                or manifest["run_id"].startswith("history-"))


def candidate_version(candidate):
    return digest({key: value for key, value in candidate.items() if key not in CANDIDATE_METADATA})


def normalize_daily_candidates(packet, selected):
    """Collector-owned identity is separate from daily labels and content hashes."""
    normalized = []
    for candidate in packet["candidates"]:
        targets = candidate.get("work_ids", [])
        if not isinstance(targets, list) or any(not valid_work_id(value) for value in targets):
            raise ValueError("work_ids must be a list of valid work IDs")
        candidate["work_ids"] = sorted(set(targets))
        refs = candidate.get("identity_citations", [
            {"store": "primary" if selected[ref].get("intake") == "primary" else "work", "record_id": ref}
            for ref in candidate["citations"]])
        if not isinstance(refs, list) or any(not isinstance(ref, dict) or
                any(not isinstance(ref.get(key), str) or not ref[key].strip() for key in ("store", "record_id"))
                for ref in refs):
            raise ValueError("identity_citations must contain store and record_id strings")
        pairs = sorted({(ref["store"].strip().lower(), ref["record_id"].strip()) for ref in refs})
        candidate["identity_citations"] = [{"store": store, "record_id": identity} for store, identity in pairs]
        candidate["citations"] = sorted(set(candidate["citations"]))
        for key in ("candidate_key", "version", "candidate_version", "aliases", "prior_reviews"):
            candidate.pop(key, None)
        if "merged_candidates" in candidate:
            candidate["merged_candidates"] = [{k: v for k, v in member.items() if k not in CANDIDATE_METADATA - {"id"}}
                                               for member in candidate["merged_candidates"]]
        candidate["version"] = candidate_version(candidate)
        candidate["candidate_key"] = (
            "work:" + candidate["work_ids"][0] if len(candidate["work_ids"]) == 1 else
            "evidence:" + hashlib.sha256("\n".join(sorted({f"{store}:{identity}" for store, identity in pairs})).encode()).hexdigest()[:16]
            if pairs else "unkeyed:" + candidate["version"])
        normalized.append(candidate)
    groups = {}
    for candidate in normalized:
        if candidate["candidate_key"].startswith("work:"):
            groups.setdefault(candidate["candidate_key"], []).append(candidate)
    merged, seen_work = [], set()
    for candidate in normalized:
        key = candidate["candidate_key"]
        if key in seen_work:
            continue
        if key.startswith("work:"):
            seen_work.add(key)
        members = groups.get(key, [candidate])
        if len(members) == 1:
            if candidate not in merged:
                merged.append(candidate)
            continue
        # Only validated top-level findings establish the member set; a worker's
        # supplied merge metadata cannot erase an original or its source coverage.
        originals = [dict(member) for member in members]
        originals.sort(key=lambda member: member["id"])
        originals = [{k: v for k, v in member.items() if k not in CANDIDATE_METADATA - {"id"}}
                     for member in originals]
        combined = dict(originals[0])
        for field in CANDIDATE_TEXT:
            values = list(dict.fromkeys(value if isinstance(value, str) else encoded(value).decode()
                                       for value in (member[field] for member in originals)))
            combined[field] = "\n\n".join(values)
        for field in ("citations", "evidence_citations"):
            combined[field] = sorted({ref for member in originals for ref in member.get(field, [])})
        pairs = sorted({(ref["store"], ref["record_id"]) for member in originals for ref in member["identity_citations"]})
        combined["identity_citations"] = [{"store": store, "record_id": identity} for store, identity in pairs]
        combined["merged_candidates"] = originals
        combined["version"] = candidate_version(combined)
        combined["candidate_key"] = candidate["candidate_key"]
        merged.append(combined)
    packet["candidates"] = sorted(merged, key=lambda candidate: candidate["id"])
    packet.setdefault("coverage", {})["unkeyed"] = sum(c["candidate_key"].startswith("unkeyed:") for c in packet["candidates"])
    return packet


def finding(candidate, alias, legacy=False):
    """Compile identity; model-provided keys never establish equivalence."""
    candidate = json.loads(json.dumps(candidate))
    attention = candidate.get("bart_attention")
    if attention is None and legacy:
        attention = {"reasons": ["uncertain"], "rationale": "Legacy packet requires current relevance review."}
    if (not isinstance(attention, dict) or not isinstance(attention.get("reasons"), list)
            or any(not isinstance(r, str) or r not in ATTENTION for r in attention["reasons"])
            or not isinstance(attention.get("rationale"), str) or not attention["rationale"].strip()):
        raise ValueError("invalid history attention annotation")
    attention = {"reasons": sorted(set(attention["reasons"])), "rationale": attention["rationale"]}
    candidate["bart_attention"] = attention
    key = candidate.get("finding_key", "legacy-" + digest(candidate["problem"])[:24])
    if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,119}", key):
        raise ValueError("invalid finding key")
    content = {k: v for k, v in candidate.items() if k not in {"id", "citations", "finding_key", "finding_version"}}
    # A changed occurrence must never disappear behind an earlier recommendation.
    if CHANGED & set(attention["reasons"]):
        content["occurrence"] = alias
    version = digest(content)
    candidate.update(finding_key=key, finding_version=version)
    return {"alias": alias, "candidate": candidate, "version": version}


def completed_history(settings, baseline_path):
    """Read and validate retained originals/reports before any admission or write."""
    history_baseline(settings, baseline_path)
    inventory = read(settings.state_root / "history.json")
    if not inventory or inventory.get("inventory_digest") != digest({k: v for k, v in inventory.items() if k != "inventory_digest"}):
        raise ValueError("valid frozen history inventory required")
    if inventory.get("baseline_sha256") != hashlib.sha256(Path(baseline_path).read_bytes()).hexdigest():
        raise ValueError("historical baseline conflict")
    originals = {s["id"]: s for s in inventory["sources"]}
    completed = {}
    for number, ids in enumerate(inventory["batches"], 1):
        run_id = f"history-{inventory['inventory_digest'][:16]}-{number:04}"
        root = settings.state_root / "runs" / run_id
        manifest = read(root / "collection.json")
        if manifest is None:
            continue
        if (manifest.get("run_id") != run_id or manifest.get("phase") != "history"
                or manifest.get("history", {}).get("inventory_digest") != inventory["inventory_digest"]
                or manifest["history"].get("batch") != number
                or manifest.get("sources") != [originals[i] for i in ids]):
            raise ValueError("edited or stale historical manifest")
        stages = [read(root / f"{name}.json", {}) for name in ("sol", "astra")]
        if not all(s.get("packet") for s in stages):
            continue
        for name, stage in zip(("sol", "astra"), stages):
            report = stage.get("report", {})
            model, effort = ("gpt-6.1-sol", "medium") if name == "sol" else ("gpt-6-astra", "high")
            if (stage.get("packet_hash") != digest(stage["packet"])
                    or report.get("effective_model") != model or report.get("effective_effort") != effort
                    or report.get("report_id") != stage.get("report_id")
                    or not stage.get("generation") or not stage.get("stream_id") or not stage.get("closed")
                    or not stage.get("cleanup", {}).get("session", {}).get("status") == "closed"
                    or stage["cleanup"]["session"].get("session_generation") != stage["generation"]):
                raise ValueError("history packet/report/model/hash/cleanup proof invalid")
            validate_packet(stage["packet"], manifest)
            inp = read(root / f"{name}-input-{stage.get('attempt')}.json")
            if not inp or inp.get("report_id") != stage["report_id"] or inp["collection"].get("sources") != manifest["sources"]:
                raise ValueError("history report input provenance invalid")
            validator = validate_history_packet if inp["collection"].get("cumulative_context") else validate_packet
            expected = validator(report.get("extras", {}).get("daily_retro", {}), manifest)
            expected["collection"] = inp["collection"]
            if expected != stage["packet"]:
                raise ValueError("history packet differs from retained worker report")
        mode = read(root / "history-mode.json")
        review = read(root / "review.json")
        if mode is None and review is None:
            raise ValueError("unreviewed legacy history is not a consolidation input")
        if mode is not None and mode.get("no_deliver") is not True:
            raise ValueError("unknown history delivery mode")
        if mode:
            context_path = root / "history-context.json"
            if (mode.get("context_path") != str(context_path) or not context_path.exists()
                    or hashlib.sha256(context_path.read_bytes()).hexdigest() != mode.get("context_sha256")):
                raise ValueError("edited cumulative context")
        if review and review.get("result", {}).get("packet_hash") != stages[1]["packet_hash"]:
            raise ValueError("history review hash mismatch")
        completed[number] = {"manifest": manifest, "final": stages[1], "review": review,
                             "mode": mode, "root": str(root)}
    return inventory, completed


def history_checkpoints(settings):
    for path in sorted((settings.state_root / "runs").glob("history-consolidated-*/collection.json")):
        manifest = read(path)
        if manifest.get("consolidation", {}).get("mode") == "checkpoint":
            yield path.parent, manifest


def history_catalogue(completed):
    entries = []
    for number, run in sorted(completed.items()):
        stage = run["final"]
        for candidate in stage["packet"]["candidates"]:
            alias = {"run_id": run["manifest"]["run_id"], "candidate_id": candidate["id"], "packet_hash": stage["packet_hash"]}
            row = finding(candidate, alias, legacy=run["mode"] is None)
            row["alias_id"] = digest(alias)
            row["prior_review"] = next((r for r in (run["review"] or {}).get("result", {}).get("dispositions", [])
                                       if r["id"] == candidate["id"]), None)
            entries.append(row)
    return entries


def history_context(settings, inventory, completed):
    entries = history_catalogue(completed)
    checkpoints = []
    for root, manifest in history_checkpoints(settings):
        final = read(root / "astra.json")
        if not final or final.get("packet_hash") != digest(final["packet"]):
            raise ValueError("incomplete or edited prior checkpoint")
        review = read(root / "review.json")
        if review and review.get("result", {}).get("packet_hash") != final["packet_hash"]:
            raise ValueError("checkpoint review hash mismatch")
        checkpoints.append({"run_id": manifest["run_id"], "packet_hash": final["packet_hash"],
                            "review": review, "quiet": not final["packet"]["candidates"]})
    return {"inventory_digest": inventory["inventory_digest"], "findings": entries,
            "inputs": [{"run_id": r["manifest"]["run_id"], "packet_hash": r["final"]["packet_hash"],
                        "packet_path": str(Path(r["root"]) / "astra.json"), "review": r["review"]}
                       for _, r in sorted(completed.items())], "checkpoints": checkpoints,
            "work_root": str(settings.memory_root / "work")}


def validate_history_packet(packet, manifest):
    packet = validate_packet(packet, manifest)
    for c in packet["candidates"]:
        compiled = finding(c, {"run_id": manifest["run_id"], "candidate_id": c["id"]})
        c.update(compiled["candidate"])
    return packet


def validate_final_selection(packet, manifest):
    catalogue = manifest["consolidation"]["catalogue"]
    if packet.get("run_id") != manifest["run_id"] or packet.get("input_digest") != digest(catalogue):
        raise ValueError("final selection identity/input digest mismatch")
    rows = packet.get("selections")
    aliases = {r["alias_id"]: r for r in catalogue}
    if (not isinstance(rows, list) or len(rows) != len(aliases)
            or {r.get("alias_id") for r in rows} != set(aliases)):
        raise ValueError("one final selection per retained alias required")
    packet = json.loads(json.dumps(packet))
    for row in packet["selections"]:
        if row.get("disposition") not in {"keep", "drop"} or not row.get("reason"):
            raise ValueError("final keep/drop reason required")
        candidate = json.loads(json.dumps(row.get("candidate", aliases[row["alias_id"]]["candidate"])))
        if "bart_attention" in row:
            candidate["bart_attention"] = row["bart_attention"]
        original = aliases[row["alias_id"]]["candidate"]
        if (candidate.get("id") != original["id"] or candidate.get("citations") != original["citations"]
                or candidate.get("evidence_citations", []) != original.get("evidence_citations", [])):
            raise ValueError("final selection invented alias or citation")
        checked_candidate = finding(candidate, aliases[row["alias_id"]]["alias"])["candidate"]
        if row["disposition"] == "drop" and checked_candidate["bart_attention"]["reasons"]:
            raise ValueError("cannot drop a relevant or uncertain final finding")
        if row["disposition"] == "keep" and not checked_candidate["bart_attention"]["reasons"]:
            raise ValueError("final keep requires relevance rationale")
        row["candidate"] = checked_candidate
    return packet


def compile_history_packet(manifest, completed, selections=None):
    catalogue = manifest["consolidation"]["catalogue"]
    selected = {r["alias_id"]: r for r in selections["selections"]} if selections else {}
    already = set(manifest["consolidation"]["already_reported"])
    groups, audit = {}, []
    for entry in catalogue:
        row = selected.get(entry["alias_id"])
        candidate = row["candidate"] if row else entry["candidate"]
        version = finding(candidate, entry["alias"])["version"]
        eligible = bool(candidate["bart_attention"]["reasons"])
        include = row["disposition"] == "keep" if row else eligible and version not in already
        audit.append({**entry, "selected": include, "final_selection": row,
                      "reason": row["reason"] if row else candidate["bart_attention"]["rationale"]})
        if include:
            group = groups.setdefault(version, {**candidate, "id": version, "finding_version": version,
                                               "citations": [], "aliases": [], "prior_reviews": []})
            group["citations"] = sorted(set(group["citations"] + candidate["citations"]))
            group["aliases"].append(entry["alias"])
            if entry["prior_review"]:
                group["prior_reviews"].append(entry["prior_review"])
            for checkpoint in manifest["consolidation"]["context"]["checkpoints"]:
                for review in (checkpoint["review"] or {}).get("result", {}).get("dispositions", []):
                    if review["id"] == entry["version"] and review not in group["prior_reviews"]:
                        group["prior_reviews"].append(review)
    dispositions, evidence = [], {}
    for _, run in sorted(completed.items()):
        packet = run["final"]["packet"]
        dispositions.extend(packet["dispositions"])
        sources = packet.get("evidence_sources", {})
        if isinstance(sources, list):
            sources = ((value.get("id") or f"evidence-{index}", value)
                       if isinstance(value, dict) else (f"evidence-{index}", value)
                       for index, value in enumerate(sources))
        else:
            sources = sources.items()
        for key, value in sources:
            if key in evidence and evidence[key] != value:
                # Labels are batch-local; retain conflicting proofs without rewriting citations.
                qualified = f"{run['manifest']['run_id']}:{key}:{digest(value)}"
                key, suffix = qualified, 1
                while key in evidence and evidence[key] != value:
                    key = f"{qualified}:{suffix}"
                    suffix += 1
            evidence[key] = value
    packet = {"run_id": manifest["run_id"], "dispositions": dispositions,
              "candidates": [groups[k] for k in sorted(groups)], "evidence_sources": evidence,
              "audit": audit, "counts": {"originals": len(dispositions), "candidate_occurrences": len(catalogue),
                  "surfaced_occurrences": sum(r["selected"] for r in audit), "surfaced_candidates": len(groups),
                  "retained_only_candidates": sum(not r["selected"] for r in audit)}}
    return validate_packet(packet, manifest)


def worker_collection(manifest, path):
    """Keep historical enrollment bookkeeping out of the analytical input."""
    if manifest.get("consolidation"):
        return {"run_id": manifest["run_id"], "phase": "history-final", "manifest_path": str(path),
                "sources_count": len(manifest["sources"]), "consolidation": manifest["consolidation"]}
    return {**{k: v for k, v in manifest.items() if k != "baseline"},
            "baseline_exclusions": {"count": len(manifest["baseline"]), "reviewed": False,
                                    "path": str(path), "manifest_hash": digest(manifest)}}


def worker_prompt(settings, manifest, stage, input_path):
    model, effort = ("gpt-6.1-sol", "medium") if stage == "sol" else ("gpt-6-astra", "high")
    duty = ("Investigate originals, named follow-ups, related current work/rules and prior decisions; group repeated issues. "
            "Verify current defects rather than treating retros as conclusions. Draft one prioritized packet.") if stage == "sol" else (
            "Read EVERY original and ALL Sol dispositions/draft, including no-action. Detect omitted insights and evidence gaps. "
            "Correct small gaps directly, flag substantial uncertainty without returning to Sol, finalize other decisions.")
    if daily_manifest(manifest) and stage == "sol":
        duty = duty.replace("Draft one prioritized packet.", "Return one prioritized final packet; there is no second producer pass.")
    report_id = read(input_path)["report_id"]
    if stage == "final-astra":
        return f"""Final historical relevance/dedupe review; Codex gpt-6-astra/high, READ ONLY.
Read immutable input JSON {input_path}; memory root {settings.memory_root}. This is a findings review, not a repeated original investigation. Read the complete catalogue in collection.consolidation and prior reviews. Verify current relevant work/decisions; retained per-batch paths/hashes give every original/disposition for targeted verification. No edits, questions, new lanes, delivery or improvement execution. One report self-closes your generation.
For every alias_id return exactly one keep/drop selection with reason and current bart_attention. The original candidate is retained by reference; include a full candidate override ONLY when action, current existing/owner/decision or evidence requires correction. Retain all citation IDs/evidence_citations and candidate id. Drop deprecated, retired, shipped/resolved or already-owned unchanged subjects ONLY with verified evidence. KEEP recurrence, changed_evidence, ownership_gap, revised_action, new_grant, no_owning_work or uncertain cases even on linked/shipped work. An existing work link or triage backlog alone cannot suppress. Empty bart_attention.reasons requires a verified unchanged/excluded subject; include rationale. Full audit preserves dropped findings. Finding versions are computed by producer; do not invent hashes.
Return agent-orch report --msg-id 0 --status done --report-id {report_id} --result JSON (ReportPayloadV1 summary/findings/next_action/extras). extras.daily_retro={{run_id:"{manifest['run_id']}",input_digest:"{digest(manifest['consolidation']['catalogue'])}",selections:[{{alias_id,disposition:"keep"|"drop",reason,bart_attention,candidate(optional override)}}]}}. No omitted/invented aliases. Prior checkpoint actions are historical custody, never a new commission. Preserve recurrence/changed evidence and genuine uncertainty.
"""
    history_duty = ""
    identity_duty = (" Each daily candidate may add work_ids as a list of exact target work IDs, and identity_citations as structured {store,record_id} records. Do not infer targets from free text. Omitted identity citations derive from original source IDs; an explicit empty list means no identity citation. The collector computes candidate_key/version and merges findings with one equal target; do not invent keys or hashes." if daily_manifest(manifest) else "")
    if manifest.get("schema_version") == 2:
        history_duty = """\nContinuous intake: each candidate MUST include recommendation_kind=no_change|resolved|duplicate|future_work|immediate_work|investigate. No_change means evidence establishes no change is needed; future work is future_work even if an unassigned backlog exists. Verify accepting owner receipt, checkable dated/named-event checkpoint and success measure. Challenge missing acceptance, overdue triggers and recurrence using existing work; never scan free-text keywords to infer dispositions. Primary Bart metadata is independent evidence from the same bounded window. Read every observed failure/unexplained-stall entry and state next action at the first applicable digest. Report overflow, missing provenance and unknown correlation explicitly; a report, terminal badge or PID never proves operator publication. No provider/tool logs. Preserve live-tool, evidenced-wait and retained-planner distinctions. Old Retro text never establishes today's health.\n"""
    if manifest.get("cumulative_context"):
        context = manifest["cumulative_context"]
        history_duty = f"""\nHistorical serial context: read {context['path']} (SHA256 {context['sha256']}); all prior findings, source hashes, reviews and work references are retained there. Read every new original for coverage, but skip renewed detailed investigation of an equivalent unchanged known finding; disposition cites prior finding key/version and current original. A new recurrence/evidence/owner/action/grant is a new version and must be evaluated. A known issue alone is not proof of deprecation. No silent truncation.
Each candidate adds finding_key (stable lowercase issue label <=120 chars) and bart_attention={{reasons:[no_owning_work|new_grant|recurrence|changed_evidence|ownership_gap|revised_action|uncertain],rationale}}. Producer computes finding_version. Drop deprecated/retired/shipped/resolved/currently accepted owned unchanged subjects by empty reasons ONLY with current evidence in existing/owner/action/decision/rationale. Keep every exception even on existing work; unassigned triage is not an executing owner. Ambiguous relevance remains uncertain. Existing-work link alone never suppresses. Keep all candidates/full source dispositions, including unchanged exclusions, in this private packet; producer selects checkpoint relevance.\n"""
    return f"""Daily retrospective {stage} pass, run {manifest['run_id']}; {model}/{effort}, Codex only.
{duty}
Read immutable input JSON {input_path}; memory root {settings.memory_root}. Look up only relevant normal work records and bounded recent packets under {settings.state_root}/runs for recurrence/prior decisions. Respect existing standing grants.{history_duty}
READ ONLY: no edits, questions, publication, new lanes or feedback pass. No authority to execute improvements. Your terminal report self-closes this generation; producer owns only fenced cleanup.
Return one durable report: agent-orch report --msg-id 0 --status done --report-id {report_id} --result JSON. ReportPayloadV1 summary/findings/next_action/extras; extras.daily_retro is the packet object.
Packet: run_id; dispositions [{{id,fingerprint,reason}}] EXACTLY once for each original, including no-action; candidates unique prioritized [{{id,problem,consequence,citations:[source IDs],prior_occurrences,existing,action,benefit,effort,risk,uncertainty,owner,decision}}]. decision describes exact needed grant/recommended option/meaningful alternatives, or existing authorization/no decision. Candidates.citations contain ONLY IDs from collection.sources; put verification labels, file paths and related-work references in evidence_sources/evidence_citations or existing, never in original citations. Each recommendation needs at least one collected original ID. Existing fields name authoritative fixes/rules/work and current state.{identity_duty} No-new sources is an explicit successful empty packet; coverage gaps never imply all-clear. Preserve prior materially changed recommendations/actions needing attention. Keep front concise, no silent source truncation.
"""


def validate_proposal(proposal, *, require_v2=False):
    if not require_v2 and proposal.get("schema_version") != 2:
        return
    if proposal.get("schema_version") != 2:
        raise ValueError("new deferral requires schema_version 2")
    owner = proposal.get("owner")
    acceptance = proposal.get("owner_acceptance")
    if not isinstance(owner, str) or not owner.strip() or not isinstance(acceptance, dict):
        raise ValueError("accepting owner receipt required")
    if acceptance.get("owner") != owner or not isinstance(acceptance.get("receipt"), str) or not acceptance["receipt"].strip():
        raise ValueError("owner acceptance must match the responsible owner and cite a receipt")
    if aware(acceptance.get("accepted_at")) > datetime.now(timezone.utc):
        raise ValueError("owner acceptance must already have occurred")
    checkpoint = proposal.get("checkpoint")
    if not isinstance(checkpoint, dict) or checkpoint.get("owner") != owner:
        raise ValueError("checkpoint requires responsible owner")
    if bool(checkpoint.get("at")) == bool(checkpoint.get("event")):
        raise ValueError("checkpoint requires one date or named event")
    if checkpoint.get("at"):
        aware(checkpoint["at"])
    elif (not isinstance(checkpoint["event"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{1,99}", checkpoint["event"])
          or not checkpoint.get("trigger_ref")):
        raise ValueError("named checkpoint requires stable event and checkable trigger_ref")
    if checkpoint.get("review_at"):
        aware(checkpoint["review_at"])
    if not isinstance(proposal.get("success_measure"), str) or not proposal["success_measure"].strip():
        raise ValueError("concrete success_measure required")
    if proposal.get("disposition") == "resolved":
        validate_outcome(proposal.get("outcome_evidence"))


def validate_outcome(evidence):
    if not isinstance(evidence, dict) or not isinstance(evidence.get("receipt"), str) or not evidence["receipt"].strip() or not evidence.get("measure"):
        raise ValueError("resolved requires observed outcome_evidence receipt and measure")
    if aware(evidence.get("observed_at")) > datetime.now(timezone.utc):
        raise ValueError("outcome must already have been observed")
    if evidence.get("shipped_at") and aware(evidence["shipped_at"]) > datetime.now(timezone.utc):
        raise ValueError("shipment must already have been observed")


def proposal_version(proposal):
    fields = ("scope", "title", "body", "options")
    if proposal.get("schema_version") == 2:
        fields += ("schema_version", "disposition", "owner", "owner_acceptance", "checkpoint", "success_measure", "authority", "citations")
    return digest({k: proposal.get(k) for k in fields})


def weekly_gap_accounting(settings, days):
    """Project distinct source inventories, retaining unknown scan coverage."""
    snapshots, excluded = [], []
    for path in sorted((settings.state_root / "runs").glob("*/collection.json")):
        day = path.parent.name
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            continue
        try:
            parsed_day = datetime.fromisoformat(day).date()
        except ValueError:
            excluded.append({"run_id": day, "reason": "invalid run date"})
            continue
        if day > days[-1]:
            continue
        try:
            collection = read(path)
            captured = aware(collection["collected_at"])
            if (collection.get("run_id") != day or collection.get("timezone") != ZONE.key
                    or captured.astimezone(ZONE).date() != parsed_day):
                raise ValueError("snapshot metadata mismatch")
            gaps = collection.get("gaps")
            if not isinstance(gaps, list):
                raise ValueError("source inventory unavailable")
            keys = set()
            for gap in gaps:
                if not isinstance(gap, dict):
                    raise ValueError("invalid gap row")
                source, reason = gap.get("path") or gap.get("source"), gap.get("reason")
                if not isinstance(source, str) or not source or not isinstance(reason, str) or not reason:
                    raise ValueError("invalid gap key")
                keys.add((source, reason))
            snapshots.append({"run_id": day, "collected_at": collection["collected_at"],
                              "sha256": digest(collection), "keys": keys})
        except (ValueError, TypeError, KeyError, AttributeError, OSError):
            excluded.append({"run_id": day, "reason": "snapshot inventory unavailable or invalid"})

    def pointer(snapshot):
        return {k: snapshot[k] for k in ("run_id", "collected_at", "sha256")} if snapshot else None

    current = snapshots[-1] if snapshots else None
    seed = snapshots[0] if snapshots else None
    baseline = {key for key in seed["keys"] if key[1] == "missing Retro"
                and key[0].startswith(("work/completed/", "work/deprecated/"))} if seed else set()
    week = [row for row in snapshots if days[0] <= row["run_id"] <= days[-1]]
    prior = [row for row in snapshots if row["run_id"] < days[0]]
    comparison = prior[-1] if prior else week[0] if week else None
    observed = set().union(*(row["keys"] for row in week)) if week else set()
    current_keys = current["keys"] if current else set()
    status = "unavailable" if not current else "current" if current["run_id"] == days[-1] else "stale"
    additions = len(observed - comparison["keys"]) if week and comparison else None
    actionable = len(current_keys - baseline) if current else None
    baseline_current = len(current_keys & baseline) if current else None
    number = lambda value: "unknown" if value is None else str(value)
    headline = (f"New this week: {number(additions)}; actionable current: {number(actionable)} ({status}).\n"
                f"Retained initial terminal-format baseline: {number(baseline_current)}.")
    return {"status": status, "headline": headline,
            "distinct_current": len(current_keys) if current else None,
            "current": pointer(current), "baseline": pointer(seed),
            "baseline_kind": "retained initial terminal-format inventory; age before convention unproven",
            "baseline_initial": len(baseline),
            "baseline_current": baseline_current,
            "actionable_current": actionable,
            "new_this_week": additions,
            "comparison": pointer(comparison),
            "comparison_mode": "prior_snapshot" if prior and week else "first_observed" if week else "unavailable",
            "missing_dates": [day for day in days if day not in {row["run_id"] for row in week}],
            "excluded_snapshots": excluded,
            "coverage_limit": "Distinct source inventories, not resolved work or fleet health. Missing scans cannot prove resolution. First-observed comparison has an unknown left boundary; stale snapshots are not current scans. Primary gaps have separate sampled denominators."}


def retained_final_path(root):
    """Stored legacy final/ownership decides compatibility, never current config."""
    manifest = read(root / "collection.json", {"run_id": root.name})
    legacy = read(root / "astra.json", {})
    return root / ("astra.json" if not daily_manifest(manifest) or legacy.get("packet") or legacy.get("stream_id") else "sol.json")


def retained_final(root):
    stage = read(retained_final_path(root))
    return stage if stage and stage.get("packet") is not None else None


def observed_outcome(status, baseline):
    """Physical-folder observation only; never proof of shipment or causation."""
    if status is None:
        return "not_found"
    order = ("backlog", "analysis", "ready_for_dev", "in_progress", "needs_qa", "completed")
    valid = set(order) | {"deprecated", "blocked"}
    if not isinstance(baseline, str) or baseline not in valid or status not in valid:
        return "legacy_unknown"
    if status == "deprecated":
        return "observed_dropped"
    if status == "completed":
        return "observed_completed"
    if status == "blocked" or baseline in {"completed", "deprecated"}:
        return "observed_regressed"
    if baseline == "blocked":
        return "observed_progressed"
    if order.index(status) < order.index(baseline):
        return "observed_regressed"
    if order.index(status) > order.index(baseline):
        return "observed_progressed"
    return "observed_unchanged"


def read_usage_rollup(settings):
    """Read the existing unbounded CLI envelope; no aggregate/time inference."""
    identity = getattr(settings, "usage_spec_id", None)
    if not valid_work_id(identity):
        return None
    try:
        response = subprocess.run(["agent-orch", "usage", "rollup", "--spec", identity, "--json"],
                                  capture_output=True, text=True, check=True, timeout=30)
        envelope = json.loads(response.stdout)
        specs = envelope.get("specs") if isinstance(envelope, dict) else None
        if not isinstance(specs, list):
            return None
        matching = [row for row in specs if isinstance(row, dict) and row.get("spec_id") == identity]
        return matching[0] if len(matching) == 1 else None
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None


def recorded_producer_streams(root):
    streams = set()
    def retain(row):
        if not isinstance(row, dict):
            return
        admission = row.get("admission")
        admission = admission if isinstance(admission, dict) else {}
        session = admission.get("session")
        session = session if isinstance(session, dict) else {}
        for identity in (row.get("stream_id"), admission.get("stream_id"), session.get("stream_id")):
            if isinstance(identity, str) and identity:
                streams.add(identity)
    for name in STAGES:
        retain(read(root / f"{name}.json", {}))
        attempts = read(root / f"{name}-attempts.json", [])
        if isinstance(attempts, list):
            for attempt in attempts:
                retain(attempt)
    summary = read(root / "summary.json", {})
    cost = summary.get("producer_cost", {}) if isinstance(summary, dict) else {}
    for identity in cost.get("streams", []) if isinstance(cost, dict) else []:
        if isinstance(identity, str) and identity:
            streams.add(identity)
    return sorted(streams)


def producer_cost(settings, run_id, rollup):
    streams = recorded_producer_streams(settings.state_root / "runs" / run_id)
    unknown = {"state": "unknown", "dollars": None, "streams": streams}
    if not valid_work_id(getattr(settings, "usage_spec_id", None)):
        return unknown
    codex = rollup.get("codex") if isinstance(rollup, dict) else None
    rows = codex.get("by_stream") if isinstance(codex, dict) else None
    if not isinstance(rows, list) or not streams:
        return unknown
    by_id = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("stream_id"), str):
            by_id.setdefault(row["stream_id"], []).append(row)
    usable, complete = [], True
    for identity in streams:
        matches = by_id.get(identity, [])
        row = matches[0] if len(matches) == 1 else {}
        dollars = row.get("dollars")
        if isinstance(dollars, bool) or not isinstance(dollars, (int, float)) or not math.isfinite(dollars) or dollars < 0:
            complete = False
            continue
        usable.append(dollars)
        value = row.get("completeness")
        complete = complete and not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value == 1.0
    if not usable:
        return unknown
    return {"state": "measured" if complete else "partial", "dollars": sum(usable), "streams": streams}


def weekly_summary(settings, end_day):
    """Account from retained receipts; missing measurements stay unknown."""
    end = datetime.fromisoformat(end_day).date()
    days = [(end - timedelta(days=n)).isoformat() for n in range(6, -1, -1)]
    result = {"window": {"start": days[0], "end": days[-1]}, "runs_collected": 0, "runs_reviewed": 0,
        "missing_runs": [], "unreviewed_runs": [], "sources_selected": 0, "sources_reviewed": 0,
        "candidates_observed": 0, "candidates_reviewed": 0, "dispositions": {k: 0 for k in sorted(DISPOSITIONS)},
        "legacy_review_runs": [], "retro_coverage": {"gaps": 0, "deferred": 0, "baseline_not_reviewed": 0},
        "primary_coverage": {"runs_with_primary": 0, "gaps": 0, "tables": {}, "deferred_by_kind": {},
            "observations": {"observed": 0, "selected": 0, "urgent": 0, "deferred_urgent": 0, "deferred": 0},
            "completion_correlation": {"observed_done": 0, "explicitly_published": 0, "unknown": 0},
            "reference_deferred": 0},
        "completion_to_publication_seconds": {"observed": 0, "values": []},
        "dot_blocked_time": "unknown unless measured in owning milestone receipts",
        "dot_rework": "unknown unless measured in owning milestone receipts",
        "candidate_identities": [], "coverage": {"unkeyed": 0},
        "current_work": [], "current_work_counts": {"defer": 0, "propose": 0, "authorized": 0, "investigate": 0},
        "verified_outcomes": 0, "shipped_observed": 0, "unverified_ownership": 0, "oldest_unresolved": None,
        "dot_measurements": {"sample_receipts": [], "blocked_seconds": [], "correction_rounds": [], "avoidable_stops": []}}
    result["runs_total"], result["runs_failed"] = 0, 0
    result["coverage"]["excluded_runs"] = []
    for root in sorted((settings.state_root / "runs").glob("*")):
        if root.is_dir() and (root.name.startswith("history-") or (root.name in days and not (root / "collection.json").is_file())):
            result["coverage"]["excluded_runs"].append({"run_id": root.name,
                "reason": "history" if root.name.startswith("history-") else "orphan without collection"})
    result["producer_costs"] = []
    rollup = read_usage_rollup(settings)
    outcome_receipts = set()

    def account_outcome(outcome):
        try:
            validate_outcome(outcome)
        except (ValueError, TypeError):
            return False
        if outcome["receipt"] not in outcome_receipts:
            outcome_receipts.add(outcome["receipt"])
            observed_day = aware(outcome["observed_at"]).astimezone(ZONE).date().isoformat()
            in_week = days[0] <= observed_day <= days[-1]
            result["verified_outcomes"] += int(in_week)
            if outcome.get("shipped_at"):
                shipped_day = aware(outcome["shipped_at"]).astimezone(ZONE).date().isoformat()
                result["shipped_observed"] += int(days[0] <= shipped_day <= days[-1])
            if outcome.get("measurement_scope") == "dot_milestone" and in_week:
                measurements = result["dot_measurements"]
                measurements["sample_receipts"].append(outcome["receipt"])
                for key in ("blocked_seconds", "correction_rounds", "avoidable_stops"):
                    value = outcome.get(key)
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                        measurements[key].append(value)
        return True

    linked = {}
    for day in days:
        root = settings.state_root / "runs" / day
        if (root / "collection.json").is_file():
            result["runs_total"] += 1
            delivery = read(root / "delivery.json", {})
            failed = any(path.is_file() for path in root.glob("failure*.json")) or any(
                isinstance(attempt, dict) and attempt.get("confirmed") is False for attempt in delivery.get("attempts", []))
            result["runs_failed"] += int(failed)
        collection = read(root / "collection.json") if (root / "collection.json").is_file() else None
        if not collection:
            result["missing_runs"].append(day)
            continue
        result["runs_collected"] += 1
        result["producer_costs"].append({"run_id": day, "producer_cost": producer_cost(settings, day, rollup),
                                         "fd_cost": {"unknown_reason": "shared_fd_seat"}})
        result["sources_selected"] += len(collection["sources"])
        for key in ("deferred", "baseline_not_reviewed"):
            result["retro_coverage"][key] += collection.get("coverage", {}).get(key, 0)
        primary = collection.get("primary")
        if primary:
            pc = result["primary_coverage"]
            pc["runs_with_primary"] += int(bool(primary.get("store")))
            pc["gaps"] += len(primary.get("gaps", []))
            coverage = primary.get("coverage", {})
            for name, counts in coverage.items():
                if {"observed", "scanned", "overflow"} <= counts.keys():
                    total = pc["tables"].setdefault(name, {"observed": 0, "scanned": 0, "overflow": 0})
                    for key in total:
                        total[key] += counts[key]
            for key in pc["observations"]:
                pc["observations"][key] += (primary.get("deferred", {}).get("count", 0) if key == "deferred"
                                           else coverage.get("observations", {}).get(key, 0))
            for kind, count in primary.get("deferred", {}).get("by_kind", {}).items():
                pc["deferred_by_kind"][kind] = pc["deferred_by_kind"].get(kind, 0) + count
            for key in pc["completion_correlation"]:
                pc["completion_correlation"][key] += coverage.get("completion_correlation", {}).get(key, 0)
            pc["reference_deferred"] += sum(c.get("deferred", 0) for c in primary.get("reference_coverage", {}).values())
            values = [o["completion_to_publication_seconds"] for o in primary.get("observations", []) if "completion_to_publication_seconds" in o]
            result["completion_to_publication_seconds"]["values"].extend(values)
        else:
            result["primary_coverage"]["gaps"] += 1
        final, review = retained_final(root), read(root / "review.json")
        if final:
            result["candidates_observed"] += len(final["packet"]["candidates"])
            for candidate in final["packet"]["candidates"]:
                key = candidate.get("candidate_key", "legacy_unknown")
                result["candidate_identities"].append({"run_id": day, "id": candidate["id"],
                    "candidate_key": key, "version": candidate.get("version", "legacy_unknown")})
                result["coverage"]["unkeyed"] += int(key.startswith("unkeyed:"))
        if not review:
            result["unreviewed_runs"].append(day)
            continue
        if not final or review["result"].get("packet_hash") != final.get("packet_hash"):
            raise ValueError("weekly retained review/packet hash mismatch")
        result["runs_reviewed"] += 1
        result["sources_reviewed"] += len(collection["sources"])
        if final["packet"].get("schema_version") != 2:
            result["legacy_review_runs"].append(day)
        for row in review["result"]["dispositions"]:
            result["candidates_reviewed"] += 1
            result["dispositions"][row["disposition"]] += 1
            if row["disposition"] == "resolved":
                account_outcome(row.get("outcome_evidence"))
            if row.get("work_id") and row.get("proposal_id"):
                linked[(row["work_id"], row["proposal_id"])] = review["reviewed_at"]
    # Older retained work can still be the oldest unresolved issue; the daily
    # denominators above remain bounded to this week's receipts.
    for path in sorted((settings.state_root / "runs").glob("*/review.json")):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.parent.name) or path.parent.name > end_day:
            continue
        review = read(path)
        for row in review["result"]["dispositions"]:
            if row.get("work_id") and row.get("proposal_id"):
                key = (row["work_id"], row["proposal_id"])
                linked[key] = min(linked.get(key, review["reviewed_at"]), review["reviewed_at"])
    # These immutable pointers are helper receipts, not a second authority
    # ledger. The proposal in the normal work record remains authoritative.
    latest_decisions = {}
    for path in sorted((settings.state_root / "decision-receipts").glob("*.json")):
        receipt = read(path)
        local_day = aware(receipt["recorded_at"]).astimezone(ZONE).date().isoformat()
        if local_day <= days[-1]:
            key = (receipt["work_id"], receipt["proposal_id"])
            linked[key] = min(linked.get(key, receipt["recorded_at"]), receipt["recorded_at"])
            if key not in latest_decisions or aware(receipt["recorded_at"]) > aware(latest_decisions[key]["recorded_at"]):
                latest_decisions[key] = receipt
    for (work_id, identity), first_seen in sorted(linked.items()):
        missing = False
        try:
            path = work_path(settings, work_id, authorization=True)
            status = path.resolve().parent.parent.name
            record = proposals(path.read_text()).get(identity)
        except (ValueError, OSError) as exc:
            record = None
            missing = isinstance(exc, FileNotFoundError) or str(exc) == "unknown_work_id"
        if not record:
            entry = {"work_id": work_id, "proposal_id": identity, "coverage": "proposal unavailable",
                     "outcome": "legacy_unknown"}
            receipt = latest_decisions.get((work_id, identity), {})
            if missing:
                entry["outcome"] = "not_found"
            if receipt:
                # Receipt creation time cannot reconstruct replay chronology or
                # current authority after the normal work record disappears.
                entry["decision_receipt"] = {key: receipt[key] for key in
                    ("version", "state", "recorded_at", "baseline_status") if key in receipt}
                entry["decision_receipt"]["evidence_scope"] = "historical receipt; current proposal unavailable"
            result["current_work"].append(entry)
            continue
        entry = {"work_id": work_id, "proposal_id": identity, "version": record["version"],
            "state": record.get("state"), "disposition": record.get("disposition"), "first_observed_at": first_seen,
            "owner": record.get("owner"), "owner_acceptance": record.get("owner_acceptance"),
            "checkpoint": record.get("checkpoint"), "success_measure": record.get("success_measure")}
        if "baseline_status" in record:
            entry["baseline_status"] = record["baseline_status"]
        if record.get("state") == "authorized":
            entry["outcome"] = observed_outcome(status, record.get("baseline_status"))
        try:
            validate_proposal(record, require_v2=True)
            entry["ownership_coverage"] = "schema2 receipt; independent verification required"
        except (ValueError, TypeError):
            entry["ownership_coverage"] = "legacy or unverified acceptance/checkpoint"
            result["unverified_ownership"] += 1
        outcome = record.get("outcome_evidence")
        if record.get("disposition") in result["current_work_counts"] and record.get("state") not in {"resolved", "shipped", "rejected", "answered"}:
            result["current_work_counts"][record["disposition"]] += 1
        if account_outcome(outcome):
            entry["outcome_evidence"] = outcome
        else:
            entry.setdefault("outcome", "legacy_unknown")
            if not result["oldest_unresolved"] or first_seen < result["oldest_unresolved"]["first_observed_at"]:
                result["oldest_unresolved"] = {"work_id": work_id, "proposal_id": identity, "first_observed_at": first_seen}
        checkpoint = record.get("checkpoint")
        if isinstance(checkpoint, dict):
            due = checkpoint.get("at") or checkpoint.get("review_at")
            entry["checkpoint_state"] = ("met by observed outcome" if "outcome_evidence" in entry else
                "overdue" if due and aware(due).astimezone(ZONE).date() <= end else
                "future" if due else "event occurrence unknown")
            if checkpoint.get("event"):
                entry["event_occurrence"] = "unknown; inspect trigger_ref"
        else:
            entry["checkpoint_state"] = "legacy/uncheckable"
        result["current_work"].append(entry)
    result["gap_accounting"] = weekly_gap_accounting(settings, days)
    result["retro_coverage"]["gaps"] = result["gap_accounting"]["distinct_current"] or 0
    result["completion_to_publication_seconds"]["observed"] = len(result["completion_to_publication_seconds"]["values"])
    for key in ("blocked_seconds", "correction_rounds", "avoidable_stops"):
        values = result["dot_measurements"][key]
        result["dot_measurements"][key] = {"observed": len(values), "total": sum(values) if values else None}
    result["coverage_limit"] = "Observed retained daily samples, not universal health. Windows can overlap; counts are sampled receipts, not unique fleet incidents. Outcomes cover retained resolved review rows and current normal-work proposal metadata; prior proposal versions without retained outcome receipts are unknown. Legacy reasons were not semantically validated."
    return result


def retain_weekly_summary(settings, run_id):
    day = datetime.fromisoformat(run_id).date()
    rolling = weekly_summary(settings, run_id)
    summary_path = settings.state_root / "runs" / run_id / "summary.json"
    if not summary_path.exists():
        current = next((row["producer_cost"] for row in rolling["producer_costs"] if row["run_id"] == run_id),
                       {"state": "unknown", "dollars": None, "streams": []})
        atomic(summary_path, {**rolling, "producer_cost": current, "fd_cost": {"unknown_reason": "shared_fd_seat"}})
    # Sunday closes the local week. A later first review catches up the prior
    # week; no timer or additional model is admitted.
    week_end = day if day.weekday() == 6 else day - timedelta(days=day.weekday() + 1)
    year, week, _ = week_end.isocalendar()
    path = settings.state_root / "weekly" / f"{year}-W{week:02}.json"
    with locked(settings.state_root / "weekly.lock"):
        old = read(path)
        if old:
            return {"path": str(path), "sha256": digest(old), "publication_key": "daily-retro-weekly-" + old["week"],
                    "due": old["generated_by_run"] == run_id, "summary": old["summary"]}
        summary = rolling if week_end == day else weekly_summary(settings, week_end.isoformat())
        receipt = {"week": f"{year}-W{week:02}", "generated_by_run": run_id, "summary": summary}
        atomic(path, receipt)
        return {"path": str(path), "sha256": digest(receipt), "publication_key": "daily-retro-weekly-" + receipt["week"],
                "due": True, "summary": summary}


# A daemon restart surfaces as a refused/reset socket or a closed websocket.
# Nothing else (a missing token file, a disk error) is a transport loss.
TRANSPORT_ERRORS = (ConnectionError, ConnectionClosed)
# Failure records never carry exception text: free text can hold a credential
# in forms no pattern list anticipates. A record names the exception class, a
# fixed reason code, and the byte length and sha256 of the raw message. The raw
# text stays only in the local run directory (0600) and is never sent.
_ERROR_FIELDS = ("class", "reason", "bytes", "sha256")
_SHA256 = re.compile(r"[0-9a-f]{64}")
# Both vocabularies are fixed by code, never by the failing value. A class is
# named only when it is the genuine class of a trusted module (so a dynamically
# created class cannot smuggle text in its name); anything else is named by
# its nearest trusted base.
_TRUSTED_ERROR_MODULES = ("builtins", "asyncio.exceptions", "concurrent.futures._base", "json.decoder",
                          "sqlite3", "websockets.exceptions")
_RECORD_CLASSES = ("LegacyText", "WorkerReport")
_REASONS = frozenset({"transport_loss", "timeout", "error", "interrupted", "legacy_text", "worker_failed",
                      "os_error", *(f"os_error:{name}" for name in errno.errorcode.values())})


def _trusted_class_name(cls):
    module = sys.modules.get(cls.__module__)
    if cls.__module__ in _TRUSTED_ERROR_MODULES and module is not None and getattr(module, cls.__name__, None) is cls:
        return cls.__name__
    return None


def _error_class_names():
    import concurrent.futures  # noqa: F401 - loads concurrent.futures._base
    names = set(_RECORD_CLASSES)
    for name in _TRUSTED_ERROR_MODULES:
        for value in vars(sys.modules[name]).values():
            if isinstance(value, type) and issubclass(value, BaseException) and _trusted_class_name(value):
                names.add(value.__name__)
    return frozenset(names)


_ERROR_CLASSES = _error_class_names()


def _error_class(exc):
    return next(name for name in map(_trusted_class_name, type(exc).__mro__) if name)


def _reason_code(exc):
    if isinstance(exc, TRANSPORT_ERRORS):
        return "transport_loss"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, OSError):
        return "os_error" + (f":{errno.errorcode[exc.errno]}" if exc.errno in errno.errorcode else "")
    return "error" if isinstance(exc, Exception) else "interrupted"


def _is_structured(value):
    return (isinstance(value, dict) and set(value) == set(_ERROR_FIELDS)
            and isinstance(value["class"], str) and value["class"] in _ERROR_CLASSES
            and isinstance(value["reason"], str) and value["reason"] in _REASONS
            and type(value["bytes"]) is int and value["bytes"] >= 0
            and isinstance(value["sha256"], str) and _SHA256.fullmatch(value["sha256"]) is not None)


def structured_error(exc, root=None):
    """Durable failure record: {class, reason, bytes, sha256}, never message text.

    Idempotent: a genuine structured record is returned unchanged. Anything
    else that is not an exception (a legacy text record, or a dict that is not
    a valid record) is treated as raw text. With `root`, the raw text is kept
    at root/errors/<sha256>.txt (0600) for local diagnosis."""
    if _is_structured(exc):
        return dict(exc)
    if isinstance(exc, BaseException):
        return _text_record(str(exc), root, _error_class(exc), _reason_code(exc))
    return _text_record(exc if isinstance(exc, str) else encoded(exc).decode(), root, "LegacyText", "legacy_text")


def _text_record(raw, root, record_class, reason):
    data = raw.encode("utf-8", "backslashreplace")
    sha = hashlib.sha256(data).hexdigest()
    if root is not None:
        _keep_raw_error(Path(root), sha, data)
    return {"class": record_class, "reason": reason, "bytes": len(data), "sha256": sha}


_FAILURE_ERROR_FIELDS = ("error", "cleanup_error", "notice_error")
#: Failure-notice attempts written with a structured body (older ones are rebuilt).
FAILURE_BODY_FORMAT = 2


def _structured_failure_entry(entry, root):
    """A failure.json entry with every error field structured (retained
    entries from before structured records are converted in place)."""
    return {**entry, **{k: structured_error(entry[k], root) for k in _FAILURE_ERROR_FIELDS if k in entry}}


_WORKER_RECEIPT_FIELDS = frozenset({"result_kind", "status", "report_id", "ledger_row_id", "error"})


def _worker_failure_receipt(response, root, report_id):
    """What a failed or invalid worker report keeps: identity and outcome
    metadata plus a structured record of the whole response, never its text."""
    report = response.get("report") if isinstance(response.get("report"), dict) else response
    status = report.get("status") if isinstance(report, dict) else None
    row = response.get("ledger_row_id") if "ledger_row_id" in response else (report or {}).get("ledger_row_id")
    kind = response.get("result_kind")
    return {"result_kind": kind if isinstance(kind, str) and kind in {"report", "closed_without_report"} else None,
            "status": status if isinstance(status, str) and status in {"done", "error", "aborted"} else None,
            "report_id": report_id if (report or {}).get("report_id") == report_id else None,
            "ledger_row_id": row if type(row) is int else None,
            "error": _text_record(encoded(response).decode(), root, "WorkerReport", "worker_failed")}


def _projected_stage(stage, root):
    """A failed stage receipt retained from before projection: replace a raw
    failure response or failed report with its receipt."""
    if not stage.get("failed"):
        return stage
    stage = dict(stage)
    failure = stage.get("failure")
    if isinstance(failure, dict) and not _is_worker_receipt(failure, stage.get("report_id")):
        stage["failure"] = _worker_failure_receipt(failure, root, stage.get("report_id"))
    elif isinstance(failure, str) and failure != "invalid terminal packet":
        stage["failure"] = structured_error(failure, root)
    report = stage.get("report")
    if isinstance(report, dict) and not _is_worker_receipt(report, stage.get("report_id")):
        stage["report"] = _worker_failure_receipt({"report": report}, root, stage.get("report_id"))
    return stage


def _is_worker_receipt(value, report_id):
    """Exactly the projected receipt schema: any extra or non-conforming field
    means the value is raw and is projected again."""
    return (isinstance(value, dict) and set(value) == _WORKER_RECEIPT_FIELDS
            and (value["result_kind"] is None or (isinstance(value["result_kind"], str)
                                                  and value["result_kind"] in {"report", "closed_without_report"}))
            and (value["status"] is None or (isinstance(value["status"], str)
                                             and value["status"] in {"done", "error", "aborted"}))
            and (value["report_id"] is None or value["report_id"] == report_id)
            and (value["ledger_row_id"] is None or type(value["ledger_row_id"]) is int)
            and _is_structured(value["error"]))


def render_error(record):
    record = structured_error(record)
    return f"{record['class']} reason={record['reason']} bytes={record['bytes']} sha256={record['sha256'][:16]}"


def _keep_raw_error(root, name, data):
    """Keep raw text at root/errors/<name>.txt. Best effort: losing the local
    copy must never fail failure recording."""
    try:
        folder = root / "errors"
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
        fd = os.open(folder / f"{name}.txt", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, data)
        finally:
            os.close(fd)
    except OSError:
        pass


def record_failure(root, run_id, *, stage, error, notice):
    """Append one failure to the run's durable failure state and return its seq.

    The top-level fields always describe the latest failure; earlier ones move
    to a bounded per-failure `history`, each keeping its own notice state."""
    path = root / "failure.json"
    state = _structured_failure_entry(read(path, {}), root)
    history = [_structured_failure_entry(h, root) for h in state.get("history", [])]
    if state.get("error"):
        history.append({k: state[k] for k in ("seq", "at", "stage", "error", "notice", "cleanup_error", "notice_error") if k in state})
    seq = int(state.get("seq") or len(history)) + 1
    atomic(path, {"run_id": run_id, "seq": seq, "at": now_iso(), "stage": stage, "error": error,
                  "notice": notice, "latest": True, "history": history[-8:]})
    return seq


def annotate_failure(root, run_id, seq=None, **fields):
    """Merge fields into the latest failure (or the history entry for `seq`)."""
    path = root / "failure.json"
    state = read(path, {})
    if seq is not None and state.get("seq") != seq:
        state["history"] = [{**h, **fields} if h.get("seq") == seq else h for h in state.get("history", [])]
        atomic(path, state)
        return
    atomic(path, {**state, "run_id": run_id, **fields})


class Pipeline:
    def __init__(self, settings, rpc=wsclient):
        self.settings, self.rpc, self.config = settings, rpc, settings.rpc()

    async def binding(self):
        binding = checked(await self.rpc.assistant_once(self.config, {"type": "assistant.binding"}), "assistant.binding.ok")
        binding["session_generation"] = binding.get("generation") or binding.get("session_generation")
        if not binding.get("stream_id") or not binding.get("session_generation"):
            raise RuntimeError("current assistant binding unavailable")
        return binding

    async def worker(self, manifest, name, prior=None):
        root = self.settings.state_root / "runs" / manifest["run_id"]
        receipt_path = root / f"{name}.json"
        stage = read(receipt_path, {})
        daily = daily_manifest(manifest)
        if daily and self.settings.producers != ["sol"]:
            raise ValueError("producers must be exactly [sol]")
        if stage.get("packet"):
            return stage
        if daily and name != "sol":
            if not stage.get("stream_id") or stage.get("failed"):
                raise RuntimeError("legacy Astra cannot admit a new or replacement producer")
            if not stage.get("generation"):
                raise RuntimeError("owned generation unproven; cleanup and new admission blocked")
        if daily and (not stage.get("stream_id") or stage.get("failed")):
            if not valid_work_id(self.settings.usage_spec_id):
                raise ValueError("usage_spec_id required for new daily producer admission")
            if stage and not stage.get("failed") and stage.get("payload", {}).get("spec_id") != self.settings.usage_spec_id:
                raise ValueError("retained producer admission usage_spec_id mismatch; exact intent preserved")
        model, effort = ("gpt-6.1-sol", "medium") if name == "sol" else ("gpt-6-astra", "high")
        if not stage:
            attempt = 1
        elif stage.get("failed"):
            attempt = stage["attempt"] + 1
            if attempt > 2:
                raise RuntimeError(f"{name} recovery budget exhausted; retained for assistant")
            history = [_projected_stage(h, root) for h in read(root / f"{name}-attempts.json", [])]
            history.append(_projected_stage(stage, root))
            atomic(root / f"{name}-attempts.json", history)
            stage = {}
        else:
            attempt = stage["attempt"]
        if not stage:
            input_path = root / f"{name}-input-{attempt}.json"
            report_id = f"daily-retro-{self.settings.namespace}-{name}-{manifest['run_id']}-{attempt}"
            atomic(input_path, {"collection": worker_collection(manifest, root / "collection.json"),
                                "sol": prior, "report_id": report_id})
            key = f"daily-retro-{self.settings.namespace}-{manifest['run_id']}-{name}-{attempt}"
            stage = {"attempt": attempt, "created_at": now_iso(), "report_id": report_id,
                     "payload": {"host": self.settings.host, "provider": "codex", "model": model,
                                 "effort": effort, "visibility": "hidden", "role": "worker", "cwd": str(self.settings.memory_root),
                                 "self_close_on_completion": True,
                                 "request_id": key, "idempotency_key": key,
                                 "initial_prompt": worker_prompt(self.settings, manifest, name, input_path)}}
            if daily:
                stage["payload"]["spec_id"] = self.settings.usage_spec_id
            atomic(receipt_path, stage)  # Intent survives interruption before/after admission.
        if not stage.get("stream_id"):
            # Repeating the exact spawn key is the existing admission reconciliation contract.
            admitted = await self.rpc.spawn_once(self.config, dict(stage["payload"]))
            row = admitted.get("session") or {}
            stage["admission"] = admitted
            stage["stream_id"] = admitted.get("stream_id") or row.get("stream_id")
            stage["generation"] = row.get("session_generation") or admitted.get("session_generation")
            atomic(receipt_path, stage)
            if admitted.get("type") != "spawn.ok" or not stage["stream_id"] or not stage["generation"]:
                raise RuntimeError(f"{name} admission indeterminate; exact intent retained")
        if not stage.get("generation"):
            raise RuntimeError("owned generation unproven; cleanup and new admission blocked")
        deadline = monotonic() + 3600
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise RuntimeError("worker report deadline exceeded")
            try:
                response = await asyncio.wait_for(
                    self.rpc.await_report_once(self.config, stage["stream_id"], 0,
                                               include_details=True, include_extras=True,
                                               timeout=min(900, remaining)), timeout=remaining)
            except TimeoutError as exc:
                raise RuntimeError("worker report deadline exceeded") from exc
            if response.get("type") != "await_report.timeout":
                break
            if response.get("stream_id") != stage["stream_id"] or response.get("msg_id") != 0:
                checked(response, "await_report.ok")
        if response.get("result_kind") == "closed_without_report" or (response.get("result_kind") == "report" and not response.get("ok")):
            stage.update(failed=True, failure=_worker_failure_receipt(response, root, stage["report_id"]))
            atomic(receipt_path, stage)
            raise RuntimeError(f"{name} confirmed failed; recover on next run")
        checked(response, "await_report.ok")
        report = response["report"]
        if report.get("effective_model") != model or report.get("effective_effort") != effort:
            raise RuntimeError("worker report model/effort mismatch")
        if report.get("report_id") != stage["report_id"]:
            raise RuntimeError("unexpected worker report identity")
        validator = (validate_final_selection if name == "final-astra" else
                     validate_history_packet if manifest.get("cumulative_context") else validate_packet)
        try:
            packet = validator(report.get("extras", {}).get("daily_retro", {}), manifest)
        except Exception:
            stage.update(failed=True, failure="invalid terminal packet",
                         report=_worker_failure_receipt(response, root, stage["report_id"]))
            atomic(receipt_path, stage)
            raise
        if daily and name == "sol":
            packet.pop("astra_changes", None)
            packet.pop("acceptance_audit", None)
        packet = {**packet, "collection": worker_collection(manifest, root / "collection.json")}
        stage.update(packet=packet, packet_hash=digest(packet), report=report, completed_at=now_iso())
        atomic(receipt_path, stage)
        return stage

    def queue_failure_notice(self, manifest, *, seq, stage, failure):
        """Persist the failure notice before any RPC: the daemon may be down.
        A newer failure supersedes an older notice that never landed."""
        root = self.settings.state_root / "runs" / manifest["run_id"]
        path = root / "failure-delivery.json"
        record = read(path, {"attempts": []})
        prior = record.get("pending")
        if prior and prior.get("seq") != seq:
            annotate_failure(root, manifest["run_id"], seq=prior.get("seq"), notice=f"superseded by failure {seq}")
        record["pending"] = {"seq": seq, "stage": stage, "failure": failure, "at": now_iso()}
        atomic(path, record)

    def supersede_pending_notice(self, manifest, reason):
        root = self.settings.state_root / "runs" / manifest["run_id"]
        path = root / "failure-delivery.json"
        record = read(path, {"attempts": []})
        pending = record.get("pending")
        if pending:
            record["pending"] = None
            atomic(path, record)
            annotate_failure(root, manifest["run_id"], seq=pending.get("seq"), notice=f"superseded: {reason}")

    @staticmethod
    def _failure_body(manifest, stage, failure, root):
        return (f"REPORT daily-retro failure {manifest['run_id']} stage {stage}: {render_error(failure)}. "
                f"Retained state: {root}. The next producer pass resumes the retained stage without a new worker; "
                "no operator notification for routine retries.")

    def normalize_retained_failure_state(self, manifest):
        """Convert failure state retained from before structured records:
        error fields, the pending notice, failed stage receipts, and every
        failure-notice attempt body. An attempt keeps its request id, so a
        resend stays exactly-once (the daemon dedupes by key) and only ever
        carries the structured body; a landed attempt is never resent."""
        root = self.settings.state_root / "runs" / manifest["run_id"]
        path = root / "failure.json"
        state = read(path, None)
        if state:
            converted = {**_structured_failure_entry(state, root),
                         **({"history": [_structured_failure_entry(h, root) for h in state["history"]]}
                            if "history" in state else {})}
            if converted != state:
                atomic(path, converted)
        for name in STAGES:
            for receipt_path, many in ((root / f"{name}.json", False), (root / f"{name}-attempts.json", True)):
                value = read(receipt_path, None)
                if value:
                    converted = [_projected_stage(v, root) for v in value] if many else _projected_stage(value, root)
                    if converted != value:
                        atomic(receipt_path, converted)
        delivery_path = root / "failure-delivery.json"
        record = read(delivery_path, None)
        if not record:
            return
        original = json.loads(encoded(record))
        failures = {e.get("seq"): e for e in [*(state or {}).get("history", []), state or {}] if e.get("seq")}
        pending = record.get("pending")
        if pending:
            pending["failure"] = structured_error(pending.get("failure"), root)
            failures[pending.get("seq")] = {"stage": pending.get("stage"), "error": pending["failure"]}
        for attempt in record.get("attempts", []):
            if attempt.get("body_format") == FAILURE_BODY_FORMAT:
                continue
            known = failures.get(attempt.get("seq"), {})
            text = attempt.get("payload", {}).get("text", "")
            attempt["payload"]["text"] = self._failure_body(manifest, known.get("stage", "unknown"),
                                                            known.get("error", text), root)
            attempt["body_format"] = FAILURE_BODY_FORMAT
            if "receipt" in attempt:
                attempt["receipt"] = {k: attempt["receipt"].get(k) for k in ("type", "delivery", "state", "request_id")
                                      if isinstance(attempt["receipt"], dict)}
        if record != original:
            atomic(delivery_path, record)

    async def deliver(self, manifest, final=None, failure=None):
        root = self.settings.state_root / "runs" / manifest["run_id"]
        path = root / ("failure-delivery.json" if failure else "delivery.json")
        if failure:
            self.normalize_retained_failure_state(manifest)
        record = read(path, {"attempts": []})

        def project_notice_attempts():
            if failure and isinstance(record.get("notice_attempts"), list):
                projection_path = root / "delivery.json"
                projection = read(projection_path, {"attempts": []})
                if projection.get("notice_attempts") != record["notice_attempts"]:
                    projection["notice_attempts"] = record["notice_attempts"]
                    atomic(projection_path, projection)

        project_notice_attempts()  # Heal interruption after the authority write.

        if (root / "review.json").exists():
            return record
        pending = record.get("pending") if failure else None
        if failure and not pending:
            return record
        if failure:
            # Legacy notice state remains authoritative. A proven intent belongs
            # to one failure, even if the current assistant binding later moves.
            seq = pending.get("seq")
            matches = [a for a in record["attempts"] if a.get("seq") == seq]
            attempt = next((a for a in matches if a.get("confirmed")), matches[-1] if matches else None)
            if attempt and attempt.get("confirmed"):
                self._notice_landed(manifest, record, path, seq)
                return record
            budgets = record.setdefault("notice_retries", {})
            budget = budgets.get(str(seq))

            def notice_event(index, *, error=None, receipt=None, confirmed=False):
                event = {"seq": seq, "request_id": attempt.get("request_id") if attempt else None,
                         "target": attempt.get("target") if attempt else None,
                         "generation": attempt.get("generation") if attempt else None,
                         "at": now_iso(), "confirmed": confirmed, "retry_index": index}
                if error is not None:
                    event["error"] = structured_error(error, root)
                if receipt is not None:
                    event["receipt"] = {k: receipt[k] for k in ("type", "delivery", "state", "request_id") if k in receipt}
                record.setdefault("notice_attempts", []).append(event)
                atomic(path, record)
                project_notice_attempts()

            while True:
                index = 1 if budget else 0
                if budget and not budget.get("consumed"):
                    remaining = max(0.0, (aware(budget["not_before"]) - aware(now_iso())).total_seconds())
                    if remaining:
                        await asyncio.sleep(remaining)
                    # Persist consumption before any retry RPC, including binding.
                    budget["consumed"] = True
                    atomic(path, record)
                notice_rpc, persisting = False, False
                try:
                    if not attempt:
                        # Binding is the notice path's first RPC; a down daemon refuses here.
                        notice_rpc = True
                        binding = await self.binding()
                        notice_rpc = False
                        target, generation = binding["stream_id"], binding["session_generation"]
                        key = "daily-retro-" + digest([self.settings.namespace, manifest["run_id"], target, generation, True, seq])[:32]
                        host, session = target.split(":", 1)
                        attempt = {"target": target, "generation": generation, "request_id": key, "seq": seq,
                                   "body_format": FAILURE_BODY_FORMAT,
                                   "payload": {"host": host, "session_name": session, "request_id": key, "optimistic_id": key,
                                               "text": self._failure_body(manifest, pending.get("stage"), pending["failure"], root)}}
                        record["attempts"].append(attempt)
                        persisting = True
                        atomic(path, record)
                        persisting = False
                    notice_rpc = True
                    receipts = await self.rpc.send_receipt_once(self.config, attempt["target"], attempt["request_id"])
                    notice_rpc = False
                    if receipts.get("type") != "send.receipt.get.ok":
                        raise RuntimeError("delivery reconciliation unavailable")
                    landed = next((r for r in receipts.get("receipts", []) if r.get("delivery") == "landed" or r.get("state") == "landed"), None)
                    response = landed
                    if response is None:
                        notice_rpc = True
                        response = await self.rpc.send_once(self.config, dict(attempt["payload"]))
                        notice_rpc = False
                    attempt.update(receipt=response, confirmed=response.get("delivery") == "landed" or response.get("state") == "landed", at=now_iso())
                    attempt["collection_to_delivery_seconds"] = (aware(attempt["at"]) - aware(manifest["collected_at"])).total_seconds()
                    if not attempt["confirmed"]:
                        raise RuntimeError("delivery pending; exact target/body/key retained")
                    persisting = True
                    notice_event(index, receipt=response, confirmed=True)
                    self._notice_landed(manifest, record, path, seq)
                    return record
                except Exception as exc:
                    if persisting:
                        raise
                    refused = isinstance(exc, ConnectionRefusedError) or (isinstance(exc, OSError) and exc.errno == errno.ECONNREFUSED)
                    retry = refused and notice_rpc and budget is None
                    if retry:
                        budget = {"not_before": (aware(now_iso()) + timedelta(seconds=60)).isoformat(), "consumed": False}
                        budgets[str(seq)] = budget
                    notice_event(index, error=exc)
                    if not retry:
                        raise
        # Packet delivery keeps its generation-specific binding contract.
        binding = await self.binding()
        target, generation = binding["stream_id"], binding["session_generation"]
        seq = pending.get("seq") if pending else None
        attempt = next((a for a in reversed(record["attempts"]) if a["target"] == target and a["generation"] == generation
                        and a.get("seq") == seq), None)
        if attempt and attempt.get("confirmed"):
            if pending:
                self._notice_landed(manifest, record, path, seq)
            return record
        if not attempt:
            # One notice per failure: the key carries the failure seq (absent for
            # the ready REPORT, which keeps its historical key).
            key = "daily-retro-" + digest([self.settings.namespace, manifest["run_id"], target, generation, bool(failure),
                                           *([seq] if pending else [])])[:32]
            if failure:
                body = self._failure_body(manifest, pending.get("stage"), pending["failure"], root)
            else:
                summary = ""
                if manifest.get("consolidation"):
                    packet = final["packet"]
                    summary = (f" mode={manifest['consolidation']['mode']} counts={encoded(packet['counts']).decode()} "
                               f"relevant={encoded([{'id': c['id'], 'problem': c['problem'], 'action': c['action'], 'attention': c['bart_attention'], 'citations': c['citations'], 'prior_reviews': c.get('prior_reviews', [])} for c in packet['candidates']]).decode()}. "
                               "Previously reviewed versions retain their work/decision custody; do not recommission them. ")
                body = (f"REPORT daily-retro ready run={manifest['run_id']} report_id={final['report']['report_id']} "
                        f"packet_hash={final['packet_hash']} path={retained_final_path(root)}. "
                        f"{summary}"
                        f"Ingest once and review now under the accepted daily retro contract. Record every disposition with "
                        f"{Path(__file__).resolve()} record-review --config {self.settings.config_path} --run-id {manifest['run_id']} --result RESULT_JSON. "
                        "Use normal work proposal records and decision helper to recover still-open decisions after generation replacement. "
                        "Quiet ordinary days: retain review receipt. Publish material decisions and observed results in the same handling turn through supported visible delivery; hidden prose is never delivery. "
                        "If record-review returns a due weekly_summary, publish its compact accounting once, leading with gap_accounting.headline (new-this-week and actionable current, baseline as one separate labelled line), then denominators and coverage limits; never sum repeated daily source gaps. "
                        "Ask only for an actual missing grant through the existing versioned decision helper; routine authorized work needs no operator permission.")
            host, session = target.split(":", 1)
            attempt = {"target": target, "generation": generation, "request_id": key,
                       **({"seq": seq, "body_format": FAILURE_BODY_FORMAT} if pending else {}),
                       "payload": {"host": host, "session_name": session, "text": body,
                                   "request_id": key, "optimistic_id": key}}
            record["attempts"].append(attempt)
            atomic(path, record)
        if attempt.get("confirmed") is False:
            # Retained negative evidence survives later success; newest exact
            # intent remains the reconciliation authority for this generation.
            attempt = {k: v for k, v in attempt.items() if k not in {"confirmed", "receipt", "at", "collection_to_delivery_seconds"}}
            record["attempts"].append(attempt)
            atomic(path, record)
        receipts = await self.rpc.send_receipt_once(self.config, target, attempt["request_id"])
        if receipts.get("type") != "send.receipt.get.ok":
            raise RuntimeError("delivery reconciliation unavailable")
        landed = next((r for r in receipts.get("receipts", []) if r.get("delivery") == "landed" or r.get("state") == "landed"), None)
        response = landed or await self.rpc.send_once(self.config, dict(attempt["payload"]))
        attempt["receipt"] = response
        attempt["confirmed"] = response.get("delivery") == "landed" or response.get("state") == "landed"
        attempt["at"] = now_iso()
        attempt["collection_to_delivery_seconds"] = (datetime.fromisoformat(attempt["at"]) - datetime.fromisoformat(manifest["collected_at"])).total_seconds()
        atomic(path, record)
        if pending and attempt["confirmed"]:
            self._notice_landed(manifest, record, path, seq)
        if not attempt["confirmed"]:
            raise RuntimeError("delivery pending; exact target/body/key retained")
        return record

    def _notice_landed(self, manifest, record, path, seq):
        record["pending"] = None
        atomic(path, record)
        annotate_failure(self.settings.state_root / "runs" / manifest["run_id"], manifest["run_id"],
                         seq=seq, notice="delivered")

    async def cleanup(self, manifest, force=False):
        root = self.settings.state_root / "runs" / manifest["run_id"]
        for name in STAGES:
            path = root / f"{name}.json"
            stage = read(path, {})
            if stage.get("closed") or not stage.get("generation") or not (force or stage.get("packet") or stage.get("failed")):
                continue
            result = await self.rpc.close_once(self.config, stage["stream_id"],
                                               expected_generation=stage["generation"], reason="report_terminate")
            stage["cleanup"] = result
            row = result.get("session") or {}
            stage["closed"] = (result.get("type") in {"close.ok", "close.already_closed"}
                               and not result.get("failed") and row.get("status") == "closed"
                               and row.get("session_generation") == stage["generation"])
            atomic(path, stage)
            if not stage["closed"]:
                raise RuntimeError("generation-owned worker cleanup blocked")

    async def run(self, stamp=None, on_demand=False):
        stamp = stamp or datetime.now(timezone.utc)
        if not on_demand and not timer_due(stamp):
            return {"state": "before_05_local"}
        with locked(self.settings.state_root / "run.lock"):
            collect(self.settings, stamp)
            completed = []
            for path in sorted((self.settings.state_root / "runs").glob("*/collection.json")):
                manifest = read(path)
                if await self.run_manifest(manifest):
                    completed.append(manifest["run_id"])
            return {"delivered": completed}

    async def run_manifest(self, manifest):
        root = self.settings.state_root / "runs" / manifest["run_id"]
        self.normalize_retained_failure_state(manifest)
        if (root / "review.json").exists():
            self.supersede_pending_notice(manifest, "review recorded")
            await self.cleanup(manifest)
            return False
        if read(root / "failure-delivery.json", {}).get("pending"):
            try:  # A notice that could not reach the daemon stays durable until it lands.
                await self.deliver(manifest, failure=True)
            except Exception as notice_error:
                annotate_failure(root, manifest["run_id"], notice_error=structured_error(notice_error, root))
        primary, stage = None, "sol"
        try:
            legacy = read(root / "astra.json", {})
            if legacy.get("packet"):
                final = legacy
            else:
                sol = await self.worker(manifest, "sol")
                final = sol
                if not daily_manifest(manifest) or legacy.get("stream_id"):
                    stage = "astra"
                    final = await self.worker(manifest, "astra", sol["packet"])
            stage = "delivery"
            await self.deliver(manifest, final)
            return True
        except BaseException as exc:
            primary = exc
            error = structured_error(exc, root)
            seq = record_failure(root, manifest["run_id"], stage=stage, error=error, notice="pending")
            self.queue_failure_notice(manifest, seq=seq, stage=stage, failure=error)
            if isinstance(exc, Exception):
                try:
                    await self.deliver(manifest, failure=True)
                except Exception as delivery_error:
                    annotate_failure(root, manifest["run_id"], notice_error=structured_error(delivery_error, root))
            raise
        finally:
            try:
                await self.cleanup(manifest)
            except Exception as cleanup_error:
                # Cleanup is retried by the next pass; it never replaces the primary error.
                if primary is None:
                    raise
                annotate_failure(root, manifest["run_id"], cleanup_error=structured_error(cleanup_error, root))

    async def history_run(self, baseline_path, batch, no_deliver=False):
        history_baseline(self.settings, baseline_path)
        if no_deliver and (self.settings.state_root / "history.json").exists():
            completed_history(self.settings, baseline_path)
        with locked(self.settings.state_root / "run.lock"):
            manifest = history_collect(self.settings, baseline_path, batch)
            root = self.settings.state_root / "runs" / manifest["run_id"]
            mode = read(root / "history-mode.json")
            if mode and not no_deliver:
                raise ValueError("history delivery mode conflict")
            if no_deliver:
                inventory, completed = completed_history(self.settings, baseline_path)
                if batch in completed:
                    return {"prepared": [] if completed[batch]["review"] else [manifest["run_id"]], "delivered": []}
                if any(n not in completed for n in range(1, batch)):
                    raise ValueError("serial history requires preceding batches completed")
                if any(read(root / f"{stage}.json") for stage in STAGES) and mode is None:
                    raise ValueError("history delivery mode conflict")
                if mode is None:
                    context = history_context(self.settings, inventory, completed)
                    context_path = root / "history-context.json"
                    atomic(context_path, context)
                    mode = {"no_deliver": True, "context_sha256": hashlib.sha256(context_path.read_bytes()).hexdigest(),
                            "context_path": str(context_path)}
                    atomic(root / "history-mode.json", mode)
                context_path = root / "history-context.json"
                if (mode.get("no_deliver") is not True or mode.get("context_path") != str(context_path)
                        or not context_path.exists() or hashlib.sha256(context_path.read_bytes()).hexdigest() != mode.get("context_sha256")):
                    raise ValueError("edited cumulative context")
                for prior in read(context_path)["inputs"]:
                    packet = read(Path(prior["packet_path"]))
                    if not packet or packet.get("packet_hash") != prior["packet_hash"] or digest(packet["packet"]) != prior["packet_hash"]:
                        raise ValueError("edited cumulative packet provenance")
                manifest = {**manifest, "cumulative_context": {"path": str(context_path), "sha256": mode["context_sha256"]}}
                if any(read(root / f"{name}.json", {}).get("failed") for name in STAGES):
                    raise RuntimeError("historical worker recovery requires parent ruling")
                try:
                    sol = await self.worker(manifest, "sol")
                    await self.worker(manifest, "astra", sol["packet"])
                except Exception as exc:
                    stage = next((n for n in ("sol", "astra") if not read(root / f"{n}.json", {}).get("packet")), "astra")
                    record_failure(root, manifest["run_id"], stage=stage, error=structured_error(exc, root),
                                   notice="not required: no-deliver history batch")
                    raise
                finally:
                    await self.cleanup(manifest, force=True)
                completed_history(self.settings, baseline_path)
                return {"prepared": [manifest["run_id"]], "delivered": []}
            delivered = await self.run_manifest(manifest)
            return {"delivered": [manifest["run_id"]] if delivered else []}

    async def history_consolidate(self, baseline_path, batches=None, final=False):
        inventory, completed = completed_history(self.settings, baseline_path)
        if final:
            if batches is not None or set(completed) != set(range(1, len(inventory["batches"]) + 1)):
                raise ValueError("final consolidation requires complete history inventory")
            numbers = sorted(completed)
        else:
            if (not isinstance(batches, list) or not 1 <= len(batches) <= 5
                    or any(type(n) is not int for n in batches) or len(set(batches)) != len(batches)):
                raise ValueError("one to five unique batch numbers required")
            numbers = sorted(batches)
            if any(n not in completed or completed[n]["mode"] is None or completed[n]["review"] for n in numbers):
                raise ValueError("checkpoint requires completed no-deliver batches")
        chosen = {n: completed[n] for n in numbers}
        members = [{"run_id": r["manifest"]["run_id"], "packet_hash": r["final"]["packet_hash"]} for _, r in sorted(chosen.items())]
        mode = "final" if final else "checkpoint"
        run_id = "history-consolidated-" + digest([self.settings.namespace, inventory["inventory_digest"], mode, members])
        root = self.settings.state_root / "runs" / run_id
        with locked(self.settings.state_root / "run.lock"):
            existing = read(root / "collection.json")
            reported = set()
            for prior_root, prior_manifest in history_checkpoints(self.settings):
                if prior_manifest["run_id"] == run_id:
                    continue
                if not final and set(numbers) & set(prior_manifest["consolidation"]["batches"]):
                    raise ValueError("overlapping history checkpoint")
                stage = read(prior_root / "astra.json")
                if not stage or stage.get("packet_hash") != digest(stage["packet"]):
                    raise ValueError("incomplete or edited prior checkpoint")
                delivery = read(prior_root / "delivery.json", {"attempts": []})
                if stage["packet"]["candidates"] and not any(a.get("confirmed") for a in delivery["attempts"]):
                    raise RuntimeError("prior checkpoint delivery unresolved")
                reported.update(c["finding_version"] for c in stage["packet"]["candidates"])
            if existing:
                manifest = existing
                expected_catalogue = history_catalogue(chosen)
                catalogue = manifest.get("consolidation", {}).get("catalogue", [])
                if (manifest.get("run_id") != run_id or manifest.get("phase") != "history-consolidated"
                        or manifest["consolidation"].get("members") != members
                        or manifest["consolidation"].get("mode") != mode
                        or manifest["consolidation"].get("batches") != numbers
                        or manifest.get("sources") != [s for _, r in sorted(chosen.items()) for s in r["manifest"]["sources"]]
                        or [{k: v for k, v in row.items() if k != "prior_review"} for row in catalogue] !=
                           [{k: v for k, v in row.items() if k != "prior_review"} for row in expected_catalogue]):
                    raise ValueError("edited consolidation membership")
            else:
                context = history_context(self.settings, inventory, completed)
                manifest = {"run_id": run_id, "phase": "history-consolidated", "baseline": {},
                            "collected_at": inventory["collected_at"],
                            "sources": [s for _, r in sorted(chosen.items()) for s in r["manifest"]["sources"]],
                            "consolidation": {"mode": mode, "batches": numbers, "members": members,
                                "catalogue": history_catalogue(chosen), "already_reported": sorted(reported),
                                "context": context, "context_digest": digest(context)}}
                atomic(root / "collection.json", manifest)
            if digest(manifest["consolidation"]["context"]) != manifest["consolidation"]["context_digest"]:
                raise ValueError("edited consolidation context")
            compiled = read(root / "astra.json")
            if compiled:
                if compiled.get("packet_hash") != digest(compiled["packet"]):
                    raise ValueError("edited compiled history packet")
                validate_packet(compiled["packet"], manifest)
                selection = None
                if final:
                    retained = read(root / "final-astra.json", {})
                    report = retained.get("report", {})
                    if (report.get("report_id") != retained.get("report_id")
                            or report.get("effective_model") != "gpt-6-astra" or report.get("effective_effort") != "high"
                            or retained.get("packet_hash") != digest(retained.get("packet"))):
                        raise ValueError("final report/model/hash proof invalid")
                    selection = validate_final_selection(report.get("extras", {}).get("daily_retro", {}), manifest)
                    selection["collection"] = worker_collection(manifest, root / "collection.json")
                    if selection != retained["packet"] or compiled["report"] != report:
                        raise ValueError("compiled final differs from retained worker report")
                if compiled["packet"] != compile_history_packet(manifest, chosen, selection):
                    raise ValueError("compiled packet differs from immutable inputs")
            else:
                selection = None
                try:
                    if final:
                        if read(root / "final-astra.json", {}).get("failed"):
                            raise RuntimeError("final worker recovery requires parent ruling")
                        selection = await self.worker(manifest, "final-astra")
                    packet = compile_history_packet(manifest, chosen, selection["packet"] if selection else None)
                    compiled = {"packet": packet, "packet_hash": digest(packet), "producer": "history-consolidation",
                                "report": selection["report"] if selection else {
                                    "report_id": "history-checkpoint-" + digest(members), "kind": "deterministic-compilation",
                                    "source_report_ids": [r["final"]["report_id"] for _, r in sorted(chosen.items())]}}
                    atomic(root / "astra.json", compiled)
                finally:
                    await self.cleanup(manifest, force=True)
            # A compiled receipt may survive interruption before owned cleanup.
            await self.cleanup(manifest, force=True)
            if final or compiled["packet"]["candidates"]:
                await self.deliver(manifest, compiled)
            return {"run_id": run_id, "packet_hash": compiled["packet_hash"], "counts": compiled["packet"]["counts"],
                    "quiet": not final and not compiled["packet"]["candidates"]}

    async def actor(self):
        binding = await self.binding()
        actor = os.environ.get("PENTACLE_STREAM_ID") or os.environ.get("AGENT_ORCH_STREAM_ID")
        if actor != binding["stream_id"]:
            raise RuntimeError("only CURRENT assistant may ingest or prepare decisions")
        inspected = checked(await self.rpc.inspect_stream_once(self.config, actor), "inspect_stream.ok")
        row = inspected.get("session") or {}
        if row.get("status") != "open" or row.get("session_generation") != binding["session_generation"]:
            raise RuntimeError("open current assistant generation required")
        return binding

    async def record_review(self, run_id, result):
        binding = await self.actor()
        if not re.fullmatch(r"(?:\d{4}-\d{2}-\d{2}|history-[0-9a-f]{16}-\d{4}|history-consolidated-[0-9a-f]{64})", run_id):
            raise ValueError("invalid run ID")
        root = self.settings.state_root / "runs" / run_id
        with locked(root / "review.lock"):
            final = retained_final(root)
            if not final or result.get("packet_hash") != final.get("packet_hash"):
                raise ValueError("review must bind exact final packet hash")
            rows = result.get("dispositions", [])
            candidate_map = {c["id"]: c for c in final["packet"]["candidates"]}
            candidates = set(candidate_map)
            if len(rows) != len(candidates) or {r.get("id") for r in rows} != candidates:
                raise ValueError("one assistant disposition per recommendation required")
            proposal_bindings = {}
            for row in rows:
                if row.get("disposition") not in DISPOSITIONS or not row.get("reason"):
                    raise ValueError("explicit assistant disposition/reason required")
                schema2 = final["packet"].get("schema_version") == 2
                kind = candidate_map[row["id"]].get("recommendation_kind")
                if schema2:
                    if kind not in RECOMMENDATIONS:
                        raise ValueError("schema2 candidate requires recommendation_kind")
                    if row["disposition"] == "no_change" and kind != "no_change":
                        raise ValueError("future/actionable work cannot be suppressed as no_change")
                    if row["disposition"] == "resolved":
                        validate_outcome(row.get("outcome_evidence"))
                    if row["disposition"] == "duplicate":
                        evidence = row.get("existing_work_evidence")
                        if not isinstance(evidence, dict) or not evidence.get("owner") or not evidence.get("acceptance_receipt"):
                            raise ValueError("duplicate requires accepted existing-work evidence")
                        work_path(self.settings, evidence.get("work_id"))
                if row["disposition"] in {"investigate", "authorized", "propose", "defer"}:
                    path = work_path(self.settings, row.get("work_id"))
                    records = proposals(path.read_text())
                    proposal = records.get(row.get("proposal_id"))
                    candidate = candidate_map[row["id"]]
                    projected = (not run_id.startswith("history-") and "candidate_key" in candidate
                                 and row.get("version") == candidate.get("version")
                                 and "proposal_version" in row)
                    bound_version = row.get("proposal_version") if projected else row.get("version")
                    if not proposal or proposal.get("version") != bound_version or not proposal.get("citations"):
                        raise ValueError("action needs durable proposal/version/citations in normal work")
                    proposal_bindings[row["id"]] = bound_version
                    if schema2:
                        validate_proposal(proposal, require_v2=True)
                        if proposal_version(proposal) != proposal["version"] or row["disposition"] != proposal.get("disposition"):
                            raise ValueError("disposition must match exact current schema2 proposal")
            # Keep the existing proposal-binding input, but expose the exact
            # normalized candidate's content identity on new daily review rows.
            result = json.loads(json.dumps(result))
            for row in result["dispositions"]:
                candidate = candidate_map[row["id"]]
                if not run_id.startswith("history-") and "candidate_key" in candidate:
                    if row["id"] in proposal_bindings:
                        row["proposal_version"] = proposal_bindings[row["id"]]
                    else:
                        row.pop("proposal_version", None)
                    row.update(candidate_key=candidate["candidate_key"], version=candidate["version"])
                    row.pop("candidate_version", None)
            old = read(root / "review.json")
            if old:
                if old["result"] != result:
                    raise ValueError("run already reviewed; amend normal work rather than repeat ingestion")
                if final["packet"].get("schema_version") == 2 and "weekly_summary" not in old:
                    old = {**old, "weekly_summary": retain_weekly_summary(self.settings, run_id)}
                    atomic(root / "review.json", old)
                return old
            receipt = {"result": result, "actor": binding, "reviewed_at": now_iso(), "decision_ready_at": now_iso()}
            manifest = read(root / "collection.json")
            receipt["collection_to_decision_ready_seconds"] = (datetime.fromisoformat(receipt["decision_ready_at"]) - datetime.fromisoformat(manifest["collected_at"])).total_seconds()
            atomic(root / "review.json", receipt)
            if final["packet"].get("schema_version") == 2:
                receipt["weekly_summary"] = retain_weekly_summary(self.settings, run_id)
                atomic(root / "review.json", receipt)
            return receipt

    async def _blocked_report(self, path, preimage, records, record, attempt):
        notice = attempt.get("blocked_report")
        if notice is None:
            target = attempt["producer"]
            host, session = target.split(":", 1)
            key = "retro-blocked-" + digest([self.settings.namespace, str(path), attempt["question_id"]])[:32]
            notice = {"target": target, "request_id": key, "payload": {
                "host": host, "session_name": session, "request_id": key, "optimistic_id": key,
                "text": f"REPORT daily-retro decision blocked proposal={record['id']} version={record['version']} state=ask_blocked reason=ask_parent path={path}. No authorization or retry loop; after the current-assistant prompt admission is live, resume explicitly with decision --retry-blocked."}}
            attempt["blocked_report"] = notice
        records[record["id"]] = record
        preimage = save_proposals(path, preimage, records)
        if notice.get("confirmed"):
            return record
        receipts = checked(await self.rpc.send_receipt_once(self.config, notice["target"], notice["request_id"]), "send.receipt.get.ok")
        landed = next((r for r in receipts.get("receipts", []) if r.get("delivery") == "landed" or r.get("state") == "landed"), None)
        response = landed or await self.rpc.send_once(self.config, dict(notice["payload"]))
        notice.update(receipt=response, confirmed=response.get("delivery") == "landed" or response.get("state") == "landed")
        save_proposals(path, preimage, records)
        if not notice["confirmed"]:
            raise RuntimeError("blocked decision REPORT pending; exact notice retained")
        return record

    async def decision(self, work_id, proposal, *, retry_blocked=False):
        binding = await self.actor()
        # Validate before using the ID as a lock name; authorization resolves
        # again under that lock so status moves cannot bypass the guard.
        if not valid_work_id(work_id):
            raise ValueError("normal work ID required")
        path = None if proposal.get("disposition") == "authorized" else work_path(self.settings, work_id)
        identity = proposal.get("id")
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", identity):
            raise ValueError("stable proposal ID required")
        for field in ("scope", "citations", "owner", "checkpoint", "success_measure"):
            if not proposal.get(field):
                raise ValueError(f"proposal requires {field}")
        disposition = proposal.get("disposition", "propose")
        if disposition not in DISPOSITIONS:
            raise ValueError("unknown proposal disposition")
        if disposition == "propose":
            for field in ("title", "body", "options"):
                if not proposal.get(field):
                    raise ValueError(f"operator decision requires {field}")
        if disposition in {"authorized", "investigate"} and not proposal.get("authority"):
            raise ValueError("existing authority reference required")
        version = proposal_version(proposal)
        with locked(self.settings.state_root / "locks" / f"{work_id}.lock"):
            baseline = None
            if disposition == "authorized":
                if work_id in self.settings.self_assignment_exclusions:
                    raise ValueError("refused: use resolved or duplicate")
                path = work_path(self.settings, work_id, authorization=True)
                baseline = path.resolve().parent.parent.name
                if baseline in {"completed", "deprecated"}:
                    raise ValueError("refused: use resolved or duplicate")
            preimage = path.read_bytes()
            text = preimage.decode()
            records = proposals(text)
            record = records.get(identity, {"attempts": []})
            validate_proposal(proposal, require_v2=record.get("schema_version") == 2 or
                              (disposition == "defer" and identity not in records))
            old_version = record.get("version")
            old_disposition = record.get("disposition", "propose")
            if disposition == "authorized":
                receipt_path = self.settings.state_root / "decision-receipts" / (
                    digest([work_id, identity, version, "authorized"]) + ".json")
                previous = read(receipt_path)
                # One authorization/version has one immutable observation,
                # including a legacy absence, even after another disposition.
                if previous is not None:
                    baseline = previous.get("baseline_status")
                elif old_version == version and (old_disposition == "authorized" or record.get("state") == "authorized"):
                    baseline = record.get("baseline_status")
            record.pop("baseline_status", None)
            if disposition == "authorized" and baseline is not None:
                record["baseline_status"] = baseline
            attempts = record["attempts"]
            # Query all historical attempts, including terminal ones: an answer
            # may have committed before its former generation's final turn.
            live, answers = [], []
            for attempt in attempts:
                status = await self.rpc.prompt_status_once(self.config, attempt["question_id"])
                if status.get("type") == "prompt.error" and status.get("error_code") == "question_not_found" and attempt.get("state") in {"intent", "blocked"}:
                    attempt["observed_state"] = "absent"
                    continue
                checked(status, "prompt.status.ok")
                question = status["question"]
                attempt["observed_state"] = question["state"]
                if question["state"] == "answered":
                    attempt["answer"] = question["answer"]
                    if attempt["version"] == version:
                        answers.append(attempt)
                    else:
                        attempt["stale_answer_refused"] = True
                elif question["state"] == "open":
                    live.append(attempt)
                elif question["state"] not in {"expired", "cancelled", "canceled"}:
                    raise RuntimeError("unknown question state; no new ask")
            if len(live) > 1:
                raise RuntimeError("multiple live questions; escalate trace before any new ask")
            if live and live[0]["version"] != version:
                reply = await self.rpc.prompt_cancel_once(self.config, {"type": "prompt.cancel", "question_id": live[0]["question_id"], "reason": "proposal_scope_changed"})
                checked(reply, "prompt.cancel.ok")
                status = checked(await self.rpc.prompt_status_once(self.config, live[0]["question_id"]), "prompt.status.ok")
                if status["question"]["state"] == "open":
                    raise RuntimeError("old-version live question cannot be retired")
                live = []
            if old_version != version and "outcome_evidence" not in proposal:
                record.pop("outcome_evidence", None)
            record.update({k: proposal[k] for k in proposal if k not in {"attempts", "answer", "version", "state", "baseline_status"}})
            record["version"] = version
            if answers:
                if len({digest(a["answer"]) for a in answers}) != 1:
                    raise RuntimeError("conflicting current-version answers")
                record.update(state="answered", answer=answers[-1]["answer"], answer_question_id=answers[-1]["question_id"])
            elif live:
                record.update(state="pending")
                record.pop("answer", None)
            elif disposition == "propose" and old_version == version and record.get("state") == "ask_blocked" and not retry_blocked and any(
                    a.get("state") == "blocked" and a["version"] == version for a in attempts):
                attempt = next(a for a in reversed(attempts) if a.get("state") == "blocked" and a["version"] == version)
                return await self._blocked_report(path, preimage, records, record, attempt)
            elif (old_version == version and record.get("state") in {"rejected", "deferred", "authorized", "shipped"}
                  and not (record.get("state") == "authorized" and old_disposition != disposition)):
                pass
            elif proposal.get("disposition") in {"resolved", "duplicate", "no_change", "authorized", "investigate", "defer"}:
                record["state"] = proposal["disposition"]
                record.pop("answer", None)
            else:
                question_id = "retro-q-" + digest([identity, version, binding["session_generation"]])[:48]
                attempt = next((a for a in attempts if a["question_id"] == question_id), None)
                if attempt is None:
                    attempt = {"question_id": question_id, "version": version, "producer": binding["stream_id"],
                               "generation": binding["session_generation"], "state": "intent"}
                    attempts.append(attempt)
                envelope = prompt_protocol.build_envelope(
                    title=proposal["title"], body=proposal["body"], response_mode="single_choice",
                    raw_options=[prompt_protocol.PromptOption(**option) for option in proposal["options"]],
                    question_id=question_id, dedup_key=question_id, producer_stream_id=binding["stream_id"], spec_id=work_id)
                record.update(state="pending")
                record.pop("answer", None)
                records[identity] = record
                preimage = save_proposals(path, preimage, records)  # Durable intent BEFORE ask.
                reply = await self.rpc.prompt_ask_once(self.config, {"type": "prompt.ask", "envelope": envelope,
                                                       "actions": prompt_protocol.notification_actions(envelope),
                                                       "request_id": question_id})
                if reply.get("type") == "prompt.error" and reply.get("error_code") == "ask_parent":
                    attempt.update(state="blocked", refusal=reply, blocked_at=now_iso())
                    record.update(state="ask_blocked")
                    return await self._blocked_report(path, preimage, records, record, attempt)
                checked(reply, "prompt.ask.ok")
                attempt.update(state="asked", receipt=reply, asked_at=now_iso())
            records[identity] = record
            save_proposals(path, preimage, records)
            receipt_path = self.settings.state_root / "decision-receipts" / (digest([work_id, identity, version, record["state"]]) + ".json")
            if not receipt_path.exists():
                atomic(receipt_path, {"work_id": work_id, "proposal_id": identity, "version": version,
                                     "state": record["state"], "recorded_at": now_iso(), "path": str(path),
                                     **({"baseline_status": record["baseline_status"]} if "baseline_status" in record else {})})
            return record


def valid_work_id(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_:-]+", value) is not None


def work_path(settings, work_id, *, authorization=False):
    if not valid_work_id(work_id):
        raise ValueError("normal work ID required")
    found = []
    for path in (settings.memory_root / "work").glob("*/*/spec.md"):
        try:
            if parse_frontmatter(path).get("id") == work_id:
                resolved = path.resolve()
                if settings.memory_root not in resolved.parents:
                    raise ValueError("work file escapes memory root")
                found.append(path)
        except (OSError, UnicodeError, RuntimeError):
            continue
    if not found and authorization:
        raise ValueError("unknown_work_id")
    if len(found) != 1:
        raise ValueError("normal work ID must resolve exactly once; create with existing triage first")
    return found[0]


def proposals(text):
    if PROPOSAL_START not in text:
        return {}
    block = text.split(PROPOSAL_START, 1)[1].split(PROPOSAL_END, 1)[0].strip()
    return json.loads(block.removeprefix("```json\n").removesuffix("\n```"))


def save_proposals(path, preimage, records):
    if path.read_bytes() != preimage:
        raise RuntimeError("work preimage moved; no mutation")
    text = preimage.decode()
    block = PROPOSAL_START + "\n```json\n" + encoded(records).decode() + "\n```\n" + PROPOSAL_END
    if PROPOSAL_START in text:
        start = text.index(PROPOSAL_START)
        end = text.index(PROPOSAL_END, start) + len(PROPOSAL_END)
        text = text[:start] + block + text[end:]
    else:
        text += "\n\n" + block + "\n"
    atomic(path, text.encode())
    return text.encode()


class ProducerTransport:
    """Existing operator RPCs restricted to owned workers and frozen REPORTs."""
    ALLOWED = frozenset({"spawn", "await_spawn", "await_report", "close",
                         "assistant.binding", "send.receipt.get", "send"})
    def __init__(self, settings):
        self.settings = settings

    def stages(self):
        for name in STAGES:
            for path in (self.settings.state_root / "runs").glob(f"*/{name}.json"):
                yield read(path)

    def owned(self, stream, generation=None):
        return any(stage.get("stream_id") == stream and stage.get("generation")
                   and (generation is None or stage["generation"] == generation) for stage in self.stages())

    async def call(self, payload, timeout=30):
        verb = payload["type"]
        if verb not in self.ALLOWED:
            raise ValueError("unattended operator verb outside explicit allowlist")
        body = {k: v for k, v in payload.items() if k != "type"}
        if verb == "spawn" and not any(stage.get("payload") == body for stage in self.stages()):
            raise ValueError("spawn outside retained intent")
        if verb == "await_spawn" and not any(stage.get("payload", {}).get("request_id") == body.get("spawn_request_id") for stage in self.stages()):
            raise ValueError("await outside retained admission")
        if verb == "await_report" and (body.get("msg_id") != 0 or not self.owned(body.get("stream_id"))):
            raise ValueError("await outside owned worker")
        if verb == "close" and (not body.get("expected_generation") or not self.owned(
                f"{body.get('host')}:{body.get('session_name')}", body["expected_generation"])):
            raise ValueError("close outside generation ownership")
        if verb == "assistant.binding" and body:
            raise ValueError("binding read has no mutable fields")
        if verb == "send" and (not body.get("text", "").startswith("REPORT daily-retro ") or not any(a["payload"] == body for a in self.deliveries())):
            raise ValueError("send outside retained REPORT")
        if verb == "send.receipt.get" and not any(a["target"] == body.get("to_stream_id") and a["request_id"] == body.get("request_id") for a in self.deliveries()):
            raise ValueError("receipt outside retained REPORT")
        def invoke():
            with authenticated_operator_connection(self.settings.ws_url, self.settings.token_path, timeout + 2) as connection:
                return connection.rpc(payload)
        if not wsclient._is_rpc_retry_eligible(payload):
            return await asyncio.to_thread(invoke)
        # Same reconnect contract as the agent-orch client: a daemon restart is
        # survived inside the verb's own retry deadline (await/spawn re-sends are
        # ledger-resolved or idempotency-keyed), then the transport error stands.
        policy = wsclient._rpc_retry_policy_from_env(timeout)
        deadline = wsclient._retry_deadline(monotonic(), policy)
        attempt = 0
        while True:
            attempt += 1
            try:
                return await asyncio.to_thread(invoke)
            except TRANSPORT_ERRORS:
                delay, _reason = wsclient._transport_retry_next_delay(policy, deadline, attempt)
                if delay is None:
                    raise
                await asyncio.sleep(delay)

    async def spawn_once(self, config, payload):
        if not any(stage.get("payload") == payload for stage in self.stages()):
            raise ValueError("spawn must match retained run intent")
        if payload.get("provider") != "codex" or not payload.get("self_close_on_completion") or payload.get("parent_stream_id"):
            raise ValueError("only transient self-closing top-level Codex workers")
        response = await self.call({**payload, "type": "spawn"}, 180)
        if response.get("type") == "spawn.ok" and response.get("state") == "starting":
            response = await self.call({"type": "await_spawn", "spawn_request_id": payload["request_id"],
                                        "request_id": payload["request_id"] + "-await"}, 180)
            if response.get("type") == "await_spawn.ok":
                response = {**response, "type": "spawn.ok"}
        return response

    async def await_report_once(self, config, stream, msg_id, **kwargs):
        if not self.owned(stream) or msg_id != 0:
            raise ValueError("await must target an owned generation's terminal report")
        return await self.call({"type": "await_report", "stream_id": stream, "msg_id": 0,
                                "include_details": True, "include_extras": True,
                                "timeout": kwargs.get("timeout", 30)}, kwargs.get("timeout", 30))

    async def close_once(self, config, stream, **kwargs):
        generation = kwargs.get("expected_generation")
        if not generation or not self.owned(stream, generation):
            raise ValueError("cleanup requires exact retained ownership")
        host, name = stream.split(":", 1)
        return await self.call({"type": "close", "host": host, "session_name": name,
                                "expected_generation": generation, "reason": "report_terminate"})

    def deliveries(self):
        for name in ("delivery.json", "failure-delivery.json"):
            for path in (self.settings.state_root / "runs").glob(f"*/{name}"):
                yield from read(path)["attempts"]

    async def assistant_once(self, config, payload):
        if payload != {"type": "assistant.binding"}:
            raise ValueError("producer may only read current binding")
        return await self.call(payload)

    async def send_once(self, config, payload):
        if not payload.get("text", "").startswith("REPORT daily-retro ") or not any(a["payload"] == payload for a in self.deliveries()):
            raise ValueError("send must exactly match retained REPORT target/body/key")
        return await self.call({**payload, "type": "send"})

    async def send_receipt_once(self, config, target, key):
        if not any(a["target"] == target and a["request_id"] == key for a in self.deliveries()):
            raise ValueError("receipt must match retained REPORT attempt")
        return await self.call({"type": "send.receipt.get", "to_stream_id": target, "request_id": key})


async def rehearse(settings, workers, evidence_dir):
    """Real worker reports, restricted to a running isolated assistant counterpart."""
    if not settings.isolated or settings.memory_root == workers.memory_root:
        raise ValueError("rehearsal requires separate fixture memory and isolated current-assistant endpoint")
    worker_settings = replace(workers, memory_root=settings.memory_root, state_root=settings.state_root)
    owned = ProducerTransport(worker_settings)
    delivery = ProducerTransport(settings)

    class RehearsalTransport:
        spawn_once = owned.spawn_once
        await_report_once = owned.await_report_once
        close_once = owned.close_once
        assistant_once = delivery.assistant_once
        send_once = delivery.send_once
        send_receipt_once = delivery.send_receipt_once

    pipeline = Pipeline(replace(settings, host=workers.host, usage_spec_id=workers.usage_spec_id), RehearsalTransport())
    with locked(settings.state_root / "run.lock"):
        manifest = collect(settings, datetime.now(timezone.utc))
        required = {"spec_fixture_repeat_a", "spec_fixture_repeat_b", "spec_fixture_serious",
                    "spec_fixture_fixed", "spec_fixture_owned", "spec_fixture_uncertain"}
        if {s["id"] for s in manifest["sources"]} != required:
            raise ValueError("rehearsal must use the six pinned analytical fixtures")
        try:
            sol = await pipeline.worker(manifest, "sol")
            legacy = read(settings.state_root / "runs" / manifest["run_id"] / "astra.json", {})
            final = legacy if legacy.get("packet") else sol
            if legacy.get("stream_id") and not legacy.get("packet"):
                final = await pipeline.worker(manifest, "astra", sol["packet"])
            candidates = final["packet"]["candidates"]
            serious = [c for c in candidates if "spec_fixture_serious" in c["citations"]]
            repeated = [c for c in candidates if {"spec_fixture_repeat_a", "spec_fixture_repeat_b"} <= set(c["citations"])]
            uncertain = [c for c in candidates if "spec_fixture_uncertain" in c["citations"] and c["uncertainty"]]
            if not serious or not repeated or not uncertain or any("[fixture gap:" in c["consequence"] for c in repeated):
                raise RuntimeError("Sol analytical fixture acceptance failed; retain exact report for QA")
            await pipeline.deliver(manifest, final)
            receipt = {"run_id": manifest["run_id"], "sol_report_id": sol["report"]["report_id"],
                       "tool_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                       "isolated_url": settings.ws_url, "workers_url": workers.ws_url,
                       "review": read(settings.state_root / "runs" / manifest["run_id"] / "review.json"),
                       "scope": "real Sol; isolated Codex assistant/provider counterpart, synthetic questions excluded"}
            atomic(Path(evidence_dir) / "rehearsal.json", receipt)
            return receipt
        finally:
            await pipeline.cleanup(manifest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("collect", "run", "record-review", "decision", "summary", "rehearse", "history-collect", "history-run", "history-consolidate"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--config", required=True)
        if name.startswith("history-"):
            cmd.add_argument("--baseline", required=True)
            if name == "history-consolidate":
                group = cmd.add_mutually_exclusive_group(required=True)
                group.add_argument("--batches", help="comma-separated completed batch numbers, one to five")
                group.add_argument("--final", action="store_true", help="one final Astra review of the complete inventory")
            else:
                cmd.add_argument("--batch", required=True, type=int)
                if name == "history-run":
                    cmd.add_argument("--no-deliver", action="store_true", help="retain serial-context packets without any delivery")
        elif name == "collect":
            cmd.add_argument("--now", required=True)
        elif name == "run":
            cmd.add_argument("--on-demand", action="store_true")
        elif name == "record-review":
            cmd.add_argument("--run-id", required=True)
            cmd.add_argument("--result", required=True, help="JSON file path")
        elif name == "decision":
            cmd.add_argument("--work-id", required=True)
            cmd.add_argument("--proposal", required=True, help="JSON file path")
            cmd.add_argument("--retry-blocked", action="store_true", help="Resume one blocked ask after daemon admission is live")
        elif name == "summary":
            cmd.add_argument("--end-day", required=True, help="last local date of a seven-day retained-receipt summary")
        else:
            cmd.add_argument("--workers-config", required=True)
            cmd.add_argument("--evidence-dir", required=True)
    args = parser.parse_args()
    settings_holder = []
    try:
        result = _run_command(args, settings_holder)
    except Exception as exc:
        # The scheduled job's stderr is a log file: never let a traceback carry
        # the exception text there. The raw traceback is kept with the run's
        # other raw errors (0600); stderr gets the structured record only.
        root = settings_holder[0].state_root if settings_holder else None
        record = structured_error(exc, root)
        if root is not None:
            _keep_raw_error(Path(root), record["sha256"] + ".traceback",
                            traceback.format_exc().encode("utf-8", "backslashreplace"))
        print(json.dumps({"error": record}), file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False))


def _run_command(args, settings_holder):
    settings = Settings.load(args.config)
    settings_holder.append(settings)
    if args.command == "collect":
        result = collect(settings, datetime.fromisoformat(args.now))
    elif args.command == "summary":
        result = weekly_summary(settings, args.end_day)
    elif args.command == "history-collect":
        result = history_collect(settings, args.baseline, args.batch)
    elif args.command == "rehearse":
        result = asyncio.run(rehearse(settings, Settings.load(args.workers_config), args.evidence_dir))
    else:
        pipeline = Pipeline(settings, ProducerTransport(settings)) if args.command in {"run", "history-run", "history-consolidate"} else Pipeline(settings)
        if args.command == "run":
            result = asyncio.run(pipeline.run(on_demand=args.on_demand))
        elif args.command == "history-run":
            result = asyncio.run(pipeline.history_run(args.baseline, args.batch, no_deliver=args.no_deliver))
        elif args.command == "history-consolidate":
            batches = [int(n) for n in args.batches.split(",")] if args.batches else None
            result = asyncio.run(pipeline.history_consolidate(args.baseline, batches, final=args.final))
        elif args.command == "record-review":
            result = asyncio.run(pipeline.record_review(args.run_id, read(Path(args.result))))
        else:
            result = asyncio.run(pipeline.decision(args.work_id, read(Path(args.proposal)), retry_blocked=args.retry_blocked))
    return result


if __name__ == "__main__":
    main()
