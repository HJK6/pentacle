"""`agent-orch work-lane` wire shapes (spec D7 CLI)."""

from __future__ import annotations

import json

import pytest

from agent_orch import cli, work_lane_cli


@pytest.fixture(autouse=True)
def _composite_stream_id(monkeypatch):
    monkeypatch.setattr(work_lane_cli, "DEFAULT_COMPOSITE", "home:assistant")


def _wire(monkeypatch, reply):
    sent = []

    async def fake_once(_config, payload, *, timeout):
        sent.append(payload)
        return reply(payload) if callable(reply) else reply

    monkeypatch.setattr(cli, "assistant_once", fake_once)
    monkeypatch.setattr(cli, "prompt_ask_once", fake_once)
    monkeypatch.setattr(cli, "load_config", lambda: object())
    return sent


def _run(argv):
    args = cli.build_parser().parse_args(argv)
    return args.func(args)


def test_list_and_show_are_read_verbs(monkeypatch, capsys):
    lane = {"lane_id": "wl-1", "version": 2, "state": "paused", "state_reason": "lead_lost",
            "owner_kind": "fd", "title": "T", "lead": None,
            "visible_chat": {"stream_id": "home:assistant", "available": "open"}}
    sent = _wire(monkeypatch, {"type": "work_lanes.list.ok", "lanes": [lane]})
    assert _run(["work-lane", "list", "--include-done"]) == 0
    assert sent[-1] == {"type": "work_lanes.list", "include_done": True, "limit": 200}
    assert "paused/lead_lost" in capsys.readouterr().out
    sent = _wire(monkeypatch, {"type": "work_lanes.show.ok", "lane": {"lane_id": "wl-1"}, "projection": lane,
                               "events": [], "updates": []})
    assert _run(["work-lane", "show", "wl-1", "--json"]) == 0
    assert sent[-1] == {"type": "work_lanes.show", "lane_id": "wl-1"}


def test_mutators_wrap_assistant_operation(monkeypatch):
    sent = _wire(monkeypatch, {"type": "assistant.operation.ok"})
    assert _run(["work-lane", "set-state", "wl-1", "--to", "done", "--outcome", "Shipped",
                 "--expected-version", "3", "--request-id", "r1", "--confirmation-question-id", "q1"]) == 0
    assert sent[-1] == {
        "type": "assistant.operation", "request_id": "r1", "composite_stream_id": "home:assistant",
        "dispatch_id": "none", "operation": "work_lane.set_state", "lane_id": "wl-1",
        "expected_lane_version": 3,
        "payload": {"to": "done", "outcome": "Shipped", "operator_confirmation": {"question_id": "q1"}}}
    assert _run(["work-lane", "update", "wl-1", "--kind", "milestone", "--source-id", "m1", "--summary", "S",
                 "--grouped-source-ids", "a,b", "--expected-version", "4", "--request-id", "r2"]) == 0
    assert sent[-1]["payload"] == {"kind": "milestone", "source_id": "m1", "summary": "S",
                                   "grouped_source_ids": ["a", "b"]}
    assert _run(["work-lane", "set-lead", "wl-1", "--lead-stream-id", "h:s", "--lead-generation", "g",
                 "--expected-version", "5", "--request-id", "r3"]) == 0
    assert sent[-1]["payload"] == {"lead": {"stream_id": "h:s", "generation": "g"}}
    assert _run(["work-lane", "set-lead", "wl-1", "--expected-version", "5", "--request-id", "r4"]) == 2
    with pytest.raises(SystemExit):
        _run(["work-lane", "set-state", "wl-1", "--to", "done", "--request-id", "r5"])  # version required


def test_adopt_apply_uses_stable_request_ids(monkeypatch, tmp_path, capsys):
    sent = _wire(monkeypatch, {"type": "assistant.operation.ok"})
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"candidates": [
        {"adoption_key": "stream:h:a", "title": "A", "owner_kind": "operator", "work_state": "paused",
         "visible_chat": {"stream_id": "h:a", "generation": "g"}, "evidence": {"role": "lead"}},
    ]}))
    assert _run(["work-lane", "adopt", "--apply", str(plan)]) == 0
    assert _run(["work-lane", "adopt", "--apply", str(plan)]) == 0
    assert [m["request_id"] for m in sent] == ["adopt:stream:h:a", "adopt:stream:h:a"]
    assert "evidence" not in sent[0]["payload"] and sent[0]["operation"] == "work_lane.adopt"
    assert _run(["work-lane", "adopt", "--preview"]) == 0
    assert sent[-1] == {"type": "work_lanes.adopt_preview", "composite_stream_id": "home:assistant"}
    assert _run(["work-lane", "adopt"]) == 2


def test_request_confirmation_carries_typed_context(monkeypatch):
    monkeypatch.setenv("AGENT_ORCH_STREAM_ID", "host-b:v2-fd")
    sent = _wire(monkeypatch, {"type": "prompt.ask.ok"})
    assert _run(["work-lane", "request-confirmation", "wl-1", "--action", "set_state:done",
                 "--title", "Close lane?", "--body", "Mark the lane done."]) == 0
    envelope = sent[-1]["envelope"]
    assert envelope["context"] == {"schema": "WorkLaneConfirmationV1", "lane_id": "wl-1",
                                   "action": "set_state:done"}
    assert [o["value"] for o in envelope["options"]] == ["Confirm", "Not yet"]
    assert envelope["dedup_key"] == "work-lane-confirm:wl-1:set_state:done"


def test_member_commands_and_preview_payload(monkeypatch, tmp_path):
    sent = _wire(monkeypatch, {"type": "assistant.operation.ok"})
    assert _run(["work-lane", "set-members", "wl-1", "--member", "spec_demo__one", "--member", "spec_demo__two",
                 "--expected-version", "3", "--request-id", "members-1"]) == 0
    assert sent[-1]["operation"] == "work_lane.set_members"
    assert sent[-1]["payload"] == {"members": ["spec_demo__one", "spec_demo__two"], "no_spec_reason": None}
    assert _run(["work-lane", "show", "wl-1", "--members", "--json"]) == 0
    assert sent[-1]["members"] is True
    assert _run(["work-lane", "adopt", "--preview", "--epic", "epic_demo"]) == 0
    assert sent[-1]["epic"] == "epic_demo"
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps([{"adoption_key": "stream:h:b", "members": ["spec_demo__one"],
                                 "no_spec_reason": None, "evidence": "preview only"}]))
    assert _run(["work-lane", "adopt", "--apply", str(plan)]) == 0
    assert sent[-1]["payload"]["members"] == ["spec_demo__one"]
    assert "evidence" not in sent[-1]["payload"]


@pytest.mark.parametrize("completion,reported,stale", [(True, False, False), (None, None, True), (False, True, False)])
def test_r6_json_passthrough_and_unchanged_human_text(monkeypatch, capsys, completion, reported, stale):
    lane = {"lane_id": "wl-1", "version": 2, "state": "paused", "state_reason": "lead_lost",
            "owner_kind": "fd", "title": "T", "lead": None,
            "visible_chat": {"stream_id": "fixture:chat", "available": "open"}}
    _wire(monkeypatch, {"type": "work_lanes.list.ok", "lanes": [lane]})
    _run(["work-lane", "list"])
    prior = capsys.readouterr().out
    enriched = dict(lane, completion_pending=completion, lead_reported_done=reported, stale=stale)
    reply = {"type": "work_lanes.list.ok", "lanes": [enriched]}
    _wire(monkeypatch, reply)
    _run(["work-lane", "list"])
    assert capsys.readouterr().out == prior
    _run(["work-lane", "list", "--json"])
    assert json.loads(capsys.readouterr().out) == reply
    reply = {"type": "work_lanes.show.ok", "lane": {"lane_id": "wl-1"}, "projection": enriched,
             "events": [], "updates": []}
    _wire(monkeypatch, reply)
    _run(["work-lane", "show", "wl-1", "--members", "--json"])
    assert json.loads(capsys.readouterr().out) == reply


def test_preview_conflicts_retained_and_display_metadata_stripped(monkeypatch, tmp_path, capsys):
    conflicts = [{"spec_id": "spec_demo__bridge", "lane_id": "wl-existing"}]
    candidate = {"adoption_key": "request:preview", "members": ["spec_demo__bridge"], "member_conflicts": conflicts,
                 "member_sources": [{"epic_id": "epic_demo"}], "evidence": {"role": "lead"}}
    _wire(monkeypatch, {"type": "work_lanes.adopt_preview.ok", "candidates": [candidate]})
    assert _run(["work-lane", "adopt", "--preview"]) == 0
    assert json.loads(capsys.readouterr().out)["candidates"][0]["member_conflicts"] == conflicts
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps([candidate]))
    sent = _wire(monkeypatch, {"type": "assistant.operation.ok"})
    assert _run(["work-lane", "adopt", "--apply", str(plan)]) == 0
    assert sent[0]["payload"] == {"adoption_key": "request:preview", "members": ["spec_demo__bridge"]}


def test_offline_dispatch_forbids_config_store_socket_watcher_and_writes(tmp_path, monkeypatch, capsys):
    import builtins
    import hashlib
    import io
    import os
    import socket
    import sqlite3
    from _shared.specs_service import SpecsSubsystem
    from agent_orch import config, work_lane_cli
    root = tmp_path / "root"
    folder = root / "work" / "in_progress" / "demo__bridge"
    folder.mkdir(parents=True)
    (folder / "spec.md").write_text("---\nid: spec_demo__bridge\n---\n## Estimate\n- remaining_work_h: 2–4 (median 3) as_of 2026-10-09\n")
    (folder / "summary.md").write_text("Synthetic summary\n")
    before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    source = __import__('pathlib').Path(work_lane_cli.__file__)
    source_before = source.read_bytes()
    value = {"schema": "work_lane_estimate_manifest_v1", "lanes": [{"composite_stream_id": "example:assistant",
        "lane_id": "wl-test", "state": "paused", "members_total": 1, "members": ["spec_demo__bridge"]}]}
    def forbidden(*args, **kwargs):
        raise AssertionError("offline path attempted configuration, database, socket or watcher I/O")
    monkeypatch.setattr(cli, "load_config", forbidden)
    monkeypatch.setattr(config, "load_config", forbidden)
    monkeypatch.setattr(work_lane_cli, "_call", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(SpecsSubsystem, "start", forbidden)
    monkeypatch.setenv("PENTACLE_MEMORY_ROOT", str(tmp_path / "trap-environment-root"))
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(value)))
    real_open, real_os_open = io.open, os.open
    def readonly(file, mode="r", *args, **kwargs):
        assert not any(c in mode for c in "wax+")
        return real_open(file, mode, *args, **kwargs)
    def readonly_fd(file, flags, *args, **kwargs):
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        return real_os_open(file, flags, *args, **kwargs)
    with monkeypatch.context() as guards:
        guards.setattr(io, "open", readonly)
        guards.setattr(builtins, "open", readonly)
        guards.setattr(os, "open", readonly_fd)
        assert cli.main(["work-lane", "estimate-preview", "--memory-root", str(root), "--lanes-json", "-"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["errors"] == [] and result["lanes"][0]["open_estimate_h"] == {"p25": 2, "p75": 4, "median": 3}
    assert {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    assert source.read_bytes() == source_before


@pytest.mark.parametrize("args", [[], ["--lanes-json", "-"], ["--memory-root", "/synthetic"],
                                  ["--memory-root", "/synthetic", "--lanes-json", "manifest.json"]])
def test_offline_requires_explicit_root_and_literal_stdin(args):
    with pytest.raises(SystemExit) as result:
        _run(["work-lane", "estimate-preview", *args])
    assert result.value.code == 2


def test_lead_lost_fault_names_repair(monkeypatch, capsys):
    lane = {"lane_id": "wl-9", "version": 3, "state": "active", "state_reason": "lead_lost",
            "owner_kind": "fd", "title": "T", "lead": {"stream_id": "h:dead", "qualifies": False},
            "visible_chat": {"stream_id": "h:composite", "available": "open"}}
    _wire(monkeypatch, {"type": "work_lanes.list.ok", "lanes": [lane]})
    assert _run(["work-lane", "list"]) == 0
    out = capsys.readouterr().out
    assert "active/lead_lost — LEAD LOST — repair: set-lead wl-9" in out


def test_unattended_is_read_only_and_reports_quality(monkeypatch, capsys):
    work = {"label": "in_progress with no assigned implementation lead", "count": 1,
            "quality": "partial", "omitted_count": 2, "error": None,
            "items": [{"spec_id": "spec_demo__one", "repo": "demo", "age_s": None}]}
    sent = _wire(monkeypatch, {"type": "work.unattended.ok", "unattended_work": work})
    assert _run(["work", "unattended", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["unattended_work"] == work
    assert sent == [{"type": "work.unattended"}]
    assert _run(["work", "unattended"]) == 0
    text = capsys.readouterr().out
    assert "partial" in text and "omitted 2" in text and "age unknown" in text
    work.update(quality="unavailable", count=0, error="work_index_unavailable", items=[])
    assert _run(["work", "unattended"]) == 0
    assert "unavailable" in capsys.readouterr().out


@pytest.mark.parametrize("conflicts", [0, 1, 2])
def test_add_member_preserves_concurrent_members_and_bounds_retry(monkeypatch, capsys, conflicts):
    version, members, attempts = 3, ["spec_demo__first"], 0
    def reply(payload):
        nonlocal version, members, attempts
        if payload["type"] == "work_lanes.show":
            return {"type": "work_lanes.show.ok", "lane": {"version": version, "members": members.copy()}}
        attempts += 1
        assert payload["operation"] == "work_lane.set_members"
        assert payload["expected_lane_version"] == version
        if attempts <= conflicts:
            version += 1
            members.append(f"spec_demo__concurrent_{attempts}")
            return {"type": "assistant.operation.error", "error_code": "assistant_lane_version_conflict"}
        assert payload["payload"] == {"members": [*members, "spec_demo__new"]}
        version += 1
        members.append("spec_demo__new")
        return {"type": "assistant.operation.ok", "lane": {"version": version, "members": members}}
    sent = _wire(monkeypatch, reply)
    assert _run(["work-lane", "add-member", "wl-1", "spec_demo__new"]) == (1 if conflicts == 2 else 0)
    assert attempts == min(conflicts + 1, 2)
    writes = [m for m in sent if m["type"] == "assistant.operation"]
    assert all(m["request_id"] for m in writes)
    if len(writes) == 2:
        assert writes[0]["request_id"] != writes[1]["request_id"]
    assert json.loads(capsys.readouterr().out)["type"] == ("assistant.operation.error" if conflicts == 2 else "assistant.operation.ok")


@pytest.mark.parametrize("error", ["work_lane_members_unchanged", "work_lane_member_unresolved", "work_lane_actor_unverified"])
def test_add_member_preserves_other_refusals_without_retry(monkeypatch, capsys, error):
    def reply(payload):
        if payload["type"] == "work_lanes.show":
            return {"type": "work_lanes.show.ok", "lane": {"version": 7, "members": ["spec_demo__first"]}}
        return {"type": "assistant.operation.error", "error_code": error}
    sent = _wire(monkeypatch, reply)
    assert _run(["work-lane", "add-member", "wl-1", "spec_demo__new", "--request-id", "caller-id"]) == 1
    assert len(sent) == 2 and sent[-1]["request_id"] == "caller-id"
    assert json.loads(capsys.readouterr().out)["error_code"] == error


def test_add_member_repeat_uses_unique_members_and_stable_request(monkeypatch, capsys):
    def reply(payload):
        if payload["type"] == "work_lanes.show":
            return {"type": "work_lanes.show.ok", "lane": {"version": 8, "members": ["spec_demo__new"]}}
        assert payload["payload"]["members"] == ["spec_demo__new"]
        return {"type": "assistant.operation.error", "error_code": "work_lane_members_unchanged"}
    sent = _wire(monkeypatch, reply)
    for _ in range(2):
        assert _run(["work-lane", "add-member", "wl-1", "spec_demo__new"]) == 1
    assert sent[1]["request_id"] == sent[3]["request_id"]


def test_add_member_show_failure_never_writes(monkeypatch):
    sent = _wire(monkeypatch, {"type": "work_lanes.show.error", "error_code": "work_lane_not_found"})
    assert _run(["work-lane", "add-member", "wl-1", "spec_demo__new"]) == 1
    assert len(sent) == 1


def test_add_member_explicit_request_id_survives_conflict(monkeypatch):
    reads = 0
    def reply(payload):
        nonlocal reads
        if payload["type"] == "work_lanes.show":
            reads += 1
            return {"type": "work_lanes.show.ok", "lane": {"version": reads, "members": []}}
        assert payload["request_id"] == "caller-id"
        if reads == 1:
            return {"type": "assistant.operation.error", "error_code": "assistant_lane_version_conflict"}
        return {"type": "assistant.operation.ok"}
    sent = _wire(monkeypatch, reply)
    assert _run(["work-lane", "add-member", "wl-1", "spec_demo__new", "--request-id", "caller-id"]) == 0
    assert len(sent) == 4
