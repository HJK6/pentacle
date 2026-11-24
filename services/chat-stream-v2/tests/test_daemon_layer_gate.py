"""Admission: exercise B05's AST selector checks with source-level controls.

Journey: a candidate protected selector reaches the same scope comparison used
by the daemon contract CLI. Controls cover line-only drift, moving a rule,
duplicating an access, changing an aliased SQL statement, and adding a routing
branch. Fixtures are synthetic and use only the standard library.
"""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "daemon_layer_gate.py"
sys.path.insert(0, str(MODULE_PATH.parent))
try:
    import daemon_layer_gate as gate
finally:
    sys.path.pop(0)


def _scopes(path: str, source: str, symbol: str) -> dict[tuple[str, str], ast.AST]:
    tree = ast.parse(source)
    node = gate._scope_nodes(tree).get(symbol)
    return {(path, symbol): node} if node is not None else {}


class DaemonLayerGateTests(unittest.TestCase):
    def _errors(self, before: str, after: str, *, symbol: str = "Router.send",
                line: int = 3) -> list[str]:
        path = "services/chat-stream-v2/router.py"
        anchors = [gate.Anchor(path, line, symbol)]
        frozen = _scopes(path, before, symbol)
        return gate._compare_anchors("F6_delivery", anchors,
                                     frozen, frozen,
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

    def test_delivery_write_uses_its_own_sql_binding(self) -> None:
        template = "def send(conn):\n    sql = {target!r}\n    conn.execute(sql)\n"
        sibling = "def other():\n    sql = {other!r}\n"
        write, read = "UPDATE v2_tell_deliveries SET state=?", "SELECT 1"
        for target, other, expected in [(write, read, [("send", write.lower())]), (read, write, [])]:
            source = template.format(target=target)
            for suffix in ("", sibling.format(other=other)):
                with self.subTest(target=target, suffix=suffix):
                    self.assertEqual(gate._delivery_sql_mutations(source + suffix), expected)

    def test_delivery_sql_closure_and_parameter_shadowing(self) -> None:
        source = ("def outer():\n"
                  "    sql = 'UPDATE v2_tell_deliveries SET state=?'\n"
                  "    def send(conn): conn.execute(sql)\n"
                  "    def other(conn, sql): conn.execute(sql)\n")
        self.assertEqual(gate._delivery_sql_mutations(source),
                         [("outer.send", "update v2_tell_deliveries set state=?")])

    def test_aliased_observation_reader_and_publisher_are_detected(self) -> None:
        source = ("from hosts import Hosts as H\n"
                  "def inspect(hosts):\n"
                  "    read = H.is_online\n"
                  "    publish = hosts.apply_live\n"
                  "    return read(hosts), publish()\n")
        accesses = gate._observation_accesses(source)
        self.assertTrue(any(item[1:] == ("is_online", "hosts.Hosts.is_online") for item in accesses))
        self.assertTrue(any(item[1:] == ("apply_live", "hosts.apply_live") for item in accesses))

    def test_duplicate_observation_reader_fails_with_clean_control(self) -> None:
        base = ("from hosts import Hosts\n"
                "def inspect(host):\n    return Hosts.is_online(host)\n")
        duplicate = ("from hosts import Hosts\n"
                     "def inspect(host):\n"
                     "    result = Hosts.is_online(host)\n"
                     "    return result, Hosts.is_online(host)\n")
        clean = ("from hosts import Hosts\n"
                 "def inspect(host):\n    return Hosts.is_online(host)\n")
        self.assertTrue(gate._added_observation_accesses(base, base, duplicate, "observers.py"))
        self.assertEqual(gate._added_observation_accesses(base, base, clean, "observers.py"), [])
        field_read = ("def inspect(host):\n"
                      "    age = host.snapshot['pane_status']\n"
                      "    return age\n")
        self.assertTrue(gate._added_observation_accesses("", "", field_read, "observers.py"))

    def test_unanchored_optional_policy_decision_fails_with_clean_control(self) -> None:
        base = "def ordinary(value):\n    return value\n"
        candidate = ("def ordinary(value):\n    return value\n\n"
                     "def new_workflow(context):\n    return optional_policy.check(context)\n")
        self.assertTrue(gate._added_policy_decisions(base, base, candidate, "workflow.py"))
        aliased = ("def ordinary(value):\n    return value\n\n"
                   "def new_workflow(context):\n"
                   "    check = optional_policy.check\n    return check(context)\n")
        self.assertTrue(gate._added_policy_decisions(base, base, aliased, "workflow.py"))
        self.assertEqual(gate._added_policy_decisions(base, base, base, "workflow.py"), [])

    def test_retired_selector_reintroduction_uses_merge_base(self) -> None:
        before = ast.parse("def decide(self):\n    self.policy.check()\n").body[0]
        removed = ast.parse("def decide(self):\n    return None\n").body[0]
        reintroduced = ast.parse("def decide(self):\n    self.policy.check()\n").body[0]
        selector = gate._fingerprint(gate._anchor_statement(before, 2))
        self.assertIsNone(gate._retired_selector_status(selector, removed, removed))
        self.assertEqual(gate._retired_selector_status(selector, removed, reintroduced),
                         "reintroduced since merge base")
        self.assertEqual(gate._retired_selector_status(selector, before, reintroduced),
                         "remains after exception removal")

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

if __name__ == "__main__":
    unittest.main()
