import json
from pathlib import Path

from _shared import pytest_conftest, test_resource_ledger


def test_live_test_admission_requires_explicit_host_allowlist(monkeypatch):
    monkeypatch.delenv("PENTACLE_LIVE_TEST_HOSTS", raising=False)
    assert not pytest_conftest._hostname_is_allowed_host("workstation.local")
    assert not pytest_conftest._host_id_is_allowed("workstation")
    monkeypatch.setenv("PENTACLE_LIVE_TEST_HOSTS", "workstation, coordinator")
    assert pytest_conftest._hostname_is_allowed_host("workstation.local")
    assert pytest_conftest._host_id_is_allowed("coordinator")
    assert not pytest_conftest._host_id_is_allowed("unconfigured")


def test_resource_ledger_local_alias_is_configured(monkeypatch):
    monkeypatch.delenv("PENTACLE_TEST_LOCAL_HOST", raising=False)
    assert test_resource_ledger._canonical_host("localhost") == "local"
    assert test_resource_ledger._canonical_host("workstation") == "workstation"
    monkeypatch.setenv("PENTACLE_TEST_LOCAL_HOST", "workstation")
    assert test_resource_ledger._canonical_host("workstation") == "local"


def test_shipped_boot_limits_have_no_host_overrides():
    path = Path(__file__).resolve().parents[2] / "_shared/spawn_defaults.json"
    config = json.loads(path.read_text())
    assert config["host_overrides"] == {}
    assert config["max_concurrent_boots"] == 3
