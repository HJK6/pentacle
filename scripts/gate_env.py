#!/usr/bin/env python3
"""Supported gate environment: prepare an owned toolchain, preflight it from the
child's own execution context, then run one closed-mapping target.

    python3 scripts/gate_env.py prepare --profile pentacle --workspace OWNED_ROOT --evidence-dir E ...
    python3 scripts/gate_env.py run --profile pentacle --workspace OWNED_ROOT --evidence-dir E \
        --target v2-unit --spec SPEC --series ID --purpose acceptance --attempt-manifest PATH

Profiles and targets come only from scripts/gate-env.json (closed mapping, no
shell strings). The child environment is built from an allow-list; nothing is
inherited. Every invocation writes a `started` receipt before any other work, and
setup failures, cancellation and crashes stay visible (scripts/lib/gate_attempts.py).
Stdlib only; runs on any python3 >= 3.9, the prepared venv carries 3.13.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import gate_attempts as ga  # noqa: E402

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
CONFIG_PATH = HERE / "gate-env.json"
EXIT = {"pass": 0, "product_failure": 1, "setup_failure": 3, "cancelled": 130}
SETUP_FAILURE_CODES = ("config", "identity_args", "toolchain", "locale", "layout", "dependencies", "capability", "preflight", "cache", "selection")
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_PIN = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*==\s*([A-Za-z0-9_.!+-]+)\s*$")
_BARE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*(?:[<>=!~]=?.*)?$")


class SetupFailure(Exception):
    """One or more independent setup faults, reported together."""

    def __init__(self, failures: List[Dict[str, str]]) -> None:
        super().__init__("; ".join(f["code"] + ": " + f["detail"] for f in failures))
        self.failures = failures


def fail(code: str, detail: str, remediation: str = "") -> Dict[str, str]:
    return {"code": code, "detail": detail, "remediation": remediation}


# ----------------------------------------------------------------- config

def load_config(path: Path = CONFIG_PATH) -> Dict[str, Any]:
    """Fail closed on anything malformed."""
    try:
        config = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise SetupFailure([fail("config", "cannot read gate-env.json (%s)" % type(exc).__name__)])
    problems = []
    if config.get("schema") != "pentacle.gate-env.v1":
        problems.append("schema")
    for key in ("toolchain", "profiles", "capability_sets", "search_path", "locales", "system_path"):
        if not isinstance(config.get(key), (dict, list)):
            problems.append(key)
    deadline = config.get("deadline")
    if not isinstance(deadline, dict) or any(not isinstance(deadline.get(k), int) or isinstance(deadline.get(k), bool) or deadline[k] < 0 for k in ("preflight_s", "reserve_s", "slack_s")):
        problems.append("deadline")
    for key in ("reserve_s", "max_tmpdir_bytes", "min_free_mb"):
        if not isinstance(config.get(key), int) or isinstance(config.get(key), bool) or config[key] < 0:
            problems.append(key)
    if not problems:
        for pname, profile in config["profiles"].items():
            if not _NAME.match(pname) or not isinstance(profile.get("targets"), dict) or not profile["targets"]:
                problems.append("profile " + pname)
                continue
            for tname, target in profile["targets"].items():
                if not _NAME.match(tname) or target.get("kind") not in ("run_gate", "pytest_files", "npm_test", "web_gate"):
                    problems.append("target " + tname)
                    continue
                if not isinstance(target.get("timeout_s"), int) or target["timeout_s"] <= 0 or target["timeout_s"] > target.get("max_timeout_s", 0):
                    problems.append("timeout of " + tname)
                if target.get("capabilities") and target["capabilities"] not in config["capability_sets"]:
                    problems.append("capability set of " + tname)
                if target.get("npm_build") and (not isinstance(target["npm_build"], list) or any(b not in ("prestart", "build:web") for b in target["npm_build"])):
                    problems.append("npm_build of " + tname)
                limits = target.get("tier_timeouts")
                if limits is not None and (target.get("gate") != "merge" or not isinstance(limits, dict) or set(limits) != {"unit", "smoke"} or any(type(v) is not int or v <= 0 for v in limits.values()) or sum(limits.values()) != target["timeout_s"]):
                    problems.append("tier_timeouts of " + tname)

    if problems:
        raise SetupFailure([fail("config", "malformed gate-env.json: " + ", ".join(problems), "restore the checked-in scripts/gate-env.json")])
    return config


def file_digest(path: Path) -> str:
    return ga.sha256_file(path)


# ---------------------------------------------------------- toolchain

def _version(run, exe: str, flag: str = "--version", env: Optional[Dict[str, str]] = None) -> Optional[str]:
    try:
        out = run([exe, flag], capture_output=True, text=True, timeout=15, env=env if env is not None else {"PATH": "%s:/usr/bin:/bin" % os.path.dirname(exe)})
    except (OSError, subprocess.SubprocessError):
        return None
    text = (out.stdout + out.stderr).strip()
    match = re.search(r"(\d+(?:\.\d+)+)", text)
    return match.group(1) if out.returncode == 0 and match else None


def candidate_dirs(config: Dict[str, Any], ambient_path: str, system: str) -> List[str]:
    dirs = [d for d in ambient_path.split(os.pathsep) if d.startswith("/")]
    dirs += config["search_path"].get(system, [])
    dirs += config["system_path"]
    return list(dict.fromkeys(dirs))


def find_exe(name: str, dirs: List[str], *, want_major: Optional[str] = None, run=subprocess.run, flag: str = "--version") -> Tuple[Optional[Dict[str, str]], List[str]]:
    """First real executable named `name` (optionally a version prefix). Returns
    (resolved, rejected-descriptions); resolved symlinks are followed."""
    rejected = []
    for folder in dirs:
        path = Path(folder) / name
        if not (path.is_file() and os.access(path, os.X_OK)):
            continue
        real = os.path.realpath(path)
        version = _version(run, str(path), flag)
        if version is None:
            rejected.append("%s does not report a version" % path)
            continue
        if want_major and not (version == want_major or version.startswith(want_major + ".")):
            rejected.append("%s is %s" % (path, version))
            continue
        return {"path": str(path), "realpath": real, "version": version, "sha256": file_digest(Path(real))}, rejected
    return None, rejected


def resolve_toolchain(config: Dict[str, Any], needs: List[str], *, ambient_path: Optional[str] = None,
                      system: Optional[str] = None, run=subprocess.run) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    system = "darwin" if (system or sys.platform) == "darwin" else "linux"
    dirs = candidate_dirs(config, os.environ.get("PATH", "") if ambient_path is None else ambient_path, system)
    found: Dict[str, Any] = {}
    failures = []
    wanted = {"python": ("python3.13" if config["toolchain"]["python"] == "3.13" else "python3", config["toolchain"]["python"]),
              "node": ("node", config["toolchain"]["node"])}
    for key in ("python", "node"):
        if key in needs:
            exe, major = wanted[key]
            hit, rejected = find_exe(exe, dirs, want_major=major, run=run)
            if hit is None and key == "python":
                hit, more = find_exe("python3", dirs, want_major=major, run=run)
                rejected += more
            if hit is None:
                failures.append(fail("toolchain", "no %s %s executable found (%s)" % (key, major, "; ".join(rejected) or "none on search path"),
                                     "install %s %s or add its directory to PATH" % (key, major)))
            else:
                found[key] = hit
    if "npm" in needs:
        folders = ([str(Path(found["node"]["path"]).parent)] if "node" in found else []) + dirs
        hit, rejected = find_exe("npm", list(dict.fromkeys(folders)), run=run)
        if hit is None:
            failures.append(fail("toolchain", "no working npm executable found (%s)" % ("; ".join(rejected) or "none on search path"),
                                 "install npm alongside node %s; paths are never guessed" % config["toolchain"]["node"]))
        else:
            found["npm"] = hit
    if "tmux" in needs:
        hit, rejected = find_exe("tmux", dirs, run=run, flag="-V")
        if hit is None:
            failures.append(fail("toolchain", "no working tmux executable found", "install tmux"))
        else:
            found["tmux"] = hit
    return found, failures


def pick_locale(config: Dict[str, Any], *, run=subprocess.run) -> Tuple[Optional[str], Optional[Dict[str, str]]]:
    try:
        listing = run(["/usr/bin/locale", "-a"], capture_output=True, text=True, timeout=10, env={"PATH": "/usr/bin:/bin"}).stdout.split()
    except (OSError, subprocess.SubprocessError):
        listing = []
    norm = lambda s: s.lower().replace("utf-8", "utf8")  # noqa: E731
    available = {norm(l) for l in listing}
    for candidate in config["locales"]:
        if norm(candidate) in available:
            return candidate, None
    return None, fail("locale", "none of %s is an installed UTF-8 locale" % config["locales"], "install a UTF-8 locale; the ambient LANG is never trusted")


# -------------------------------------------------------------- layout

def scratch_paths(workspace: Path, tmp_root: Path, attempt_id: str) -> Tuple[Path, Path, Path]:
    """(HOME, TMPDIR, basetemp). HOME sits in the workspace; TMPDIR is short for socket paths and
    holds the pytest basetemp, so harness isolation guards (roots under the OS temp base) accept it."""
    tmp = tmp_root / attempt_id[:8]
    return workspace / "runs" / attempt_id[:8] / "home", tmp, tmp / "b"

def _inside(child: Path, parent: Path) -> bool:
    child, parent = Path(os.path.realpath(child)), Path(os.path.realpath(parent))
    return child == parent or parent in child.parents


def check_layout(checkout: Path, workspace: Path, evidence: Path, home: Path, tmp: Path, basetemp: Path, max_tmp: int) -> List[Dict[str, str]]:
    failures = []
    for label, path in (("workspace", workspace), ("evidence", evidence)):
        if _inside(path, checkout):
            failures.append(fail("layout", "%s is inside the source checkout (it would dirty the gate's clean-tree check)" % label, "use a directory outside the checkout"))
    for a_label, a, b_label, b in (("evidence", evidence, "home", home), ("evidence", evidence, "tmp", tmp), ("evidence", evidence, "basetemp", basetemp),
                                   ("home", home, "basetemp", basetemp), ("home", home, "tmp", tmp)):
        if _inside(a, b) or _inside(b, a):
            failures.append(fail("layout", "%s and %s are nested; pytest may delete basetemp and HOME must never live under it" % (a_label, b_label), "keep them as siblings"))
    if not _inside(basetemp, tmp):
        failures.append(fail("layout", "basetemp is not under TMPDIR; the harness isolation guards only accept roots under the OS temp base", "keep basetemp inside the owned TMPDIR"))
    if len(str(tmp).encode()) > max_tmp:
        failures.append(fail("layout", "TMPDIR %r is %d bytes (>%d): unix and tmux socket paths would overflow" % (str(tmp), len(str(tmp).encode()), max_tmp), "pass a shorter --tmp-root"))
    return failures


# --------------------------------------------------------- environment

def build_env(config: Dict[str, Any], profile: Dict[str, Any], target: Dict[str, Any], *, checkout: Path, toolchain: Dict[str, Any],
              venv: Optional[Path], home: Path, tmp: Path, locale_name: str, evidence: Path, attempt_id: str,
              pass_env: Dict[str, str], ambient: Dict[str, str], tier_timeout_s: Optional[int] = None, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Closed allow-list: nothing from `ambient` except individually declared names."""
    dirs = []
    if venv is not None:
        dirs.append(str(venv / "bin"))
    for key in ("node", "npm", "tmux"):
        if key in toolchain:
            dirs.append(str(Path(toolchain[key]["path"]).parent))
    dirs += config["system_path"]
    env = {
        "PATH": os.pathsep.join(dict.fromkeys(dirs)),
        "HOME": str(home), "TMPDIR": str(tmp), "TMP": str(tmp), "TEMP": str(tmp),
        "LANG": locale_name, "LC_ALL": locale_name,
        "SHELL": "/bin/sh", "TERM": "dumb", "PYTHONNOUSERSITE": "1",
        "PENTACLE_TEST_STRICT_CAPABILITIES": "1",
        "PENTACLE_GATE_ATTEMPT_ID": attempt_id,
        "PENTACLE_GATE_EVIDENCE_DIR": str(evidence),
    }
    if "python" in target.get("needs", []):
        env["V2_PYTHON_BIN"] = str(venv / "bin" / "python") if venv else ""
        env["PYTHONPATH"] = os.pathsep.join(str(checkout / p) for p in profile.get("pythonpath", []))
    for tier, limit in target.get("tier_timeouts", {}).items():
        env["V2_%s_TIMEOUT_S" % tier.upper()] = str(limit)
    if target["kind"] == "web_gate":
        env["PENTACLE_PYTHON"] = str(venv / "bin" / "python") if venv else ""
        env["PENTACLE_WEB_GATE_REPORT_ROOT"] = str(evidence / "gate" / "web")
    if target["kind"] == "npm_test":
        env["PENTACLE_TEST_FILE_TIMEOUT_MS"] = "60000"
        env["PENTACLE_TEST_RUN_TIMEOUT_MS"] = str(int(target["timeout_s"]) * 1000)
    allowed = set(target.get("pass_env", []))
    for name, value in pass_env.items():
        if not _ENV_NAME.match(name) or name not in allowed:
            raise SetupFailure([fail("selection", "environment name %r is not declared for this target" % name, "declare it under pass_env in gate-env.json")])
        env[name] = value
    if target.get("pytest_addopts"):
        env["PYTEST_ADDOPTS"] = target["pytest_addopts"]
    if extra:
        env.update(extra)
    # ambient is accepted only so tests can prove nothing leaks; it is never read here.
    del ambient
    return {k: v for k, v in env.items() if v != "" or k in ("V2_PYTHON_BIN",)}


# ------------------------------------------------ prepared environment

def parse_requirements(paths: List[Path]) -> Tuple[Dict[str, str], List[str]]:
    pins: Dict[str, str] = {}
    bare: List[str] = []
    for path in paths:
        for raw in path.read_text("utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            pin = _PIN.match(line)
            if pin:
                pins[pin.group(1).lower().replace("_", "-")] = pin.group(2)
            else:
                bare_match = _BARE.match(line)
                if bare_match:
                    bare.append(bare_match.group(1).lower().replace("_", "-"))
    return pins, bare


def prepared_key(config: Dict[str, Any], profile_name: str, profile: Dict[str, Any], toolchain: Dict[str, Any], checkout: Path, needs_npm: bool) -> str:
    inputs = {
        "profile": [profile_name, profile], "toolchain_cfg": config["toolchain"],
        "tools": {k: [v["version"], v["sha256"]] for k, v in sorted(toolchain.items()) if k in ("python", "node", "npm")},
        "requirements": {p: file_digest(checkout / p) for p in profile.get("python_requirements", [])},
        "npm_lock": file_digest(checkout / profile["package_lock"]) if needs_npm and profile.get("package_lock") else None,
        "platform": [platform.system(), platform.machine()],
    }
    return ga.digest_of(inputs)[:16]


def freeze(run, venv_python: Path, env: Dict[str, str]) -> Dict[str, str]:
    code = "import importlib.metadata as m,json;print(json.dumps(sorted((d.metadata['Name'].lower().replace('_','-'),d.version) for d in m.distributions())))"
    out = run([str(venv_python), "-I", "-c", code], capture_output=True, text=True, timeout=60, env=env)
    if out.returncode:
        raise RuntimeError("venv inspection failed")
    return dict(json.loads(out.stdout))


def ensure_python_env(run, config: Dict[str, Any], profile: Dict[str, Any], toolchain: Dict[str, Any], checkout: Path, prepared: Path,
                      env: Dict[str, str], rebuild: bool, log) -> Tuple[Optional[Path], Dict[str, Any], List[Dict[str, str]]]:
    """Create/reuse the owned venv. Reuse requires the recorded dependency identity to still match."""
    venv, ready = prepared / "venv", prepared / "python-ready.json"
    info: Dict[str, Any] = {}
    if rebuild and prepared.exists():
        shutil.rmtree(prepared)
    if ready.exists():
        try:
            recorded = json.loads(ready.read_text("utf-8"))
            actual = ga.digest_of(freeze(run, venv / "bin" / "python", env))
        except (OSError, ValueError, RuntimeError):
            return None, info, [fail("cache", "prepared Python environment is unreadable", "rerun with --rebuild (removes only %s)" % prepared)]
        if recorded.get("dependency_digest") != actual:
            return None, info, [fail("cache", "prepared Python environment no longer matches its recorded dependencies", "rerun with --rebuild (removes only %s)" % prepared)]
        info = {"reused": True, "dependency_digest": actual}
        return venv, info, []
    prepared.mkdir(parents=True, exist_ok=True)
    log("creating venv with %s" % toolchain["python"]["version"])
    steps = [[toolchain["python"]["path"], "-m", "venv", str(venv)]]
    pip = [str(venv / "bin" / "python"), "-m", "pip", "install", "--no-input", "--disable-pip-version-check"]
    for req in profile.get("python_requirements", []):
        pip += ["-r", str(checkout / req)]
    for editable in profile.get("python_editable", []):
        pip += ["-e", str(checkout / editable)]
    steps.append(pip)
    pip_env = dict(env, PIP_NO_INPUT="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
    for step in steps:
        out = run(step, capture_output=True, text=True, timeout=1500, env=pip_env, cwd=str(checkout))
        if out.returncode:
            tail = (out.stdout + out.stderr).strip().splitlines()[-5:]
            shutil.rmtree(prepared, ignore_errors=True)
            return None, info, [fail("dependencies", "%s failed: %s" % (" ".join(step[:4]), " | ".join(tail)[:400]), "fix the dependency declaration or network, then rerun")]
    frozen = freeze(run, venv / "bin" / "python", env)
    digest = ga.digest_of(frozen)
    atomic = json.dumps({"dependency_digest": digest, "packages": len(frozen)}, sort_keys=True).encode()
    ga.atomic_write(ready, atomic)
    return venv, {"reused": False, "dependency_digest": digest}, []


def ensure_npm_env(run, checkout: Path, prepared: Path, env: Dict[str, str], profile: Dict[str, Any], rebuild: bool, log) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    lock = checkout / profile["package_lock"]
    marker, installed = prepared / "npm-ready.json", checkout / "node_modules" / ".package-lock.json"
    want = file_digest(lock)
    if marker.exists() and installed.exists() and not rebuild:
        try:
            recorded = json.loads(marker.read_text("utf-8"))
        except (OSError, ValueError):
            recorded = {}
        if recorded.get("lock") == want and recorded.get("installed") == file_digest(installed):
            return {"reused": True, "lock": want}, []
        return {}, [fail("cache", "node_modules no longer matches the lockfile it was prepared from", "rerun with --rebuild")]
    log("npm ci")
    out = run(["npm", "ci"], capture_output=True, text=True, timeout=1500, env=env, cwd=str(checkout))
    if out.returncode:
        return {}, [fail("dependencies", "npm ci failed: " + " | ".join((out.stdout + out.stderr).strip().splitlines()[-5:])[:400], "inspect the npm log; no manual node_modules edits")]
    prepared.mkdir(parents=True, exist_ok=True)
    ga.atomic_write(marker, json.dumps({"lock": want, "installed": file_digest(installed) if installed.exists() else None}, sort_keys=True).encode())
    return {"reused": False, "lock": want}, []


# ------------------------------------------------------------ preflight

CAPABILITY_SCRIPT = (
    "import json,sys; sys.path.insert(0, sys.argv[1]); import capabilities as c; r=c.Registry(); "
    "print(json.dumps({n:[r.check(n).status, r.check(n).reason] for n in json.loads(sys.argv[2])}))"
)


def check_dependencies(run, venv_python: Path, env: Dict[str, str], requirements: List[Path]) -> List[Dict[str, str]]:
    pins, bare = parse_requirements(requirements)
    try:
        installed = freeze(run, venv_python, env)
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError):
        return [fail("dependencies", "cannot inspect the prepared venv from the child environment", "rerun with --rebuild")]
    failures = []
    for name, version in pins.items():
        if installed.get(name) != version:
            failures.append(fail("dependencies", "%s declared ==%s but prepared env has %s" % (name, version, installed.get(name, "nothing")), "rerun with --rebuild"))
    for name in bare:
        if name not in installed:
            failures.append(fail("dependencies", "%s is declared but not installed" % name, "rerun with --rebuild"))
    return failures


def check_capabilities(run, venv_python: Path, env: Dict[str, str], tests_dir: Path, names: List[str]) -> List[Dict[str, str]]:
    try:
        out = run([str(venv_python), "-c", CAPABILITY_SCRIPT, str(tests_dir), json.dumps(names)], capture_output=True, text=True, timeout=120, env=env, cwd=str(tests_dir.parent))
        result = json.loads(out.stdout) if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        result = None
    if result is None:
        return [fail("capability", "capability probes could not run from the child environment", "check the prepared venv imports pytest")]
    return [fail("capability", "%s is %s: %s" % (name, status, reason), "strict gates need every required capability; fix the host, never opt out")
            for name, (status, reason) in result.items() if status != "available"]


def check_process_listing(run, env: Dict[str, str]) -> List[Dict[str, str]]:
    """The node runner on darwin shells out to `ps -A -o pid=,pgid=,stat=` by name."""
    if sys.platform.startswith("linux"):
        return [] if os.access("/proc/self/stat", os.R_OK) else [fail("capability", "/proc is not readable", "run on a host with a readable /proc")]
    try:
        out = run(["ps", "-A", "-o", "pid=,pgid=,stat="], capture_output=True, text=True, timeout=10, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return [fail("capability", "ps cannot be executed from the child environment (%s)" % type(exc).__name__, "the node runner needs ps on the owned PATH")]
    rows = [l.split() for l in out.stdout.splitlines() if l.strip()]
    if out.returncode or not any(len(r) == 3 and r[0] == str(os.getpid()) or (len(r) == 3 and r[0].isdigit()) for r in rows):
        return [fail("capability", "ps returned no process rows (rc=%s)" % out.returncode, "run where ps can list processes")]
    return []


def check_node_npm(run, toolchain: Dict[str, Any], env: Dict[str, str]) -> List[Dict[str, str]]:
    failures = []
    for tool in ("node", "npm"):
        if tool in toolchain and _version(run, tool, "--version", env=env) is None:
            failures.append(fail("toolchain", "%s does not run from the child PATH" % tool, "fix the owned PATH resolution"))
    return failures


def check_scratch(evidence: Path, home: Path, tmp: Path, min_free_mb: int, timeout_s: int, reserve_s: int) -> List[Dict[str, str]]:
    failures = []
    for label, path in (("evidence", evidence), ("home", home), ("tmp", tmp)):
        try:
            probe = path / (".w-%d" % os.getpid())
            probe.write_text("x")
            probe.unlink()
        except OSError as exc:
            failures.append(fail("layout", "%s directory %s is not writable (%s)" % (label, path, type(exc).__name__), "pick a writable owned location"))
    free_mb = shutil.disk_usage(str(evidence)).free // (1024 * 1024)
    if free_mb < min_free_mb:
        failures.append(fail("layout", "only %d MB free (< %d)" % (free_mb, min_free_mb), "free disk before launch"))
    if timeout_s <= 0 or reserve_s < 0:
        failures.append(fail("layout", "invalid deadline", "use a positive --timeout-seconds"))
    return failures


def run_preflight_script(run, script: Path, env: Dict[str, str]) -> List[Dict[str, str]]:
    try:
        out = run(["/bin/bash", str(script)], capture_output=True, text=True, timeout=60, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return [fail("preflight", "%s could not run (%s)" % (script.name, type(exc).__name__))]
    if out.returncode:
        return [fail("preflight", "%s: %s" % (script.name, (out.stderr or out.stdout).strip().splitlines()[0] if (out.stderr or out.stdout).strip() else "rc=%d" % out.returncode),
                     "see the script's remediation text")]
    return []


# ------------------------------------------------------------ selection

def validate_selection(target: Dict[str, Any], select: List[str], checkout: Path) -> List[str]:
    spec = target.get("select")
    if not select:
        if spec and "default_patterns" not in target:
            # A bare pytest target would collect its whole rootdir; a focused target never defaults to the full suite.
            raise SetupFailure([fail("selection", "this target requires at least one --select", "name the test files (and optional ::nodes) to run; use a v2-unit/v2-merge target for the full suite")])
        return list(target.get("default_patterns", []))
    if not spec:
        raise SetupFailure([fail("selection", "this target takes no --select", "")])
    pattern = re.compile(spec["pattern"])
    out = []
    root = checkout / spec.get("root", "")
    for item in select:
        if item.startswith("-") or ".." in Path(item).parts or item.startswith("/") or not pattern.match(item):
            raise SetupFailure([fail("selection", "selector %r does not match the target's declared pattern" % item, "")])
        file_part = item.split("::", 1)[0]
        if "*" not in file_part and not (root / file_part).is_file():
            raise SetupFailure([fail("selection", "selector %r names no existing file" % item, "")])
        out.append(item)
    return out


# --------------------------------------------------------- classification

def junit_counts(path: Path) -> Optional[Dict[str, int]]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    suites = [root] if root.tag == "testsuite" else list(root.findall(".//testsuite"))
    return {k: sum(int(s.attrib.get(k, "0") or 0) for s in suites) for k in ("tests", "failures", "errors", "skipped")}


def classify_run_gate(evidence_json: Path, returncode: int, timed_out: bool, cancelled: bool) -> Tuple[str, Optional[Dict[str, str]], Dict[str, Any]]:
    """Fold run_gate evidence. Reads the runner's own `primary_outcome` (falling back to `reason` for
    older runners) and keeps its reporting/cleanup facts as secondaries instead of erasing them."""
    if cancelled:
        return "cancelled", {"stage": "subject", "code": "cancelled"}, {}
    if timed_out:
        return "timeout_unclassified", {"stage": "subject", "code": "wall_deadline"}, {}
    try:
        data = json.loads(evidence_json.read_text("utf-8"))
    except (OSError, ValueError):
        return "harness_failure", {"stage": "subject", "code": "no_gate_evidence"}, {}
    tiers = data.get("tiers", [])
    secondary: Dict[str, Any] = {}
    reporting = sorted({r for t in tiers for r in (t.get("secondary_outcomes") or [])})
    if reporting:
        secondary["reporting"] = reporting
    if data.get("finished") is False:
        return "incomplete", {"stage": "subject", "code": "evidence_not_finished"}, secondary
    first = next((t for t in tiers if not t.get("passed")), None)
    if first is None and returncode == 0:
        return "pass", None, dict(secondary, tiers=[t.get("tier") for t in tiers])
    if first is None:
        return "harness_failure", {"stage": "subject", "code": "rc_%d_without_failed_tier" % returncode}, secondary
    reason = first.get("primary_outcome") or first.get("reason") or "unknown"
    counts = first.get("counts") or {}
    ff = {"stage": first.get("tier", "subject"), "code": reason}
    kind = (first.get("termination") or {}).get("kind")
    if kind == "external_signal" or reason == "terminated":
        return "cancelled", ff, secondary
    if reason == "deadline_envelope_exceeded":
        return "setup_failure", ff, secondary
    if reason in ("ambient_tmux", "tmux_missing", "preflight_failed", "dirty_worktree", "invalid_soak_tier") or reason.startswith("manifest_error"):
        return "setup_failure", ff, secondary
    if reason == "timeout":
        return "timeout_unclassified", ff, secondary
    if kind == "subject_signal":
        return "harness_failure", ff, secondary  # killed by a signal the runner did not send
    if reason == "test_failures":
        return ("product_failure" if counts.get("failures") else "unknown"), ff, secondary
    if reason in ("missing_junit", "invalid_junit", "no_tests_collected", "source_changed_during_gate", "soak_skipped", "evidence_incomplete", "usage_manifest_invalid", "usage_manifest_timeout"):
        return "harness_failure", ff, secondary
    return "unknown", ff, secondary


def classify_pytest(junit: Path, returncode: int, timed_out: bool, cancelled: bool) -> Tuple[str, Optional[Dict[str, str]], Dict[str, Any]]:
    if cancelled:
        return "cancelled", {"stage": "subject", "code": "cancelled"}, {}
    if timed_out:
        return "timeout_unclassified", {"stage": "subject", "code": "wall_deadline"}, {}
    counts = junit_counts(junit)
    if counts is None:
        return "harness_failure", {"stage": "subject", "code": "missing_junit"}, {}
    if counts["tests"] == 0:
        return "harness_failure", {"stage": "subject", "code": "no_tests_collected"}, {"counts": counts}
    if returncode == 0 and not counts["failures"] and not counts["errors"]:
        return "pass", None, {"counts": counts}
    if counts["failures"]:
        return "product_failure", {"stage": "subject", "code": "test_failures"}, {"counts": counts}
    return "unknown", {"stage": "subject", "code": "errors_without_assertions"}, {"counts": counts}


def classify_npm(summary: Path, returncode: int, timed_out: bool, cancelled: bool) -> Tuple[str, Optional[Dict[str, str]], Dict[str, Any]]:
    if cancelled:
        return "cancelled", {"stage": "subject", "code": "cancelled"}, {}
    if timed_out:
        return "timeout_unclassified", {"stage": "subject", "code": "wall_deadline"}, {}
    try:
        data = json.loads(summary.read_text("utf-8"))
    except (OSError, ValueError):
        return "harness_failure", {"stage": "subject", "code": "no_runner_summary"}, {}
    rows = data.get("files", [])
    secondary: Dict[str, Any] = {"files": len(rows)}
    unclean = [r["file"] for r in rows if (r.get("cleanup") or {}).get("status") not in (None, "clean", "not_started")]
    if unclean:
        secondary["cleanup"] = "failed"
    if data.get("runner_errors") or data.get("directory_cleanup_errors"):
        return "harness_failure", {"stage": "subject", "code": "runner_errors"}, secondary
    if any(r.get("status") == "timeout" for r in rows):
        return "timeout_unclassified", {"stage": "subject", "code": "file_timeout"}, secondary
    if any(r.get("status") == "fail" for r in rows):
        return ("cleanup_failure" if unclean and all((r.get("execution") or {}).get("exitCode") == 0 for r in rows if r.get("status") == "fail") else "product_failure"), \
            {"stage": "subject", "code": "test_failures"}, secondary
    if data.get("exit_code") == 0 and returncode == 0:
        return "pass", None, secondary
    return "unknown", {"stage": "subject", "code": "rc_%d" % returncode}, secondary


def classify_web(summary: Path, returncode: int, timed_out: bool) -> Tuple[str, Optional[Dict[str, str]], Dict[str, Any]]:
    if timed_out:
        return "timeout_unclassified", {"stage": "subject", "code": "wall_deadline"}, {}
    try:
        data = json.loads(summary.read_text("utf-8"))
    except (OSError, ValueError):
        return "harness_failure", {"stage": "subject", "code": "missing_web_summary"}, {}
    phases = data.get("phases", [])
    failed = next((p for p in phases if p.get("returncode") != 0), None)
    if not failed and returncode == 0 and len(phases) == 2:
        return "pass", None, {"phases": len(phases)}
    # A nonzero browser exit without assertion evidence is not a product assertion.
    return "harness_failure", {"stage": "subject", "code": "web_gate_failed"}, {"phases": phases}


# ---------------------------------------------------------------- runner

def source_identity(checkout: Path) -> Dict[str, Any]:
    env = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "GIT_OPTIONAL_LOCKS": "0"}
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(checkout), capture_output=True, text=True, timeout=15, env=env).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=str(checkout), capture_output=True, text=True, timeout=60, env=env).stdout.strip())
        return {"sha": sha or UNKNOWN, "dirty": dirty}
    except (OSError, subprocess.SubprocessError):
        return {"sha": UNKNOWN, "dirty": None}


UNKNOWN = ga.UNKNOWN


def harness_identity() -> str:
    h = hashlib.sha256()
    for p in (Path(__file__), HERE / "lib" / "gate_attempts.py", CONFIG_PATH):
        h.update(p.read_bytes())
    return h.hexdigest()


def evidence_refs(folder: Path) -> List[Dict[str, Any]]:
    refs = []
    if folder.is_dir():
        for p in sorted(folder.iterdir()):
            if p.is_file() and p.stat().st_size <= 64 * 1024 * 1024:
                refs.append({"host": socket.gethostname().split(".")[0], "path": str(p), "sha256": file_digest(p), "bytes": p.stat().st_size})
    return refs


def stop_group(proc: subprocess.Popen, grace: float = 20.0) -> None:
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        pgid = None
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if pgid is not None:
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            proc.wait(timeout=grace)
            break
        except subprocess.TimeoutExpired:
            continue


class OwnedProcesses:
    """The gate owner manifest (tools/gate_owner_manifest.py) for a subject the entrypoint launches directly.

    Test fixtures record the daemons/tmux servers they start into it (V2_GATE_OWNER_MANIFEST); `stop` reaps
    exactly those after the subject ends, is cancelled or times out, so teardown never depends on one process group."""

    ENV = "V2_GATE_OWNER_MANIFEST"

    def __init__(self, service_dir: Path, path: Path) -> None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("gate_owner_manifest_for_gate_env", service_dir / "tools" / "gate_owner_manifest.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)  # type: ignore[union-attr]
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.module.reap_manifest(path)  # a stale manifest from a dead owner is reaped, a live one raises
        self.run_id = self.module.initialize_manifest(path)

    def stop(self) -> bool:
        """True when every recorded entry of this run was reaped and the manifest is gone."""
        try:
            self.module.mark_owner_stopping(self.path)
            self.module.reap_manifest(self.path, self.run_id)
        except Exception:  # a manifest that cannot be read is an unproven teardown, not a crash
            return False
        return not self.path.exists()


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Cancelled(BaseException):
    pass


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("prepare", "run"))
    parser.add_argument("--profile", required=True)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--target")
    parser.add_argument("--select", action="append", default=[], help="run only: test files/nodes; required for pytest targets (never defaults to the full suite)")
    parser.add_argument("--pass-env", action="append", default=[], metavar="NAME")
    parser.add_argument("--spec")
    parser.add_argument("--series")
    parser.add_argument("--purpose", choices=ga.PURPOSES)
    parser.add_argument("--attempt-manifest", type=Path)
    parser.add_argument("--prior-attempt")
    parser.add_argument("--timeout-seconds", type=int)
    parser.add_argument("--tmp-root", type=Path)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--checkout", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    workspace = args.workspace.resolve()
    checkout = args.checkout.resolve()
    attempt_id = ga.new_attempt_id()
    evidence = (args.evidence_dir or workspace / "evidence" / attempt_id).resolve()
    evidence.mkdir(parents=True, exist_ok=True)

    missing_identity = [n for n, v in (("spec", args.spec), ("series", args.series), ("purpose", args.purpose)) if not v]
    identity = {
        "spec_id": args.spec or UNKNOWN, "series_id": args.series or UNKNOWN, "purpose": args.purpose or UNKNOWN,
        "prior_attempt_id": args.prior_attempt, "command": args.command, "profile": args.profile, "target": args.target,
        "host": socket.gethostname().split(".")[0],
        "owner_stream_id": os.environ.get("AGENT_ORCH_STREAM_ID") or UNKNOWN,
        "owner_generation": os.environ.get("PENTACLE_SESSION_GENERATION") or UNKNOWN,
        "history": "unknown" if missing_identity else ("continues" if args.prior_attempt else "first_observed"),
        "history_complete": False if missing_identity else None,
        "requested_at": ga.utc_now(),
        "identities": {"source": source_identity(checkout), "harness": harness_identity(), "config": None, "profile": None},
        "selected": {"select": list(args.select), "pass_env_names": list(args.pass_env)},
    }
    writer = ga.AttemptWriter(evidence, attempt_id, identity=identity)
    writer.emit("started")
    print("[gate-env] attempt=%s evidence=%s" % (attempt_id, evidence))

    state: Dict[str, Any] = {"outcome": "harness_failure", "first_failure": {"stage": "setup", "code": "crash"}, "secondary": {}, "evidence": [], "extra": {}}
    cancelled = {"hit": False}
    tmp_root = (args.tmp_root or Path("/tmp/ge-%d" % os.getuid())).resolve()
    home, tmp, basetemp = scratch_paths(workspace, tmp_root, attempt_id)
    run_dir = home.parent

    def on_signal(_n, _f):
        cancelled["hit"] = True
        raise Cancelled()

    old = {s: signal.signal(s, on_signal) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    proc = None
    owner: Optional[OwnedProcesses] = None
    try:
        if args.command == "run" and not args.target:
            raise SetupFailure([fail("identity_args", "run needs --target")])
        config = load_config(CONFIG_PATH)
        profile = config["profiles"].get(args.profile)
        if profile is None:
            raise SetupFailure([fail("config", "unknown profile %r" % args.profile, "choose one of %s" % sorted(config["profiles"]))])
        target = None
        if args.target:
            target = profile["targets"].get(args.target)
            if target is None:
                raise SetupFailure([fail("config", "unknown target %r" % args.target, "choose one of %s" % sorted(profile["targets"]))])
        if args.command == "prepare":
            # prepare never launches a subject: it provisions and preflights --target's needs (or every target's when none is named).
            if args.select:
                raise SetupFailure([fail("selection", "prepare takes no --select (it launches nothing)", "use `run` to execute a selection")])
            if target is not None:
                target = dict(target, kind="prepare")
        if target is None:  # prepare without a target: the union of every target's needs
            target = {"kind": "prepare", "needs": sorted({n for t in profile["targets"].values() for n in t.get("needs", [])}),
                      "npm_ci": any(t.get("npm_ci") for t in profile["targets"].values()), "timeout_s": 600, "max_timeout_s": 600}
        identity["identities"]["config"] = ga.digest_of(config)
        identity["identities"]["profile"] = ga.digest_of(profile)
        timeout_s = args.timeout_seconds or target["timeout_s"]
        if timeout_s > target["max_timeout_s"]:
            raise SetupFailure([fail("selection", "--timeout-seconds %d exceeds the target maximum %d" % (timeout_s, target["max_timeout_s"]))])
        selection = validate_selection(target, args.select, checkout) if target["kind"] != "prepare" else []
        pass_env = {}
        for name in args.pass_env:
            if name not in os.environ:
                raise SetupFailure([fail("selection", "pass-env %s is not set in the caller's environment" % name)])
            pass_env[name] = os.environ[name]

        failures: List[Dict[str, str]] = []
        for d in (home, basetemp, tmp, tmp_root):
            d.mkdir(parents=True, exist_ok=True)
        failures += check_layout(checkout, workspace, evidence, home, tmp, basetemp, config["max_tmpdir_bytes"])
        toolchain, tool_fail = resolve_toolchain(config, target.get("needs", []))
        failures += tool_fail
        locale_name, locale_fail = pick_locale(config)
        if locale_fail:
            failures.append(locale_fail)
        if failures:
            raise SetupFailure(failures)  # nothing below is safe without a toolchain/layout

        prepared_dir = workspace / "prepared" / prepared_key(config, args.profile, profile, toolchain, checkout, bool(target.get("npm_ci")))
        env = build_env(config, profile, target, checkout=checkout, toolchain=toolchain, venv=None, home=home, tmp=tmp, locale_name=locale_name,
                        evidence=evidence, attempt_id=attempt_id, pass_env=pass_env, ambient=dict(os.environ))
        log = lambda m: print("[gate-env] " + m, flush=True)  # noqa: E731
        venv = None
        dep_info: Dict[str, Any] = {}
        if "python" in target.get("needs", []):
            venv, dep_info, f = ensure_python_env(subprocess.run, config, profile, toolchain, checkout, prepared_dir, env, args.rebuild, log)
            failures += f
        npm_info: Dict[str, Any] = {}
        if target.get("npm_ci") and not failures:
            npm_info, f = ensure_npm_env(subprocess.run, checkout, prepared_dir, env, profile, args.rebuild, log)
            failures += f
        if failures:
            raise SetupFailure(failures)
        for build in target.get("npm_build", []):
            out = subprocess.run(["npm", "run", build], cwd=str(checkout), env=env, capture_output=True, text=True, timeout=300)
            build_log = evidence / ("build-%s.log" % build.replace(":", "-"))
            build_log.write_text(out.stdout + out.stderr)
            if out.returncode:
                raise SetupFailure([fail("dependencies", "npm run %s failed; see %s" % (build, build_log.name))])

        env = build_env(config, profile, target, checkout=checkout, toolchain=toolchain, venv=venv, home=home, tmp=tmp, locale_name=locale_name,
                        evidence=evidence, attempt_id=attempt_id, pass_env=pass_env, ambient=dict(os.environ))
        service_dir = checkout / profile["service_dir"]
        # Preflight from the child's own context; every independent fault in one pass.
        failures += check_scratch(evidence, home, tmp, config["min_free_mb"], timeout_s, config["reserve_s"])
        failures += check_node_npm(subprocess.run, toolchain, env)
        if venv is not None:
            failures += check_dependencies(subprocess.run, venv / "bin" / "python", env, [checkout / r for r in profile["python_requirements"]])
            caps = config["capability_sets"].get(target.get("capabilities") or "", [])
            if caps and not failures:
                failures += check_capabilities(subprocess.run, venv / "bin" / "python", env, service_dir / "tests", caps)
        if target.get("process_listing"):
            failures += check_process_listing(subprocess.run, env)
        if target.get("preflight_script") and not failures:
            failures += run_preflight_script(subprocess.run, checkout / target["preflight_script"], env)
        if failures:
            raise SetupFailure(failures)
        writer.emit("prepared", toolchain={k: {"version": v["version"], "sha256": v["sha256"], "realpath": v["realpath"]} for k, v in toolchain.items()},
                    locale=locale_name, dependency=dep_info, npm=npm_info, prepared_key=prepared_dir.name,
                    env_names=sorted(env), capabilities_checked=config["capability_sets"].get(target.get("capabilities") or "", []))
        log("preflight ok")

        if target["kind"] == "prepare":
            state.update(outcome="pass", first_failure=None)
        else:
            command, results = build_command(config, target, venv, checkout, service_dir, evidence, basetemp, selection, timeout_s, env)
            writer.emit("subject_started", command_kind=target["kind"], argv_digest=ga.digest_of(command), timeout_s=timeout_s)
            t_start = time.monotonic()
            if target["kind"] == "pytest_files":  # run_gate and the npm runner own their own teardown; a bare pytest subject needs one
                owner = OwnedProcesses(service_dir, basetemp / ".owned.json")
                env[OwnedProcesses.ENV] = str(owner.path)
            proc = subprocess.Popen(command, cwd=str(results["cwd"]), env=env, start_new_session=True)
            timed_out = False
            try:
                proc.wait(timeout=results.get("wall_s", timeout_s + config["reserve_s"]))
            except subprocess.TimeoutExpired:
                timed_out = True
                stop_group(proc)
            pgid = proc.pid
            if group_alive(pgid):
                state["secondary"]["cleanup"] = "survivors_terminated"
                stop_group(proc)
                if group_alive(pgid):
                    state["secondary"]["cleanup"] = "failed"
            outcome, ff, more = results["classify"](proc.returncode, timed_out)
            state.update(outcome=outcome, first_failure=ff)
            state["secondary"].update(more)
            state["extra"] = {"subject_elapsed_s": round(time.monotonic() - t_start, 3), "returncode": proc.returncode}
            if outcome == "pass" and state["secondary"].get("cleanup") == "failed":
                state["outcome"] = "cleanup_failure"
        state["evidence"] = evidence_refs(evidence / "gate")
    except SetupFailure as exc:
        state.update(outcome="setup_failure", first_failure={"stage": "setup", "code": exc.failures[0]["code"]},
                     extra={"failures": exc.failures})
    except Cancelled:
        if proc is not None and proc.poll() is None:
            stop_group(proc)
        state.update(outcome="cancelled", first_failure={"stage": "subject" if proc else "setup", "code": "cancelled"})
    except BaseException as exc:  # a crash still leaves a terminal receipt when it can
        if proc is not None and proc.poll() is None:
            stop_group(proc)
        state.update(outcome="harness_failure", first_failure={"stage": "setup", "code": "unexpected_" + type(exc).__name__})
    finally:
        for s, h in old.items():
            signal.signal(s, h)
        if owner is not None and not owner.stop():
            state["secondary"]["cleanup"] = "failed"
            state["secondary"]["owned_processes"] = "unreaped"
        # Owned scratch only: HOME, basetemp, TMPDIR. Evidence and the prepared cache stay.
        for d in (run_dir, tmp):
            try:
                shutil.rmtree(d)
            except FileNotFoundError:
                pass
            except OSError:
                state["secondary"]["cleanup"] = "failed"
        if state["secondary"].get("cleanup") == "failed" and state["outcome"] == "pass":
            state["outcome"] = "cleanup_failure"
        if not writer.terminal:
            try:
                writer.emit("terminal", outcome=state["outcome"], first_failure=state["first_failure"], secondary=state["secondary"],
                            evidence=state["evidence"], detail=state["extra"], stage=writer._state)
            except ga.AttemptError as exc:
                print("[gate-env] could not write terminal receipt: %s" % exc, file=sys.stderr)
        try:
            ga.write_index(evidence)
            if args.attempt_manifest:
                ga.register_in_manifest(args.attempt_manifest, {"attempt_id": attempt_id, "spec_id": identity["spec_id"], "series_id": identity["series_id"],
                                                                "evidence_root": str(evidence), "host": identity["host"]})
        except (OSError, ga.AttemptError) as exc:
            print("[gate-env] index/manifest not updated (events are authoritative): %s" % exc, file=sys.stderr)

    if state["outcome"] == "setup_failure":
        for f in state["extra"].get("failures", []):
            print("[gate-env] SETUP FAILURE %s: %s" % (f["code"], f["detail"]) + (" -> " + f["remediation"] if f["remediation"] else ""), file=sys.stderr)
    if missing_identity:
        print("[gate-env] note: missing %s; attempt recorded as unknown history and excluded from acceptance denominators" % ", ".join(missing_identity), file=sys.stderr)
    print("[gate-env] outcome=%s attempt=%s" % (state["outcome"], attempt_id))
    return EXIT.get(state["outcome"], 1)


def profile_root(service_dir: Path, checkout: Path) -> str:
    return str(service_dir.relative_to(checkout))


def build_command(config: Dict[str, Any], target: Dict[str, Any], venv: Optional[Path], checkout: Path, service_dir: Path, evidence: Path, basetemp: Path,
                  selection: List[str], timeout_s: int, env: Dict[str, str]) -> Tuple[List[str], Dict[str, Any]]:
    gate_dir = evidence / "gate"
    gate_dir.mkdir(parents=True, exist_ok=True)
    kind = target["kind"]
    if kind == "run_gate":
        tiers = 2 if target["gate"] == "merge" else 1
        limits = target.get("tier_timeouts")
        tier_s = max(1, timeout_s // tiers)
        runner = service_dir / "tools" / "run_gate.py"
        command = [str(venv / "bin" / "python"), str(runner), target["gate"], "--evidence-dir", str(gate_dir),
                   "--basetemp", str(basetemp)]
        if not limits:
            command += ["--timeout-seconds", str(tier_s)]
        wall_s = timeout_s + config["reserve_s"]
        if "--deadline-seconds" in runner.read_text("utf-8"):  # runner owns the finite envelope when it supports one
            budget = config["deadline"]
            preflights = 1 if target["gate"] in ("smoke", "merge") else 0
            # The runner measures the envelope from its own start, so launch/git-status time eats a bare sum: keep a start margin.
            deadline_s = (sum(limits.values()) if limits else tier_s * tiers) + budget["preflight_s"] * preflights + budget["reserve_s"] + budget["slack_s"]
            command += ["--deadline-seconds", str(deadline_s), "--deadline-reserve-seconds", str(budget["reserve_s"])]
            wall_s = deadline_s + budget["slack_s"]
        out = gate_dir / ("v2-%s-evidence.json" % target["gate"])
        return command, {"cwd": checkout, "wall_s": wall_s, "classify": lambda rc, to: classify_run_gate(out, rc, to, False)}
    if kind == "pytest_files":
        junit = gate_dir / "pytest.junit.xml"
        command = [str(venv / "bin" / "python"), "-m", "pytest", *selection, "-q", "-rs",
                   *(["--strict-capabilities"] if target.get("strict_capabilities", True) else []),
                   "--junitxml=%s" % junit, "--basetemp", str(basetemp / "pytest")]
        return command, {"cwd": checkout / target.get("select", {}).get("root", profile_root(service_dir, checkout)), "classify": lambda rc, to: classify_pytest(junit, rc, to, False)}
    if kind == "web_gate":
        summary = gate_dir / "web-summary.json"
        command = [str(venv / "bin" / "python"), str(checkout / "scripts" / "gate_web.py"), str(summary)]
        return command, {"cwd": checkout, "classify": lambda rc, to: classify_web(summary, rc, to)}
    summary = gate_dir / "npm-test-summary.json"
    env["PENTACLE_TEST_SUMMARY_PATH"] = str(summary)
    command = ["node", "scripts/run-tests.js", *selection]
    return command, {"cwd": checkout, "classify": lambda rc, to: classify_npm(summary, rc, to, False)}


if __name__ == "__main__":
    raise SystemExit(main())
