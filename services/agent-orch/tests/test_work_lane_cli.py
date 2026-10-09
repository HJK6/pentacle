"""`agent-orch work-lane` wire shapes (spec D7 CLI)."""

from __future__ import annotations

import json

import pytest

from agent_orch import cli


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
            "visible_chat": {"stream_id": "bart:assistant", "available": "open"}}
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
        "type": "assistant.operation", "request_id": "r1", "composite_stream_id": "bart:assistant",
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
    assert sent[-1] == {"type": "work_lanes.adopt_preview", "composite_stream_id": "bart:assistant"}
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
