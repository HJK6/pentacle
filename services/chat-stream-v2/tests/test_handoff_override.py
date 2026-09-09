"""Regression coverage for v2 warn-and-proceed handoff tuple changes."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import logging

import pytest

import spawnctl
from spawnctl import SpawnCtl
from sessions import VerbError
from store import Store


SOURCE_STREAM = "hosta:codex-old"
SOURCE_TUPLE = {"provider": "codex", "model": "model-b", "effort": "high", "role": "qa"}
REQUESTED_TUPLE = {**SOURCE_TUPLE, "effort": "xhigh"}


def _message(**overrides):
    message = {"objective": "Exercise the existing spawn contract",
        "request_id": "spawn-handoff-warn-and-proceed",
        "handoff": True,
        "handoff_from_stream_id": SOURCE_STREAM,
        "provider": "codex",
        "model": "model-b",
        "effort": "xhigh",
        # A missing role is intentionally inherited from the retiring session.
        "spec_ids": ["example_spec__handoff_confirmation"],
    }
    message.update(overrides)
    return message


async def _ctl(source_tuple: dict[str, str] | None = None) -> tuple[Store, SpawnCtl]:
    source = source_tuple or SOURCE_TUPLE
    store = Store(":memory:")
    store.start()
    await store.open_session(
        "hosta",
        "codex-old",
        provider=source["provider"],
        effective_model=source["model"],
        effective_effort=source["effort"],
        role=source["role"],
    )
    return store, SpawnCtl(store, object(), tmux=object())


def test_changed_handoff_proceeds_with_a_named_warning(caplog) -> None:
    async def run() -> None:
        store, ctl = await _ctl()
        try:
            with caplog.at_level(logging.WARNING, logger="chat_streamd_v2.spawnctl"):
                resolved = await ctl._resolve_handoff(_message(), "hostb", name="v2-successor")
            assert resolved["effort"] == "xhigh"
            assert resolved["resolution_source"] == "handoff_warn_and_proceed"
            assert resolved["_handoff_model_change_warning"] == {
                "changed_fields": ["effort"],
                "source_tuple": SOURCE_TUPLE,
                "requested_tuple": REQUESTED_TUPLE,
            }
            assert "changed_fields=effort" in caplog.text
        finally:
            store.stop()

    asyncio.run(run())


def test_spawnctl_requires_warn_and_proceed_policy(monkeypatch) -> None:
    """The RPC path must enforce the resolved policy, not a hard-coded branch."""
    async def run() -> None:
        store, ctl = await _ctl()
        actual_resolve_handoff = spawnctl.resolve_handoff
        try:
            def stale_policy(**kwargs):
                resolved = actual_resolve_handoff(**kwargs)
                return replace(
                    resolved, policy={**resolved.policy, "tuple_change": "operator_confirm"}
                )

            monkeypatch.setattr(spawnctl, "resolve_handoff", stale_policy)
            with pytest.raises(VerbError) as exc:
                await ctl._resolve_handoff(_message(), "hostb", name="v2-successor")
            assert exc.value.code == "handoff_policy_invalid"
        finally:
            store.stop()

    asyncio.run(run())


def test_role_only_handoff_proceeds_with_a_named_warning() -> None:
    async def run() -> None:
        store, ctl = await _ctl()
        try:
            resolved = await ctl._resolve_handoff(
                _message(effort="high", role="nexus"),
                "hostb",
                name="v2-successor",
            )
            assert resolved["role"] == "nexus"
            assert resolved["_handoff_model_change_warning"]["changed_fields"] == ["role"]
        finally:
            store.stop()

    asyncio.run(run())


def test_drift_recovery_resolves_requested_tuple_without_a_ceremony() -> None:
    async def run() -> None:
        drifted_source = {
            "provider": "codex",
            "model": "gpt-5.6-luna",
            "effort": "low",
            "role": "nexus",
        }
        store, ctl = await _ctl(drifted_source)
        try:
            resolved = await ctl._resolve_handoff(
                _message(model="model-c", effort="xhigh", role="nexus"),
                "hostb",
                name="v2-successor",
            )
            assert {field: resolved[field] for field in ("provider", "model", "effort", "role")} == {
                "provider": "codex",
                "model": "model-c",
                "effort": "xhigh",
                "role": "nexus",
            }
            assert resolved["_handoff_model_change_warning"]["changed_fields"] == ["model", "effort"]
        finally:
            store.stop()

    asyncio.run(run())


def test_confirm_flag_suppresses_warning_and_accepts_old_cli_tuple_shape() -> None:
    async def run() -> None:
        store, ctl = await _ctl()
        try:
            # Older CLIs omitted role from nested audit tuples. Compatibility
            # requires accepting the flag without comparing that legacy data.
            resolved = await ctl._resolve_handoff(
                _message(
                    confirm_model_change=True,
                    handoff_model_change_override={
                        "flag": "--confirm-model-change",
                        "source": {key: SOURCE_TUPLE[key] for key in ("provider", "model", "effort")},
                        "requested": {key: REQUESTED_TUPLE[key] for key in ("provider", "model", "effort")},
                        "changed_fields": ["effort"],
                    },
                ),
                "hostb",
                name="v2-successor",
            )
            assert resolved["effort"] == "xhigh"
            assert "_handoff_model_change_warning" not in resolved
        finally:
            store.stop()

    asyncio.run(run())


def test_malformed_legacy_override_is_never_an_approval_gate() -> None:
    async def run() -> None:
        store, ctl = await _ctl()
        try:
            resolved = await ctl._resolve_handoff(
                _message(
                    confirm_model_change=True,
                    handoff_model_change_override={"flag": "--obsolete-confirmation"},
                ),
                "hostb",
                name="v2-successor",
            )
            assert resolved["effort"] == "xhigh"
            assert "_handoff_model_change_warning" not in resolved
        finally:
            store.stop()

    asyncio.run(run())


def test_resolved_warning_is_returned_in_spawn_metadata() -> None:
    async def run() -> None:
        store, ctl = await _ctl()
        try:
            _command, resolution, overrides = await ctl._resolve_launch(
                {**_message(command="true")},
                "hostb",
                "v2-successor",
            )
            assert resolution["handoff_model_change_warning"]["changed_fields"] == ["effort"]
            assert overrides["role"] == "qa"
        finally:
            store.stop()

    asyncio.run(run())


def test_unchanged_handoff_is_unaffected_by_confirmation_flag() -> None:
    async def run() -> None:
        store, ctl = await _ctl()
        try:
            resolved = await ctl._resolve_handoff(
                _message(effort="high", confirm_model_change=True),
                "hostb",
                name="v2-successor",
            )
            assert resolved["resolution_source"] == "handoff_inherited"
            assert resolved["role"] == "qa"
            assert "_handoff_model_change_warning" not in resolved
        finally:
            store.stop()

    asyncio.run(run())
