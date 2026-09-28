#!/usr/bin/env python3
"""Unit test for check_public_residue's detectors (run under Public checks).

Standalone (no pytest dependency). Sample addresses are assembled from parts so
this test file itself carries no literal CGNAT address or anonymizer token that
the checker would (correctly) flag — the same trick check_public_residue.py uses
for its own patterns. Run: `python3 scripts/test_check_public_residue.py`.
"""
from __future__ import annotations

import importlib.util
from contextlib import contextmanager
import os
import json
import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

_mod_path = Path(__file__).resolve().with_name("check_public_residue.py")
_spec = importlib.util.spec_from_file_location("check_public_residue", _mod_path)
cpr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cpr)

_P = "100."  # not a CGNAT address on its own (not four octets)


def _cgnat(second: int, rest: str) -> str:
    return _P + f"{second}.{rest}"


@contextmanager
def isolated_git_environment():
    # Hooks export repository-selection variables. Fixture Git commands must
    # select their owned temporary repository, then restore the caller context.
    saved = {key: value for key, value in os.environ.items() if key.startswith("GIT_")}
    for key in saved:
        os.environ.pop(key)
    try:
        yield
    finally:
        os.environ.update(saved)


def _reject(call):
    try:
        call()
    except ValueError:
        return
    raise AssertionError("invalid guard input accepted")


def _portable_and_semantic_rules():
    examples = {
        "private_home_path": "/" + "Users/fixture-person/project",
        "credential": "AK" + "IA" + "X" * 16,
        "personal_email": "fixture-person" + "@" + "gmail.com",
        "private_endpoint": "ws://" + "10.2.3.4:7796",
    }
    for rule, value in examples.items():
        assert rule in cpr._line_rules(value), rule
    for value in ("/home/example/project", "/Users/<user>/project", "$HOME/project",
                  "https://203.0.113.10:7796", "ws://127.0.0.1:7796"):
        assert not cpr._line_rules(value), value
    semantic = {
        "fixed_host_exclusions": 'EXCLUDED_HOSTS = {"retired-example"}\n',
        "fixed_permission_spec": 'RECOVERY = "spec_example__recovery"\ndef authorized(row):\n    return row.get("spec_id") == RECOVERY\n',
        "fixed_pin_owner": 'if pin["owner"] != "local:lead":\n    raise ValueError()\n',
        "fixed_satellite_inventory": '_required(runtime["satellites"], {"worker-one"}, "satellites")\n',
        "fixed_prechange_hosts": '_required(top["receipts"], {"prechange_local"}, "receipts")\n',
        "fixed_assistant_presentation_id": 'if (streamId === "local:assistant") return false;\n',
    }
    for rule, source in semantic.items():
        name = "runtime.js" if rule.endswith("presentation_id") else "runtime.py"
        assert any(hit[1] == rule for hit in cpr._semantic_hits(name, source)), rule
        assert not cpr._semantic_hits("tests/" + name, source), "synthetic test mistaken for runtime"
    assert not cpr._semantic_hits("runtime.js", 'if (bubbleType === "bubble:assistant") return true;')
    assert not cpr._semantic_hits("runtime.py", 'record["spec_source"] == "spec_example__note"\n')
    assert not cpr._semantic_hits("runtime.py", 'EXCLUDED_HOSTS = ()\n_required(runtime["satellites"], configured_hosts, "satellites")\n')


def _external_dictionary_and_exceptions():
    with isolated_git_environment(), tempfile.TemporaryDirectory() as temporary:
        parent = Path(temporary)
        root = parent / "checkout"
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        source = root / "source.txt"
        source.write_text("clean public example\n")
        subprocess.run(["git", "-C", str(root), "add", "source.txt"], check=True)
        allowlist = parent / "allowlist.json"
        manifest = {"version": 1, "fixtures": {}}
        allowlist.write_text(json.dumps(manifest))
        terms = parent / "terms.json"
        term = "InventedNebulaPerson"
        terms.write_text(json.dumps([term]))

        def cli():
            return subprocess.run([sys.executable, str(_mod_path), "--root", str(root),
                                   "--allowlist", str(allowlist), "--private-terms-file", str(terms)],
                                  capture_output=True, text=True, check=False)

        clean = cli()
        assert clean.returncode == 0, clean.stderr
        receipt = json.loads(clean.stdout)["private_terms"]
        assert receipt == {"status": "checked", "count": 1,
                           "sha256": hashlib.sha256(terms.read_bytes()).hexdigest()}
        assert cpr.check(root, allowlist)["private_terms"]["status"] == "not_run"
        # Apply a real tracked-file mutation, prove scanner RED, then remove the
        # same mutation and prove GREEN against the unchanged external input.
        source.write_text("clean public example\n" + term.swapcase() + "\n")
        assert term.casefold() in source.read_text().casefold(), "mutation was not applied"
        red = cli()
        assert red.returncode == 1, red.stderr
        result = json.loads(red.stdout)
        assert result["rule_hits"] == [{"path": "source.txt", "line": 2, "rule": "private_term"}]
        assert result["raw_match_count"] == result["unexcepted_match_count"] == 1
        assert term.casefold() not in (red.stdout + red.stderr).casefold(), "dictionary value leaked"
        source.write_text("clean public example\n")
        assert cli().returncode == 0
        assert terms.read_bytes() == json.dumps([term]).encode(), "dictionary changed during proof"

        # The path of a matching tracked file is also private.
        private_source = root / (term + ".txt")
        source.rename(private_source)
        private_source.write_text(term + "\n")
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        private_result = cli()
        assert private_result.returncode == 1
        assert term.casefold() not in (private_result.stdout + private_result.stderr).casefold()
        assert json.loads(private_result.stdout)["rule_hits"][0]["path"].startswith("<private-path:")
        private_source.unlink()
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        (root / "terms.json").write_text(terms.read_text())
        _reject(lambda: cpr.check(root, allowlist, root / "terms.json"))
        for invalid in ([], {}, [""], [1], [term, term.lower()], "invalid"):
            terms.write_text(json.dumps(invalid))
            _reject(lambda: cpr.check(root, allowlist, terms))
        terms.write_text("invalid JSON")
        assert cli().returncode == 2
        terms.write_text(json.dumps([term]))

        fixture = root / "tests" / "historical.txt"
        fixture.parent.mkdir()
        fixture.write_text(term + "\n")
        subprocess.run(["git", "-C", str(root), "add", "tests"], check=True)
        exception = {"path": "tests/historical.txt", "rule": "private_term",
                     "sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
                     "reason": "Frozen synthetic fixture under separate normalization work."}
        manifest["synthetic_exceptions"] = [exception]
        allowlist.write_text(json.dumps(manifest))
        accepted = cpr.check(root, allowlist, terms)
        assert accepted["passed"] and accepted["raw_match_count"] == 1
        assert accepted["unexcepted_match_count"] == 0 and len(accepted["accepted_synthetic_hits"]) == 1
        in_checkout = root / "allowlist.json"
        in_checkout.write_text(allowlist.read_text())
        _reject(lambda: cpr.check(root, in_checkout, terms))
        fixture.write_text(fixture.read_text() + "changed\n")
        _reject(lambda: cpr.check(root, allowlist, terms))
        fixture.write_text(term + "\n")
        for rule in ("credential", "fixed_permission_spec", "fixed_pin_owner", "fixed_satellite_inventory"):
            manifest["synthetic_exceptions"] = [{**exception, "rule": rule}]
            allowlist.write_text(json.dumps(manifest))
            _reject(lambda: cpr.check(root, allowlist, terms))
        manifest["synthetic_exceptions"] = [{**exception, "path": "source.txt"}]
        allowlist.write_text(json.dumps(manifest))
        _reject(lambda: cpr.check(root, allowlist, terms))
        # An old fixture allowlist cannot hide a newly recognized credential.
        manifest = {"version": 1, "fixtures": {"tests/historical.txt": "Synthetic test."}}
        fixture.write_text("AK" + "IA" + "X" * 16 + "\n")
        allowlist.write_text(json.dumps(manifest))
        assert cpr.check(root, allowlist)["rule_hits"][0]["rule"] == "credential"

        # Each semantic mutation must fail through the actual tracked scanner.
        fixture.write_text("clean\n")
        mutations = [
            ("runtime.py", 'EXCLUDED_HOSTS = {"retired-example"}\n', "fixed_host_exclusions", 1),
            ("runtime.py", 'def authorized(row):\n    return row["spec_id"] == "spec_example__recovery"\n', "fixed_permission_spec", 2),
            ("runtime.py", 'if pin["owner"] != "local:lead":\n    raise ValueError()\n', "fixed_pin_owner", 1),
            ("runtime.py", '_required(runtime["satellites"], {"worker-one"}, "satellites")\n', "fixed_satellite_inventory", 1),
            ("runtime.py", '_required(top["receipts"], {"prechange_local"}, "receipts")\n', "fixed_prechange_hosts", 1),
            ("runtime.js", 'if (streamId === "local:assistant") return false;\n', "fixed_assistant_presentation_id", 1),
        ]
        for name, mutation, rule, line in mutations:
            runtime = root / name
            runtime.write_text(mutation)
            subprocess.run(["git", "-C", str(root), "add", name], check=True)
            assert runtime.read_text() == mutation
            red = cpr.check(root, allowlist)
            assert red["rule_hits"] == [{"path": name, "line": line, "rule": rule}]
            runtime.write_text("// clean\n" if name.endswith(".js") else "# clean\n")
            assert cpr.check(root, allowlist)["passed"]


def main() -> int:
    _portable_and_semantic_rules()
    _external_dictionary_and_exceptions()
    # In-range (second octet 64-127) must be flagged, anywhere on the line.
    hits = [
        _cgnat(64, "0.0"), _cgnat(64, "0.1"), _cgnat(80, "28.24"),
        _cgnat(70, "128.35"), _cgnat(127, "255.255"),
        "  --bind " + _cgnat(96, "10.10") + " --port 7796",
        "host: '" + _cgnat(111, "2.3") + "'",
    ]
    for s in hits:
        assert cpr.CGNAT_PATTERN.search(s), f"expected CGNAT hit: {s!r}"
        assert cpr._line_hits(s), f"expected _line_hits: {s!r}"

    # Outside the range, loopback, private, and RFC5737 doc ranges: no flag.
    misses = [
        _cgnat(63, "255.255"),        # just below
        _cgnat(128, "0.0"),           # just above
        _cgnat(200, "1.1"),           # second octet > 127
        "10.0.0.1", "192.168.1.5", "127.0.0.1", "0.0.0.0",
        "198.51.100.24", "203.0.113.7",
        "1" + _cgnat(64, "0.1"),      # leading digit -> not a boundary
        _cgnat(64, "0.1") + ".5",     # trailing dotted digit
    ]
    for s in misses:
        assert not cpr.CGNAT_PATTERN.search(s), f"unexpected CGNAT hit: {s!r}"

    # The anonymizer-residue pattern still fires and _line_hits unions both.
    anon = "host" + "a"
    assert cpr.PATTERN.search(anon), "anonymizer pattern regressed"
    assert cpr._line_hits("something " + "host" + "b here")
    assert not cpr._line_hits("a perfectly clean line 10.0.0.1")

    for word in ["host" + suffix for suffix in ("a", "b", "c", "config", "admission", "busy")]:
        assert cpr._line_hits(word), "default web detector changed"
        assert not cpr._line_hits(word, True), "fixed mobile vocabulary rejected"
        assert not cpr._line_hits("optimistic_" + word + "_one", True), "underscore boundary rejected"
    assert cpr._line_hits("host" + "alpha", True), "non-approved residue exempted"
    assert cpr._line_hits("ab" + "ra", True), "other anonymizer exempted"
    for address in hits:
        assert cpr._line_hits("host" + "a " + address, True), "mobile profile masked address"

    with isolated_git_environment(), tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        source = root / "source.txt"
        source.write_text("id_" + "host" + "b_fixture " + "host" + "config")
        subprocess.run(["git", "-C", str(root), "add", "source.txt"], check=True)
        allowlist = root / "allowlist.json"
        allowlist.write_text(json.dumps({"version": 1, "profile": "mobile-synthetic", "fixtures": {}}))
        assert cpr.check(root, allowlist)["passed"]
        source.write_text(source.read_text() + " " + hits[0])
        assert not cpr.check(root, allowlist)["passed"], "profile hid original address"
        allowlist.write_text(json.dumps({"version": 1, "profile": "arbitrary", "fixtures": {}}))
        try:
            cpr.check(root, allowlist)
        except ValueError:
            pass
        else:
            raise AssertionError("unknown profile accepted")

    print("ok - residue guard: portable rules, external dictionary RED/GREEN, frozen exceptions, default web/mobile")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
