"""Disposable Git controls for the merge coverage comparison."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from select_tests import check_coverage


ROOT = Path(__file__).resolve().parents[1]
DAEMON = "services/chat-stream-v2/tests"
CLI = "services/agent-orch/tests"
SHARED = "services/_shared/tests"
CLI_LOSS = "cli::tests/test_assistant_brief_cli.py::test_brief_cli_payload"
SHARED_LOSS = "shared::tests/test_checkpoint_policy.py::test_authored_checkpoint_survives_punctuation_and_filter"
NODE_LOSS = ("test/working_timer_paint_contract.test.js::"
             "working timer paints only under activity=working and retains its legacy fallback")
NODE_DUP = 'test/duplicate.test.js::["a b","c"]'
NODE_DOUBLE_LOSS = "test/duplicate.test.js::both"
DAEMON_QUARANTINE = "daemon::tests/test_sentinel.py::test_quarantine[a]"
DAEMON_RENAME = "daemon::tests/test_old.py::test_rename"


class Repo:
    def __init__(self, root):
        self.root = root
        root.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test Agent")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, text=True).strip()

    def write(self, path, content):
        dst = self.root / path
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content)

    def commit(self, *trailers):
        self.git("add", "-A")
        args = ("-m", "\n".join("Coverage-Drop: " + item for item in trailers)) if trailers else ()
        self.git("commit", "-qm", "Fixture change", *args)
        return self.git("rev-parse", "HEAD")

    def quarantine(self, *entries):
        self.write("scripts/quarantine-v2.json", json.dumps({"version": 2, "nonblocking_tests": entries}))

    def check(self, base, head):
        check_coverage(self.root, base, head, python=sys.executable, timeout=60)


def _node_duplicates(count):
    return """
const {test} = require('node:test');
test('a b', async t => {
""" + "  await t.test('c', () => {});\n" * count + """
});
test('a', async t => { await t.test('b c', () => {}); });
test('["a b","c"]', () => {});
""" + "test('both', () => {});\n" * (2 if count == 2 else 0)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    if not all(shutil.which(tool) for tool in ("node", "npm")):
        pytest.skip("requires the Node and npm gate tools")
    monkeypatch.setenv("npm_config_offline", "true")
    repo = Repo(tmp_path / "repo")
    for path in (".gitignore", "scripts/quarantine_plugin.py", "scripts/quarantine_list.py"):
        repo.write(path, (ROOT / path).read_text())
    for path in (ROOT / "scripts/lib").glob("*.js"):
        repo.write(str(path.relative_to(ROOT)), path.read_text())
    config = json.loads((ROOT / "scripts/gate-env.json").read_text())
    npm = config["profiles"]["pentacle"]["targets"]["npm-test"]
    npm["default_patterns"] = ["test/*.test.js", "test/duplicate.test.js"]
    npm["npm_build"] = []
    repo.write("scripts/gate-env.json", json.dumps(config))
    package = {"name": "fixture", "version": "1.0.0"}
    repo.write("package.json", json.dumps(package))
    repo.write("package-lock.json", json.dumps({**package, "lockfileVersion": 3, "packages": {"": package}}))
    repo.write("scripts/quarantine.json", '{"version":1,"nonblocking_tests":[]}')
    repo.quarantine()
    for path in (DAEMON, CLI, SHARED):
        repo.write(f"{path}/test_sentinel.py", "def test_sentinel(): pass\n")
    repo.write(f"{DAEMON}/test_sentinel.py", """import pytest

def test_sentinel(): pass

@pytest.mark.parametrize('case', ['a', 'b'])
def test_quarantine(case): pass
""")
    repo.write(f"{DAEMON}/test_old.py", "def test_rename(): pass\n")
    repo.write(f"{CLI}/test_assistant_brief_cli.py", "def test_brief_cli_payload(): pass\n")
    repo.write(f"{SHARED}/test_checkpoint_policy.py", "def test_authored_checkpoint_survives_punctuation_and_filter(): pass\n")
    repo.write("test/working_timer_paint_contract.test.js",
               f"require('node:test').test({json.dumps(NODE_LOSS.split('::', 1)[1])}, () => {{}});\n")
    repo.write("test/duplicate.test.js", _node_duplicates(2))
    repo.write("test/sentinel.test.js", "require('node:test').test('sentinel', () => {});\n")
    return repo


def test_old_base_cutover_does_not_start_collections(tmp_path, capsys):
    repo = Repo(tmp_path / "repo")
    repo.write("scripts/quarantine.json", '{"version":1,"nonblocking_tests":[]}')
    repo.write("scripts/admission_inputs.py", "# legacy selector")
    base = repo.commit()
    with pytest.raises(RuntimeError, match="Coverage comparison inconclusive"):
        repo.check(base, base)
    repo.quarantine()
    head = repo.commit()
    repo.check(base, head)
    assert capsys.readouterr().out == "Coverage comparison unavailable: merge base predates gate cutover\n"


@pytest.mark.parametrize("contents", [None, '{"version":2,"nonblocking_tests": [42]}'])
def test_legacy_snapshot_cannot_hide_missing_or_invalid_active_list(repo, contents):
    active = repo.root / "scripts/quarantine-v2.json"
    active.unlink()
    if contents is not None:
        active.write_text(contents)
    broken = repo.commit()
    repo.quarantine()
    good = repo.commit()
    with pytest.raises(RuntimeError, match="Coverage comparison inconclusive"):
        repo.check(broken, good)
    with pytest.raises(RuntimeError, match="Coverage comparison inconclusive"):
        repo.check(good, broken)


def test_cross_family_occurrences_require_exact_tip_trailers(repo, capsys):
    base = repo.commit()
    repo.check(base, base)
    assert capsys.readouterr().out.count("npm ci") == 1
    (repo.root / f"{CLI}/test_assistant_brief_cli.py").unlink()
    (repo.root / f"{SHARED}/test_checkpoint_policy.py").unlink()
    repo.git("mv", f"{DAEMON}/test_old.py", f"{DAEMON}/test_new.py")
    repo.quarantine({"id": DAEMON_QUARANTINE, "owner": "fixture", "reason": "test", "expires": "2999-01-01"})
    (repo.root / "test/working_timer_paint_contract.test.js").unlink()
    repo.write("test/duplicate.test.js", _node_duplicates(1))
    repo.write("test/sentinel.test.js", "require('node:test').test('sentinel', {skip: 'fixture'}, () => {});\n")
    losses = (CLI_LOSS, SHARED_LOSS, DAEMON_RENAME, DAEMON_QUARANTINE, NODE_LOSS, NODE_DUP,
              "test/sentinel.test.js::sentinel")
    head = repo.commit(CLI_LOSS + "*", DAEMON_QUARANTINE.replace("[a]", "[b]"),
                       NODE_LOSS.replace("test/", "elsewhere/"), NODE_DOUBLE_LOSS)
    with pytest.raises(RuntimeError, match="Coverage loss undeclared"):
        repo.check(base, head)
    log = capsys.readouterr().out
    for test in losses:
        assert "Coverage loss " + test + ":" in log
    assert "lost 1 (1 quarantined)" in log
    assert "lost 1 (1 skipped/todo)" in log
    assert f"Coverage loss {NODE_DUP}: 2 -> 1, lost 1" in log
    assert f"Coverage loss {NODE_DOUBLE_LOSS}: 2 -> 0, lost 2 (2 missing), declared 1/2" in log
    assert "2 -> 2 invocations" in log
    assert log.count("declared 0/1") == len(losses)
    assert "R100 " + DAEMON + "/test_old.py -> " + DAEMON + "/test_new.py" in log
    repo.git("commit", "--amend", "-m", "Declare fixture losses", "-m",
             "\n".join("Coverage-Drop: " + test for test in (*losses, NODE_DOUBLE_LOSS, NODE_DOUBLE_LOSS)))
    repo.check(base, "HEAD")
    declared = capsys.readouterr().out
    assert declared.count("declared 1/1") == len(losses)
    assert declared.count("declared 2/2") == 1
    repo.git("commit", "--allow-empty", "-qm", "A new tip without declarations")
    with pytest.raises(RuntimeError, match="Coverage loss undeclared"):
        repo.check(base, "HEAD")


def test_historical_expiry_allows_restoration_but_candidate_expiry_fails(repo, capsys):
    repo.quarantine({"id": DAEMON_QUARANTINE, "owner": "fixture", "reason": "test", "expires": "2000-01-01"})
    base = repo.commit()
    with pytest.raises(RuntimeError, match="Coverage comparison inconclusive"):
        repo.check(base, base)
    assert DAEMON_QUARANTINE in capsys.readouterr().out
    repo.quarantine()
    repo.check(base, repo.commit())


def test_collection_failure_keeps_independent_known_loss(repo, capsys):
    base = repo.commit()
    (repo.root / f"{CLI}/test_assistant_brief_cli.py").unlink()
    repo.write(f"{SHARED}/test_break.py", "def test_bad(:\n")
    head = repo.commit()
    with pytest.raises(RuntimeError, match="Coverage comparison inconclusive"):
        repo.check(base, head)
    log = capsys.readouterr().out
    assert "Coverage loss " + CLI_LOSS + ":" in log
    assert f"{head} shared:" in log
