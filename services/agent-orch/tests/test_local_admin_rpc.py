"""Local operator bootstrap proof is loaded only at the authorized egress."""
import asyncio
import json
import os
from pathlib import Path

import pytest

from agent_orch.config import Config, local_admin_token
from agent_orch import send_boundary as boundary


class Socket:
    def __init__(self):
        self.frames = []
        self.closed = False

    async def send(self, raw):
        self.frames.append(json.loads(raw))

    async def close(self):
        self.closed = True


@pytest.fixture
def local_proof(tmp_path, monkeypatch):
    for name in boundary.IDENTITY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    path = tmp_path / ".config/pentacle-stream/local-admin.token"
    path.parent.mkdir(parents=True)
    path.write_text("1" * 64 + "\n")
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("url", ["ws://127.0.0.1:1", "ws://[::1]:1", "ws://[::ffff:127.0.0.1]:1", "ws://localhost:1"])
@pytest.mark.parametrize("verb", ["grant_token", "spawn_freeze", "spawn_unfreeze"])
def test_operator_shell_attaches_file_proof_only_when_sending(local_proof, tmp_path, url, verb):
    cfg = Config(url, "", "fixture-host", tmp_path)
    queued = boundary.prepare_frame({"type": verb})
    assert "local_admin_token" not in queued
    # Rotation after construction proves this reads the actual send-time input.
    local_proof.write_text("2" * 64 + "\n")
    socket = Socket()
    asyncio.run(boundary.send_frame(socket, queued, config=cfg))
    assert socket.frames[0]["local_admin_token"] == "2" * 64
    assert "local_admin_token" not in queued


@pytest.mark.parametrize("kind", ["seat", "service", "ambiguous"])
@pytest.mark.parametrize("verb", ["grant_token", "spawn_freeze", "spawn_unfreeze"])
def test_other_identities_never_load_local_admin(local_proof, tmp_path, monkeypatch, kind, verb):
    service = next(iter(boundary.FIXED_SERVICE_IDENTITIES))
    monkeypatch.setenv("AGENT_ORCH_STREAM_ID", "fixture:seat" if kind == "seat" else service)
    if kind == "ambiguous":
        monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:conflict")
    if kind == "service":
        source = tmp_path / "service.token"
        source.write_text("synthetic-service-token")
        source.chmod(0o600)
        monkeypatch.setenv("AGENT_ORCH_STREAM_TOKEN_FILE", str(source))
    monkeypatch.setattr(boundary, "local_admin_token", lambda *_: pytest.fail("local-admin credential borrowed"))
    socket = Socket()
    cfg = Config("ws://127.0.0.1:1", "", "fixture-host", tmp_path)
    if kind == "ambiguous":
        with pytest.raises(boundary.ServiceCredentialUnavailable):
            asyncio.run(boundary.send_frame(socket, {"type": verb}, config=cfg))
        assert socket.closed and not socket.frames
    else:
        asyncio.run(boundary.send_frame(socket, {"type": verb}, config=cfg))
        assert "local_admin_token" not in socket.frames[0]


@pytest.mark.parametrize("url,verb", [("ws://192.0.2.1:1", "grant_token"),
    ("ws://not-local.example:1", "spawn_freeze"), ("ws://127.0.0.1:1", "spawn")])
def test_remote_and_ordinary_rpc_never_read_local_admin(local_proof, tmp_path, monkeypatch, url, verb):
    monkeypatch.setattr(boundary, "local_admin_token", lambda *_: pytest.fail("local-admin reader reached"))
    socket = Socket()
    asyncio.run(boundary.send_frame(socket, {"type": verb}, config=Config(url, "", "fixture-host", tmp_path)))
    assert "local_admin_token" not in socket.frames[0]


@pytest.mark.parametrize("mode", ["missing", "unsafe", "symlink", "malformed"])
def test_invalid_file_never_emits_local_admin_proof(local_proof, tmp_path, mode):
    if mode == "missing":
        local_proof.unlink()
    elif mode == "unsafe":
        local_proof.chmod(0o644)
    elif mode == "symlink":
        target = local_proof.with_name("target")
        local_proof.rename(target)
        local_proof.symlink_to(target)
    else:
        local_proof.write_text("invalid")
    socket = Socket()
    cfg = Config("ws://127.0.0.1:1", "", "fixture-host", tmp_path)
    if mode == "missing":
        asyncio.run(boundary.send_frame(socket, {"type": "grant_token"}, config=cfg))
        assert "local_admin_token" not in socket.frames[0]
    else:
        with pytest.raises((OSError, ValueError)):
            asyncio.run(boundary.send_frame(socket, {"type": "grant_token"}, config=cfg))
        assert not socket.frames


def test_explicit_reader_rejects_remote_before_file_access(local_proof, tmp_path, monkeypatch):
    monkeypatch.setattr(os, "open", lambda *_: pytest.fail("remote local-admin file read"))
    with pytest.raises(ValueError, match="loopback endpoint"):
        local_admin_token(Config("ws://192.0.2.1:1", "", "fixture-host", tmp_path))
