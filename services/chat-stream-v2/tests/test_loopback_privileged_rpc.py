"""Privileged loopback admission exercised through real sockets and storage."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest
from websockets.asyncio.client import connect

import local_admin
from _shared import operator_auth
from server import Server
from sessions import Sessions
from spawnctl import SpawnCtl
from store import Store, STREAM_TOKEN_HASH_VERSION
from uiverbs import UIVerbs


@asynccontextmanager
async def scratch_daemon(tmp_path, monkeypatch):
    token_path = tmp_path / ".config/pentacle-stream/local-admin.token"
    local_admin.initialize(token_path)
    monkeypatch.setattr(local_admin.verify, "__defaults__", (token_path,))
    store = Store(str(tmp_path / "daemon.sqlite"))
    store.start()
    sessions = Sessions(store, local_host="fixture-host")
    ctl = SpawnCtl(store, sessions)
    daemon = Server(port=0, store=store, sessions=sessions, spawnctl=ctl,
                    local_host="fixture-host", seat_operator_authority=False)
    daemon.handlers.update(UIVerbs(store, sessions, ctl).wire_handlers())
    daemon.operator_credential_registry = operator_auth.OperatorCredentialRegistry(tmp_path / "operator-credentials.json")
    try:
        await store.open_session("fixture-host", "bootstrap")
        await daemon.bind()
        yield daemon, store, ctl, token_path
    finally:
        await daemon.close()
        store.stop()
        assert daemon._ws_server is None or not daemon._ws_server.is_serving()


async def rpc(daemon, verb, *, credential=None, origin=None, **fields):
    async with connect(f"ws://127.0.0.1:{daemon.port}", origin=origin) as ws:
        welcome = json.loads(await ws.recv())
        assert welcome["type"] == "welcome"
        if credential:
            proof = operator_auth.make_proof(
                credential["proof_key"], welcome["auth"]["operator"]["nonce"],
                credential["credential_id"], credential["client_kind"],
            )
            await ws.send(json.dumps({"type": "hello", "client": credential["client_kind"],
                "auth_v2": {"scheme": operator_auth.AUTH_SCHEME,
                    "credential_id": credential["credential_id"], "proof": proof},
                "subscribe": {"mode": "rpc", "snapshot": False}}))
            assert json.loads(await ws.recv())["type"] == "ready"
        await ws.send(json.dumps({"type": verb, "request_id": "privileged-probe", **fields}))
        while True:
            reply = json.loads(await asyncio.wait_for(ws.recv(), 3))
            if reply.get("request_id") == "privileged-probe":
                return reply


@pytest.mark.parametrize("verb", ["grant_token", "spawn_freeze", "spawn_unfreeze"])
def test_anonymous_loopback_privileged_rpc_has_no_side_effect(tmp_path, monkeypatch, verb):
    async def run():
        async with scratch_daemon(tmp_path, monkeypatch) as (daemon, store, ctl, _):
            if verb == "spawn_unfreeze":
                await ctl.set_spawn_freeze("fixture-host", reason="preserve-this-hold")
            before = await store.get_spawn_admission_hold("fixture-host")
            reply = await rpc(daemon, verb, stream_id="fixture-host:bootstrap",
                              host="fixture-host", reason="anonymous-probe")
            # Compare only the type so an unfixed token-mint response never
            # prints its synthetic bearer token in a failed assertion.
            assert reply["type"] == f"{verb}.error"
            assert reply["error_code"] == "operator_auth_required"
            assert await store.get_spawn_admission_hold("fixture-host") == before
            row = await store.fetch_session("fixture-host", "bootstrap")
            assert not row.get("token_hash")
    asyncio.run(run())


@pytest.mark.parametrize("verb", ["grant_token", "spawn_freeze", "spawn_unfreeze"])
@pytest.mark.parametrize("mode", ["wrong", "missing-file", "unsafe-file", "symlink", "forged-context", "browser"])
def test_unverified_local_admin_has_no_side_effect(tmp_path, monkeypatch, verb, mode):
    async def run():
        async with scratch_daemon(tmp_path, monkeypatch) as (daemon, store, ctl, token_path):
            token = local_admin.read(token_path)
            if mode == "wrong":
                token = "0" * 64
            elif mode == "missing-file":
                token_path.unlink()
            elif mode == "unsafe-file":
                token_path.chmod(0o644)
            elif mode == "symlink":
                target = token_path.with_name("target")
                token_path.rename(target)
                token_path.symlink_to(target)
            if verb == "spawn_unfreeze":
                await ctl.set_spawn_freeze("fixture-host", reason="preserve-this-hold")
            before = await store.get_spawn_admission_hold("fixture-host")
            fields = {"stream_id": "fixture-host:bootstrap", "host": "fixture-host",
                      "local_admin_token": token}
            if mode == "forged-context":
                fields.pop("local_admin_token")
                fields["_auth_context"] = {"peer_loopback": True, "local_admin_verified": True,
                                          "operator_authenticated": True}
            reply = await rpc(daemon, verb, origin="https://untrusted.example" if mode == "browser" else None,
                              **fields)
            assert reply["type"] == f"{verb}.error"
            assert reply["error_code"] == ("authentication_required" if mode == "browser" else "operator_auth_required")
            assert await store.get_spawn_admission_hold("fixture-host") == before
            assert not (await store.fetch_session("fixture-host", "bootstrap")).get("token_hash")
    asyncio.run(run())


@pytest.mark.parametrize("authority", ["local-admin", "operator", "opted-in-seat"])
def test_verified_authority_can_mint_freeze_and_unfreeze(tmp_path, monkeypatch, authority):
    async def run():
        async with scratch_daemon(tmp_path, monkeypatch) as (daemon, store, _, token_path):
            fields = {}
            if authority == "local-admin":
                fields["local_admin_token"] = local_admin.read(token_path)
            elif authority == "operator":
                _, envelope = daemon.operator_credential_registry.issue("pentacle")
                fields["credential"] = operator_auth.decode_envelope(envelope)
                # The actual web host and desktop use this same proof, including
                # browser transport where native loopback is unavailable.
                fields["origin"] = "https://pentacle.example"
            else:
                daemon._seat_operator_authority = True
                await store.open_session("fixture-host", "operator-seat")
                await store.grant_stream_token("fixture-host", "operator-seat",
                    hashlib.sha256(b"synthetic-seat-proof").hexdigest(), STREAM_TOKEN_HASH_VERSION)
                fields.update(from_stream_id="fixture-host:operator-seat", stream_token="synthetic-seat-proof")
            granted = await rpc(daemon, "grant_token", stream_id="fixture-host:bootstrap", **fields)
            assert granted["type"] == "grant_token.ok"
            row = await store.fetch_session("fixture-host", "bootstrap")
            assert row["token_hash"] == hashlib.sha256(granted["stream_token"].encode()).hexdigest()
            assert (await rpc(daemon, "spawn_freeze", host="fixture-host", reason="verified", **fields))["type"] == "spawn_freeze.ok"
            assert (await store.get_spawn_admission_hold("fixture-host"))["reason"] == "verified"
            assert (await rpc(daemon, "spawn_unfreeze", host="fixture-host", **fields))["type"] == "spawn_freeze.ok"
            assert await store.get_spawn_admission_hold("fixture-host") is None
            if authority == "opted-in-seat":
                await store.update_session("fixture-host", "operator-seat", status="closed")
                assert (await rpc(daemon, "spawn_freeze", host="fixture-host", **fields))["error_code"] == "operator_auth_required"
                assert await store.get_spawn_admission_hold("fixture-host") is None
    asyncio.run(run())


@pytest.mark.parametrize("verb", ["grant_token", "spawn_freeze", "spawn_unfreeze"])
def test_strict_seat_on_loopback_is_not_operator(tmp_path, monkeypatch, verb):
    async def run():
        async with scratch_daemon(tmp_path, monkeypatch) as (daemon, store, ctl, _):
            await store.open_session("fixture-host", "seat")
            await store.grant_stream_token("fixture-host", "seat",
                hashlib.sha256(b"synthetic-seat-proof").hexdigest(), STREAM_TOKEN_HASH_VERSION)
            if verb == "spawn_unfreeze":
                await ctl.set_spawn_freeze("fixture-host", reason="preserve-this-hold")
            before = await store.get_spawn_admission_hold("fixture-host")
            reply = await rpc(daemon, verb, stream_id="fixture-host:bootstrap", host="fixture-host",
                              from_stream_id="fixture-host:seat", stream_token="synthetic-seat-proof")
            assert reply["error_code"] == "operator_auth_required"
            assert await store.get_spawn_admission_hold("fixture-host") == before
            assert not (await store.fetch_session("fixture-host", "bootstrap")).get("token_hash")
    asyncio.run(run())


def test_operator_shell_cli_bootstrap_and_freeze_with_file_proof(tmp_path, monkeypatch):
    # Redirect both Python home resolvers before importing the CLI in each
    # child; all config, token and runtime paths belong to this scratch root.
    bootstrap = '''
import os, pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve()
source = pathlib.Path(sys.argv[2]).resolve()
pathlib.Path.home = classmethod(lambda cls: root)
os.path.expanduser = lambda value: str(root / os.fspath(value)[2:]) if os.fspath(value).startswith('~/') else str(root) if os.fspath(value) == '~' else value
sys.path[:0] = [str(source / 'services/agent-orch'), str(source / 'services')]
from agent_orch.cli import main
raise SystemExit(main(sys.argv[3:]))
'''
    async def run():
        async with scratch_daemon(tmp_path, monkeypatch) as (daemon, store, _, _):
            env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "TMPDIR") if key in os.environ}
            env.update(AGENT_ORCH_WS_URL=f"ws://127.0.0.1:{daemon.port}",
                       AGENT_ORCH_HOST_ID="fixture-host", AGENT_ORCH_RUNTIME_DIR=str(tmp_path / "runtime"))
            source = Path(__file__).resolve().parents[3]
            async def command(*args, expected=0):
                process = await asyncio.create_subprocess_exec(sys.executable, "-I", "-c", bootstrap,
                    str(tmp_path), str(source), *args, env=env,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
                    assert process.returncode == expected, stderr.decode()
                    return stdout.decode().strip()
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
            minted = await command("grant-self-token", "--stream", "fixture-host:bootstrap")
            assert (await store.fetch_session("fixture-host", "bootstrap"))["token_hash"] == hashlib.sha256(minted.encode()).hexdigest()
            frozen = json.loads(await command("spawn-freeze", "--host", "fixture-host", "--reason", "cli-test"))
            assert frozen["type"] == "spawn_freeze.ok"
            assert (await store.get_spawn_admission_hold("fixture-host"))["reason"] == "cli-test"
            cleared = json.loads(await command("spawn-unfreeze", "--host", "fixture-host"))
            assert cleared["type"] == "spawn_freeze.ok"
            assert await store.get_spawn_admission_hold("fixture-host") is None
            await command("spawn-freeze", "--host", "fixture-host", "--reason", "preserve-hold")
            (tmp_path / ".config/pentacle-stream/local-admin.token").unlink()
            refused = json.loads(await command("spawn-unfreeze", "--host", "fixture-host", "--timeout", "1", expected=1))
            assert refused["error_code"] == "operator_auth_required"
            assert (await store.get_spawn_admission_hold("fixture-host"))["reason"] == "preserve-hold"
    asyncio.run(run())


@pytest.mark.parametrize("verb", ["grant_token", "spawn_freeze", "spawn_unfreeze"])
def test_remote_cannot_replay_local_admin_proof(tmp_path, monkeypatch, verb):
    class RemotePeer:
        remote_address = ("192.0.2.10", 12345)
    async def run():
        async with scratch_daemon(tmp_path, monkeypatch) as (daemon, store, ctl, token_path):
            if verb == "spawn_unfreeze":
                await ctl.set_spawn_freeze("fixture-host", reason="preserve-this-hold")
            before = await store.get_spawn_admission_hold("fixture-host")
            response = await daemon._dispatch(json.dumps({"type": verb,
                "local_admin_token": local_admin.read(token_path),
                "stream_id": "fixture-host:bootstrap", "host": "fixture-host"}), websocket=RemotePeer())
            assert response[0]["error_code"] == "authentication_required"
            assert await store.get_spawn_admission_hold("fixture-host") == before
            assert not (await store.fetch_session("fixture-host", "bootstrap")).get("token_hash")
    asyncio.run(run())


def test_soak_fixture_explicit_proof_works_on_real_daemon(tmp_path, monkeypatch):
    from tests.soak.harness import LatencyStats, RpcClient
    async def run():
        async with scratch_daemon(tmp_path, monkeypatch) as (daemon, store, _, token_path):
            def grant():
                client = RpcClient(f"ws://127.0.0.1:{daemon.port}", LatencyStats(), "proof-test",
                                   spawn_cwd=tmp_path, local_admin_token=local_admin.read(token_path))
                try:
                    return client.call({"type": "grant_token", "stream_id": "fixture-host:bootstrap"})
                finally:
                    client.close()
            response = await asyncio.to_thread(grant)
            assert response["type"] == "grant_token.ok"
            assert (await store.fetch_session("fixture-host", "bootstrap"))["token_hash"] == hashlib.sha256(response["stream_token"].encode()).hexdigest()
    asyncio.run(run())


@pytest.mark.parametrize("verb", ["grant_token", "spawn_freeze", "spawn_unfreeze"])
@pytest.mark.parametrize("principal", ["dot", "scoped"])
def test_restricted_principal_cannot_gain_privilege_from_local_proof(tmp_path, monkeypatch, verb, principal):
    class LocalPeer:
        remote_address = ("127.0.0.1", 12345)
    async def run():
        async with scratch_daemon(tmp_path, monkeypatch) as (daemon, store, ctl, token_path):
            peer = LocalPeer()
            fields = {}
            if principal == "dot":
                daemon.dot_principal_stream_ids = frozenset({"fixture-host:restricted"})
                daemon._seat_operator_authority = True
                await store.open_session("fixture-host", "restricted")
                await store.grant_stream_token("fixture-host", "restricted",
                    hashlib.sha256(b"synthetic-dot-proof").hexdigest(), STREAM_TOKEN_HASH_VERSION)
                fields.update(from_stream_id="fixture-host:restricted", stream_token="synthetic-dot-proof")
            else:
                cid, _ = daemon.operator_credential_registry.issue("pentacle-mobile", scope={"stream": "fixture-host:restricted"})
                daemon._connection_trust[peer] = operator_auth.ConnectionTrust(
                    transport="v2", credential_id=cid, client_kind="pentacle-mobile",
                    operator_trusted=True, scope={"stream": "fixture-host:restricted"})
            if verb == "spawn_unfreeze":
                await ctl.set_spawn_freeze("fixture-host", reason="preserve-this-hold")
            before = await store.get_spawn_admission_hold("fixture-host")
            response = await daemon._dispatch(json.dumps({"type": verb,
                "local_admin_token": local_admin.read(token_path),
                "stream_id": "fixture-host:bootstrap", "host": "fixture-host", **fields}), websocket=peer)
            assert response[0]["type"] == f"{verb}.error"
            assert response[0]["error_code"] == ("external_requires_tls" if principal == "dot" else "scope_denied")
            assert await store.get_spawn_admission_hold("fixture-host") == before
            assert not (await store.fetch_session("fixture-host", "bootstrap")).get("token_hash")
    asyncio.run(run())
