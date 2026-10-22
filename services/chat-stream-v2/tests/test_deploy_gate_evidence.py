from __future__ import annotations

import json

import pytest

from deploy.deploy import DeployError, _load_gate_evidence


SHA = "c" * 40


def _row(tier, **overrides):
    row = {
        "tier": tier,
        "command": [],
        "returncode": 0,
        "timed_out": False,
        "counts": {"tests": 3, "failures": 0, "errors": 0, "skipped": 0},
        "passed": True,
        "reason": None,
    }
    row.update(overrides)
    return row


def _evidence(tiers):
    return {
        "schema": "pentacle.v2.gate-evidence.v1",
        "gate": "merge",
        "sha": SHA,
        "source": {
            "sha_before": SHA, "sha_after": SHA,
            "clean_before": True, "clean_after": True,
        },
        "tiers": tiers,
        "passed": True,
    }


def _load(tmp_path, payload):
    path = tmp_path / "gate.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return _load_gate_evidence(path, SHA)


@pytest.mark.parametrize("tiers", [
    [_row("unit"), _row("smoke")],
    [{"tier": "unit", "passed": True}, {"tier": "smoke", "passed": True}],
    [_row("unit"), _row("smoke"), _row("usage_manifest", counts=None)],
], ids=["canonical", "legacy_minimal", "usage_manifest"])
def test_valid_gate_evidence_loads(tmp_path, tiers):
    payload = _evidence(tiers)
    assert _load(tmp_path, payload) == payload


@pytest.mark.parametrize("tiers, message", [
    ([_row("unit", returncode=1), _row("smoke")], "tier unit contradicts passed"),
    ([_row("unit"), _row("smoke", timed_out=True)], "tier smoke contradicts passed"),
    ([_row("unit", counts={"failures": 1, "errors": 0}), _row("smoke")],
     "tier unit contradicts passed"),
    ([_row("unit"), _row("smoke", counts={"failures": 0, "errors": 2})],
     "tier smoke contradicts passed"),
    ([_row("unit"), _row("unit", passed=False, returncode=1), _row("smoke")],
     "repeats tier unit"),
    ([_row("unit"), _row("unit"), _row("smoke")], "repeats tier unit"),
    ([_row("unit"), _row("smoke"), _row("source_integrity", passed=False, returncode=125)],
     "top-level pass contradicts failing tier source_integrity"),
    ([_row("unit"), _row("smoke"), None], "tier row is malformed"),
    ([_row("unit", returncode=True), _row("smoke")], "tier unit contradicts passed"),
], ids=["rc_nonzero", "timed_out", "count_failures", "count_errors",
        "duplicate_conflicting_tier", "duplicate_passing_tier", "failed_extra_row",
        "non_dict_row", "bool_returncode"])
def test_contradictory_gate_evidence_is_refused(tmp_path, tiers, message):
    with pytest.raises(DeployError, match="^gate evidence " + message + "$"):
        _load(tmp_path, _evidence(tiers))


@pytest.mark.parametrize("overrides", [
    {"tier": ""}, {"tier": None}, {"tier": 1},
    {"passed": 1}, {"passed": None},
    {"returncode": False}, {"returncode": 0.0}, {"returncode": None},
    {"timed_out": 0}, {"timed_out": None},
    {"counts": []}, {"counts": {}},
    {"counts": {"failures": False, "errors": 0}},
    {"counts": {"failures": 0, "errors": False}},
    {"counts": {"failures": 0.0, "errors": 0}},
])
def test_malformed_success_fields_are_refused(tmp_path, overrides):
    with pytest.raises(DeployError):
        _load(tmp_path, _evidence([_row("unit") | overrides, _row("smoke")]))


@pytest.mark.parametrize("field, value, message", [
    ("sha", "d" * 40, "gate evidence sha does not match deploy sha"),
    ("passed", False, "gate evidence is not passing"),
    ("tiers", [_row("unit")], "gate evidence is missing a passing v2 unit or smoke tier"),
])
def test_existing_refusals_keep_their_messages(tmp_path, field, value, message):
    payload = _evidence([_row("unit"), _row("smoke")])
    payload[field] = value
    with pytest.raises(DeployError, match="^" + message + "$"):
        _load(tmp_path, payload)
