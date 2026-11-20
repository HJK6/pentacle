"""Assignment-scoped Status evidence, evaluated inside the report write transaction."""
import json

from store_specs import effective_assignment, normalize_spec_binding_provenance
from store_watch_wake import _session
from work_lane_progress import STATUS_BEHIND_GRACE_S, seat_live


class ReportStatusRefusal(ValueError):
    def __init__(self, code, spec_id, reason):
        self.code, self.spec_id, self.reason = code, spec_id, reason
        super().__init__(f"{spec_id or 'Unknown assignment'}: {reason}. Update summary.md **Status** to the actual result and file an item report.")


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS v2_report_status_boundaries (
        stream_id TEXT PRIMARY KEY, generation TEXT NOT NULL, spec_id TEXT NOT NULL,
        boundary_at REAL NOT NULL, known INTEGER NOT NULL, status_line TEXT,
        summary_revision TEXT)""")


def observation(conn, spec_id):
    row = conn.execute("""SELECT o.record_json FROM v2_work_item_observations o
        JOIN v2_work_index_state i ON i.singleton=1
        WHERE o.spec_id=? AND json_extract(i.state_json,'$.available')=1
        AND json_extract(o.record_json,'$.member.observation.quality')='fresh'""", (spec_id,)).fetchone()
    return json.loads(row[0]) if row else {}


def save_boundary(conn, stream, generation, spec, now, known, line, revision):
    conn.execute("INSERT OR REPLACE INTO v2_report_status_boundaries VALUES (?,?,?,?,?,?,?)",
                 (stream, generation, spec, now, int(known), line, revision))


def assignment_changed_conn(conn, stream, now):
    reporter = _session(conn, stream)
    spec = effective_assignment(reporter)
    if not spec or not reporter or reporter["status"] != "open":
        conn.execute("DELETE FROM v2_report_status_boundaries WHERE stream_id=?", (stream,))
        return
    generation = reporter["session_generation"]
    if conn.execute("SELECT 1 FROM v2_report_status_boundaries WHERE stream_id=? AND generation=? AND spec_id=?",
                    (stream, generation, spec)).fetchone():
        return
    record = observation(conn, spec)
    revision = (record.get("document_revisions") or {}).get("summary.md")
    member = record.get("member", {})
    save_boundary(conn, stream, generation, spec, now, bool(revision and "status_line" in member),
                  member.get("status_line"), revision)


def _explicitly_assigned(row, spec):
    if row.get("assigned_spec_id") is not None:
        return effective_assignment(row) == spec
    grants = normalize_spec_binding_provenance(row.get("spec_binding_provenance"), spec_ids=row.get("spec_ids"))
    return effective_assignment(row) == spec and any(
        g["spec_id"] == spec and g["provenance"] in {"spawn_explicit", "operator_v2"} for g in grants)


def _helper_witness(conn, reporter, spec, authenticated):
    from store_host_transfer import authority_state
    from work_attendance import attendance_conn

    def refuse(reason):
        raise ReportStatusRefusal("seat_only_ineligible", spec, reason)

    stream = reporter["stream_id"]
    if (not authenticated or not spec or not seat_live(reporter) or not _explicitly_assigned(reporter, spec)
            or authority_state(conn, stream, reporter["session_generation"])):
        refuse("helper eligibility unverifiable")
    attendance = attendance_conn(conn)
    owners = attendance.owners.get(spec, [])
    if stream in owners:
        refuse("reporter owns this item")
    if stream in attendance.uncertain.get(spec, ()) or reporter.get("provider") == "composite":
        refuse("helper eligibility unverifiable")
    rows = conn.execute("""SELECT host,session_name,s.status,s.spec_id,s.spec_ids,s.assigned_spec_id,
        s.spec_binding_provenance,s.presumed_dead_at,s.offline_since_ts,s.pane_status,s.provider,
        g.generation AS session_generation FROM sessions s
        LEFT JOIN v2_session_generations g USING(host,session_name) WHERE s.status='open'
        ORDER BY s.host,s.session_name""")
    uncertain = False
    for raw in rows:
        peer = dict(raw)
        witness = f"{peer['host']}:{peer['session_name']}"
        if witness == stream or peer.get("provider") == "composite" or not seat_live(peer):
            continue
        if not (witness in owners or _explicitly_assigned(peer, spec)):
            continue
        if authority_state(conn, witness, peer["session_generation"]):
            continue
        if peer["session_generation"] and peer.get("pane_status") == "pane_alive":
            return {"stream_id": witness, "generation": peer["session_generation"]}
        uncertain = True
    refuse("helper eligibility unverifiable" if uncertain else "sole live assignee")


def apply_report_gate(conn, report, reporter, context, now):
    """Called after replay and binding CAS. Only daemon-computed evidence is consumed."""
    if report["status"] != "done":
        return
    stream, generation = reporter["stream_id"], reporter["session_generation"]
    spec = effective_assignment(reporter)
    auth = context.get("authenticated_generation")
    verified = bool(auth and auth == generation)
    receipt = {"spec_id": spec, "generation": generation, "report_id": report["report_id"]}
    if report.get("completion_kind") == "seat_only":
        receipt.update(outcome="seat_only", witness=_helper_witness(conn, reporter, spec, verified))
        report["usage_snapshot"]["attribution"] = {"mode": "exclusive_primary", "spec_id": spec}
    elif not spec or reporter["status"] != "open":
        return
    elif report.get("qa_verdict") and verified and "qa" in {reporter.get("role"), reporter.get("phase")}:
        receipt.update(outcome="review_exemption")
    else:
        checkpoint = context.get("checkpoint") or {}
        if checkpoint and (checkpoint.get("spec_id"), checkpoint.get("session_generation")) != (spec, generation):
            from store import SpecInputsUnavailable
            raise SpecInputsUnavailable("report_assignment_changed")
        boundary = conn.execute("""SELECT * FROM v2_report_status_boundaries
            WHERE stream_id=? AND generation=? AND spec_id=?""", (stream,generation,spec)).fetchone()
        facts = checkpoint.get("facts") or {}
        known = "status_line" in facts and checkpoint.get("state") != "unverifiable"
        line = facts.get("status_line")
        rev = (checkpoint.get("source_revisions") or {}).get("summary.md")
        if known:
            receipt["source_revisions"] = checkpoint["source_revisions"]
        if known and not line:
            raise ReportStatusRefusal("report_status_invalid", spec, "Status is missing or invalid")
        if known and boundary and boundary["known"] and line == boundary["status_line"]:
            raise ReportStatusRefusal("report_status_unchanged", spec, "Status is unchanged since the last assignment or report")
        receipt.update(outcome="unverifiable", cause="source_unavailable" if not known else "assignment_history_unavailable")
        if known and boundary and boundary["known"]:
            record = observation(conn, spec)
            change = record.get("status_change") or {}
            publication = checkpoint.get("status_publication") or {}
            published = publication.get("at")
            # A scan can lag publication. Its clock cannot date an old edit.
            # Without exact publication time, the last observation of the old
            # value supplies only a lower bound for the change.
            prior_seen = change.get("after")
            observed_after = (change.get("value") == line and prior_seen is not None
                              and prior_seen >= boundary["boundary_at"])
            if published is not None and published + 1 <= boundary["boundary_at"] and not observed_after:
                raise ReportStatusRefusal("report_status_unchanged", spec, "the accepted Status predates this assignment or report")
            if (published is not None and boundary["boundary_at"] < published <= now) or observed_after:
                receipt.update(outcome="changed", status_revision=publication.get("revision") or change.get("revision"),
                               published_at=published, summary_revision=rev,
                               covers_event=bool(published is not None and 0 <= now - published < STATUS_BEHIND_GRACE_S))
                receipt.pop("cause")
            else:
                receipt["cause"] = "publication_order_unverifiable"
        save_boundary(conn, stream, generation, spec, now, known, line, rev)
    report["status_freshness"] = receipt
