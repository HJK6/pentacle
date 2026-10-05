"""Portable, fixture-backed counterparts for every changed host-dependent path."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

FIXTURES = Path(__file__).parent / "fixtures/h2_capabilities"
pytestmark = pytest.mark.timeout(20)


def test_recorded_descendant_tree_and_complete_argv(monkeypatch):
    import test_harness_reaping as helper
    calls = []
    def output(args, **kwargs):
        calls.append(args)
        assert kwargs["timeout"] == 2
        return (FIXTURES / "process_rows.txt").read_text()
    monkeypatch.setattr(helper, "subprocess", SimpleNamespace(check_output=output))
    assert helper._descendant_commands(42420) == ["python synthetic-daemon --disable-hosts", "python synthetic-stub-cli"]
    assert calls == [["ps", "axww", "-o", "pid=,ppid=,command="]]
    class MissingProc:
        def is_file(self): return False
    monkeypatch.setattr(helper, "Path", lambda path: MissingProc())
    expected = "python " + "long-synthetic-argument " * 100
    helper.subprocess.check_output = lambda args, **kwargs: expected
    assert helper._process_command(42420) == expected


def owner_fixture(monkeypatch, tmp_path):
    from tools import gate_owner_manifest as owner
    actual_os = owner.os
    class OwnedOS:
        def __getattr__(self, name): return getattr(actual_os, name)
        def getpgrp(self): return 42419
        def getpgid(self, pid):
            assert pid == 42420
            return 42420
    monkeypatch.setattr(owner, "os", OwnedOS())
    identities = {os.getpid(): "owner-start", 42420: "victim-start"}
    monkeypatch.setattr(owner, "process_start_identity", lambda pid: identities.get(pid))
    monkeypatch.setattr(owner, "_exists", lambda pid: pid in identities)
    signals = []
    monkeypatch.setattr(owner, "_reap_group", signals.append)
    transports = []
    class FakeProcess:
        def communicate(self, timeout):
            assert timeout == 3
            return b"", b""
    def popen(args, **kwargs):
        assert args[:2] == ["tmux", "-S"] and args[2].startswith(str(tmp_path))
        transports.append(args)
        return FakeProcess()
    monkeypatch.setattr(owner, "_REAL_POPEN", popen)
    return owner, identities, signals, transports


@pytest.mark.parametrize("recorded,run_id,expected", [("victim-start", "owned-run", True), ("recycled-start", "owned-run", False), (None, "owned-run", False), ("", "owned-run", False), ("victim-start", "foreign-run", False)])
def test_recorded_identity_reaping_matches_missing_recycled_and_foreign_cases(monkeypatch, tmp_path, recorded, run_id, expected):
    owner, identities, signals, transports = owner_fixture(monkeypatch, tmp_path)
    sock = tmp_path / "synthetic.sock"; sock.touch()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema": owner.MANIFEST_SCHEMA, "version": owner.MANIFEST_VERSION,
        "owner_pid": os.getpid(), "owner_start_identity": "owner-start", "owner_state": "stopping", "run_id": "owned-run",
        "entries": [{"pgid": 42420, "leader_pid": 42420, "leader_start_identity": recorded, "run_id": run_id, "socket": str(sock), "socket_path": str(sock)}]}))
    owner.reap_manifest(manifest, "owned-run")
    assert signals == ([42420] if expected else [])
    assert sock.exists() is not expected
    assert len(transports) == int(expected)
    if run_id == "foreign-run": assert manifest.exists()


def test_recording_rejects_missing_identity_and_own_group_with_frozen_processes(monkeypatch, tmp_path):
    owner, identities, signals, transports = owner_fixture(monkeypatch, tmp_path)
    manifest = tmp_path / "manifest.json"
    owner.initialize_manifest(manifest, "owned-run")
    identities.pop(42420)
    with pytest.raises(owner.ManifestError, match="requires a group leader and start identity"):
        owner.record(manifest, 42420, None, leader_pid=42420)
    with pytest.raises(owner.ManifestError, match="own process group"):
        owner.record(manifest, 42419, None, leader_pid=os.getpid())
    assert not signals and not transports


def test_hermetic_daemon_launch_and_owned_shutdown_use_fake_process_boundary(monkeypatch, tmp_path):
    from tests.soak import harness
    calls = []; terminated = []
    monkeypatch.setenv("PENTACLE_MACHINES_JSON", '{"machines":[{"name":"synthetic-remote","ssh_target":"synthetic@example.com"}]}')
    monkeypatch.setenv("SSH_AUTH_SOCK", "/synthetic/ambient.sock")
    monkeypatch.setenv("PENTACLE_USAGE_PROBE_HOST", "synthetic-remote")
    monkeypatch.setattr(harness, "shutil", SimpleNamespace(which=lambda name: "/synthetic/tmux"))
    namespace = harness.TmuxNamespace(tmp_path)
    namespace.socket_path = tmp_path / "owned-tmux.sock"
    class FakeProcess:
        pid = 42420
        returncode = None
        def poll(self): return self.returncode
    child = FakeProcess()
    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        kwargs["stdout"].write(b"listening on 127.0.0.1:42420\n")
        return child
    def run(argv, **kwargs):
        assert argv[:2] == ["/synthetic/tmux", "-L"]
        assert kwargs["timeout"] == 5
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(harness, "subprocess", SimpleNamespace(Popen=popen, run=run, STDOUT=-2))
    monkeypatch.setattr(harness, "leader_pgid", lambda pid: pid)
    monkeypatch.setattr(harness, "_record_owned", lambda *args, **kwargs: None)
    def terminate(proc, **kwargs): terminated.append(proc.pid); proc.returncode = 0
    monkeypatch.setattr(harness, "terminate_process_group", terminate)
    daemon = harness.Daemon(str(tmp_path / "synthetic.db"), namespace)
    try:
        assert daemon.start() >= 0
        argv, kwargs = calls[0]
        assert "--disable-hosts" in argv and "--disable-usage-state-publisher" in argv
        assert str(harness.STUB_CLI.resolve()) in argv
        assert all(key not in kwargs["env"] for key in ("PENTACLE_MACHINES_JSON", "SSH_AUTH_SOCK", "PENTACLE_USAGE_PROBE_HOST"))
        assert json.loads(Path(kwargs["env"]["PENTACLE_MACHINES_FILE"]).read_text()) == {"machines": [{"name": "soakhost"}]}
        assert daemon.port == 42420
        daemon.kill(); namespace.kill_server()
        assert terminated == [42420]
    finally:
        if daemon._logf is not None: daemon._logf.close()


@pytest.mark.parametrize("access,expected", [("r", 0), ("w", 1)])
def test_recorded_descriptor_preserves_real_first_bind_parser(monkeypatch, tmp_path, access, expected):
    import ingest as module
    from ingest import Ingest, _StreamIngest, _close_stream
    from sessions import Sessions
    from store import Store
    target = tmp_path / ".codex/sessions/synthetic.jsonl"; target.parent.mkdir(parents=True)
    target.write_bytes((FIXTURES / "transcript.jsonl").read_bytes())
    stat = target.stat()
    lsof = (FIXTURES / "descriptor_template.txt").read_text().format(access=access, device=hex(stat.st_dev), inode=stat.st_ino, path=target)
    observed = []
    async def execute(*args, **kwargs):
        observed.append(args)
        assert args == ("lsof", "-p", "42420", "-FpfatDin")
        return 0, lsof
    async def record(pid): return {"pid": 42420, "uid": os.getuid(), "start_id": "Mon Oct 5 12:00:00 2026", "command": "/synthetic/python fixture-provider"}
    async def tree(pid): return ["42420"]
    monkeypatch.setattr(module, "_exec", execute)
    monkeypatch.setattr(module, "process_record", record)
    monkeypatch.setattr(module, "process_tree", tree)
    async def scenario():
        store = Store(":memory:"); store.start()
        class Tmux:
            async def pane_pid(self, name): return "42420"
        state = _StreamIngest()
        try:
            sessions = Sessions(store, local_host="synthetic")
            await sessions.open("synthetic", "case", provider="codex", pane_pid="42420",
                observer_binding={"executable": "/synthetic/python", "pane_started_at": "Mon Oct 5 12:00:00 2026"})
            ingest = Ingest(store, sessions, Tmux(), lambda frame: asyncio.sleep(0), local_host="synthetic", recent_limit=500)
            assert await ingest._ingest_stream(sessions.get("synthetic:case"), state, 500) == expected
            assert len(await store.fetch_session_event_tail("synthetic:case", limit=500)) == expected
        finally: _close_stream(state); store.stop()
    asyncio.run(scenario())
    assert observed


def test_recorded_process_birth_reaches_real_spawn_normalization(monkeypatch):
    import prockill
    from spawnctl import SpawnCtl
    from sessions import Sessions
    from store import Store
    payload = (FIXTURES / "ps_birth.txt").read_text().format(uid=os.getuid()).encode()
    class Process:
        returncode = 0
        async def communicate(self): return payload, b""
    async def spawn(*args, **kwargs):
        assert args == ("ps", "-ww", "-p", "42420", "-o", "pid=,uid=,lstart=,command=")
        return Process()
    monkeypatch.setattr(prockill.asyncio, "create_subprocess_exec", spawn)
    async def scenario():
        store = Store(":memory:"); store.start()
        try:
            ctl = SpawnCtl(store, Sessions(store, local_host="synthetic"))
            assert await ctl._pane_started_at("42420", host="synthetic") == "Mon Oct 5 12:00:00 2026"
        finally: store.stop()
    asyncio.run(scenario())


def test_in_process_ceremony_uses_synthetic_signatures_without_children(tmp_path, monkeypatch):
    import asyncio as aio
    import subprocess as sub
    from test_consent import Ceremony, refused
    from test_lifecycle_authority import scenario
    import store_consent as consent
    def forbidden(*args, **kwargs): raise AssertionError("pure ceremony must not spawn")
    async def forbidden_async(*args, **kwargs): raise AssertionError("pure ceremony must not spawn")
    monkeypatch.setattr(sub, "Popen", forbidden)
    monkeypatch.setattr(aio, "create_subprocess_exec", forbidden_async)
    async def check(env):
        c = Ceremony(env, tmp_path)
        await env.open("bart", role="lead")
        await refused(env.lifecycle(c.auth, "designate", target="bart"), "consent_no_active_key")
        await c.enroll()
        challenge = await c.request()
        signature = c.sign(consent.decode(challenge["challenge_bytes"]))
        result = await c.call("consent.approve", challenge_id=challenge["challenge_id"], key_id=c.key_id, signature=signature)
        assert result["receipt"]["consent_id"] == challenge["challenge_id"]
        replay = await c.call("consent.approve", challenge_id=challenge["challenge_id"], key_id=c.key_id, signature=signature)
        assert replay["replayed"] is True and (await env.grant())["revision"] == 1
        c.registry.revoke(c.cid)
        await refused(c.call("consent.status", challenge_id=challenge["challenge_id"]), "consent_principal_invalid")
    scenario(check)


def test_owner_signal_cleanup_sequence_with_injected_resources(monkeypatch, tmp_path):
    """Execute the real owner setup/handler with fake resources; omit only idle wait."""
    import ast
    import signal
    import sys
    import types
    import test_harness_reaping as source
    calls = []; handlers = {}; printed = []
    class Namespace:
        socket = "synthetic-owned-socket"
        def __init__(self, root): assert root == tmp_path
        def start(self): calls.append("namespace_start")
        def kill_server(self): calls.append("namespace_kill")
    class Daemon:
        proc = SimpleNamespace(pid=42420)
        def __init__(self, db, namespace): pass
        def start(self): calls.append("daemon_start")
        def kill(self): calls.append("daemon_kill")
    fake_harness = types.ModuleType("tests.soak.harness"); fake_harness.Daemon = Daemon; fake_harness.TmuxNamespace = Namespace
    fake_owner = types.ModuleType("tools.gate_owner_manifest")
    fake_owner.initialize_manifest = lambda path: calls.append("initialize")
    fake_owner.mark_owner_stopping = lambda path: calls.append("mark_stopping")
    fake_owner.reap_manifest = lambda path: calls.append("reap")
    monkeypatch.setitem(sys.modules, "tests.soak.harness", fake_harness)
    monkeypatch.setitem(sys.modules, "tools.gate_owner_manifest", fake_owner)
    monkeypatch.setattr(sys, "argv", ["synthetic-owner", str(tmp_path), str(tmp_path / "manifest.json")])
    monkeypatch.setattr(signal, "signal", lambda number, handler: handlers.setdefault(number, handler))
    monkeypatch.setattr(os, "getpgid", lambda pid: 42420)
    tree = ast.parse(source.OWNER_SCRIPT)
    assert isinstance(tree.body[-1], ast.While)  # wait is not part of cleanup logic
    tree.body.pop()
    namespace = {"print": lambda value, **kwargs: printed.append(json.loads(value))}
    exec(compile(tree, "synthetic-owner-fixture", "exec"), namespace)
    with pytest.raises(SystemExit) as ended: handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert ended.value.code == 0
    assert calls == ["initialize", "namespace_start", "daemon_start", "mark_stopping", "daemon_kill", "namespace_kill", "reap"]
    assert printed[0]["daemon_pgid"] == 42420
