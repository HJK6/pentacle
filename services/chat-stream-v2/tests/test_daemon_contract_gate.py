from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "daemon_contract_gate.py"
SPEC = importlib.util.spec_from_file_location("daemon_contract_gate", MODULE_PATH)
assert SPEC and SPEC.loader
daemon_gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(daemon_gate)


class DaemonContractGateTests(unittest.TestCase):
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
