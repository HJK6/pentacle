"""Admission: exercise B05's AST selector checks with source-level controls.

Journey: a candidate protected selector reaches the same scope comparison used
by the daemon contract CLI. Controls cover line-only drift, moving a rule,
duplicating an access, changing an aliased SQL statement, and adding a routing
branch. Fixtures are synthetic and use only the standard library.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "daemon_layer_gate.py"
SPEC = importlib.util.spec_from_file_location("daemon_layer_gate", MODULE_PATH)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gate
SPEC.loader.exec_module(gate)


def _scopes(path: str, source: str, symbol: str) -> dict[tuple[str, str], ast.AST]:
    tree = ast.parse(source)
    node = gate._scope_nodes(tree).get(symbol)
    return {(path, symbol): node} if node is not None else {}


class DaemonLayerGateTests(unittest.TestCase):
    def _errors(self, before: str, after: str, *, symbol: str = "Router.send",
                line: int = 3) -> list[str]:
        path = "services/chat-stream-v2/router.py"
        anchors = [gate.Anchor(path, line, symbol)]
        return gate._compare_anchors("F6_delivery", anchors,
                                     _scopes(path, before, symbol),
                                     _scopes(path, after, symbol))

    def test_line_only_drift_preserves_ast_identity(self) -> None:
        before = "class Router:\n    def send(self, value):\n        self.deliver(value)\n"
        after = "# moved by comments only\n\n" + before
        self.assertEqual(self._errors(before, after), [])

    def test_moved_rule_is_rejected(self) -> None:
        before = "class Router:\n    def send(self, value):\n        self.deliver(value)\n"
        after = "class Router:\n    def send(self, value):\n        pass\n    def other(self, value):\n        self.deliver(value)\n"
        self.assertTrue(any("selector count changed" in error for error in self._errors(before, after)))

    def test_duplicate_access_inside_old_symbol_is_rejected(self) -> None:
        before = "class Router:\n    def send(self, value):\n        self.deliver(value)\n"
        after = "class Router:\n    def send(self, value):\n        self.deliver(value)\n        self.deliver(value)\n"
        self.assertTrue(self._errors(before, after))

    def test_aliased_duplicate_policy_call_is_rejected(self) -> None:
        before = ("class Router:\n    def send(self, value):\n"
                  "        self.policy.allow(value)\n")
        after = ("class Router:\n    def send(self, value):\n"
                 "        self.policy.allow(value)\n"
                 "        decision = self.policy.allow\n"
                 "        decision(value)\n")
        self.assertTrue(self._errors(before, after))

    def test_aliased_sql_change_is_rejected(self) -> None:
        before = ('class Router:\n    def send(self, conn):\n'
                  '        conn.execute("UPDATE v2_tell_deliveries SET state=?")\n')
        after = ('class Router:\n    def send(self, conn):\n'
                 '        sql = "UPDATE v2_tell_deliveries SET state=?"\n'
                 '        conn.execute(sql)\n')
        self.assertTrue(self._errors(before, after, line=3))

    def test_new_aliased_delivery_sql_is_rejected(self) -> None:
        before = "def send(conn):\n    return None\n"
        after = ('def send(conn):\n'
                 '    statement = "UPDATE v2_tell_deliveries SET state=?"\n'
                 '    conn.execute(statement)\n')
        additions = gate._added_delivery_sql(before, after, "services/chat-stream-v2/example.py")
        self.assertEqual(len(additions), 1)
        self.assertEqual(additions[0][0], "send")

    def test_aliased_observation_reader_and_publisher_are_detected(self) -> None:
        source = ("from hosts import Hosts as H\n"
                  "def inspect(hosts):\n"
                  "    read = H.is_online\n"
                  "    publish = hosts.apply_live\n"
                  "    return read(hosts), publish()\n")
        accesses = gate._observation_accesses(source)
        self.assertTrue(any(item[1:] == ("is_online", "hosts.Hosts.is_online") for item in accesses))
        self.assertTrue(any(item[1:] == ("apply_live", "hosts.apply_live") for item in accesses))

    def test_new_routing_branch_is_rejected(self) -> None:
        before = "class Router:\n    def send(self, value):\n        self.deliver(value)\n"
        after = ("class Router:\n    def send(self, value):\n"
                 "        self.deliver(value)\n"
                 "        if front_desk_requested:\n            self.route_to_front_desk(value)\n")
        self.assertTrue(any("new routing branch" in error for error in self._errors(before, after)))

    def test_new_routing_branch_in_new_symbol_is_rejected(self) -> None:
        before = "def handle(value):\n    return value\n"
        after = ("def handle(value):\n    return value\n\n"
                 "def route(value):\n    if front_desk_requested:\n        deliver(value)\n")
        self.assertTrue(gate._added_routing_branches(before, after, "service.py"))

    def test_new_routing_branch_in_new_symbol_is_rejected(self) -> None:
        before = "def handle(value):\n    return value\n"
        after = ("def handle(value):\n    return value\n\n"
                 "def route(value):\n    if front_desk_requested:\n        deliver(value)\n")
        self.assertTrue(gate._added_routing_branches(before, after, "service.py"))


if __name__ == "__main__":
    unittest.main()
