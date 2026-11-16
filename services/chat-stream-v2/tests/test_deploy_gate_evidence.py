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

def _timed(seconds, **coverage):
    """Warm-up then three samples; the summary is the samples' median."""
    invocations = [{"seconds": value, "statements": 3, **coverage} for value in seconds]
    return {"verdict": "PASS", "seconds": sorted(seconds[1:])[1],
            "warmup": invocations[0], "samples": invocations[1:]}


def read_timing_receipt(sha=SHA):
    """A passing budget-version-2 receipt, shaped as tools/read_timing.py writes it."""
    page = {"events": 200, "user_or_tell": 31}
    return {
        "schema": "pentacle.v2.read-timing.v1", "budget_version": 2, "sha": sha,
        "source": {"sha_before": sha, "sha_after": sha, "clean_before": True, "clean_after": True},
        "verdict": "PASS", "passed": True, "refusals": [],
        "reads": [
            {"name": "list_open_sessions_with_event_summary", "budget_s": 3.0,
             **_timed([6.9, 2.4, 2.9, 2.3], rows=40)},
            {"name": "fetch_session_event_page", "budget_s": 1.0, **_timed([0.4, 0.36, 0.37, 0.35], **page)},
            {"name": "list_open_usage_fence_rows", "verdict": "PASS", "budget_s": 0.1,
             "expected_hosts": ["host-a", "host-b"],
             "hosts": [{"host": "host-a", **_timed([0.03, 0.004, 0.005, 0.003], rows=2)},
                       {"host": "host-b", **_timed([0.02, 0.003, 0.002, 0.004], rows=0)}]},
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


INV, PAGE, FENCE = "list_open_sessions_with_event_summary", "fetch_session_event_page", "list_open_usage_fence_rows"


def _read(payload, name):
    return next(row for row in payload["reads"] if row["name"] == name)


def _drop_read(p):
    p["reads"].pop(0)


def _fail_read(p):
    _read(p, INV).update(verdict="FAIL")


def _unevaluated_read(p):
    _read(p, "list_open_usage_fence_rows").update(verdict="NOT_EVALUATED")


def _duplicate_read(p):
    p["reads"].append(dict(p["reads"][0]))


def _host_without_row(p):
    _read(p, "list_open_usage_fence_rows")["hosts"].pop()


def _host_failed(p):
    _read(p, FENCE)["hosts"][1].update(verdict="FAIL")


def _sample(name, index, host=None, **changes):
    def mutate(p):
        row = _read(p, name)
        if host is not None:
            row = row["hosts"][host]
        (row["warmup"] if index == "warmup" else row["samples"][index]).update(changes)
    return mutate


def _summary(name, **changes):
    return lambda p: _read(p, name).update(changes)


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(schema="pentacle.v2.gate-evidence.v1"),
    lambda p: p.update(budget_version=1),
    lambda p: p.update(budget_version=True),
    lambda p: p.update(sha="d" * 40),
    lambda p: p["source"].update(sha_after="d" * 40),
    lambda p: p["source"].update(clean_before=False),
    lambda p: p["source"].update(clean_after=False),
    lambda p: p.update(passed=False),
    lambda p: p.update(verdict="FAIL"),
    lambda p: p.update(refusals=["database outside the owned directory: /outside/sessions_archive.db"]),
    lambda p: p.pop("refusals"),
    _drop_read, _fail_read, _unevaluated_read, _duplicate_read, _host_without_row, _host_failed,
    _sample(INV, 0, seconds=float("nan")),
    _sample(INV, 1, seconds=float("inf")),
    _sample(PAGE, 2, seconds=-0.1),
    _sample(PAGE, 0, seconds="0.36"),
    _sample(PAGE, 0, seconds=True),
    _sample(INV, "warmup", seconds=float("nan")),
    _summary(INV, seconds=2.3),
    _summary(INV, seconds=float("nan")),
    lambda p: _read(p, INV).update(seconds=3.2, samples=[
        {"seconds": v, "statements": 3, "rows": 40} for v in (3.2, 3.3, 2.0)]),
    lambda p: _read(p, PAGE).update(seconds=1.4, samples=[
        {"seconds": v, "statements": 3, "events": 200, "user_or_tell": 31} for v in (1.4, 1.5, 0.3)]),
    lambda p: _read(p, INV)["samples"].pop(),
    lambda p: _read(p, INV)["samples"].append({"seconds": 2.0, "statements": 3, "rows": 40}),
    lambda p: _read(p, INV).pop("warmup"),
    lambda p: _read(p, INV).pop("samples"),
    lambda p: _read(p, FENCE)["hosts"][0].update(seconds=0.2, samples=[
        {"seconds": v, "statements": 3, "rows": 2} for v in (0.2, 0.3, 0.01)]),
    _sample(FENCE, 0, host=0, seconds=0.2),
    _sample(FENCE, 2, host=1, seconds=float("nan")),
    lambda p: _read(p, FENCE)["hosts"][0]["samples"].pop(),
    _sample(FENCE, 0, host=1, rows=None),
    lambda p: _read(p, FENCE).update(expected_hosts=[]),
    lambda p: _read(p, FENCE)["hosts"].append(dict(_read(p, FENCE)["hosts"][0])),
    _sample(PAGE, 1, events=12), _sample(PAGE, "warmup", events=0), _sample(PAGE, 2, user_or_tell=3),
    _sample(INV, 0, rows=0), _sample(INV, 1, statements=0), _sample(INV, "warmup", statements=0),
], ids=["wrong_schema", "single_sample_v1", "bool_budget_version", "wrong_sha", "source_sha_after",
        "dirty_before", "dirty_after", "passed_false", "verdict_fail", "recorded_refusal",
        "refusals_absent", "read_absent", "read_failed",
        "read_not_evaluated", "duplicate_read", "host_without_row", "host_failed", "nan_sample",
        "inf_sample", "negative_sample", "string_sample", "bool_sample", "nan_warm_up",
        "summary_not_median", "nan_summary", "median_over_budget", "history_median_over_budget",
        "two_samples", "four_samples", "no_warm_up", "no_samples", "host_median_over_budget",
        "host_sample_contradicts_summary", "host_nan_sample", "host_two_samples", "host_sample_without_output",
        "no_expected_hosts", "duplicate_host_row", "short_sample_page", "short_warm_up_page",
        "sample_without_user_or_tell", "inventory_sample_empty", "sample_without_statements",
        "warm_up_without_statements"])
def test_read_timing_receipt_refusals(tmp_path, mutate):
    payload = read_timing_receipt()
    mutate(payload)
    with pytest.raises(DeployError, match="^(read-timing receipt refused|invalid read-timing receipt): "):
        _load_timing(tmp_path, payload)


def test_read_timing_receipt_optional_but_validated_when_supplied(tmp_path):
    # Operator decision 2026-11-11: the receipt is an optional manual check, not a
    # forward-deploy requirement, so a missing one is admitted (returns {}). A
    # supplied receipt is still validated for the exact SHA.
    from deploy.deploy import _load_read_timing_receipt
    assert _load_read_timing_receipt(None, SHA) == {}
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


def _runtime(receipt):
    """A coordinated Runtime holding only the deployer and the receipt path."""
    from deploy.coordinated_runtime import Runtime
    import deploy.deploy as module
    runtime = Runtime.__new__(Runtime)
    runtime.d, runtime.read_timing_receipt = module, receipt
    return runtime


def test_coordinated_prepare_binds_a_supplied_receipt_to_the_intent_it_seals(tmp_path):
    # A supplied receipt is still bound to the sealed target SHA; one for another
    # SHA is refused. (A missing receipt no longer blocks — see the optional test.)
    other = tmp_path / "other.json"
    other.write_text(json.dumps(read_timing_receipt("d" * 40)))
    with pytest.raises(DeployError, match="sha does not match deploy sha"):
        _runtime(other).prepare({"target_sha": SHA}, tmp_path / "unused")


def test_coordinated_apply_checks_the_sealed_target_before_any_stop(tmp_path, monkeypatch):
    from deploy.coordinated_runtime import Runtime
    reached = []
    monkeypatch.setattr(Runtime, "validate_common", lambda self, receipt, forward: reached.append(forward)
                        or (_ for _ in ()).throw(RuntimeError("past the read-timing check")))
    sealed = {"intent": {"target_sha": SHA, "prior_sha": "e" * 40}}
    other = tmp_path / "other.json"
    other.write_text(json.dumps(read_timing_receipt("d" * 40)))
    # A supplied receipt for the wrong SHA is still refused before validate_common.
    with pytest.raises(DeployError):
        _runtime(other).revalidate(sealed, forward=True)
    assert reached == []
    good = tmp_path / "good.json"
    good.write_text(json.dumps(read_timing_receipt()))
    # A good receipt passes the check; a missing one no longer gates a forward
    # deploy; rollback revalidation is never gated. All reach validate_common.
    for receipt, forward in ((good, True), (None, True), (None, False)):
        with pytest.raises(RuntimeError, match="past the read-timing check"):
            _runtime(receipt).revalidate(sealed, forward=forward)
    assert reached == [True, True, False]


def test_coordinated_deploy_hands_the_receipt_to_the_transaction_runtime(tmp_path, monkeypatch):
    from deploy import coordinated, coordinated_runtime
    import deploy.deploy as module
    seen = []

    class Transaction:
        def __init__(self, adapter, _root):
            seen.append(adapter)

        def apply(self, _plan, _receipt):
            return {}

        prepare = apply

        def rollback(self, _receipt, **_kwargs):
            return {}

    monkeypatch.setattr(coordinated, "Transaction", Transaction)
    monkeypatch.setattr(coordinated_runtime, "Runtime", lambda *_a, **kwargs: kwargs.get("read_timing_receipt"))
    service = module.SERVICES["chat-streamd-v2"]
    (tmp_path / "release").mkdir()
    common = {"release_checkout": tmp_path / "release", "ref": "origin/main",
              "coordinated_receipt": tmp_path / "receipt.json", "runner": module._run}
    timing = tmp_path / "timing.json"
    module.deploy(service, **common, coordinated_plan=tmp_path / "intent.json", read_timing_receipt=timing)
    module.deploy(service, **common, coordinated_plan=tmp_path / "intent.json", prepare_only=True,
                  read_timing_receipt=timing)
    module.deploy(service, **common, rollback=True)
    assert seen == [timing, timing, None]


def test_one_slow_sample_under_a_passing_median_is_admitted(tmp_path):
    """Version 2 certifies warmed median execution, not the tail or a cold first read."""
    payload = read_timing_receipt()
    _read(payload, INV).update(seconds=2.4, samples=[
        {"seconds": v, "statements": 3, "rows": 40} for v in (2.4, 9.0, 2.2)])
    assert _load_timing(tmp_path, payload) == payload
