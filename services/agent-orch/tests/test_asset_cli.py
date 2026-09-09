from __future__ import annotations

import json
from argparse import Namespace

import pytest

from agent_orch import cli
from agent_orch.config import Config
from _shared.asset_schema import validate_asset_payload as shared_validate_asset_payload


def _args(tmp_path, **overrides):
    content_file = overrides.get("content_file") or _report_file(tmp_path)
    data = {
        "title": "Stage notes",
        "content_type": "report",
        "content_file": str(content_file),
        "tags": [],
        "session": None,
        "asset_id": None,
        "asset_command": "publish",
        "asset_id_arg": None,
        "asset_extra_args": [],
        "timeout": 1.0,
        "spec_id": None,
        "unresolved": False,
        "note": None,
    }
    data.update(overrides)
    return Namespace(**data)


def _report_payload() -> dict:
    return {
        "schema_version": 1,
        "title": "Review packet",
        "sections": [
            {
                "id": "sec-summary",
                "title": "Summary",
                "status": "in_progress",
                "blocks": [
                    {
                        "id": "blk-summary",
                        "type": "para",
                        "runs": ["Lead owns the CLI surface."],
                    }
                ],
            }
        ],
    }


def _report_file(tmp_path, payload: dict | None = None):
    path = tmp_path / "report.json"
    path.write_text(json.dumps(payload if payload is not None else _report_payload()), encoding="utf-8")
    return path


def test_asset_parser_accepts_required_flags(tmp_path):
    content_file = _report_file(tmp_path)
    parser = cli.build_parser()

    parsed = parser.parse_args(
        [
            "asset",
            "--title",
            "Stage notes",
            "--type",
            "report",
            "--content-file",
            str(content_file),
            "--tag",
            "stage-b",
            "--session",
            "hostb:codex-a",
            "--asset-id",
            "notes",
        ]
    )

    assert parsed.func is cli.asset
    assert parsed.asset_command == "publish"
    assert parsed.content_type == "report"
    assert parsed.tags == ["stage-b"]
    assert parsed.session == "hostb:codex-a"
    assert parsed.asset_id == "notes"


def test_asset_parser_accepts_list_get_and_comments_actions():
    parser = cli.build_parser()

    listed = parser.parse_args(["asset", "list", "--spec-id", "example__demo"])
    got = parser.parse_args(["asset", "get", "notes", "--session", "hostb:codex-a"])
    comments = parser.parse_args(["asset", "comments", "notes", "--unresolved"])
    resolved = parser.parse_args(["asset", "comments", "resolve", "notes", "c-1", "--note", "done"])

    assert listed.func is cli.asset
    assert listed.asset_command == "list"
    assert listed.spec_id == "example__demo"
    assert got.asset_command == "get"
    assert got.asset_id_arg == "notes"
    assert comments.asset_command == "comments"
    assert comments.asset_id_arg == "notes"
    assert comments.unresolved is True
    assert resolved.asset_command == "comments"
    assert resolved.asset_id_arg == "resolve"
    assert resolved.asset_extra_args == ["notes", "c-1"]
    assert resolved.note == "done"


def test_asset_parser_defers_bad_type_to_stable_validation(tmp_path):
    content_file = tmp_path / "asset.md"
    content_file.write_text("# Notes\n", encoding="utf-8")
    parser = cli.build_parser()

    parsed = parser.parse_args(
        [
            "asset",
            "--title",
            "Stage notes",
            "--type",
            "html",
            "--content-file",
            str(content_file),
        ]
    )

    assert parsed.content_type == "html"


def test_asset_report_payload_defaults_to_caller_session(tmp_path):
    payload = cli._asset_publish_payload_from_args(
        _args(tmp_path, tags=["stage-b"]),
        caller_stream_id="hostb:codex-a",
    )

    assert payload == {
        "type": "asset.publish",
        "stream_id": "hostb:codex-a",
        "from_stream_id": "hostb:codex-a",
        "producer": "hostb:codex-a",
        "title": "Stage notes",
        "content_type": "report",
        "body": json.dumps(_report_payload(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        "tags": ["stage-b"],
    }


def test_asset_report_payload_uses_shared_validator_and_spec_id(tmp_path):
    content_file = _report_file(tmp_path)

    payload = cli._asset_publish_payload_from_args(
        _args(
            tmp_path,
            content_type="report",
            content_file=str(content_file),
            spec_id="example__demo",
        ),
        caller_stream_id="hostb:codex-a",
    )

    assert cli.validate_asset_payload is shared_validate_asset_payload
    assert payload["content_type"] == "report"
    assert payload["spec_id"] == "example__demo"
    assert json.loads(str(payload["body"])) == _report_payload()


@pytest.mark.parametrize("content_type", ["markdown", "json_table", "html"])
def test_asset_payload_rejects_non_report_content_type(tmp_path, content_type):
    try:
        cli._asset_publish_payload_from_args(
            _args(tmp_path, content_type=content_type),
            caller_stream_id="hostb:codex-a",
        )
    except ValueError as exc:
        assert "content_type must be one of" in str(exc)
    else:
        raise AssertionError("expected validation error")


@pytest.mark.parametrize(
    ("mutator", "path_fragment"),
    [
        (lambda payload: payload["sections"][0].pop("id"), "$.sections[0].id"),
        (
            lambda payload: payload["sections"][0]["blocks"][0].update({"id": "sec-summary"}),
            "$.sections[0].blocks[0].id",
        ),
        (lambda payload: payload.update({"schema_version": 2}), "$.schema_version"),
        (lambda payload: payload.update({"surprise": True}), "$"),
    ],
)
def test_asset_report_payload_prevalidation_errors_include_paths(tmp_path, mutator, path_fragment):
    payload = _report_payload()
    mutator(payload)
    content_file = _report_file(tmp_path, payload)

    with pytest.raises(ValueError) as excinfo:
        cli._asset_publish_payload_from_args(
            _args(tmp_path, content_type="report", content_file=str(content_file)),
            caller_stream_id="hostb:codex-a",
        )

    assert path_fragment in str(excinfo.value)


def test_asset_payload_rejects_over_cap(tmp_path, monkeypatch):
    content_file = tmp_path / "large.md"
    content_file.write_text("12345", encoding="utf-8")
    monkeypatch.setenv("PENTACLE_ASSET_BODY_MAX_BYTES", "4")

    try:
        cli._asset_publish_payload_from_args(
            _args(tmp_path, content_file=str(content_file)),
            caller_stream_id="hostb:codex-a",
        )
    except ValueError as exc:
        assert "asset body exceeds" in str(exc)
    else:
        raise AssertionError("expected validation error")


@pytest.mark.parametrize("content_type", ["markdown", "json_table", "html"])
def test_asset_cli_invalid_content_type_surfaces_stable_code(monkeypatch, tmp_path, capsys, content_type):
    async def fail_publish(*_args, **_kwargs):
        raise AssertionError("validation should happen before publish")

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-a")
    monkeypatch.setattr(cli, "asset_publish_once", fail_publish)

    assert cli.asset(_args(tmp_path, content_type=content_type)) == 2

    captured = capsys.readouterr()
    assert json.loads(captured.out)["error"] == "asset_invalid"
    assert "asset_invalid" in captured.err


def test_asset_cli_over_cap_surfaces_stable_code(monkeypatch, tmp_path, capsys):
    content_file = tmp_path / "large.md"
    content_file.write_text("12345", encoding="utf-8")

    async def fail_publish(*_args, **_kwargs):
        raise AssertionError("validation should happen before publish")

    monkeypatch.setenv("PENTACLE_ASSET_BODY_MAX_BYTES", "4")
    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-a")
    monkeypatch.setattr(cli, "asset_publish_once", fail_publish)

    assert cli.asset(_args(tmp_path, content_file=str(content_file))) == 2

    captured = capsys.readouterr()
    assert json.loads(captured.out)["error"] == "asset_body_too_large"
    assert "asset_body_too_large" in captured.err


def test_asset_cli_publishes_default_session(monkeypatch, tmp_path, capsys):
    calls = []

    async def fake_publish(config, payload, timeout):
        calls.append((config, payload, timeout))
        return {
            "type": "asset.publish.ok",
            "asset": {"asset_id": "asset-1", "title": payload["title"]},
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-a")
    monkeypatch.setattr(cli, "asset_publish_once", fake_publish)

    assert cli.asset(_args(tmp_path, asset_id="asset-1")) == 0

    assert calls[0][1]["stream_id"] == "hostb:codex-a"
    assert calls[0][1]["asset_id"] == "asset-1"
    assert calls[0][2] == 1.0
    assert json.loads(capsys.readouterr().out)["type"] == "asset.publish.ok"


def test_asset_cli_publishes_explicit_session(monkeypatch, tmp_path):
    calls = []

    async def fake_publish(config, payload, timeout):
        calls.append(payload)
        return {"type": "asset.publish.ok", "asset": {"asset_id": "asset-1"}}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-caller")
    monkeypatch.setattr(cli, "asset_publish_once", fake_publish)

    assert cli.asset(_args(tmp_path, session="hostb:codex-target")) == 0

    assert calls[0]["stream_id"] == "hostb:codex-target"
    assert calls[0]["from_stream_id"] == "hostb:codex-target"
    assert calls[0]["producer"] == "hostb:codex-caller"


def test_asset_cli_publishes_report_with_spec_id(monkeypatch, tmp_path):
    calls = []
    content_file = _report_file(tmp_path)

    async def fake_publish(config, payload, timeout):
        calls.append(payload)
        return {"type": "asset.publish.ok", "asset": {"asset_id": "asset-1"}}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-caller")
    monkeypatch.setattr(cli, "asset_publish_once", fake_publish)

    assert cli.asset(
        _args(
            tmp_path,
            content_type="report",
            content_file=str(content_file),
            spec_id="example__demo",
        )
    ) == 0

    assert calls[0]["content_type"] == "report"
    assert calls[0]["spec_id"] == "example__demo"


def test_asset_cli_lists_explicit_session(monkeypatch, tmp_path, capsys):
    calls = []

    async def fake_list(config, payload, timeout):
        calls.append((payload, timeout))
        return {"type": "asset.list.ok", "assets": [{"asset_id": "notes"}]}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-caller")
    monkeypatch.setattr(cli, "asset_list_once", fake_list)

    assert cli.asset(_args(tmp_path, asset_command="list", session="hostb:codex-target")) == 0

    assert calls == [({"type": "asset.list", "stream_id": "hostb:codex-target", "from_stream_id": "hostb:codex-caller"}, 1.0)]
    assert json.loads(capsys.readouterr().out)["type"] == "asset.list.ok"


def test_asset_cli_lists_spec_id_without_session(monkeypatch, tmp_path, capsys):
    calls = []

    async def fake_list(config, payload, timeout):
        calls.append((payload, timeout))
        return {
            "type": "asset.list.ok",
            "assets": [
                {
                    "asset_id": "report-1",
                    "spec_id": "example__demo",
                    "stream_id": "hostb:codex-producer",
                    "producer": "hostb:codex-producer",
                }
            ],
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-caller")
    monkeypatch.setattr(cli, "asset_list_once", fake_list)

    assert cli.asset(_args(tmp_path, asset_command="list", spec_id="example__demo")) == 0

    assert calls == [({"spec_id": "example__demo", "stream_id": "hostb:codex-caller", "from_stream_id": "hostb:codex-caller", "type": "asset.list"}, 1.0)]
    assert json.loads(capsys.readouterr().out)["assets"][0]["asset_id"] == "report-1"


def test_asset_cli_gets_asset_by_positional_id(monkeypatch, tmp_path, capsys):
    calls = []

    async def fake_get(config, payload, timeout):
        calls.append(payload)
        return {"type": "asset.get.ok", "asset": {"asset_id": "notes", "body": "# Notes"}}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-caller")
    monkeypatch.setattr(cli, "asset_get_once", fake_get)

    assert cli.asset(_args(tmp_path, asset_command="get", asset_id_arg="notes", session="hostb:codex-target")) == 0

    assert calls == [{"type": "asset.get", "stream_id": "hostb:codex-target", "from_stream_id": "hostb:codex-caller", "asset_id": "notes"}]
    assert json.loads(capsys.readouterr().out)["asset"]["body"] == "# Notes"


def test_asset_cli_health_prints_store_identity(monkeypatch, tmp_path, capsys):
    async def fake_health(_config, payload, timeout):
        assert payload == {"type": "asset.health"}
        assert timeout == 1.0
        return {"type": "asset.health.ok", "request_id": "health-1", "store": {"path": "/tmp/assets.db", "inode": 7, "schema_version": 1}}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-caller")
    monkeypatch.setattr(cli, "asset_health_once", fake_health)

    assert cli.asset(_args(tmp_path, asset_command="health")) == 0
    assert json.loads(capsys.readouterr().out)["store"]["inode"] == 7


def test_asset_cli_lists_comments_compactly(monkeypatch, tmp_path, capsys):
    calls = []

    async def fake_comments(config, payload, timeout):
        calls.append((payload, timeout))
        return {
            "type": "asset.comments.list.ok",
            "comments": [
                {
                    "comment_id": "c-1",
                    "section_id": "sec-summary",
                    "block_id": "blk-summary",
                    "run_index": None,
                    "excerpt": "Lead owns",
                    "body": "Tighten this.",
                    "author": "user@example.com",
                    "created_at": "2026-07-05T00:00:00Z",
                    "resolved": 0,
                    "resolution_note": "Addressed in v2.",
                    "updated_at": "ignored",
                }
            ],
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-caller")
    monkeypatch.setattr(cli, "asset_comments_list_once", fake_comments)

    assert cli.asset(
        _args(tmp_path, asset_command="comments", asset_id_arg="report-1", unresolved=True)
    ) == 0

    assert calls == [
        (
            {
                    "stream_id": "hostb:codex-caller",
                    "from_stream_id": "hostb:codex-caller",
                "type": "asset.comments.list",
                "asset_id": "report-1",
                "unresolved": True,
            },
            1.0,
        )
    ]
    assert json.loads(capsys.readouterr().out) == [
        {
            "comment_id": "c-1",
            "section_id": "sec-summary",
            "block_id": "blk-summary",
            "run_index": None,
            "excerpt": "Lead owns",
            "body": "Tighten this.",
            "author": "user@example.com",
            "created_at": "2026-07-05T00:00:00Z",
            "resolved": False,
            "resolution_note": "Addressed in v2.",
        }
    ]


def test_asset_cli_resolves_comment(monkeypatch, tmp_path, capsys):
    calls = []

    async def fake_resolve(config, payload, timeout):
        calls.append((payload, timeout))
        return {"type": "asset.comment.resolve.ok", "comment": {"comment_id": "c-1", "resolved": True}}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-caller")
    monkeypatch.setattr(cli, "asset_comment_resolve_once", fake_resolve)

    assert cli.asset(
        _args(
            tmp_path,
            asset_command="comments",
            asset_id_arg="resolve",
            asset_extra_args=["report-1", "c-1"],
            spec_id="example__demo",
            note="addressed",
        )
    ) == 0

    assert calls == [
        (
            {
                "stream_id": "hostb:codex-caller",
                "spec_id": "example__demo",
                "type": "asset.comment.resolve",
                "asset_id": "report-1",
                "comment_id": "c-1",
                "resolved": True,
                "from_stream_id": "hostb:codex-caller",
                "resolved_by": "hostb:codex-caller",
                "note": "addressed",
            },
            1.0,
        )
    ]
    assert json.loads(capsys.readouterr().out)["type"] == "asset.comment.resolve.ok"

