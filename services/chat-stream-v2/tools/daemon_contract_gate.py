#!/usr/bin/env python3
"""Check daemon structure config and frozen exception baselines."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_ROOT = SERVICE_ROOT / "daemon_contract"
CONTRACT_PATH = CONTRACT_ROOT / "contract.json"
INVENTORY_PATH = CONTRACT_ROOT / "inventory_manifest.json"
LOCAL_OWNER_CONFIG_PATH = Path.home() / ".config" / "pentacle" / "daemon-contract.json"
BASELINE_COMMIT = "f153ca8adce2a38e0c5092b2dc98ff5aec16e06e"
PINNED_INVENTORIES = {
    "exceptions/F1_protected_core.txt": "501e96719f82ba6d1cc89c04de6ebe23eabccc342f64cfcf2cbf699ab9b3377f",
    "exceptions/F2_projections.txt": "be29143b9679b5332f7434fdd42834c256bcdb17de6dc413ff39402fff9251f9",
    "exceptions/F3_policy.txt": "b8a6e722d4838653be22f6b96d2a040c285ac484b20fb737f4130a942cca24c9",
    "exceptions/P0_release_versioning.txt": "f4d2ff67602e5ad1ab7940112b5421efb4ff9bdf3cbe08f6e60a9476e8ab6a56",
    "exceptions/F5_observation.txt": "b67c524bda55743601211cc7acec58d53fb7b2a9a6608518ef2f3be80f814193",
    "exceptions/F6_delivery.txt": "544e839a86178c0987c70ac31e676b17eefbba46ad5bb6242f6c62319b177285",
}
EXPECTED_INVENTORY_PATHS = {
    "F1_protected_core": "exceptions/F1_protected_core.txt",
    "F2_projections": "exceptions/F2_projections.txt",
    "F3_policy": "exceptions/F3_policy.txt",
    "F4_migrations_and_safe_switch": "exceptions/P0_release_versioning.txt",
    "F5_observation": "exceptions/F5_observation.txt",
    "F6_durable_delivery": "exceptions/F6_delivery.txt",
}
EXPECTED_LAYERS = [
    "F1_protected_chat_core", "F2_event_feed_and_projections",
    "F3_workflow_policy", "F4_migrations_and_safe_switch",
    "F5_observation", "F6_durable_delivery",
]
IDENTITY_RE = re.compile(r"^[a-z0-9_-]+:v2-[a-z0-9-]+$", re.I)


class GateError(RuntimeError):
    pass


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=REPO_ROOT, text=True,
                            capture_output=True, check=False, timeout=20)
    if result.returncode:
        raise GateError(f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()[:300]}")
    return result.stdout


def _base_file(base: str, relative: str) -> bytes | None:
    result = subprocess.run(["git", "show", f"{base}:{relative}"], cwd=REPO_ROOT,
                            text=False, capture_output=True, check=False, timeout=20)
    if result.returncode:
        return None
    return result.stdout


def _local_structure_owner_configured() -> bool:
    """Read the optional installation owner without exposing its value."""
    try:
        raw = LOCAL_OWNER_CONFIG_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise GateError("local daemon contract config is unreadable") from exc
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GateError("local daemon contract config is invalid JSON") from exc
    owner = config.get("structure_owner") if isinstance(config, dict) else None
    if not isinstance(owner, str) or not IDENTITY_RE.fullmatch(owner):
        raise GateError("local daemon contract config must contain a valid structure_owner")
    return True


def _validate_contract() -> list[str]:
    errors: list[str] = []
    try:
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        manifest = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError(f"contract configuration is unreadable: {type(exc).__name__}") from exc
    if contract.get("contract_version") != 1 or contract.get("baseline_commit") != BASELINE_COMMIT:
        errors.append("contract version or frozen baseline commit changed")
    if contract.get("max_active_daemon_lanes") != 2:
        errors.append("max_active_daemon_lanes must remain exactly 2")
    if contract.get("layers") != EXPECTED_LAYERS:
        errors.append("the six daemon layers must appear once in the frozen order")
    if contract.get("structure_owner") is not None or contract.get("structure_owner_status") != "pending":
        errors.append("public structure_owner must remain explicit pending/null")
    if not _local_structure_owner_configured():
        print("[daemon-contract] structure owner is pending local configuration")
    else:
        print("[daemon-contract] local structure owner is configured")

    inventories = manifest.get("inventories")
    expected_names = {
        "F1_protected_core", "F2_projections", "F3_policy",
        "F4_migrations_and_safe_switch", "F5_observation", "F6_durable_delivery",
    }
    if manifest.get("frozen_at_commit") != BASELINE_COMMIT or not isinstance(inventories, dict) or set(inventories) != expected_names:
        errors.append("inventory manifest must retain the six frozen inventory identities")
        return errors
    seen_paths: set[str] = set()
    for name, entry in inventories.items():
        relative = entry.get("file") if isinstance(entry, dict) else None
        digest = entry.get("sha256") if isinstance(entry, dict) else None
        expected_path = EXPECTED_INVENTORY_PATHS.get(name)
        if relative != expected_path or digest != PINNED_INVENTORIES.get(expected_path):
            errors.append(f"{name}: inventory path or pinned digest does not match its canonical layer")
            continue
        seen_paths.add(relative)
        path = CONTRACT_ROOT / relative
        try:
            baseline_raw = path.read_bytes()
        except OSError:
            errors.append(f"{name}: frozen inventory file is missing")
            continue
        actual = hashlib.sha256(baseline_raw).hexdigest()
        if actual != digest:
            errors.append(f"{name}: frozen inventory digest mismatch")
            continue
        active_relative = "active/" + Path(relative).name
        active_path = CONTRACT_ROOT / active_relative
        try:
            active_raw = active_path.read_bytes()
        except OSError:
            errors.append(f"{name}: active exception list is missing")
            continue
        baseline_entries = set(baseline_raw.decode("utf-8").splitlines()) - {""}
        active_entries = set(active_raw.decode("utf-8").splitlines()) - {""}
        if active_entries - baseline_entries:
            errors.append(f"{name}: active list adds entries beyond its frozen inventory")
        base_active = _base_file(_ACTIVE_BASE, (CONTRACT_ROOT / active_relative).relative_to(REPO_ROOT).as_posix())
        if base_active is None:
            if active_entries != baseline_entries:
                errors.append(f"{name}: first-adoption active list must match its frozen inventory")
        else:
            added = _exception_additions(base_active.decode("utf-8"), active_raw.decode("utf-8"))
            if added:
                errors.append(f"{name}: candidate adds or moves {len(added)} active exception entr{'y' if len(added) == 1 else 'ies'}")
    if len(seen_paths) != len(EXPECTED_INVENTORY_PATHS):
        errors.append("the six layers must reference six unique canonical inventory files")
    return errors


def sql_call_bindings(tree: ast.Module) -> dict[ast.Call, dict[str, ast.expr]]:
    """Index simple assignments visible to each execute call in B01 and B05."""
    calls: dict[ast.Call, dict[str, ast.expr]] = {}
    module: dict[str, ast.expr] = {}
    functions = []

    def visit(node: ast.AST, values: dict[str, ast.expr], closure: dict[str, ast.expr]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            # Defaults, bases, annotations and decorators run in the defining scope.
            body = [node.body] if isinstance(node, ast.Lambda) else node.body
            body_nodes = set(body)
            for child in ast.iter_child_nodes(node):
                if child not in body_nodes:
                    visit(child, values, closure)
            if isinstance(node, ast.ClassDef):
                namespace = closure.copy()
                for statement in body:
                    visit(statement, namespace, closure)
            else:
                # Delay the body so it can see later outer definitions. A method
                # closes over its enclosing function/module, skipping class locals.
                functions.append((body, node.args, closure))
            if not isinstance(node, ast.Lambda):
                values.pop(node.name, None)
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            local = closure.copy()
            for index, generator in enumerate(node.generators):
                visit(generator.iter, values if index == 0 else local, closure if index == 0 else local)
                visit(generator.target, local, local)
                for condition in generator.ifs:
                    visit(condition, local, local)
            outputs = (node.key, node.value) if isinstance(node, ast.DictComp) else (node.elt,)
            for output in outputs:
                visit(output, local, local)
            return
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"execute", "executemany", "executescript"} and node.args):
            calls[node] = values.copy()
        if isinstance(node, ast.Global):
            for name in node.names:
                if name in module:
                    values[name] = module[name]
                else:
                    values.pop(name, None)
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            visit(node.value, values, closure)
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                visit(target, values, closure)
                if isinstance(target, ast.Name):
                    values[target.id] = node.value
            if isinstance(node, ast.AnnAssign):
                visit(node.annotation, values, closure)
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            values.pop(node.id, None)
        if isinstance(node, ast.alias):
            values.pop(node.asname or node.name.split(".")[0], None)
        for child in ast.iter_child_nodes(node):
            visit(child, values, closure)

    visit(tree, module, module)
    while functions:
        body, arguments, outer = functions.pop()
        local = outer.copy()
        for arg in ast.iter_child_nodes(arguments):
            if isinstance(arg, ast.arg):
                local.pop(arg.arg, None)
        for statement in body:
            visit(statement, local, local)
    return calls


def _exception_additions(base_text: str, candidate_text: str) -> list[str]:
    base_entries = set(base_text.splitlines()) - {""}
    candidate_entries = set(candidate_text.splitlines()) - {""}
    return sorted(candidate_entries - base_entries)


def check(base: str) -> list[str]:
    global _ACTIVE_BASE
    if not re.fullmatch(r"[0-9a-f]{40}", base):
        raise GateError("--base must be a full lowercase commit SHA")
    _ACTIVE_BASE = base
    _git("cat-file", "-e", f"{base}^{{commit}}")
    errors = _validate_contract()
    return errors


_ACTIVE_BASE = BASELINE_COMMIT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="trusted merge-base commit")
    args = parser.parse_args(argv)
    try:
        errors = check(args.base)
    except (GateError, OSError, UnicodeError, SyntaxError) as exc:
        print(f"[daemon-contract] FAIL: {exc}", file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(f"[daemon-contract] FAIL: {error}", file=sys.stderr)
        return 1
    print("[daemon-contract] PASS: six layers, two-lane cap and frozen inventories")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
