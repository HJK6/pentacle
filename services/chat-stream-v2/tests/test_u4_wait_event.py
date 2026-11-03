"""Deterministic observation tests; no daemon, tmux or wall-clock sleeps."""
import asyncio
from types import SimpleNamespace

import pytest

from helpers import u4_process_harness as harness


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=0.0, wakes=[], sleeps=[], reads=[], rows=[])

    async def sleep(delay):
        state.sleeps.append(delay)
        assert state.wakes, "unexpected extra polling interval"
        state.now = state.wakes.pop(0)

    def events(_root):
        state.reads.append(state.now)
        return [row for row in state.rows if row['mono_s'] <= state.now]

    monkeypatch.setattr(harness, 'time', SimpleNamespace(monotonic=lambda: state.now))
    monkeypatch.setattr(harness, 'asyncio', SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(harness, 'events', events)
    return state


def row(at, name='completed'):
    return {'event': name, 'mono_s': at}


def test_normal_completion(clock):
    clock.wakes = [0.05]
    clock.rows = [row(0.04)]
    assert asyncio.run(harness.wait_event(None, 'completed', timeout=0.2)) == row(0.04)
    assert clock.reads == [0.0, 0.05]
    assert clock.sleeps == [0.05]


def test_late_wake_observes_completion_before_deadline(clock):
    clock.wakes = [0.22]
    clock.rows = [row(0.19)]
    assert asyncio.run(harness.wait_event(None, 'completed', timeout=0.2)) == row(0.19)
    assert clock.reads == [0.0, 0.22]
    assert clock.sleeps == [0.05]


@pytest.mark.parametrize('rows', [[], [row(0.01)], [row(0.19, 'other')]])
def test_missing_stale_or_other_event_fails_after_one_final_read(clock, rows):
    clock.wakes = [0.22]
    clock.rows = rows
    with pytest.raises(RuntimeError, match='^missing_event_completed$'):
        asyncio.run(harness.wait_event(None, 'completed', timeout=0.2, after=0.1))
    assert clock.reads == [0.0, 0.22]
    assert clock.sleeps == [0.05]


def test_newest_eligible_event_returned(clock):
    clock.wakes = [0.22]
    clock.rows = [row(0.01), row(0.1), row(0.19), row(0.2, 'other')]
    assert asyncio.run(harness.wait_event(None, 'completed', timeout=0.2, after=0.1)) == row(0.19)
    assert clock.reads == [0.0, 0.22]
    assert clock.sleeps == [0.05]


def test_expired_deadline_still_observes_once_without_sleep(clock):
    with pytest.raises(RuntimeError, match='^missing_event_completed$'):
        asyncio.run(harness.wait_event(None, 'completed', timeout=0))
    assert clock.reads == [0.0]
    assert clock.sleeps == []
