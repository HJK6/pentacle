from __future__ import annotations

from _shared.test_resource_ledger import ResourceLedger


def test_agent_orch_resource_ledger_fixture_imports(resource_ledger: ResourceLedger) -> None:
    assert resource_ledger.test_id.endswith("test_agent_orch_resource_ledger_fixture_imports")
    assert resource_ledger.test_prefix.startswith("pentacle-test-")
