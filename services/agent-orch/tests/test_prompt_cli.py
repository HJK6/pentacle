from __future__ import annotations

import json

import pytest

from agent_orch import cli


def test_prompt_parser_accepts_ask_status_answer_cancel_and_list():
    parser = cli.build_parser()

    ask = parser.parse_args(
        [
            "prompt",
            "ask",
            "--title",
            "Choose",
            "--body",
            "Pick one",
            "--option",
            "Yes=yes",
        ]
    )
    ask_mode_alias = parser.parse_args(
        [
            "prompt",
            "ask",
            "--title",
            "Explain",
            "--body",
            "Why?",
            "--mode",
            "free_text",
        ]
    )
    status = parser.parse_args(["prompt", "status", "q-1"])
    answer = parser.parse_args(["prompt", "answer", "q-1", "--text", "typed answer"])
    cancel = parser.parse_args(["prompt", "cancel", "q-1", "--note", "moot"])
    listing = parser.parse_args(["prompt", "list", "--from", "hostb:codex-a", "--open"])

    assert ask.prompt_command == "ask"
    assert ask_mode_alias.response_mode == "free_text"
    assert status.question_id == "q-1"
    assert answer.text == "typed answer"
    assert cancel.question_id == "q-1"
    assert cancel.note == "moot"
    assert listing.from_stream_id == "hostb:codex-a"
    assert listing.open is True


def test_investigation_parser_accepts_decide_and_list():
    parser = cli.build_parser()

    decide = parser.parse_args(
        [
            "investigation",
            "decide",
            "inv-1",
            "--decision",
            "real_issue",
            "--proposed-fix",
            "patch it",
            "--affected-subsystem",
            "notification_spawn",
        ]
    )
    listing = parser.parse_args(["investigation", "list", "--status", "open"])
    drain = parser.parse_args(["investigation", "drain"])
    split = parser.parse_args(["investigation", "split", "inv-1", "--event-id", "event-2"])

    assert decide.investigation_command == "decide"
    assert decide.investigation_id == "inv-1"
    assert decide.decision == "real_issue"
    assert decide.affected_subsystem == "notification_spawn"
    assert listing.investigation_command == "list"
    assert listing.status == ["open"]
    assert drain.investigation_command == "drain"
    assert split.investigation_command == "split"
    assert split.event_id == "event-2"


def test_nexus_parser_and_read_commands(monkeypatch, capsys):
    parser = cli.build_parser()
    listing = parser.parse_args(["nexus", "list", "--all"])
    inspect = parser.parse_args(["nexus", "inspect", "example-repo"])
    context = parser.parse_args(["nexus", "context", "--since", "7"])
    assert listing.nexus_command == "list"
    assert listing.all is True
    assert inspect.identifier == "example-repo"

    monkeypatch.setattr(cli, "load_config", lambda: object())
    seen = []

    async def _nexus(_config, payload, *, timeout):
        seen.append((payload, timeout))
        return {"type": f"{payload['type']}.ok", "request_id": "n-1"}

    monkeypatch.setattr(cli, "nexus_once", _nexus)
    assert cli.nexus(listing) == 0
    assert cli.nexus(inspect) == 0
    assert cli.nexus(context) == 0
    output = capsys.readouterr().out.strip().splitlines()
    assert [json.loads(line)["type"] for line in output] == [
        "nexus.list.ok", "nexus.inspect.ok", "nexus.context.ok"
    ]
    assert seen[0][0] == {"type": "nexus.list", "all": True}
    assert seen[1][0]["identifier"] == "example-repo"
    assert seen[2][0]["since"] == 7


def test_nexus_mutation_and_auto_route_cli_are_not_exposed():
    parser = cli.build_parser()
    commands = [
        ["nexus", "declare", "child-domain", "--title", "Child", "--charter", "Own child decisions.", "--parent", "example-repo", "--audience", "scoped", "--binding", "repo:example", "--alias", "child", "--epoch", "4"],
        ["nexus", "register", "example-repo", "--mode", "watch"],
        ["nexus", "unregister", "example-repo"],
        ["nexus", "claim", "example-repo", "--override", "--confirmation-token", "tok"],
        ["nexus", "release", "example-repo", "--epoch", "4"],
        ["nexus", "route", "example-repo", "--kind", "decision", "--message", "Choose the safe path.", "--route-id", "route-fixed"],
        ["nexus", "route", "--auto", "--scope", "repo:example", "--kind", "decision", "--message", "Choose automatically."],
        ["nexus", "resolve", "example-repo", "--epoch", "4"],
        ["nexus", "archive", "example-repo", "--epoch", "4"],
    ]
    for argv in commands:
        if argv[1:2] == ["route"] and "--auto" not in argv:
            assert parser.parse_args(argv).nexus_command == "route"
            continue
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


def test_investigation_decide_publishes_to_daemon(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: object())
    seen = {}

    async def _investigation(_config, payload, *, timeout):
        seen["payload"] = payload
        seen["timeout"] = timeout
        return {
            "type": "investigation.decide.ok",
            "investigation": {
                "investigation_id": payload["investigation_id"],
                "decision_kind": payload["decision"]["decision"],
            },
        }

    monkeypatch.setattr(cli, "investigation_once", _investigation)
    args = cli.build_parser().parse_args(
        [
            "investigation",
            "decide",
            "inv-1",
            "--decision",
            "needs_more_information",
            "--question-id",
            "q-1",
            "--rationale",
            "need operator input",
            "--timeout",
            "5",
        ]
    )

    assert cli.investigation(args) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["type"] == "investigation.decide.ok"
    assert seen["payload"]["type"] == "investigation.decide"
    assert seen["payload"]["decision"]["question_id"] == "q-1"
    assert seen["timeout"] == 5.0

    args = cli.build_parser().parse_args(
        [
            "investigation",
            "decide",
            "inv-2",
            "--decision",
            "real_issue",
            "--proposed-fix",
            "patch it",
            "--affected-subsystem",
            "notification_spawn",
        ]
    )
    assert cli.investigation(args) == 0
    assert seen["payload"]["decision"]["affected_subsystem"] == "notification_spawn"


def test_investigation_split_publishes_to_daemon(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: object())
    seen = {}

    async def _investigation(_config, payload, *, timeout):
        seen["payload"] = payload
        return {"type": "investigation.split.ok", "result": {"event_id": payload["event_id"]}}

    monkeypatch.setattr(cli, "investigation_once", _investigation)
    args = cli.build_parser().parse_args(
        ["investigation", "split", "inv-1", "--event-id", "event-2"]
    )

    assert cli.investigation(args) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["type"] == "investigation.split.ok"
    assert seen["payload"]["type"] == "investigation.split"
    assert seen["payload"]["investigation_id"] == "inv-1"
    assert seen["payload"]["event_id"] == "event-2"


def test_investigation_drain_publishes_to_daemon(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: object())
    seen = {}

    async def _investigation(_config, payload, *, timeout):
        seen["payload"] = payload
        seen["timeout"] = timeout
        return {"type": "investigation.drain.ok", "results": []}

    monkeypatch.setattr(cli, "investigation_once", _investigation)
    args = cli.build_parser().parse_args(["investigation", "drain", "--timeout", "7"])

    assert cli.investigation(args) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["type"] == "investigation.drain.ok"
    assert seen["payload"]["type"] == "investigation.drain"
    assert seen["timeout"] == 7.0


def test_prompt_ask_prints_fallback_json_and_inline_block(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_ORCH_STREAM_ID", "hostb:codex-a")
    monkeypatch.setattr(cli, "load_config", lambda: object())

    async def _raise_unreachable(_config, _payload, *, timeout):
        raise OSError("offline")

    monkeypatch.setattr(cli, "prompt_ask_once", _raise_unreachable)
    args = cli.build_parser().parse_args(
        [
            "prompt",
            "ask",
            "--question-id",
            "q-1",
            "--title",
            "Choose",
            "--body",
            "Pick one",
            "--spec-id",
            "example__prompt_protocol",
            "--option",
            "Proceed=proceed",
            "--provider",
            "codex",
        ]
    )

    assert cli.prompt_ask(args) == 0
    captured = capsys.readouterr()
    response = json.loads(captured.out)

    assert response["type"] == "prompt.fallback"
    assert response["error_code"] == "daemon_unreachable"
    assert response["envelope"]["producer_stream_id"] == "hostb:codex-a"
    assert response["envelope"]["producer_provider"] == "codex"
    assert "AGENT_QUESTION_V1" in captured.err
    assert "inline fallback only" in captured.err


def test_prompt_ask_publishes_to_daemon(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_ORCH_STREAM_ID", "hostb:codex-a")
    monkeypatch.setattr(cli, "load_config", lambda: object())
    seen = {}

    async def _ask(_config, payload, *, timeout):
        seen["payload"] = payload
        seen["timeout"] = timeout
        return {
            "type": "prompt.ask.ok",
            "ok": True,
            "question": {
                "question_id": payload["envelope"]["question_id"],
                "state": "open",
                "notification_id": "n-1",
            },
        }

    monkeypatch.setattr(cli, "prompt_ask_once", _ask)
    args = cli.build_parser().parse_args(
        [
            "prompt",
            "ask",
            "--question-id",
            "q-1",
            "--title",
            "Choose",
            "--body",
            "Pick one",
            "--option",
            "Proceed=proceed",
            "--timeout",
            "5",
        ]
    )

    assert cli.prompt_ask(args) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["type"] == "prompt.ask.ok"
    assert seen["payload"]["type"] == "prompt.ask"
    assert seen["payload"]["actions"][0]["value"]["answer"] == "proceed"
    assert seen["timeout"] == 5.0


def test_prompt_ask_loudly_surfaces_undelivered_live_parent_notice(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_ORCH_STREAM_ID", "hostb:codex-a")
    monkeypatch.setattr(cli, "load_config", lambda: object())

    async def _ask(_config, payload, *, timeout):
        return {
            "type": "prompt.ask.ok",
            "ok": True,
            "question": {
                "question_id": payload["envelope"]["question_id"],
                "state": "open",
                "notification_id": "n-1",
            },
            "notice_delivery": {
                "delivery_status": "failed",
                "error_code": "delivery_not_submitted",
                "to_stream_id": "hostb:codex-lead",
                "tell_id": "prompt-question-ready-q-1-abc",
            },
        }

    monkeypatch.setattr(cli, "prompt_ask_once", _ask)
    args = cli.build_parser().parse_args(
        [
            "prompt", "ask", "--question-id", "q-1", "--title", "Choose",
            "--body", "Pick one", "--option", "Proceed=proceed",
        ]
    )

    assert cli.prompt_ask(args) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["notice_delivery"]["delivery_status"] == "failed"
    assert "live-session notice not delivered" in captured.err
    assert "hostb:codex-lead" in captured.err
    assert "prompt-question-ready-q-1-abc" in captured.err


def _committed_notice(**overrides):
    notice = {
        "delivery_status": "committed_pending_proof",
        "do_not_resubmit": True,
        "action_committed": True,
        "to_stream_id": "hostb:codex-lead",
        "tell_id": "prompt-question-ready-q-1-abc",
        "ledger_row_id": 4242,
    }
    notice.update(overrides)
    return notice


def test_prompt_ask_committed_pending_proof_live_notice_is_authoritative(monkeypatch, capsys):
    """A durably-committed live-session notice whose USER-event proof is late
    is delivered-enough: the card exists and the paste committed, so prompt ask
    returns 0 and reconciles asynchronously instead of hard-failing exit 1."""
    monkeypatch.setenv("AGENT_ORCH_STREAM_ID", "hostb:codex-a")
    monkeypatch.setattr(cli, "load_config", lambda: object())

    async def _ask(_config, payload, *, timeout):
        return {
            "type": "prompt.ask.ok",
            "ok": True,
            "question": {
                "question_id": payload["envelope"]["question_id"],
                "state": "open",
                "notification_id": "n-1",
            },
            "to_stream_id": "hostb:codex-lead",
            "notice_delivery": _committed_notice(),
        }

    monkeypatch.setattr(cli, "prompt_ask_once", _ask)
    args = cli.build_parser().parse_args(
        [
            "prompt", "ask", "--question-id", "q-1", "--title", "Choose",
            "--body", "Pick one", "--option", "Proceed=proceed",
        ]
    )

    assert cli.prompt_ask(args) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["type"] == "prompt.ask.ok"
    assert "live-session notice not delivered" not in captured.err


def test_prompt_ask_committed_pending_proof_without_commitment_is_not_authoritative(
    monkeypatch, capsys,
):
    """committed_pending_proof status alone is not enough — without the
    do_not_resubmit commitment the notice is still treated as undelivered."""
    monkeypatch.setenv("AGENT_ORCH_STREAM_ID", "hostb:codex-a")
    monkeypatch.setattr(cli, "load_config", lambda: object())

    async def _ask(_config, payload, *, timeout):
        return {
            "type": "prompt.ask.ok",
            "ok": True,
            "question": {
                "question_id": payload["envelope"]["question_id"],
                "state": "open",
                "notification_id": "n-1",
            },
            "to_stream_id": "hostb:codex-lead",
            "notice_delivery": _committed_notice(do_not_resubmit=False),
        }

    monkeypatch.setattr(cli, "prompt_ask_once", _ask)
    args = cli.build_parser().parse_args(
        [
            "prompt", "ask", "--question-id", "q-1", "--title", "Choose",
            "--body", "Pick one", "--option", "Proceed=proceed",
        ]
    )

    assert cli.prompt_ask(args) == 1
    assert "live-session notice not delivered" in capsys.readouterr().err


def test_prompt_ask_preserves_mixed_option_order_and_allow_custom(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: object())
    seen = {}

    async def _ask(_config, payload, *, timeout):
        seen["payload"] = payload
        return {
            "type": "prompt.ask.ok",
            "ok": True,
            "question": {
                "question_id": payload["envelope"]["question_id"],
                "state": "open",
                "notification_id": "n-1",
            },
        }

    monkeypatch.setattr(cli, "prompt_ask_once", _ask)
    args = cli.build_parser().parse_args(
        [
            "prompt",
            "ask",
            "--question-id",
            "q-options",
            "--title",
            "Choose",
            "--body",
            "Pick one",
            "--option",
            "A=a",
            "--option-json",
            '{"label":"B","value":"b","description":"Second option"}',
            "--option",
            "C=c",
            "--allow-custom",
        ]
    )

    assert cli.prompt_ask(args) == 0
    assert json.loads(capsys.readouterr().out)["type"] == "prompt.ask.ok"
    assert seen["payload"]["envelope"]["options"] == [
        {"label": "A", "value": "a"},
        {"label": "B", "value": "b", "description": "Second option"},
        {"label": "C", "value": "c"},
    ]
    assert seen["payload"]["envelope"]["allow_custom"] is True
    assert "description" not in seen["payload"]["actions"][1]


def test_prompt_ask_surfaces_server_validation_error(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: object())

    async def _reject(_config, _payload, *, timeout):
        return {
            "type": "prompt.error",
            "ok": False,
            "error_code": "prompt_invalid",
            "message": "dedup_key already belongs to open question_id: q-1",
        }

    monkeypatch.setattr(cli, "prompt_ask_once", _reject)
    args = cli.build_parser().parse_args(
        [
            "prompt",
            "ask",
            "--question-id",
            "q-2",
            "--title",
            "Choose",
            "--body",
            "Pick one",
            "--option",
            "Proceed=proceed",
        ]
    )

    assert cli.prompt_ask(args) == 2
    captured = capsys.readouterr()
    response = json.loads(captured.out)
    assert response["type"] == "prompt.error"
    assert response["error_code"] == "prompt_invalid"
    assert "AGENT_QUESTION_V1" not in captured.err


def test_prompt_ask_await_answer_reads_back_status(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: object())
    calls = []

    async def _ask(_config, payload, *, timeout):
        calls.append(("ask", payload["envelope"]["question_id"], timeout))
        return {
            "type": "prompt.ask.ok",
            "ok": True,
            "question": {
                "question_id": payload["envelope"]["question_id"],
                "state": "open",
                "notification_id": "n-1",
            },
        }

    async def _await(_config, notification_id, *, timeout):
        calls.append(("await", notification_id, timeout))
        return {
            "type": "notification.await.ok",
            "answer": {"notification_id": notification_id, "value": {"answer": "proceed"}},
        }

    async def _status(_config, question_id, *, timeout):
        calls.append(("status", question_id, timeout))
        return {
            "type": "prompt.status.ok",
            "ok": True,
            "question": {
                "question_id": question_id,
                "state": "answered",
                "answer": {"value": {"answer": "proceed"}},
            },
        }

    monkeypatch.setattr(cli, "prompt_ask_once", _ask)
    monkeypatch.setattr(cli, "notification_await_once", _await)
    monkeypatch.setattr(cli, "prompt_status_once", _status)
    args = cli.build_parser().parse_args(
        [
            "prompt",
            "ask",
            "--question-id",
            "q-1",
            "--title",
            "Choose",
            "--body",
            "Pick one",
            "--option",
            "Proceed=proceed",
            "--await-answer",
            "--timeout",
            "7",
        ]
    )

    assert cli.prompt_ask(args) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["type"] == "prompt.status.ok"
    assert response["question"]["answer"]["value"]["answer"] == "proceed"
    assert calls == [("ask", "q-1", 7.0), ("await", "n-1", 7.0), ("status", "q-1", 7.0)]


def test_prompt_ask_accepts_free_text(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: object())
    seen = {}

    async def _ask(_config, payload, *, timeout):
        seen["payload"] = payload
        return {
            "type": "prompt.ask.ok",
            "ok": True,
            "question": {
                "question_id": payload["envelope"]["question_id"],
                "state": "open",
                "notification_id": "n-1",
            },
        }

    monkeypatch.setattr(cli, "prompt_ask_once", _ask)
    args = cli.build_parser().parse_args(
        [
            "prompt",
            "ask",
            "--title",
            "Explain",
            "--body",
            "Why?",
            "--response-mode",
            "free_text",
        ]
    )

    assert cli.prompt_ask(args) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["type"] == "prompt.ask.ok"
    assert seen["payload"]["envelope"]["response_mode"] == "free_text"
    assert seen["payload"]["envelope"]["options"] == []
    assert seen["payload"]["actions"] == []


def test_prompt_answer_posts_text(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: object())
    seen = []

    async def _answer(_config, payload, *, timeout):
        seen.append({"payload": payload, "timeout": timeout})
        answer = {}
        if "text" in payload:
            answer["text"] = payload["text"]
        if "value" in payload:
            answer["value"] = payload["value"]
        if "selections" in payload:
            answer["selections"] = payload["selections"]
        return {
            "type": "prompt.answer.ok",
            "ok": True,
            "question": {
                "question_id": payload["question_id"],
                "state": "answered",
                "answer": answer,
            },
        }

    monkeypatch.setattr(cli, "prompt_answer_once", _answer)
    parser = cli.build_parser()

    cases = [
        (
            [
                "prompt",
                "answer",
                "q-1",
                "--text",
                "first line\nsecond line",
                "--by",
                "tester",
                "--timeout",
                "9",
            ],
            {
                "type": "prompt.answer",
                "question_id": "q-1",
                "text": "first line\nsecond line",
                "by": "tester",
            },
        ),
        (
            ["prompt", "answer", "q-single", "--select", "yes"],
            {
                "type": "prompt.answer",
                "question_id": "q-single",
                "selections": ["yes"],
            },
        ),
        (
            [
                "prompt",
                "answer",
                "q-multi",
                "--selection",
                "alpha",
                "--selection",
                "beta",
            ],
            {
                "type": "prompt.answer",
                "question_id": "q-multi",
                "selections": ["alpha", "beta"],
            },
        ),
        (
            ["prompt", "answer", "q-both", "--select", "yes", "--text", "with a caveat"],
            {
                "type": "prompt.answer",
                "question_id": "q-both",
                "selections": ["yes"],
                "text": "with a caveat",
            },
        ),
    ]

    for argv, expected_payload in cases:
        args = parser.parse_args(argv)
        assert cli.prompt_answer(args) == 0
        response = json.loads(capsys.readouterr().out)
        assert response["question"]["state"] == "answered"
        assert seen[-1]["payload"] == expected_payload

    assert seen[0]["timeout"] == 9.0


def test_prompt_answer_requires_a_selection_or_text(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: object())

    async def _answer(_config, payload, *, timeout):  # pragma: no cover - must not run
        raise AssertionError("empty answer must be rejected before the wire")

    monkeypatch.setattr(cli, "prompt_answer_once", _answer)
    parser = cli.build_parser()
    # No --select and no --text (and blank text) is a client-side error, exit 2,
    # and never reaches the daemon.
    assert cli.prompt_answer(parser.parse_args(["prompt", "answer", "q"])) == 2
    assert cli.prompt_answer(parser.parse_args(["prompt", "answer", "q", "--text", "   "])) == 2


def test_prompt_cancel_posts_cancel(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: object())
    seen = []

    async def _cancel(_config, payload, *, timeout):
        seen.append({"payload": payload, "timeout": timeout})
        return {
            "type": "prompt.cancel.ok",
            "ok": True,
            "question": {
                "question_id": payload["question_id"],
                "state": "dismissed",
                "answer": {"action_kind": "resolved", "note": payload.get("note")},
            },
        }

    monkeypatch.setattr(cli, "prompt_cancel_once", _cancel)
    args = cli.build_parser().parse_args(
        [
            "prompt",
            "cancel",
            "q-1",
            "--note",
            "not needed",
            "--by",
            "tester",
            "--timeout",
            "9",
        ]
    )

    assert cli.prompt_cancel(args) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["question"]["state"] == "dismissed"
    assert seen == [
        {
            "payload": {
                "type": "prompt.cancel",
                "question_id": "q-1",
                "by": "tester",
                "note": "not needed",
            },
            "timeout": 9.0,
        }
    ]


def test_prompt_status_and_list_query_daemon(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: object())

    async def _status(_config, question_id, *, timeout):
        return {
            "type": "prompt.status.ok",
            "ok": True,
            "question": {"question_id": question_id, "state": "answered"},
        }

    async def _list(_config, payload, *, timeout):
        return {
            "type": "prompt.list.ok",
            "ok": True,
            "questions": [{"question_id": "q-1", "state": "open"}],
            "payload": payload,
        }

    monkeypatch.setattr(cli, "prompt_status_once", _status)
    monkeypatch.setattr(cli, "prompt_list_once", _list)
    status_args = cli.build_parser().parse_args(["prompt", "status", "q-1"])
    list_args = cli.build_parser().parse_args(
        ["prompt", "list", "--spec-id", "example__prompt_protocol", "--open"]
    )

    assert cli.prompt_status(status_args) == 0
    status_response = json.loads(capsys.readouterr().out)
    assert status_response["type"] == "prompt.status.ok"
    assert status_response["question"]["question_id"] == "q-1"

    assert cli.prompt_list(list_args) == 0
    list_response = json.loads(capsys.readouterr().out)
    assert list_response["type"] == "prompt.list.ok"
    assert list_response["payload"]["spec_id"] == "example__prompt_protocol"
    assert list_response["payload"]["open"] is True


def test_prompt_ask_loudly_surfaces_missing_authoritative_notice(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_ORCH_STREAM_ID", "hostb:codex-a")
    monkeypatch.setattr(cli, "load_config", lambda: object())

    async def _ask(_config, payload, *, timeout):
        return {
            "type": "prompt.ask.ok",
            "ok": True,
            "question": {
                "question_id": payload["envelope"]["question_id"],
                "state": "open",
                "notification_id": "n-1",
            },
            "to_stream_id": "hostb:codex-lead",
        }

    monkeypatch.setattr(cli, "prompt_ask_once", _ask)
    args = cli.build_parser().parse_args(
        [
            "prompt", "ask", "--question-id", "q-missing", "--title", "Choose",
            "--body", "Pick one", "--option", "Proceed=proceed",
        ]
    )

    assert cli.prompt_ask(args) == 1
    captured = capsys.readouterr()
    assert "live-session notice not delivered" in captured.err
    assert "hostb:codex-lead" in captured.err
    assert "correlation unknown" in captured.err
