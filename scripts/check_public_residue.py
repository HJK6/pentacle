#!/usr/bin/env python3
"""Check portable privacy rules and an optional external private dictionary.

This finite guard supplements review; it is not an exhaustive secret scanner.
Private dictionary values and matched source text are never emitted.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess

PATTERN = re.compile("|".join(["host" + suffix for suffix in "abc"] + ["ab" + "ra"]))

# CGNAT / Tailscale range 100.64/10 (second octet 64-127): a concrete
# tailnet address must never ship in the public tree. The residue check missed a
# real one (server/README.md, a deployment example) — this makes it a guard.
# Bounded by non-digit/non-dot on both sides so it never fires inside a larger
# number, and second octet 64-127 excludes 100.0-63 / 100.128-255.
CGNAT_PATTERN = re.compile(
    r"(?<![0-9.])100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])"
    r"\.[0-9]{1,3}\.[0-9]{1,3}(?![0-9.])"
)


# Mobile uses these fixed symbolic identifiers and ordinary words in its public
# source. Keep the default detector unchanged and inspect addresses before
# normalization. Underscores delimit fixture-ID components.
MOBILE_SYNTHETIC_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:" + "|".join(
        ["host" + suffix for suffix in "abc"]
        + ["host" + suffix for suffix in ("config", "admission", "busy")]
    ) + r")(?![A-Za-z0-9])"
)

HOME_PATH_PATTERN = re.compile(
    r"(?<![\w/])/(?:Users|home)/(?!example(?:/|\b)|[<${])[\w][\w.-]*(?:/|(?=['\"\s]|$))"
    r"|[A-Za-z]:\\+Users\\+(?!example(?:\\|\b)|[<${])[\w][\w.-]*", re.IGNORECASE
)
CREDENTIAL_PATTERN = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    r"|(?<![A-Za-z0-9_-])(?:AKIA[A-Z0-9]{16}|sk-(?:proj-|ant-)[A-Za-z0-9_-]{24,}"
    r"|ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{50,})(?![A-Za-z0-9_-])"
)
PERSONAL_EMAIL_PATTERN = re.compile(
    r"[A-Za-z0-9._%+-]+@(?:gmail\.com|outlook\.com|hotmail\.com|yahoo\.com|proton\.me|protonmail\.com)\b",
    re.IGNORECASE,
)
PRIVATE_ENDPOINT_PATTERN = re.compile(
    r"(?:https?|wss?|ssh)://(?:10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}"
    r"|172\.(?:1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}"
    r"|192\.168\.[0-9]{1,3}\.[0-9]{1,3}|\[f[cd][0-9a-f:]+\])(?=[:/\s'\"\]]|$)", re.IGNORECASE
)
PRESENTATION_ID_PATTERN = re.compile(
    r"(?:\b(?:\w*StreamId|streamId|stream_id)|slotCopyIdStreamId\([^)]*\))\s*(?:===|!==)\s*"
    r"(['\"])[A-Za-z0-9_.-]+:assistant\1"
)
LEXICAL_RULES = {
    "private_home_path": HOME_PATH_PATTERN, "credential": CREDENTIAL_PATTERN,
    "personal_email": PERSONAL_EMAIL_PATTERN, "private_endpoint": PRIVATE_ENDPOINT_PATTERN,
}
LEGACY_FIXTURE_RULES = {"anonymizer", "cgnat"}
EXCEPTION_RULES = {"private_term", "private_home_path", "personal_email", "private_endpoint"}
CONTRACT_FIXTURES = {"services/chat-stream-v2/tests/fixtures/usage_stage1_manifest_schema.json"}


def _fixture_path(name: str) -> bool:
    return (bool({"test", "tests", "fixtures"} & set(Path(name).parts))
            or name == ".github/ci/hermetic_machines.json") and name not in CONTRACT_FIXTURES


def _line_rules(line: str, mobile_synthetic: bool = False) -> set[str]:
    rules = {rule for rule, pattern in LEXICAL_RULES.items() if pattern.search(line)}
    if CGNAT_PATTERN.search(line):
        rules.add("cgnat")
    normalized = MOBILE_SYNTHETIC_PATTERN.sub("synthetic", line) if mobile_synthetic else line
    if PATTERN.search(normalized):
        rules.add("anonymizer")
    return rules


def _literal(node, constants):
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"set", "frozenset", "tuple", "list"} and len(node.args) == 1:
        return _literal(node.args[0], constants)
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return None


def _key(node):
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        return node.slice.value
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
            and node.args and isinstance(node.args[0], ast.Constant)):
        return node.args[0].value
    return None


def _semantic_hits(name: str, content: str) -> list[tuple[int, str]]:
    if _fixture_path(name):
        return []
    if name.endswith(('.js', '.ts')):
        return [(number, "fixed_assistant_presentation_id") for number, line in enumerate(content.splitlines(), 1)
                if PRESENTATION_ID_PATTERN.search(line)]
    if not name.endswith('.py'):
        return []
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []  # Lexical/dictionary checks still run; compilation is a separate gate.
    constants = {}
    hits = []
    authorization_nodes = {id(child) for node in ast.walk(tree)
                           if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and "authoriz" in node.name
                           for child in ast.walk(node)}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            value = _literal(node.value, constants)
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = value
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (isinstance(target, ast.Name) and "EXCLUDED" in target.id.upper()
                        and "HOST" in target.id.upper() and _literal(node.value, constants)):
                    hits.append((node.lineno, "fixed_host_exclusions"))
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            values = [_literal(operand, constants) for operand in operands]
            spec_context = (id(node) in authorization_nodes
                            or any(_key(operand) == "spec_id" for operand in operands)
                            or any(isinstance(child, ast.Name) and ("qualified" in child.id or "provenance" in child.id)
                                   for child in ast.walk(node)))
            if spec_context and any(isinstance(value, str) and re.fullmatch(r"spec_[a-z0-9_]+", value) for value in values):
                hits.append((node.lineno, "fixed_permission_spec"))
            if (any(_key(operand) == "owner" for operand in operands)
                    and any(isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_-]*:[A-Za-z0-9_.:-]+", value)
                            for value in values)):
                hits.append((node.lineno, "fixed_pin_owner"))
        if isinstance(node, ast.Call) and len(node.args) >= 2 and isinstance(node.func, ast.Name) and node.func.id == "_required":
            field = _key(node.args[0])
            keys = _literal(node.args[1], constants)
            if isinstance(keys, (set, list, tuple, dict)) and keys:
                if field == "satellites":
                    hits.append((node.lineno, "fixed_satellite_inventory"))
                if field == "receipts" and any(isinstance(key, str) and key.startswith("prechange_") for key in keys):
                    hits.append((node.lineno, "fixed_prechange_hosts"))
    return hits


def _private_terms(root: Path, path: Path | None) -> tuple[list[str], dict]:
    if path is None:
        return [], {"status": "not_run", "count": 0}
    if path.resolve().is_relative_to(root):
        raise ValueError("private terms file must be outside the source checkout")
    raw = path.read_bytes()
    try:
        values = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("invalid private terms JSON") from exc
    if (not isinstance(values, list) or not values or len(values) > 1000
            or any(not isinstance(value, str) or not value.strip() or len(value) > 1024 for value in values)):
        raise ValueError("private terms must be a nonempty JSON string array")
    terms = [value.casefold() for value in values]
    if len(set(terms)) != len(terms):
        raise ValueError("private terms must be unique")
    return terms, {"status": "checked", "count": len(terms), "sha256": hashlib.sha256(raw).hexdigest()}


def _display_path(name: str, terms: list[str]) -> str:
    if any(term in name.casefold() for term in terms):
        return f"<private-path:{hashlib.sha256(name.encode()).hexdigest()}>"
    return name


def _line_hits(line: str, mobile_synthetic: bool = False) -> bool:
    return bool(_line_rules(line, mobile_synthetic))


def check(root: Path, allowlist: Path, private_terms_file: Path | None = None) -> dict:
    root = root.resolve()
    terms, terms_receipt = _private_terms(root, private_terms_file)
    manifest = json.loads(allowlist.read_text())
    if manifest.get("version") != 1 or not isinstance(manifest.get("fixtures"), dict):
        raise ValueError("expected version 1 and an exact-path fixtures object")
    profile = manifest.get("profile")
    if profile not in (None, "mobile-synthetic"):
        raise ValueError("unknown residue profile")
    fixtures = manifest["fixtures"]
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
    names = set(tracked)
    for name, reason in fixtures.items():
        path = Path(name)
        if name not in names or not isinstance(reason, str) or not reason.strip():
            raise ValueError("invalid fixture entry")
        if not _fixture_path(name):
            raise ValueError("allowlist cannot exempt shipped source")
    exceptions = set()
    synthetic_exceptions = manifest.get("synthetic_exceptions", [])
    if not isinstance(synthetic_exceptions, list):
        raise ValueError("synthetic exceptions must be an array")
    if synthetic_exceptions and allowlist.resolve().is_relative_to(root):
        raise ValueError("frozen synthetic exceptions must be supplied outside the source checkout")
    for entry in synthetic_exceptions:
        if not isinstance(entry, dict) or set(entry) != {"path", "rule", "sha256", "reason"}:
            raise ValueError("invalid synthetic exception")
        name, rule = entry["path"], entry["rule"]
        if (not isinstance(name, str) or name not in names or not _fixture_path(name)
                or not isinstance(rule, str) or rule not in EXCEPTION_RULES or not isinstance(entry["reason"], str) or not entry["reason"].strip()
                or not isinstance(entry["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"])):
            raise ValueError("synthetic exceptions require an exact fixture, permitted rule, digest and reason")
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("synthetic exception content changed; remove it or independently review the changed fixture")
        if (name, rule) in exceptions:
            raise ValueError("duplicate synthetic exception")
        exceptions.add((name, rule))
    violations = []
    allowed_hits = {}
    rule_hits = []
    accepted_synthetic_hits = []
    raw_match_count = 0
    for name in sorted(names - {""}):
        path = root / name
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        hits = {(number, rule) for number, line in enumerate(content.splitlines(), 1)
                for rule in _line_rules(line, profile == "mobile-synthetic")}
        hits.update((number, "private_term") for number, line in enumerate(content.splitlines(), 1)
                    if any(term in line.casefold() for term in terms))
        hits.update(_semantic_hits(name, content))
        bad_lines, legacy_lines = set(), set()
        display_name = _display_path(name, terms)
        for number, rule in sorted(hits):
            raw_match_count += 1
            record = {"path": display_name, "line": number, "rule": rule}
            if name in fixtures and rule in LEGACY_FIXTURE_RULES:
                legacy_lines.add(number)
            elif (name, rule) in exceptions:
                accepted_synthetic_hits.append(record)
            else:
                bad_lines.add(number)
                rule_hits.append(record)
        if legacy_lines:
            allowed_hits[display_name] = len(legacy_lines)
        if bad_lines:
            violations.append({"path": display_name, "lines": sorted(bad_lines)})
    return {"passed": not violations, "violations": violations, "fixture_hits": allowed_hits,
            "rule_hits": rule_hits, "accepted_synthetic_hits": accepted_synthetic_hits,
            "raw_match_count": raw_match_count, "unexcepted_match_count": len(rule_hits),
            "private_terms": terms_receipt}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--allowlist", type=Path)
    parser.add_argument("--private-terms-file", type=Path,
                        help="private JSON string array outside the checkout; values are never printed")
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        result = check(root, args.allowlist or root / "configs/public_fixture_allowlist.json", args.private_terms_file)
    except ValueError as exc:
        parser.error(str(exc))
    except (OSError, subprocess.CalledProcessError):
        parser.error("cannot read residue guard inputs or tracked source")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
