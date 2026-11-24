"""Admission: exercise the daemon contract CLI boundary, not its helper only.

Journey: a candidate Store startup source or active exception list reaches the
same CLI used by the local merge runner and protected CI. Boundary: every
persistent startup addition and every moved/added allowance is refused, while
the frozen baseline and exception removals pass. Supported runners are the
repository's Python 3.13 unit lane and Linux Python 3.13 contracts job.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "daemon_contract_gate.py"
SPEC = importlib.util.spec_from_file_location("daemon_contract_gate", MODULE_PATH)
assert SPEC and SPEC.loader
daemon_gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(daemon_gate)


class DaemonContractGateTests(unittest.TestCase):
    def _record_result(self, name: str, base: str, result: subprocess.CompletedProcess[str]) -> None:
        directory = os.environ.get("DAEMON_CONTRACT_EVIDENCE_DIR")
        if not directory:
            return
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        (path / f"{name}.json").write_text(json.dumps({
            "control": name,
            "base": base,
            "command": result.args,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    def test_frozen_contract_and_inventory_configuration_are_valid(self) -> None:
        self.assertEqual(daemon_gate._validate_contract(), [])

    def test_new_startup_write_is_detected(self) -> None:
        base = '''\
class Store:
    def __init__(self):
        pass
    def _run(self):
        conn.execute("SELECT 1")
'''
        candidate = '''\
class Store:
    def __init__(self):
        pass
    def _run(self):
        conn.execute("SELECT 1")
        conn.execute("CREATE TABLE added(id INTEGER)")
'''
        self.assertEqual(daemon_gate._startup_writes(base), {})
        self.assertEqual(len(daemon_gate._startup_writes(candidate)), 1)

    def test_moved_startup_write_changes_its_identity(self) -> None:
        before = '''\
class Store:
    def __init__(self):
        pass
    def _run(self):
        conn.execute("CREATE TABLE old(id INTEGER)")
'''
        after = '''\
class Store:
    def __init__(self):
        pass
    def _run(self):
        marker = 1
        conn.execute("CREATE TABLE old(id INTEGER)")
'''
        self.assertNotEqual(set(daemon_gate._startup_writes(before)), set(daemon_gate._startup_writes(after)))

    def test_unresolved_startup_sql_is_not_ignored(self) -> None:
        source = '''\
class Store:
    def __init__(self):
        pass
    def _run(self):
        conn.execute(sql)
'''
        self.assertEqual(len(daemon_gate._startup_writes(source)), 1)

    def test_same_line_duplicate_startup_statement_is_detected(self) -> None:
        before = '''\
class Store:
    def __init__(self):
        pass
    def _run(self):
        conn.executescript("CREATE TABLE item(id INTEGER);")
'''
        after = '''\
class Store:
    def __init__(self):
        pass
    def _run(self):
        conn.executescript("CREATE TABLE item(id INTEGER); CREATE TABLE item(id INTEGER);")
'''
        self.assertTrue(daemon_gate._startup_write_additions(before, after))

    def _fixture(self, store_source: str | None = None) -> tuple[tempfile.TemporaryDirectory, Path, Path, str]:
        temporary = tempfile.TemporaryDirectory(prefix="daemon-contract-gate-")
        root = Path(temporary.name)
        service = root / "services" / "chat-stream-v2"
        tools = service / "tools"
        tools.mkdir(parents=True)
        shutil.copy2(daemon_gate.__file__, tools / "daemon_contract_gate.py")
        shutil.copytree(Path(daemon_gate.__file__).resolve().parents[1] / "daemon_contract",
                        service / "daemon_contract")
        if store_source is None:
            store_source = "class Store:\n    def __init__(self):\n        pass\n    def _run(self):\n        pass\n"
        (service / "store.py").write_text(store_source, encoding="utf-8")
        for command in (
            ["git", "init", "-q"],
            ["git", "config", "user.name", "Contract Test"],
            ["git", "config", "user.email", "contract-test@example.invalid"],
            ["git", "add", "services"],
            ["git", "commit", "-qm", "fixture base"],
        ):
            subprocess.run(command, cwd=root, check=True, capture_output=True)
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                              text=True, capture_output=True).stdout.strip()
        return temporary, root, service, base

    def _run_fixture_gate(self, root: Path, service: Path, base: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(service / "tools" / "daemon_contract_gate.py"), "--base", base],
            cwd=root, text=True, capture_output=True, check=False,
        )

    def test_checker_cli_passes_frozen_baseline_control(self) -> None:
        temporary, root, service, base = self._fixture()
        try:
            result = self._run_fixture_gate(root, service, base)
            self._record_result("baseline-pass", base, result)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        finally:
            temporary.cleanup()

    def test_checker_cli_rejects_new_startup_write(self) -> None:
        temporary, root, service, base = self._fixture()
        try:
            store = service / "store.py"
            store.write_text(store.read_text() + "\n# candidate\n", encoding="utf-8")
            store.write_text(store.read_text().replace(
                "    def _run(self):\n        pass\n",
                '    def _run(self):\n        conn.execute("CREATE TABLE added(id INTEGER)")\n',
            ), encoding="utf-8")
            result = self._run_fixture_gate(root, service, base)
            self._record_result("new-startup-write-reject", base, result)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("adds or moves", result.stderr)
        finally:
            temporary.cleanup()

    def test_checker_cli_rejects_same_line_duplicate_startup_write(self) -> None:
        source = '''\
class Store:
    def __init__(self):
        pass
    def _run(self):
        conn.execute("CREATE TABLE item(id INTEGER)")
'''
        temporary, root, service, base = self._fixture(source)
        try:
            store = service / "store.py"
            store.write_text(store.read_text(encoding="utf-8").replace(
                'conn.execute("CREATE TABLE item(id INTEGER)")',
                'conn.execute("CREATE TABLE item(id INTEGER)"); conn.execute("CREATE TABLE item(id INTEGER)")',
            ), encoding="utf-8")
            result = self._run_fixture_gate(root, service, base)
            self._record_result("duplicate-same-line-write-reject", base, result)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("adds or moves", result.stderr)
        finally:
            temporary.cleanup()

    def test_checker_cli_allows_remove_only_exception_change(self) -> None:
        temporary, root, service, base = self._fixture()
        try:
            active = service / "daemon_contract" / "active" / "F2_projections.txt"
            active.write_text("\n".join(active.read_text(encoding="utf-8").splitlines()[1:]) + "\n",
                              encoding="utf-8")
            result = self._run_fixture_gate(root, service, base)
            self._record_result("remove-only-control-pass", base, result)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        finally:
            temporary.cleanup()

    def test_checker_cli_rejects_moved_and_added_allowances(self) -> None:
        temporary, root, service, base = self._fixture()
        try:
            active = service / "daemon_contract" / "active" / "F2_projections.txt"
            rows = active.read_text(encoding="utf-8").splitlines()
            match = re.fullmatch(r"(.+?):(\d+)(\s+.+)", rows[0])
            self.assertIsNotNone(match, rows[0])
            rows[0] = f"{match.group(1)}:{int(match.group(2)) + 1}{match.group(3)}"
            active.write_text("\n".join(rows) + "\n", encoding="utf-8")
            result = self._run_fixture_gate(root, service, base)
            self._record_result("moved-allowance-reject", base, result)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("adds or moves", result.stderr)
            moved_receipt = json.dumps({"control": "moved-allowance-reject", "stdout": result.stdout, "stderr": result.stderr}, sort_keys=True)

            shutil.rmtree(service / "daemon_contract")
            shutil.copytree(Path(daemon_gate.__file__).resolve().parents[1] / "daemon_contract",
                            service / "daemon_contract")
            active = service / "daemon_contract" / "active" / "F2_projections.txt"
            active.write_text(active.read_text(encoding="utf-8") + "new.py:999 Added.rule\n", encoding="utf-8")
            result = self._run_fixture_gate(root, service, base)
            self._record_result("base-list-addition-reject", base, result)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("adds entries beyond", result.stderr)
            added_receipt = json.dumps({"control": "base-list-addition-reject", "stdout": result.stdout, "stderr": result.stderr}, sort_keys=True)
            self.assertNotEqual(moved_receipt, added_receipt)
        finally:
            temporary.cleanup()

    def test_checker_cli_rejects_aliased_inventory_path_and_skipped_list_addition(self) -> None:
        temporary, root, service, base = self._fixture()
        try:
            manifest_path = service / "daemon_contract" / "inventory_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            f1 = manifest["inventories"]["F1_protected_core"]
            manifest["inventories"]["F2_projections"] = dict(f1)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            f2_active = service / "daemon_contract" / "active" / "F2_projections.txt"
            f2_active.write_text(f2_active.read_text(encoding="utf-8") + "hidden.py:999 New.rule\n",
                                 encoding="utf-8")
            result = self._run_fixture_gate(root, service, base)
            self._record_result("aliased-inventory-reject", base, result)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("canonical layer", result.stderr)
        finally:
            temporary.cleanup()

    def test_exception_inventory_addition_is_rejected(self) -> None:
        base = "store.py:10 C:CREATE_TABLE.sessions\n"
        candidate = base + "store.py:11 C:CREATE_TABLE.added\n"
        self.assertEqual(daemon_gate._exception_additions(base, candidate), ["store.py:11 C:CREATE_TABLE.added"])

    def test_exception_inventory_removal_is_allowed(self) -> None:
        base = "store.py:10 C:CREATE_TABLE.sessions\nstore.py:11 C:CREATE_TABLE.old\n"
        candidate = "store.py:10 C:CREATE_TABLE.sessions\n"
        self.assertEqual(daemon_gate._exception_additions(base, candidate), [])


if __name__ == "__main__":
    unittest.main()
