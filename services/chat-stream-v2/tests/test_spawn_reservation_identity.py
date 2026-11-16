"""Reservation identity and settled-lifecycle contract tests.

Adoption binds a live pane to the spawn that created it by a creation nonce
injected into the pane environment, rather than by host and session name.
Synthetic cases cover crash-window adoption, foreign-pane rejection, expiry,
cancellation, unreachable peers, ownership, and legacy nonce-less rows.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest

from sessions import Sessions  # noqa: E402
from spawnctl import SpawnCtl  # noqa: E402
from store import Store  # noqa: E402

HOST = "localhost"
BRIEF = "please investigate the flaky FL foreclosure scraper right now"


class IdentityTmux:
    """A controllable live/dead pane. `kill_session` records a kill so a test can
    prove adoption/terminalization NEVER kills a pane it did not create."""

    def __init__(self, *, alive: bool = True, pane_pid: str = "1234", screen: str = "$ ") -> None:
        self.alive = alive
        self._pid = pane_pid
        self.screen = screen
        self.pasted: list[str] = []
        self.keys: list[tuple] = []
        self.killed = False

    async def has_session(self, name: str) -> bool:
        return self.alive

    async def session_state(self, name: str) -> str:
        return "alive" if self.alive else "gone"

    async def capture(self, name: str) -> str:
        return self.screen

    async def pane_pid(self, name: str) -> str:
        return self._pid

    async def kill_session(self, name: str) -> None:
        self.killed = True
        self.alive = False

    async def run(self, *args: str, **_kw: object) -> tuple[int, str]:
        self.keys.append(args)
        return 0, ""

    async def paste(self, name: str, text: str) -> None:
        self.pasted.append(text)
        self.screen += f"\nECHO {text}"


def _held_transcript(tmp_path: Path, content: str):
    log = tmp_path / ".claude" / "projects" / "proj" / "sess.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(content)
    return open(log)  # noqa: SIM115 - held open for the probe's lifetime


def _stub_tmux_nonce(ctl: SpawnCtl, *, readable: bool, value: str) -> None:
    async def _read(*_args: object) -> tuple[bool, str]:
        return (readable, value)

    ctl._tmux_nonce = _read  # type: ignore[method-assign]


async def _new_ctl(store: Store, tmux: IdentityTmux, *, instance_id: str = "") -> SpawnCtl:
    sessions = Sessions(store, tmux=tmux, local_host=HOST)
    ctl = SpawnCtl(store, sessions, tmux=tmux)
    # Set as an attribute so the same test remains compatible with constructors
    # that do not expose this option.
    ctl.instance_id = instance_id
    return ctl


# -- Crash-window pane adopted with a matching nonce --------------------------


def test_crash_window_pane_adopts_on_nonce_match(tmp_path: Path) -> None:
    if shutil.which("lsof") is None or shutil.which("ps") is None:
        pytest.skip("adoption transcript probe needs lsof + ps")
    # The reservation has a nonce (written at reserve, before the pane) but was
    # never marked (`tmux_created=0`): the crash window. The live pane exposes
    # the same nonce, so it is proven ours and adopted with its own pane_pid.
    fh = _held_transcript(tmp_path, json.dumps({"type": "user", "message": {"content": BRIEF}}) + "\n")
    try:
        tmux = IdentityTmux(alive=True, pane_pid=str(os.getpid()), screen="boot")
        async def _go():
            store = Store(":memory:")
            store.start()
            try:
                ctl = await _new_ctl(store, tmux)
                _stub_tmux_nonce(ctl, readable=True, value="win-nonce")
                assert await store.reserve_stream_id(HOST, "cw", ttl_s=60.0, request_id="r", nonce="win-nonce")
                await store.record_spawn_intent(HOST, "cw", {"open_fields": {"objective": "Exercise interrupted spawn adoption", }, "brief": BRIEF})
                # tmux_created stays 0: the daemon died before mark.
                result = await ctl.reconcile_spawn_intents()
                row = await store.fetch_session(HOST, "cw")
                return result, row
            finally:
                store.stop()
        result, row = asyncio.run(_go())
    finally:
        fh.close()
    assert result == {"adopted": 1, "released": 0}
    assert row is not None and row["status"] == "open"
    assert str(row["pane_pid"]) == str(os.getpid())  # the matching pane, not a new one
    assert tmux.killed is False


# -- Foreign same-name pane is not adopted, delivered, or killed ---------------


def test_foreign_nonce_mismatch_terminalizes_without_kill_or_delivery() -> None:
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            tmux = IdentityTmux(alive=True, pane_pid="9999")
            ctl = await _new_ctl(store, tmux)
            # The live pane's env nonce is READ successfully and DIFFERS: foreign.
            _stub_tmux_nonce(ctl, readable=True, value="theirs")
            assert await store.reserve_stream_id(HOST, "clash", ttl_s=60.0, request_id="r", nonce="ours")
            await store.record_spawn_intent(HOST, "clash", {"open_fields": {"objective": "Exercise interrupted spawn adoption", }, "brief": BRIEF})
            await store.mark_tmux_created(HOST, "clash", pane_pid="9999")
            result = await ctl.reconcile_spawn_intents()
            return result, tmux, await store.get_spawn_outcome(HOST, "clash"), await store.fetch_session(HOST, "clash")
        finally:
            store.stop()
    result, tmux, outcome, row = asyncio.run(_go())
    assert result == {"adopted": 0, "released": 1}
    assert row is None                       # never registered
    assert tmux.pasted == []                 # brief never delivered to a foreign pane
    assert tmux.killed is False              # never kill a pane we did not create
    assert outcome is not None and outcome["state"] == "failed"
    assert "foreign_pane" in outcome["reason"]


# -- Expiry does not delete a payload-bearing intent ---------------------------


def test_expiry_exempts_a_payload_bearing_intent() -> None:
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            # A pre-create intent: payload present, tmux_created=0, TTL lapsed.
            # No nonce is needed for this payload-retention case.
            assert await store.reserve_stream_id(HOST, "pending", ttl_s=0.01, request_id="r")
            await store.record_spawn_intent(HOST, "pending", {"open_fields": {"objective": "Exercise interrupted spawn adoption", }, "brief": BRIEF})
            await asyncio.sleep(0.03)
            # A reservation of another id runs the expiry sweep. The sweep
            # collects only rows that are BOTH pane-less AND intent-less, so the
            # payload-bearing intent survives to be reconciled.
            assert await store.reserve_stream_id(HOST, "other", ttl_s=60.0, request_id="r2")
            names = {r["session_name"] for r in await store.reservations(include_expired=True)}
            return names
        finally:
            store.stop()
    names = asyncio.run(_go())
    assert "pending" in names, "the payload-bearing intent was swept before adoption"


# -- An unreadable nonce defers reconciliation ----------------------------------


def test_unreadable_nonce_defers_before_deadline() -> None:
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            tmux = IdentityTmux(alive=True, pane_pid="5555")
            ctl = await _new_ctl(store, tmux)
            # A transient probe failure: readable=False. NOT past deadline.
            _stub_tmux_nonce(ctl, readable=False, value="")
            assert await store.reserve_stream_id(HOST, "blip", ttl_s=60.0, request_id="r", nonce="n")
            await store.record_spawn_intent(HOST, "blip", {"open_fields": {"objective": "Exercise interrupted spawn adoption", }, "brief": BRIEF})
            await store.mark_tmux_created(HOST, "blip", pane_pid="5555")
            result = await ctl.reconcile_spawn_intents()
            return result, tmux, await store.get_spawn_outcome(HOST, "blip"), await store.reservations(include_expired=True)
        finally:
            store.stop()
    result, tmux, outcome, reservations = asyncio.run(_go())
    assert result == {"adopted": 0, "released": 0}          # neither adopted nor terminalized
    assert outcome is None                                  # a transient blip writes NO terminal outcome
    assert any(r["session_name"] == "blip" for r in reservations)  # handle retained
    assert tmux.killed is False


def test_unreadable_nonce_quarantines_past_deadline() -> None:
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            tmux = IdentityTmux(alive=True, pane_pid="5555")
            ctl = await _new_ctl(store, tmux)
            _stub_tmux_nonce(ctl, readable=False, value="")
            assert await store.reserve_stream_id(HOST, "blip", ttl_s=0.01, request_id="r", nonce="n")
            await store.record_spawn_intent(HOST, "blip", {"open_fields": {"objective": "Exercise interrupted spawn adoption", }, "brief": BRIEF})
            await store.mark_tmux_created(HOST, "blip", pane_pid="5555")
            await asyncio.sleep(0.03)  # past the bounded deadline
            result = await ctl.reconcile_spawn_intents()
            return result, await store.get_spawn_outcome(HOST, "blip")
        finally:
            store.stop()
    result, outcome = asyncio.run(_go())
    assert result == {"adopted": 0, "released": 0}
    assert outcome is not None and outcome["state"] == "failed"
    assert "unverifiable" in outcome["reason"]


# -- Cancellation during spawn retains the reservation -------------------------


def test_cancellation_after_intent_retains_the_reservation() -> None:
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            # No live pane at reserve time (the spawn's own create is stubbed).
            tmux = IdentityTmux(alive=False, pane_pid="4242")
            ctl = await _new_ctl(store, tmux, instance_id="daemon-A")

            async def _cancel_after_create(*_a, **_k):
                # Stand in for a shutdown cancelling the shielded spawn task after
                # the pane may exist. `_spawn_impl` has already recorded the intent.
                raise asyncio.CancelledError()

            ctl._spawn_fenced = _cancel_after_create  # type: ignore[method-assign]
            cancelled = False
            try:
                await ctl._spawn_impl(
                    {"objective": "Exercise the existing spawn contract", "command": "stub", "session_name": "torn", "request_id": "r", "prompt": BRIEF},
                    HOST,
                )
            except asyncio.CancelledError:
                cancelled = True
            reservations = await store.reservations(include_expired=True)
            outcome = await store.get_spawn_outcome(HOST, "torn")
            owner = reservations[0]["owner_instance_id"] if reservations else None
            return cancelled, reservations, outcome, owner
        finally:
            store.stop()
    cancelled, reservations, outcome, owner = asyncio.run(_go())
    assert cancelled is True
    assert any(r["session_name"] == "torn" for r in reservations), (
        "cancellation blind-released the reservation; a live pane would have no recovery handle"
    )
    assert outcome is None  # cancellation is not a confirmed terminal outcome
    assert owner is None  # final retained-teardown CAS clears the live owner


# -- An unreachable peer keeps its recovery handle ------------------------------


class _PeerHosts:
    def __init__(self, peers: dict) -> None:
        self.peers = peers

    def is_online(self, _host: str) -> bool:
        return False


def test_disabled_peer_intent_retains_handle_past_deadline() -> None:
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            tmux = IdentityTmux(alive=True, pane_pid="1")
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            ctl = SpawnCtl(store, sessions, tmux=tmux, hosts=_PeerHosts({"peerbox": object()}))
            # A configured peer whose reachability never resolves (--disable-hosts).
            assert await store.reserve_stream_id("peerbox", "remote", ttl_s=0.01, request_id="r", nonce="n")
            await store.record_spawn_intent("peerbox", "remote", {"open_fields": {"objective": "Exercise interrupted spawn adoption", }, "brief": BRIEF})
            await asyncio.sleep(0.03)  # past deadline
            result = await ctl.reconcile_spawn_intents()
            return result, await store.get_spawn_outcome("peerbox", "remote"), await store.reservations(include_expired=True)
        finally:
            store.stop()
    result, outcome, reservations = asyncio.run(_go())
    assert result == {"adopted": 0, "released": 0}
    assert outcome is None
    assert [row["session_name"] for row in reservations] == ["remote"]


def test_configured_peer_intent_defers_before_deadline() -> None:
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            tmux = IdentityTmux(alive=True, pane_pid="1")
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            ctl = SpawnCtl(store, sessions, tmux=tmux, hosts=_PeerHosts({"peerbox": object()}))
            assert await store.reserve_stream_id("peerbox", "remote", ttl_s=60.0, request_id="r", nonce="n")
            await store.record_spawn_intent("peerbox", "remote", {"open_fields": {"objective": "Exercise interrupted spawn adoption", }, "brief": BRIEF})
            result = await ctl.reconcile_spawn_intents()
            return result, await store.reservations(include_expired=True)
        finally:
            store.stop()
    result, reservations = asyncio.run(_go())
    assert result == {"adopted": 0, "released": 0}
    assert any(r["session_name"] == "remote" for r in reservations)


# -- A reservation owned by this live instance is never adopted -----------------


def test_reservation_owned_by_this_instance_is_not_adopted() -> None:
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            tmux = IdentityTmux(alive=True, pane_pid="8888")
            ctl = await _new_ctl(store, tmux, instance_id="daemon-A")
            # A live in-process spawn of THIS instance owns the reservation. The
            # skip is valid only while SpawnCtl's existing live task list says
            # that work is still active.
            _stub_tmux_nonce(ctl, readable=True, value="n")
            assert await store.reserve_stream_id(
                HOST, "inflight", ttl_s=60.0, request_id="r", nonce="n", owner_instance_id="daemon-A"
            )
            await store.record_spawn_intent(HOST, "inflight", {"open_fields": {"objective": "Exercise interrupted spawn adoption", }, "brief": BRIEF})
            await store.mark_tmux_created(HOST, "inflight", pane_pid="8888")
            active = asyncio.create_task(asyncio.sleep(60))
            ctl._background_spawns.add(active)
            try:
                result = await ctl.reconcile_spawn_intents()
            finally:
                active.cancel()
                await asyncio.gather(active, return_exceptions=True)
            return result, await store.get_spawn_outcome(HOST, "inflight"), await store.reservations(include_expired=True)
        finally:
            store.stop()
    result, outcome, reservations = asyncio.run(_go())
    assert result == {"adopted": 0, "released": 0}          # skipped, not adopted
    assert outcome is None                                  # the in-flight spawn owns the terminal write
    assert any(r["session_name"] == "inflight" for r in reservations)


def test_reservation_owned_by_a_dead_instance_is_eligible(tmp_path: Path) -> None:
    # The complement: a DIFFERENT (dead-daemon) owner does NOT skip adoption.
    if shutil.which("lsof") is None or shutil.which("ps") is None:
        pytest.skip("adoption transcript probe needs lsof + ps")
    fh = _held_transcript(tmp_path, json.dumps({"type": "user", "message": {"content": BRIEF}}) + "\n")
    try:
        tmux = IdentityTmux(alive=True, pane_pid=str(os.getpid()), screen="boot")
        async def _go():
            store = Store(":memory:")
            store.start()
            try:
                ctl = await _new_ctl(store, tmux, instance_id="daemon-B")
                _stub_tmux_nonce(ctl, readable=True, value="n")
                assert await store.reserve_stream_id(
                    HOST, "orphan", ttl_s=60.0, request_id="r", nonce="n", owner_instance_id="daemon-A"
                )
                await store.record_spawn_intent(HOST, "orphan", {"open_fields": {"objective": "Exercise interrupted spawn adoption", }, "brief": BRIEF})
                result = await ctl.reconcile_spawn_intents()
                return result
            finally:
                store.stop()
        result = asyncio.run(_go())
    finally:
        fh.close()
    assert result["adopted"] == 1


# -- Legacy nonce-less rows use the existing terminal disposition --------------


def test_nonce_less_row_uses_existing_failure_release() -> None:
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            tmux = IdentityTmux(alive=True, pane_pid="7777")
            ctl = await _new_ctl(store, tmux)
            assert await store.reserve_stream_id(HOST, "nonce-less", ttl_s=60.0, request_id="r")
            await store.record_spawn_intent(HOST, "nonce-less", {"open_fields": {"objective": "Exercise interrupted spawn adoption", }, "brief": ""})
            await store.mark_tmux_created(HOST, "nonce-less", pane_pid="7777")
            result = await ctl.reconcile_spawn_intents()
            outcome = await store.get_spawn_outcome(HOST, "nonce-less") or {}
            reservations = await store.reservations(include_expired=True)
            return result, outcome, reservations, tmux
        finally:
            store.stop()
    result, outcome, reservations, tmux = asyncio.run(_go())
    assert result == {"adopted": 0, "released": 1}
    assert outcome["state"] == "failed"
    assert outcome["reason"] == "boot_not_ready"
    assert reservations == []
    assert tmux.pasted == []
    assert tmux.killed is False
