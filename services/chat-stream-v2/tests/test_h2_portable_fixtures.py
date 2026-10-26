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
    monkeypatch.setattr(owner, "_reap_group", lambda pgid, **kwargs: signals.append(pgid))
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
    if run_id == "foreign-run":
        owner.reap_manifest(manifest, "owned-run")
    else:
        with pytest.raises(owner.ManifestError, match="identity|ownership binding"):
            owner.reap_manifest(manifest, "owned-run")
    assert signals == ([42420] if expected else [])
    assert sock.exists(), "a legacy unbound endpoint must never be unlinked"
    assert not transports
    assert manifest.exists()


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
        assert argv[:3] == ["/synthetic/tmux", "-N", "-S"]
        assert kwargs["timeout"] == 5
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(harness, "subprocess", SimpleNamespace(Popen=popen, run=run, STDOUT=-2))
    monkeypatch.setattr(harness, "leader_pgid", lambda pid: pid)
    recorded = []
    monkeypatch.setattr(harness, "_record_owned", lambda *args, **kwargs: recorded.append((args, kwargs)))
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
        assert recorded == [((42420, None), {"leader_pid": 42420, "socket_owner": None})]
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
        await env.open("assistant", role="lead")
        await refused(env.lifecycle(c.auth, "designate", target="assistant"), "consent_no_active_key")
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


@pytest.mark.parametrize("dead_mode", [False, True])
def test_owner_signal_cleanup_sequence_with_injected_resources(monkeypatch, tmp_path, dead_mode):
    """Execute the real owner setup/handler with fake resources; omit only idle wait."""
    import ast
    import signal
    import sys
    import types
    import test_harness_reaping as source
    calls = []; handlers = {}; printed = []
    class Namespace:
        socket = "synthetic-owned-socket"
        socket_path = SimpleNamespace(is_socket=lambda: True)
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
    monkeypatch.setattr(sys, "argv", ["synthetic-owner", str(tmp_path), str(tmp_path / "manifest.json"), "dead-leader" if dead_mode else "live-leader"])
    from tools import run_gate as fixture_runner
    monkeypatch.setattr(fixture_runner, "_process_group_exists", lambda pgid: False)
    monkeypatch.setattr(signal, "signal", lambda number, handler: handlers.setdefault(number, handler))
    monkeypatch.setattr(os, "getpgid", lambda pid: 42420)
    tree = ast.parse(source.OWNER_SCRIPT)
    assert isinstance(tree.body[-1], ast.While)  # wait is not part of cleanup logic
    tree.body.pop()
    namespace = {"print": lambda value, **kwargs: printed.append(json.loads(value))}
    exec(compile(tree, "synthetic-owner-fixture", "exec"), namespace)
    with pytest.raises(SystemExit) as ended: handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert ended.value.code == 0
    assert calls == (["initialize", "namespace_start", "daemon_start"] + (["daemon_kill"] if dead_mode else [])
                     + ["mark_stopping", "daemon_kill", "namespace_kill", "reap"])
    assert printed[0]["daemon_pgid"] == 42420


@pytest.mark.parametrize("outcome", ["complete", "kill-complete", "delayed", "denied", "unknown", "identity-lost", "dead-with-survivor", "terminal-complete", "terminal-survivor"])
def test_recorded_live_group_requires_terminal_absence(monkeypatch, outcome):
    """Frozen OS/event boundary exercises the real group and entry reapers."""
    import signal
    from tools import gate_owner_manifest as owner
    state = dict(live=outcome != "dead-with-survivor", group=True, registered=False, socket=False, clock=0)
    signals, observations = [], []
    class OwnedOS:
        def getpgrp(self): return 42419
        def getpgid(self, pid): return 42420
        def killpg(self, pgid, signum):
            assert pgid == 42420
            if signum == 0:
                if outcome == "unknown": raise PermissionError("synthetic unreadable group")
                observations.append(state["group"])
                if not state["group"]: raise ProcessLookupError()
                return
            assert state["registered"]
            signals.append(signum)
            if outcome == "denied": raise PermissionError("synthetic denied TERM")
            if outcome == "complete" or (outcome == "kill-complete" and signum == signal.SIGKILL):
                state.update(live=False, group=False)
            elif outcome == "identity-lost": state["live"] = False
    class Event:
        terminal = False
        def __init__(self, pid):
            assert pid == 42420
            state["registered"] = True
            self.terminal = outcome.startswith("terminal-")
            if outcome == "terminal-complete": state.update(live=False, group=False)
        def wait(self, deadline):
            if state["live"] and not self.terminal: raise owner.ManifestError("synthetic delayed exit")
        def close(self): state["registered"] = False
    def clock():
        state["clock"] += .1
        return state["clock"]
    monkeypatch.setattr(owner, "os", OwnedOS())
    monkeypatch.setattr(owner, "_exists", lambda pid: state["live"])
    monkeypatch.setattr(owner, "process_start_identity", lambda pid: "recorded-start")
    monkeypatch.setattr(owner, "_ExitEvent", Event)
    monkeypatch.setattr(owner, "time", SimpleNamespace(monotonic=clock, sleep=lambda seconds: None))
    monkeypatch.setattr(owner, "_reap_socket", lambda *args: state.update(socket=True))
    entry = dict(pgid=42420, leader_pid=42420, leader_start_identity="recorded-start",
                 socket="/synthetic/bound/socket", socket_owner={"synthetic": True}, run_id="run")
    data = {"entries": [entry]}
    complete = outcome in ("complete", "kill-complete", "terminal-complete")
    if complete:
        owner._reap_entries(data, "run")
    else:
        with pytest.raises(owner.ManifestError): owner._reap_entries(data, "run")
    assert state["socket"], "group failure must not strand an independently bound socket"
    assert data["entries"] == ([] if complete else [entry])
    assert not state["registered"]
    if outcome in ("dead-with-survivor", "terminal-complete", "terminal-survivor"): assert not signals
    else: assert signals[0] == signal.SIGTERM
    if outcome == "identity-lost": assert signals == [signal.SIGTERM]
    if complete: assert observations[-1] is False


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("outcome", ["complete", "delayed", "denied"])
def test_recorded_kernel_exit_events_are_finite_and_fail_closed(monkeypatch, platform, outcome):
    from tools import gate_owner_manifest as owner
    calls, closed = [], []
    def pidfd_open(pid, flags):
        calls.append(("pidfd", pid, flags))
        if outcome == "denied": raise PermissionError("synthetic pidfd denial")
        return 42421
    def control(changes, count, timeout):
        calls.append(("control", changes, count, timeout))
        if outcome == "denied": raise PermissionError("synthetic kqueue denial")
        return [SimpleNamespace(ident=42420, filter=-5, flags=0, fflags=16)] if changes is None and outcome == "complete" else []
    def wait(read, write, error, timeout):
        calls.append(("select", read, timeout))
        return (read if outcome == "complete" else [], [], [])
    monkeypatch.setattr(owner, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(owner, "os", SimpleNamespace(pidfd_open=pidfd_open, close=closed.append))
    monkeypatch.setattr(owner, "select", SimpleNamespace(
        select=wait, kqueue=lambda: SimpleNamespace(control=control, close=lambda: closed.append("kqueue")),
        kevent=lambda pid, **kwargs: (pid, kwargs), KQ_FILTER_PROC=-5, KQ_EV_ADD=1,
        KQ_EV_ENABLE=4, KQ_NOTE_EXIT=16, KQ_EV_ERROR=32))
    if outcome == "denied":
        with pytest.raises(owner.ManifestError, match="register"): owner._ExitEvent(42420)
    else:
        event = owner._ExitEvent(42420)
        try:
            if outcome == "complete": event.wait(owner.time.monotonic() + 5)
            else:
                with pytest.raises(owner.ManifestError, match="not observed"):
                    event.wait(owner.time.monotonic() + 5)
        finally: event.close()
        assert closed == ([42421] if platform == "linux" else ["kqueue"])
        assert len([x for x in calls if x[0] == ("select" if platform == "linux" else "control")]) == (1 if platform == "linux" else 2)


@pytest.mark.parametrize("outcome", ["complete", "false-guard", "client-error", "event-timeout", "denied-event", "replacement", "cancellation", "recycled-server", "root-mode", "replacement-at-exit", "root-replacement-at-exit"])
def test_bound_socket_controls_preserve_unresolved_identity(monkeypatch, tmp_path, outcome):
    import shutil
    import socket
    import tempfile
    import uuid
    from tools import gate_owner_manifest as owner
    root = Path(tempfile.mkdtemp(prefix="v2s-", dir="/tmp")); root.chmod(0o700)
    address = root / "s"; endpoint = socket.socket(socket.AF_UNIX); endpoint.bind(str(address))
    rs, ss = root.stat(), address.lstat(); nonce = uuid.uuid4().hex
    binding = dict(nonce=nonce, server_pid=42421, server_start_identity="server-start",
                   root_dev=rs.st_dev, root_ino=rs.st_ino, socket_dev=ss.st_dev, socket_ino=ss.st_ino)
    state, clients, events = {"live": True}, [], []
    class Event:
        terminal = False
        def __init__(self, pid):
            events.append("register")
            if outcome == "denied-event": raise owner.ManifestError("synthetic event denied")
            if outcome == "replacement":
                address.unlink()
                address.write_text("foreign replacement")
        def wait(self, deadline):
            events.append("wait")
            if outcome == "event-timeout": raise owner.ManifestError("synthetic no exit event")
            if outcome == "cancellation": raise KeyboardInterrupt()
            state["live"] = False
            if outcome == "replacement-at-exit":
                address.unlink()
                address.write_text("foreign replacement after exit")
            if outcome == "root-replacement-at-exit":
                root.rename(str(root) + "-original")
                root.mkdir(mode=0o700)
                address.write_text("foreign root replacement")
        def close(self): events.append("close")
    class Client:
        returncode = 1 if outcome in ("false-guard", "client-error") else 0
        def communicate(self, timeout):
            assert 0 <= timeout <= 5
            return b"", b"synthetic refused" if self.returncode else b""
    def popen(argv, **kwargs):
        clients.append(argv)
        assert argv[:4] == ["tmux", "-N", "-S", str(address)]
        assert argv[4:6] == ["if-shell", "-F"]
        assert str(binding["server_pid"]) in argv[6] and nonce in argv[6]
        assert argv[7:] == ["kill-server", "run-shell 'exit 1'"]
        assert events[0] == "register"
        return Client()
    monkeypatch.setattr(owner, "_exists", lambda pid: state["live"])
    monkeypatch.setattr(owner, "process_start_identity", lambda pid: "foreign-start" if outcome == "recycled-server" else "server-start")
    monkeypatch.setattr(owner, "_ExitEvent", Event)
    monkeypatch.setattr(owner, "_REAL_POPEN", popen)
    if outcome == "root-mode": root.chmod(0o755)
    try:
        if outcome == "complete":
            owner._reap_socket(str(address), binding, owner.time.monotonic() + 5)
            assert not address.exists() and not root.exists()
            assert events == ["register", "wait", "close"]
        else:
            expected = KeyboardInterrupt if outcome == "cancellation" else owner.ManifestError
            with pytest.raises(expected):
                owner._reap_socket(str(address), binding, owner.time.monotonic() + 5)
            assert address.exists() and root.exists()
            if outcome == "replacement":
                assert address.read_text() == "foreign replacement"
                assert not clients
            if outcome in ("denied-event", "recycled-server", "root-mode"): assert not clients
            if outcome == "replacement-at-exit": assert address.read_text() == "foreign replacement after exit"
            if outcome == "root-replacement-at-exit": assert address.read_text() == "foreign root replacement"
    finally:
        endpoint.close()
        if address.exists(): address.unlink()  # only this fixture's socket or authored replacement
        if root.exists(): root.rmdir()
        original = Path(str(root) + "-original")
        if original.exists():
            (original / "s").unlink()
            original.rmdir()


@pytest.mark.parametrize("bad", ["owner-pid", "owner-start", "owner-state", "owner-run", "entry-run", "binding-fields", "default-socket"])
def test_malformed_manifest_never_authorizes_cleanup(monkeypatch, tmp_path, bad):
    from tools import gate_owner_manifest as owner
    entry = dict(pgid=42420, leader_pid=42420, leader_start_identity="victim-start",
                 run_id="run", socket=None, socket_path=None)
    data = dict(schema=owner.MANIFEST_SCHEMA, version=owner.MANIFEST_VERSION,
                owner_pid=42419, owner_start_identity="owner-start", owner_state="active", run_id="run", entries=[entry])
    if bad == "owner-pid": data["owner_pid"] = 0
    elif bad == "owner-start": data["owner_start_identity"] = None
    elif bad == "owner-state": data["owner_state"] = "unknown"
    elif bad == "owner-run": data["run_id"] = ""
    elif bad == "entry-run": entry["run_id"] = ""
    elif bad == "binding-fields": entry.update(socket="/tmp/v2s-synthetic/s", socket_path="/tmp/v2s-synthetic/s", socket_owner={"nonce": "incomplete"})
    else: entry["socket"] = "default"
    path = tmp_path / "malformed.json"; path.write_text(json.dumps(data)); before = path.read_bytes()
    monkeypatch.setattr(owner, "_reap_group", lambda *args, **kwargs: pytest.fail("unsafe group mutation"))
    monkeypatch.setattr(owner, "_REAL_POPEN", lambda *args, **kwargs: pytest.fail("unsafe socket client"))
    with pytest.raises(owner.ManifestError): owner.reap_manifest(path)
    assert path.read_bytes() == before


def test_stale_foreign_survivors_and_live_nested_owner_are_preserved(monkeypatch, tmp_path):
    from tools import gate_owner_manifest as owner
    path = tmp_path / "owned.json"
    data = dict(schema=owner.MANIFEST_SCHEMA, version=owner.MANIFEST_VERSION,
        owner_pid=42419, owner_start_identity="owner-start", owner_state="active", run_id="original-run",
        entries=[dict(pgid=42420, leader_pid=42420, leader_start_identity="victim-start",
                      run_id="foreign-run", socket=None, socket_path=None)])
    path.write_text(json.dumps(data))
    monkeypatch.setattr(owner, "_exists", lambda pid: False)
    monkeypatch.setattr(owner, "_reap_group", lambda *args, **kwargs: pytest.fail("foreign group mutation"))
    monkeypatch.setattr(owner, "_REAL_POPEN", lambda *args, **kwargs: pytest.fail("foreign socket mutation"))
    with pytest.raises(owner.ManifestError, match="foreign"):
        owner.initialize_manifest(path, "new-run")
    assert json.loads(path.read_text()) == data
    owner.reap_manifest(path, "original-run")
    assert json.loads(path.read_text()) == data
    monkeypatch.setattr(owner, "_exists", lambda pid: True)
    monkeypatch.setattr(owner, "process_start_identity", lambda pid: "owner-start")
    with pytest.raises(owner.ManifestBusyError, match="live gate"):
        owner.initialize_manifest(path, "nested-run")
    assert json.loads(path.read_text()) == data


@pytest.mark.parametrize("outcome", ["terminal", "pid-reaped", "live", "denied-event",
                                      "recycled-before", "recycled-after", "unreadable-after"])
def test_absent_bound_root_requires_same_instance_kernel_exit(monkeypatch, tmp_path, outcome):
    from types import SimpleNamespace
    from tools import gate_owner_manifest as owner
    binding = dict(nonce="a" * 32, server_pid=42421, server_start_identity="server-start",
                   root_dev=1, root_ino=1, socket_dev=1, socket_ino=1)
    state, events, clients = {"registered": False}, [], []
    address = "/tmp/v2s-synthetic-absent/s"
    def objects(*args):
        raise FileNotFoundError("owned synthetic root absent")
    def identity(pid):
        if outcome == "recycled-before" or (state["registered"] and outcome == "recycled-after"):
            return "foreign-start"
        if state["registered"] and outcome == "unreadable-after":
            return None
        return "server-start"
    class Event:
        terminal = False
        def __init__(self, pid):
            events.append("register")
            if outcome == "denied-event":
                raise owner.ManifestError("synthetic registration denied")
            state["registered"] = True
        def wait(self, deadline):
            assert deadline == 100.0, "absent-root check must not extend the deadline"
            events.append("wait")
            if outcome == "live":
                raise owner.ManifestError("synthetic terminal event not ready")
        def close(self):
            events.append("close")
    def popen(*args, **kwargs):
        clients.append(args)
        raise AssertionError("absent-root reentry must never signal or contact tmux")
    monkeypatch.setattr(owner, "_exists", lambda pid: not (state["registered"] and outcome == "pid-reaped"))
    monkeypatch.setattr(owner, "process_start_identity", identity)
    monkeypatch.setattr(owner, "_socket_objects", objects)
    monkeypatch.setattr(owner, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(owner, "_ExitEvent", Event)
    monkeypatch.setattr(owner, "_REAL_POPEN", popen)
    monkeypatch.setattr(owner, "time", SimpleNamespace(monotonic=lambda: 100.0))
    if outcome in ("terminal", "pid-reaped"):
        owner._reap_socket(address, binding, 105.0)
        assert events == ["register", "wait", "close"]
    else:
        with pytest.raises(owner.ManifestError):
            owner._reap_socket(address, binding, 105.0)
        if outcome == "recycled-before":
            assert events == []
        elif outcome == "denied-event":
            assert events == ["register"]
        elif outcome == "live":
            assert events == ["register", "wait", "close"]
        else:
            assert events == ["register", "close"]
    assert not clients


@pytest.mark.parametrize("outcome", ["esrch", "note-exit", "pid-reaped", "live", "denied",
    "wrong-pid", "wrong-filter", "wrong-error", "recycled-before", "recycled-after",
    "unreadable-after"])
def test_absent_bound_root_darwin_registration_witness(monkeypatch, outcome):
    from types import SimpleNamespace
    from tools import gate_owner_manifest as owner
    binding = dict(nonce="a" * 32, server_pid=42421, server_start_identity="server-start",
                   root_dev=1, root_ino=1, socket_dev=1, socket_ino=1)
    state, calls = {"registered": False}, []
    def identity(pid):
        if outcome == "recycled-before" or (state["registered"] and outcome == "recycled-after"):
            return "foreign-start"
        return None if state["registered"] and outcome == "unreadable-after" else "server-start"
    def objects(*args):
        raise FileNotFoundError("owned synthetic root absent")
    class Kernel:
        def control(self, changes, count, timeout):
            assert len(changes) == count == 1 and timeout == 0
            assert changes[0] == dict(ident=42421, filter=-5, flags=5, fflags=0x80000000)
            calls.append("register")
            state["registered"] = True
            if outcome == "denied":
                raise PermissionError("synthetic event denied")
            if outcome == "live":
                return []
            return [SimpleNamespace(ident=42422 if outcome == "wrong-pid" else 42421,
                filter=-1 if outcome == "wrong-filter" else -5,
                flags=0 if outcome == "note-exit" else 0x4000,
                fflags=0x80000000 if outcome == "note-exit" else 0,
                data=1 if outcome == "wrong-error" else 3)]
        def close(self):
            calls.append("close")
    def forbidden(*args, **kwargs):
        raise AssertionError("absent-root witness must not signal, unlink, contact tmux or use global exit helper")
    monkeypatch.setattr(owner, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(owner, "select", SimpleNamespace(kqueue=Kernel,
        kevent=lambda ident, **kw:dict(ident=ident, **kw), KQ_FILTER_PROC=-5,
        KQ_EV_ADD=1, KQ_EV_ENABLE=4, KQ_NOTE_EXIT=0x80000000, KQ_EV_ERROR=0x4000))
    monkeypatch.setattr(owner, "_exists", lambda pid:not(state["registered"] and outcome == "pid-reaped"))
    monkeypatch.setattr(owner, "process_start_identity", identity)
    monkeypatch.setattr(owner, "_socket_objects", objects)
    monkeypatch.setattr(owner, "_REAL_POPEN", forbidden)
    monkeypatch.setattr(owner, "_ExitEvent", forbidden)
    monkeypatch.setattr(owner.os, "unlink", forbidden)
    if outcome in ("esrch", "note-exit", "pid-reaped"):
        owner._reap_socket("/tmp/v2s-synthetic-absent/s", binding, 105.0)
    else:
        with pytest.raises(owner.ManifestError):
            owner._reap_socket("/tmp/v2s-synthetic-absent/s", binding, 105.0)
    assert calls == ([] if outcome == "recycled-before" else ["register", "close"])


@pytest.mark.parametrize("outcome", ["esrch", "wrong-pid", "wrong-filter", "wrong-error",
    "denied", "note-exit", "note-wrong-filter", "missing-filter"])
def test_darwin_exit_registration_requires_matching_terminal_event(monkeypatch, outcome):
    from tools import gate_owner_manifest as owner
    closed = []
    def control(changes, count, timeout):
        if outcome == "denied": raise PermissionError("synthetic denial")
        fields = dict(ident=42421 if outcome == "wrong-pid" else 42420,
            filter=-1 if outcome in ("wrong-filter", "note-wrong-filter") else -5,
            flags=0 if outcome.startswith("note-") or outcome == "missing-filter" else 32,
            fflags=16, data=1 if outcome == "wrong-error" else owner.errno.ESRCH)
        if outcome == "missing-filter": fields.pop("filter")
        return [SimpleNamespace(**fields)]
    monkeypatch.setattr(owner, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(owner, "select", SimpleNamespace(
        kqueue=lambda: SimpleNamespace(control=control, close=lambda: closed.append(True)),
        kevent=lambda pid, **kwargs: (pid, kwargs), KQ_FILTER_PROC=-5, KQ_EV_ADD=1,
        KQ_EV_ENABLE=4, KQ_NOTE_EXIT=16, KQ_EV_ERROR=32))
    if outcome in ("esrch", "note-exit"):
        event = owner._ExitEvent(42420)
        try:
            assert event.terminal
            event.wait(owner.time.monotonic())
        finally: event.close()
    else:
        with pytest.raises(owner.ManifestError):
            event = owner._ExitEvent(42420)
            try: event.wait(owner.time.monotonic())
            finally: event.close()
    assert closed == [True]


@pytest.mark.parametrize("outcome", ["terminal", "recycled", "replacement", "root-replacement"])
def test_terminal_bound_socket_preserves_ownership_without_contact(monkeypatch, outcome):
    import socket
    import tempfile
    import uuid
    from tools import gate_owner_manifest as owner
    root = Path(tempfile.mkdtemp(prefix="v2s-", dir="/tmp")); root.chmod(0o700)
    address = root / "s"
    endpoint = socket.socket(socket.AF_UNIX); endpoint.bind(str(address))
    rs, ss = root.stat(), address.lstat()
    binding = dict(nonce=uuid.uuid4().hex, server_pid=42421, server_start_identity="server-start",
                   root_dev=rs.st_dev, root_ino=rs.st_ino, socket_dev=ss.st_dev, socket_ino=ss.st_ino)
    state, events = {"registered": False}, []
    class Event:
        terminal = True
        def __init__(self, pid):
            assert pid == binding["server_pid"]
            state["registered"] = True
            events.append("register")
            if outcome == "replacement":
                address.unlink()
                address.write_text("foreign replacement")
            if outcome == "root-replacement":
                root.rename(str(root) + "-original")
                root.mkdir(mode=0o700)
                address.write_text("foreign replacement")
        def wait(self, deadline): pytest.fail("terminal witness needs no wait")
        def close(self): events.append("close")
    def forbidden(*args, **kwargs): pytest.fail("terminal server must never be contacted")
    monkeypatch.setattr(owner, "_ExitEvent", Event)
    monkeypatch.setattr(owner, "_exists", lambda pid: True)
    monkeypatch.setattr(owner, "process_start_identity", lambda pid: "foreign-start"
        if outcome == "recycled" and state["registered"] else "server-start")
    monkeypatch.setattr(owner, "_REAL_POPEN", forbidden)
    try:
        if outcome == "terminal":
            owner._reap_socket(str(address), binding, owner.time.monotonic() + 5)
            assert not address.exists() and not root.exists()
        else:
            with pytest.raises(owner.ManifestError):
                owner._reap_socket(str(address), binding, owner.time.monotonic() + 5)
            assert address.exists() and root.exists()
            if outcome in ("replacement", "root-replacement"):
                assert address.read_text() == "foreign replacement"
        assert events == ["register", "close"]
    finally:
        endpoint.close()
        if address.exists(): address.unlink()
        if root.exists(): root.rmdir()
        original = Path(str(root) + "-original")
        if original.exists():
            (original / "s").unlink()
            original.rmdir()
