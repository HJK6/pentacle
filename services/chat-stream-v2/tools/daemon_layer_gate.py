#!/usr/bin/env python3
"""Ratchet the frozen policy, observation, and delivery exception scopes.

The sidecars identify the legacy symbols that still own these decisions. Their
source locations are review anchors; AST identity is used for comparison so
line-only movement does not create or erase an exception.
"""
from __future__ import annotations

import ast
import argparse
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

if __package__:
    from .daemon_contract_gate import sql_call_bindings
else:
    from daemon_contract_gate import sql_call_bindings

SERVICE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVICE_ROOT.parents[1]
BASELINE_COMMIT = "f153ca8adce2a38e0c5092b2dc98ff5aec16e06e"
SIDECARS = {
    "F3_policy": SERVICE_ROOT / "daemon_contract" / "active" / "F3_policy.txt",
    "F5_observation": SERVICE_ROOT / "daemon_contract" / "active" / "F5_observation.txt",
    "F6_delivery": SERVICE_ROOT / "daemon_contract" / "active" / "F6_delivery.txt",
}


@dataclass(frozen=True)
class Anchor:
    path: str
    line: int
    symbol: str


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=REPO_ROOT, text=True,
                            capture_output=True, check=False, timeout=20)
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()[:300]}")
    return result.stdout


def _repo_path(path: str) -> str:
    if path.startswith("services/chat-stream-v2/"):
        return path
    return "services/chat-stream-v2/" + path


def _parse_anchors(text: str) -> list[Anchor]:
    anchors = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            location, symbol = line.split(" ", 1)
            path, line_number = location.rsplit(":", 1)
            anchors.append(Anchor(_repo_path(path), int(line_number), symbol.strip()))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"invalid exception anchor on sidecar line {number}") from exc
    return anchors


def _scope_nodes(tree: ast.AST) -> dict[str, ast.AST]:
    scopes: dict[str, ast.AST] = {"<module>": tree}

    def visit(node: ast.AST, parents: tuple[str, ...]) -> None:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            name = ".".join((*parents, node.name))
            scopes[name] = node
            parents = (*parents, node.name)
        for child in ast.iter_child_nodes(node):
            visit(child, parents)

    visit(tree, ())
    return scopes


def _fingerprint(node: ast.AST) -> str:
    return ast.dump(node, annotate_fields=True, include_attributes=False)


def _anchor_statement(scope: ast.AST, line: int) -> ast.AST | None:
    """Select the innermost statement containing a sidecar locator line."""
    matches = [node for node in ast.walk(scope) if isinstance(node, ast.stmt)
               and getattr(node, "lineno", line + 1) <= line
               and getattr(node, "end_lineno", getattr(node, "lineno", line + 1)) >= line]
    if not matches:
        return None
    return min(matches, key=lambda node: (getattr(node, "end_lineno", line) - getattr(node, "lineno", line),
                                          not isinstance(node, ast.stmt)))


def _fingerprint_counts(scope: ast.AST) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in ast.walk(scope):
        ident = _fingerprint(node)
        counts[ident] = counts.get(ident, 0) + 1
    return counts


def _resolved_expr(node: ast.AST, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _resolved_expr(node.value, aliases)
        return f"{parent}.{node.attr}" if parent else None
    return None


def _call_targets(scope: ast.AST) -> list[str]:
    aliases: dict[str, str] = {}
    for _ in range(4):
        for node in ast.walk(scope):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value = _resolved_expr(node.value, aliases)
                if value:
                    for target in targets:
                        if isinstance(target, ast.Name):
                            aliases[target.id] = value
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    aliases[alias.asname or alias.name.split(".")[0]] = alias.name
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    aliases[alias.asname or alias.name] = f"{module}.{alias.name}".strip(".")
    return [target for node in ast.walk(scope) if isinstance(node, ast.Call)
            if (target := _resolved_expr(node.func, aliases))]


ROUTING_TOKENS = ("front_desk", "assistant_composite", "assistant_alias", "desk_dispatch",
                  "delivery", "outbound_notice", "tell_queue", "send_receipt", "outbox")
PROTECTED_CALL_TOKENS = ROUTING_TOKENS + (
    "policy", "ruling", "attendance", "admit", "spawn", "spec_binding", "work_link",
    "pool", "cap_input", "qualif", "freeze", "consent", "generation",
    "snapshot", "health", "observe", "fresh", "online", "reachable", "pane",
    "working", "mirror", "capture", "usage", "provider", "readback", "stats",
)
DELIVERY_TABLES = ("v2_outbound_notices", "v2_assistant_composite_tell_queue",
                   "v2_send_receipts", "v2_tell_deliveries", "v2_assistant_composite_routes")
DELIVERY_SQL = re.compile(
    r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM|REPLACE\s+INTO)\s+"
    r"[\"`\[]?(" + "|".join(DELIVERY_TABLES) + r")\b", re.I)
OBSERVATION_READERS = {"is_online", "ensure_reachable", "snapshot", "health_snapshot", "host_stats_snapshot"}
OBSERVATION_PUBLISHERS = {"record_observation", "apply_live", "apply_durable", "publish_observation"}
OBSERVATION_FIELDS = {
    "online", "is_online", "last_check", "check_time", "pane_status", "capture_marker",
    "mirror_marker", "working_state", "background_state", "preview_state", "capture_age",
    "account_health", "health_snapshot", "observed_at", "effective_tuple", "provider_context",
    "stats_sample", "sampled_at", "limit_snapshot",
}
POLICY_TOKENS = ("policy", "ruling", "attendance", "pool", "spec_binding", "work_link",
                 "qualification", "workflow", "spawn_freeze")
POLICY_CALL_NAMES = {
    "request_spawn", "request_send", "_work_link_refusal", "_lane_admission",
    "resolve_spec_binding", "_resolve_spec_binding", "admit_launch", "input_guard",
    "cap_input", "suppress_routine_backend_ingress", "operator_dispatch", "check",
    "decide", "evaluate", "allow", "deny", "admit", "authorize",
}
POLICY_IMPORT_TOKENS = ("policy", "lane_rulings", "work_attendance", "work_item_tracking",
                        "account_pool", "spec_binding")


def _routing_branch(node: ast.AST) -> bool:
    tokens = []
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            tokens.append(child.id.lower())
        elif isinstance(child, ast.Attribute):
            tokens.append(child.attr.lower())
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            tokens.append(child.value.lower())
    return any(token in text for token in ROUTING_TOKENS for text in tokens)


def _delivery_sql_mutations(source: str, path: str = "<fixture>") -> list[tuple[str, str]]:
    tree = ast.parse(source, filename=path)
    bindings = sql_call_bindings(tree)
    parents: dict[int, str] = {}

    def record(node: ast.AST, prefix: tuple[str, ...] = ()) -> None:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            prefix = (*prefix, node.name)
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = ".".join(prefix) or "<module>"
            record(child, prefix)

    record(tree)
    output = []
    for node, constants in bindings.items():
        argument = node.args[0]
        value = constants.get(argument.id) if isinstance(argument, ast.Name) else argument
        sql = value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else None
        if isinstance(argument, ast.Name) and isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
            if all(isinstance(part, ast.Constant) and isinstance(part.value, str)
                   for part in (value.left, value.right)):
                sql = value.left.value + value.right.value
        if sql is None and isinstance(argument, ast.JoinedStr):
            pieces = [part.value for part in argument.values if isinstance(part, ast.Constant) and isinstance(part.value, str)]
            sql = "".join(pieces) if pieces else None
        if sql is None:
            continue
        match = DELIVERY_SQL.search(sql)
        if match:
            normalized = " ".join(sql.split()).lower()
            output.append((parents.get(id(node), "<module>"), normalized))
    return output


def _routing_branches(source: str, path: str = "<fixture>") -> list[tuple[str, str]]:
    tree = ast.parse(source, filename=path)
    parents: dict[int, str] = {}

    def record(node: ast.AST, prefix: tuple[str, ...] = ()) -> None:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            prefix = (*prefix, node.name)
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = ".".join(prefix) or "<module>"
            record(child, prefix)

    record(tree)
    return [(parents.get(id(node), "<module>"), _fingerprint(node))
            for node in ast.walk(tree) if isinstance(node, (ast.If, ast.Match)) and _routing_branch(node)]


def _added_delivery_sql(before_source: str, candidate_source: str, path: str) -> list[tuple[str, str]]:
    before: dict[tuple[str, str], int] = {}
    after: dict[tuple[str, str], int] = {}
    for item in _delivery_sql_mutations(before_source, path):
        before[item] = before.get(item, 0) + 1
    for item in _delivery_sql_mutations(candidate_source, path):
        after[item] = after.get(item, 0) + 1
    return [item for item, count in after.items() if count > before.get(item, 0)]


def _added_routing_branches(before_source: str, candidate_source: str, path: str) -> list[tuple[str, str]]:
    before: dict[tuple[str, str], int] = {}
    after: dict[tuple[str, str], int] = {}
    for item in _routing_branches(before_source, path):
        before[item] = before.get(item, 0) + 1
    for item in _routing_branches(candidate_source, path):
        after[item] = after.get(item, 0) + 1
    return [item for item, count in after.items() if count > before.get(item, 0)]


def _observation_accesses(source: str, path: str = "<fixture>") -> list[tuple[str, str, str]]:
    tree = ast.parse(source, filename=path)
    parents: dict[int, str] = {}

    def record(node: ast.AST, prefix: tuple[str, ...] = ()) -> None:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            prefix = (*prefix, node.name)
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = ".".join(prefix) or "<module>"
            record(child, prefix)

    record(tree)
    output = []
    for target in _call_targets(tree):
        terminal = target.rsplit(".", 1)[-1]
        if terminal in OBSERVATION_READERS | OBSERVATION_PUBLISHERS:
            output.append(("call", terminal, target))
    for node in ast.walk(tree):
        field = None
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            field = node.attr.lower()
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            field = node.slice.value.lower()
        if field in OBSERVATION_FIELDS:
            output.append((parents.get(id(node), "<module>"), "field", field))
    return output


def _policy_signatures(source: str, path: str = "<fixture>") -> list[tuple[str, str, str]]:
    tree = ast.parse(source, filename=path)
    parents: dict[int, str] = {}

    def record(node: ast.AST, prefix: tuple[str, ...] = ()) -> None:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            prefix = (*prefix, node.name)
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = ".".join(prefix) or "<module>"
            record(child, prefix)

    record(tree)
    signatures = []
    for node in ast.walk(tree):
        scope = parents.get(id(node), "<module>")
        owner = path.endswith("/assistant_policy.py") or (
            path.endswith("/main.py") and (scope == "<module>" or scope.endswith("_assistant_ingress_policy")))
        if owner:
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name.lower()
                if any(token in module for token in POLICY_IMPORT_TOKENS):
                    signatures.append((scope, "import", module))
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").lower()
            if any(token in module for token in POLICY_IMPORT_TOKENS):
                signatures.append((scope, "import", module))
        elif isinstance(node, ast.Call):
            target = _resolved_expr(node.func, {})
            if target and (node.func and isinstance(node.func, (ast.Name, ast.Attribute))):
                terminal = target.rsplit(".", 1)[-1]
                if terminal in POLICY_CALL_NAMES or any(token in target.lower() for token in POLICY_TOKENS):
                    signatures.append((scope, "call", target.lower()))
        elif isinstance(node, ast.Attribute):
            target = _resolved_expr(node, {})
            if target and (node.attr.lower() in POLICY_CALL_NAMES
                           or any(token in target.lower() for token in POLICY_TOKENS)):
                signatures.append((scope, "attribute", target.lower()))
        elif isinstance(node, (ast.If, ast.IfExp)):
            test = node.test
            tokens = []
            for child in ast.walk(test):
                if isinstance(child, ast.Name):
                    tokens.append(child.id.lower())
                elif isinstance(child, ast.Attribute):
                    tokens.append(child.attr.lower())
                elif isinstance(child, ast.Constant) and isinstance(child.value, str):
                    tokens.append(child.value.lower())
            if any(token == "pool_errors" or any(marker in token for marker in POLICY_TOKENS)
                   for token in tokens):
                signatures.append((scope, "branch", _fingerprint(test)))
    return signatures


def _added_policy_decisions(base_source: str, frozen_source: str, candidate_source: str,
                            path: str = "<fixture>") -> list[tuple[str, str, str]]:
    base_counts = Counter(_policy_signatures(base_source, path))
    frozen_counts = Counter(_policy_signatures(frozen_source, path))
    candidate_counts = Counter(_policy_signatures(candidate_source, path))
    return [item for item, count in candidate_counts.items()
            if count > max(base_counts.get(item, 0), frozen_counts.get(item, 0))]


def _added_observation_accesses(base_source: str, frozen_source: str, candidate_source: str,
                                path: str = "<fixture>") -> list[tuple[str, str, str]]:
    base_counts = Counter(_observation_accesses(base_source, path))
    frozen_counts = Counter(_observation_accesses(frozen_source, path))
    candidate_counts = Counter(_observation_accesses(candidate_source, path))
    return [item for item, count in candidate_counts.items()
            if count > max(base_counts.get(item, 0), frozen_counts.get(item, 0))]


def _check_new_delivery_sql(base: str) -> list[str]:
    changed = set(_git("diff", "--name-only", BASELINE_COMMIT, "--", "services/chat-stream-v2").splitlines())
    changed.update(_git("diff", "--name-only", base, "--", "services/chat-stream-v2").splitlines())
    routing_paths = {anchor.path for anchor in _active_anchors("F6_delivery")}
    errors = []
    for relative in changed:
        if not relative.endswith(".py") or "/tests/" in relative or relative.endswith("/tools/daemon_layer_gate.py"):
            continue
        baseline_raw = _source_at(BASELINE_COMMIT, relative)
        baseline = baseline_raw or ""
        base_source = _source_at(base, relative) or ""
        candidate_path = REPO_ROOT / relative
        if not candidate_path.exists():
            continue
        candidate = candidate_path.read_text(encoding="utf-8")
        allowed_sql: dict[tuple[str, str], int] = {}
        for source in (baseline, base_source):
            for item, count in Counter(_delivery_sql_mutations(source, relative)).items():
                allowed_sql[item] = max(allowed_sql.get(item, 0), count)
        candidate_sql = Counter(_delivery_sql_mutations(candidate, relative))
        for (symbol, _sql), count in candidate_sql.items():
            if count > allowed_sql.get((symbol, _sql), 0):
                errors.append(f"F6_delivery: new aliased or literal delivery-table SQL in {relative}:{symbol}")
        if relative in routing_paths:
            allowed_branches: dict[tuple[str, str], int] = {}
            for source in (baseline, base_source):
                for item, count in Counter(_routing_branches(source, relative)).items():
                    allowed_branches[item] = max(allowed_branches.get(item, 0), count)
            candidate_branches = Counter(_routing_branches(candidate, relative))
            for (symbol, _branch), count in candidate_branches.items():
                if count > allowed_branches.get((symbol, _branch), 0):
                    errors.append(f"F6_delivery: new routing branch in {relative}:{symbol}")
        for (_scope, _kind, selector) in _added_observation_accesses(base_source, baseline, candidate, relative):
            errors.append(f"F5_observation: new raw observation access in {relative}:{selector}")
    return errors


def _source_at(revision: str, path: str) -> str | None:
    result = subprocess.run(["git", "show", f"{revision}:{path}"], cwd=REPO_ROOT,
                            text=True, capture_output=True, check=False, timeout=20)
    return result.stdout if result.returncode == 0 else None


def _active_anchors(name: str) -> list[Anchor]:
    return _parse_anchors(SIDECARS[name].read_text(encoding="utf-8"))


def _compare_anchors(name: str, anchors: list[Anchor],
                     frozen_scopes: dict[tuple[str, str], ast.AST],
                     mergebase_scopes: dict[tuple[str, str], ast.AST],
                     candidate_scopes: dict[tuple[str, str], ast.AST]) -> list[str]:
    errors: list[str] = []
    count_cache: dict[tuple[str, str, str], dict[str, int]] = {}
    for path, symbol, line in sorted({(a.path, a.symbol, a.line) for a in anchors}):
        baseline_scope = frozen_scopes.get((path, symbol))
        mergebase_scope = mergebase_scopes.get((path, symbol))
        candidate_scope = candidate_scopes.get((path, symbol))
        if baseline_scope is None:
            errors.append(f"{name}: frozen symbol missing: {path}:{line} {symbol}")
            continue
        if candidate_scope is None:
            errors.append(f"{name}: protected symbol is missing: {path}:{line} {symbol}")
            continue
        baseline_node = _anchor_statement(baseline_scope, line)
        if baseline_node is None:
            errors.append(f"{name}: frozen AST anchor missing: {path}:{line} {symbol}")
            continue
        fingerprint = _fingerprint(baseline_node)
        base_key, candidate_key = (path, symbol, "base"), (path, symbol, "candidate")
        if base_key not in count_cache:
            count_cache[base_key] = _fingerprint_counts(baseline_scope)
        if candidate_key not in count_cache:
            count_cache[candidate_key] = _fingerprint_counts(candidate_scope)
        baseline_counts, candidate_counts = count_cache[base_key], count_cache[candidate_key]
        baseline_count = baseline_counts.get(fingerprint, 0)
        candidate_count = candidate_counts.get(fingerprint, 0)
        mergebase_count = 0
        if mergebase_scope is not None:
            mergebase_count = _fingerprint_counts(mergebase_scope).get(fingerprint, 0)
        expected_count = max(baseline_count, mergebase_count)
        if candidate_count != expected_count:
            errors.append(f"{name}: protected AST selector count changed ({expected_count} to {candidate_count}): {path}:{line} {symbol}")
        for call_id in {_fingerprint(n) for n in ast.walk(baseline_node) if isinstance(n, ast.Call)}:
            if candidate_counts.get(call_id, 0) > baseline_counts.get(call_id, 0):
                errors.append(f"{name}: duplicate protected call in {path} {symbol}")
        baseline_targets = _call_targets(baseline_scope)
        candidate_targets = _call_targets(candidate_scope)
        selected_targets = {_resolved_expr(n.func, {}) for n in ast.walk(baseline_node)
                            if isinstance(n, ast.Call)}
        selected_targets.discard(None)
        selected_targets = {target for target in selected_targets
                            if any(token in target.lower() for token in PROTECTED_CALL_TOKENS)}
        for target in selected_targets:
            if candidate_targets.count(target) > baseline_targets.count(target):
                errors.append(f"{name}: aliased duplicate protected call in {path} {symbol}")
        for branch in (n for n in ast.walk(candidate_scope) if isinstance(n, (ast.If, ast.Match))
                       and _routing_branch(n)):
            branch_id = _fingerprint(branch)
            if candidate_counts.get(branch_id, 0) > baseline_counts.get(branch_id, 0):
                errors.append(f"{name}: new routing branch in {path} {symbol}")
    return sorted(set(errors))


def _retired_selector_errors(base: str, active_anchors: list[Anchor]) -> list[str]:
    frozen_anchors = _parse_anchors(SERVICE_ROOT.joinpath("daemon_contract", "exceptions", "F3_policy.txt").read_text(encoding="utf-8"))
    frozen_anchors += _parse_anchors(SERVICE_ROOT.joinpath("daemon_contract", "exceptions", "F5_observation.txt").read_text(encoding="utf-8"))
    frozen_anchors += _parse_anchors(SERVICE_ROOT.joinpath("daemon_contract", "exceptions", "F6_delivery.txt").read_text(encoding="utf-8"))
    active_keys = {(a.path, a.line, a.symbol) for a in active_anchors}
    retired = [a for a in frozen_anchors if (a.path, a.line, a.symbol) not in active_keys]
    if not retired:
        return []
    frozen_scopes = _load_revision(BASELINE_COMMIT, retired)
    mergebase_scopes = _load_revision(base, retired)
    candidate_scopes = _load_revision("HEAD", retired)
    active_fingerprints: set[tuple[str, str, str]] = set()
    frozen_fingerprints: dict[tuple[str, str, int], str] = {}
    for anchor in frozen_anchors:
        scope = frozen_scopes.get((anchor.path, anchor.symbol))
        node = _anchor_statement(scope, anchor.line) if scope else None
        if node is not None:
            ident = _fingerprint(node)
            frozen_fingerprints[(anchor.path, anchor.symbol, anchor.line)] = ident
            if (anchor.path, anchor.line, anchor.symbol) in active_keys:
                active_fingerprints.add((anchor.path, anchor.symbol, ident))
    errors = []
    for anchor in retired:
        fingerprint = frozen_fingerprints.get((anchor.path, anchor.symbol, anchor.line))
        if fingerprint is None or (anchor.path, anchor.symbol, fingerprint) in active_fingerprints:
            continue
        candidate_scope = candidate_scopes.get((anchor.path, anchor.symbol))
        if candidate_scope is None:
            continue
        mergebase_scope = mergebase_scopes.get((anchor.path, anchor.symbol))
        disposition = _retired_selector_status(fingerprint, mergebase_scope, candidate_scope)
        if disposition:
            errors.append(f"retired selector {disposition}: {anchor.path}:{anchor.line} {anchor.symbol}")
    return sorted(set(errors))


def _retired_selector_status(fingerprint: str, mergebase_scope: ast.AST | None,
                             candidate_scope: ast.AST) -> str | None:
    mergebase_count = _fingerprint_counts(mergebase_scope).get(fingerprint, 0) if mergebase_scope else 0
    candidate_count = _fingerprint_counts(candidate_scope).get(fingerprint, 0)
    if not candidate_count:
        return None
    return "reintroduced since merge base" if candidate_count > mergebase_count else "remains after exception removal"


def _check_candidate_policy(base: str) -> list[str]:
    changed = set(_git("diff", "--name-only", BASELINE_COMMIT, "--", "services/chat-stream-v2").splitlines())
    changed.update(_git("diff", "--name-only", base, "--", "services/chat-stream-v2").splitlines())
    errors = []
    for relative in sorted(changed):
        if (not relative.endswith(".py") or "/tests/" in relative or "/tools/" in relative
                or relative.endswith("/assistant_policy.py")):
            continue
        candidate_path = REPO_ROOT / relative
        if not candidate_path.exists():
            continue
        candidate = candidate_path.read_text(encoding="utf-8")
        mergebase = _source_at(base, relative) or ""
        frozen = _source_at(BASELINE_COMMIT, relative) or ""
        for scope, kind, selector in _added_policy_decisions(mergebase, frozen, candidate, relative):
            errors.append(f"F3_policy: new named policy {kind} in {relative}:{scope} ({selector[:90]})")
    return errors


def _load_revision(revision: str, anchors: list[Anchor]) -> dict[tuple[str, str], ast.AST]:
    scopes: dict[tuple[str, str], ast.AST] = {}
    parsed_cache: dict[str, dict[str, ast.AST]] = {}
    for anchor in anchors:
        if anchor.path not in parsed_cache:
            source = (Path(REPO_ROOT / anchor.path).read_text(encoding="utf-8")
                      if revision == "HEAD" else _source_at(revision, anchor.path))
            try:
                parsed_cache[anchor.path] = _scope_nodes(ast.parse(source, filename=anchor.path)) if source is not None else {}
            except SyntaxError:
                parsed_cache[anchor.path] = {}
        scope = parsed_cache[anchor.path].get(anchor.symbol)
        if scope is not None:
            scopes[(anchor.path, anchor.symbol)] = scope
    return scopes


def validate_layer_scopes(base: str) -> list[str]:
    """Reject changed protected scopes and sidecar entries absent from base.

    The base parameter is the candidate merge base used by the existing daemon
    contract gate. The immutable exception identity remains tied to B01's
    frozen source baseline.
    """
    errors: list[str] = []
    all_anchors: list[Anchor] = []
    for name in SIDECARS:
        try:
            anchors = _active_anchors(name)
            frozen_scopes = _load_revision(BASELINE_COMMIT, anchors)
            mergebase_scopes = _load_revision(base, anchors)
            candidate_scopes = _load_revision("HEAD", anchors)
        except (OSError, SyntaxError, ValueError, RuntimeError) as exc:
            errors.append(f"{name}: cannot load frozen selector inventory ({type(exc).__name__})")
            continue
        all_anchors.extend(anchors)
        errors.extend(_compare_anchors(name, anchors, frozen_scopes, mergebase_scopes, candidate_scopes))
    errors.extend(_retired_selector_errors(base, all_anchors))
    errors.extend(_check_candidate_policy(base))
    errors.extend(_check_new_delivery_sql(base))
    return sorted(set(errors))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="trusted merge-base commit SHA")
    args = parser.parse_args(argv)
    try:
        if len(args.base) != 40 or any(char not in "0123456789abcdef" for char in args.base):
            raise ValueError("--base must be a full lowercase commit SHA")
        _git("cat-file", "-e", f"{args.base}^{{commit}}")
        errors = validate_layer_scopes(args.base)
    except (OSError, UnicodeError, SyntaxError, ValueError, RuntimeError) as exc:
        print(f"[daemon-layer] FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(f"[daemon-layer] FAIL: {error}", file=sys.stderr)
        return 1
    print("[daemon-layer] PASS: policy, observation, and delivery AST selectors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
