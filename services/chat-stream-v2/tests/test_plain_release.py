"""Plain release journeys against Git and owned command fixtures, never launchd."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
PREFIX = "PENTACLE_RELEASE_"
SHA_UNAVAILABLE = "e" * 40


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True, stderr=subprocess.PIPE).strip()


def commit(path, name, value):
    file = path / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(value)
    git(path, "add", name)
    git(path, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", value)
    return git(path, "rev-parse", "HEAD")


def executable(path, body):
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o700)


@pytest.fixture
def rig(tmp_path):
    origin, src, rt = (tmp_path / x for x in ("origin.git", "source", "runtime checkout"))
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    git(tmp_path, "init", "--initial-branch=main", str(src))
    requirements = "services/chat-stream-v2/requirements.txt"
    service = DEPLOY.parent
    tree = ast.parse((service / "provider_cli_artifact.py").read_text())
    identity = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "code_identity")
    names = ast.literal_eval(next(n.value for n in identity.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "files" for t in n.targets)))
    for name in names:
        dst = src / "services/chat-stream-v2" / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes((service / name).read_bytes())
    shared = src / "services/_shared/provider_cli_release.py"
    shared.parent.mkdir(parents=True, exist_ok=True)
    shared.write_bytes((service.parent / "_shared/provider_cli_release.py").read_bytes())
    git(src, "add", "services")
    root = commit(src, requirements, "requirements")
    prior = commit(src, "version", "prior")
    target = commit(src, "version", "target")
    changed_requirements = commit(src, requirements, "changed")
    git(src, "checkout", "-q", "--detach", target)
    helper_change = commit(src, "services/chat-stream-v2/" + names[0], "helper change")
    git(src, "branch", "helper-change", helper_change)
    git(src, "checkout", "-q", "-b", "other", root)
    diverged = commit(src, "version", "diverged")
    git(src, "push", str(origin), "main", "other", "helper-change")
    git(tmp_path, "clone", str(origin), str(rt))
    git(rt, "checkout", "--detach", prior)
    only_local = commit(rt, "version", "not published")
    git(rt, "checkout", "--detach", prior)

    bins = tmp_path / "bin"
    bins.mkdir()
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"pid": 100, "serving": prior, "mode": "normal", "restarts": []}))
    log = tmp_path / "daemon.log"
    log.write_text("")
    clock = tmp_path / "clock"
    clock.write_text("1000")
    common = (
        "import json, os, pathlib, subprocess, sys\n"
        "state = pathlib.Path(os.environ['FIXTURE_STATE'])\n"
        "data = json.loads(state.read_text())\n"
    )
    executable(bins / "launchctl", common + '''
assert sys.argv[-1] == 'gui/' + str(os.getuid()) + '/demo.daemon'
if sys.argv[1] == 'print':
    if data['pid']:
        print('  pid =', data['pid'])
    print('  stdout path =', os.environ['FIXTURE_LOG'])
else:
    assert sys.argv[1:-1] == ['kickstart', '-k']
    sha = subprocess.check_output(
        ['git', '-C', os.environ['PENTACLE_RELEASE_RUNTIME_CHECKOUT'], 'rev-parse', 'HEAD'],
        text=True,
    ).strip()
    data['restarts'].append(sha)
    if 'FIXTURE_POLICY' in os.environ:
        policy = pathlib.Path(os.environ['FIXTURE_POLICY'])
        data.setdefault('policies', []).append(policy.read_text())
    data['pid'] = 100 + len(data['restarts'])
    if data['mode'] == 'failure' or (data['mode'] == 'putback' and len(data['restarts']) == 1):
        data['serving'] = 'wrong-sha'
    else:
        data['serving'] = sha
    pathlib.Path(os.environ['FIXTURE_LOG']).write_text('generation=' + json.dumps({'pid': data['pid'], 'start': 'Fri Nov 27 08:00:00 2026', 'boot_uuid': '12345678-1234-4234-8234-123456789abc'}, separators=(',', ':')) + ' chat_streamd_v2 listening on fixture\\n')
    state.write_text(json.dumps(data))
''')
    probe = bins / "probe wrapper"
    executable(probe, common + '''
if sys.argv[1] == '-c':
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
if pathlib.Path(sys.argv[1]).name == 'restart_probe.py':
    sys.exit(int(os.environ.get('FIXTURE_PROBE_FAIL', '0')) if sys.argv[2] == 'verify' else 0)
if pathlib.Path(sys.argv[1]).name == 'helper_release.py':
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
assert pathlib.Path(sys.argv[1]).name == 'welcome_probe.py'
assert sys.argv[2] == 'ws://127.0.0.1:12345'
print(data['serving'])
''')
    executable(bins / "date", '''
import os, pathlib, sys
p = pathlib.Path(os.environ['FIXTURE_CLOCK'])
n = int(p.read_text())
p.write_text(str(n + 31))
print(n if sys.argv[1] == '+%s' else '00:00:00')
''')
    executable(bins / "sleep", "pass\n")
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "PATH": str(bins) + os.pathsep + os.environ["PATH"],
           "FIXTURE_STATE": str(state), "FIXTURE_CLOCK": str(clock), "FIXTURE_LOG": str(log),
           PREFIX + "RUNTIME_CHECKOUT": str(rt), PREFIX + "LAUNCHD_LABEL": "demo.daemon",
           PREFIX + "RUNTIME_PYTHON": str(probe), PREFIX + "DAEMON_URL": "ws://127.0.0.1:12345"}
    for setting in ("PUTBACK_WORK", "RECORD_DIR", "PROBE_TARGET"):
        env.pop(PREFIX + setting, None)
    return env, rt, state, {"prior": prior, "target": target, "diverged": diverged,
                            "changed_requirements": changed_requirements, "only_local": only_local, "helper_change": helper_change}


def invoke(env, *args):
    return subprocess.run([str(DEPLOY / "plain_release.sh"), *args], env=env,
                          capture_output=True, text=True, timeout=20)


def clean_prior(rt, state, prior):
    assert git(rt, "rev-parse", "HEAD") == prior
    assert not json.loads(state.read_text())["restarts"]


def change(state, **kwargs):
    state.write_text(json.dumps({**json.loads(state.read_text()), **kwargs}))


@pytest.mark.parametrize("setting", ["RUNTIME_CHECKOUT", "LAUNCHD_LABEL", "RUNTIME_PYTHON", "DAEMON_URL"])
def test_missing_configuration_refuses_before_any_change(rig, setting):
    env, rt, state, shas = rig
    del env[PREFIX + setting]
    result = invoke(env, shas["target"])
    assert result.returncode == 2 and "set PENTACLE_RELEASE_" in result.stdout
    clean_prior(rt, state, shas["prior"])


@pytest.mark.parametrize(("case", "reason"), [
    ("unreadable", "runtime checkout unreadable"), ("dirty", "checkout is not clean"),
    ("prior", "different full sha"), ("absent", "target not in origin"),
    ("only_local", "not contained in an origin ref"), ("diverged", "not an ancestor"),
    ("changed_requirements", "requirements changed"), ("no_pid", "no running daemon pid"),
    ("wrong_serving", "not serving the checked-out commit"), ("fetch", "fetch failed"),
])
def test_refusals_do_not_check_out_or_restart(rig, case, reason):
    env, rt, state, shas = rig
    target = shas.get(case, SHA_UNAVAILABLE if case == "absent" else shas["target"])
    if case == "unreadable":
        env[PREFIX + "RUNTIME_CHECKOUT"] = str(rt / "missing")
    elif case == "dirty":
        (rt / "version").write_text("unsaved")
    elif case == "no_pid":
        change(state, pid=None)
    elif case == "wrong_serving":
        change(state, serving=target)
    elif case == "fetch":
        git(rt, "remote", "set-url", "origin", str(rt / "missing.git"))
    result = invoke(env, target)
    assert result.returncode == 2, result
    assert "REFUSED" in result.stdout and reason in result.stdout, result
    clean_prior(rt, state, shas["prior"])


def test_plain_dry_run_then_release(rig):
    env, rt, state, shas = rig
    dry = invoke(env, shas["target"], "--dry-run")
    assert dry.returncode == 0 and "DRY RUN" in dry.stdout, dry
    clean_prior(rt, state, shas["prior"])
    release = invoke(env, shas["target"])
    assert release.returncode == 0 and "RELEASED" in release.stdout, release
    assert git(rt, "rev-parse", "HEAD") == shas["target"]
    observed = json.loads(state.read_text())
    assert observed["restarts"] == [shas["target"]]
    assert observed["pid"] == 101 and observed["serving"] == shas["target"]


@pytest.mark.parametrize(("mode", "code", "outcome"), [
    ("putback", 4, "PRIOR RESTORED"), ("failure", 5, "NEEDS A HUMAN"),
])
def test_plain_putback_and_failed_putback(rig, mode, code, outcome):
    env, rt, state, shas = rig
    change(state, mode=mode)
    result = invoke(env, shas["target"])
    assert result.returncode == code and outcome in result.stdout, result
    assert git(rt, "rev-parse", "HEAD") == shas["prior"]
    observed = json.loads(state.read_text())
    assert observed["restarts"] == [shas["target"], shas["prior"]]
    assert observed["pid"] == 102
    assert (observed["serving"] == shas["prior"]) == (code == 4)


def test_putback_restores_policy_bytes_and_mode_before_prior_restart(rig, tmp_path):
    from deploy import helper_release as helper

    env, rt, state, shas = rig
    policy = tmp_path / "account policy"
    before, after = b'{"executors": {}}\n', b'{"executors":{"demo":{"script":"candidate"}}}\n'
    policy.write_bytes(after)
    policy.chmod(0o640)
    work = tmp_path / "recovery state"
    work.mkdir()
    (work / "policy.before").write_bytes(before)
    helper.save(work, {"policy": {"path": str(policy), "sha256": helper.digest(before),
                                 "written_sha256": helper.digest(after), "mode": 0o600}})
    env[PREFIX + "PUTBACK_WORK"] = str(work)
    env["FIXTURE_POLICY"] = str(policy)
    change(state, mode="putback")

    result = invoke(env, shas["target"])
    assert result.returncode == 4, result
    assert policy.read_bytes() == before and (policy.stat().st_mode & 0o777) == 0o600
    assert json.loads(state.read_text())["policies"] == [after.decode(), before.decode()]
    assert helper.read(work / "state.json")["phase"] == "policy_restored"
    assert git(rt, "rev-parse", "HEAD") == shas["prior"]


def test_unstage_removes_only_owned_unreferenced_helpers_and_records_removal(tmp_path):
    import io
    import tarfile

    from deploy import helper_release as helper

    installer = helper.cr.load_installer(helper.SERVICE.parents[1])
    sha = "a" * 40
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    with tarfile.open(bundle / "pentacle.tar", "w") as archive:
        content = b"fixture"
        entry = tarfile.TarInfo("example.txt")
        entry.size = len(content)
        archive.addfile(entry, io.BytesIO(content))
    manifest = {"commit": sha, "archive_sha256": helper.digest((bundle / "pentacle.tar").read_bytes())}
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    owned = installer.Target("own", "unused", str(tmp_path / "one" / ".pentacle-account-helpers"), True)
    reused = installer.Target("existing", "unused", str(tmp_path / "two" / ".pentacle-account-helpers"), True)
    installer.stage_helper_source(reused, bundle, sha, manifest)
    token = "b" * 32
    helper.stage_owned(installer, owned, bundle, sha, manifest, token)
    helper.stage_owned(installer, reused, bundle, sha, manifest, token)
    assert helper.ownership(installer, owned, sha, token) == "owned"
    assert helper.ownership(installer, reused, sha, token) == "foreign"

    def record(target):
        script = helper.staged_path(target, sha) / "app" / helper.cr.HELPER
        return {"script": str(script), "target": vars(target), "stage_started": True}

    work = tmp_path / "work"
    work.mkdir()
    policy = tmp_path / "policy.json"
    hosts = {"existing": record(reused), "own": record(owned)}
    helper.save(work, {"target": sha, "token": token, "policy": {"path": str(policy)}, "hosts": hosts})
    policy.write_text(json.dumps({"executors": {
        "own": {"script": str(tmp_path / "unrelated")},
        "existing": {"script": hosts["own"]["script"]},
    }}))
    with pytest.raises(helper.Invalid, match="policy may reference"):
        helper.unstage(work)
    assert helper.staged_path(owned, sha).is_dir()
    assert helper.staged_path(reused, sha).is_dir()

    alias = tmp_path / "referenced-alias"
    alias.symlink_to(helper.staged_path(owned, sha) / "app")
    policy.write_text(json.dumps({"executors": {
        "own": {"script": str(alias / helper.cr.HELPER)},
        "existing": {"script": hosts["existing"]["script"]},
    }}))
    with pytest.raises(helper.Invalid, match="policy may reference"):
        helper.unstage(work)
    assert helper.staged_path(owned, sha).is_dir()

    policy.write_text(json.dumps({"executors": {
        "own": {"script": str(tmp_path / "unrelated")},
        "existing": {"script": hosts["existing"]["script"]},
    }}))
    assert helper.unstage(work) == helper.unstage(work) == 0
    assert not helper.staged_path(owned, sha).exists()
    assert helper.staged_path(reused, sha).is_dir()
    state = helper.read(work / "state.json")
    assert state["hosts"]["own"]["created"] is True
    assert state["hosts"]["own"]["removed"] is True
    assert not state["hosts"]["existing"].get("removed")


def test_welcome_probe_ignores_non_welcome_frames(monkeypatch, capsys):
    import asyncio

    from deploy import welcome_probe

    class FixtureSocket:
        async def __aenter__(self):
            self.frames = iter(('{"type":"unrelated"}', '{"type":"welcome","runtime_sha":"sample-sha"}'))
            return self

        async def __aexit__(self, *exc):
            pass

        async def recv(self):
            return next(self.frames)

    def connect(url, open_timeout):
        assert url == "ws://fixture.invalid" and open_timeout == 5.0
        return FixtureSocket()

    monkeypatch.setattr(welcome_probe.websockets, "connect", connect)
    assert asyncio.run(welcome_probe.probe("ws://fixture.invalid")) == 0
    assert capsys.readouterr().out == "sample-sha\n"


def test_release_writes_record(rig, tmp_path):
    env, rt, state, shas = rig
    records_dir = tmp_path / 'records'
    records_dir.mkdir()
    env[PREFIX + 'RECORD_DIR'] = str(records_dir)
    result = invoke(env, shas['target'])
    assert result.returncode == 0, result
    records = list(records_dir.glob('*.json'))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record['target'] == shas['target'] and record['prior'] == shas['prior']
    assert record['outcome'] == 'released' and record['exit_code'] == 0
    assert record['old_pid'] == '100' and record['new_pid'] == '101'
    assert record['boot_id'] == '12345678-1234-4234-8234-123456789abc'
    assert record['preflight'].endswith('Z') and record['stop'].endswith('Z') and record['serving'].endswith('Z')


def test_helper_change_refused_for_plain_but_helper_path_proceeds(rig, tmp_path, monkeypatch):
    env, rt, state, shas = rig
    result = invoke(env, shas['helper_change'])
    assert result.returncode == 2 and 'needs the helper release' in result.stdout
    clean_prior(rt, state, shas['prior'])
    from types import SimpleNamespace
    from deploy import helper_release as helper
    work = tmp_path / 'helper-work'
    calls = []
    monkeypatch.setattr(helper, 'plain', lambda args, **kwargs: calls.append(kwargs) or 2)
    assert helper.release(SimpleNamespace(work=work)) == 2
    assert calls == [{'dry_run': True, 'work': work}]
    env[PREFIX + 'PUTBACK_WORK'] = str(work)
    assert invoke(env, shas['helper_change'], '--dry-run').returncode == 0
    assert invoke(env, shas['helper_change']).returncode == 0


@pytest.mark.parametrize(('mode', 'outcome', 'code'), [('putback', 'put_back', 4), ('failure', 'needs_human', 5)])
def test_record_putback_and_needs_human(rig, tmp_path, mode, outcome, code):
    env, rt, state, shas = rig
    directory = tmp_path / 'records'
    directory.mkdir()
    env[PREFIX + 'RECORD_DIR'] = str(directory)
    change(state, mode=mode)
    result = invoke(env, shas['target'])
    assert result.returncode == code, result
    records = list(directory.glob('*.json'))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record['outcome'] == outcome and record['exit_code'] == code
    assert record['target'] == shas['target'] and record['prior'] == shas['prior']


@pytest.mark.parametrize(('fails', 'verdict', 'code'), [(False, 'passed', 0), (True, 'failed', 6)])
def test_probe_verdict_recorded(rig, tmp_path, fails, verdict, code):
    env, rt, state, shas = rig
    directory = tmp_path / 'records'
    directory.mkdir()
    env.update({PREFIX + 'RECORD_DIR': str(directory), PREFIX + 'PROBE_TARGET': 'fixture:target', 'FIXTURE_PROBE_FAIL': str(int(fails))})
    result = invoke(env, shas['target'])
    assert result.returncode == code, result
    records = list(directory.glob('*.json'))
    assert len(records) == 1
    assert json.loads(records[0].read_text())['probe_verdict'] == verdict
    assert git(rt, 'rev-parse', 'HEAD') == shas['target']
