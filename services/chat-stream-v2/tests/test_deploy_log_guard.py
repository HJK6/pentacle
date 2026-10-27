from __future__ import annotations

from functools import partial
import importlib.util
import json
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from .test_deploy_script import DEPLOY_PATH


def _load_deploy(home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    spec = importlib.util.spec_from_file_location("deploy_log_guard_under_test", DEPLOY_PATH)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("user", ["operator-one", "operator-two"])
def test_default_guard_uses_current_home_without_override(tmp_path, monkeypatch, user):
    home = tmp_path / user
    log_dir = home / "Library/Logs/pentacle/chat-streamd-v2"
    log_dir.mkdir(parents=True)
    module = _load_deploy(home, monkeypatch)

    assert module.build_parser().parse_args(["chat-streamd-v2"]).log_guard_dir is None
    service = module.SERVICES["chat-streamd-v2"]
    assert Path(service.log_guard_dir) == log_dir
    module.verify_log_guard(service.log_guard_dir)
    log_dir.rmdir()


def test_volume_guard_still_requires_a_mount(monkeypatch):
    module = _load_deploy(Path("/Users/example"), monkeypatch)
    monkeypatch.setattr(Path, "is_dir", lambda _path: True)
    monkeypatch.setattr(Path, "is_mount", lambda _path: False)
    with pytest.raises(module.DeployError, match="log guard unavailable"):
        module.verify_log_guard("/Volumes/example-logs")
    monkeypatch.setattr(Path, "is_mount", lambda _path: True)
    module.verify_log_guard("/Volumes/example-logs")


@pytest.fixture
def isolated_release(tmp_path, monkeypatch, request):
    """Real Git/stamp files; launchd, gate execution and live probes are counterparts."""
    module = _load_deploy(tmp_path / "home", monkeypatch)
    service = module.SERVICES["chat-streamd-v2"]
    repo, origin = tmp_path / "release", tmp_path / "origin.git"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, text=True, capture_output=True, check=True
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Deploy test")
    git("config", "user.email", "deploy-test@example.invalid")
    (repo / ".gitignore").write_text(".pentacle-deploy/\nservices/chat-stream-v2/.venv/\n")
    main = repo / "services/chat-stream-v2/main.py"
    main.parent.mkdir(parents=True)
    main.write_text("version = 1\n")
    (repo / service.requirements_path).write_text("")
    selection_fixture = getattr(request, "param", None) == "selection"
    if selection_fixture:
        from .test_release_selection import _seed_selector_files
        _seed_selector_files(repo)
        # Only an external provider counterpart: records the real child argv/env.
        smoke = repo / "services/chat-stream-v2/tools/spawn_fleet_smoke.py"
        smoke.parent.mkdir(parents=True, exist_ok=True)
        smoke.write_text("""import json, os, pathlib, sys
root = pathlib.Path(__file__).resolve().parents[3].parent
json.dump({'argv': sys.argv, 'machines_file': os.environ.get('PENTACLE_MACHINES_FILE'),
           'ambient_hosts': os.environ.get('PENTACLE_SMOKE_HOSTS'),
           'ambient_json': os.environ.get('PENTACLE_MACHINES_JSON')}, open(root/'observed-child.json', 'w'))
payload = json.load(open(root/'synthetic-provider-result.json'))
print(json.dumps(payload['result']))
sys.exit(payload['returncode'])
""")
    git("add", ".")
    git("commit", "-qm", "prior")
    prior = git("rev-parse", "HEAD")
    main.write_text("version = 2\n")
    if selection_fixture:
        main.write_text("version = 1\n")
        (repo / "renderer").mkdir(exist_ok=True)
        (repo / "renderer/fixture.js").write_text("// isolated client change\n")
        git("add", "renderer/fixture.js")
    git("commit", "-qam", "target")
    target = git("rev-parse", "HEAD")
    git("init", "--bare", "-q", str(origin))
    git("remote", "add", "origin", str(origin))
    git("push", "-q", "origin", "HEAD:refs/heads/main")
    git("checkout", "-q", "--detach", prior)
    python = module._venv_python(repo, service)
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable) if selection_fixture else python.touch()
    guard = Path(service.log_guard_dir)
    assert guard.is_relative_to(tmp_path), "test guard must stay inside the owned temporary home"
    guard.mkdir(parents=True)
    plist = tmp_path / "daemon.plist"
    plist.write_bytes(plistlib.dumps({
        "ProgramArguments": [str(python), str(main)],
        "StandardOutPath": str(guard / "launchd.log"),
        "EnvironmentVariables": {"HOME": str(tmp_path / "home")},
    }))
    monkeypatch.setattr(module, "_launchd_plist_path", lambda label:
                        tmp_path / "collector.plist" if selection_fixture and label == module.USAGE_COLLECTOR_LABEL else plist)
    # A stale stamp must not replace the checkout's actual pre-activation identity. The managed
    # digest is stamped to match the on-disk plist, so an unchanged definition still kickstarts
    # (no spurious reload); out-of-band drift is covered by its own test below.
    module._write_stamp(repo, service, {
        "sha": "c" * 40, "prior_sha": target,
        "requirements_hash": module._sha256(repo / service.requirements_path),
        "launchd_plist_digest": module._launchd_plist_managed_digest(service.launchd_label),
    })
    before = module._stamp_path(repo, service).read_bytes()
    monkeypatch.setattr(module, "verify_deploy_guard", lambda *_a, **_k: {"sole_deployer": True})
    monkeypatch.setattr(module, "_ensure_v2_usage_probe_launchd", lambda *_a: False)
    if not selection_fixture:
        monkeypatch.setattr(module, "_v2_usage_probe_rollback", lambda *_a: lambda: None)
    calls = []
    # The job is loaded (pid 111) until a bootout, and gone after it, as launchd reports it.
    monkeypatch.setattr(module, "_launchd_state", lambda *_a: (
        module.LaunchdState(False, None, True) if any(call[1:2] == ("bootout",) for call in calls)
        else module.LaunchdState(True, 111)))
    # Exercise the real post-activation/stamp boundary, replacing only the live boot probe.
    monkeypatch.setattr(module, "_apply_post_activation", partial(
        module._apply_post_activation,
        verify_boot=lambda *_a, **_k: module.BootReadback(
            module.BOOT_NOT_OBSERVED, "isolated probe", 0, 222, 111
        ),
    ))
    evidence = tmp_path / "gate.json"
    evidence.write_text(json.dumps({
        "schema": "pentacle.v2.gate-evidence.v1", "gate": "merge", "sha": target,
        "source": {"sha_before": target, "sha_after": target,
                   "clean_before": True, "clean_after": True},
        "tiers": [{"tier": tier, "passed": True} for tier in ("unit", "smoke")],
        "passed": True,
    }))

    def activate(*, reload=False, rollback=False, same_ref=False, fail=False, override=None,
                 release_inputs=None):
        selected = module._read_stamp(repo, service)["prior_sha"] if rollback else prior if same_ref else target
        actual_prior = git("rev-parse", "HEAD")
        payload = json.loads(evidence.read_text())
        payload["sha"] = selected
        payload["source"].update(sha_before=selected, sha_after=selected)
        evidence.write_text(json.dumps(payload))
        monkeypatch.setattr(module, "_ensure_launchd_environment", lambda *_a, **_k: reload)

        def runner(command, cwd):
            calls.append(tuple(command))
            if command[0] == "git":
                return subprocess.run(command, cwd=cwd, text=True, capture_output=True)
            if command[0] == "ps":
                return subprocess.CompletedProcess(command, 0, "Mon Jan 1 11:00:00 2024", "")
            if selection_fixture and (command[0] == str(python) or command[0] == "/usr/bin/env"):
                return subprocess.run(command, cwd=cwd, text=True, capture_output=True)
            assert command[0] == "launchctl", command
            if selection_fixture and command[1] == "bootout" and command[2].endswith("/" + module.USAGE_COLLECTOR_LABEL):
                return subprocess.CompletedProcess(command, 0, "", "")
            # Inspect actual disk bytes at the moment activation is attempted.
            stamp = module._read_stamp(repo, service)
            assert stamp["prior_sha"] == actual_prior
            assert stamp["sha"] == selected
            assert stamp["restart_activation"]["pre_restart_pid"] == 111
            return subprocess.CompletedProcess(command, 1 if fail else 0, "", "isolated failure" if fail else "")

        return module.deploy(
            service, release_checkout=repo,
            ref=prior if rollback or same_ref else "origin/main", rollback=rollback,
            gate_evidence=evidence, deployer_stream_id="test-host:deployer",
            log_guard_dir=override, runner=runner,
            **({"release_inputs": release_inputs} if release_inputs is not None else {}),
        )

    yield module, service, repo, prior, target, before, calls, activate
    if guard.is_dir():
        guard.rmdir()


@pytest.mark.parametrize("reload", [False, True], ids=["kickstart", "plist-reload"])
@pytest.mark.parametrize("operation", ["deploy", "rollback", "same-ref"])
def test_every_activation_stamps_prior_checkout_before_restart(isolated_release, reload, operation):
    module, service, repo, prior, target, _before, calls, activate = isolated_release
    stamp = activate(reload=reload, rollback=operation == "rollback", same_ref=operation == "same-ref")
    expected = prior if operation == "same-ref" else target
    assert stamp["sha"] == expected
    assert stamp["prior_sha"] == prior
    assert module._read_stamp(repo, service) == stamp
    assert module._git(repo, "rev-parse", "HEAD", runner=module._run) == expected
    assert [call[1] for call in calls if call[0] == "launchctl"] == (
        ["bootout", "bootstrap"] if reload else ["kickstart"]
    )


def _make_stamp_stale(module, repo, service):
    """Simulate the 2026-10-19 incident: the on-disk plist drifted from what the last deploy
    bootstrapped, so the stamped managed digest no longer matches."""
    stamp = module._read_stamp(repo, service)
    stamp["launchd_plist_digest"] = "stale" + "0" * 59
    module._write_stamp(repo, service, stamp)


def test_out_of_band_plist_change_forces_rebootstrap(isolated_release):
    """End-to-end scope 1: when the on-disk plist drifted from what the last deploy bootstrapped
    (the 2026-10-19 incident), deploy must bootout+bootstrap even though its own env/usage edits
    reported no change — not kickstart the stale loaded definition."""
    module, service, repo, _prior, _target, _before, calls, activate = isolated_release
    _make_stamp_stale(module, repo, service)
    activate(reload=False)  # _ensure_launchd_environment reports no change (old contract: kickstart)
    assert [call[1] for call in calls if call[0] == "launchctl"] == ["bootout", "bootstrap"]


def test_unobserved_rebootstrap_is_retried_on_the_next_deploy(isolated_release):
    """End-to-end scope 1 failure semantics: the fixture never observes boot, so a reload that
    completes but is not confirmed must NOT advance the stamped digest; a follow-on deploy with the
    same on-disk plist therefore still diverges and re-attempts the reload."""
    module, service, repo, prior, _target, _before, calls, activate = isolated_release
    _make_stamp_stale(module, repo, service)
    activate(reload=False)  # diverge -> reload (bootout+bootstrap); boot NOT observed
    assert module._read_stamp(repo, service)["launchd_plist_digest"] == "stale" + "0" * 59
    # Reset the simulated launchd (loaded again) and HEAD, then drive a genuine follow-on deploy.
    calls.clear()
    subprocess.run(["git", "checkout", "-q", "--detach", prior], cwd=repo, check=True)
    activate(reload=False)
    assert [call[1] for call in calls if call[0] == "launchctl"] == ["bootout", "bootstrap"]


def test_failed_rebootstrap_is_retried_on_the_next_deploy(isolated_release):
    """End-to-end scope 1 failure semantics: a failed restart leaves the stamped digest at
    D_prior, so a follow-on deploy still diverges and re-attempts the reload."""
    module, service, repo, prior, _target, _before, calls, activate = isolated_release
    _make_stamp_stale(module, repo, service)
    stamp = activate(reload=False, fail=True)  # diverge -> reload attempted -> launchctl rc1 -> fails
    assert "post_activation_error" in stamp
    assert module._read_stamp(repo, service)["launchd_plist_digest"] == "stale" + "0" * 59
    calls.clear()
    subprocess.run(["git", "checkout", "-q", "--detach", prior], cwd=repo, check=True)
    activate(reload=False, fail=False)  # follow-on retries the reload
    assert [call[1] for call in calls if call[0] == "launchctl"] == ["bootout", "bootstrap"]


@pytest.mark.parametrize("reload", [False, True])
def test_restart_failure_retains_applied_stamp(isolated_release, reload):
    module, service, repo, prior, target, _before, _calls, activate = isolated_release
    stamp = activate(reload=reload, fail=True)
    assert "post_activation_error" in stamp
    assert (stamp["sha"], stamp["prior_sha"]) == (target, prior)
    assert module._read_stamp(repo, service) == stamp
    assert module._git(repo, "rev-parse", "HEAD", runner=module._run) == target


@pytest.mark.parametrize("missing", ["default", "override"])
def test_missing_guard_refuses_and_restores_before_restart(isolated_release, missing, tmp_path):
    module, service, repo, prior, _target, before, calls, activate = isolated_release
    override = tmp_path / "absent-override" if missing == "override" else None
    if missing == "default":
        Path(service.log_guard_dir).rmdir()
    with pytest.raises(module.DeployError, match="log guard unavailable"):
        activate(override=override)
    assert not any(call[0] == "launchctl" for call in calls)
    assert module._git(repo, "rev-parse", "HEAD", runner=module._run) == prior
    assert module._stamp_path(repo, service).read_bytes() == before


def test_explicit_guard_overrides_missing_default(isolated_release, tmp_path):
    module, service, _repo, _prior, target, _before, calls, activate = isolated_release
    Path(service.log_guard_dir).rmdir()
    override = tmp_path / "custom-logs"
    override.mkdir()
    try:
        assert activate(override=override)["sha"] == target
        assert any(call[0] == "launchctl" for call in calls)
    finally:
        override.rmdir()


def _managed_window(module, tmp_path, monkeypatch, stdout, stderr, *, mutate=None, **kwargs):
    from managed_logs import Config
    config = Config(tmp_path / 'daemon.out', tmp_path / 'daemon.err')
    for path in (config.stdout, config.stderr):
        path.write_text('prior\n')
    monkeypatch.setattr(module, '_daemon_log_paths', lambda _: (config.stdout, config.stderr))
    monkeypatch.setattr(module, '_managed_log_config', lambda _: config)
    offsets = module._capture_log_offsets((config.stdout, config.stderr))
    for path, content in ((config.stdout, stdout), (config.stderr, stderr)):
        with path.open('a') as stream:
            stream.write(content)
    if mutate:
        mutate(config)
    kwargs.setdefault('runner', lambda argv, cwd: subprocess.CompletedProcess(argv, 1, '', ''))
    return module._scan_slow_consumer_window(module.SERVICES['chat-streamd-v2'], offsets, **kwargs)


@pytest.mark.parametrize('sink', ['stdout', 'stderr', 'both', 'neither'])
@pytest.mark.parametrize('diagnostic', ['', 'INFO ws client=reader close code=1011\n',
                                      'WARNING slow_consumer overflow client=reader\n'])
def test_legacy_boot_prefix_cannot_prove_managed_generation(tmp_path, monkeypatch, sink, diagnostic):
    module = _load_deploy(tmp_path, monkeypatch)
    boot = module.V2_BOOT_LINE + ' 127.0.0.1:1234\n'
    result = _managed_window(module, tmp_path, monkeypatch,
                             boot if sink in ('stdout', 'both') else '',
                             (boot if sink in ('stderr', 'both') else '') + diagnostic)
    assert result['outcome'] == module.SLOW_CONSUMER_FAILED
    assert result['attribution']['outcome'] == 'attribution_ambiguous'
    assert result['attribution']['reasons']
    assert result['scan_complete']


_ACTIVE_START = 'Mon Jan 1 12:00:00 2024'
_OLD_START = 'Mon Jan 1 11:00:00 2024'
_BOOT_UUID = '9d530b43-e74b-46e4-a7f8-e597f7440ef8'


def _boot(**changes):
    identity = dict(pid=222, start=_ACTIVE_START, boot_uuid=_BOOT_UUID)
    identity.update(changes)
    return 'generation=' + json.dumps(identity) + ' chat_streamd_v2 listening on 127.0.0.1:1234\n'


def _diag(event, conn='reader', **values):
    return 'INFO conn_diag ' + json.dumps(dict(schema=1, event=event, conn_id=conn, **values)) + '\n'


def _proof(module, **changes):
    def runner(argv, cwd):
        pid = int(argv[2])
        return subprocess.CompletedProcess(argv, 0 if pid == 222 else 1,
                                           _ACTIVE_START if pid == 222 else '', '')
    return dict(expected_pid=222, expected_start=_ACTIVE_START, prior_pid=111,
                prior_start=_OLD_START, runner=runner, **changes)


@pytest.mark.parametrize('new_failure,kind', [
    ('', None),
    (_diag('close', close_code=1011, termination='handler_cancelled', client_kind='mobile'), '1011_close'),
    (_diag('force_close', initiator='server', cause='slow_consumer'), 'overflow'),
    (_diag('force_close', initiator='peer', cause='liveness'), 'peer_liveness'),
    ('WARNING slow_consumer overflow client=reader\n', 'overflow'),
    ('INFO ws client=reader close code=4000\n', '4000_close'),
    (_diag('slow_consumer', phase='enter'), None),
])
@pytest.mark.parametrize('old_failure', ['', _diag('close', conn='old', close_code=1011)])
def test_generation_matrix_retains_old_and_condemns_new(tmp_path, monkeypatch, new_failure, kind, old_failure):
    module = _load_deploy(tmp_path, monkeypatch)
    result = _managed_window(module, tmp_path, monkeypatch, _boot(),
                             old_failure + _boot() + new_failure, **_proof(module))
    assert result['scan_complete']
    assert result['attribution']['outcome'] == 'attributed'
    assert result['outcome'] == ('failed' if kind else 'passed')
    assert result['failure_count'] == bool(kind)
    assert [row['kind'] for row in result['failures']] == ([kind] if kind else [])
    assert result['old_generation']['classifier']['failure_count'] == bool(old_failure)
    from deploy.packet.log_scan import lines_many
    assert old_failure in ''.join(lines_many(result['old_generation']['inputs']))
    assert (old_failure not in ''.join(lines_many(result['selected_inputs']))) if old_failure else True


@pytest.mark.parametrize('change', ['missing_stdout','missing_stderr','both_missing','conflicting_uuid',
                                    'duplicate_marker','out_of_order_marker','mismatched_pid',
                                    'reused_pid','missing_start','old_writer_alive','new_writer_dead',
                                    'new_writer_reused_during_scan','invalid_uuid','partial_marker',
                                    'rotated_away','truncate','invalid_utf8'])
def test_uncertain_generation_never_passes(tmp_path, monkeypatch, change):
    module = _load_deploy(tmp_path, monkeypatch)
    stdout = stderr = _boot()
    kwargs = _proof(module)
    mutate = None
    if change == 'missing_stdout': stdout = ''
    elif change == 'missing_stderr': stderr = ''
    elif change == 'both_missing': stdout = stderr = ''
    elif change == 'conflicting_uuid': stderr = _boot(boot_uuid='d84cb03f-ff3e-4461-8650-e30a3832373e')
    elif change == 'duplicate_marker': stderr += _boot()
    elif change == 'out_of_order_marker': stderr += _boot(pid=111, start=_OLD_START)
    elif change == 'mismatched_pid': stderr = _boot(pid=333)
    elif change == 'reused_pid': stderr = _boot(start=_OLD_START)
    elif change == 'missing_start': kwargs['expected_start'] = None
    elif change == 'old_writer_alive':
        kwargs['runner'] = lambda argv, cwd: subprocess.CompletedProcess(argv, 0, _OLD_START if argv[2]=='111' else _ACTIVE_START, '')
    elif change == 'new_writer_dead':
        kwargs['runner'] = lambda argv, cwd: subprocess.CompletedProcess(argv, 1, '', '')
    elif change == 'new_writer_reused_during_scan':
        calls = []
        def runner(argv, cwd):
            calls.append(argv[2])
            return subprocess.CompletedProcess(argv, 0, _ACTIVE_START if calls.count('222')==1 and argv[2]=='222' else 'changed', '')
        kwargs['runner'] = runner
    elif change == 'invalid_uuid': stderr = _boot(boot_uuid='missing')
    elif change == 'partial_marker': stderr = stderr[:-1]
    elif change == 'rotated_away':
        def mutate(config):
            config.stderr.rename(tmp_path/'expired'); config.stderr.write_text(_boot())
    elif change == 'truncate':
        def mutate(config): config.stderr.write_text('')
    elif change == 'invalid_utf8':
        def mutate(config):
            with config.stderr.open('ab') as output: output.write(b'\xff\n')
    result = _managed_window(module, tmp_path, monkeypatch, stdout, stderr, mutate=mutate, **kwargs)
    assert result['outcome'] == 'failed'
    assert result['attribution']['outcome'] == 'attribution_ambiguous'
    assert result['attribution']['reasons']


@pytest.mark.parametrize('reverse', [False, True])
def test_duplicate_out_of_order_diagnostics_remain_current_failures(tmp_path, monkeypatch, reverse):
    module = _load_deploy(tmp_path, monkeypatch)
    records = [_diag('close', close_code=1011),
               _diag('force_close', initiator='server', cause='slow_consumer')]
    if reverse: records.reverse()
    result = _managed_window(module, tmp_path, monkeypatch, _boot()+records[0]*2,
                             _boot()+records[1]*2, **_proof(module))
    assert result['failure_count'] == 1 and result['failures'][0]['kind'] == 'overflow'
    assert result['outcome'] == 'failed'


def test_marker_and_old_evidence_survive_retained_rotation(tmp_path, monkeypatch):
    module = _load_deploy(tmp_path, monkeypatch)
    old = _diag('close', conn='old', close_code=1011)
    def rotate(config):
        raw = config.stderr.read_bytes()
        # A marker split between retained generations remains one record.
        split = raw.index(b'generation=') + 7
        config.stderr.rename(Path(str(config.stderr)+'.1'))
        Path(str(config.stderr)+'.1').write_bytes(raw[:split])
        config.stderr.write_bytes(raw[split:])
    result = _managed_window(module, tmp_path, monkeypatch, _boot(), old+_boot(),
                             mutate=rotate, **_proof(module))
    assert result['outcome'] == 'passed' and result['scan_complete']
    assert result['old_generation']['classifier']['failure_count'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('tls_start_fails', [False, True])
async def test_paused_listener_rejects_then_reconnects_after_markers(tmp_path, caplog, tls_start_fails):
    import asyncio
    import logging
    from unittest.mock import AsyncMock, Mock
    from websockets.asyncio.client import connect
    from server import Server
    from notification_answer_fixture import fixture
    async with fixture(tmp_path, host='fixture') as (_, _, comms, _, sessions, store):
        server = Server(store=store, sessions=sessions, comms=comms, local_host='fixture',
                        binds=['127.0.0.1'], port=0, dot_tls_port=0,
                        dot_tls_binds=[], dot_tls_cert='', dot_tls_key='')
        try:
            with caplog.at_level(logging.INFO, logger='chat_streamd_v2.server'):
                port = await server.bind(start_serving=False)
                with pytest.raises(OSError):
                    async with connect(f'ws://127.0.0.1:{port}', open_timeout=.5):
                        pass
                assert not any('conn_diag ' in row.message for row in caplog.records)
                if tls_start_fails:
                    tls = Mock(start_serving=AsyncMock(side_effect=OSError('fixture TLS listen failure')),
                               wait_closed=AsyncMock())
                    server._tls_ws_server = tls
                await server.start_serving()
                async with connect(f'ws://127.0.0.1:{port}') as peer:
                    assert json.loads(await asyncio.wait_for(peer.recv(), 2))['type'] == 'welcome'
                assert any('conn_diag ' in row.message for row in caplog.records)
                if tls_start_fails:
                    tls.close.assert_called_once()
                    assert server._tls_ws_server is None
        finally:
            await server.close()


@pytest.mark.asyncio
async def test_plain_start_failure_is_fatal():
    from unittest.mock import AsyncMock
    from server import Server
    server = Server.__new__(Server)
    server._ws_server = type('Listener', (), {'start_serving': AsyncMock(side_effect=OSError('listen failed'))})()
    server._tls_ws_server = None
    with pytest.raises(OSError, match='listen failed'):
        await server.start_serving()


def test_ambiguous_cli_failure_names_provenance(tmp_path, monkeypatch, capsys):
    module = _load_deploy(tmp_path, monkeypatch)
    monkeypatch.setattr(module, 'deploy', lambda *_a, **_kw: {
        'slow_consumer': {'outcome': 'failed', 'attribution': {
            'outcome': 'attribution_ambiguous', 'reasons': ['missing paired marker']},
            'failures': [{'client': 'unattributed', 'kind': '1011_close'}]}})
    assert module.main(['chat-streamd-v2']) == module.EXIT_SLOW_CONSUMER_FAILED
    assert 'attribution_ambiguous' in capsys.readouterr().err


def test_unanswered_old_writer_query_cannot_prove_exit(tmp_path, monkeypatch):
    module = _load_deploy(tmp_path, monkeypatch)
    proof = _proof(module)
    def runner(argv, cwd):
        return (subprocess.CompletedProcess(argv, 2, '', 'process query denied') if argv[2] == '111'
                else subprocess.CompletedProcess(argv, 0, _ACTIVE_START, ''))
    proof['runner'] = runner
    result = _managed_window(module, tmp_path, monkeypatch, _boot(), _boot(), **proof)
    assert result['outcome'] == 'failed'
    assert result['attribution']['outcome'] == 'attribution_ambiguous'


@pytest.mark.parametrize('probe_fails', [False, True])
def test_real_boot_producer_preserves_existing_port_readers(monkeypatch, capsys, probe_fails):
    import re
    import main as daemon
    def process_start(*args, **kwargs):
        if probe_fails:
            raise subprocess.CalledProcessError(1, args[0])
        return _ACTIVE_START
    monkeypatch.setattr(daemon.subprocess, 'check_output', process_start)
    daemon._emit_boot_marker('127.0.0.1', 1234)
    captured = capsys.readouterr()
    assert captured.out == captured.err
    # These are the existing wire/soak readers, unchanged.
    assert int(captured.out.rsplit(':', 1)[1].strip()) == 1234
    assert re.search(r'chat_streamd_v2 listening on 127\.0\.0\.1:(\d+)', captured.out)[1] == '1234'
