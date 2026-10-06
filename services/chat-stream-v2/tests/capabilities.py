"""TH-H2 test-only capability gates. Broken dependencies are never skips."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import pytest


@dataclass(frozen=True)
class ProbeResult:
    status: str
    reason: str
    value: str | None = None

    def __post_init__(self):
        if self.status not in {"available", "absent", "error"}:
            raise ValueError("invalid capability result")


@contextmanager
def owned_child(*, opened_file: Path | None = None, popen=subprocess.Popen):
    """A bounded isolated interpreter and only its owned temporary files."""
    with tempfile.TemporaryDirectory(prefix="th2-probe-") as folder:
        ready = Path(folder) / "ready"
        script = "import pathlib,sys,time; f=open(sys.argv[2],'a') if sys.argv[2] else None; pathlib.Path(sys.argv[1]).write_text('ready'); time.sleep(30)"
        child = popen([sys.executable, "-I", "-c", script, str(ready), str(opened_file or ""), "TH_H2_OWNED_ARGV_SENTINEL"],
                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 2
            while not ready.exists():
                if child.poll() is not None:
                    raise RuntimeError("owned probe child exited before readiness")
                if time.monotonic() >= deadline:
                    raise TimeoutError("owned probe child readiness timeout")
                time.sleep(.01)
            yield child
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=2)


def command_probe(kind, *, run=subprocess.run, which=shutil.which, child_factory=owned_child, isfile=os.path.isfile, access=os.access):
    binary = "lsof" if kind == "lsof" else "ps"
    executable = which(binary)
    if executable is None and binary == "lsof":
        executable = next((path for path in ("/usr/sbin/lsof", "/sbin/lsof") if isfile(path) and access(path, os.X_OK)), None)
    if executable is None:
        return ProbeResult("absent", f"{binary} executable unavailable")
    with tempfile.TemporaryDirectory(prefix="th2-descriptor-") as folder:
        target = Path(folder) / "synthetic.txt"
        with child_factory(opened_file=target if kind == "lsof" else None) as child:
            if kind == "lsof":
                args = [executable, "-a", "-p", str(child.pid), "-F", "pfan", "--", str(target)]
            elif kind == "birth":
                args = [executable, "-p", str(child.pid), "-o", "lstart="]
            else:
                args = [executable, "-ww", "-p", str(child.pid), "-o", "args="]
            try:
                reply = run(args, capture_output=True, text=True, timeout=2)
            except PermissionError:
                # A sandbox that forbids exec of ps/lsof is the canonical unavailable capability.
                return ProbeResult("absent", f"{binary} cannot be executed here (permission denied)")
            if child.poll() is not None:
                raise RuntimeError("owned child exited during capability probe")
            if reply.returncode != 0:
                if reply.returncode == 1 and (not reply.stderr.strip() or any(text in reply.stderr.lower() for text in ("permission denied", "operation not permitted"))):
                    return ProbeResult("absent", f"{binary} cannot inspect the owned child")
                raise RuntimeError(f"{binary} probe returned unexpected exit {reply.returncode}")
            if kind == "lsof":
                # lsof prints the resolved path; macOS temp dirs sit behind /var -> /private/var.
                names = {line[1:] for line in reply.stdout.splitlines() if line.startswith("n")}
                found = bool(names & {str(target), os.path.realpath(target)}) and any(line.startswith("a") and line[1:] in {"w", "u"} for line in reply.stdout.splitlines())
                if names and not names & {str(target), os.path.realpath(target)}:
                    raise RuntimeError("lsof reported a different path for the owned descriptor")
            elif kind == "birth":
                import re
                found = bool(re.fullmatch(r"\S+\s+\S+\s+\d{1,2}\s+\d\d:\d\d:\d\d\s+\d{4}", reply.stdout.strip()))
            else:
                found = "TH_H2_OWNED_ARGV_SENTINEL" in reply.stdout
            if not found and reply.stdout.strip():
                if kind == "birth" or (kind == "argv" and not any(word in reply.stdout.lower() for word in ("python", "<restricted>", "<defunct>"))) or (kind == "lsof" and any(line and line[0] not in "pfan" for line in reply.stdout.splitlines())):
                    raise RuntimeError("malformed capability command response")
            return ProbeResult("available" if found else "absent", "owned child observation verified" if found else f"{binary} owned observation unavailable")


def identity_probe(*, child_factory=owned_child):
    if not sys.platform.startswith("linux"):
        return command_probe("birth", child_factory=child_factory)
    with child_factory() as child:
        try:
            values = [Path(f"/proc/{child.pid}/stat").read_text().rsplit(")", 1)[1].split()[19] for _ in range(2)]
        except (PermissionError, FileNotFoundError):
            if child.poll() is not None:
                raise RuntimeError("owned identity child exited")
            return ProbeResult("absent", "owned process start identity unavailable")
        if child.poll() is not None or not all(value.isdecimal() for value in values) or values[0] != values[1]:
            raise RuntimeError("malformed or unstable owned process identity")
        return ProbeResult("available", "owned process start identity readable")


def socket_directory_probe(*, candidates=None, socket_factory=socket.socket, temporary=tempfile.TemporaryDirectory):
    if not hasattr(socket, "AF_UNIX"):
        return ProbeResult("absent", "AF_UNIX unavailable")
    expected = {errno.EACCES, errno.EPERM, errno.EROFS, errno.ENAMETOOLONG, errno.ENOENT, errno.ENOSYS, errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT}
    for directory in dict.fromkeys(candidates or [tempfile.gettempdir(), "/tmp"]):
        try:
            manager = temporary(prefix="th2-", dir=directory)
        except OSError as exc:
            if exc.errno not in expected:
                raise
            continue
        try:
            target = str(Path(manager.name) / "s")
            if len(os.fsencode(target)) >= 100:
                continue
            try:
                sock = socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)
            except OSError as exc:
                if exc.errno not in expected: raise
                continue
            try:
                try:
                    sock.bind(target)
                except OSError as exc:
                    if exc.errno not in expected: raise
                    continue
            finally:
                # A close failure is an error, even if bind was unavailable.
                sock.close()
            return ProbeResult("available", "owned short-path Unix socket bound and cleaned", str(directory))
        finally:
            # Cleanup exceptions are unexpected errors, never capability absence.
            manager.cleanup()
    return ProbeResult("absent", "no writable short-path Unix socket directory")


def tmux_probe(*, run=subprocess.run, which=shutil.which, temporary=tempfile.TemporaryDirectory, candidates=None):
    executable = which("tmux")
    if executable is None:
        return ProbeResult("absent", "tmux executable unavailable")
    for parent in dict.fromkeys(candidates or [tempfile.gettempdir(), "/tmp"]):
        try:
            manager = temporary(prefix="th2-tmux-", dir=parent)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOENT, errno.ENAMETOOLONG}: raise
            continue
        try:
            folder = manager.name
            target = str(Path(folder) / "s")
            if len(os.fsencode(target)) >= 100: continue
            result = _tmux_in_directory(executable, folder, target, run)
            if result.status != "absent": return result
        finally:
            manager.cleanup()
    return ProbeResult("absent", "no writable short-path tmux socket directory")


def _tmux_in_directory(executable, folder, target, run):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": folder, "LANG": "C", "LC_ALL": "C", "TERM": "dumb"}
    started = False
    attempted = False
    try:
        attempted = True
        reply = run([executable, "-f", os.devnull, "-S", target, "new-session", "-d", "-s", "th2-probe", "sleep", "5"], env=env, capture_output=True, text=True, timeout=3)
        if reply.returncode:
            if any(reason in reply.stderr.lower() for reason in ("operation not permitted", "permission denied", "function not implemented")):
                return ProbeResult("absent", "owned tmux session unavailable in this host")
            raise RuntimeError(f"tmux creation returned unexpected exit {reply.returncode}")
        started = True
        checked = run([executable, "-f", os.devnull, "-S", target, "has-session", "-t", "=th2-probe"], env=env, capture_output=True, text=True, timeout=2)
        if checked.returncode:
            raise RuntimeError("created tmux session could not be verified")
        return ProbeResult("available", "owned tmux socket/session verified")
    finally:
        # Always address the exact owned socket, never the default server.
        if attempted:
            cleaned = run([executable, "-f", os.devnull, "-S", target, "kill-server"], env=env, capture_output=True, text=True, timeout=3)
            absent_messages = ("no server running", "no such file", "connection refused")
            if cleaned.returncode and not any(text in cleaned.stderr.lower() for text in absent_messages):
                raise RuntimeError("owned tmux cleanup failed")
            remaining = run([executable, "-f", os.devnull, "-S", target, "has-session", "-t", "=th2-probe"], env=env, capture_output=True, text=True, timeout=2)
            if remaining.returncode != 1 or not any(text in remaining.stderr.lower() for text in absent_messages):
                raise RuntimeError("owned tmux cleanup could not be verified")


def isolated_dependencies(*, python=None, run=subprocess.run):
    python = python or sys.executable
    script = "from websockets.asyncio.client import connect; from cryptography.hazmat.primitives import hashes, serialization; from cryptography.hazmat.primitives.asymmetric import ec"
    reply = run([python, "-I", "-c", script], capture_output=True, text=True, timeout=5)
    if reply.returncode:
        return ProbeResult("error", "required isolated-interpreter dependencies are broken; create a venv and run its python -m pip install -r services/chat-stream-v2/requirements.txt, then run pytest with that same interpreter (the ceremony keeps -I)")
    return ProbeResult("available", "exact isolated interpreter imports websockets and cryptography")


class Registry:
    def __init__(self, probes=None):
        self.probes = probes if probes is not None else {
            "ps argv readable": lambda: command_probe("argv"),
            "ps birth readable": lambda: command_probe("birth"),
            "process start identity readable": identity_probe,
            "lsof descriptor readable": lambda: command_probe("lsof"),
            "tmux usable": tmux_probe,
            "short-path unix-socket directory writable": socket_directory_probe,
            "environment: isolated Python dependencies": isolated_dependencies,
        }
        self.results = {}
        self.skipped = {}

    def check(self, name):
        if name not in self.results:
            try:
                if name not in self.probes:
                    raise ValueError("unknown capability name")
                value = self.probes[name]()
                if not isinstance(value, ProbeResult):
                    raise TypeError("probe did not return ProbeResult")
                self.results[name] = value
            except Exception as exc:
                self.results[name] = ProbeResult("error", f"unexpected probe error: {type(exc).__name__}")
        return self.results[name]


def managed_key(item):
    return f"{Path(str(item.fspath)).name}::{getattr(item, 'originalname', None) or item.name.split('[')[0]}"


def pytest_addoption(parser):
    parser.addoption("--strict-capabilities", action="store_true", default=False, help="Fail if any required TH-H2 host capability is absent")


def pytest_configure(config):
    strict_env = os.environ.get("PENTACLE_TEST_STRICT_CAPABILITIES", "0")
    if strict_env not in {"0", "1"}:
        raise pytest.UsageError("PENTACLE_TEST_STRICT_CAPABILITIES must be 0 or 1")
    config._th_strict_capabilities = config.getoption("--strict-capabilities") or strict_env == "1"
    config._th_capabilities = Registry()
    config._th_authorized_skips = set()
    config._th_worker_receipts = {}
    config._th_missing_worker_receipts = set()
    config.addinivalue_line("markers", "requires(*names): named, probed TH-H2 host capabilities")
    config.addinivalue_line("markers", "requires_environment: declared dependencies must work in the exact isolated interpreter")
    manifest = Path(__file__).parent / "fixtures" / "h2_capabilities" / "changed_tests.json"
    config._th_managed_nodes = set(json.loads(manifest.read_text())["node_ids"])


def require(config, nodeid, name):
    result = config._th_capabilities.check(name)
    if result.status == "available":
        return result
    if result.status == "error" or name.startswith("environment:"):
        kind = "required environment broken" if name.startswith("environment:") else "capability probe error"
        pytest.fail(f"{kind}: {name}: {result.reason}", pytrace=False)
    if config._th_strict_capabilities:
        pytest.fail(f"requires: {name}: {result.reason} (strict full-capability mode)", pytrace=False)
    config._th_authorized_skips.add(nodeid)
    config._th_capabilities.skipped.setdefault(name, set()).add(nodeid)
    pytest.skip(f"requires: {name}: {result.reason}")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    if managed_key(item) in item.config._th_managed_nodes:
        if not (item.config.pluginmanager.hasplugin("timeout") or item.config.pluginmanager.hasplugin("pytest_timeout")):
            pytest.fail("required environment broken: install the declared pytest-timeout dependency", pytrace=False)
    if item.get_closest_marker("requires_environment"):
        require(item.config, item.nodeid, "environment: isolated Python dependencies")
    for marker in item.iter_markers("requires"):
        for name in marker.args:
            require(item.config, item.nodeid, name)


@pytest.fixture
def requires(request):
    return lambda name: require(request.config, request.node.nodeid, name)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.skipped and managed_key(item) in item.config._th_managed_nodes and item.nodeid not in item.config._th_authorized_skips:
        report.outcome = "failed"
        report.longrepr = "TH-H2 changed-test skip bypassed requires(...): " + item.nodeid


def pytest_sessionfinish(session, exitstatus):
    config = session.config
    if hasattr(config, "workeroutput"):
        registry = config._th_capabilities
        config.workeroutput["th_capability_receipt"] = {
            "results": {name: {"status": result.status, "reason": result.reason} for name, result in registry.results.items()},
            "skipped": {name: sorted(nodes) for name, nodes in registry.skipped.items()},
        }
    elif config._th_missing_worker_receipts and session.exitstatus == 0:
        session.exitstatus = 1


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error):
    worker = node.gateway.id
    receipt = getattr(node, "workeroutput", {}).get("th_capability_receipt")
    if receipt is None:
        node.config._th_missing_worker_receipts.add(worker)
    else:
        node.config._th_worker_receipts[worker] = receipt


def pytest_terminal_summary(terminalreporter):
    config = terminalreporter.config
    registry = config._th_capabilities
    if not registry.results and not config._th_worker_receipts and not config._th_missing_worker_receipts:
        return
    terminalreporter.write_sep("-", "TH-H2 capability/environment results")
    groups = [("", {"results": {name: {"status": result.status, "reason": result.reason} for name, result in registry.results.items()}, "skipped": registry.skipped})]
    groups.extend((f"[{worker}] ", receipt) for worker, receipt in sorted(config._th_worker_receipts.items()))
    for prefix, receipt in groups:
        for name, result in sorted(receipt["results"].items()):
            nodes = receipt["skipped"].get(name, ())
            terminalreporter.write_line(f"{prefix}{name}: {result['status']}; skipped={len(nodes)}; {result['reason']}")
            for node in sorted(nodes): terminalreporter.write_line("  " + node)
    for worker in sorted(config._th_missing_worker_receipts):
        terminalreporter.write_line(f"[{worker}] capability receipt unavailable: error")
