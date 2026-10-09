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
