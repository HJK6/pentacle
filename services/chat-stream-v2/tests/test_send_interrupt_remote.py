"""Remote interrupt binds the caller's selected lifecycle and exact tmux pane."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace

from sessions import VerbError
from uiverbs import UIVerbs


LOCAL = "local"
PEER = "peer"
NAME = "owned-seat"
GENERATION = "selected-generation"
PANE = {"pane_pid": "31415", "pane_id": "%19", "tty": "/dev/pts/19",
        "tmux_socket": "/tmp/test-tmux/socket", "session_name": NAME}


class Store:
    def __init__(self) -> None:
        self.row = {"status": "open", "session_generation": GENERATION, "pane_pid": PANE["pane_pid"]}
        self.fetches = 0
        self.on_fetch = None

    async def fetch_session(self, host, name):
        assert (host, name) == (PEER, NAME)
        self.fetches += 1
        if self.on_fetch:
            self.on_fetch(self.fetches)
        return deepcopy(self.row)


class Tmux:
    def __init__(self) -> None:
        self.state = "alive"
        self.identity = deepcopy(PANE)
        self.identity_reads = 0
        self.on_identity = None
        self.keys = []

    async def session_state(self, name):
        assert name == NAME
        return self.state

    async def has_session(self, name):
        assert name == NAME
        return self.state == "alive"

    async def pane_identity(self, name):
        assert name == NAME
        self.identity_reads += 1
        if self.on_identity:
            self.on_identity(self.identity_reads)
        return deepcopy(self.identity)

    async def run(self, *args):
        self.keys.append(args)
        return 0, ""


class Hosts:
    local_host = LOCAL

    def __init__(self, tmux):
        self.peers = {PEER: object()}
        self.tmux = tmux
        self.transport_calls = 0
        self.reachable = True

    async def ensure_reachable(self, host, what):
        assert (host, what) == (PEER, "send.interrupt")
        if not self.reachable:
            raise VerbError("host_offline", "peer is unreachable")

    def tmux_for(self, host):
        assert host == PEER
        self.transport_calls += 1
        return self.tmux


class Sessions:
    local_host = LOCAL

    def assert_local(self, host, what):
        if host != LOCAL:
            raise VerbError("unsupported_host", f"{host} is not local for {what}")


def rig():
    store = Store()
    tmux = Tmux()
    hosts = Hosts(tmux)
    spawnctl = SimpleNamespace(tmux=tmux, hosts=hosts)
    return UIVerbs(store, Sessions(), spawnctl), store, tmux, hosts


def send(ui, *, host=PEER, generation=GENERATION):
    msg = {"request_id": "irq-1", "host": host, "session_name": NAME}
    if generation is not None:
        msg["expected_session_generation"] = generation
    return asyncio.run(ui.send_interrupt(msg))


def test_configured_remote_interrupt_targets_checked_pane_id_once():
    ui, store, tmux, hosts = rig()
    result = send(ui)
    assert result["type"] == "send.interrupt.ok"
    assert result["confirm"] == "interrupt_unconfirmed"
    assert result["interrupted"] is True and result["landed"] is False
    assert store.fetches >= 2 and tmux.identity_reads >= 2
    assert hosts.transport_calls == 1
    assert tmux.keys == [("send-keys", "-t", PANE["pane_id"], "Escape")]


def test_remote_missing_generation_refuses_installed_mobile_payload_without_key():
    ui, _store, tmux, _hosts = rig()
    result = send(ui, generation=None)
    assert result["type"] == "send.interrupt.error"
    assert result["error_code"] == "generation_required"
    assert tmux.keys == []


def test_unknown_host_is_rejected_before_transport_fallback():
    ui, _store, tmux, hosts = rig()
    result = send(ui, host="unknown")
    assert result["type"] == "send.interrupt.error"
    assert result["error_code"] == "unsupported_host"
    assert hosts.transport_calls == 0 and tmux.keys == []


def test_stale_selected_generation_refuses_reopened_row():
    ui, store, tmux, _hosts = rig()
    store.row["session_generation"] = "replacement-generation"
    result = send(ui)
    assert result["type"] == "send.interrupt.error"
    assert result["error_code"] == "stale_session_generation"
    assert tmux.keys == []


def test_generation_changed_at_final_fence_refuses_key():
    ui, store, tmux, _hosts = rig()
    store.on_fetch = lambda count: store.row.update(session_generation="replacement-generation") if count == 2 else None
    result = send(ui)
    assert result["type"] == "send.interrupt.error"
    assert result["error_code"] == "stale_session_generation"
    assert tmux.keys == []


def test_pane_switch_at_final_fence_refuses_key_to_either_pane():
    ui, _store, tmux, _hosts = rig()
    tmux.on_identity = lambda count: tmux.identity.update(pane_id="%20") if count == 2 else None
    result = send(ui)
    assert result["type"] == "send.interrupt.error"
    assert result["error_code"] == "pane_identity_changed"
    assert tmux.keys == []


def test_unreachable_peer_is_not_misreported_as_absent_pane():
    ui, _store, tmux, hosts = rig()
    hosts.reachable = False
    result = send(ui)
    assert result["type"] == "send.interrupt.error"
    assert result["error_code"] == "host_offline"
    assert tmux.keys == []


def test_unreachable_ssh_tmux_session_state_sends_no_key():
    ui, _store, tmux, _hosts = rig()
    tmux.state = "unreachable"
    result = send(ui)
    assert result["type"] == "send.interrupt.error"
    assert result["error_code"] == "host_unreachable"
    assert tmux.keys == []


def test_pane_pid_mismatch_sends_no_key():
    ui, _store, tmux, _hosts = rig()
    tmux.identity["pane_pid"] = "77777"
    result = send(ui)
    assert result["type"] == "send.interrupt.error"
    assert result["error_code"] == "pane_identity_changed"
    assert tmux.keys == []


def test_pane_pid_changed_in_durable_row_at_final_fence_sends_no_key():
    ui, store, tmux, _hosts = rig()
    store.on_fetch = lambda count: store.row.update(pane_pid="77777") if count == 2 else None
    result = send(ui)
    assert result["type"] == "send.interrupt.error"
    assert result["error_code"] == "stale_session_generation"
    assert tmux.keys == []


def test_missing_pane_has_honest_unavailable_receipt():
    ui, _store, tmux, _hosts = rig()
    tmux.state = "gone"
    result = send(ui)
    assert result["type"] == "send.interrupt.ok"
    assert result["confirm"] == "pane_unavailable"
    assert result["interrupted"] is False and tmux.keys == []


def test_local_legacy_payload_keeps_existing_session_name_target_and_receipt():
    ui, store, tmux, hosts = rig()
    result = send(ui, host=LOCAL, generation=None)
    assert result["type"] == "send.interrupt.ok"
    assert result["confirm"] == "interrupt_unconfirmed"
    assert tmux.keys == [("send-keys", "-t", f"={NAME}:", "Escape")]
    assert store.fetches == 0 and hosts.transport_calls == 0
