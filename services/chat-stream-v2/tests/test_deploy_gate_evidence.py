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


# -- read-timing receipt (spec_pentacle__prerelease_live_read_timing_2026_11) --

def read_timing_receipt(sha=SHA):
    """A passing budget-version-1 receipt, shaped as tools/read_timing.py writes it."""
    return {
        "schema": "pentacle.v2.read-timing.v1", "budget_version": 1, "sha": sha,
        "source": {"sha_before": sha, "sha_after": sha, "clean_before": True, "clean_after": True},
        "verdict": "PASS", "passed": True, "refusals": [],
        "reads": [
            {"name": "list_open_sessions_with_event_summary", "verdict": "PASS", "seconds": 2.6,
             "statements": 40, "budget_s": 3.0},
            {"name": "fetch_session_event_page", "verdict": "PASS", "seconds": 0.5, "statements": 9,
             "budget_s": 1.0, "events": 200, "user_or_tell": 31},
            {"name": "list_open_usage_fence_rows", "verdict": "PASS", "seconds": 0.004, "statements": 2,
             "budget_s": 0.1, "expected_hosts": ["host-a", "host-b"],
             "hosts": [{"host": "host-a", "verdict": "PASS", "seconds": 0.004, "statements": 1},
                       {"host": "host-b", "verdict": "PASS", "seconds": 0.003, "statements": 1}]},
        ],
    }


def _load_timing(tmp_path, payload, *, raw=None):
    from deploy.deploy import _load_read_timing_receipt
    path = tmp_path / "read-timing.json"
    path.write_text(raw if raw is not None else json.dumps(payload), encoding="utf-8")
    return _load_read_timing_receipt(path, SHA)


def test_passing_read_timing_receipt_is_admitted(tmp_path):
    payload = read_timing_receipt()
    assert _load_timing(tmp_path, payload) == payload


def _read(payload, name):
    return next(row for row in payload["reads"] if row["name"] == name)


def _drop_read(p):
    p["reads"].pop(0)


def _fail_read(p):
    _read(p, "list_open_sessions_with_event_summary").update(verdict="FAIL", seconds=15.2)


def _unevaluated_read(p):
    _read(p, "list_open_usage_fence_rows").update(verdict="NOT_EVALUATED")


def _duplicate_read(p):
    p["reads"].append(dict(p["reads"][0]))


def _host_without_row(p):
    _read(p, "list_open_usage_fence_rows")["hosts"].pop()


def _host_failed(p):
    _read(p, "list_open_usage_fence_rows")["hosts"][1].update(verdict="FAIL", seconds=0.4)


def _timing(name, value):
    return lambda p: _read(p, name).update(seconds=value)


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(schema="pentacle.v2.gate-evidence.v1"),
    lambda p: p.update(budget_version=2),
    lambda p: p.update(budget_version=True),
    lambda p: p.update(sha="d" * 40),
    lambda p: p["source"].update(sha_after="d" * 40),
    lambda p: p["source"].update(clean_before=False),
    lambda p: p["source"].update(clean_after=False),
    lambda p: p.update(passed=False),
    lambda p: p.update(verdict="FAIL"),
    _drop_read, _fail_read, _unevaluated_read, _duplicate_read, _host_without_row, _host_failed,
    _timing("list_open_sessions_with_event_summary", float("nan")),
    _timing("list_open_sessions_with_event_summary", float("inf")),
    _timing("fetch_session_event_page", -0.1),
    _timing("fetch_session_event_page", "0.5"),
    _timing("fetch_session_event_page", True),
    _timing("list_open_sessions_with_event_summary", 3.01),
    _timing("fetch_session_event_page", 1.5),
    lambda p: _read(p, "list_open_usage_fence_rows")["hosts"][0].update(seconds=0.2),
    lambda p: _read(p, "list_open_usage_fence_rows")["hosts"][0].update(seconds=float("nan")),
    lambda p: _read(p, "list_open_usage_fence_rows").update(expected_hosts=[]),
    lambda p: _read(p, "list_open_usage_fence_rows")["hosts"].append(
        {"host": "host-a", "verdict": "PASS", "seconds": 0.001}),
    lambda p: _read(p, "fetch_session_event_page").update(events=12),
    lambda p: _read(p, "fetch_session_event_page").update(user_or_tell=3),
    lambda p: _read(p, "list_open_sessions_with_event_summary").update(statements=0),
], ids=["wrong_schema", "wrong_budget_version", "bool_budget_version", "wrong_sha", "source_sha_after",
        "dirty_before", "dirty_after", "passed_false", "verdict_fail", "read_absent", "read_failed",
        "read_not_evaluated", "duplicate_read", "host_without_row", "host_failed", "nan_timing",
        "inf_timing", "negative_timing", "string_timing", "bool_timing", "inventory_over_budget",
        "history_over_budget", "host_over_budget", "host_nan_timing", "no_expected_hosts",
        "duplicate_host_row", "short_page", "page_without_user_or_tell", "no_statements"])
def test_read_timing_receipt_refusals(tmp_path, mutate):
    payload = read_timing_receipt()
    mutate(payload)
    with pytest.raises(DeployError, match="^(read-timing receipt refused|invalid read-timing receipt): "):
        _load_timing(tmp_path, payload)


def test_missing_or_invalid_read_timing_receipt_is_refused(tmp_path):
    from deploy.deploy import _load_read_timing_receipt
    with pytest.raises(DeployError, match="requires --read-timing-receipt"):
        _load_read_timing_receipt(None, SHA)
    with pytest.raises(DeployError, match="^invalid read-timing receipt"):
        _load_read_timing_receipt(tmp_path / "absent.json", SHA)
    with pytest.raises(DeployError, match="^invalid read-timing receipt"):
        _load_timing(tmp_path, None, raw="{not json")
    with pytest.raises(DeployError, match="^read-timing receipt refused"):
        _load_timing(tmp_path, None, raw="[]")


def test_deploy_parser_has_receipt_argument_and_no_bypass():
    from deploy.deploy import build_parser
    options = {option for action in build_parser()._actions for option in action.option_strings}
    assert "--read-timing-receipt" in options
    assert not {option for option in options if "timing" in option} - {"--read-timing-receipt"}


@pytest.mark.parametrize("prepare_only", [False, True], ids=["apply", "prepare"])
def test_coordinated_forward_needs_receipt_and_rollback_does_not(tmp_path, monkeypatch, prepare_only):
    from deploy import coordinated, coordinated_runtime
    import deploy.deploy as module
    calls = []

    class Transaction:
        def __init__(self, _adapter, _root):
            pass

        def apply(self, _plan, _receipt):
            calls.append("apply")
            return {}

        def prepare(self, _plan, _receipt):
            calls.append("prepare")
            return {}

        def rollback(self, _receipt, **_kwargs):
            calls.append("rollback")
            return {}

    monkeypatch.setattr(coordinated, "Transaction", Transaction)
    monkeypatch.setattr(coordinated, "validate_intent", lambda _path: {"target_sha": SHA})
    monkeypatch.setattr(coordinated_runtime, "Runtime", lambda *_a, **_k: None)
    service = module.SERVICES["chat-streamd-v2"]
    (tmp_path / "release").mkdir()
    common = {"release_checkout": tmp_path / "release", "ref": "origin/main",
              "coordinated_receipt": tmp_path / "receipt.json", "runner": module._run}
    forward = {**common, "coordinated_plan": tmp_path / "intent.json", "prepare_only": prepare_only}
    other = tmp_path / "other.json"
    other.write_text(json.dumps(read_timing_receipt("d" * 40)))
    for receipt, message in ((None, "requires --read-timing-receipt"), (other, "sha does not match deploy sha")):
        with pytest.raises(DeployError, match=message):
            module.deploy(service, **forward, read_timing_receipt=receipt)
    assert calls == []
    good = tmp_path / "good.json"
    good.write_text(json.dumps(read_timing_receipt()))
    module.deploy(service, **forward, read_timing_receipt=good)
    module.deploy(service, **common, rollback=True)
    assert calls == ["prepare" if prepare_only else "apply", "rollback"]
