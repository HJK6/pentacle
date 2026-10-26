from __future__ import annotations

import argparse
import ast
import base64
import json
import os
from pathlib import Path

import pytest

from agent_orch import cli, send_boundary as b, wsclient as w
from send_boundary_capture import Connection, Socket

CASES = json.loads((Path(__file__).parent / "fixtures/send_boundary/dispatch_cases.json").read_text())


def parser_leaves(parser, prefix=()):
    subs = next((action for action in parser._actions if isinstance(action, argparse._SubParsersAction)), None)
    if subs:
        for name, child in subs.choices.items():
            yield from parser_leaves(child, prefix + (name,))
    else:
        yield " ".join(prefix), parser.get_default("func")


def test_dispatch_table_covers_parser_and_preargparse_report_route():
    leaves = dict(parser_leaves(cli.build_parser()))
    assert set(leaves) | {"report get"} == {row["command"] for row in CASES}
    for row in CASES:
        assert bool(row.get("network_entry")) != bool(row.get("not_applicable"))
        if row["command"] in leaves:
            fn = leaves[row["command"]]
            assert row["handler"] == fn.__module__ + "." + fn.__name__


@pytest.mark.parametrize("row", [row for row in CASES if row["network_entry"]], ids=lambda row: row["command"])
def test_every_network_dispatch_translates_only_service_error(monkeypatch, tmp_path, capsys, row):
    monkeypatch.setattr(os, "environ", {
        "HOME": str(tmp_path), "AGENT_ORCH_HOST_ID": "fixture",
        "AGENT_ORCH_STREAM_ID": "amaterasu:wmi-pg-dailybackup",
        "AGENT_ORCH_STREAM_TOKEN_FILE": str(tmp_path / "missing-service-file"),
        "AGENT_ORCH_TOKEN": "OPERATOR-must-not-appear",
    })
    # Only source metadata and harmless local-admin test data are provisioned.
    runtime = tmp_path / ".agent-orch"
    runtime.mkdir()
    admin = tmp_path / ".config/pentacle-stream/local-admin.token"
    admin.parent.mkdir(parents=True)
    admin.write_text("ADMIN-sentinel")
    admin.chmod(0o600)
    item = tmp_path / "work/backlog/fixture__triage"
    item.mkdir(parents=True)
    (item / "spec.md").write_text("---\nid: spec_fixture__triage\ntitle: Fixture\nstatus: backlog\ntags:\n- triage\n---\nFixture\n")
    (item / "summary.md").write_text("---\nid: work_fixture__triage\ntitle: Fixture\nstatus: backlog\n---\nFixture\n")
    file = tmp_path / "fixture.txt"
    file.write_text("fixture")
    json_file = tmp_path / "fixture.json"
    json_file.write_text("{}")
    png = tmp_path / "fixture.png"
    png.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg=="))
    values = {"__FILE__": str(file), "__PNG__": str(png), "__JSON__": str(json_file), "__ROOT__": str(tmp_path)}
    argv = [values.get(arg, arg) for arg in row["argv"]]
    sockets, hits = [], []
    async def boundary(*args, **kwargs):
        hits.append(True)
        # Inject at the real send boundary, after the real command, RPC
        # builder and connection path have run. No outbound frame is emitted.
        raise b.ServiceCredentialUnavailable(b.resolve_outbound_identity())
    # Imported boundary references in both real transport modules are patched,
    # rather than replacing any command, dispatcher, or RPC entry function.
    monkeypatch.setattr(w, "send_frame", boundary)
    from agent_orch import work_report_cli
    monkeypatch.setattr(work_report_cli, "send_frame", boundary)
    def connect(*a, **kw):
        socket = Socket()
        sockets.append(socket)
        return Connection(socket)
    monkeypatch.setattr(w.websockets, "connect", connect)
    try:
        result = cli.main(argv)
    finally:
        import asyncio
        for socket in sockets:
            asyncio.run(socket.close())
    output = capsys.readouterr()
    assert hits or row["command"] == "dashboard transfer-owner", (row, result, output)
    assert not any(socket.raw for socket in sockets)
    assert "sentinel" not in output.out + output.err
    assert "OPERATOR-must-not-appear" not in output.out + output.err
    if row["command"] == "report get":
        assert result == 1
        assert not output.err
        assert json.loads(output.out)["error_code"] == "unavailable"
    else:
        assert result == 2, (row, output)
        assert not output.out
        assert len(output.err.splitlines()) == 1
        assert "service credential unavailable" in output.err
        assert "amaterasu:wmi-pg-dailybackup" in output.err
        assert str(tmp_path / "missing-service-file") in output.err


def test_command_local_catches_do_not_swallow_service_error():
    # Audit every package catch, including command modules imported by the CLI.
    # Reader's intentionally isolated JSON translator is the sole exception.
    for module in ("cli", "work_lane_cli", "screen_cli", "external_work_cli"):
        tree = ast.parse((Path(cli.__file__).parent / (module + ".py")).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                generic = next((i for i, handler in enumerate(node.handlers) if handler.type and ast.unparse(handler.type) == "Exception"), None)
                if generic is not None:
                    assert any(handler.type and ast.unparse(handler.type) == "ServiceCredentialUnavailable" for handler in node.handlers[:generic]), (module, node.lineno)


def test_service_error_diagnostic_is_one_sanitized_line(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_ORCH_STREAM_TOKEN_FILE", "fixture\npath\r")
    assert cli._service_credential_error(b.ServiceCredentialUnavailable(b.OutboundIdentity("service", "altum-bot-cd"))) == 2
    assert len(capsys.readouterr().err.splitlines()) == 1
