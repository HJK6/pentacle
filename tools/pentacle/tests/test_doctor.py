"""Diagnostic dependency and privacy contracts, including hostile subprocess text."""
import argparse
import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pentacle import discovery as d
from pentacle.cli import doctor_text, parser, version_text
from pentacle.doctor import CHECKS, Snapshot, evaluate, private_config, transactions
from pentacle.install import atomic_json, identity, release_manifest


class DoctorTest(unittest.TestCase):
    def facts(self):
        return {**{key: "ok" for key, *_ in CHECKS}, "transactions": []}

    def test_every_check_failure_and_dependency_skip(self):
        for failed, *_ in CHECKS:
            if failed == "install.transaction":
                continue
            with self.subTest(check=failed):
                facts = self.facts()
                facts[failed] = "fail"
                actual = {c["id"]: c for c in evaluate(facts, live=True)}
                self.assertEqual(actual[failed]["status"], "fail")
                for check_id, dependencies, *_ in CHECKS:
                    blocked = [key for key in dependencies if actual[key]["status"] != "ok"]
                    self.assertEqual(actual[check_id]["blocked_by"], blocked)
                    if blocked:
                        self.assertEqual(actual[check_id]["status"], "skip")

    def test_transaction_exception_is_only_named_served_and_no_other_open(self):
        cases = [([], None, "ok"), ([{"id": "one", "state": "served"}], None, "fail"),
                 ([{"id": "one", "state": "served"}], "one", "ok"),
                 ([{"id": "one", "state": "started"}], "one", "fail"),
                 ([{"id": "one", "state": "served"}, {"id": "two", "state": "planned"}], "one", "fail")]
        for open_records, chosen, expected in cases:
            with self.subTest(records=open_records, chosen=chosen):
                facts = self.facts()
                facts["transactions"] = open_records
                checks = {c["id"]: c for c in evaluate(facts, for_transaction=chosen)}
                self.assertEqual(checks["install.transaction"]["status"], expected)

    def test_live_is_opt_in(self):
        facts = self.facts()
        facts["live.reply"] = "fail"
        self.assertEqual(evaluate(facts)[-1]["status"], "skip")
        self.assertEqual(evaluate(facts, live=True)[-1]["status"], "fail")

    def test_raw_fixture_fields_cannot_reach_either_output_form(self):
        markers = ["TOKEN_SEED", "UNKNOWN_KEY_SEED", "CONFIG_VALUE_SEED", "THROWN_SEED",
                   "PROVIDER_STDOUT_SEED", "PROVIDER_STDERR_SEED", "URL_QUERY_SEED", "OUTSIDE_DIRECTORY_SEED"]
        facts = self.facts()
        facts["raw"] = markers
        for check_id, *_ in CHECKS:
            facts[check_id] = "fail"
        value = {"schema_version": 1, "identity": {"schema_version": 1,
                 "install": {"commit": None, "version": None, "release_remote": "custom"},
                 "daemon": {"commit": None}, "web": {"build_commit": None, "build_id": None},
                 "latest": {"commit": None, "version": None}, "manifest": {"source": "none", "fetched_at": None}, "behind_releases": None},
                 "checks": evaluate(facts, live=True), "summary": {"ok": 0, "warn": 0, "fail": 0, "skip": 0}}
        with contextlib.redirect_stdout(io.StringIO()) as output:
            doctor_text(value)
        combined = output.getvalue() + json.dumps(value)
        for marker in [*markers, socket.gethostname(), str(Path.home())]:
            self.assertNotIn(marker, combined)

    def test_config_loader_stdout_stderr_and_exception_are_contained(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            # Use the real project config loader with an actual throwing config.
            source_root = Path(__file__).resolve().parents[3]
            config = root / "private.js"
            config.write_text("console.log('CONFIG_VALUE_SEED'); console.error('STDERR_SEED'); throw Error('THROWN_SEED');")
            with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
                value = d.config(source_root, config)
            self.assertIsNone(value)
            self.assertEqual(out.getvalue() + err.getvalue(), "")
            config.write_text("module.exports={UNKNOWN_KEY_SEED:'CONFIG_VALUE_SEED', chatStream:{url:'ws://127.0.0.1:1/?URL_QUERY_SEED',tokenPath:'TOKEN_PATH_SEED'}}")
            value = d.config(source_root, config)
            self.assertEqual(value["warnings"], ["unknown-top-level"])
            self.assertNotIn("UNKNOWN_KEY_SEED", json.dumps(value))
            self.assertNotIn("CONFIG_VALUE_SEED", json.dumps(value))

    def test_provider_stdout_and_stderr_are_not_findings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            provider = root / "provider"
            provider.write_text("#!/bin/sh\necho PROVIDER_STDOUT_SEED\necho PROVIDER_STDERR_SEED >&2\nexit 1\n")
            provider.chmod(0o700)
            snapshot = object.__new__(Snapshot)
            snapshot.daemon_url = "ws://127.0.0.1:1/?URL_QUERY_SEED"
            snapshot.daemon_process = d.Process(os.getpid(), [sys.executable, "--claude-bin", str(provider)], root, dict(os.environ))
            self.assertEqual(snapshot.provider_status(), "warn")
            provider.unlink()
            self.assertEqual(snapshot.provider_status(), "fail")

    def test_private_permissions_and_unreadable_transaction(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config_dir = root / "config"
            config_dir.mkdir(mode=0o700)
            token = config_dir / "token"
            token.write_text("TOKEN_SEED")
            token.chmod(0o600)
            self.assertTrue(private_config(config_dir / "config.js", token))
            token.chmod(0o644)
            self.assertFalse(private_config(config_dir / "config.js", token))
            record = root / ".pentacle-deploy/update/open/transaction.json"
            atomic_json(record, {"id": "open", "state": "planned"})
            self.assertEqual(record.stat().st_mode & 0o777, 0o600)
            self.assertEqual(transactions(root), [{"id": "open", "state": "planned"}])
            record.write_text("corrupt")
            self.assertEqual(transactions(root), [{}])

    def test_loopback_is_literal_and_remote_discovery_is_skipped(self):
        for value in ("ws://127.0.0.1:1", "ws://[::1]:1", "ws://localhost:1"):
            self.assertTrue(d.loopback(value))
        for value in ("ws://example.invalid:1", "ws://192.0.2.1:1", "broken"):
            self.assertFalse(d.loopback(value))
            self.assertIsNone(d.listener(value))

    def test_common_options_work_before_and_after_command(self):
        for argv in (["--json", "--install", "/tmp/install", "doctor"],
                     ["doctor", "--json", "--install", "/tmp/install"]):
            parsed = parser().parse_args(argv)
            self.assertTrue(parsed.json)
            self.assertEqual(parsed.install, "/tmp/install")

    def test_only_explicit_release_remote_is_fetched_and_missing_cache_is_truthful(self):
        with tempfile.TemporaryDirectory() as folder:
            for remote in ("https://github.com/HJK6/pentacle.git", "https://example.invalid/fork.git"):
                calls = []
                def command(argv, **kwargs):
                    calls.append(argv)
                    return __import__("subprocess").CompletedProcess(argv, 1, "", "SECRET_STDERR")
                with patch("pentacle.install.run", side_effect=command):
                    result = release_manifest(Path(folder), remote)
                self.assertEqual(calls, [["git", "-C", Path(folder), "fetch", remote, "main"]])
                self.assertEqual(result["source"], "none")
                self.assertIsNone(result["data"])
                self.assertEqual(result["release_remote"], "official" if "github.com" in remote else "custom")


if __name__ == "__main__":
    unittest.main()
