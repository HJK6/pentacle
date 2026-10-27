"""Pinned, local Codex startup input for the existing fleet smoke runner.

The private tuple file contains public Binding metadata and config hashes,
never credentials. Provisioning and candidate-daemon ownership stay with the
caller; this probe neither enrolls an account nor changes its default.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tomllib

from machines import configured_local_host
from provider_accounts import AccountError, Binding
from provider_account_native import codex_observation, verify_binary


def _config_identity(config, model):
    # Preserve TOML types as well as values (Python considers True == 1).
    def typed(value):
        if isinstance(value, dict):
            return ["dict", [[key, typed(item)] for key, item in sorted(value.items())]]
        if isinstance(value, list):
            return ["list", [typed(item) for item in value]]
        return [type(value).__name__, repr(value)]

    # Only this existing leaf is omitted from the effective identity. Its
    # presence, integer type and monotonic transition are checked separately.
    tui = config.get("tui")
    nux = tui.get("model_availability_nux") if isinstance(tui, dict) else None
    counter = nux.get(model) if isinstance(nux, dict) else None
    if isinstance(nux, dict) and model in nux:
        if type(counter) is not int or counter < 0:
            raise AccountError("provider_config_invalid")
        nux = dict(nux, **{model: 0})
        config = dict(config, tui=dict(tui, model_availability_nux=nux))
    return hashlib.sha256(json.dumps(typed(config), separators=(",", ":")).encode()).hexdigest(), counter


class ProviderStartupProbe:
    def __init__(self, path: Path, host: str):
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or set(value) != {"binding", "config_sha256", "model", "effort"}:
            raise ValueError("provider tuple requires binding, config_sha256, model and effort")
        self.binding = Binding.from_public(value["binding"])
        self.config_sha256 = value["config_sha256"]
        self.model, self.effort = value["model"], value["effort"]
        self.config_comparison = {}
        self._last_config_sha256 = None
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

    def verify_inputs(self, *, initial=False):
        config = Path(self.binding.home) / "config.toml"
        try:
            content = config.read_bytes()
        except OSError:
            raise AccountError("provider_config_unavailable") from None
        raw_sha256 = hashlib.sha256(content).hexdigest()
        self.config_comparison.update(raw_before_sha256=self.config_sha256,
                                      raw_after_sha256=raw_sha256,
                                      allowed_field=f"tui.model_availability_nux.{self.model}",
                                      matched=False, comparison="refused",
                                      effective_after_sha256=None)
        first = initial or self._last_config_sha256 is None
        if first and raw_sha256 != self.config_sha256:
            raise AccountError("provider_config_changed")
        try:
            parsed = tomllib.loads(content.decode("utf-8"))
        except (UnicodeError, tomllib.TOMLDecodeError):
            raise AccountError("provider_config_invalid") from None
        effective_sha256, counter = _config_identity(parsed, self.model)
        self.config_comparison["effective_after_sha256"] = effective_sha256
        if first:
            self._initial_config = parsed
            self._effective_config_sha256 = effective_sha256
            self.config_comparison["effective_before_sha256"] = effective_sha256
        elif (effective_sha256 != self._effective_config_sha256
              or (raw_sha256 != self._last_config_sha256
                  and (counter is None or self._last_nux_counter is None
                       or counter <= self._last_nux_counter))):
            raise AccountError("provider_config_changed")
        self._last_config_sha256, self._last_nux_counter = raw_sha256, counter
        self.config_comparison.update(matched=True, comparison=(
            "exact_raw_pin" if raw_sha256 == self.config_sha256 else "allowed_native_nux_increase"))
        verify_binary(self.binding)
        # Observe the owner's frozen, already provisioned config. Native UI
        # sections are valid here; bootstrap policy belongs to enrollment.

    def observe(self):
        self.verify_inputs(initial=True)
        observation = codex_observation(self.binding)
        if (observation["provider_id"] != self.binding.provider_id
                or observation["pool_id"] != self.binding.pool_id):
            raise AccountError("provider_identity_changed")
        self.verify_inputs()
        return {"provider": self.binding.provider, "account": self.binding.alias,
                "host": self.binding.host, "generation": self.binding.generation,
                "executable": self.binding.executable, "version": self.binding.version,
                "executable_sha256": self.binding.executable_sha256,
                "config_sha256": self.config_sha256, "native_identity": "matched",
                "config_comparison": self.config_comparison}

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
