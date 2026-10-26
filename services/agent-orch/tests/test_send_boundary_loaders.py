from __future__ import annotations

import asyncio
from itertools import product
import json
import os
from pathlib import Path
import re
import threading
import time
import subprocess
import sys

import pytest

from agent_orch import cli, config, send_boundary as b, wsclient as w
from send_boundary_capture import Connection, Socket, ENVELOPE, NONCE


@pytest.mark.parametrize("values", list(product([None, "fixture:seat", "altum-bot-cd", "amaterasu:wmi-pg-dailybackup", "work-report-reader"], repeat=4)))
def test_identity_all_source_conflicts(values):
    explicit, *environment = values
    env = {name: value for name, value in zip(b.IDENTITY_ENV, environment) if value is not None}
    present = [value for value in values if value]
    services = {value for value in present if value in b.FIXED_SERVICE_IDENTITIES}
    identity = b.resolve_outbound_identity(explicit, env)
    if services:
        assert identity.kind == ("service" if len(set(present)) == 1 else "ambiguous")
        assert identity.stream_id in services
    else:
        assert identity.kind == ("seat" if present else "operator")
        assert identity.stream_id == (present[0] if present else None)


def test_service_mapping_matches_daemon():
    server = (Path(__file__).parents[2] / "chat-stream-v2/server.py").read_text()
    names = ("FIXED_SYSTEM_PRODUCER_STREAM_ID", "WMI_BACKUP_PRODUCER_STREAM_ID", "WORK_REPORT_READER")
    identities = {re.search(rf'^{name}\s*=\s*[\'"]([^\'"]+)', server, re.M).group(1) for name in names}
    assert set(b.FIXED_SERVICE_IDENTITIES) == identities
    assert b.FIXED_SERVICE_IDENTITIES["work-report-reader"] == ("env", "PENTACLE_WORK_REPORT_READER_TOKEN")


class Environment(dict):
    def __init__(self, *args):
        super().__init__(*args)
        self.reads = []

    def get(self, key, default=None):
        if key == "AGENT_ORCH_TOKEN":
            self.reads.append(key)
        return super().get(key, default)

    def __getitem__(self, key):
        if key == "AGENT_ORCH_TOKEN":
            self.reads.append(key)
        return super().__getitem__(key)


@pytest.mark.parametrize("identity", ["altum-bot-cd", "amaterasu:wmi-pg-dailybackup", "work-report-reader", "ambiguous"])
@pytest.mark.parametrize("path", ["hello", "request", "retry", "reconnect", "cli"])
def test_real_loader_path_never_reads_operator_for_services(monkeypatch, tmp_path, identity, path):
    token = tmp_path / "token"
    token.write_text("FILE-sentinel")
    token.chmod(0o600)
    env = Environment({"HOME": str(tmp_path), "AGENT_ORCH_HOST_ID": "fixture",
                       "AGENT_ORCH_STREAM_ID": "altum-bot-cd" if identity == "ambiguous" else identity,
                       "AGENT_ORCH_TOKEN": "OPERATOR-must-not-load",
                       "AGENT_ORCH_STREAM_TOKEN_FILE": str(token),
                       "PENTACLE_WORK_REPORT_READER_TOKEN": "READER-sentinel"})
    if identity == "ambiguous":
        env["PENTACLE_STREAM_ID"] = "fixture:conflict"
    monkeypatch.setattr(os, "environ", env)
    monkeypatch.setattr(b, "_file_token", lambda *a: pytest.fail("operator file loader reached"))
    cfg = config.load_config()
    assert not env.reads
    assert cfg.token == ""
    assert "OPERATOR-must-not-load" not in repr(cfg.operator_token_source)
    sockets = []
    def connect(*a, **kw):
        socket = Socket(fail=path == "retry" and not sockets)
        sockets.append(socket)
        return Connection(socket)
    monkeypatch.setattr(w.websockets, "connect", connect)
    monkeypatch.setattr(w, "_retry_next_delay", lambda *a: (0, "") if a[-1] < 2 else (None, "retry_exhausted"))
    async def journey():
        if path in {"hello", "reconnect"}:
            client = w.WebsocketClient(cfg)
            await client._connect_and_read()
            if path == "reconnect":
                await client._connect_and_read()
        else:
            await w._one_shot_rpc(cfg, {"type": "thread.read", "request_id": "fixture"}, prefix="thread", timeout=1)
    try:
        if path == "cli":
            assert cli.main(["thread", "fixture:target", "--timeout", "1"]) == (2 if identity == "ambiguous" else 0)
        elif identity == "ambiguous":
            with pytest.raises(b.ServiceCredentialUnavailable):
                asyncio.run(journey())
        else:
            asyncio.run(journey())
    finally:
        for socket in sockets:
            asyncio.run(socket.close())
    assert not env.reads


@pytest.mark.parametrize("source,expected,kind", [("file", "FILE-operator-sentinel", "file"),
    ("empty-env", "", "env"), ("missing", "", "none"), ("env", "ENV-operator-sentinel", "env")])
def test_operator_file_success_from_real_cli(monkeypatch, tmp_path, source, expected, kind, capsys):
    env = Environment({"HOME": str(tmp_path), "AGENT_ORCH_HOST_ID": "fixture"})
    if source == "empty-env":
        env["AGENT_ORCH_TOKEN"] = ""
    if source == "env":
        env["AGENT_ORCH_TOKEN"] = "ENV-operator-sentinel"
    operator_file = tmp_path / ".config/pentacle-stream/token"
    operator_file.parent.mkdir(parents=True)
    if source != "missing":
        operator_file.write_text(" FILE-operator-sentinel\n")
    monkeypatch.setattr(os, "environ", env)
    assert not env.reads
    cfg = config.load_config()
    assert not env.reads
    assert cfg.operator_token_source.env_present == (source in {"empty-env", "env"})
    reads, decisions = [], []
    original_file, original_identity = b._file_token, b.resolve_outbound_identity
    def read_file(*args):
        reads.append("file")
        return original_file(*args)
    def identity(*args, **kwargs):
        decisions.append("identity")
        return original_identity(*args, **kwargs)
    monkeypatch.setattr(b, "_file_token", read_file)
    monkeypatch.setattr(b, "resolve_outbound_identity", identity)
    socket = Socket()
    monkeypatch.setattr(w.websockets, "connect", lambda *a, **k: Connection(socket))
    assert cli.main(["list"]) == 0
    assert decisions
    hello = json.loads(socket.raw[0])
    assert hello.get("token", "") == expected
    assert len(reads) == (0 if source in {"empty-env", "env"} else 1)
    resolved = b.resolve_operator_token(cfg)
    assert (resolved.kind, resolved.value) == (kind, expected)
    assert expected == "" or expected not in repr(resolved)
    assert socket.closed
    assert "sentinel" not in str(capsys.readouterr())


def test_explicit_config_token_remains_explicit(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "environ", {"AGENT_ORCH_TOKEN": "wrong-env"})
    cfg = config.Config("ws://fixture.invalid", "EXPLICIT-sentinel", "fixture", tmp_path)
    resolved = b.resolve_operator_token(cfg)
    assert (resolved.kind, resolved.value) == ("explicit", "EXPLICIT-sentinel")
    sock = Socket()
    asyncio.run(b.send_frame(sock, w._rpc_hello(cfg), config=cfg))
    assert json.loads(sock.raw[0])["token"] == "EXPLICIT-sentinel"


@pytest.mark.parametrize("failure", ["missing", "ambiguous"])
@pytest.mark.parametrize("path", ["persistent", "retry"])
def test_open_service_connection_is_poisoned_before_further_frames(monkeypatch, tmp_path, failure, path):
    token = tmp_path / "token"
    token.write_text("FILE-sentinel")
    token.chmod(0o600)
    monkeypatch.setattr(os, "environ", {"AGENT_ORCH_STREAM_ID": "altum-bot-cd", "AGENT_ORCH_STREAM_TOKEN_FILE": str(token)})
    cfg = config.Config("ws://fixture.invalid", "OPERATOR-sentinel", "fixture", tmp_path)
    sock = Socket()
    async def journey():
        await b.send_frame(sock, w._rpc_hello(cfg), config=cfg)
        assert len(sock.raw) == 1
        if failure == "missing":
            token.unlink()
        else:
            monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:conflict")
        with pytest.raises(b.ServiceCredentialUnavailable):
            if path == "persistent":
                client = w.WebsocketClient(cfg)
                client._ws = sock
                pending = w.PendingRpc("fixture", threading.Event(), {"type": "fixture", "request_id": "fixture"}, send_owner="fixture")
                client._pending["fixture"] = pending
                await client._send_pending_payload("fixture", pending, pending.payload, owner="fixture")
            else:
                await b.send_frame(sock, {"type": "fixture", "request_id": "fixture"}, config=cfg)
        assert sock.closed and len(sock.raw) == 1
        with pytest.raises(b.ServiceCredentialUnavailable):
            await b.send_uncredentialed(sock, {"type": "pong"})
        assert len(sock.raw) == 1
    asyncio.run(journey())


def test_uncredentialed_frame_runtime_assertion():
    for key in b.CREDENTIAL_KEYS:
        with pytest.raises(AssertionError):
            asyncio.run(b.send_uncredentialed(Socket(), {"type": "pong", key: "forbidden"}))


def test_attachment_metadata_accessor_never_opens_token(monkeypatch, tmp_path):
    path = tmp_path / "unread-token"
    monkeypatch.setenv("AGENT_ORCH_STREAM_TOKEN_FILE", str(path))
    def forbidden(*args, **kwargs):
        pytest.fail("metadata accessor touched a file")
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(os, "open", forbidden)
    assert b.configured_token_file_paths() == [path]


def test_service_never_reads_caller_credential_while_building_or_sending(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("FILE-sentinel")
    token.chmod(0o600)
    monkeypatch.setattr(os, "environ", {"AGENT_ORCH_STREAM_TOKEN_FILE": str(token), "AGENT_ORCH_STREAM_ID": "altum-bot-cd"})
    class Caller(dict):
        def __getitem__(self, key):
            if key in b.CREDENTIAL_KEYS:
                pytest.fail("service read a caller credential")
            return super().__getitem__(key)
    frame = b.defer_stream_token(Caller(type="fixture", stream_token="CALLER-sentinel", identity_token="CALLER-other", token="OPERATOR-caller"))
    assert b.CREDENTIAL_KEYS.isdisjoint(frame)
    assert b.caller_stream_token(frame) is None
    socket = Socket()
    cfg = config.Config("ws://fixture.invalid", "OPERATOR-sentinel", "fixture", tmp_path)
    asyncio.run(b.send_frame(socket, frame, config=cfg))
    assert json.loads(socket.raw[0])["stream_token"] == "FILE-sentinel"


def test_envelope_proof_material_never_leaves_boundary_repr_or_frames(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(os, "environ", {})
    cfg = config.Config("ws://fixture.invalid", ENVELOPE, "fixture", tmp_path)
    resolved = b.resolve_operator_token(cfg)
    assert resolved.form == "envelope"
    assert ENVELOPE not in repr(resolved)
    fields = b.operator_auth_v2_hello_fields(resolved, {"nonce": NONCE})
    assert set(fields) == {"client", "auth_v2"}
    assert set(fields["auth_v2"]) == {"scheme", "credential_id", "proof"}
    socket = Socket()
    asyncio.run(b.send_frame(socket, w._rpc_hello(cfg), config=cfg, challenge={"nonce": NONCE}))
    assert "token" not in json.loads(socket.raw[0])
    assert "proof_key" not in socket.raw[0] + repr(fields) + repr(resolved)
    assert ENVELOPE not in socket.raw[0]
    assert not capsys.readouterr().out


def test_persistent_queue_and_replay_resolve_file_at_each_send(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("FILE-initial")
    token.chmod(0o600)
    monkeypatch.setattr(os, "environ", {"AGENT_ORCH_STREAM_ID": "altum-bot-cd", "AGENT_ORCH_STREAM_TOKEN_FILE": str(token)})
    cfg = config.Config("ws://fixture.invalid", "OPERATOR-sentinel", "fixture", tmp_path)
    client = w.WebsocketClient(cfg)
    queued = []
    client._rpc = lambda prefix, payload: queued.append(payload)
    client.tell("fixture", "altum-bot-cd", "fixture:peer", "fixture", request_id="fixture")
    frame = queued[0]
    assert b.CREDENTIAL_KEYS.isdisjoint(frame)
    token.write_text("FILE-first-send")
    socket = Socket()
    client._ws = socket
    pending = w.PendingRpc("tell", threading.Event(), frame, retry_eligible=True,
                           retry_policy=w._rpc_retry_policy_from_env(1),
                           retry_deadline=time.monotonic() + 30, attempts=1, send_owner="fixture")
    client._pending["fixture"] = pending
    asyncio.run(client._send_pending_payload("fixture", pending, frame, owner="fixture"))
    token.write_text("FILE-replay")
    pending.in_flight = False
    pending.reconnect_waiting = True
    asyncio.run(client._resume_pending_after_reconnect())
    assert [json.loads(raw)["stream_token"] for raw in socket.raw] == ["FILE-first-send", "FILE-replay"]
    assert b.CREDENTIAL_KEYS.isdisjoint(pending.payload)


def test_retry_loop_does_not_retry_after_open_connection_credential_failure(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("FILE-sentinel")
    token.chmod(0o600)
    monkeypatch.setattr(os, "environ", {"AGENT_ORCH_STREAM_ID": "altum-bot-cd", "AGENT_ORCH_STREAM_TOKEN_FILE": str(token)})
    sockets = []
    class LoseCredential(Socket):
        async def send(self, raw):
            await super().send(raw)
            if json.loads(raw)["type"] == "hello":
                token.unlink()
    def connect(*args, **kwargs):
        socket = LoseCredential()
        sockets.append(socket)
        return Connection(socket)
    monkeypatch.setattr(w.websockets, "connect", connect)
    cfg = config.Config("ws://fixture.invalid", "OPERATOR-sentinel", "fixture", tmp_path)
    with pytest.raises(b.ServiceCredentialUnavailable):
        asyncio.run(w._one_shot_rpc(cfg, {"type": "thread.read", "request_id": "fixture"}, prefix="thread", timeout=1))
    assert len(sockets) == 1 and sockets[0].closed
    assert [json.loads(raw)["type"] for raw in sockets[0].raw] == ["hello"]


@pytest.mark.parametrize("value,seat", [("READER-sentinel", False), (" READER-sentinel ", False),
                                      ("", False), ("  ", False), ("READER-sentinel", True)])
def test_reader_real_preargparse_cli_contract(monkeypatch, tmp_path, capsys, value, seat):
    env = {"HOME": str(tmp_path), "AGENT_ORCH_HOST_ID": "fixture", "PENTACLE_WORK_REPORT_READER_TOKEN": value}
    if seat:
        env["AGENT_ORCH_STREAM_ID"] = "fixture:seat"
    monkeypatch.setattr(os, "environ", env)
    sockets = []
    def connect(*args, **kwargs):
        socket = Socket()
        sockets.append(socket)
        return Connection(socket)
    monkeypatch.setattr(w.websockets, "connect", connect)
    valid = bool(value.strip()) and not seat
    assert cli.main(["report", "get", "fixture", "--json"]) == (0 if valid else 1)
    output = capsys.readouterr()
    assert not output.err and "sentinel" not in output.out
    if valid:
        hello, request = [json.loads(raw) for raw in sockets[0].raw if json.loads(raw)["type"] != "pong"]
        assert hello["stream_token"] == value
        assert "token" not in hello
        assert set(request) == {"type", "request_id", "report_id"}
    else:
        assert not sockets
        assert json.loads(output.out)["error_code"] == "unavailable"


@pytest.mark.parametrize("state", ["missing-file", "empty-file", "unreadable-file", "empty-env"])
def test_thread_unusable_seat_source_precedes_operator_loader(monkeypatch, tmp_path, capsys, state):
    token = tmp_path / "seat-token"
    env = {"HOME": str(tmp_path), "AGENT_ORCH_HOST_ID": "fixture"}
    if state == "empty-env":
        env["AGENT_ORCH_STREAM_TOKEN"] = ""
    else:
        env["AGENT_ORCH_STREAM_TOKEN_FILE"] = str(token)
        if state != "missing-file":
            token.write_text("" if state == "empty-file" else "FILE-sentinel")
            token.chmod(0o600)
    monkeypatch.setattr(os, "environ", env)
    reads = []
    def operator_loader(*args):
        reads.append(True)
        raise PermissionError("operator file unreadable")
    monkeypatch.setattr(b, "_file_token", operator_loader)
    if state == "unreadable-file":
        original = Path.read_text
        def read(self, *args, **kwargs):
            if self == token:
                raise PermissionError("seat file unreadable")
            return original(self, *args, **kwargs)
        monkeypatch.setattr(Path, "read_text", read)
    socket = Socket()
    monkeypatch.setattr(w.websockets, "connect", lambda *args, **kwargs: Connection(socket))
    assert cli.main(["thread", "fixture:child", "--timeout", "1"]) == 2
    output = capsys.readouterr()
    assert not output.out
    assert output.err == "agent-orch thread: unauthorized (verified seat token required)\n"
    assert not reads
    assert not socket.raw and socket.closed


@pytest.mark.parametrize("token", [None, "", "LEGACY-sentinel"])
def test_migrated_tool_policy_keeps_nullable_wire_token(monkeypatch, tmp_path, token):
    monkeypatch.setattr(os, "environ", {} if token is None else {"AGENT_ORCH_STREAM_TOKEN": token})
    cfg = config.Config("ws://fixture.invalid", "", "fixture", tmp_path)
    frame = b.defer_stream_token({"type": "fixture", "request_id": "fixture", "from_stream_id": "fixture:seat"},
                                 keep=False, include_empty=True)
    assert "stream_token" not in frame
    socket = Socket()
    asyncio.run(b.send_frame(socket, frame, config=cfg))
    assert json.loads(socket.raw[0])["stream_token"] == (token or None)


def test_uncredentialed_frame_check_survives_python_optimization():
    program = """
import asyncio
from agent_orch.send_boundary import send_uncredentialed
class Socket:
    async def send(self, raw):
        raise RuntimeError('forbidden send reached')
try:
    asyncio.run(send_uncredentialed(Socket(), {'type': 'pong', 'token': 'synthetic'}))
except AssertionError:
    pass
else:
    raise RuntimeError('credential check missing')
"""
    result = subprocess.run([sys.executable, "-O", "-c", program],
                            env={"PYTHONPATH": str(Path(__file__).parents[1])}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
