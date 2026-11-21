"""Per-item tracking incidents. All transitions share the lane sweep's writer transaction."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import hashlib
import json
import os
import re

from outbound_notices import ensure_notice_marker
from store_lifecycle_authority import configured, front_desk_principal
from store_routing import _insert_outbound_notice_conn
from work_lane_progress import member_status_behind
from work_owed import owed_dispositions_conn
from work_unattended import unattended_work_conn

REPEAT_SECONDS = 86400
KIND = "work_item_tracking"
EXPIRY = "2026-11-23T19:00:00+00:00"


def epoch(stamp):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def enabled():
    return os.environ.get("PENTACLE_ITEM_TRACKING_RELEASE") == "1"


def active(conn):
    return enabled() and conn.execute("SELECT 1 FROM v2_item_tracking_activation").fetchone() is not None


def activate(conn, stamp):
    if not enabled():
        return False
    if active(conn):
        return True
    clearance = os.environ.get("PENTACLE_ITEM_TRACKING_CLEARANCE_SHA256", "")
    if re.fullmatch(r"[0-9a-f]{64}", clearance):
        reason = "clearance"
    elif epoch(stamp) >= epoch(EXPIRY):
        reason, clearance = "expiry", None
    else:
        return False
    retire_legacy(conn, stamp)
    conn.execute("INSERT INTO v2_item_tracking_activation VALUES (1,?,?,?)", (stamp, reason, clearance))
    return True


def retire_legacy(conn, stamp):
    """Cancel only pre-input status openings. Release unrelated folded members for normal batching."""
    rows = conn.execute("""SELECT n.* FROM v2_outbound_notices n
        WHERE n.kind IN ('front_desk_held','error_alert','lane_digest')
        AND n.delivered_at IS NULL AND n.terminal_at IS NULL AND (n.lease_until IS NULL OR n.lease_until<=?)
        AND NOT EXISTS (SELECT 1 FROM v2_tell_deliveries t WHERE t.tell_id=n.tell_id)""", (epoch(stamp),)).fetchall()
    def behind(metadata):
        return metadata.get("family") == "work_lane.v1" and conn.execute(
            "SELECT 1 FROM v2_work_lane_episodes WHERE kind='status_behind' AND episode_id=?",
            (metadata.get("episode_id"),)).fetchone() is not None
    for row in rows:
        meta = json.loads(row["metadata"])
        if row["kind"] == "lane_digest":
            from message_envelopes import build_message_envelope, match_message_envelope
            data = match_message_envelope(row["body"]) or {}
            if "unattended_work" not in data and "owed_dispositions" not in data:
                continue
            # Preserve the lane inventory in a new immutable notice.
            nid = "d2:" + hashlib.sha256((row["notice_id"] + "/lanes").encode()).hexdigest()
            body = build_message_envelope("lane_digest", notice_id=nid, lanes=data["lanes"],
                                          evaluated_at=data["evaluated_at"])
            _insert_outbound_notice_conn(conn, notice_id=nid, tell_id=nid, kind="lane_digest",
                dedupe_key=nid, recipient_stream_id=row["recipient_stream_id"], body=body, metadata=meta)
        elif meta.get("typed_digest"):
            members = list(conn.execute("""SELECT notice_id,metadata FROM v2_outbound_notices
                WHERE notice_id IN (SELECT value FROM json_each(?)) AND terminal_reason='folded_into_digest'""",
                (json.dumps(meta.get("member_notice_ids", [])),)))
            obsolete = {m[0] for m in members if behind(json.loads(m[1]))}
            if not obsolete:
                continue
            for member_id, _ in members:
                if member_id not in obsolete:
                    conn.execute("""UPDATE v2_outbound_notices SET terminal_at=NULL,terminal_reason=NULL,
                        metadata=json_remove(metadata,'$.folded_into_notice_id') WHERE notice_id=?""", (member_id,))
        elif not behind(meta):
            continue
        conn.execute("UPDATE v2_outbound_notices SET terminal_at=?,terminal_reason='tracking_superseded' WHERE notice_id=?",
                     (stamp, row["notice_id"]))


def cause(condition, kind, ident, *, at=None, actor=None, generation=None, role=None, gate=None, outcome=None, reason=None, action):
    return dict(condition=condition, event_kind=kind, event_id=ident, event_at=at, actor=actor or "unknown", actor_generation=generation,
                seat_role=role or "unknown", gate=gate or "legacy/unknown",
                gate_outcome=outcome or "legacy/unknown", gate_reason=reason, action=action)


def collect(conn, lanes, stamp, *, spec_id=None):
    causes = defaultdict(dict)
    uncertain = defaultdict(set)
    for lane in sorted(lanes, key=lambda lane: lane["lane_id"]):
        if lane["work_state"] == "done" or (lane["work_state"] == "paused" and lane.get("parked_at")):
            continue
        for member in lane["_members"]:
            spec = member["spec_id"]
            if spec_id is not None and spec != spec_id:
                continue
            if member.get("observation", {}).get("quality") != "fresh":
                uncertain[spec].add("status_behind")
            if not member_status_behind(member, stamp, lane["_work_index_available"]) or "status_behind" in causes.get(spec, {}):
                continue
            event = member["_latest_event"]
            rid = member["_latest_report_id"]
            report = conn.execute("SELECT from_stream_id,status_freshness FROM v2_reports WHERE report_id=?", (rid,)).fetchone()
            proof = json.loads(report[1] or "{}") if report else {}
            if not isinstance(proof, dict) or (proof.get("report_id"), proof.get("spec_id")) != (rid, spec):
                proof = {}
            causes[spec]["status_behind"] = cause("status_behind", event["kind"], rid,
                at=event["at"], actor=report[0] if report else None, generation=proof.get("generation"),
                role=proof.get("seat_role"), reason=proof.get("cause"),
                gate="report_status_freshness" if proof else None, outcome=proof.get("outcome"),
                action=f"Read agent-orch work-lane show {lane['lane_id']} --members and update this item's Status.")
    unattended = unattended_work_conn(conn, spec_id=spec_id)
    for item in unattended["omitted"]:
        uncertain[item["spec_id"]].add("unattended")
    for item in unattended["items"]:
        spec = item["spec_id"]
        row = conn.execute("SELECT * FROM v2_work_attendance_causes WHERE spec_id=? "
                           "ORDER BY observed_at DESC,gate LIMIT 1", (spec,)).fetchone()
        evidence = dict(row) if row else {}
        # A cleared occurrence cannot lend its gate outcome to a later loss.
        old = conn.execute("SELECT cleared_at,prior_clear FROM v2_item_tracking WHERE spec_id=?", (spec,)).fetchone()
        boundary = (old["cleared_at"] or old["prior_clear"]) if old else None
        if evidence and boundary and epoch(evidence["observed_at"]) <= epoch(boundary):
            evidence = {}
        causes[spec]["unattended"] = cause("unattended", evidence.get("gate") or "owner_missing",
            evidence.get("observed_at") or spec, at=evidence.get("observed_at"), actor=evidence.get("stream_id"),
            role=evidence.get("seat_role"), generation=evidence.get("generation"),
            gate=evidence.get("gate"), outcome=evidence.get("cause"),
            action="Read agent-orch work unattended --json and assign a live implementation owner or record a disposition.")
    debts = defaultdict(list)
    for debt in owed_dispositions_conn(conn, spec_id=spec_id)["items"]:
        debts[debt["spec_id"]].append(debt)
    for spec, items in debts.items():
        debt = items[-1]
        identity = hashlib.sha256(json.dumps([(i["closing_stream_id"], i["session_generation"], i["closed_at"])
                                              for i in items]).encode()).hexdigest()
        text = "Read agent-orch work owed --json and record the ended assignment’s disposition."
        if any(i["quality"] == "unknown" for i in items):
            text += " Verify the source status and the assignment baseline first."
        causes[spec]["owed"] = cause("owed", "assignment_ended", identity, actor=debt["closing_stream_id"],
                                     at=debt["closed_at"], generation=debt["session_generation"], role=debt.get("seat_role"), action=text)
        causes[spec]["owed"].update(assignment_started_at=debt["started_at"], debt_count=len(items),
                                     source_quality="unknown" if any(i["quality"] == "unknown" for i in items) else "fresh")
    return causes, uncertain, unattended["available"]


def revision(value):
    return (value["condition"], value["event_id"], value["gate"], value["gate_outcome"])


def reconcile(conn, lanes, stamp, repeat_seconds=REPEAT_SECONDS):
    observed, uncertain, available = collect(conn, lanes, stamp)
    opened = {r["spec_id"]: r for r in conn.execute("SELECT * FROM v2_item_tracking WHERE cleared_at IS NULL")}
    try:
        desk = front_desk_principal(conn, configured(conn))
    except ValueError:
        desk = None
    from work_attendance import assigned_owners_conn
    owners = assigned_owners_conn(conn) if observed else {}
    for spec in sorted(opened.keys() | observed.keys()):
        state = opened.get(spec)
        current = dict(observed.get(spec, {}))
        previous = json.loads(state["causes"]) if state else {}
        if "unattended" in previous and "unattended" not in current and not conn.execute(
                "SELECT 1 FROM v2_work_item_observations WHERE spec_id=? "
                "AND json_extract(record_json,'$.member.observation.quality')='fresh'", (spec,)).fetchone():
            uncertain[spec].add("unattended")
        for key, value in previous.items():
            if key not in current and (not available or key in uncertain.get(spec, ())):
                current[key] = value
        notice = (conn.execute("SELECT * FROM v2_outbound_notices WHERE notice_id=?", (state["notice_id"],)).fetchone()
                  if state and state["notice_id"] else None)
        verified = bool(observed.get(spec))
        changed_after_input = False
        if notice and not notice["delivered_at"] and not notice["terminal_at"]:
            attempted = conn.execute("SELECT 1 FROM v2_tell_deliveries WHERE tell_id=?", (notice["tell_id"],)).fetchone()
            if attempted:
                # This row has its own evidence-only recovery. A newly verified
                # cause may need a new message while the old proof is pending.
                sent = {revision(c) for c in json.loads(notice["metadata"])["tracking_causes"]}
                changed_after_input = any(revision(c) not in sent for c in observed.get(spec, {}).values())
                if current and not changed_after_input:
                    continue
            elif current == previous:
                if bool(state["verified"]) != verified:
                    conn.execute("UPDATE v2_item_tracking SET verified=? WHERE spec_id=?", (verified, spec))
                continue
            else:
                conn.execute("UPDATE v2_outbound_notices SET terminal_at=?,terminal_reason='tracking_superseded' WHERE notice_id=?",
                             (stamp, notice["notice_id"]))
        if not current:
            conn.execute("UPDATE v2_item_tracking SET cleared_at=?,verified=0 WHERE spec_id=?", (stamp, spec))
            continue
        if not state:
            conn.execute("""INSERT INTO v2_item_tracking(spec_id,occurrence,opened_at) VALUES (?,1,?)
                          ON CONFLICT(spec_id) DO UPDATE SET occurrence=occurrence+1,opened_at=excluded.opened_at,
                          prior_clear=cleared_at,cleared_at=NULL,notice_id=NULL,last_success=NULL,sequence=0""", (spec, stamp))
            state = conn.execute("SELECT * FROM v2_item_tracking WHERE spec_id=?", (spec,)).fetchone()
        last = notice if notice and notice["delivered_at"] else None
        if not last and state["last_success"]:
            last = conn.execute("SELECT * FROM v2_outbound_notices WHERE notice_id=?", (state["last_success"],)).fetchone()
        conn.execute("UPDATE v2_item_tracking SET causes=?,verified=?,last_success=? WHERE spec_id=?",
                     (json.dumps(current, sort_keys=True), verified, last["notice_id"] if last else None, spec))
        if not verified or not desk:
            continue
        delivery = "changed" if changed_after_input else "initial"
        if last and not changed_after_input:
            prior = {revision(c) for c in json.loads(last["metadata"])["tracking_causes"]}
            if any(revision(c) not in prior for c in current.values()):
                delivery = "changed"
            elif epoch(stamp) - epoch(last["delivered_at"]) >= repeat_seconds:
                delivery = "repeat"
            else:
                continue
        seq = state["sequence"] + 1
        occurrence = state["occurrence"]
        nid = "tracking:" + hashlib.sha256(json.dumps([spec, occurrence, seq]).encode()).hexdigest()
        events = [current[k] for k in sorted(current)]
        responsible = owners.get(spec) or [c["actor"] for c in events if c["condition"] in {"owed","unattended"}
                                           and c["actor"] != "unknown" and c["gate"] != "commission"]
        owner = ", ".join(dict.fromkeys(responsible[:3])) if responsible else "unknown; verify the assignment"
        lines = [f"{e['condition']} ({e['event_kind']}, {e['event_id']} at {e['event_at'] or 'unknown time'}, actor {e['actor']}, {e['gate_outcome']}). {e['action']}"
                 for e in events]
        body = ensure_notice_marker(nid, f"Work tracking {delivery} for {spec} (owner or last known owner: {owner})\n" + "\n".join(lines))
        _insert_outbound_notice_conn(conn, notice_id=nid, tell_id=nid, kind=KIND, dedupe_key=nid,
            recipient_stream_id=desk["stream_id"], episode_id=f"{spec}/{occurrence}", body=body, created_at=stamp,
            metadata=dict(spec_id=spec, occurrence=occurrence, delivery_type=delivery, owner=owner,
                          root_generation=desk["session_generation"], tracking_causes=events))
        conn.execute("UPDATE v2_item_tracking SET sequence=?,notice_id=? WHERE spec_id=?", (seq, nid, spec))


def check_input(conn, notice_id, target, generation):
    """Fence the first possible paste against the current source and bound generation."""
    from store_work_index import effective_membership_conn
    from store_work_lanes import enrich_latest_reports_conn, _now
    from sessions import VerbError
    row = conn.execute("""SELECT n.metadata FROM v2_item_tracking i JOIN v2_outbound_notices n
        ON n.notice_id=i.notice_id WHERE i.notice_id=? AND i.cleared_at IS NULL
        AND n.terminal_at IS NULL AND n.delivered_at IS NULL""", (notice_id,)).fetchone()
    desk = front_desk_principal(conn, configured(conn)) or {}
    if not active(conn) or not row or (target,generation) != (desk.get("stream_id"),desk.get("session_generation")):
        raise VerbError("tracking_changed", "tracking incident or current front desk changed")
    meta = json.loads(row[0])
    spec = meta["spec_id"]
    snapshot = effective_membership_conn(conn, spec_ids=[spec])
    member = snapshot["members"].get(spec)
    lanes = []
    if member:
        enrich_latest_reports_conn(conn, [member])
        for lane in conn.execute("SELECT lane_id,work_state,parked_at FROM v2_assistant_composite_lanes"):
            if spec in snapshot["membership"].get(lane["lane_id"], ()):
                lanes.append(dict(lane, _members=[member], _work_index_available=snapshot["state"]["available"]))
    causes, unknown, available = collect(conn, lanes, _now(), spec_id=spec)
    fresh = causes.get(spec, {})
    expected = {c["condition"]:c for c in meta["tracking_causes"]}
    # Historical causes held across a partial read never authorize a paste.
    if not fresh or any(k not in expected or revision(c) != revision(expected[k]) for k,c in fresh.items()) or any(
            k not in fresh and available and k not in unknown.get(spec, ()) for k in expected):
        raise VerbError("tracking_changed", "await a current tracking observation")
    if conn.execute("SELECT 1 FROM v2_tell_deliveries WHERE tell_id=?", (notice_id,)).fetchone():
        raise VerbError("tracking_input_owned", "reconcile the existing input receipt")
