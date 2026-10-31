"""Assignment/disposition history in the existing work-index JSON, on its writer."""
from __future__ import annotations

from collections import Counter
import copy
import json
from pathlib import PurePath

from store_specs import effective_assignment, _hydrate_spec_row, _row
from store_work_index import _state_conn
from _shared.work_observations import timestamp

STATE_KEY = "_lead_dispositions"


def _registry(state):
    return copy.deepcopy(state.get(STATE_KEY) or {"episodes": {}, "owed": {}})


def disposition_spec_ids_conn(conn):
    """Current episode/debt references, read within the caller's transaction."""
    registry = _registry(_state_conn(conn))
    return {entry["spec_id"] for kind in ("episodes", "owed") for entry in registry[kind].values()}


def _save(conn, state, registry):
    if registry == state.get(STATE_KEY):
        return
    state[STATE_KEY] = registry
    conn.execute("INSERT INTO v2_work_index_state(singleton,state_json) VALUES (1,?) "
                 "ON CONFLICT(singleton) DO UPDATE SET state_json=excluded.state_json",
                 (json.dumps(state, separators=(",", ":")),))


def _owners(conn):
    owners = {}
    for raw in conn.execute("SELECT s.*,g.generation AS session_generation FROM sessions s "
                            "JOIN v2_session_generations g USING(host,session_name) "
                            "WHERE s.status='open' AND s.role IN ('lead','nexus')"):
        row = _hydrate_spec_row(_row(raw))
        spec = effective_assignment(row)
        if spec and not row.get("presumed_dead_at"):
            stream = f"{row['host']}:{row['session_name']}"
            key = json.dumps([spec, stream, row["session_generation"]], separators=(",", ":"))
            owners[key] = (spec, stream, row)
    return owners


def _observation(conn, state, spec):
    raw = conn.execute("SELECT record_json FROM v2_work_item_observations WHERE spec_id=?", (spec,)).fetchone()
    record = json.loads(raw[0]) if raw else {}
    member = record.get("member") or {}
    observation = member.get("observation") or {}
    fresh = bool(state.get("available") and observation.get("quality") == "fresh"
                 and record.get("source_hash"))
    return {"known": fresh, "status": member.get("status") if fresh else "unknown",
            "revision": record.get("source_hash") if fresh else None,
            "parked": member.get("parked_declaration") if fresh else None,
            "observed_at": state.get("snapshot_at") if fresh else None,
            "title": member.get("title"), "path": record.get("path"),
            "document_revisions": record.get("document_revisions") or {}}


def _advance(episode, observation):
    """Only a witnessed revision transition can establish disposition evidence."""
    if not observation["known"]:
        return
    prior = episode.get("previous") or {}
    if observation["status"] == "in_progress":
        episode["disposition"] = None
    elif prior.get("known") and prior.get("revision") != observation["revision"]:
        moved = prior.get("status") != observation["status"]
        parked = (observation["status"] == "blocked" and observation.get("parked")
                  and prior.get("parked") != observation["parked"])
        if moved or parked:
            episode["disposition"] = observation["revision"]
    episode["previous"] = observation


def _disposed(episode, observation):
    return bool(observation["known"] and (
        observation["status"] in {"completed", "deprecated", "backlog"}
        or (episode.get("disposition") and observation["status"] != "in_progress")))


def reconcile_dispositions_conn(conn, now):
    """Called inside each session/accepted-observation mutation transaction."""
    state = _state_conn(conn)
    registry = _registry(state)
    owners = _owners(conn)
    specs = {value[0] for value in owners.values()}
    observations = {}
    from store_watch_wake import _session

    def observed(spec):
        if spec not in observations:
            observations[spec] = _observation(conn, state, spec)
        return observations[spec]

    for key, episode in list(registry["episodes"].items()):
        raw = owners[key][2] if key in owners else _session(conn, episode["closing_stream_id"])
        if (raw and raw["session_generation"] == episode["session_generation"]
                and raw["parent_stream_id"] != episode.get("assignment_parent_stream_id")):
            parent = raw["parent_stream_id"]
            parent_row = _session(conn, parent) if parent else None
            pair = [parent, parent_row["session_generation"] if parent_row else None]
            episode.update(parent_stream_id=parent, parent_generation=pair[1],
                assignment_parent_stream_id=parent,
                front_desk_owned=not parent or pair == registry.get("root"))
        observation = observed(episode["spec_id"])
        _advance(episode, observation)
        if key in owners:
            continue
        if episode["spec_id"] not in specs and not _disposed(episode, observation):
            registry["owed"][key] = {**episode, "closed_at": timestamp(now)}
        del registry["episodes"][key]
    for key, (spec, stream, row) in owners.items():
        if key not in registry["episodes"]:
            observation = observed(spec)
            parent = row.get("parent_stream_id")
            parent_row = _session(conn, parent) if parent else None
            registry["episodes"][key] = {"spec_id": spec, "closing_stream_id": stream,
                "session_generation": row["session_generation"], "started_at": timestamp(now),
                "baseline_known": observation["known"], "baseline_revision": observation["revision"],
                "previous": observation, "disposition": None,
                "parent_stream_id": parent, "parent_generation": parent_row["session_generation"] if parent_row else None,
                "assignment_parent_stream_id": parent,
                "front_desk_owned": not bool(parent)}
    for key, debt in list(registry["owed"].items()):
        observation = observed(debt["spec_id"])
        _advance(debt, observation)
        if debt["spec_id"] in specs or _disposed(debt, observation):
            del registry["owed"][key]
    _save(conn, state, registry)


def move_disposition_custody_conn(conn, source, generation, target, target_generation, *, child=None):
    state = _state_conn(conn)
    registry = _registry(state)
    for episode in [*registry["episodes"].values(), *registry["owed"].values()]:
        if (episode.get("parent_stream_id"), episode.get("parent_generation")) != (source, generation):
            continue
        if child and (episode["closing_stream_id"], episode["session_generation"]) != tuple(child):
            continue
        episode.update(parent_stream_id=target, parent_generation=target_generation)
        if child:
            episode.update(assignment_parent_stream_id=target,
                           front_desk_owned=registry.get("root") == [target, target_generation])
    _save(conn, state, registry)


def disposition_root_conn(conn, root, now):
    """Use the existing generation-bound digest custody, retaining every debt."""
    state = _state_conn(conn)
    registry = _registry(state)
    prior = registry.get("root")
    if prior and prior != list(root):
        conn.execute("UPDATE v2_outbound_notices SET terminal_at=?, "
            "terminal_reason='root_generation_replaced' WHERE kind='lane_digest' "
            "AND recipient_stream_id=? AND delivered_at IS NULL AND terminal_at IS NULL "
            "AND json_extract(metadata,'$.root_generation')=?", (timestamp(now), *prior))
    for episode in [*registry["episodes"].values(), *registry["owed"].values()]:
        pair = [episode.get("parent_stream_id"), episode.get("parent_generation")]
        if episode.get("front_desk_owned") or (prior and pair == prior) or pair == list(root):
            episode.update(parent_stream_id=root[0], parent_generation=root[1], front_desk_owned=True)
    registry["root"] = list(root)
    _save(conn, state, registry)


def owed_dispositions_conn(conn, *, now=None, root_binding=None):
    state = _state_conn(conn)
    registry = _registry(state)
    valid_root = (isinstance(root_binding, (tuple, list)) and len(root_binding) == 2
                  and all(isinstance(value, str) and value for value in root_binding))
    root = root_binding if valid_root else registry.get("root")
    items = []
    for debt in registry["owed"].values():
        observation = _observation(conn, state, debt["spec_id"])
        unknown = not debt["baseline_known"] or not observation["known"]
        parent = root[0] if root and debt.get("front_desk_owned") else debt.get("parent_stream_id")
        parent_generation = root[1] if root and debt.get("front_desk_owned") else debt.get("parent_generation")
        stem = PurePath(observation.get("path") or debt["spec_id"].removeprefix("spec_")).name
        items.append({"spec_id": debt["spec_id"], "closing_stream_id": debt["closing_stream_id"],
            "session_generation": debt["session_generation"], "parent_stream_id": parent,
            "parent_generation": parent_generation,
            "started_at": debt["started_at"], "closed_at": debt["closed_at"],
            "status": observation["status"], "quality": "unknown" if unknown else "fresh",
            "title": observation.get("title"), "repo": stem.split("__", 1)[0]})
    items.sort(key=lambda item: (item["closed_at"], item["spec_id"], item["closing_stream_id"]))
    unknown = sum(item["quality"] == "unknown" for item in items)
    from store_watch_wake import _fleet_interval
    return {"total": len(items), "counts_by_repo": dict(sorted(Counter(i["repo"] for i in items).items())),
            "unknown_count": unknown, "quality": "degraded" if unknown or not state.get("available") else "fresh",
            "digest_delivery": "enabled" if _fleet_interval("PENTACLE_LANE_DIGEST_S", 1800) else "disabled",
            "query": "agent-orch work owed --json", "items": items}


def owes_on_close_conn(conn, stream, generation):
    state = _state_conn(conn)
    owners = _owners(conn)
    own = [(key, spec) for key, (spec, sid, row) in owners.items()
           if sid == stream and row["session_generation"] == generation]
    for key, spec in own:
        if any(other_spec == spec and sid != stream for other_spec, sid, _ in owners.values()):
            continue
        observation = _observation(conn, state, spec)
        episode = _registry(state)["episodes"].get(key)
        if not _disposed(episode or {}, observation):
            return True
    return False


def lead_close_context_conn(conn, stream, generation):
    state = _state_conn(conn)
    owners = _owners(conn)
    own = [(spec, row) for spec, sid, row in owners.values()
           if sid == stream and row["session_generation"] == generation]
    for spec, row in own:
        observation = _observation(conn, state, spec)
        if (observation["known"] and observation["status"] == "in_progress"
                and not any(other_spec == spec and sid != stream for other_spec, sid, _ in owners.values())):
            return {"spec_id": spec, "stream_id": stream, "session_generation": generation, **observation}
    return None


def disposition_commands(context):
    """Recovery uses the observed two-document revisions, never a move override."""
    import shlex
    folder = PurePath(context.get("path") or "/memory/work/in_progress/item")
    stem = folder.name
    root = folder.parents[2] if len(folder.parents) >= 3 else PurePath("/memory")
    client = str(root / "scripts/memory_client.py")
    expected = {f"work/in_progress/{stem}/{name}": revision
                for name, revision in context["document_revisions"].items()}
    move = {"item_id": context["spec_id"], "status": "backlog", "expected": expected,
            "requesting_stream_id": context["stream_id"],
            "reason": "<reason for disposition>", "next_action": "<resume action>"}
    commands = []
    if len(expected) != 2:
        commands.extend(f"python3 {shlex.quote(client)} read <<'JSON'\n"
                        + json.dumps({"path": f"work/in_progress/{stem}/{name}"}) + "\nJSON"
                        for name in ("spec.md", "summary.md"))
    if len(expected) == 2:
        commands.append(f"python3 {shlex.quote(client)} move <<'JSON'\n" + json.dumps(move) + "\nJSON")
    commands.extend([
        f"agent-orch spec assign <successor> {context['spec_id']}",
        "Authorized parent/FD: agent-orch close --operator-confirm --expected-generation "
        + shlex.quote(context["session_generation"]) + " " + shlex.quote(context["stream_id"]),
    ])
    return commands
