"""Exercise the daemon contract and local owner configuration at the CLI boundary."""
from __future__ import annotations

import importlib.util
import json
import os
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

    def test_local_structure_owner_config_is_optional_and_validated_at_cli_boundary(self) -> None:
        temporary, root, service, base = self._fixture()
        try:
            home = root / "home"
            config_path = home / ".config" / "pentacle" / "daemon-contract.json"
            missing = self._run_fixture_gate(root, service, base, home=home)
            self._record_result("local-owner-missing-cli-pass", base, missing)
            self.assertEqual(missing.returncode, 0, missing.stdout + missing.stderr)
            self.assertIn("pending local configuration", missing.stdout)

            config_path.parent.mkdir(parents=True)
            identity = "example:v2-seat"
            config_path.write_text(json.dumps({"structure_owner": identity}), encoding="utf-8")
            valid = self._run_fixture_gate(root, service, base, home=home)
            self._record_result("local-owner-valid-cli-pass", base, valid)
            self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)
            self.assertIn("local structure owner is configured", valid.stdout)
            self.assertNotIn(identity, valid.stdout + valid.stderr)

            config_path.write_text(json.dumps({"structure_owner": "invalid"}), encoding="utf-8")
            invalid = self._run_fixture_gate(root, service, base, home=home)
            self._record_result("local-owner-invalid-cli-reject", base, invalid)
            self.assertNotEqual(invalid.returncode, 0, invalid.stdout + invalid.stderr)
            self.assertIn("valid structure_owner", invalid.stderr)
            self.assertNotIn('"invalid"', invalid.stdout + invalid.stderr)
        finally:
            temporary.cleanup()

    def _fixture(self) -> tuple[tempfile.TemporaryDirectory, Path, Path, str]:
        temporary = tempfile.TemporaryDirectory(prefix="daemon-contract-gate-")
        root = Path(temporary.name)
        service = root / "services" / "chat-stream-v2"
        tools = service / "tools"
        tools.mkdir(parents=True)
        shutil.copy2(daemon_gate.__file__, tools / "daemon_contract_gate.py")
        shutil.copytree(Path(daemon_gate.__file__).resolve().parents[1] / "daemon_contract",
                        service / "daemon_contract")
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

    def _run_fixture_gate(self, root: Path, service: Path, base: str, *,
                          home: Path | None = None) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        if home is not None:
            environment["HOME"] = str(home)
        return subprocess.run(
            [sys.executable, str(service / "tools" / "daemon_contract_gate.py"), "--base", base],
            cwd=root, env=environment, text=True, capture_output=True, check=False,
        )

    def test_checker_cli_passes_frozen_baseline_control(self) -> None:
        temporary, root, service, base = self._fixture()
        try:
            result = self._run_fixture_gate(root, service, base)
            self._record_result("baseline-pass", base, result)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        finally:
            temporary.cleanup()

if __name__ == "__main__":
    unittest.main()
