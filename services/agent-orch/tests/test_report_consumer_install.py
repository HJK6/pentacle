"""Copied-byte verification, refresh, and clean reader dispatch regressions."""
import asyncio
import hashlib
import grp
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

DEPLOY = Path(__file__).parents[1] / "deploy"


@pytest.fixture(autouse=True)
def _restore_wrapper_environment():
    """The process-entry wrapper scrubs its environment; contain in-process calls."""
    with patch.dict(os.environ):
        yield


def load(name):
    spec = importlib.util.spec_from_file_location(name, DEPLOY / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    bytecode = sys.dont_write_bytecode
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = bytecode
    return module


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bundle(path, commit, extra=None):
    """A launchable reader dependency tree; no ambient package supplies it."""
    files = {
        "services/agent-orch/agent_orch/__init__.py": b"",
        "services/agent-orch/agent_orch/work_report_cli.py":
            b"import json,websockets\ndef main(args):\n print(json.dumps({'args':args,'dependency':websockets.__file__}))\n return 0\n",
        "services/_shared/__init__.py": b"",
        "runtime-deps/websockets/websockets/__init__.py": b"",
    }
    path.mkdir()
    with tarfile.open(path / "pentacle.tar", "w") as archive:
        for name, data in files.items():
            member = tarfile.TarInfo(name); member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        if extra:
            archive.addfile(extra)
    manifest = {"schema": "PentacleReleaseManifestV2", "commit": commit,
                "archive_sha256": digest(path / "pentacle.tar")}
    (path / "manifest.json").write_text(json.dumps(manifest))
    return path, digest(path / "manifest.json")


@pytest.fixture
def install(tmp_path, monkeypatch):
    m = load("report_consumer_install")
    monkeypatch.setattr(m, "ROOT", tmp_path / "consumer")
    monkeypatch.setattr(m, "PYTHON", sys.executable)
    # Exercise real files, archive verification/imports/pointers without root.
    # Production ownership and ancestor checks have separate refusal tests;
    # actual root ownership/publisher-uid proof belongs to installed acceptance.
    monkeypatch.setattr(m, "_ROOT_UID", os.getuid())
    monkeypatch.setattr(m.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid()))
    run = subprocess.run
    def unprivileged_probe(*args, **kwargs):
        if "user" in kwargs:
            assert kwargs.pop("user") == os.getuid()
            assert kwargs.pop("group") == os.getgid()
            assert kwargs.pop("extra_groups") == []
        return run(*args, **kwargs)
    monkeypatch.setattr(m.subprocess, "run", unprivileged_probe)
    original_directory = m._directory
    def directory(path):
        if path == tmp_path:
            return
        return original_directory(path)
    monkeypatch.setattr(m, "_directory", directory)
    monkeypatch.setattr(m.os, "geteuid", lambda: 0)
    try:
        yield m
    finally:
        # Production releases stay immutable. This unprivileged fixture owns
        # its temporary installation and must release it for strict gate cleanup.
        if m.ROOT.is_dir() and not m.ROOT.is_symlink():
            for path in [m.ROOT, *m.ROOT.rglob("*")]:
                if path.is_symlink():
                    continue
                info = path.stat()
                if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid():
                    path.chmod(stat.S_IMODE(info.st_mode) | stat.S_IWUSR)


def test_fixture_teardown_allows_strict_gate_removal(tmp_path):
    owned = tmp_path / "owned"; owned.mkdir()
    with pytest.MonkeyPatch.context() as patch:
        lifecycle = install.__wrapped__(owned, patch)
        engine = next(lifecycle)
        try:
            source, pin = bundle(owned / "source", "a" * 40)
            engine.install(source, "a" * 40, pin)
            assert stat.S_IMODE((engine.ROOT / "releases" / ("a" * 40)).stat().st_mode) == 0o555
        finally:
            lifecycle.close()
    shutil.rmtree(owned)
    assert not owned.exists()


def test_private_home_failure_then_consumer_executes_from_export(install, tmp_path):
    home = tmp_path / "private-home"; home.mkdir()
    cli = home / "agent-orch"; cli.write_text("#!/bin/sh\nexit 0\n"); cli.chmod(0o755)
    home.chmod(0)
    try:
        with pytest.raises(PermissionError):
            subprocess.run([str(cli), "report", "get", "example"], check=True)
        source, pinned = bundle(tmp_path / "source", "a" * 40)
        result = install.install(source, "a" * 40, pinned)
        wrapper = Path(result["consumer"])
        env = {"HOME": str(home), "PYTHONPATH": str(home),
               "PENTACLE_WORK_REPORT_READER_TOKEN": "synthetic-test-reader",
               "AGENT_ORCH_WS_URL": "ws://127.0.0.1:1"}
        run = subprocess.run([sys.executable, "-I", str(wrapper), "report", "get", "--json", "example"],
                             env=env, text=True, capture_output=True, check=True)
        response = json.loads(run.stdout)
        assert response["args"] == ["--json", "example"]
        assert Path(response["dependency"]).is_relative_to(install.ROOT)
        assert result["active"] == "releases/" + "a" * 40
    finally:
        home.chmod(0o700)


@pytest.mark.parametrize("tamper", ["manifest", "archive", "commit"])
def test_tamper_never_imports_or_changes_active(install, tmp_path, monkeypatch, tamper):
    source, pinned = bundle(tmp_path / "source", "a" * 40)
    install.install(source, "a" * 40, pinned)
    newer, new_pin = bundle(tmp_path / "new", "b" * 40)
    if tamper == "manifest":
        (newer / "manifest.json").write_text("{}")
    elif tamper == "archive":
        with (newer / "pentacle.tar").open("ab") as handle: handle.write(b"changed")
    else:
        data = json.loads((newer / "manifest.json").read_text()); data["commit"] = "c" * 40
        (newer / "manifest.json").write_text(json.dumps(data)); new_pin = digest(newer / "manifest.json")
    monkeypatch.setattr(install, "_probe", lambda _: pytest.fail("import before copied-byte verification"))
    with pytest.raises(ValueError, match="identity|hash"):
        install.install(newer, "b" * 40, new_pin)
    assert install._active() == "releases/" + "a" * 40


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE])
def test_archive_links_and_special_files_refuse_before_import(install, tmp_path, kind):
    member = tarfile.TarInfo("services/agent-orch/agent_orch/unsafe"); member.type = kind; member.linkname = "/etc/passwd"
    source, pinned = bundle(tmp_path / "source", "a" * 40, member)
    with pytest.raises(ValueError, match="link or special"):
        install.install(source, "a" * 40, pinned)
    assert not (install.ROOT / "active").exists()


def test_verified_copy_not_later_mutable_source_is_executed(install, tmp_path, monkeypatch):
    source, pinned = bundle(tmp_path / "source", "a" * 40)
    copy = install.shutil.copyfile
    def mutate_after_copy(src, dst):
        result = copy(src, dst)
        if Path(src) == source / "pentacle.tar":
            Path(src).write_bytes(b"source changed after copy")
        return result
    monkeypatch.setattr(install.shutil, "copyfile", mutate_after_copy)
    assert install.install(source, "a" * 40, pinned)["ok"]


def test_failed_probe_preserves_selection_and_refresh_retains_previous(install, tmp_path, monkeypatch):
    a, ap = bundle(tmp_path / "a", "a" * 40); b, bp = bundle(tmp_path / "b", "b" * 40)
    install.install(a, "a" * 40, ap)
    probe = install._probe
    def failed(_): raise RuntimeError("interrupted probe")
    monkeypatch.setattr(install, "_probe", failed)
    with pytest.raises(RuntimeError): install.install(b, "b" * 40, bp)
    assert install._active() == "releases/" + "a" * 40
    monkeypatch.setattr(install, "_probe", probe)
    result = install.install(b, "b" * 40, bp)
    assert result["previous"] == "releases/" + "a" * 40
    assert os.readlink(install.ROOT / "previous") == result["previous"]
    assert install._active() == "releases/" + "b" * 40


@pytest.mark.parametrize("prior_previous", [False, True])
@pytest.mark.parametrize("fault", ["previous", "active", "readback"])
def test_failed_selection_restores_both_pointer_preimages(install, tmp_path, monkeypatch,
                                                        prior_previous, fault):
    if prior_previous:
        a, ap = bundle(tmp_path / "a", "a" * 40)
        install.install(a, "a" * 40, ap)
    b, bp = bundle(tmp_path / "b", "b" * 40)
    install.install(b, "b" * 40, bp)
    before = {name: os.readlink(install.ROOT / name) if os.path.lexists(install.ROOT / name) else None
              for name in ("active", "previous")}
    c, cp = bundle(tmp_path / "c", "c" * 40)
    link, active = install._link, install._active
    injected = False
    def failing_link(name, target):
        nonlocal injected
        if not injected and fault == name:
            injected = True
            raise OSError("injected pointer selection failure")
        return link(name, target)
    def failing_readback():
        nonlocal injected
        selected = active()
        if not injected and fault == "readback" and selected == "releases/" + "c" * 40:
            injected = True
            raise OSError("injected pointer readback failure")
        return selected
    monkeypatch.setattr(install, "_link", failing_link)
    monkeypatch.setattr(install, "_active", failing_readback)
    with pytest.raises(OSError, match="injected pointer"):
        install.install(c, "c" * 40, cp)
    assert injected
    after = {name: os.readlink(install.ROOT / name) if os.path.lexists(install.ROOT / name) else None
             for name in ("active", "previous")}
    assert after == before


def test_root_only_and_unsafe_ancestor_refused(tmp_path):
    m = load("report_consumer_install")
    with pytest.raises(ValueError, match="requires root"):
        m.install(tmp_path, "a" * 40, "0" * 64)
    with pytest.raises(ValueError, match="unsafe consumer directory"):
        m._directory(tmp_path)


def test_wrapper_refuses_other_routes_and_missing_credentials(monkeypatch, capsys):
    m = load("report_consumer_cli")
    monkeypatch.setattr(m, "_load_reader", lambda: pytest.fail("must refuse before import"))
    assert m.main(["close", "host:seat"]) == 2
    monkeypatch.delenv(m.TOKEN, raising=False)
    assert m.main(["report", "get", "example"]) == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == "unavailable"


def test_wrapper_removes_seat_and_operator_credentials(monkeypatch):
    m = load("report_consumer_cli")
    for key in ["AGENT_ORCH_STREAM_TOKEN", "AGENT_ORCH_STREAM_TOKEN_FILE", "AGENT_ORCH_TOKEN",
                "AGENT_ORCH_STREAM_ID", "PENTACLE_STREAM_ID"]:
        monkeypatch.setenv(key, "synthetic-forbidden")
    monkeypatch.setenv(m.TOKEN, "synthetic-reader"); monkeypatch.setenv(m.URL, "ws://127.0.0.1:1")
    observed = {}
    def reader(args): observed.update(os.environ); return 0
    monkeypatch.setattr(m, "_load_reader", lambda: reader)
    assert m.main(["report", "get", "example"]) == 0
    assert observed["AGENT_ORCH_TOKEN"] == ""
    assert observed[m.TOKEN] == "synthetic-reader"
    assert not any(key in observed for key in ["AGENT_ORCH_STREAM_TOKEN", "AGENT_ORCH_STREAM_TOKEN_FILE", "AGENT_ORCH_STREAM_ID"])


def test_installer_mode_is_root_only_and_excludes_fleet_flags():
    argv = [sys.executable, "-I", str(DEPLOY / "install_fleet_spawn_tooling.py"), "--commit", "a" * 40,
            "--install-report-consumer", "/unused", "--consumer-manifest-sha256", "0" * 64]
    result = subprocess.run(argv, capture_output=True, text=True)
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"] == "consumer installation requires root"
    result = subprocess.run(argv + ["--hosts", "remote"], capture_output=True, text=True)
    assert result.returncode == 2 and "cannot perform a fleet rollout" in result.stderr


@pytest.fixture
def configured(tmp_path, monkeypatch):
    m = load("report_consumer_cli")
    prefix = tmp_path / "consumer"; prefix.mkdir()
    config = prefix / "reader.json"
    config.write_text(json.dumps({"token": "synthetic-reader", "ws_url": "ws://127.0.0.1:1"}))
    config.chmod(0o600)
    monkeypatch.setattr(m, "__file__", str(prefix / "releases" / ("a" * 40) / "agent-orch"))
    monkeypatch.setattr(m, "_root_path", lambda path: None)
    monkeypatch.setattr(m.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=os.getuid()))
    monkeypatch.setattr(grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=0))
    fstat = m.os.fstat
    def root_info(fd):
        real = fstat(fd)
        return SimpleNamespace(st_uid=0, st_gid=0, st_mode=real.st_mode, st_size=real.st_size)
    monkeypatch.setattr(m.os, "fstat", root_info)
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: SimpleNamespace(
        stdout="file metadata\n 0: user:_triforce_memory allow read\n"))
    return m, config


def test_config_returns_exact_clean_environment(configured):
    m, _ = configured
    assert m.reader_environment() == {m.TOKEN: "synthetic-reader", m.URL: "ws://127.0.0.1:1",
                                      "AGENT_ORCH_TOKEN": ""}


def test_config_requires_wheel_group_before_reading(configured, monkeypatch):
    m, _ = configured
    fstat = m.os.fstat
    def wrong_group(fd):
        info = fstat(fd)
        return SimpleNamespace(st_uid=info.st_uid, st_gid=1,
                               st_mode=info.st_mode, st_size=info.st_size)
    monkeypatch.setattr(m.os, "fstat", wrong_group)
    monkeypatch.setattr(m.json, "load", lambda _: pytest.fail("config read before group check"))
    with pytest.raises(ValueError, match="unsafe reader configuration"):
        m.reader_environment()


@pytest.mark.parametrize("acl", ["", "0: group:staff allow read", "0: user:_triforce_memory allow read,write",
                                  "0: user:_triforce_memory allow read\n1: user:other allow read"])
def test_config_refuses_missing_broad_or_writable_acl(configured, monkeypatch, acl):
    m, _ = configured
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=acl))
    with pytest.raises(ValueError, match="publisher read ACL"):
        m.reader_environment()


@pytest.mark.parametrize("mode", [0o640, 0o644, 0o660, 0o400])
def test_config_requires_exact_owner_only_mode(configured, mode):
    m, config = configured; config.chmod(mode)
    with pytest.raises(ValueError, match="unsafe reader configuration"):
        m.reader_environment()


@pytest.mark.parametrize("value", [{}, {"token": "", "ws_url": "ws://127.0.0.1:1"},
    {"token": "synthetic", "ws_url": "https://example.invalid"},
    {"token": "synthetic", "ws_url": "ws://user:password@localhost"}])
def test_config_invalid_values_fail_closed(configured, value):
    m, config = configured; config.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="invalid reader"):
        m.reader_environment()


def test_config_symlink_and_missing_refuse(configured):
    m, config = configured; config.unlink()
    with pytest.raises(FileNotFoundError): m.reader_environment()
    config.symlink_to("missing")
    with pytest.raises(OSError): m.reader_environment()


def test_config_wrong_identity_refuses(configured, monkeypatch):
    m, _ = configured
    monkeypatch.setattr(m.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=-1))
    with pytest.raises(ValueError, match="publisher identity"):
        m.reader_environment()


def test_root_config_parent_check_is_not_bypassed(tmp_path):
    m = load("report_consumer_cli")
    with pytest.raises(ValueError, match="unsafe reader installation"):
        m._root_path(tmp_path)


@pytest.fixture
def frozen_report_reader(monkeypatch):
    """Import the pinned installed module without invoking its launcher or daemon."""
    release_value = os.environ.get("PENTACLE_WORK_REPORT_RELEASE_DIR")
    expected_sha256 = os.environ.get("PENTACLE_WORK_REPORT_MODULE_SHA256")
    if not release_value or not expected_sha256:
        pytest.skip("installed-release fixture requires a release path and module hash")

    release = Path(release_value).resolve(strict=True)
    package_dir = release / "app/services/agent-orch/agent_orch"
    module_path = package_dir / "work_report_cli.py"
    assert module_path.is_file()
    assert digest(module_path) == expected_sha256

    # Load under a private package name so the repository checkout cannot
    # satisfy relative imports in place of the immutable installed release.
    package_name = "_pentacle_frozen_report_reader"
    package_spec = importlib.util.spec_from_file_location(
        package_name, package_dir / "__init__.py",
        submodule_search_locations=[str(package_dir)],
    )
    assert package_spec and package_spec.loader
    package = importlib.util.module_from_spec(package_spec)
    sys.modules[package_name] = package
    package_spec.loader.exec_module(package)

    monkeypatch.syspath_prepend(str(release / "app/runtime-deps/websockets"))
    reader_spec = importlib.util.spec_from_file_location(
        package_name + ".work_report_cli", module_path,
    )
    assert reader_spec and reader_spec.loader
    reader = importlib.util.module_from_spec(reader_spec)
    sys.modules[reader_spec.name] = reader
    reader_spec.loader.exec_module(reader)
    assert Path(reader.__file__).resolve() == module_path.resolve()
    try:
        yield reader
    finally:
        for name in list(sys.modules):
            if name == package_name or name.startswith(package_name + "."):
                sys.modules.pop(name, None)


class DisposableReportSocket:
    """A local WebSocket-shaped fixture; it has no network or daemon endpoint."""

    def __init__(self, response=None):
        self.response = response
        self.reply = None
        self.sent = []
        self.closed = False

    async def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        if message.get("type") == "work.report.get" and self.response is not None:
            self.reply = {**self.response, "request_id": message["request_id"]}

    async def recv(self):
        if self.reply is None:
            await asyncio.Future()
        return json.dumps(self.reply, separators=(",", ":"))


class DisposableReportConnection:
    def __init__(self, socket):
        self.socket = socket

    async def __aenter__(self):
        return self.socket

    async def __aexit__(self, exc_type, exc, traceback):
        self.socket.closed = True


def configure_frozen_reader(reader, monkeypatch, socket, identity_calls):
    monkeypatch.setattr(reader, "load_config", lambda: SimpleNamespace(ws_url="ws://fixture.invalid"))

    def reader_identity(explicit):
        identity_calls.append(explicit)
        return {"fixture_identity": explicit or "ordinary-fixture-seat"}

    monkeypatch.setattr(reader, "reader_identity_ready", reader_identity)
    monkeypatch.setattr(
        reader.wsclient, "_rpc_hello",
        lambda config, from_stream_id=None: {"type": "hello.fixture", "from_stream_id": from_stream_id},
    )

    async def send_fixture_frame(ws, message, identity, **kwargs):
        await ws.send(json.dumps(message, separators=(",", ":")))

    monkeypatch.setattr(reader, "send_frame", send_fixture_frame)
    monkeypatch.setattr(
        reader.wsclient.websockets, "connect",
        lambda *args, **kwargs: DisposableReportConnection(socket),
    )


def test_frozen_reader_round_trips_new_advisor_projection(frozen_report_reader, monkeypatch, capsys):
    reader = frozen_report_reader
    monkeypatch.setenv(reader.TOKEN_ENV, "fixture-reader-token")
    stamp = {
        "version": 1,
        "verdict": "accept",
        "operator_message_id": "fixture-message-42",
        "fd_event_id": "fixture-tell-42",
        "path": "docs/reference/assistant_" + "bar" + "t_context.md",
        "base_revision": "a" * 64,
        "new_bytes_sha256": "b" * 64,
        "scope": {"mode": "single", "removed": ["c" * 64], "added": ["d" * 64]},
        "advisor_stream_id": "fixture:advisor-1",
        "advisor_generation": "fixture-advisor-generation",
        "fd_stream_id": "fixture:fd-1",
        "fd_generation": "fixture-fd-generation",
        "fd_binding_revision": 12,
        "operator_text_sha256": "e" * 64,
    }
    # The service-reader fixture carries the new allowlisted attestation.
    service_response = {
        "type": "work.report.get.ok",
        "report_id": "fixture-report-42",
        "status": "progress",
        "from_stream_id": "fixture:advisor-1",
        "provenance_qualified_spec_ids": ["spec_fixture"],
        "created_at": "2026-11-22T20:00:00Z",
        "advisor_rule_accept": stamp,
        "session_generation": "fixture-advisor-generation",
    }
    socket = DisposableReportSocket(service_response)
    identity_calls = []
    configure_frozen_reader(reader, monkeypatch, socket, identity_calls)

    exit_code = reader.main(["fixture-report-42"])
    printed = capsys.readouterr().out
    actual = json.loads(printed)
    request = next(message for message in socket.sent if message.get("type") == "work.report.get")
    expected = {
        **service_response,
        "advisor_rule_accept": stamp,
        "session_generation": "fixture-advisor-generation",
        "request_id": request["request_id"],
    }

    assert exit_code == 0
    assert identity_calls == [reader.READER]
    assert socket.closed
    assert actual == expected
    assert set(actual) == {
        "type", "report_id", "status", "from_stream_id",
        "provenance_qualified_spec_ids", "created_at", "advisor_rule_accept",
        "session_generation", "request_id",
    }


def test_frozen_reader_timeout_without_service_reply_fails_closed(
    frozen_report_reader, monkeypatch, capsys,
):
    reader = frozen_report_reader
    monkeypatch.setenv(reader.TOKEN_ENV, "fixture-reader-token")
    socket = DisposableReportSocket()
    identity_calls = []
    configure_frozen_reader(reader, monkeypatch, socket, identity_calls)

    exit_code = reader.main(["fixture-report-timeout", "--timeout", "0.02"])
    actual = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert actual["type"] == "work.report.get.error"
    assert actual["error_code"] == "unavailable"
    assert actual["request_id"].startswith("work-report-")
    assert identity_calls == [reader.READER]
    assert socket.closed


def test_frozen_reader_ordinary_seat_response_has_no_advisor_stamp(
    frozen_report_reader, monkeypatch, capsys,
):
    reader = frozen_report_reader
    monkeypatch.delenv(reader.TOKEN_ENV, raising=False)
    service_response = {
        "type": "work.report.get.ok",
        "report_id": "fixture-report-ordinary",
        "status": "progress",
        "from_stream_id": "fixture:advisor-1",
        "provenance_qualified_spec_ids": ["spec_fixture"],
        "created_at": "2026-11-22T20:00:00Z",
    }
    socket = DisposableReportSocket(service_response)
    identity_calls = []
    configure_frozen_reader(reader, monkeypatch, socket, identity_calls)

    exit_code = reader.main(["fixture-report-ordinary"])
    actual = json.loads(capsys.readouterr().out)
    request = next(message for message in socket.sent if message.get("type") == "work.report.get")

    assert exit_code == 0
    assert identity_calls == [None]
    assert actual == {**service_response, "request_id": request["request_id"]}
    assert "advisor_rule_accept" not in actual
    assert "session_generation" not in actual
