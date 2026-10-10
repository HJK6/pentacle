"""Controlled-clock SYNTHETIC reference-contract tests, not installed-checker proof.

These tests never import daemon classification, contact an SMS provider, or use
private transport/configuration. Real process/held-fault evidence belongs to the
separate process test suite. Synthetic SID strings do not prove submission.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

# Path-based import permits copying this pair into an existing tests/ directory
# without changing that repository's package or production import configuration.
_MODEL = Path(__file__).parent / "helpers" / "u4_checker_model.py"
_SPEC = importlib.util.spec_from_file_location("synthetic_u4_checker_model", _MODEL)
model = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(model)

BOOT_A = "00000000-0000-4000-8000-000000000001"
BOOT_B = "00000000-0000-4000-8000-000000000002"
INSTANCE = "synthetic-installation"


class Clock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value

    def at(self, value):
        self.value = value
        return value


def snapshot(now, *, loop=None, seq=None, boot=BOOT_A, instance=INSTANCE,
             current=None, oldest=None, completed=0):
    running, pending = int(current is not None), int(oldest is not None)
    return {"version": "daemon-progress.v1", "instance_id": instance,
            "boot_id": boot, "pid": 4321, "sample_mono_s": now,
            "loop_seq": int(now) + 1 if seq is None else seq,
            "loop_mono_s": now if loop is None else loop,
            "store": {"enqueued_seq": completed + running + pending,
                      "started_seq": completed + running, "finished_seq": completed,
                      "pending_count": pending, "current_started_mono_s": current,
                      "oldest_pending_mono_s": oldest}}


def write_snapshot(path, value):
    path.write_text(json.dumps(value, allow_nan=True))
    path.chmod(0o600)


@pytest.fixture
def setup(tmp_path):
    tmp_path.chmod(0o700)
    clock = Clock()
    journal = model.AtomicJournal(tmp_path / "synthetic-checker.json")
    checker = model.SyntheticChecker(journal, clock)
    return checker, clock, journal, tmp_path


def open_loop_episode(checker, clock, start=0, *, completed=0):
    clock.at(start)
    checker.observe(snapshot(start, completed=completed))
    clock.at(start + 5)
    checker.observe(snapshot(start + 5, loop=start, seq=int(start) + 1, completed=completed))
    assert checker.active is not None
    return checker.active["episode_id"]


def recover(checker, clock, start=6, *, boot=BOOT_A, completed=0):
    clock.at(start)
    checker.observe(snapshot(start, boot=boot, completed=completed))
    assert checker.recovery is None
    clock.at(start + 1)
    checker.observe(snapshot(start + 1, boot=boot, completed=completed))
    assert checker.recovery is not None


class Caller:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = result if result is not None else {"sid": "SYNTHETIC-SID", "status": "queued"}
        self.error = error

    def __call__(self, body):
        self.calls.append(copy.deepcopy(body))
        if self.error is not None:
            raise self.error
        return self.result


def test_startup_grace_is_exactly_five_seconds(setup):
    checker, clock, _, _ = setup
    checker.observe(snapshot(0))
    clock.at(4.999)
    assert checker.observe(snapshot(4.999, loop=0, seq=1)) == "healthy"
    assert checker.active is None
    clock.at(5)
    assert checker.observe(snapshot(5, loop=0, seq=1)) == "loop"
    assert checker.active["condition"] == "active"
    assert checker.active["ordinal"] == 1


def test_new_valid_boot_receives_own_startup_grace(setup):
    checker, clock, _, _ = setup
    checker.observe(snapshot(0))
    clock.at(20)
    # Already-old loop timestamps cannot bypass grace on a newly bound boot.
    checker.observe(snapshot(20, loop=10, boot=BOOT_B))
    assert checker.active is None
    clock.at(24.999)
    checker.observe(snapshot(24.999, loop=10, boot=BOOT_B))
    assert checker.active is None
    clock.at(25)
    checker.observe(snapshot(25, loop=10, boot=BOOT_B))
    assert checker.active["boot_id"] == BOOT_B


def test_healthy_idle_store_stays_quiet_and_state_is_constant_size(setup):
    checker, clock, journal, _ = setup
    sizes = []
    for second in range(200):
        clock.at(second)
        assert checker.observe(snapshot(second)) == "healthy"
        assert checker.active is None and checker.recovery is None
        sizes.append(journal.path.stat().st_size)
    assert max(sizes) < 1500
    assert max(sizes) - min(sizes) < 80
    assert checker.state["healthy_count"] == 2
    assert checker.state["ordinal"] == 0


@pytest.mark.parametrize("kind,expected", [("loop", "loop"), ("current", "store"),
                                            ("oldest", "store"), ("both", "loop_store")])
def test_age_five_threshold_and_single_episode_for_each_fault(setup, kind, expected):
    checker, clock, _, _ = setup
    options = {"loop": 0, "seq": 1} if kind in ("loop", "both") else {}
    if kind in ("current", "both"):
        options["current"] = 0
    if kind == "oldest":
        options.update(current=4, oldest=0)
    checker.observe(snapshot(0))
    clock.at(4.999)
    checker.observe(snapshot(4.999, **options))
    assert checker.active is None
    clock.at(5)
    assert checker.observe(snapshot(5, **options)) == expected
    identity = checker.active["episode_id"]
    for second in range(6, 11):
        clock.at(second)
        checker.observe(snapshot(second, **options))
        assert checker.active["episode_id"] == identity
        assert checker.state["ordinal"] == 1


@pytest.mark.parametrize("process_condition", ["publisher_frozen", "whole_process_frozen"])
def test_frozen_sample_has_independent_unavailable_cause(setup, process_condition):
    # Identical public-wire observations intentionally cannot distinguish these
    # fault sources. Actual freeze barriers are exercised outside this model.
    checker, clock, _, _ = setup
    frozen = snapshot(0)
    checker.observe(frozen)
    clock.at(4)
    assert checker.observe(frozen) == "healthy"
    assert checker.active is None
    clock.at(5)
    assert checker.observe(frozen) == "unavailable"
    assert checker.active["cause"] == "unavailable"
    assert process_condition in {"publisher_frozen", "whole_process_frozen"}


def test_pid_changes_are_diagnostic_not_recovery(setup):
    checker, clock, _, _ = setup
    open_loop_episode(checker, clock)
    clock.at(6)
    stale = snapshot(6, loop=0, seq=1)
    stale["pid"] = 99999
    assert checker.observe(stale) == "loop"
    assert checker.recovery is None


@pytest.mark.parametrize("fault", ["missing", "malformed", "insecure_file", "insecure_parent",
                                    "oversized", "unknown_version", "file_symlink", "unreadable"])
def test_unavailable_after_bind_is_sustained_and_not_fabricated_health(setup, monkeypatch, fault):
    checker, clock, _, directory = setup
    path = directory / "progress.json"
    write_snapshot(path, snapshot(0))
    assert checker.poll_file(path) == "healthy"
    if fault == "missing":
        path.unlink()
    elif fault == "malformed":
        path.write_text("{this is not JSON")
    elif fault == "insecure_file":
        path.chmod(0o644)
    elif fault == "insecure_parent":
        # Keep journal secure in its original directory, use a bad snapshot dir.
        nested = directory / "insecure"
        nested.mkdir(mode=0o755)
        path = nested / "progress.json"
        write_snapshot(path, snapshot(0))
    elif fault == "oversized":
        path.write_bytes(b" " * 4097)
    elif fault == "unknown_version":
        value = snapshot(0)
        value["version"] = "daemon-progress.v999"
        write_snapshot(path, value)
    elif fault == "file_symlink":
        target = directory / "target.json"
        path.rename(target)
        path.symlink_to(target)
    elif fault == "unreadable":
        original = model.read_owner_json
        def denied(candidate):
            if Path(candidate) == path:
                raise PermissionError("synthetic inaccessible file")
            return original(candidate)
        monkeypatch.setattr(model, "read_owner_json", denied)
    for moment in (1, 5.999):
        clock.at(moment)
        assert checker.poll_file(path) == "unavailable"
        assert checker.active is None
    clock.at(6)
    assert checker.poll_file(path) == "unavailable"
    assert checker.active["cause"] == "unavailable"
    clock.at(7)
    assert checker.poll_file(path) == "unavailable"
    assert checker.recovery is None


@pytest.mark.parametrize("value", [None, {}, {"version": "unknown"}])
def test_first_bind_failure_never_allocates_or_claims_bound(setup, value):
    checker, clock, _, _ = setup
    for second in (0, 5, 50):
        clock.at(second)
        assert checker.observe(value) == "unbound"
    assert checker.state["instance_id"] is None
    assert checker.active is None
    assert checker.state["ordinal"] == 0


def test_valid_but_changed_installation_identity_stays_unavailable(setup):
    checker, clock, _, _ = setup
    checker.observe(snapshot(0))
    for second in (1, 2, 6, 7):
        clock.at(second)
        assert checker.observe(snapshot(second, instance="other-valid-installation")) == "unavailable"
    assert checker.state["instance_id"] == INSTANCE
    assert checker.active["cause"] == "unavailable"
    assert checker.recovery is None


def test_transient_missing_progress_recovers_without_incident(setup):
    checker, clock, _, _ = setup
    checker.observe(snapshot(0))
    clock.at(1)
    checker.observe(None)
    clock.at(4)
    checker.observe(snapshot(4))
    clock.at(5)
    checker.observe(None)
    clock.at(9)
    checker.observe(None)
    assert checker.active is None
    assert checker.state["unavailable_since"] == 5


def test_valid_new_boot_needs_two_fresh_samples_to_recover_old_identity(setup):
    checker, clock, _, _ = setup
    identity = open_loop_episode(checker, clock)
    recover(checker, clock, start=10, boot=BOOT_B)
    assert checker.active["episode_id"] == identity
    assert checker.recovery["episode_id"] == identity
    assert checker.recovery["boot_id"] == BOOT_A
    assert checker.state["boot_id"] == BOOT_B
    assert checker.state["ordinal"] == 1


def test_replayed_healthy_snapshot_and_subsecond_polls_do_not_fake_two_samples(setup):
    checker, clock, _, _ = setup
    open_loop_episode(checker, clock)
    clock.at(6)
    healthy = snapshot(6)
    checker.observe(healthy)
    clock.at(7)
    checker.observe(healthy)
    assert checker.recovery is None
    clock.at(8)
    checker.observe(snapshot(8))
    clock.at(8.5)
    checker.observe(snapshot(8.5, seq=20))
    assert checker.recovery is None
    clock.at(9.5)
    checker.observe(snapshot(9.5, seq=21))
    assert checker.recovery is None
    clock.at(10.5)
    checker.observe(snapshot(10.5, seq=22))
    assert checker.recovery is not None


def test_unhealthy_sample_breaks_consecutive_recovery_proof(setup):
    checker, clock, _, _ = setup
    open_loop_episode(checker, clock)
    clock.at(6)
    checker.observe(snapshot(6))
    clock.at(7)
    checker.observe(None)
    clock.at(8)
    checker.observe(snapshot(8))
    assert checker.recovery is None
    clock.at(9)
    checker.observe(snapshot(9))
    assert checker.recovery is not None


def test_cause_changes_update_handoff_without_resubmitting_active_payload(setup):
    checker, clock, _, _ = setup
    identity = open_loop_episode(checker, clock)
    caller = Caller()
    checker.deliver("active", caller)
    original_submission = copy.deepcopy(caller.calls[0])
    for second, options, expected in [(6, {"current": 0}, "store"),
                                      (11, {"current": 0, "loop": 6, "seq": 7}, "loop_store")]:
        clock.at(second)
        assert checker.observe(snapshot(second, **options)) == expected
        assert checker.active["episode_id"] == identity
        assert checker.handoff()["active"]["cause"] == expected
        assert checker.deliver("active", caller) == "accepted"
        assert caller.calls == [original_submission]
    clock.at(16)
    assert checker.observe(snapshot(11, current=0, loop=6, seq=7)) == "unavailable"
    assert checker.state["ordinal"] == 1


@pytest.mark.parametrize("mutation", [
    ("sample_mono_s", -1), ("sample_mono_s", 11), ("sample_mono_s", float("nan")),
    ("loop_mono_s", -1), ("loop_mono_s", 11), ("loop_seq", -1), ("loop_seq", True),
    ("pid", 0), ("boot_id", "not-a-uuid"), ("version", "wrong"),
    ("store.pending_count", -1), ("store.enqueued_seq", 1),
    ("store.started_seq", 1), ("store.finished_seq", 1),
    ("store.current_started_mono_s", 0), ("store.oldest_pending_mono_s", 0),
])
def test_invalid_wire_values_cannot_classify_healthy(mutation):
    value = snapshot(10)
    key, replacement = mutation
    if "." in key:
        root, key = key.split(".")
        value[root][key] = replacement
    else:
        value[key] = replacement
    with pytest.raises(ValueError):
        model.classify(value, 10)


def test_future_active_store_timestamps_are_rejected():
    for value in (snapshot(10, current=11), snapshot(10, oldest=11)):
        with pytest.raises(ValueError):
            model.validate(value, 10)


@pytest.mark.parametrize("field", ["loop_seq", "sample_mono_s", "loop_mono_s", "store"])
def test_same_boot_counter_or_time_regression_is_unavailable(setup, field):
    checker, clock, _, _ = setup
    clock.at(10)
    checker.observe(snapshot(10, completed=4))
    value = snapshot(11, completed=4)
    if field == "store":
        value["store"] = snapshot(11, completed=3)["store"]
    else:
        value[field] = 9
    clock.at(11)
    assert checker.observe(value) == "unavailable"
    assert checker.state["healthy_count"] == 0


def test_secure_reader_rejects_wrong_owner_directory_links_and_duplicate_keys(setup, monkeypatch):
    _, _, _, directory = setup
    path = directory / "progress.json"
    write_snapshot(path, snapshot(0))
    real_fstat = model.os.fstat
    def wrong_owner(fd):
        result = real_fstat(fd)
        if stat.S_ISREG(result.st_mode):
            return SimpleNamespace(st_uid=os.getuid() + 1, st_mode=result.st_mode, st_size=result.st_size)
        return result
    with monkeypatch.context() as patch:
        patch.setattr(model.os, "fstat", wrong_owner)
        with pytest.raises(ValueError, match="insecure_file"):
            model.read_owner_json(path)
    target = directory / "actual"
    target.mkdir(mode=0o700)
    write_snapshot(target / "progress.json", snapshot(0))
    link = directory / "linked"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        model.read_owner_json(link / "progress.json")
    path.write_text('{"version":"one","version":"two"}')
    with pytest.raises(ValueError, match="duplicate"):
        model.read_owner_json(path)


def test_journal_restart_preserves_ordinal_identity_dedup_and_owner_modes(setup):
    checker, clock, journal, directory = setup
    identity = open_loop_episode(checker, clock)
    expected = hashlib.sha256(f"{INSTANCE}\0{BOOT_A}\0{1}".encode()).hexdigest()
    assert identity == expected
    caller = Caller()
    assert checker.deliver("active", caller) == "accepted"
    assert checker.deliver("active", caller) == "accepted"
    checker = model.SyntheticChecker(journal, clock)
    assert checker.active["episode_id"] == identity
    assert checker.deliver("active", caller) == "accepted"
    assert len(caller.calls) == 1
    assert stat.S_IMODE(journal.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert not list(directory.glob(".reference-*"))
    recover(checker, clock)
    assert checker.deliver("recovery", caller) == "accepted"
    assert checker.acknowledge_terminal(identity)
    checker = model.SyntheticChecker(journal, clock)
    second_id = open_loop_episode(checker, clock, start=10)
    assert second_id != identity
    assert checker.active["ordinal"] == 2
    assert checker.state["last_terminal"]["episode_id"] == identity


@pytest.mark.parametrize("condition", ["active", "recovery"])
def test_restart_inflight_becomes_unknown_and_never_resubmits(setup, condition):
    checker, clock, journal, _ = setup
    open_loop_episode(checker, clock)
    if condition == "recovery":
        checker.deliver("active", Caller())
        recover(checker, clock)
    checker.state[condition]["delivery"].update(state="inflight", attempts=1)
    journal.save(checker.state)
    checker = model.SyntheticChecker(journal, clock)
    caller = Caller()
    assert checker.state[condition]["delivery"]["state"] == "submission_unknown"
    for second in (10, 100, 1000):
        clock.at(second)
        assert checker.deliver(condition, caller) == "submission_unknown"
    assert caller.calls == []
    assert journal.load()[condition]["delivery"]["state"] == "submission_unknown"


def test_inflight_and_ordinal_are_durable_before_caller_invocation(setup):
    checker, clock, journal, _ = setup
    identity = open_loop_episode(checker, clock)
    def caller(body):
        durable = journal.load()
        assert durable["active"]["delivery"]["state"] == "inflight"
        assert durable["ordinal"] == 1
        assert durable["active"]["episode_id"] == identity == body["episode_id"]
        assert set(body) == {"episode_id", "condition", "cause"}
        assert len(json.dumps(body)) < 400
        return {"sid": "SYNTHETIC-ONLY", "status": "queued"}
    assert checker.deliver("active", caller) == "accepted"


def test_definitely_unsubmitted_preflight_uses_only_three_attempts_and_delays_2_5(setup):
    checker, clock, journal, _ = setup
    open_loop_episode(checker, clock)
    preflight_times = []
    def refuse():
        preflight_times.append(clock())
        raise model.DefinitelyUnsubmitted("synthetic local preflight refusal")
    caller = Caller()
    assert checker.deliver("active", caller, refuse) == "retry_wait"
    assert checker.active["delivery"]["next_attempt"] == 7
    clock.at(6.999)
    assert checker.deliver("active", caller, refuse) == "retry_wait"
    checker = model.SyntheticChecker(journal, clock)
    clock.at(7)
    assert checker.deliver("active", caller, refuse) == "retry_wait"
    assert checker.active["delivery"]["next_attempt"] == 12
    clock.at(11.999)
    assert checker.deliver("active", caller, refuse) == "retry_wait"
    clock.at(12)
    assert checker.deliver("active", caller, refuse) == "exhausted"
    checker = model.SyntheticChecker(journal, clock)
    clock.at(1000)
    assert checker.deliver("active", caller, refuse) == "exhausted"
    assert checker.active["delivery"]["attempts"] == 3
    assert preflight_times == [5, 7, 12]
    assert caller.calls == []


def test_definitely_unsubmitted_refusal_can_retry_once_then_accept(setup):
    checker, clock, _, _ = setup
    open_loop_episode(checker, clock)
    def refuse():
        raise model.DefinitelyUnsubmitted()
    caller = Caller()
    checker.deliver("active", caller, refuse)
    clock.at(7)
    assert checker.deliver("active", caller) == "accepted"
    assert checker.active["delivery"]["attempts"] == 2
    assert len(caller.calls) == 1


@pytest.mark.parametrize("result,error", [
    ("not-json", None), ({"sid": "", "status": "queued"}, None),
    ({"sid": "SYNTHETIC", "status": "failed"}, None),
    ({"sid": "SYNTHETIC", "status": ""}, None),
    ({"status": "queued"}, None), (None, TimeoutError("synthetic timeout")),
    (None, RuntimeError("synthetic process crash")),
    (None, model.DefinitelyUnsubmitted("too late: already invoked caller")),
])
def test_invoked_caller_uncertainty_never_automatically_retries(setup, result, error):
    checker, clock, journal, _ = setup
    open_loop_episode(checker, clock)
    caller = Caller(result, error)
    assert checker.deliver("active", caller) == "submission_unknown"
    checker = model.SyntheticChecker(journal, clock)
    for second in (7, 12, 1000):
        clock.at(second)
        assert checker.deliver("active", caller) == "submission_unknown"
    assert len(caller.calls) == 1
    assert checker.active["delivery"]["attempts"] == 1
    assert checker.active["delivery"]["sid"] is None


def test_storage_failure_after_acceptance_leaves_unknown_not_resend(setup, monkeypatch):
    checker, clock, journal, _ = setup
    open_loop_episode(checker, clock)
    real_save = journal.save
    def fail_result_save(value):
        if value["active"]["delivery"]["state"] in ("accepted", "submission_unknown"):
            raise OSError("synthetic storage outage after caller")
        real_save(value)
    monkeypatch.setattr(journal, "save", fail_result_save)
    caller = Caller()
    assert checker.deliver("active", caller) == "submission_unknown"
    assert journal.load()["active"]["delivery"]["state"] == "inflight"
    monkeypatch.setattr(journal, "save", real_save)
    checker = model.SyntheticChecker(journal, clock)
    assert checker.deliver("active", caller) == "submission_unknown"
    assert len(caller.calls) == 1


def test_failed_inflight_persistence_prevents_caller_invocation(setup, monkeypatch):
    checker, clock, journal, _ = setup
    open_loop_episode(checker, clock)
    real_save = journal.save
    def fail_inflight(value):
        if value["active"]["delivery"]["state"] == "inflight":
            raise OSError("synthetic persistence refusal")
        real_save(value)
    monkeypatch.setattr(journal, "save", fail_inflight)
    caller = Caller()
    with pytest.raises(OSError):
        checker.deliver("active", caller)
    assert caller.calls == []


def test_unknown_incident_keeps_recovery_and_blocks_ack_or_recovery_send(setup):
    checker, clock, _, _ = setup
    identity = open_loop_episode(checker, clock)
    caller = Caller(error=TimeoutError())
    checker.deliver("active", caller)
    recover(checker, clock)
    assert checker.deliver("recovery", caller) == "blocked_on_incident"
    assert not checker.acknowledge_terminal(identity)
    assert checker.active is not None and checker.recovery is not None
    assert len(caller.calls) == 1


def test_pending_recovery_is_never_replaced_and_terminal_state_is_bounded(setup):
    checker, clock, journal, _ = setup
    identity = open_loop_episode(checker, clock)
    caller = Caller()
    checker.deliver("active", caller)
    recover(checker, clock)
    pending = copy.deepcopy(checker.recovery)
    clock.at(12)
    checker.observe(snapshot(12, loop=7, seq=8))
    assert checker.active["episode_id"] == identity
    assert checker.recovery == pending
    assert checker.state["ordinal"] == 1
    assert not checker.acknowledge_terminal("other-identity")
    checker.deliver("recovery", caller)
    checker.deliver("recovery", caller)
    assert len(caller.calls) == 2
    assert checker.acknowledge_terminal(identity)
    assert not checker.acknowledge_terminal(identity)
    assert checker.state["last_terminal"]["episode_id"] == identity
    assert checker.active is None and checker.recovery is None
    clock.at(13)
    checker.observe(snapshot(13, loop=7, seq=8))
    assert checker.active["ordinal"] == 2
    assert checker.state["last_terminal"]["ordinal"] == 1
    assert journal.path.stat().st_size < model.LIMIT


def test_many_acknowledged_episodes_keep_only_active_recovery_and_last_terminal(setup):
    checker, clock, journal, _ = setup
    caller = Caller()
    for number in range(1, 31):
        start = (number - 1) * 10
        identity = open_loop_episode(checker, clock, start=start)
        checker.deliver("active", caller)
        recover(checker, clock, start=start + 6)
        checker.deliver("recovery", caller)
        assert checker.acknowledge_terminal(identity)
        assert checker.state["last_terminal"]["ordinal"] == number
        assert checker.state["ordinal"] == number
        assert journal.path.stat().st_size < 1700
        assert checker.active is None and checker.recovery is None
        assert not any(isinstance(value, list) for value in checker.state.values())
    assert len(caller.calls) == 60
    assert len({(body["episode_id"], body["condition"]) for body in caller.calls}) == 60


def test_journal_corruption_and_unknown_version_fail_closed(setup):
    _, clock, journal, _ = setup
    journal.path.write_text("{")
    with pytest.raises(ValueError):
        model.SyntheticChecker(journal, clock)
    journal.path.write_text('{"version":"unknown"}')
    with pytest.raises(ValueError, match="unknown_journal"):
        model.SyntheticChecker(journal, clock)


def test_new_boot_after_monotonic_reset_does_not_borrow_previous_ages(setup):
    checker, clock, journal, _ = setup
    identity = open_loop_episode(checker, clock, start=100)
    clock.at(0)
    checker = model.SyntheticChecker(journal, clock)
    assert checker.observe(snapshot(0, boot=BOOT_B)) == "healthy"
    assert checker.active["episode_id"] == identity and checker.recovery is None
    clock.at(1)
    checker.observe(snapshot(1, boot=BOOT_B))
    assert checker.recovery["episode_id"] == identity
    assert checker.state["boot_seen"] == 0


def test_handoff_is_public_bounded_and_contains_no_delivery_authority(setup):
    checker, clock, _, _ = setup
    identity = open_loop_episode(checker, clock)
    recover(checker, clock)
    handoff = checker.handoff()
    assert set(handoff) == {"version", "instance_id", "active", "recovery"}
    for condition in ("active", "recovery"):
        row = handoff[condition]
        assert set(row) == {"boot_id", "ordinal", "episode_id", "condition", "cause"}
        assert row["episode_id"] == identity
    encoded = json.dumps(handoff)
    assert len(encoded) < model.LIMIT
    for prohibited in ("recipient", "auth", "token", "mode", "SYNTHETIC-SID", "path", "command"):
        assert prohibited not in encoded


def test_loop_then_unavailable_then_healthy2_keeps_identity_without_second_active_send(setup):
    checker, clock, _, _ = setup
    checker.observe(snapshot(0))
    clock.at(1)
    frozen = snapshot(1, loop=0, seq=1)
    checker.observe(frozen)
    clock.at(5)
    assert checker.observe(frozen) == "loop"
    identity = checker.active["episode_id"]
    caller = Caller()
    assert checker.deliver("active", caller) == "accepted"
    assert caller.calls[0]["cause"] == "loop"
    clock.at(6)
    assert checker.observe(frozen) == "unavailable"
    assert checker.handoff()["active"]["cause"] == "unavailable"
    assert checker.active["episode_id"] == identity
    assert checker.deliver("active", caller) == "accepted"
    assert len(caller.calls) == 1
    recover(checker, clock, start=7)
    assert checker.recovery["episode_id"] == identity
    assert checker.deliver("recovery", caller) == "accepted"
    assert [(body["condition"], body["cause"]) for body in caller.calls] == [
        ("active", "loop"), ("recovered", "unavailable")]
    assert checker.state["ordinal"] == 1


def test_unexpected_preflight_failure_is_not_a_retry_permission(setup):
    checker, clock, journal, _ = setup
    open_loop_episode(checker, clock)
    def crash():
        raise RuntimeError("synthetic unexpected preflight crash")
    caller = Caller()
    assert checker.deliver("active", caller, crash) == "submission_unknown"
    checker = model.SyntheticChecker(journal, clock)
    clock.at(100)
    assert checker.deliver("active", caller) == "submission_unknown"
    assert caller.calls == []


def test_crash_after_attempt_budget_persistence_cannot_enable_fourth_attempt(setup):
    checker, clock, journal, _ = setup
    open_loop_episode(checker, clock)
    checker.active["delivery"].update(state="pending", attempts=3)
    journal.save(checker.state)
    checker = model.SyntheticChecker(journal, clock)
    caller = Caller()
    assert checker.deliver("active", caller) == "exhausted"
    assert checker.active["delivery"]["attempts"] == 3
    assert caller.calls == []
