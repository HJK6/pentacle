from __future__ import annotations

from argparse import Namespace
import json
import pytest

from agent_orch import cli, stream_id
from agent_orch.config import Config
from _shared.report_payload_v1 import SchemaError, validate as validate_report_payload


def _args(**overrides):
    data = {
        "result": json.dumps({"summary": "ok", "findings": [], "next_action": "leader_proceed"}),
        "result_file": None,
        "msg_id": 7,
        "status": "done",
        "reason": None,
        "report_id": "report-1",
        "timeout": 1.0,
        "debug": False,
        "qa_verdict": None,
        "target_sha": None,
        "qa_attestation_stream_id": None,
        "qa_attestation_report_id": None,
    }
    data.update(overrides)
    return Namespace(**data)


def test_durable_notice_authority_does_not_depend_on_advisory_ack_fields() -> None:
    receipt = {
        "delivery_status": "delivered",
        "tell_id": "child-report-ready-v2-proof",
        "ledger_row_id": 18075,
        "to_stream_id": "hosta:parent",
        "delivery_ack_at": None,
        "submission_confirmed": False,
    }
    assert cli._notice_delivery_is_authoritative(
        receipt,
        to_stream_id="hosta:parent",
    ) is True


def test_report_parser_rejects_result_and_result_file_together():
    parser = cli.build_parser()
    try:
        parser.parse_args(
            [
                "report",
                "--msg-id",
                "1",
                "--status",
                "done",
                "--result",
                "{}",
                "--result-file",
                "/tmp/result.json",
            ]
        )
    except SystemExit as exc:
        assert exc.code == 2
    else:  # pragma: no cover
        raise AssertionError("argparse accepted mutually exclusive flags")


def test_report_requires_reason_for_error_before_network(capsys):
    assert cli.report(_args(status="error", result="{}")) == 2
    assert "missing_reason" in capsys.readouterr().err


def test_report_inline_success_uses_wrapperless_websocket(monkeypatch, capsys, tmp_path):
    calls = {}

    async def fake_report_once(config, request, timeout):
        calls["config"] = config
        calls["request"] = request
        calls["timeout"] = timeout
        return {"type": "report.ok", "request_id": "report-1", "report_id": "report-1", "ledger_row_id": 5, "ingested": False}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-x")
    monkeypatch.setattr(cli, "report_once", fake_report_once)

    assert cli.report(_args()) == 0
    assert calls["request"]["from_stream_id"] == "hostb:codex-x"
    assert calls["request"]["msg_id"] == 7
    assert calls["request"]["summary"] == "ok"
    assert json.loads(capsys.readouterr().out)["type"] == "report.ok"


def test_nonterminating_report_loudly_surfaces_undelivered_parent_notice(monkeypatch, capsys, tmp_path):
    async def fake_report_once(config, request, timeout):
        return {
            "type": "report.ok",
            "request_id": "report-1",
            "report_id": "report-1",
            "ledger_row_id": 5,
            "durability_ack": True,
            "notice_delivery": {
                "delivery_status": "queued",
                "to_stream_id": "hostb:codex-lead",
                "tell_id": "child-report-ready-report-1",
            },
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-x")
    monkeypatch.setattr(cli, "report_once", fake_report_once)

    assert cli.report(_args()) == 7
    captured = capsys.readouterr()
    assert json.loads(captured.out)["notice_delivery"]["delivery_status"] == "queued"
    assert "parent notice not delivered" in captured.err
    assert "hostb:codex-lead" in captured.err
    assert "child-report-ready-report-1" in captured.err


def test_report_surfaces_daemon_actionable_hint(monkeypatch, capsys, tmp_path):
    async def fake_report_once(config, request, timeout):
        return {
            "type": "report.error",
            "error_code": "unsupported_in_v2",
            "error": "result_blob_sha reports are not implemented in v2",
            "hint": "use agent-orch report --result with an inline payload",
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-x")
    monkeypatch.setattr(cli, "report_once", fake_report_once)

    assert cli.report(_args()) == 2
    assert "use agent-orch report --result with an inline payload" in capsys.readouterr().err


def test_report_stamps_caller_stream_id_from_env(monkeypatch, capsys, tmp_path):
    # The authenticated-caller identity rides the frame as `caller_stream_id`
    # (from AGENT_ORCH_STREAM_ID), so the daemon can bind attribution to it.
    calls = {}

    async def fake_report_once(config, request, timeout):
        calls["request"] = request
        return {"type": "report.ok", "request_id": "report-1", "report_id": "report-1", "ledger_row_id": 5}

    monkeypatch.setenv("AGENT_ORCH_STREAM_ID", "hostb:codex-worker")
    monkeypatch.delenv("PENTACLE_STREAM_ID", raising=False)
    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-worker")
    monkeypatch.setattr(cli, "report_once", fake_report_once)

    assert cli.report(_args()) == 0
    assert calls["request"]["from_stream_id"] == "hostb:codex-worker"
    assert calls["request"]["caller_stream_id"] == "hostb:codex-worker"


def test_report_caller_stream_id_stays_env_when_from_stream_id_is_overridden(monkeypatch, capsys, tmp_path):
    # The CLI identity contract keeps the caller stream ID authoritative:
    # a `--from-stream-id` override rides `from_stream_id`, but `caller_stream_id`
    # remains the TRUE env identity — so the daemon sees the mismatch and refuses
    # to attribute or terminate another seat. env identity is never overridable.
    calls = {}

    async def fake_report_once(config, request, timeout):
        calls["request"] = request
        return {"type": "report.ok", "request_id": "report-override", "report_id": "report-override", "ledger_row_id": 6}

    monkeypatch.setenv("AGENT_ORCH_STREAM_ID", "hostb:codex-worker")
    monkeypatch.delenv("PENTACLE_STREAM_ID", raising=False)
    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "report_once", fake_report_once)

    args = cli.build_parser().parse_args(
        [
            "report", "--msg-id", "7", "--status", "done",
            "--from-stream-id", "hostb:other-seat",
            "--result", json.dumps({"summary": "ok", "findings": [], "next_action": "leader_proceed"}),
        ]
    )
    assert cli.report(args) == 0
    assert calls["request"]["from_stream_id"] == "hostb:other-seat"
    assert calls["request"]["caller_stream_id"] == "hostb:codex-worker"


def test_report_surfaces_daemon_warnings(monkeypatch, capsys, tmp_path):
    async def fake_report_once(config, request, timeout):
        return {
            "type": "report.ok",
            "request_id": "report-1",
            "report_id": "report-1",
            "ledger_row_id": 5,
            "durability_ack": True,
            "warnings": [
                {
                    "code": "qa_report_evidence_below_floor",
                    "message": "put narrative evidence in details",
                }
            ],
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-x")
    monkeypatch.setattr(cli, "report_once", fake_report_once)

    assert cli.report(_args()) == 0
    assert "qa_report_evidence_below_floor" in capsys.readouterr().err


def test_report_payload_preserves_structured_qa_fields_inline_and_file_backed(tmp_path):
    payload = {
        "summary": "ready",
        "findings": [],
        "next_action": "leader_proceed",
        "completion_kind": "implementation_ready",
        "qa_verdict": "accept",
        "target_sha": "0123456789abcdef0123456789abcdef01234567",
        "qa_attestation": {"stream_id": "hostb:codex-qa", "report_id": "qa-report"},
    }

    inline, inline_blob = cli._report_payload_from_args(_args(result=json.dumps(payload)))
    assert inline_blob is None
    assert inline["completion_kind"] == "implementation_ready"
    assert inline["qa_verdict"] == "accept"
    assert inline["target_sha"] == payload["target_sha"]
    assert inline["qa_attestation"] == payload["qa_attestation"]

    result_file = tmp_path / "ready.json"
    result_file.write_text(json.dumps(payload), encoding="utf-8")
    file_payload, file_blob = cli._report_payload_from_args(_args(result=None, result_file=str(result_file)))
    assert file_payload == {}
    assert json.loads(file_blob.decode("utf-8")) == payload


def test_report_payload_aggregates_malformed_structured_qa_fields():
    payload = {
        "summary": "ready",
        "findings": [],
        "next_action": "leader_proceed",
        "completion_kind": "other",
        "qa_verdict": "maybe",
        "target_sha": "abc123",
        "qa_attestation": {"stream_id": 3, "unexpected": "field"},
    }

    with pytest.raises(SchemaError) as exc_info:
        validate_report_payload(payload, "done")

    fields = {violation["field"] for violation in exc_info.value.violations}
    assert fields == {
        "completion_kind",
        "qa_verdict",
        "target_sha",
        "qa_attestation",
        "qa_attestation.report_id",
        "qa_attestation.stream_id",
        "qa_attestation.unexpected",
    }


def test_report_payload_rejects_unknown_top_level_evidence_field():
    payload = {
        "summary": "ready",
        "findings": [],
        "next_action": "leader_proceed",
        "reviewed_target_sha": "abc123",
    }

    with pytest.raises(SchemaError) as exc_info:
        cli._report_payload_from_args(_args(result=json.dumps(payload)))

    assert exc_info.value.code == "schema_error"
    assert exc_info.value.violations == [{
        "field": "reviewed_target_sha",
        "code": "unknown_field",
        "detail": "unknown field: reviewed_target_sha",
    }]


def test_report_payload_rejects_nested_qa_verdict_without_typed_field():
    for field, payload in (
        ("details.qa_verdict", {"details": {"qa_verdict": "accept"}}),
        ("extras.nested.qa_verdict", {"extras": {"nested": {"qa_verdict": "reject"}}}),
    ):
        body = {"summary": "ready", "findings": [], "next_action": "none", **payload}
        with pytest.raises(SchemaError) as exc_info:
            cli._report_payload_from_args(_args(result=json.dumps(body)))
        assert any(
            violation["field"] == field and violation["code"] == "reserved_field"
            for violation in exc_info.value.violations
        )


@pytest.mark.parametrize(
    ("container", "value", "field"),
    [
        ("details", {"completion_kind": "implementation_ready"}, "details.completion_kind"),
        ("details", {"target_sha": "a" * 40}, "details.target_sha"),
        ("details", {"qa_attestation": {"stream_id": "alpha:qa", "report_id": "qa-1"}}, "details.qa_attestation"),
        ("extras", {"nested": {"completion_kind": "implementation_ready"}}, "extras.nested.completion_kind"),
        ("extras", {"nested": {"target_sha": "b" * 40}}, "extras.nested.target_sha"),
        ("extras", {"nested": {"qa_attestation": {"stream_id": "alpha:qa", "report_id": "qa-1"}}}, "extras.nested.qa_attestation"),
    ],
)
def test_report_payload_rejects_all_nested_governance_fields(container, value, field):
    body = {"summary": "ready", "findings": [], "next_action": "none", container: value}
    with pytest.raises(SchemaError) as exc_info:
        cli._report_payload_from_args(_args(result=json.dumps(body)))
    assert any(
        violation["field"] == field and violation["code"] == "reserved_field"
        for violation in exc_info.value.violations
    )


def test_report_payload_requires_completion_kind_for_qa_attestation():
    body = {
        "summary": "ready",
        "findings": [],
        "next_action": "none",
        "qa_attestation": {"stream_id": "alpha:qa", "report_id": "qa-1"},
    }
    with pytest.raises(SchemaError) as exc_info:
        cli._report_payload_from_args(_args(result=json.dumps(body)))
    assert any(
        violation["field"] == "qa_attestation" and violation["code"] == "invalid_dependency"
        for violation in exc_info.value.violations
    )


def test_report_structured_completion_kind_rejects_payload_conflict():
    body = {
        "summary": "ready",
        "findings": [],
        "next_action": "none",
        "completion_kind": "implementation_ready",
    }
    with pytest.raises(SchemaError) as exc_info:
        cli._report_payload_from_args(_args(
            result=json.dumps(body), completion_kind="implementation_ready",
        ))
    assert exc_info.value.violations[0]["field"] == "completion_kind"
    assert exc_info.value.violations[0]["code"] == "flag_payload_conflict"


def test_report_fails_when_daemon_omits_submitted_governance_echo(monkeypatch, capsys, tmp_path):
    async def fake_report_once(_config, request, timeout):
        return {
            "type": "report.ok",
            "request_id": request["report_id"],
            "report_id": request["report_id"],
            "ledger_row_id": 55,
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-x")
    monkeypatch.setattr(cli, "report_once", fake_report_once)

    assert cli.report(_args(qa_verdict="accept")) == 2
    assert "durable_governance_field_mismatch: qa_verdict" in capsys.readouterr().err


def test_report_payload_preserves_typed_verdict_in_unknown_sidecar_error():
    body = {
        "summary": "ready",
        "findings": [],
        "next_action": "none",
        "qa_verdict": "accept",
        "candidate_sha": "abc123",
    }
    with pytest.raises(SchemaError) as exc_info:
        cli._report_payload_from_args(_args(result=json.dumps(body)))
    violations = {violation["code"]: violation for violation in exc_info.value.violations}
    assert violations["unknown_field"]["field"] == "candidate_sha"
    assert violations["preserved_field"]["field"] == "qa_verdict"
    assert "extras" in violations["preserved_field"]["detail"]


def test_report_structured_flags_build_typed_fields_without_result():
    payload, blob = cli._report_payload_from_args(_args(
        status="done",
        completion_kind="implementation_ready",
        qa_verdict="accept",
        target_sha="0123456789abcdef0123456789abcdef01234567",
        qa_attestation_stream_id="hostb:qa",
        qa_attestation_report_id="qa-report",
    ))
    assert blob is None
    assert payload["completion_kind"] == "implementation_ready"
    assert payload["qa_verdict"] == "accept"
    assert payload["target_sha"] == "0123456789abcdef0123456789abcdef01234567"
    assert payload["qa_attestation"] == {"stream_id": "hostb:qa", "report_id": "qa-report"}


def test_report_tracked_completion_kind_is_typed_and_done_only():
    args = cli.build_parser().parse_args([
        "report",
        "--msg-id", "7",
        "--status", "done",
        "--from-stream-id", "hosta:lead",
        "--completion-kind", "tracked",
        "--result", json.dumps({
            "summary": "tracked",
            "findings": [],
            "next_action": "branch_pushed",
        }),
    ])

    payload, blob = cli._report_payload_from_args(args)

    assert blob is None
    assert payload["completion_kind"] == "tracked"

    with pytest.raises(SchemaError) as exc_info:
        validate_report_payload({
            "summary": "tracked",
            "findings": [],
            "next_action": "branch_pushed",
            "completion_kind": "tracked",
        }, "progress")

    assert any(
        violation["field"] == "completion_kind" and violation["code"] == "invalid_status"
        for violation in exc_info.value.violations
    )


def test_report_structured_flags_send_typed_fields_without_handwritten_json(monkeypatch, tmp_path):
    calls = {}

    async def fake_report_once(config, request, timeout):
        calls["request"] = request
        return {
            "type": "report.ok",
            "request_id": request["report_id"],
            "report_id": request["report_id"],
            "ledger_row_id": 55,
            "completion_kind": request["completion_kind"],
            "qa_verdict": request["qa_verdict"],
            "target_sha": request["target_sha"],
            "qa_attestation": request["qa_attestation"],
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-x")
    monkeypatch.setattr(cli, "report_once", fake_report_once)

    assert cli.report(_args(
        completion_kind="implementation_ready",
        qa_verdict="reject",
        target_sha="0123456789abcdef0123456789abcdef01234567",
        qa_attestation_stream_id="hostb:qa",
        qa_attestation_report_id="qa-report",
    )) == 0
    assert calls["request"]["completion_kind"] == "implementation_ready"
    assert calls["request"]["qa_verdict"] == "reject"
    assert calls["request"]["target_sha"] == "0123456789abcdef0123456789abcdef01234567"
    assert calls["request"]["qa_attestation"] == {
        "stream_id": "hostb:qa",
        "report_id": "qa-report",
    }


def test_report_structured_flags_reject_payload_conflicts():
    body = {"summary": "ready", "findings": [], "next_action": "none", "qa_verdict": "accept"}
    with pytest.raises(SchemaError) as exc_info:
        cli._report_payload_from_args(_args(result=json.dumps(body), qa_verdict="accept"))
    assert exc_info.value.violations[0]["code"] == "flag_payload_conflict"


def test_report_structured_flags_reject_result_file_conflicts(tmp_path):
    result_file = tmp_path / "report.json"
    result_file.write_text(json.dumps({
        "summary": "ready",
        "findings": [],
        "next_action": "none",
        "qa_verdict": "accept",
    }), encoding="utf-8")

    with pytest.raises(SchemaError) as exc_info:
        cli._report_payload_from_args(_args(
            result=None,
            result_file=str(result_file),
            qa_verdict="reject",
        ))

    assert exc_info.value.violations[0]["code"] == "flag_payload_conflict"


def test_report_result_file_composes_qa_verdict_and_target_sha(tmp_path):
    result_file = tmp_path / "report.json"
    result_file.write_text(json.dumps({
        "summary": "ready",
        "findings": [],
        "next_action": "leader_proceed",
    }), encoding="utf-8")

    payload, blob = cli._report_payload_from_args(_args(
        result=None,
        result_file=str(result_file),
        qa_verdict="accept",
        target_sha="0123456789abcdef0123456789abcdef01234567",
    ))

    assert blob is None
    assert payload == {
        "summary": "ready",
        "findings": [],
        "next_action": "leader_proceed",
        "qa_verdict": "accept",
        "target_sha": "0123456789abcdef0123456789abcdef01234567",
    }


def test_report_result_file_qa_flags_send_inline_without_blob_upload(monkeypatch, tmp_path):
    result_file = tmp_path / "report.json"
    result_file.write_text(json.dumps({
        "summary": "ready",
        "findings": [],
        "next_action": "leader_proceed",
    }), encoding="utf-8")
    calls = {}

    async def fail_upload(*_args, **_kwargs):
        raise AssertionError("QA result-file composition must use the inline path")

    async def fake_report_once(config, request, timeout):
        calls["request"] = request
        return {
            "type": "report.ok",
            "request_id": request["report_id"],
            "report_id": request["report_id"],
            "ledger_row_id": 55,
            "qa_verdict": request["qa_verdict"],
            "target_sha": request["target_sha"],
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hosta", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hosta:qa")
    monkeypatch.setattr(cli, "upload_blob_once", fail_upload)
    monkeypatch.setattr(cli, "report_once", fake_report_once)

    assert cli.report(_args(
        result=None,
        result_file=str(result_file),
        qa_verdict="accept",
        target_sha="0123456789abcdef0123456789abcdef01234567",
    )) == 0
    assert calls["request"]["qa_verdict"] == "accept"
    assert calls["request"]["target_sha"] == "0123456789abcdef0123456789abcdef01234567"
    assert "result_blob_sha" not in calls["request"]


def test_report_structured_flags_require_complete_attestation_pair():
    with pytest.raises(SchemaError) as exc_info:
        cli._report_payload_from_args(_args(
            status="progress",
            result=None,
            qa_attestation_stream_id="hostb:qa",
        ))

    assert exc_info.value.violations == [{
        "field": "qa_attestation",
        "code": "missing_field",
        "detail": "qa attestation requires both flags; missing --qa-attestation-report-id",
    }]


@pytest.mark.parametrize("status", ["progress", "error", "aborted"])
def test_implementation_ready_requires_done_status(status):
    payload = {
        "summary": "ready",
        "findings": [],
        "next_action": "leader_proceed",
        "completion_kind": "implementation_ready",
    }
    if status in {"error", "aborted"}:
        payload["reason"] = "blocked"

    with pytest.raises(SchemaError) as exc_info:
        validate_report_payload(payload, status)

    assert any(
        violation["field"] == "completion_kind" and violation["code"] == "invalid_status"
        for violation in exc_info.value.violations
    )


def test_report_from_stream_id_requires_canonical_id_before_transport(monkeypatch, capsys, tmp_path):
    calls = {}

    async def fail_report_once(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("report transport must not run for a noncanonical id")

    monkeypatch.delenv("AGENT_ORCH_STREAM_ID", raising=False)
    monkeypatch.delenv("PENTACLE_STREAM_ID", raising=False)
    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "report_once", fail_report_once)

    args = cli.build_parser().parse_args(
        [
            "report",
            "--msg-id",
            "7",
            "--status",
            "done",
            "--from-stream-id",
            "not a canonical stream id",
            "--result",
            json.dumps({"summary": "ok", "findings": [], "next_action": "leader_proceed"}),
        ]
    )

    assert cli.report(args) == 2
    assert not calls
    assert "canonical <host>:<session> stream id" in capsys.readouterr().err


def test_report_without_override_keeps_stream_id_unknown_failure(monkeypatch, capsys, tmp_path):
    calls = {}

    async def fail_report_once(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("report should not start when discovery fails")

    def fake_discover(config):
        calls["config"] = config
        return None

    monkeypatch.delenv("AGENT_ORCH_STREAM_ID", raising=False)
    monkeypatch.delenv("PENTACLE_STREAM_ID", raising=False)
    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", fake_discover)
    monkeypatch.setattr(cli, "report_once", fail_report_once)

    assert cli.report(_args()) == 2
    assert calls["config"].host_id == "hostb"
    assert "stream_id_unknown" in capsys.readouterr().err


def test_report_direct_namespace_without_from_stream_id_still_uses_discovery(monkeypatch, capsys, tmp_path):
    calls = {}

    async def fake_report_once(config, request, timeout):
        calls["request"] = request
        return {
            "type": "report.ok",
            "request_id": "report-namespace",
            "report_id": "report-namespace",
            "ledger_row_id": 7,
            "ingested": False,
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-namespace")
    monkeypatch.setattr(cli, "report_once", fake_report_once)

    args = _args()
    assert not hasattr(args, "from_stream_id")
    assert cli.report(args) == 0
    assert calls["request"]["from_stream_id"] == "hostb:codex-namespace"
    assert json.loads(capsys.readouterr().out)["type"] == "report.ok"


def test_report_file_precheck_and_upload_flow(monkeypatch, capsys, tmp_path):
    result_file = tmp_path / "result.json"
    result_file.write_text(
        json.dumps({"summary": "file", "findings": [], "next_action": "leader_proceed", "details": {"x": 1}}),
        encoding="utf-8",
    )
    calls = {}

    async def fake_upload_blob_once(config, data, timeout):
        calls["uploaded"] = json.loads(data.decode("utf-8"))
        return {"type": "upload_blob.ok", "blob_sha": "abc", "size_bytes": len(data)}

    async def fake_report_once(config, request, timeout):
        calls["request"] = request
        return {"type": "report.ok", "request_id": "report-file", "report_id": "report-file", "ledger_row_id": 9, "ingested": True}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-x")
    monkeypatch.setattr(cli, "upload_blob_once", fake_upload_blob_once)
    monkeypatch.setattr(cli, "report_once", fake_report_once)

    assert cli.report(_args(result=None, result_file=str(result_file), report_id="report-file")) == 0
    assert calls["uploaded"]["summary"] == "file"
    assert calls["request"]["result_blob_sha"] == "abc"
    assert "summary" not in calls["request"]
    assert json.loads(capsys.readouterr().out)["ingested"] is True


def test_report_file_missing_exits_before_network(capsys):
    assert cli.report(_args(result=None, result_file="/missing/nope.json")) == 2
    assert "result_file_unreadable" in capsys.readouterr().err


def test_report_file_oversize_exits_before_network(monkeypatch, capsys, tmp_path):
    result_file = tmp_path / "too-large.json"
    result_file.write_text("{}", encoding="utf-8")
    with result_file.open("ab") as handle:
        handle.truncate(64 * 1024 * 1024 + 1)

    def fail_upload(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("upload should not start for an oversize result file")

    def fail_report(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("report should not start for an oversize result file")

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-x")
    monkeypatch.setattr(cli, "upload_blob_once", fail_upload)
    monkeypatch.setattr(cli, "report_once", fail_report)

    assert cli.report(_args(result=None, result_file=str(result_file))) == 2
    assert "result_file_too_large" in capsys.readouterr().err


def test_report_schema_error_names_missing_fields(capsys):
    # Ergonomics: a status=done report with an empty payload must tell the
    # agent which fields are missing and show a valid shape to copy.
    assert cli.report(_args(result="{}", terminate=True)) == 2
    err = capsys.readouterr().err
    assert "schema_error" in err
    assert "summary" in err and "findings" in err and "next_action" in err
    assert "findings[] object requires severity" in err
    assert "where (string)" in err
    assert "suggested_fix (string or null)" in err
    assert "agent-orch report --msg-id 7 --status done --result" in err
    marker = "Minimal valid --result for --status done:\n"
    example = json.loads(err.split(marker, 1)[1].splitlines()[0])
    validate_report_payload(example, "done", enforce_inline_caps=True)


def test_report_missing_reason_error_shows_valid_error_example(capsys):
    assert cli.report(_args(status="error", result="{}")) == 2
    err = capsys.readouterr().err
    assert "missing_reason" in err
    assert "reason" in err
    assert "agent-orch report --msg-id 7 --status error --result" in err
    marker = "Minimal valid --result for --status error:\n"
    example = json.loads(err.split(marker, 1)[1].splitlines()[0])
    validate_report_payload(example, "error", enforce_inline_caps=True)


def test_report_error_accepts_complete_result_payload(monkeypatch, capsys, tmp_path):
    calls = {}

    async def fake_report_once(config, request, timeout):
        calls["request"] = request
        return {"type": "report.ok", "request_id": "report-error", "report_id": "report-error", "ledger_row_id": 10, "ingested": False}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-x")
    monkeypatch.setattr(cli, "report_once", fake_report_once)

    result = {
        "summary": "blocked",
        "findings": [],
        "next_action": "leader_decide",
        "reason": "blocked",
    }
    assert cli.report(_args(status="error", result=json.dumps(result))) == 0
    assert calls["request"]["reason"] == "blocked"
    assert json.loads(capsys.readouterr().out)["type"] == "report.ok"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("summary", None),
        ("summary", ""),
        ("findings", None),
        ("findings", {}),
        ("next_action", None),
        ("next_action", ""),
        ("reason", None),
        ("reason", ""),
    ],
)
def test_report_terminal_invalid_core_fails_before_transport_or_terminate(
    monkeypatch, capsys, field, value
):
    payload = {
        "summary": "blocked",
        "findings": [],
        "next_action": "leader_decide",
        "reason": "blocked",
    }
    payload[field] = value
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda: pytest.fail("invalid terminal payload reached config/network setup"),
    )
    monkeypatch.setattr(
        cli,
        "_resolve_self_terminate",
        lambda *_args, **_kwargs: pytest.fail("invalid terminal payload reached terminate classification"),
    )

    assert cli.report(_args(status="error", result=json.dumps(payload), terminate=True)) == 2
    assert field in capsys.readouterr().err


def test_report_terminal_invalid_result_file_fails_before_transport(tmp_path, monkeypatch, capsys):
    result_file = tmp_path / "report.json"
    result_file.write_text(
        json.dumps(
            {
                "summary": "blocked",
                "findings": [],
                "next_action": None,
                "reason": "blocked",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda: pytest.fail("invalid result file reached config/network setup"),
    )

    assert cli.report(_args(status="error", result=None, result_file=str(result_file), terminate=True)) == 2
    assert "next_action" in capsys.readouterr().err


def test_report_without_msg_id_defaults_to_zero_for_proactive_report(monkeypatch, capsys, tmp_path):
    calls = {}

    async def fake_report_once(config, request, timeout):
        calls["request"] = request
        return {"type": "report.ok", "request_id": "report-1", "report_id": "report-1", "ledger_row_id": 9, "ingested": False}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-x")
    monkeypatch.setattr(cli, "report_once", fake_report_once)

    assert cli.report(_args(msg_id=None, terminate=False)) == 0
    assert calls["request"]["msg_id"] == 0
    assert json.loads(capsys.readouterr().out)["type"] == "report.ok"


def test_report_timeout_verifies_stream_mode_report_durability(monkeypatch, capsys, tmp_path):
    calls = {}

    async def timeout_report_once(config, request, timeout):
        calls["request"] = request
        raise TimeoutError("deadline")

    async def fake_inspect_stream_once(config, stream_id, **kwargs):
        calls["inspect"] = {"stream_id": stream_id, **kwargs}
        return {
            "type": "inspect_stream.ok",
            "existing_report": {
                "report_id": "report-timeout",
                "ledger_row_id": 51,
                "to_stream_id": "hostb:leader",
            },
        }

    async def fake_ledger_get_once(config, tell_id, *, timeout):
        return {
            "type": "ledger_get.ok",
            "tell_id": tell_id,
            "tell": {
                "tell_id": tell_id,
                "ledger_row_id": 61,
                "delivery_status": "delivered",
                "delivery_ack_at": "2026-08-26T08:00:00Z",
                "to_stream_id": "hostb:leader",
                "reason_key": "child_report_ready",
                "from_stream_id": "hostb:codex-x",
                "text": "[child_report_ready]\nreport_id=report-timeout",
            },
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-x")
    monkeypatch.setattr(cli, "report_once", timeout_report_once)
    monkeypatch.setattr(cli, "inspect_stream_once", fake_inspect_stream_once)
    monkeypatch.setattr(cli, "ledger_get_once", fake_ledger_get_once)
    monkeypatch.setattr(
        cli,
        "fetch_snapshot",
        lambda _config, **_kwargs: {"sessions": [{"stream_id": "hostb:codex-x", "self_close_on_completion": True}]},
    )

    rc = cli.report(_args(report_id="report-timeout", msg_id=None, terminate=True))

    assert rc == 0
    assert calls["request"]["report_id"] == "report-timeout"
    assert calls["inspect"]["stream_id"] == "hostb:codex-x"
    assert calls["inspect"]["report_id"] == "report-timeout"
    captured = capsys.readouterr()
    assert "report durability confirmed" in captured.err
    assert json.loads(captured.out)["state"] == "confirmed_after_timeout"


@pytest.mark.parametrize("outcome", ("timeout", "transport_error", "indeterminate"))
def test_report_recovery_rejects_missing_submitted_governance_echo(
    monkeypatch, capsys, tmp_path, outcome,
):
    async def recover_report_once(_config, request, timeout):
        if outcome == "timeout":
            raise TimeoutError("deadline")
        if outcome == "transport_error":
            raise OSError("socket closed")
        return {"type": "report.indeterminate", "report_id": request["report_id"]}

    async def fake_inspect_stream_once(_config, _stream_id, **kwargs):
        return {
            "type": "inspect_stream.ok",
            "existing_report": {
                "report_id": kwargs["report_id"],
                "ledger_row_id": 91,
                "qa_verdict": None,
            },
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-x")
    monkeypatch.setattr(cli, "report_once", recover_report_once)
    monkeypatch.setattr(cli, "inspect_stream_once", fake_inspect_stream_once)

    assert cli.report(_args(report_id=f"recovery-mismatch-{outcome}", qa_verdict="accept")) == 2
    captured = capsys.readouterr()
    assert "durable_governance_field_mismatch: qa_verdict" in captured.err
    assert captured.out == ""


def test_report_indeterminate_response_verifies_stream_mode_report_durability(monkeypatch, capsys, tmp_path):
    calls = {}

    async def indeterminate_report_once(config, request, timeout):
        calls["request"] = request
        return {"type": "report.indeterminate", "request_id": "report-1", "report_id": "report-indeterminate", "error_code": "report_id_in_flight_timeout"}

    async def fake_inspect_stream_once(config, stream_id, **kwargs):
        calls["inspect"] = {"stream_id": stream_id, **kwargs}
        return {
            "type": "inspect_stream.ok",
            "existing_report": {
                "report_id": "report-indeterminate",
                "ledger_row_id": 52,
                "to_stream_id": "hostb:leader",
            },
        }

    async def fake_ledger_get_once(config, tell_id, *, timeout):
        return {
            "type": "ledger_get.ok",
            "tell_id": tell_id,
            "tell": {
                "tell_id": tell_id,
                "ledger_row_id": 62,
                "delivery_status": "delivered",
                "delivery_ack_at": "2026-08-26T08:00:00Z",
                "to_stream_id": "hostb:leader",
                "reason_key": "child_report_ready",
                "from_stream_id": "hostb:codex-x",
                "text": "[child_report_ready]\nreport_id=report-indeterminate",
            },
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-x")
    monkeypatch.setattr(cli, "report_once", indeterminate_report_once)
    monkeypatch.setattr(cli, "inspect_stream_once", fake_inspect_stream_once)
    monkeypatch.setattr(cli, "ledger_get_once", fake_ledger_get_once)

    rc = cli.report(_args(report_id="report-indeterminate"))

    assert rc == 0
    assert calls["request"]["report_id"] == "report-indeterminate"
    assert calls["inspect"]["stream_id"] == "hostb:codex-x"
    assert calls["inspect"]["report_id"] == "report-indeterminate"
    captured = capsys.readouterr()
    assert json.loads(captured.out)["state"] == "confirmed_after_indeterminate"
    assert "report durability confirmed" in captured.err


def test_report_timeout_with_no_exact_durable_row_remains_a_loud_failure(monkeypatch, capsys, tmp_path):
    async def timeout_report_once(config, request, timeout):
        raise TimeoutError("deadline")

    async def fake_inspect_stream_once(config, stream_id, **kwargs):
        assert kwargs["report_id"] == "absent-report"
        return {"type": "inspect_stream.ok", "existing_report": None}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-x")
    monkeypatch.setattr(cli, "report_once", timeout_report_once)
    monkeypatch.setattr(cli, "inspect_stream_once", fake_inspect_stream_once)

    assert cli.report(_args(report_id="absent-report")) == 67
    captured = capsys.readouterr()
    assert "report may not be durable for report_id=absent-report" in captured.err
    assert captured.out == ""


@pytest.mark.parametrize(
    ("outcome", "state"),
    [
        ("timeout", "confirmed_after_timeout"),
        ("transport_error", "confirmed_after_transport_error"),
        ("indeterminate", "confirmed_after_indeterminate"),
    ],
)
def test_durable_row_without_delivered_notice_is_loud_for_every_recovery_state(
    monkeypatch, capsys, tmp_path, outcome, state,
):
    """Durability alone is never report-notice delivery success."""
    async def fake_report_once(config, request, timeout):
        if outcome == "timeout":
            raise TimeoutError("deadline")
        if outcome == "transport_error":
            raise ConnectionError("socket closed")
        return {"type": "report.indeterminate", "report_id": request["report_id"]}

    async def fake_inspect_stream_once(config, stream_id, **kwargs):
        return {
            "type": "inspect_stream.ok",
            "existing_report": {
                "report_id": kwargs["report_id"],
                "ledger_row_id": 56,
                "to_stream_id": "hostb:leader",
            },
        }

    async def fake_ledger_get_once(config, tell_id, *, timeout):
        if outcome == "timeout":
            return {
                "type": "ledger_get.error",
                "tell_id": tell_id,
                "error_code": "tell_not_found",
            }
        if outcome == "indeterminate":
            raise OSError("ledger unavailable")
        return {
            "type": "ledger_get.ok",
            "tell_id": tell_id,
            "tell": {
                "tell_id": tell_id,
                "ledger_row_id": 66,
                "delivery_status": "delivered",
                "delivery_ack_at": "2026-08-26T08:00:00Z",
                "to_stream_id": "hostb:wrong-parent",
                "reason_key": "child_report_ready",
                "from_stream_id": "hostb:codex-x",
                "text": "[child_report_ready]\nreport_id=wrong-report",
            },
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-x")
    monkeypatch.setattr(cli, "report_once", fake_report_once)
    monkeypatch.setattr(cli, "inspect_stream_once", fake_inspect_stream_once)
    monkeypatch.setattr(cli, "ledger_get_once", fake_ledger_get_once)

    rc = cli.report(_args(report_id=f"report-{outcome}"))

    assert rc == 7
    captured = capsys.readouterr()
    body = json.loads(captured.out)
    assert body["type"] == "report.durable_confirmed"
    assert body["report_id"] == f"report-{outcome}"
    assert body["ledger_row_id"] == 56
    assert body["state"] == state
    assert body["notice_delivery"]["delivery_status"] == "failed"
    assert body["notice_delivery"]["to_stream_id"] == "hostb:leader"
    assert body["notice_delivery"]["tell_id"].startswith("child-report-ready-v2-")
    assert "parent notice not delivered:" in captured.err
    assert "to hostb:leader" in captured.err
    assert body["notice_delivery"]["tell_id"] in captured.err


def test_await_command_closed_without_report_exits_69(monkeypatch, capsys, tmp_path):
    calls = {}

    async def fake_await_report_once(config, stream_id, msg_id, **kwargs):
        calls["stream_id"] = stream_id
        calls["msg_id"] = msg_id
        return {
            "type": "await_report.closed_without_report",
            "request_id": "await-7",
            "ok": False,
            "stream_id": stream_id,
            "msg_id": msg_id,
            "error": "closed_without_report",
            "reason": "closed_without_report",
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "await_report_once", fake_await_report_once)

    rc = cli.await_command(
        Namespace(
            stream_id="hostb:codex-x",
            msg_id=7,
            timeout=1.0,
            include_details=False,
            include_extras=False,
        )
    )

    assert rc == 69
    assert calls == {"stream_id": "hostb:codex-x", "msg_id": 7}
    captured = capsys.readouterr()
    assert json.loads(captured.out)["type"] == "await_report.closed_without_report"
    assert "closed without a terminal report" in captured.err


def test_report_terminate_defaults_msg_id_to_zero(monkeypatch, capsys, tmp_path):
    # With --terminate and no --msg-id, the same proactive convention applies.
    calls = {}

    async def fake_report_once(config, request, timeout):
        calls["request"] = request
        return {"type": "report.ok", "request_id": "report-1", "report_id": "report-1", "ledger_row_id": 9, "ingested": False, "closed": True, "durability_ack": True}

    async def fake_close_once(config, stream_id, **kwargs):
        raise AssertionError("daemon-atomic close_on_ingest should skip close_once")

    snap = {"sessions": [{"stream_id": "hostb:codex-x", "self_close_on_completion": True, "online": True, "parent_stream_id": "hostb:leader"}]}
    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-x")
    monkeypatch.setattr(cli, "report_once", fake_report_once)
    monkeypatch.setattr(cli, "close_once", fake_close_once)
    monkeypatch.setattr(cli, "fetch_snapshot", lambda _config, **_kwargs: snap)

    rc = cli.report(_args(msg_id=None, terminate=True))
    assert rc == 0
    assert calls["request"]["msg_id"] == 0
    assert calls["request"]["terminate"] is True
    assert calls["request"]["close_on_ingest"] is True


def test_report_terminate_uses_fallback_close_after_durable_replay(monkeypatch, tmp_path):
    calls = {}

    async def fake_report_once(config, request, timeout):
        return {
            "type": "report.ok",
            "request_id": "report-1",
            "report_id": "report-1",
            "ledger_row_id": 9,
            "durability_ack": True,
        }

    async def fake_close_once(config, stream_id, **kwargs):
        calls["close"] = {"stream_id": stream_id, **kwargs}
        return {"type": "close.ok"}

    snapshot = {
        "sessions": [
            {
                "stream_id": "hostb:codex-x",
                "self_close_on_completion": True,
                "online": True,
                "parent_stream_id": "hostb:leader",
            }
        ]
    }
    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-x")
    monkeypatch.setattr(cli, "report_once", fake_report_once)
    monkeypatch.setattr(cli, "close_once", fake_close_once)
    monkeypatch.setattr(cli, "fetch_snapshot", lambda _config, **_kwargs: snapshot)

    assert cli.report(_args(terminate=True)) == 0
    assert calls["close"]["stream_id"] == "hostb:codex-x"


def test_report_terminate_refusal_sends_report_without_close_on_ingest(monkeypatch, capsys, tmp_path):
    calls = {}

    async def fake_report_once(config, request, timeout):
        calls["request"] = request
        return {"type": "report.ok", "request_id": "report-1", "report_id": "report-1", "ledger_row_id": 9, "ingested": False, "durability_ack": True}

    async def fake_close_once(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("refused terminate must not close")

    snap = {
        "sessions": [
            {"stream_id": "hostb:codex-x", "online": True, "parent_stream_id": "hostb:leader"},
            {"stream_id": "hostb:leader", "online": True},
        ]
    }
    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-x")
    monkeypatch.setattr(cli, "report_once", fake_report_once)
    monkeypatch.setattr(cli, "close_once", fake_close_once)
    monkeypatch.setattr(cli, "fetch_snapshot", lambda _config, **_kwargs: snap)

    rc = cli.report(_args(terminate=True))
    assert rc == 6
    assert calls["request"]["terminate"] is True
    assert "close_on_ingest" not in calls["request"]
    captured = capsys.readouterr()
    assert json.loads(captured.out)["type"] == "report.ok"
    assert "terminate refused: terminate_requires_leader_close" in captured.err


@pytest.mark.parametrize("durability_ack", [None, False])
def test_report_terminate_refuses_missing_or_false_durability_ack(
    monkeypatch, capsys, tmp_path, durability_ack
):
    async def fake_report_once(config, request, timeout):
        response = {
            "type": "report.ok",
            "request_id": "report-1",
            "report_id": "report-1",
            "ledger_row_id": 9,
            "closed": True,
        }
        if durability_ack is not None:
            response["durability_ack"] = durability_ack
        return response

    async def fake_close_once(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("missing durability acknowledgement must not close")

    snapshot = {
        "sessions": [
            {
                "stream_id": "hostb:codex-x",
                "self_close_on_completion": True,
                "online": True,
                "parent_stream_id": "hostb:leader",
            }
        ]
    }
    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-x")
    monkeypatch.setattr(cli, "report_once", fake_report_once)
    monkeypatch.setattr(cli, "close_once", fake_close_once)
    monkeypatch.setattr(cli, "fetch_snapshot", lambda _config, **_kwargs: snapshot)

    assert cli.report(_args(terminate=True)) == 5
    assert "report_durability_ack_missing" in capsys.readouterr().err


def test_report_ok_missing_authoritative_notice_is_loud(monkeypatch, capsys, tmp_path):
    async def fake_report_once(_config, _request, timeout):
        return {
            "type": "report.ok",
            "report_id": "missing-receipt",
            "ledger_row_id": 71,
            "durability_ack": True,
            "to_stream_id": "hostb:leader",
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:child")
    monkeypatch.setattr(cli, "report_once", fake_report_once)

    assert cli.report(_args(report_id="missing-receipt")) == 7
    captured = capsys.readouterr()
    assert "parent notice not delivered" in captured.err
    assert "to hostb:leader" in captured.err
    assert "correlation unknown" in captured.err


# pop2: a terminal report rejected client-side by schema validation must still
# let the daemon durably record the rejected attempt for a self-close seat
# (spec_example__reporting).

def test_pop2_schema_fail_best_effort_records_rejection(monkeypatch, capsys, tmp_path):
    calls = {}

    async def fake_report_once(config, request, timeout):
        calls["request"] = request
        return {"type": "report.error", "error_code": "schema_error", "error": "summary required"}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "env_stream_id", lambda: "hostb:codex-x")
    monkeypatch.setattr(cli, "report_once", fake_report_once)

    # `--result ok` never parses to a ReportPayloadV1 — the observed pop2 shape.
    assert cli.report(_args(result="ok")) == 2
    assert "validation failed" in capsys.readouterr().err
    assert calls["request"]["from_stream_id"] == "hostb:codex-x"
    assert calls["request"]["status"] == "done"
    assert "summary" not in calls["request"]  # the invalid payload is not forwarded


def test_pop2_schema_fail_skips_transmit_without_stream_identity(monkeypatch, capsys, tmp_path):
    called = {"n": 0}

    async def fake_report_once(config, request, timeout):
        called["n"] += 1
        return {}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "env_stream_id", lambda: None)
    monkeypatch.setattr(cli, "report_once", fake_report_once)

    assert cli.report(_args(result="ok")) == 2
    assert called["n"] == 0  # a tokenless (human) caller records no rejection


def test_pop2_schema_fail_transmit_swallows_daemon_unreachable(monkeypatch, capsys, tmp_path):
    async def fake_report_once(config, request, timeout):
        raise OSError("chat_streamd unreachable")

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "env_stream_id", lambda: "hostb:codex-x")
    monkeypatch.setattr(cli, "report_once", fake_report_once)

    # Best-effort: a daemon-unreachable transmit never changes the seat's result.
    assert cli.report(_args(result="ok")) == 2
    assert "validation failed" in capsys.readouterr().err


def test_pop2_missing_reason_error_records_rejection(monkeypatch, capsys, tmp_path):
    # F1: `--status error` with no reason returns early (missing_reason) — that
    # terminal-report rejection must still be recorded for a self-close seat.
    calls = {}

    async def fake_report_once(config, request, timeout):
        calls["request"] = request
        return {"type": "report.error", "error_code": "missing_reason", "error": "reason required"}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "env_stream_id", lambda: "hostb:codex-x")
    monkeypatch.setattr(cli, "report_once", fake_report_once)

    assert cli.report(_args(status="error", reason=None, result=None)) == 2
    assert "missing_reason" in capsys.readouterr().err
    assert calls["request"]["from_stream_id"] == "hostb:codex-x"
    assert calls["request"]["status"] == "error"
