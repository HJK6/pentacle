"""Pinned, local Codex startup input for the existing fleet smoke runner.

The private tuple file contains public Binding metadata and config hashes,
never credentials. Provisioning and candidate-daemon ownership stay with the
caller; this probe neither enrolls an account nor changes its default.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from machines import configured_local_host
from provider_accounts import AccountError, Binding, _codex_config
from provider_account_native import codex_observation, verify_binary


class ProviderStartupProbe:
    def __init__(self, path: Path, host: str):
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or set(value) != {"binding", "config_sha256", "model", "effort"}:
            raise ValueError("provider tuple requires binding, config_sha256, model and effort")
        self.binding = Binding.from_public(value["binding"])
        self.config_sha256 = value["config_sha256"]
        self.model, self.effort = value["model"], value["effort"]
        if (self.binding.provider != "codex" or self.binding.host != host
                or host != configured_local_host()
                or not self.binding.enabled or not self.binding.healthy
                or not self.binding.provider_id or not self.binding.pool_id
                or self.binding.default_revision != 0
                or not isinstance(self.config_sha256, str) or len(self.config_sha256) != 64
                or any(c not in "0123456789abcdef" for c in self.config_sha256)
                or not isinstance(self.model, str) or not self.model
                or not isinstance(self.effort, str) or not self.effort):
            raise ValueError("provider tuple must pin one healthy local Codex binding and launch tuple")

    def verify_inputs(self):
        verify_binary(self.binding)
        config = Path(self.binding.home) / "config.toml"
        content = config.read_bytes()
        if hashlib.sha256(content).hexdigest() != self.config_sha256:
            raise AccountError("provider_config_changed")
        _codex_config(content.decode("utf-8"))

    def observe(self):
        self.verify_inputs()
        observation = codex_observation(self.binding)
        if (observation["provider_id"] != self.binding.provider_id
                or observation["pool_id"] != self.binding.pool_id):
            raise AccountError("provider_identity_changed")
        self.verify_inputs()
        return {"provider": self.binding.provider, "account": self.binding.alias,
                "host": self.binding.host, "generation": self.binding.generation,
                "executable": self.binding.executable, "version": self.binding.version,
                "executable_sha256": self.binding.executable_sha256,
                "config_sha256": self.config_sha256, "native_identity": "matched"}

    def verify_spawn(self, result):
        actual = (result.get("session") or {}).get("account_binding")
        expected = self.binding.public()
        # The scratch daemon selects its configured default; only its revision
        # differs from the immutable enrollment receipt. Never send an account
        # override through the operator wire (that choice belongs to the FD).
        if (not isinstance(actual, dict)
                or {k: v for k, v in actual.items() if k != "default_revision"}
                != {k: v for k, v in expected.items() if k != "default_revision"}):
            raise AccountError("provider_launch_binding_mismatch")
        session = result.get("session") or {}
        if session.get("effective_model") != self.model or session.get("effective_effort") != self.effort:
            raise AccountError("provider_launch_tuple_mismatch")
        self.verify_inputs()
