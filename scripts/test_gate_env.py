#!/usr/bin/env python3
"""Focused tests for scripts/gate_env.py and scripts/lib/gate_attempts.py.

Standalone (stdlib unittest, no pytest): `python3 scripts/test_gate_env.py`.
Setup faults are reproduced with disposable counterparts (fake executables and
injected runners); no real permission is weakened.
"""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
_spec = importlib.util.spec_from_file_location("gate_env", HERE / "gate_env.py")
ge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ge)
ga = ge.ga
REAL_CONFIG = json.loads((HERE / "gate-env.json").read_text())


def done(rc=0, out="", err=""):
    return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def fake_exe(folder: Path, name: str, version_line: str, rc: int = 0) -> Path:
    path = folder / name
    path.write_text("#!/bin/sh\necho '%s'\nexit %d\n" % (version_line, rc))
    path.chmod(0o755)
    return path


class Tmp(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory(prefix="ge-t-")
        self.tmp = Path(os.path.realpath(self._t.name))
        self.addCleanup(self._t.cleanup)


class AttemptReceipts(Tmp):
    def writer(self, **kw):
        return ga.AttemptWriter(self.tmp / "ev", kw.pop("attempt_id", ga.new_attempt_id()), identity={"spec_id": "s", "series_id": "x", "purpose": "acceptance"})

    def test_lifecycle_events_are_immutable_and_ordered(self):
        w = self.writer()
        w.emit("started")
        w.emit("prepared")
        w.emit("subject_started")
        w.emit("terminal", outcome="pass")
        names = sorted(p.name for p in w.dir.iterdir())
        self.assertEqual(names, ["0001-started.json", "0002-prepared.json", "0003-subject_started.json", "0004-terminal.json"])
        folded = ga.fold_attempt(w.dir)
        self.assertEqual((folded["status"], folded["primary_outcome"]), ("complete", "pass"))
        with self.assertRaises(ga.AttemptError):
            w.emit("terminal", outcome="pass")  # exactly one terminal

    def test_early_terminal_and_invalid_transition(self):
        w = self.writer()
        w.emit("started")
        with self.assertRaises(ga.AttemptError):
            w.emit("subject_started")  # cannot skip prepared
        with self.assertRaises(ga.AttemptError):
            w.emit("terminal", outcome="made_up")
        w.emit("terminal", outcome="setup_failure")
        self.assertEqual(ga.fold_attempt(w.dir)["primary_outcome"], "setup_failure")

    def test_crash_leaves_visible_incomplete_attempt(self):
        w = self.writer()
        w.emit("started")
        w.emit("prepared")
        folded = ga.fold_attempt(w.dir)
        self.assertEqual((folded["status"], folded["primary_outcome"], folded["last_state"]), ("incomplete", "incomplete", "prepared"))
        self.assertEqual(ga.build_index(self.tmp / "ev")["attempts"][0]["status"], "incomplete")

    def test_tamper_missing_sequence_and_event_after_terminal_invalidate(self):
        w = self.writer()
        for s in ("started", "prepared"):
            w.emit(s)
        w.emit("terminal", outcome="pass")
        (w.dir / "0002-prepared.json").unlink()
        self.assertIn("missing or duplicate sequence", ga.fold_attempt(w.dir)["problems"])
        w2 = self.writer()
        w2.emit("started")
        w2.emit("terminal", outcome="pass")
        body = json.loads((w2.dir / "0002-terminal.json").read_text())
        body["outcome"] = "setup_failure"
        (w2.dir / "0002-terminal.json").write_text(json.dumps(body))
        self.assertTrue(any("digest mismatch" in p for p in ga.fold_attempt(w2.dir)["problems"]))
        w3 = self.writer()
        w3.emit("started")
        w3.emit("terminal", outcome="pass")
        extra = json.loads((w3.dir / "0002-terminal.json").read_text())
        extra["seq"] = 3
        (w3.dir / "0003-prepared.json").write_text(json.dumps(extra))
        self.assertEqual(ga.fold_attempt(w3.dir)["status"], "invalid")

    def test_exclusive_create_refuses_second_owner(self):
        a = self.writer(attempt_id="same")
        a.emit("started")
        b = self.writer(attempt_id="same")
        with self.assertRaises(ga.AttemptError):
            b.emit("started")  # conflicting digest for sequence 1

    def test_index_is_derived_and_drift_is_reported(self):
        w = self.writer()
        w.emit("started")
        ga.write_index(self.tmp / "ev")
        self.assertEqual(ga.validate_index(self.tmp / "ev"), [])
        w.emit("terminal", outcome="pass")
        self.assertTrue(ga.validate_index(self.tmp / "ev"))
        self.assertEqual(ga.fold_attempt(w.dir)["status"], "complete")  # events stay authoritative

    def test_observations_and_manifest_are_idempotent_and_conflicts_fail(self):
        w = self.writer()
        w.emit("started")
        ga.record_observation(w.dir, "child1", {"outcome": "pass"})
        ga.record_observation(w.dir, "child1", {"outcome": "pass"})
        with self.assertRaises(ga.AttemptError):
            ga.record_observation(w.dir, "child1", {"outcome": "pass", "x": 1})
        self.assertEqual(ga.fold_attempt(w.dir)["status"], "incomplete")  # children never allocate root state
        manifest = self.tmp / "m.json"
        entry = {"attempt_id": w.attempt_id, "spec_id": "s"}
        ga.register_in_manifest(manifest, entry)
        ga.register_in_manifest(manifest, entry)
        self.assertEqual(len(json.loads(manifest.read_text())["attempts"]), 1)
        with self.assertRaises(ga.AttemptError):
            ga.register_in_manifest(manifest, dict(entry, spec_id="other"))
        manifest.write_text("not json")
        with self.assertRaises(ga.AttemptError):
            ga.register_in_manifest(manifest, entry)  # never overwrites a corrupt manifest


class ConfigFailsClosed(Tmp):
    def write(self, mutate):
        cfg = json.loads(json.dumps(REAL_CONFIG))
        mutate(cfg)
        path = self.tmp / "gate-env.json"
        path.write_text(json.dumps(cfg))
        return path

    def test_checked_in_config_loads(self):
        self.assertEqual(ge.load_config(HERE / "gate-env.json")["schema"], "pentacle.gate-env.v1")

    def test_malformed_variants_are_refused(self):
        bad = {
            "schema": lambda c: c.update(schema="other"),
            "target kind": lambda c: c["profiles"]["pentacle"]["targets"]["v2-unit"].update(kind="shell"),
            "timeout over max": lambda c: c["profiles"]["pentacle"]["targets"]["v2-unit"].update(timeout_s=99999),
            "unknown capability set": lambda c: c["profiles"]["pentacle"]["targets"]["v2-unit"].update(capabilities="nope"),
            "no targets": lambda c: c["profiles"]["pentacle"].update(targets={}),
            "deadline budget": lambda c: c["deadline"].update(slack_s=-1),
        }
        for label, mutate in bad.items():
            with self.subTest(label), self.assertRaises(ge.SetupFailure):
                ge.load_config(self.write(mutate))
        broken = self.tmp / "broken.json"
        broken.write_text("{")
        with self.assertRaises(ge.SetupFailure):
            ge.load_config(broken)
        with self.assertRaises(ge.SetupFailure):
            ge.load_config(self.tmp / "missing.json")


class ToolchainResolution(Tmp):
    def config(self):
        cfg = json.loads(json.dumps(REAL_CONFIG))
        cfg["search_path"] = {"darwin": [], "linux": []}
        cfg["system_path"] = []
        return cfg

    def test_daemon_node_closure_with_separate_tool_directories(self):
        cfg = self.config()
        profile = cfg["profiles"]["pentacle"]
        dirs = {name: self.tmp / name for name in ("python", "tmux", "node")}
        for folder in dirs.values():
            folder.mkdir()
        fake_exe(dirs["python"], "python3.13", "Python 3.13.15")
        fake_exe(dirs["tmux"], "tmux", "tmux 3.4")
        fake_exe(dirs["node"], "node", "v22.4.1")
        ambient = os.pathsep.join(str(p) for p in dirs.values())
        for name in ("v2-unit", "v2-smoke", "v2-merge", "v2-merge-ci", "v2-pytest"):
            with self.subTest(target=name):
                target = profile["targets"][name]
                self.assertIn("node", target["needs"])
                tools, failures = ge.resolve_toolchain(cfg, target["needs"], ambient_path=ambient, system="linux")
                self.assertEqual(failures, [])
                env = ge.build_env(cfg, profile, target, checkout=self.tmp / "co", toolchain=tools,
                                   venv=None, home=self.tmp / "h", tmp=self.tmp / "t", locale_name="C.UTF-8",
                                   evidence=self.tmp / "e", attempt_id="split-tools", pass_env={}, ambient={})
                child = subprocess.run(["node", "--version"], env=env, capture_output=True, text=True)
                self.assertEqual((child.returncode, child.stdout.strip()), (0, "v22.4.1"))

    def test_missing_node_has_named_setup_diagnostic(self):
        tools, failures = ge.resolve_toolchain(self.config(), ["node"], ambient_path=str(self.tmp), system="linux")
        self.assertEqual(tools, {})
        self.assertEqual([f["code"] for f in failures], ["missing_node"])
        self.assertIn("node 22", failures[0]["detail"])

    def test_wrong_node_major_is_rejected_and_right_one_found(self):
        old, new = self.tmp / "old", self.tmp / "new"
        old.mkdir(), new.mkdir()
        fake_exe(old, "node", "v20.1.0")
        fake_exe(new, "node", "v22.4.1")
        fake_exe(new, "npm", "10.9.0")
        found, failures = ge.resolve_toolchain(self.config(), ["node", "npm"], ambient_path="%s:%s" % (old, new), system="linux")
        self.assertEqual(failures, [])
        self.assertEqual(found["node"]["version"], "22.4.1")
        self.assertEqual(Path(found["npm"]["path"]).parent, new)  # npm comes from node's own directory

    def test_missing_npm_is_a_setup_failure_not_a_guessed_path(self):
        d = self.tmp / "n"
        d.mkdir()
        fake_exe(d, "node", "v22.4.1")
        found, failures = ge.resolve_toolchain(self.config(), ["node", "npm"], ambient_path=str(d), system="linux")
        self.assertEqual([f["code"] for f in failures], ["toolchain"])
        self.assertIn("npm", failures[0]["detail"])
        self.assertNotIn("npm-cli.js", failures[0]["detail"] + failures[0]["remediation"])

    def test_env_node_shebang_npm_is_resolved_with_its_own_directory_on_path(self):
        # Real npm is `#!/usr/bin/env node`; the version probe must find node next to it (regression: first npm cold run).
        d = self.tmp / "n"
        d.mkdir()
        fake_exe(d, "node", "v22.4.1")
        (d / "npm").write_text("#!/usr/bin/env node\n")
        (d / "npm").write_text("#!/bin/sh\nnode --version >/dev/null 2>&1 || exit 127\necho 10.9.0\n")
        (d / "npm").chmod(0o755)
        found, failures = ge.resolve_toolchain(self.config(), ["node", "npm"], ambient_path=str(d), system="linux")
        self.assertEqual((failures, found["npm"]["version"]), ([], "10.9.0"))

    def test_npm_that_does_not_run_is_rejected(self):
        d = self.tmp / "n"
        d.mkdir()
        fake_exe(d, "node", "v22.4.1")
        fake_exe(d, "npm", "boom", rc=1)
        _, failures = ge.resolve_toolchain(self.config(), ["node", "npm"], ambient_path=str(d), system="linux")
        self.assertTrue(failures and "does not report a version" in failures[0]["detail"])

    def test_independent_faults_are_reported_together(self):
        _, failures = ge.resolve_toolchain(self.config(), ["python", "node", "npm", "tmux"], ambient_path=str(self.tmp), system="linux")
        self.assertGreaterEqual(len(failures), 3)

    def test_tmux_uses_its_own_version_flag(self):
        d = self.tmp / "t"
        d.mkdir()
        (d / "tmux").write_text('#!/bin/sh\n[ "$1" = "-V" ] && echo "tmux 3.5a" && exit 0\nexit 1\n')
        (d / "tmux").chmod(0o755)
        found, failures = ge.resolve_toolchain(self.config(), ["tmux"], ambient_path=str(d), system="linux")
        self.assertEqual((failures, found["tmux"]["version"]), ([], "3.5"))


class ChildEnvironment(Tmp):
    def build(self, **kw):
        profile = REAL_CONFIG["profiles"]["pentacle"]
        target = dict(profile["targets"]["v2-unit"], **kw.pop("target", {}))
        tool = {"tmux": {"path": "/opt/homebrew/bin/tmux"}, "python": {"path": "/x/python"}}
        return ge.build_env(REAL_CONFIG, profile, target, checkout=self.tmp / "co", toolchain=tool, venv=self.tmp / "venv", home=self.tmp / "h", tmp=self.tmp / "t",
                            locale_name="en_US.UTF-8", evidence=self.tmp / "e", attempt_id="a1", pass_env=kw.pop("pass_env", {}), ambient=kw.pop("ambient", {}))

    def test_hostile_ambient_flags_cannot_reach_the_child(self):
        hostile = {"MAKEFLAGS": "-j99", "TMUX": "/tmp/x,1,0", "AWS_SECRET_ACCESS_KEY": "s", "PYTEST_ADDOPTS": "-k nothing", "HOME": "/real/home",
                   "PYTHONPATH": "/evil", "LC_ALL": "C", "NODE_OPTIONS": "--require=/evil.js", "V2_UNIT_TIMEOUT_S": "1"}
        with mock.patch.dict(os.environ, hostile):
            env = self.build(ambient=dict(os.environ))
        for name in ("MAKEFLAGS", "TMUX", "AWS_SECRET_ACCESS_KEY", "PYTEST_ADDOPTS", "NODE_OPTIONS", "V2_UNIT_TIMEOUT_S"):
            self.assertNotIn(name, env)
        self.assertEqual(env["HOME"], str(self.tmp / "h"))
        self.assertEqual(env["LC_ALL"], "en_US.UTF-8")
        self.assertNotIn("/evil", env["PYTHONPATH"])
        self.assertEqual(env["PENTACLE_TEST_STRICT_CAPABILITIES"], "1")
        self.assertTrue(env["PATH"].startswith(str(self.tmp / "venv" / "bin")))
        self.assertNotIn("/real/home", json.dumps(env))

    def test_only_individually_declared_names_pass(self):
        with self.assertRaises(ge.SetupFailure):
            self.build(pass_env={"SOAK_TIER": "short"})
        env = self.build(target={"pass_env": ["SOAK_TIER"]}, pass_env={"SOAK_TIER": "short"})
        self.assertEqual(env["SOAK_TIER"], "short")
        with self.assertRaises(ge.SetupFailure):
            self.build(target={"pass_env": ["bad name"]}, pass_env={"bad name": "x"})

    def test_pytest_addopts_only_as_fixed_profile_value(self):
        self.assertEqual(self.build(target={"pytest_addopts": "-p no:cacheprovider"})["PYTEST_ADDOPTS"], "-p no:cacheprovider")


class Layout(Tmp):
    def test_good_layout_has_no_findings(self):
        co, ws, ev = self.tmp / "co", self.tmp / "ws", self.tmp / "ev"
        for p in (co, ws, ev):
            p.mkdir()
        home, tmp, base = ge.scratch_paths(ws, self.tmp / "t", "abcdef0123")
        self.assertEqual(ge.check_layout(co, ws, ev, home, tmp, base, 200), [])
        self.assertEqual(base.parent, tmp)

    def test_home_or_evidence_under_basetemp_and_checkout_are_refused(self):
        co, ws = self.tmp / "co", self.tmp / "ws"
        co.mkdir(), ws.mkdir()
        tmp = self.tmp / "t"
        base = tmp / "b"
        findings = ge.check_layout(co, ws, base / "ev", base / "home", tmp, base, 200)
        self.assertGreaterEqual(len([f for f in findings if "nested" in f["detail"]]), 2)
        inside = ge.check_layout(co, ws, co / "ev", ws / "h", tmp, base, 200)
        self.assertTrue(any("source checkout" in f["detail"] for f in inside))
        outside = ge.check_layout(co, ws, self.tmp / "ev", ws / "h", tmp, ws / "elsewhere", 200)
        self.assertTrue(any("not under TMPDIR" in f["detail"] for f in outside))

    def test_long_tmpdir_is_refused_with_socket_reason(self):
        findings = ge.check_layout(self.tmp / "co", self.tmp / "ws", self.tmp / "ev", self.tmp / "h", self.tmp / ("x" * 60), self.tmp / ("x" * 60) / "b", 48)
        self.assertTrue(any("socket" in f["detail"] for f in findings))


class PreflightFaults(Tmp):
    """Disposable counterparts of the two B07 setup failures, plus the rest."""

    def test_denied_ps_is_a_setup_failure_before_subject_launch(self):
        def denied(*a, **k):
            raise PermissionError(1, "Operation not permitted")
        with mock.patch.object(ge.sys, "platform", "darwin"):
            failures = ge.check_process_listing(denied, {"PATH": "/bin"})
        self.assertEqual([f["code"] for f in failures], ["capability"])
        self.assertIn("ps", failures[0]["detail"])

    def test_ps_with_no_rows_is_refused(self):
        with mock.patch.object(ge.sys, "platform", "darwin"):
            self.assertTrue(ge.check_process_listing(lambda *a, **k: done(0, ""), {}))
            self.assertEqual(ge.check_process_listing(lambda *a, **k: done(0, " 1 1 Ss\n"), {}), [])

    def test_dependency_mismatch_and_missing_are_reported(self):
        req = self.tmp / "r.txt"
        req.write_text("websockets==17.0.1\nboto3>=1.34\n# c\n")
        wrong = lambda *a, **k: done(0, json.dumps([["websockets", "16.0"]]))  # noqa: E731
        failures = ge.check_dependencies(wrong, Path("/v/python"), {}, [req])
        self.assertEqual(len(failures), 2)
        right = lambda *a, **k: done(0, json.dumps([["websockets", "17.0.1"], ["boto3", "1.40"]]))  # noqa: E731
        self.assertEqual(ge.check_dependencies(right, Path("/v/python"), {}, [req]), [])

    def test_required_capability_absence_refuses(self):
        out = json.dumps({"tmux usable": ["absent", "tmux executable unavailable"], "ps argv readable": ["available", "ok"]})
        failures = ge.check_capabilities(lambda *a, **k: done(0, out), Path("/v/python"), {}, self.tmp, ["tmux usable", "ps argv readable"])
        self.assertEqual(len(failures), 1)
        self.assertIn("tmux usable", failures[0]["detail"])
        self.assertTrue(ge.check_capabilities(lambda *a, **k: done(1, ""), Path("/v/python"), {}, self.tmp, ["x"]))

    def test_selection_rejects_traversal_flags_and_foreign_patterns(self):
        target = REAL_CONFIG["profiles"]["pentacle"]["targets"]["v2-pytest"]
        co = self.tmp / "co"
        (co / "services/chat-stream-v2/tests").mkdir(parents=True)
        (co / "services/chat-stream-v2/tests/test_a.py").write_text("")
        self.assertEqual(ge.validate_selection(target, ["tests/test_a.py::test_x"], co), ["tests/test_a.py::test_x"])
        for bad in ("../x.py", "--co", "/etc/passwd", "tests/test_missing.py", "tests/test_a.py; rm -rf /", "tests/conftest.py"):
            with self.subTest(bad), self.assertRaises(ge.SetupFailure):
                ge.validate_selection(target, [bad], co)
        with self.assertRaises(ge.SetupFailure):
            ge.validate_selection(REAL_CONFIG["profiles"]["pentacle"]["targets"]["v2-unit"], ["tests/test_a.py"], co)


class PreparedCache(Tmp):
    def fakes(self):
        calls = []

        def run(cmd, **k):
            calls.append(list(cmd))
            if "venv" in cmd:
                (Path(cmd[-1]) / "bin").mkdir(parents=True)
                (Path(cmd[-1]) / "bin" / "python").write_text("")
            if "-c" in cmd and "importlib.metadata" in cmd[-1]:
                return done(0, json.dumps(self.frozen))
            return done(0)
        return run, calls

    def setUp(self):
        super().setUp()
        self.frozen = [["websockets", "17.0.1"]]
        self.co = self.tmp / "co"
        (self.co / "req").mkdir(parents=True)
        (self.co / "req/r.txt").write_text("websockets==17.0.1\n")
        self.profile = {"python_requirements": ["req/r.txt"], "python_editable": []}
        self.tool = {"python": {"path": "/p/python3.13", "version": "3.13.15", "sha256": "aa"}}

    def prepare(self, run, rebuild=False):
        return ge.ensure_python_env(run, REAL_CONFIG, self.profile, self.tool, self.co, self.tmp / "prep", {}, rebuild, lambda m: None)

    def test_warm_repeat_reuses_only_matching_inputs(self):
        run, calls = self.fakes()
        venv, info, failures = self.prepare(run)
        self.assertEqual((failures, info["reused"]), ([], False))
        installs = [c for c in calls if "install" in c]
        self.assertEqual(len(installs), 1)
        venv2, info2, failures2 = self.prepare(run)
        self.assertEqual((failures2, info2["reused"]), ([], True))
        self.assertEqual(len([c for c in calls if "install" in c]), 1)  # no second install

    def test_changed_dependency_or_profile_changes_the_cache_key(self):
        base = ge.prepared_key(REAL_CONFIG, "pentacle", self.profile, self.tool, self.co, False)
        (self.co / "req/r.txt").write_text("websockets==17.0.2\n")
        self.assertNotEqual(base, ge.prepared_key(REAL_CONFIG, "pentacle", self.profile, self.tool, self.co, False))
        self.assertNotEqual(base, ge.prepared_key(REAL_CONFIG, "pentacle", dict(self.profile, pythonpath=["x"]), self.tool, self.co, False))

    def test_drifted_prepared_env_is_refused_not_silently_reused(self):
        run, _ = self.fakes()
        self.prepare(run)
        self.frozen = [["websockets", "99.0"]]
        _, _, failures = self.prepare(run)
        self.assertEqual([f["code"] for f in failures], ["cache"])
        _, info, failures = self.prepare(run, rebuild=True)  # explicit rebuild removes only the owned dir
        self.assertEqual((failures, info["reused"]), ([], False))


class Classification(Tmp):
    def evidence(self, tier):
        path = self.tmp / "e.json"
        path.write_text(json.dumps({"tiers": [tier]}))
        return path

    def test_run_gate_outcomes(self):
        cases = [
            ({"tier": "unit", "passed": True}, 0, "pass"),
            ({"tier": "unit", "passed": False, "reason": "ambient_tmux"}, 1, "setup_failure"),
            ({"tier": "unit", "passed": False, "reason": "test_failures", "counts": {"failures": 2, "errors": 0}}, 1, "product_failure"),
            ({"tier": "unit", "passed": False, "reason": "test_failures", "counts": {"failures": 0, "errors": 3}}, 1, "unknown"),
            ({"tier": "unit", "passed": False, "reason": "timeout"}, 124, "timeout_unclassified"),
            ({"tier": "unit", "passed": False, "reason": "missing_junit"}, 1, "harness_failure"),
        ]
        for tier, rc, expected in cases:
            with self.subTest(expected, reason=tier.get("reason")):
                self.assertEqual(ge.classify_run_gate(self.evidence(tier), rc, False, False)[0], expected)
        self.assertEqual(ge.classify_run_gate(self.tmp / "none.json", 1, False, False)[0], "harness_failure")
        self.assertEqual(ge.classify_run_gate(self.tmp / "none.json", 1, True, False)[0], "timeout_unclassified")

    def test_run_gate_deadline_runner_fields_are_read(self):
        def tier(**kw):
            return dict({"tier": "unit", "passed": False, "reason": "x", "primary_outcome": "x"}, **kw)

        def fold(data, rc=1):
            path = self.tmp / "e.json"
            path.write_text(json.dumps(data))
            return ge.classify_run_gate(path, rc, False, False)
        out, ff, sec = fold({"finished": True, "tiers": [tier(primary_outcome="deadline_envelope_exceeded", reason="deadline_envelope_exceeded")]})
        self.assertEqual((out, ff["code"]), ("setup_failure", "deadline_envelope_exceeded"))
        self.assertEqual(fold({"finished": False, "tiers": []})[0], "incomplete")
        self.assertEqual(fold({"finished": True, "tiers": [tier(primary_outcome="evidence_incomplete", secondary_outcomes=["journal_incomplete"])]})[::2],
                         ("harness_failure", {"reporting": ["journal_incomplete"]}))
        self.assertEqual(fold({"finished": True, "tiers": [tier(primary_outcome="pytest_failed", termination={"kind": "external_signal"})]})[0], "cancelled")
        self.assertEqual(fold({"finished": True, "tiers": [tier(primary_outcome="pytest_failed", termination={"kind": "subject_signal", "signal": "SIGKILL"})]})[0], "harness_failure")
        # primary_outcome wins over the legacy reason; failures still split product from unknown
        self.assertEqual(fold({"finished": True, "tiers": [tier(reason="old", primary_outcome="test_failures", counts={"failures": 1})]})[0], "product_failure")
        # a green primary keeps reporting facts as secondaries and is still a pass
        self.assertEqual(fold({"finished": True, "tiers": [{"tier": "unit", "passed": True, "primary_outcome": None, "secondary_outcomes": ["output_truncated"]}]}, rc=0)[::2],
                         ("pass", {"reporting": ["output_truncated"], "tiers": ["unit"]}))

    def test_pytest_and_npm_outcomes(self):
        junit = self.tmp / "j.xml"
        junit.write_text('<testsuite tests="3" failures="1" errors="0" skipped="0"/>')
        self.assertEqual(ge.classify_pytest(junit, 1, False, False)[0], "product_failure")
        junit.write_text('<testsuite tests="3" failures="0" errors="0" skipped="0"/>')
        self.assertEqual(ge.classify_pytest(junit, 0, False, False)[0], "pass")
        junit.write_text('<testsuite tests="0" failures="0" errors="0" skipped="0"/>')
        self.assertEqual(ge.classify_pytest(junit, 5, False, False)[0], "harness_failure")
        summary = self.tmp / "s.json"
        summary.write_text(json.dumps({"exit_code": 0, "files": [{"file": "a", "status": "pass", "cleanup": {"status": "clean"}}]}))
        self.assertEqual(ge.classify_npm(summary, 0, False, False)[0], "pass")
        summary.write_text(json.dumps({"exit_code": 1, "files": [{"file": "a", "status": "fail", "cleanup": {"status": "clean"}, "execution": {"exitCode": 1}}]}))
        self.assertEqual(ge.classify_npm(summary, 1, False, False)[0], "product_failure")
        summary.write_text(json.dumps({"exit_code": 1, "files": [{"file": "a", "status": "timeout", "cleanup": {"status": "clean"}}]}))
        self.assertEqual(ge.classify_npm(summary, 1, False, False)[0], "timeout_unclassified")


class DeadlineEnvelope(Tmp):
    """gate_env hands run_gate a finite --deadline-seconds only when that runner supports it."""

    def command(self, gate, runner_text):
        service = self.tmp / "svc"
        (service / "tools").mkdir(parents=True, exist_ok=True)
        (service / "tools" / "run_gate.py").write_text(runner_text)
        target = dict(REAL_CONFIG["profiles"]["pentacle"]["targets"]["v2-" + gate], timeout_s=900 if gate != "merge" else 1800)
        command, results = ge.build_command(REAL_CONFIG, target, self.tmp / "venv", self.tmp, service, self.tmp / "ev", self.tmp / "t" / "b", [], target["timeout_s"], {})
        return command, results

    def test_deadline_covers_tiers_preflight_and_reserve(self):
        command, results = self.command("merge", "parser.add_argument('--deadline-seconds')")
        self.assertEqual(command[command.index("--timeout-seconds") + 1], "900")
        self.assertEqual(command[command.index("--deadline-seconds") + 1], str(900 * 2 + 30 + 60 + 30))
        self.assertEqual(results["wall_s"], 900 * 2 + 30 + 60 + 30 + 30)
        unit, results = self.command("unit", "--deadline-seconds")
        self.assertEqual(unit[unit.index("--deadline-seconds") + 1], str(900 + 60 + 30))  # unit has no smoke preflight

    def test_older_runner_without_the_flag_keeps_the_wall_bound(self):
        command, results = self.command("unit", "parser.add_argument('--timeout-seconds')")
        self.assertNotIn("--deadline-seconds", command)
        self.assertEqual(results["wall_s"], 900 + REAL_CONFIG["reserve_s"])


class EntrypointReceipts(Tmp):
    """End-to-end through main(): the started receipt precedes everything."""

    def run_main(self, *extra, patch_load=None):
        argv = ["run", "--profile", "pentacle", "--workspace", str(self.tmp / "ws"), "--evidence-dir", str(self.tmp / "ev"), "--checkout", str(self.tmp / "co"),
                "--tmp-root", str(self.tmp / "tr"), *extra]
        (self.tmp / "co").mkdir(exist_ok=True)
        with mock.patch.object(ge, "source_identity", return_value={"sha": "x", "dirty": False}):
            if patch_load:
                with mock.patch.object(ge, "load_config", side_effect=patch_load):
                    return ge.main(argv)
            return ge.main(argv)

    def attempts(self):
        return ga.build_index(self.tmp / "ev")["attempts"]

    def test_unknown_target_is_a_retained_setup_failure_and_missing_identity_is_unknown_history(self):
        code = self.run_main("--target", "does-not-exist")
        self.assertEqual(code, 3)
        (a,) = self.attempts()
        self.assertEqual((a["primary_outcome"], a["status"], a["history"], a["spec_id"]), ("setup_failure", "complete", "unknown", "unknown"))
        self.assertTrue((self.tmp / "ev" / "test-attempts.v1.json").is_file())

    def test_malformed_config_fails_closed_with_a_receipt(self):
        code = self.run_main("--target", "v2-unit", "--spec", "s", "--series", "x", "--purpose", "acceptance",
                             patch_load=ge.SetupFailure([ge.fail("config", "malformed")]))
        self.assertEqual(code, 3)
        (a,) = self.attempts()
        self.assertEqual((a["primary_outcome"], a["history"], a["purpose"]), ("setup_failure", "first_observed", "acceptance"))

    def test_resolved_default_scope_survives_setup_failure_without_prepared(self):
        with mock.patch.multiple(ge, resolve_toolchain=lambda *a, **k: ({}, [ge.fail("toolchain", "fixture setup failure")]),
                                 pick_locale=lambda *a, **k: ("en_US.UTF-8", None), check_layout=lambda *a, **k: []):
            code = self.run_main("--target", "npm-test", "--spec", "s", "--series", "x", "--purpose", "negative_control")
        self.assertEqual(code, 3)
        files = sorted((self.tmp / "ev" / "attempts").glob("*/*.json"))
        events = [json.loads(p.read_text()) for p in files]
        self.assertEqual([e["state"] for e in events], ["started", "terminal"])
        self.assertEqual(events[-1]["effective_selection"], REAL_CONFIG["profiles"]["pentacle"]["targets"]["npm-test"]["default_patterns"])

    def test_unresolved_config_failure_never_fabricates_scope(self):
        self.run_main("--target", "npm-test", patch_load=ge.SetupFailure([ge.fail("config", "fixture malformed config")]))
        files = sorted((self.tmp / "ev" / "attempts").glob("*/*.json"))
        self.assertFalse(any("effective_selection" in json.loads(p.read_text()) for p in files))

    def test_run_without_target_is_refused_after_started_receipt(self):
        self.assertEqual(self.run_main(), 3)
        self.assertEqual(self.attempts()[0]["status"], "complete")

    def test_sigterm_during_setup_records_cancelled_and_cleans_owned_scratch(self):
        def kill_self(*a, **k):
            os.kill(os.getpid(), signal.SIGTERM)
            time_sleep()
        time_sleep = lambda: __import__("time").sleep(5)  # noqa: E731
        code = self.run_main("--target", "v2-unit", "--spec", "s", "--series", "x", "--purpose", "acceptance", patch_load=kill_self)
        self.assertEqual(code, 130)
        (a,) = self.attempts()
        self.assertEqual(a["primary_outcome"], "cancelled")
        self.assertFalse((self.tmp / "ws" / "runs").exists() and any((self.tmp / "ws" / "runs").iterdir()))

    def test_prepare_with_a_target_never_launches_a_subject(self):
        (self.tmp / "co" / "services" / "chat-stream-v2").mkdir(parents=True, exist_ok=True)
        (self.tmp / "co" / "services" / "chat-stream-v2" / "requirements.txt").write_text("websockets==17.0.1\n")
        tool = {"path": "/x/t", "realpath": "/x/t", "version": "3.13.1", "sha256": "aa"}
        short = tempfile.mkdtemp(prefix="gt", dir="/tmp")  # TMPDIR must stay under the socket-path bound
        self.addCleanup(lambda: __import__("shutil").rmtree(short, ignore_errors=True))

        def boom(*a, **k):
            raise AssertionError("prepare launched a subject")
        with mock.patch.multiple(ge, resolve_toolchain=lambda *a, **k: ({"python": tool, "tmux": tool}, []), pick_locale=lambda *a, **k: ("en_US.UTF-8", None),
                                 ensure_python_env=lambda *a, **k: (self.tmp / "venv", {"reused": True}, []), check_scratch=lambda *a, **k: [],
                                 check_node_npm=lambda *a, **k: [], check_dependencies=lambda *a, **k: [], check_capabilities=lambda *a, **k: [],
                                 build_command=boom, source_identity=lambda *a, **k: {"sha": "x", "dirty": False}), mock.patch.object(ge.subprocess, "Popen", boom):
            code = ge.main(["prepare", "--profile", "pentacle", "--workspace", str(self.tmp / "ws"), "--evidence-dir", str(self.tmp / "ev"), "--checkout", str(self.tmp / "co"),
                            "--tmp-root", short, "--target", "v2-pytest", "--spec", "s", "--series", "x", "--purpose", "diagnostic"])
        self.assertEqual(code, 0)
        (a,) = self.attempts()
        self.assertEqual((a["primary_outcome"], a["last_state"]), ("pass", "terminal"))
        states = sorted(p.name.split("-", 1)[1] for p in (self.tmp / "ev" / "attempts" / a["attempt_id"]).glob("0*"))
        self.assertEqual(states, ["prepared.json", "started.json", "terminal.json"])  # no subject_started

    def test_cancellation_reaps_the_recorded_subject_tree(self):
        """Regression: a cancelled pytest subject left its detached daemon behind (it is not in the subject's process group)."""
        pidfile = self.tmp / "orphan.pid"
        short = tempfile.mkdtemp(prefix="gt", dir="/tmp")
        self.addCleanup(lambda: __import__("shutil").rmtree(short, ignore_errors=True))
        (self.tmp / "co" / "services" / "chat-stream-v2").mkdir(parents=True, exist_ok=True)
        (self.tmp / "co" / "services" / "chat-stream-v2" / "requirements.txt").write_text("websockets==17.0.1\n")
        tools = self.tmp / "co" / "services" / "chat-stream-v2" / "tools"
        tools.mkdir(parents=True, exist_ok=True)
        (self.tmp / "co" / "services" / "chat-stream-v2" / "tests").mkdir(exist_ok=True)
        (self.tmp / "co" / "services" / "chat-stream-v2" / "tests" / "test_x.py").write_text("")
        (tools / "gate_owner_manifest.py").write_bytes((HERE.parent / "services" / "chat-stream-v2" / "tools" / "gate_owner_manifest.py").read_bytes())
        tool = {"path": "/x/t", "realpath": "/x/t", "version": "3.13.1", "sha256": "aa"}
        # A subject that records a detached child into the owner manifest, like the daemon fixtures do, then waits.
        script = ("import os,subprocess,sys,time,importlib.util as u;"
                  "m=u.spec_from_file_location('g',sys.argv[1]);g=u.module_from_spec(m);m.loader.exec_module(g);"
                  "c=subprocess.Popen(['sleep','300'],start_new_session=True);"
                  "g.record(os.environ['V2_GATE_OWNER_MANIFEST'],c.pid,None,c.pid);"
                  "open(sys.argv[2],'w').write(str(c.pid));time.sleep(300)")
        argv = [sys.executable, "-c", script, str(tools / "gate_owner_manifest.py"), str(pidfile)]

        def fire():  # cancel once the subject has recorded its orphan-to-be
            deadline = __import__("time").time() + 20
            while not pidfile.exists() and __import__("time").time() < deadline:
                __import__("time").sleep(0.05)
            os.kill(os.getpid(), signal.SIGTERM)
        import threading
        threading.Thread(target=fire, daemon=True).start()
        with mock.patch.multiple(ge, resolve_toolchain=lambda *a, **k: ({"python": tool, "tmux": tool}, []), pick_locale=lambda *a, **k: ("en_US.UTF-8", None),
                                 ensure_python_env=lambda *a, **k: (self.tmp / "venv", {"reused": True}, []), check_scratch=lambda *a, **k: [],
                                 check_node_npm=lambda *a, **k: [], check_dependencies=lambda *a, **k: [], check_capabilities=lambda *a, **k: [],
                                 build_command=lambda *a, **k: (argv, {"cwd": self.tmp, "wall_s": 120, "classify": lambda rc, to: ("pass", None, {})}),
                                 source_identity=lambda *a, **k: {"sha": "x", "dirty": False}):
            code = ge.main(["run", "--profile", "pentacle", "--workspace", str(self.tmp / "ws"), "--evidence-dir", str(self.tmp / "ev"), "--checkout", str(self.tmp / "co"),
                            "--tmp-root", short, "--target", "v2-pytest", "--select", "tests/test_x.py", "--spec", "s", "--series", "x", "--purpose", "diagnostic"])
        self.assertEqual(code, 130)
        orphan = int(pidfile.read_text())
        deadline = __import__("time").time() + 10
        alive = True
        while alive and __import__("time").time() < deadline:
            try:
                os.kill(orphan, 0)
                __import__("time").sleep(0.1)
            except ProcessLookupError:
                alive = False
        if alive:
            os.kill(orphan, signal.SIGKILL)
        self.assertFalse(alive, "the detached orphan survived cancellation")
        (a,) = self.attempts()
        self.assertEqual(a["primary_outcome"], "cancelled")
        self.assertNotEqual(a.get("secondary", {}).get("cleanup"), "failed")

    def test_prepare_refuses_select_and_pytest_run_needs_one(self):
        # run with no --select on the focused pytest target: refused before any setup, never the full suite
        code = self.run_main("--target", "v2-pytest", "--spec", "s", "--series", "x", "--purpose", "diagnostic")
        self.assertEqual(code, 3)
        (a,) = self.attempts()
        self.assertEqual((a["primary_outcome"], a["first_failure"]["code"], a["last_state"]), ("setup_failure", "selection", "terminal"))
        argv = ["prepare", "--profile", "pentacle", "--workspace", str(self.tmp / "ws"), "--evidence-dir", str(self.tmp / "ev2"), "--checkout", str(self.tmp / "co"),
                "--target", "v2-pytest", "--select", "tests/test_x.py"]
        with mock.patch.object(ge, "source_identity", return_value={"sha": "x", "dirty": False}):
            self.assertEqual(ge.main(argv), 3)

    def test_validate_selection_requires_a_select_for_pytest_targets_only(self):
        pytest_target = REAL_CONFIG["profiles"]["pentacle"]["targets"]["v2-pytest"]
        with self.assertRaises(ge.SetupFailure):
            ge.validate_selection(pytest_target, [], self.tmp)
        npm = REAL_CONFIG["profiles"]["pentacle"]["targets"]["npm-test"]
        self.assertEqual(ge.validate_selection(npm, [], self.tmp), npm["default_patterns"])

    def test_retry_after_setup_failure_keeps_original_attempt(self):
        self.run_main("--target", "nope", "--spec", "s", "--series", "x", "--purpose", "acceptance")
        first = self.attempts()[0]["attempt_id"]
        self.run_main("--target", "nope", "--spec", "s", "--series", "x", "--purpose", "acceptance", "--prior-attempt", first)
        attempts = self.attempts()
        self.assertEqual(len(attempts), 2)
        self.assertEqual({a["history"] for a in attempts}, {"first_observed", "continues"})
        self.assertEqual({a["primary_outcome"] for a in attempts}, {"setup_failure"})


class PythonChromePolicy(unittest.TestCase):
    def test_selenium_launch_retains_executable_headless_and_keychain_flags(self):
        import ast
        source = (HERE.parent / "test/e2e/report_selection_copy_gate.py").read_text()
        tree = ast.parse(source)
        flags = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.For) or not isinstance(node.iter, (ast.Tuple, ast.List)):
                continue
            if not any(isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                       and call.func.attr == "add_argument" and call.args
                       and isinstance(call.args[0], ast.Name) and isinstance(node.target, ast.Name)
                       and call.args[0].id == node.target.id for statement in node.body for call in ast.walk(statement)):
                continue
            flags += [e.value for e in node.iter.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        self.assertIn("--headless=new", flags)
        self.assertIn("--use-mock-keychain", flags)


class MigratedTargets(Tmp):
    def test_ci_merge_keeps_existing_distinct_tier_limits_and_deadline(self):
        cfg = ge.load_config()
        profile = cfg["profiles"]["pentacle"]
        target = profile["targets"]["v2-merge-ci"]
        env = ge.build_env(cfg, profile, target, checkout=HERE.parent, toolchain={}, venv=self.tmp / "v",
                           home=self.tmp / "h", tmp=self.tmp / "t", locale_name="C.UTF-8", evidence=self.tmp / "ev",
                           attempt_id="a", pass_env={}, ambient={"V2_UNIT_TIMEOUT_S":"1"})
        self.assertEqual((env["V2_UNIT_TIMEOUT_S"],env["V2_SMOKE_TIMEOUT_S"]),("1200","900"))
        cmd, result = ge.build_command(cfg,target,self.tmp / "v",HERE.parent,HERE.parent / profile["service_dir"],
                                       self.tmp / "ev",self.tmp / "t/b",[],2100,env)
        self.assertNotIn("--timeout-seconds",cmd)  # runner reads preserved per-tier limits
        self.assertIn("merge",cmd)
        self.assertGreater(float(cmd[cmd.index("--deadline-seconds")+1]),2100+60+30)
        self.assertGreater(result["wall_s"],2100)

    def test_cli_and_shared_targets_use_their_own_roots_without_foreign_plugin(self):
        cfg = ge.load_config(); profile = cfg["profiles"]["pentacle"]
        for name, folder in (("cli-pytest","services/agent-orch"),("shared-pytest","services/_shared")):
            target = profile["targets"][name]
            selected = ge.validate_selection(target,["tests"],HERE.parent)
            cmd, result = ge.build_command(cfg,target,self.tmp / "v",HERE.parent,HERE.parent / profile["service_dir"],
                                           self.tmp / name,self.tmp / "t/b",selected,900,{})
            self.assertEqual(result["cwd"],HERE.parent / folder)
            self.assertNotIn("--strict-capabilities",cmd)
            with self.assertRaises(ge.SetupFailure):ge.validate_selection(target,[],HERE.parent)


if __name__ == "__main__":
    unittest.main(verbosity=1)
