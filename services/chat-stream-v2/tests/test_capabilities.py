"""Synthetic tests for capability gates, not tests of the host's availability."""
from contextlib import contextmanager
import errno
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import capabilities as cap

pytest_plugins = ["pytester"]
pytestmark = pytest.mark.timeout(30)


@contextmanager
def child_stub(**kwargs):
    yield SimpleNamespace(pid=42420, poll=lambda: None)


def test_probe_registry_caches_true_false_and_error():
    calls = []
    registry = cap.Registry({"yes": lambda: (calls.append("yes") or cap.ProbeResult("available", "synthetic")),
        "no": lambda: cap.ProbeResult("absent", "synthetic"), "broken": lambda: 1})
    assert registry.check("yes").status == registry.check("yes").status == "available"
    assert calls == ["yes"]
    assert registry.check("no").status == "absent"
    assert registry.check("broken").status == "error"
    assert registry.check("unknown").status == "error"


def test_missing_binary_is_absent_but_timeout_and_malformed_output_are_errors():
    assert cap.command_probe("argv", which=lambda _: None).status == "absent"
    def timed(*args, **kwargs):
        assert kwargs["timeout"] == 2
        raise subprocess.TimeoutExpired(args[0], 2)
    registry = cap.Registry({"ps": lambda: cap.command_probe("argv", which=lambda _: "synthetic-ps", run=timed, child_factory=child_stub)})
    assert registry.check("ps").status == "error"
    reply = lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="invalid response", stderr="")
    with pytest.raises(RuntimeError, match="malformed"):
        cap.command_probe("birth", which=lambda _: "synthetic-ps", run=reply, child_factory=child_stub)


def test_command_probe_targets_only_the_owned_pid():
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="python TH_H2_OWNED_ARGV_SENTINEL", stderr="")
    assert cap.command_probe("argv", run=run, which=lambda _: "synthetic-ps", child_factory=child_stub).status == "available"
    assert calls == [["synthetic-ps", "-ww", "-p", "42420", "-o", "args="]]


def test_declared_dependencies_use_exact_isolated_interpreter_and_never_absence():
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        assert kwargs["timeout"] == 5
        return SimpleNamespace(returncode=1, stdout="", stderr="ModuleNotFoundError")
    result = cap.isolated_dependencies(run=run)
    assert calls[0][:3] == [sys.executable, "-I", "-c"]
    assert "websockets.asyncio.client" in calls[0][3] and "cryptography" in calls[0][3]
    assert result.status == "error" and "same interpreter" in result.reason


def test_socket_cleanup_failure_is_error_not_absence(tmp_path):
    class FakeSocket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def bind(self, path): pass
        def close(self): pass
    manager = SimpleNamespace(name=str(tmp_path), cleanup=lambda: (_ for _ in ()).throw(PermissionError(errno.EACCES, "synthetic")))
    registry = cap.Registry({"socket": lambda: cap.socket_directory_probe(candidates=[str(tmp_path)], socket_factory=lambda *args: FakeSocket(), temporary=lambda **kwargs: manager)})
    assert registry.check("socket").status == "error"


def test_tmux_probe_uses_only_its_disposable_socket(tmp_path):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        assert kwargs["timeout"] in {2, 3}
        assert args[1:4] == ["-f", __import__("os").devnull, "-S"] and args[4].startswith("/synthetic/short")
        assert kwargs["env"]["HOME"] == "/synthetic/short"
        if len(calls) == 4: return SimpleNamespace(returncode=1, stdout="", stderr="no server running")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    def temporary(**kwargs): return SimpleNamespace(name="/synthetic/short", cleanup=lambda: None)
    result = cap.tmux_probe(run=run, which=lambda _: "synthetic-tmux", temporary=temporary)
    assert result.status == "available"
    assert [row[5] for row in calls] == ["new-session", "has-session", "kill-server", "has-session"]


def nested(pytester, monkeypatch, status, *, strict=False, rogue=False, managed=True, environment=False, no_timeout=False, workers=None):
    monkeypatch.setenv("PYTHONPATH", str(Path(cap.__file__).parent))
    monkeypatch.delenv("PENTACLE_TEST_STRICT_CAPABILITIES", raising=False)
    pytester.makeini("[pytest]\ntimeout = 10\ntimeout_method = thread\n")
    name = "environment: isolated Python dependencies" if environment else "synthetic"
    pytester.makeconftest(f'''
import pytest
import capabilities as cap
@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    config._th_capabilities = cap.Registry({{{name!r}: lambda: cap.ProbeResult({status!r}, "synthetic result")}})
    config._th_managed_nodes = {{"test_case.py::test_case"}} if {managed!r} else set()
''')
    marker = "@pytest.mark.requires_environment" if environment else '@pytest.mark.requires("synthetic")'
    if rogue: marker = ""
    pytester.makepyfile(test_case=f'import pytest\n{marker}\ndef test_case():\n    '+('pytest.skip("rogue direct skip")' if rogue else 'assert True')+'\n')
    args = ["-q", "-p", "capabilities"]
    if strict: args.append("--strict-capabilities")
    if no_timeout: args += ["-p", "no:timeout"]
    if workers: args += ["-n", str(workers)]
    return pytester.runpytest_subprocess(*args, timeout=20)


@pytest.mark.parametrize("status,strict,expected", [("available", False, {"passed": 1}), ("absent", False, {"skipped": 1}), ("absent", True, {"errors": 1}), ("error", False, {"errors": 1})])
def test_true_absent_strict_and_error_outcomes(pytester, monkeypatch, status, strict, expected):
    result = nested(pytester, monkeypatch, status, strict=strict)
    result.assert_outcomes(**expected)
    result.stdout.fnmatch_lines(["*synthetic: " + status + "; skipped=*synthetic result*"])
    if strict: assert "1 skipped" not in result.stdout.str()


def test_changed_direct_skip_fails_but_untouched_skip_is_not_rewritten(pytester, monkeypatch):
    nested(pytester, monkeypatch, "available", rogue=True).assert_outcomes(failed=1)


def test_unrelated_direct_skip_remains_outside_guard(pytester, monkeypatch):
    nested(pytester, monkeypatch, "available", rogue=True, managed=False).assert_outcomes(skipped=1)


def test_broken_environment_never_skips_even_when_result_is_absent(pytester, monkeypatch):
    nested(pytester, monkeypatch, "absent", environment=True).assert_outcomes(errors=1)


def test_missing_declared_timeout_plugin_is_an_environment_error(pytester, monkeypatch):
    result = nested(pytester, monkeypatch, "available", no_timeout=True)
    result.assert_outcomes(errors=1)
    assert "required environment broken" in result.stdout.str()


def test_changed_test_skip_calls_are_confined_to_requires():
    import ast
    root = Path(__file__).parent
    manifest = json.loads((root / "fixtures/h2_capabilities/changed_tests.json").read_text())
    for key in manifest["node_ids"]:
        filename, name = key.split("::")
        tree = ast.parse((root / filename).read_text())
        node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
        for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
            if isinstance(call.func, ast.Attribute) and call.func.attr in {"skip", "skipif", "importorskip"}:
                pytest.fail(f"changed test bypasses requires: {key}")


def test_original_assertions_and_tracked_skip_are_unchanged():
    import ast
    import hashlib
    root = Path(__file__).parent
    pins = json.loads((root / "fixtures/h2_capabilities/original_assertions.json").read_text())
    for key, expected in pins["assertion_sha256"].items():
        filename, name = key.split("::")
        source = (root / filename).read_text()
        node = next(n for n in ast.parse(source).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
        # ast.unparse is stable across interpreter versions; ast.dump changed its default output in 3.13.
        assertions = [ast.unparse(n) for n in ast.walk(node) if isinstance(n, ast.Assert)]
        assert hashlib.sha256(json.dumps(assertions, sort_keys=True).encode()).hexdigest() == expected, key
        if name == "test_sigkill_then_reap_manifest_cleans":
            first = min([node.lineno] + [d.lineno for d in node.decorator_list])
            text = "\n".join(source.splitlines()[first - 1:node.end_lineno])
            assert hashlib.sha256(text.encode()).hexdigest() == pins["untouched_skip_source_sha256"]


def test_fixture_manifest_is_explicitly_synthetic_and_hash_pinned():
    import hashlib
    folder = Path(__file__).parent / "fixtures/h2_capabilities"
    manifest = json.loads((folder / "MANIFEST.json").read_text())
    assert {Path(row["path"]).name for row in manifest["files"]} == {p.name for p in folder.iterdir() if p.name != "MANIFEST.json"}
    for row in manifest["files"]:
        raw = (Path(__file__).parent.parent / row["path"]).read_bytes()
        assert row["synthetic"] is True and row["contains_real_traffic"] is False
        assert len(raw) == row["bytes"]
        assert hashlib.sha256(raw).hexdigest() == row["sha256"]


def test_lsof_probe_matches_existing_standard_path_fallback():
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=f"p42420\nf3\naw\nn{args[-1]}\n", stderr="")
    result = cap.command_probe("lsof", run=run, which=lambda _: None, child_factory=child_stub,
        isfile=lambda path: path == "/usr/sbin/lsof", access=lambda path, mode: path == "/usr/sbin/lsof")
    assert result.status == "available" and calls[0][0] == "/usr/sbin/lsof"


def test_lsof_probe_accepts_resolved_path_and_rejects_a_foreign_one(monkeypatch):
    import os
    # lsof prints the resolved path; a temp dir behind a symlink (macOS /var -> /private/var) must still verify.
    monkeypatch.setattr(os.path, "realpath", lambda path: "/resolved" + str(path))
    resolved = lambda args, **kwargs: SimpleNamespace(returncode=0, stdout=f"p42420\nf3\naw\nn/resolved{args[-1]}\n", stderr="")
    assert cap.command_probe("lsof", run=resolved, which=lambda _: "synthetic-lsof", child_factory=child_stub).status == "available"
    foreign = lambda args, **kwargs: SimpleNamespace(returncode=0, stdout="p42420\nf3\naw\nn/somewhere/else.txt\n", stderr="")
    registry = cap.Registry({"lsof": lambda: cap.command_probe("lsof", run=foreign, which=lambda _: "synthetic-lsof", child_factory=child_stub)})
    assert registry.check("lsof").status == "error"


def test_tmux_probe_accepts_server_exited_race_after_owned_kill():
    # After kill-server, Linux tmux may report the dying owned server as "server exited unexpectedly".
    def run(args, **kwargs):
        verb = next(word for word in args if word in {"new-session", "has-session", "kill-server"})
        run.calls.append(verb)
        if verb == "has-session" and run.calls.count("has-session") == 2:
            return SimpleNamespace(returncode=1, stdout="", stderr="server exited unexpectedly\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    run.calls = []
    result = cap.tmux_probe(run=run, which=lambda _: "synthetic-tmux", candidates=["/tmp"])
    assert result.status == "available" and run.calls == ["new-session", "has-session", "kill-server", "has-session"]
    # A server that is still answering after the kill remains a cleanup failure.
    alive = lambda args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr="")
    registry = cap.Registry({"tmux": lambda: cap.tmux_probe(run=alive, which=lambda _: "synthetic-tmux", candidates=["/tmp"])})
    assert registry.check("tmux").status == "error"


def test_exec_permission_denied_is_absent_not_error():
    def denied(args, **kwargs):
        raise PermissionError(1, "Operation not permitted")
    assert cap.command_probe("argv", run=denied, which=lambda _: "synthetic-ps", child_factory=child_stub).status == "absent"
    refused = lambda args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="ps: Operation not permitted\n")
    assert cap.command_probe("argv", run=refused, which=lambda _: "synthetic-ps", child_factory=child_stub).status == "absent"


def test_scope_manifest_covers_all_new_portability_tests():
    import ast
    root = Path(__file__).parent
    names = set(json.loads((root / "fixtures/h2_capabilities/changed_tests.json").read_text())["node_ids"])
    for filename in ["test_capabilities.py", "test_h2_portable_fixtures.py", "test_consent_host_ceremony.py", "test_ssh_control_master_reset.py", "test_harness_reaping.py"]:
        for node in ast.parse((root / filename).read_text()).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                key = filename + "::" + node.name
                if node.name == "test_sigkill_then_reap_manifest_cleans": assert key not in names
                else: assert key in names, key


def test_tmux_probe_recovers_from_long_first_directory_without_host_paths():
    created = []; cleaned = []; calls = []
    def temporary(**kwargs):
        created.append(kwargs["dir"])
        name = "/synthetic/" + "x" * 110 if len(created) == 1 else "/synthetic/short"
        return SimpleNamespace(name=name, cleanup=lambda: cleaned.append(name))
    def run(args, **kwargs):
        calls.append(args)
        assert args[4] == "/synthetic/short/s"
        return SimpleNamespace(returncode=1 if len(calls) == 4 else 0, stdout="", stderr="no server running" if len(calls) == 4 else "")
    result = cap.tmux_probe(run=run, which=lambda _: "synthetic-tmux", temporary=temporary, candidates=["long-first", "short-second"])
    assert result.status == "available"
    assert created == ["long-first", "short-second"] and len(cleaned) == 2


def test_birth_probe_accepts_non_english_day_month_tokens():
    def run(args, **kwargs): return SimpleNamespace(returncode=0, stdout="lun. oct. 5 12:00:00 2026", stderr="")
    assert cap.command_probe("birth", run=run, which=lambda _: "synthetic-ps", child_factory=child_stub).status == "available"


def test_parallel_workers_return_capability_receipts(pytester, monkeypatch):
    result = nested(pytester, monkeypatch, "available", workers=2)
    result.assert_outcomes(passed=1)
    assert "synthetic: available; skipped=0" in result.stdout.str()
    assert "[gw" in result.stdout.str()


def test_tmux_probe_retries_permission_denied_directory_after_cleanup():
    created = []; cleaned = []; calls = []
    def temporary(**kwargs):
        name = "/synthetic/" + kwargs["dir"]
        created.append(name)
        return SimpleNamespace(name=name, cleanup=lambda: cleaned.append(name))
    def run(args, **kwargs):
        calls.append((args[4], args[5]))
        first = args[4] == "/synthetic/first/s"
        command = args[5]
        if first:
            return SimpleNamespace(returncode=1, stdout="", stderr="permission denied" if command == "new-session" else "no server running")
        if command == "has-session" and calls[-2][1] == "kill-server":
            return SimpleNamespace(returncode=1, stdout="", stderr="no server running")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    result = cap.tmux_probe(run=run, which=lambda _: "synthetic-tmux", temporary=temporary, candidates=["first", "second"])
    assert result.status == "available"
    assert cleaned == created == ["/synthetic/first", "/synthetic/second"]
    assert [c for target,c in calls if target == "/synthetic/first/s"] == ["new-session", "kill-server", "has-session"]


def test_dead_worker_without_output_records_missing_receipt_and_fails():
    config = SimpleNamespace(_th_missing_worker_receipts=set(), _th_worker_receipts={})
    cap.pytest_testnodedown(SimpleNamespace(gateway=SimpleNamespace(id="gw-dead"), config=config), RuntimeError("synthetic timeout"))
    assert config._th_missing_worker_receipts == {"gw-dead"}
    session = SimpleNamespace(config=config, exitstatus=0)
    cap.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 1
