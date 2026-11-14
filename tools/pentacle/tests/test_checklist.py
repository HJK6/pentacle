"""Checklist refusal, exact invocation, private state and immutable SQLite copies."""
import argparse
import contextlib
from contextlib import closing
import io
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pentacle import discovery as d, stores, update as u
from pentacle.cli import main
from pentacle.doctor import Snapshot


class ChecklistTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="checklist space '")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.git("init", "-q")
        self.git("config", "user.name", "Example Agent")
        self.git("config", "user.email", "agent@example.invalid")
        (self.root / "tracked").write_text("first")
        (self.root / "CHANGELOG.md").write_text("# Changes\n\n## 2026-11-01\n\n- Fixture release notes.\n")
        self.git("add", "tracked", "CHANGELOG.md"); self.git("commit", "-qm", "First")
        self.previous = self.git("rev-parse", "HEAD")
        (self.root / "tracked").write_text("second")
        self.git("commit", "-qam", "Second")
        self.middle = self.git("rev-parse", "HEAD")
        (self.root / "tracked").write_text("third")
        self.git("commit", "-qam", "Third")
        self.target = self.git("rev-parse", "HEAD")
        self.git("checkout", "--detach", self.previous)
        self.helper = self.root / "helper"; self.helper.mkdir()
        (self.helper / "COMMIT").write_text(self.target)
        self.fake_module = self.helper / "tools/pentacle/update.py"
        self.entries = [self.entry(self.middle, "2.0.0", ["first action"]), self.entry(self.target, "2.1.0", ["target action"])]
        self.manifest = {"data": {"releases": self.entries}, "source": "fetched", "release_remote": "custom", "fetched_at": "2026-01-01T00:00:00+00:00"}
        self.proc = d.Process(123456789, [sys.executable, str(self.root / "services/chat-stream-v2/main.py")], self.root,
                              {"PATH": "/usr/bin:/bin", "HOME": str(self.root), "PENTACLE_CONFIG": "quote ' and space",
                               "EXTRA": "value ' spaced", "OMITTED_SECRET": "DO_NOT_WRITE", "__PYVENV_LAUNCHER__": sys.executable}, "old")
        self.web = d.Process(123456788, ["/usr/bin/node", str(self.root / "server")], self.root, dict(self.proc.env), "old")
        self.snapshot = argparse.Namespace(daemon_process=self.proc, web_process=self.web, daemon={}, web={},
                                          stores=[self.root / "data store.db"], python=sys.executable,
                                          config_path=self.root / "config with ' quote.js", daemon_url="ws://127.0.0.1:18791",
                                          web_url="http://127.0.0.1:18795")
        self.args = argparse.Namespace(install=str(self.root), release_remote="local repo", config=str(self.snapshot.config_path),
                                       daemon_url=self.snapshot.daemon_url, web_url=self.snapshot.web_url,
                                       check=False, prepare=True, backup=False, ack_required_actions=True, json=False)
        self.addCleanup(patch.stopall)
        patch.object(u, "__file__", str(self.fake_module)).start()
        patch.object(u, "release_manifest", return_value=self.manifest).start()
        patch.object(u, "Snapshot", return_value=self.snapshot).start()
        patch.object(d, "process", side_effect=lambda pid: self.proc if pid == self.proc.pid else self.web).start()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True, text=True).stdout.strip()

    def entry(self, commit, version, actions):
        return {"commit": commit, "version": version, "tested_update_from": [self.previous], "required_actions": actions, "changelog_sections": ["2026-11-01"]}

    def test_release_check_reports_two_behind_latest_divergent_and_direct_testing(self):
        self.args.check, self.args.prepare, self.args.json = True, False, True
        code, out, err = self.run_update()
        self.assertEqual(code, 0, err)
        value = json.loads(out)
        self.assertEqual(value["behind_releases"], 2)
        self.assertTrue(value["tested_start"])
        self.assertEqual(value["install"]["release_remote"], "custom")
        # A tested intermediate hop does not establish a directly tested start.
        self.entries[-1]["tested_update_from"] = ["2.0.0"]
        self.assertFalse(json.loads(self.run_update()[1])["tested_start"])
        self.git("checkout", "--detach", self.target)
        value = json.loads(self.run_update()[1])
        self.assertEqual(value["install"]["version"], "2.1.0")
        self.assertEqual(value["behind_releases"], 0)
        self.git("checkout", "--detach", self.previous)
        (self.root / "tracked").write_text("divergent")
        self.git("commit", "-qam", "Divergent")
        value = json.loads(self.run_update()[1])
        self.assertIsNone(value["behind_releases"])
        self.assertFalse(value["tested_start"])
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no"), "")

    def test_release_check_prints_notes_all_actions_and_cache_age(self):
        self.args.check, self.args.prepare = True, False
        self.manifest.update(source="cache", fetched_at="2020-01-01T00:00:00+00:00")
        code, out, err = self.run_update()
        self.assertEqual(code, 0, err)
        for value in ("Fixture release notes.", "first action", "target action", "Cached manifest age:", "online release status unknown"):
            self.assertIn(value, out)
        self.assertNotIn("up to date", out)
        self.manifest["data"] = None
        self.assertEqual(self.run_update()[0], 2)

    def run_update(self):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = u.update(self.args)
        return code, out.getvalue(), err.getvalue()

    def stale_pointer(self):
        deploy = self.root / ".pentacle-deploy"; deploy.mkdir(exist_ok=True)
        pointer = deploy / "upgrade-current"; pointer.write_text("old plan")
        return pointer

    def assert_refused(self, contains):
        pointer = self.stale_pointer()
        code, out, err = self.run_update()
        self.assertEqual(code, 2, out + err)
        self.assertIn(contains, err)
        self.assertFalse(pointer.exists())
        self.assertFalse(list((self.root / ".pentacle-deploy").glob("upgrade-*/plan.json")))

    def test_refuses_modified_tracked_file(self):
        (self.root / "tracked").write_text("dirty")
        self.assert_refused("Tracked files")

    def test_refuses_wrong_helper(self):
        (self.helper / "COMMIT").write_text(self.previous)
        self.assert_refused("helper does not match")

    def test_refuses_non_ancestor(self):
        (self.root / "tracked").write_text("divergent")
        self.git("commit", "-qam", "Divergent")
        self.assert_refused("not an ancestor")

    def test_required_actions_include_skipped_and_target_release(self):
        self.args.ack_required_actions = False
        self.stale_pointer()
        code, out, err = self.run_update()
        self.assertEqual(code, 2)
        self.assertIn("first action", err); self.assertIn("target action", err)
        self.assertFalse((self.root / ".pentacle-deploy/upgrade-current").exists())

    def test_both_processes_must_be_running(self):
        for name in ("daemon_process", "web_process"):
            with self.subTest(name=name), patch.object(self.snapshot, name, None):
                self.assert_refused("both be running")

    def test_no_mode_writes_or_fetches_nothing(self):
        self.args.prepare = False
        with patch.object(u, "selection", side_effect=AssertionError("must not fetch")):
            code, out, err = self.run_update()
        self.assertEqual(code, 2); self.assertIn("UPGRADE.md", err)
        self.assertFalse((self.root / ".pentacle-deploy").exists())

    def test_plan_modes_quoting_allowlist_and_fresh_shell(self):
        # Execute the generated launcher from an empty environment. The process
        # fixture still has the daemon's identifying script argument.
        script = self.root / "services/chat-stream-v2/main.py"
        script.parent.mkdir(parents=True)
        script.write_text("import json,os;print(json.dumps(dict(os.environ)))")
        with patch.dict(os.environ, {"PENTACLE_KEEP_ENV": "EXTRA"}):
            code, out, err = self.run_update()
        self.assertEqual(code, 0, err)
        directory, plan = stores.load_plan(self.root)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        for name in ("plan.json", "plan.env"):
            self.assertEqual((directory / name).stat().st_mode & 0o777, 0o600)
        for name in ("start-daemon.sh", "start-web.sh", "restore-stores.sh"):
            self.assertEqual((directory / name).stat().st_mode & 0o777, 0o700)
        self.assertEqual(plan["stores"], [str(self.root / "data store.db")])
        self.assertEqual(plan["required_actions"], ["first action", "target action"])
        self.assertTrue(plan["tested_start"])
        for file in directory.iterdir():
            self.assertNotIn("DO_NOT_WRITE", file.read_text())
            self.assertNotIn("OMITTED_SECRET", file.read_text())
        result = subprocess.run([str(directory / "start-daemon.sh")], env={}, capture_output=True, text=True, check=True)
        env = json.loads(result.stdout)
        self.assertEqual(env["EXTRA"], "value ' spaced")
        self.assertEqual(env["PENTACLE_CONFIG"], "quote ' and space")
        self.assertNotIn("OMITTED_SECRET", env)
        command = ". " + shlex.quote(str(directory / "plan.env")) + '; printf "%s\\n" "$plan" "$previous" "$target" "$py" "$PENTACLE_CONFIG" "$PENTACLE_DAEMON_URL" "$PENTACLE_WEB_URL" "$PENTACLE_RELEASE_REMOTE"'
        result = subprocess.run(["bash", "-c", command], env={}, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.splitlines(), [str(directory), self.previous, self.target, sys.executable,
                         str(self.snapshot.config_path), self.snapshot.daemon_url, self.snapshot.web_url, "local repo"])

    def test_command_environment_and_cli_precedence(self):
        with patch.dict(os.environ, {"PENTACLE_CONFIG": "environment-config", "PENTACLE_DAEMON_URL": "ws://env", "PENTACLE_WEB_URL": "http://env", "PENTACLE_RELEASE_REMOTE": "environment-remote"}):
            with patch.object(u, "update", return_value=0) as called:
                self.assertEqual(main(["update", "--check", "--web-url", "http://cli"]), 0)
                args = called.call_args.args[0]
                self.assertEqual(args.config, "environment-config")
                self.assertEqual(args.daemon_url, "ws://env")
                self.assertEqual(args.web_url, "http://cli")
                self.assertEqual(args.release_remote, "environment-remote")


class StoreTest(unittest.TestCase):
    def test_backup_integrity_first_copy_preservation_and_explicit_restore(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); directory = root / "plan"; directory.mkdir()
            databases = [root / (name + ".db") for name in ("sessions", "notifications", "assets")]
            for path in databases:
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute("CREATE TABLE example(value TEXT)")
                    db.execute("INSERT INTO example VALUES ('first')")
            proc = {"pid": 123456789, "start": "old", "argv": ["python", "main.py"], "cwd": str(root)}
            plan = {"schema_version": 1, "stores": list(map(str, databases)), "daemon": proc, "web": proc}
            (directory / "plan.json").write_text(json.dumps(plan))
            (directory / "store-0.db.tmp").write_text("interrupted copy")
            with patch.object(stores, "assert_stopped"):
                stores.backup(directory, plan)
                copies = [(directory / f"store-{i}.db").read_bytes() for i in range(3)]
                for path in databases:
                    with closing(sqlite3.connect(path)) as db, db:
                        db.execute("INSERT INTO example VALUES ('later')")
                stores.backup(directory, plan)
                self.assertEqual(copies, [(directory / f"store-{i}.db").read_bytes() for i in range(3)])
                for path in databases:
                    for suffix in ("-wal", "-shm"):
                        Path(str(path) + suffix).write_text("stopped sidecar")
                stores.restore(directory)
                self.assertEqual([path.read_bytes() for path in databases], copies)
                for path in databases:
                    self.assertFalse(Path(str(path) + "-wal").exists())
                    self.assertFalse(Path(str(path) + "-shm").exists())
                self.assertFalse((directory / "store-0.db.tmp").exists())
                (directory / "store-1.db").write_text("corrupt")
                with self.assertRaises(stores.Refused):
                    stores.backup(directory, plan)

    def test_backup_and_restore_refuse_actual_running_process(self):
        proc = {"pid": os.getpid(), "start": "present", "argv": [sys.executable], "cwd": os.getcwd()}
        plan = {"schema_version": 1, "stores": ["/absent/example.db"], "daemon": proc, "web": proc}
        with tempfile.TemporaryDirectory() as folder, patch.object(d, "process", return_value=d.Process(os.getpid(), [sys.executable], Path.cwd(), start="present")):
            root = Path(folder)
            (root / "plan.json").write_text(json.dumps(plan))
            with self.assertRaises(stores.Refused):
                stores.backup(root, plan)
            with self.assertRaises(stores.Refused):
                stores.restore(root)
            self.assertFalse((root / "backup-complete").exists())


if __name__ == "__main__":
    unittest.main()
