"""Start timeout, initialization failure and stopping retain one worker owner."""
import asyncio
import threading

import pytest

from store import Store
import store_exchange


def test_timeout_retry_waits_for_same_owner(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    initialize = store_exchange.initialize
    calls = []
    def blocked(conn):
        calls.append(threading.current_thread())
        entered.set()
        assert release.wait(5)
        initialize(conn)
    monkeypatch.setattr(store_exchange, "initialize", blocked)
    store = Store()
    try:
        with pytest.raises(TimeoutError, match="initializ"):
            store.start(timeout=0.02)
        assert entered.is_set() and not store._ready.is_set()
        owner = store._thread
        with pytest.raises(TimeoutError, match="initializ"):
            store.start(timeout=0.02)
        assert store._thread is owner and calls == [owner]
        release.set()
        store.start(timeout=2)
        assert store._thread is owner
        asyncio.run(store.put("usable", "yes"))
        assert asyncio.run(store.get("usable")) == "yes"
    finally:
        release.set()
        assert store.stop()


def test_stop_during_initialization_refuses_replacement(monkeypatch):
    release = threading.Event()
    initialize = store_exchange.initialize
    def blocked(conn):
        assert release.wait(5)
        initialize(conn)
    monkeypatch.setattr(store_exchange, "initialize", blocked)
    store = Store()
    try:
        with pytest.raises(TimeoutError):
            store.start(timeout=0.02)
        owner = store._thread
        assert store.stop(timeout=0.02) is False
        with pytest.raises(RuntimeError, match="stopping"):
            store.start(timeout=0.02)
        assert store._thread is owner and owner.is_alive()
        release.set()
        assert store.stop(timeout=2)
        store.start(timeout=2)
        assert store._thread is not owner
        asyncio.run(store.put("restart", "usable"))
    finally:
        release.set()
        assert store.stop()


def test_initialization_error_is_distinct_and_retry_is_usable(monkeypatch):
    initialize = store_exchange.initialize
    def fail(conn):
        raise ValueError("initialization fixture error")
    monkeypatch.setattr(store_exchange, "initialize", fail)
    store = Store()
    try:
        with pytest.raises(ValueError, match="fixture error"):
            store.start(timeout=2)
        assert store.stop(timeout=2)
        monkeypatch.setattr(store_exchange, "initialize", initialize)
        store.start(timeout=2)
        asyncio.run(store.put("retry", "usable"))
    finally:
        assert store.stop()


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), -float("inf"), -1])
def test_invalid_start_timeout_never_creates_worker(timeout):
    store = Store()
    try:
        with pytest.raises(ValueError):
            store.start(timeout=timeout)
        assert store._thread is None
    finally:
        assert store.stop()


def test_stop_after_ready_signal_cannot_return_success(monkeypatch):
    store = Store()
    wait = store._ready.wait
    def stop_before_return(timeout=None):
        result = wait(timeout)
        assert result
        assert store.stop(timeout=2)
        return result
    monkeypatch.setattr(store._ready, "wait", stop_before_return)
    try:
        with pytest.raises(RuntimeError, match="stop|owner|initializ"):
            store.start(timeout=2)
    finally:
        assert store.stop()


def test_new_owner_cannot_satisfy_an_old_start_wait(monkeypatch):
    store = Store()
    wait = store._ready.wait
    old_signaled, release_old = threading.Event(), threading.Event()
    failures = []
    def delayed_wait(timeout=None):
        result = wait(timeout)
        if threading.current_thread().name == "old-start":
            old_signaled.set()
            assert release_old.wait(5)
        return result
    monkeypatch.setattr(store._ready, "wait", delayed_wait)
    def old_start():
        try:
            store.start(timeout=2)
        except BaseException as exc:
            failures.append(exc)
    starter = threading.Thread(target=old_start, name="old-start")
    try:
        starter.start()
        assert old_signaled.wait(2)
        old_owner = store._thread
        assert store.stop(timeout=2)
        store.start(timeout=2)
        assert store._thread is not old_owner
        release_old.set()
        starter.join(2)
        assert not starter.is_alive()
        assert len(failures) == 1 and isinstance(failures[0], RuntimeError)
        asyncio.run(store.put("new-owner", "usable"))
    finally:
        release_old.set()
        starter.join(2)
        assert store.stop()
