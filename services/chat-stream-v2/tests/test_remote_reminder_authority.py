"""Trusted capture revocation and remote reminder exclusions."""
import asyncio

import pytest

from ledger import NudgeJob
from presence import PresenceConfig, RemotePresence
from store import Store
from test_nudges import Harness
from test_event_push import _ev, _push, _sink
from test_remote_presence import (
    _BlockingCheckedPreviewTmux, _CheckedPreviewTmux, _FakeHosts, _observe_and_apply,
)


@pytest.mark.parametrize("failure", ["capture_failed", "unknown", "offline", "dead", "reopen"])
def test_remote_trusted_capture_is_revoked(failure):
    async def run():
        store = Store(":memory:"); store.start()
        try:
            h = Harness(store)
            await h.open("remote", host="hostb")
            sid = "hostb:remote"
            tmux = _CheckedPreviewTmux((0, "remote\t1234\n"), h.tmux.IDLE)
            hosts = _FakeHosts("hosta", {"hostb": tmux}, {"hostb": True})
            presence = RemotePresence(h.sessions, hosts, config=PresenceConfig(preview_cache_ttl_s=0))
            await _observe_and_apply(presence)
            assert NudgeJob._known_capture(h.sessions.get(sid))
            if failure == "capture_failed":
                tmux.checked_ok = False
            elif failure == "unknown":
                tmux.preview = ""
            elif failure == "offline":
                hosts._online["hostb"] = False
            elif failure == "dead":
                tmux.result = (0, "v2-another\t5678\n")
            else:
                # Successful old capture cannot authorize its successor.
                h.sessions._inv[sid]["session_generation"] = "successor"
                assert not NudgeJob._known_capture(h.sessions.get(sid))
                return
            await _observe_and_apply(presence)
            row = h.sessions.get(sid)
            assert not (NudgeJob._known_capture(row) and NudgeJob._eligible(row))
        finally:
            store.stop()
    asyncio.run(run())


def test_inflight_capture_never_grants_successor():
    async def run():
        store = Store(":memory:"); store.start()
        try:
            h = Harness(store)
            await h.open("remote", host="hostb")
            sid = "hostb:remote"
            tmux = _BlockingCheckedPreviewTmux((0, "remote\t1234\n"), h.tmux.IDLE)
            hosts = _FakeHosts("hosta", {"hostb": tmux}, {"hostb": True})
            presence = RemotePresence(h.sessions, hosts)
            task = asyncio.create_task(_observe_and_apply(presence))
            await tmux.capture_started.wait()
            h.sessions._inv[sid]["session_generation"] = "successor"
            tmux.release_capture.set()
            await task
            assert not NudgeJob._known_capture(h.sessions.get(sid))
        finally:
            store.stop()
    asyncio.run(run())


def test_authenticated_satellite_cannot_copy_capture_authority():
    async def run():
        store = Store(":memory:"); store.start()
        try:
            h = Harness(store)
            await h.open("remote", host="hostb", user_event_count=0)
            sid = "hostb:remote"
            generation = h.sessions.get(sid)["session_generation"]
            copied = {"capture_generation": generation, "local_mirror": True,
                      "mirror_generation": generation, "working": False,
                      "capture_liveness": "idle", "online": True}
            ep, _, _ = _sink(store, sessions=h.sessions)
            event = {**_ev("copied", stream_id=sid), **copied}
            event["raw"].update(copied)
            reply = await ep.handle_push({**_push(ep, [event]), **copied})
            assert reply["type"] == "event.push.ok" and reply["inserted"] == 1
            assert not NudgeJob._known_capture(h.sessions.get(sid))
            assert (await h.job.run_pass()).sent == 0
        finally:
            store.stop()
    asyncio.run(run())


def test_dead_result_revokes_even_with_external_inventory():
    async def run():
        store = Store(":memory:"); store.start()
        try:
            h = Harness(store)
            await h.open("remote", host="hostb")
            sid = "hostb:remote"
            tmux = _CheckedPreviewTmux((0, "remote\t1234\n"), h.tmux.IDLE)
            hosts = _FakeHosts("hosta", {"hostb": tmux}, {"hostb": True})
            presence = RemotePresence(h.sessions, hosts)
            await _observe_and_apply(presence)
            result = await presence._capture_pane("hostb", h.sessions.get(sid), asyncio.Semaphore(1))
            # Another inventory producer observes death before this read applies.
            h.sessions.apply_live(sid, pane_status="pane_dead", online=False)
            await presence._apply_capture_result(result, {sid: presence._preview_key(h.sessions.get(sid))})
            h.sessions.apply_live(sid, pane_status="pane_alive", online=True)
            assert not NudgeJob._known_capture(h.sessions.get(sid))
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("fields", [
    {"working": True}, {"working": None}, {"visibility": "hidden"},
    {"parent_stream_id": "hosta:parent"}, {"pane_status": "pane_dead"},
])
def test_remote_title_and_card_remain_silent_when_excluded(fields):
    async def run():
        store = Store(":memory:"); store.start()
        try:
            h = Harness(store)
            await h.open("remote", host="hostb")
            sid = "hostb:remote"
            tmux = _CheckedPreviewTmux((0, "remote\t1234\n"), h.tmux.IDLE)
            hosts = _FakeHosts("hosta", {"hostb": tmux}, {"hostb": True})
            await _observe_and_apply(RemotePresence(h.sessions, hosts))
            h.sessions.apply_live(sid, **fields)
            if "visibility" in fields or "parent_stream_id" in fields:
                h.sessions.apply_durable(sid, **fields)
            result = await h.job.run_pass()
            assert result.sent == 0
        finally:
            store.stop()
    asyncio.run(run())
