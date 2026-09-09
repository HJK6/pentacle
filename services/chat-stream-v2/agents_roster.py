"""Direct-child projection from the complete open inventory, before client filtering."""
from v2_runtime import iso_now


def project(rows, transitions):
    by_parent = {}
    live_keys = set()
    for row in sorted(rows, key=lambda r: (str(r.get("created_at") or ""), str(r.get("stream_id") or ""))):
        if row.get("status", "open") != "open":
            continue
        sid = str(row.get("stream_id") or "")
        generation = str(row.get("session_generation") or "")
        key = (sid, generation)
        live_keys.add(key)
        status = row.get("_agent_report_status")
        state = {"error": "blocked", "done": "done", "aborted": "done"}.get(status)
        state = state or ("working" if row.get("working") else "idle")
        prior = transitions.get(key)
        if not prior or prior[0] != state:
            report_transition = status in {"error", "done", "aborted"} or (
                status == "progress" and (not prior or prior[0] in {"blocked", "done"}))
            since = row.get("_agent_report_ts") if report_transition else None
            since = since or (iso_now() if prior else row.get("turn_state_since") or row.get("created_at"))
            transitions[key] = (state, since)
        parent = row.get("parent_stream_id")
        if parent:
            by_parent.setdefault(parent, []).append({
                "stream_id": sid, "session_generation": generation,
                "display_name": str(row.get("display_name") or row.get("title") or sid),
                "role": row.get("role"), "objective": row.get("objective"),
                **({"objective_source": row["objective_source"]} if row.get("objective_source") else {}),
                "state": state, "model": row.get("effective_model") or row.get("requested_model") or None,
                "since": transitions[key][1],
            })
    for key in set(transitions) - live_keys:
        del transitions[key]
    result = []
    for row in rows:
        clean = {k: v for k, v in row.items() if k not in {"agents", "_agent_report_status", "_agent_report_ts"}}
        if agents := by_parent.get(row.get("stream_id")):
            clean["agents"] = agents
        result.append(clean)
    return result
