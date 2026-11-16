import asyncio
import copy
from unittest.mock import patch

import pytest

from queued_send_now import route_for
from machines import _machine_from_dict
from sessions import VerbError
from test_queued_send_now import GEN, setup, message, finish
from test_codex_capacity_retry import Tmux


REMOTE_PINS = {
    ("peer-a", "claude"): ("fixture-native-a", "a" * 64),
    ("peer-b", "claude"): ("fixture-native-b", "b" * 64),
    ("peer-b", "codex"): ("fixture-native-c", "c" * 64),
}


def peer_configs():
    return {host: _machine_from_dict({"name": host, "ssh_target": "peer.invalid",
        "queued_send_now_qualifications": {provider: {"version": version, "executable_sha256": digest}
            for (name, provider), (version, digest) in REMOTE_PINS.items() if name == host}})
        for host, _provider in REMOTE_PINS}

def remote_row(host, provider):
    version, digest = REMOTE_PINS[(host, provider)]
    return {"host": host, "provider": provider,
            "account_binding": {"host": host, "provider": provider,
                                "version": version, "executable_sha256": digest}}


@pytest.mark.parametrize("host,provider", REMOTE_PINS)
def test_host_native_qualification_is_exact(host, provider):
    row = remote_row(host, provider)
    assert route_for(row, "coordinator", peer_configs()) == ("claude-native" if provider == "claude" else "codex-continue")
    for field in ("host", "provider", "version", "executable_sha256"):
        changed = copy.deepcopy(row)
        changed["account_binding"][field] = "unqualified"
        assert route_for(changed, "coordinator", peer_configs()) is None
    changed = copy.deepcopy(row)
    changed["host"] = "unknown-peer"
    assert route_for(changed, "coordinator", peer_configs()) is None


def test_unqualified_provider_bindings_remain_excluded():
    for version, digest in (
        ("fixture-unqualified-a", "d" * 64),
        ("fixture-unqualified-b", "e" * 64),
    ):
        assert route_for({"host": "peer-a", "provider": "codex", "account_binding": {
            "host": "peer-a", "provider": "codex", "version": version,
            "executable_sha256": digest}}, "coordinator", peer_configs()) is None


def test_remote_receipt_dispatch_never_uses_same_named_local_pane():
    async def run():
        h = await setup(provider="claude")
        peer = Tmux(h)
        peer.screen = h.tmux.screen
        seen = []

        class Hosts:
            peers = peer_configs()
            def is_local(self, host): return host == "fixture"
            async def ensure_reachable(self, host, verb): seen.append((host, verb))
            def tmux_for(self, host):
                assert host == "peer-b"
                return peer

        try:
            h.comms.hosts = Hosts()
            row = remote_row("peer-b", "claude")
            await h.store.open_session("peer-b", "v2-capacity", provider="claude",
                session_generation=GEN, pane_pid="8123", account_binding=row["account_binding"],
                observer_binding={"generation": GEN, "pane_pid": "8123"})
            await h.store.append_send_receipt(to_stream_id="peer-b:v2-capacity", request_id="selected",
                receipt_id="peer-receipt", state="accepted", wire_text="selected input",
                display_text="selected input", attachments=[], delivery="landed",
                submission_confirmed=False, optimistic_id="optimistic", reason="provider_queued",
                actor_stream_id="operator:fixture", actor_trusted=True)
            await h.controller.bind_receipt_locked("peer-b:v2-capacity", GEN, "selected", "peer-receipt")
            with patch("queued_send_now.claude_composer_empty", return_value=True):
                await h.controller.send(message(host="peer-b"))
                replay = await h.controller.send(message(host="peer-b", request_id="duplicate-click"))
            assert replay["coalesced"]
            assert peer.keys == [("send-keys", "-t", "=v2-capacity:", "C-x", "C-s")]
            assert h.tmux.keys == []
            assert peer.pastes == h.tmux.pastes == []
            assert seen
        finally:
            await finish(h)
    asyncio.run(run())


@pytest.mark.parametrize("offline", [False, True])
def test_remote_missing_or_offline_host_cannot_fall_back_locally(offline):
    async def run():
        h = await setup()
        calls = []
        class Hosts:
            peers = peer_configs() if offline else {}
            def is_local(self, _host): return False
            async def ensure_reachable(self, _host, _verb):
                raise VerbError("host_offline", "owned test peer offline")
            def tmux_for(self, _host):
                calls.append("unsafe-fallback")
                return h.tmux
        try:
            h.comms.hosts = Hosts()
            with pytest.raises(VerbError):
                await h.controller._peer_tmux({"host": "peer-b"})
            assert calls == h.tmux.keys == h.tmux.pastes == []
        finally:
            await finish(h)
    asyncio.run(run())


def test_peer_probe_does_not_send_native_payloads_or_browser_claims():
    async def run():
        h = await setup()
        calls = []
        class Hosts:
            peers = peer_configs()
            def is_local(self, _host): return False
            async def ensure_reachable(self, _host, _verb): pass
            def tmux_for(self, _host): return h.tmux
            async def run_command(self, host, *args, **kwargs):
                import json
                calls.append((host, args, kwargs))
                return 0, json.dumps({"ok": True, "native_executable_sha256": "observed"})
        try:
            h.comms.hosts = Hosts()
            row = {**remote_row("peer-b", "codex"), "display_text": "not metadata",
                   "browser_claim": "not authority"}
            assert (await h.controller._remote_snapshot(row))["native_executable_sha256"] == "observed"
            import json
            expected = json.loads(calls[0][1][-1])
            assert calls[0][0] == "peer-b"
            assert "display_text" not in expected and "browser_claim" not in expected
            assert calls[0][2]["timeout"] == 12.0
        finally:
            await finish(h)
    asyncio.run(run())
