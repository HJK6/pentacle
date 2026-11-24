#!/usr/bin/env python3
"""Check daemon structure config, frozen exception baselines, and startup SQL drift."""
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
BASELINE_COMMIT = "f153ca8adce2a38e0c5092b2dc98ff5aec16e06e"
PINNED_INVENTORIES = {
    "exceptions/F1_protected_core.txt": "501e96719f82ba6d1cc89c04de6ebe23eabccc342f64cfcf2cbf699ab9b3377f",
    "exceptions/F2_projections.txt": "be29143b9679b5332f7434fdd42834c256bcdb17de6dc413ff39402fff9251f9",
    "exceptions/F3_policy.txt": "b8a6e722d4838653be22f6b96d2a040c285ac484b20fb737f4130a942cca24c9",
    "exceptions/P0_release_versioning.txt": "f4d2ff67602e5ad1ab7940112b5421efb4ff9bdf3cbe08f6e60a9476e8ab6a56",
    "exceptions/F5_observation.txt": "b67c524bda55743601211cc7acec58d53fb7b2a9a6608518ef2f3be80f814193",
    "exceptions/F6_delivery.txt": "544e839a86178c0987c70ac31e676b17eefbba46ad5bb6242f6c62319b177285",
}
EXPECTED_LAYERS = [
    "F1_protected_chat_core", "F2_event_feed_and_projections",
    "F3_workflow_policy", "F4_migrations_and_safe_switch",
    "F5_observation", "F6_durable_delivery",
]
WRITE_WORDS = re.compile(r"\b(CREATE|ALTER|DROP|INSERT|UPDATE|DELETE|REPLACE)\b", re.I)
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
    owner = contract.get("structure_owner")
    owner_status = contract.get("structure_owner_status")
    if owner is None and owner_status == "pending":
        print("[daemon-contract] structure owner is pending the operator card")
    elif not isinstance(owner, str) or not IDENTITY_RE.fullmatch(owner) or owner_status != "configured":
        errors.append("structure_owner must be one configured host:v2-seat identity or explicit pending/null")

    inventories = manifest.get("inventories")
    expected_names = {
        "F1_protected_core", "F2_projections", "F3_policy",
        "F4_migrations_and_safe_switch", "F5_observation", "F6_durable_delivery",
    }
    if manifest.get("frozen_at_commit") != BASELINE_COMMIT or not isinstance(inventories, dict) or set(inventories) != expected_names:
        errors.append("inventory manifest must retain the six frozen inventory identities")
        return errors
    for name, entry in inventories.items():
        relative = entry.get("file") if isinstance(entry, dict) else None
        digest = entry.get("sha256") if isinstance(entry, dict) else None
        if relative not in PINNED_INVENTORIES or digest != PINNED_INVENTORIES.get(relative):
            errors.append(f"{name}: inventory path or pinned digest changed")
            continue
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
    return errors


def _constant_strings(tree: ast.AST) -> dict[str, tuple[str, ...]]:
    values: dict[str, tuple[str, ...]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            strings: tuple[str, ...] = ()
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                strings = (value.value,)
            elif isinstance(value, (ast.Tuple, ast.List)) and all(
                    isinstance(item, ast.Constant) and isinstance(item.value, str) for item in value.elts):
                strings = tuple(item.value for item in value.elts)
            for target in targets:
                if isinstance(target, ast.Name) and strings:
                    values[target.id] = strings
    return values


def _exception_additions(base_text: str, candidate_text: str) -> list[str]:
    base_entries = set(base_text.splitlines()) - {""}
    candidate_entries = set(candidate_text.splitlines()) - {""}
    return sorted(candidate_entries - base_entries)


def _sql_argument(node: ast.expr, constants: dict[str, tuple[str, ...]]) -> tuple[str, ...] | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return (node.value,)
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, (ast.Tuple, ast.List)):
        values: list[str] = []
        for item in node.elts:
            found = _sql_argument(item, constants)
            if found is None:
                return None
            values.extend(found)
        return tuple(values)
    if isinstance(node, ast.JoinedStr):
        pieces = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                pieces.append(value.value)
            elif isinstance(value, ast.FormattedValue) and isinstance(value.value, ast.Constant):
                pieces.append(str(value.value.value))
            else:
                pieces.append("<dynamic>")
        return ("".join(pieces),)
    return None


def _is_persistent_write(sql: str) -> bool:
    normalized = re.sub(r"(?m)--[^\n]*|/\*.*?\*/", " ", sql, flags=re.S).strip()
    if normalized.upper().startswith("PRAGMA"):
        return bool(re.search(r"\b(journal_mode|synchronous|user_version|schema_version)\s*=", normalized, re.I))
    return bool(WRITE_WORDS.search(normalized))


def _startup_writes(source: str) -> dict[tuple[str, int, str], str]:
    """Find literal persistent SQL executed directly from Store startup.

    Indirect calls and dynamically supplied SQL remain a conservative follow-up
    for the full inventory rule; this seed rule catches direct additions and
    changed literals in the open path.
    """
    tree = ast.parse(source)
    constants = _constant_strings(tree)
    store = next((node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Store"), None)
    if store is None:
        raise GateError("store.py has no Store class")
    result: dict[tuple[str, int, str], str] = {}
    for method_name in ("__init__", "_run"):
        method = next((node for node in store.body if isinstance(node, ast.FunctionDef) and node.name == method_name), None)
        if method is None:
            raise GateError(f"Store.{method_name} is missing")
        for node in ast.walk(method):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"execute", "executemany", "executescript"} or not node.args:
                continue
            sqls = _sql_argument(node.args[0], constants)
            if sqls is None:
                # An unresolved SQL argument on the open path must not silently
                # escape the baseline comparison. Treat it as a capability marker.
                sqls = (f"<unresolved SQL argument: {ast.dump(node.args[0], include_attributes=False)}>",)
            for sql in sqls:
                if sql.startswith("<unresolved SQL argument:") or _is_persistent_write(sql):
                    normalized = " ".join(sql.split())
                    result[(method_name, node.lineno, normalized)] = normalized
    return result


def _check_startup_drift(base: str) -> list[str]:
    relative = "services/chat-stream-v2/store.py"
    base_raw = _base_file(base, relative)
    if base_raw is None:
        raise GateError("trusted base lacks store.py")
    candidate = (REPO_ROOT / relative).read_text(encoding="utf-8")
    baseline = _startup_writes(base_raw.decode("utf-8"))
    current = _startup_writes(candidate)
    # Exact SQL at the same source line is the stable identity for the first
    # adoption. Moving an allowance changes the line and counts as an addition.
    added = sorted(set(current) - set(baseline))
    if not added:
        return []
    return [f"Store startup adds or moves {len(added)} persistent SQL statement(s): " +
            ", ".join(f"{method} line {line}" for method, line, _ in added[:8])]


def check(base: str) -> list[str]:
    global _ACTIVE_BASE
    if not re.fullmatch(r"[0-9a-f]{40}", base):
        raise GateError("--base must be a full lowercase commit SHA")
    _ACTIVE_BASE = base
    _git("cat-file", "-e", f"{base}^{{commit}}")
    errors = _validate_contract()
    errors.extend(_check_startup_drift(base))
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
    print("[daemon-contract] PASS: six layers, two-lane cap, frozen inventories, and startup SQL baseline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
