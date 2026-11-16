"""Recognition tests for the pinned hostc auth-context artifact contract.

The emitter remains lane 5's surface. These doubles exercise only SpawnCtl's
cross-host clear/read fence and outcome normalization; the separate private
tmux test continues to attest the real emitter when lane 5 merges it.
"""

from __future__ import annotations

import os as _os, pytest as _pytest  # env-gate: needs a live provider CLI
pytestmark = _pytest.mark.skipif(not _os.environ.get("PENTACLE_LIVE_TESTS"), reason="set PENTACLE_LIVE_TESTS=1")

import asyncio
import hashlib
import json

import pytest

import spawnctl as spawnctl_mod
from server import Server
from sessions import Sessions
from spawnctl import AUTH_CONTEXT_MARKER_BYTES, AUTH_CONTEXT_MARKER_DIR, SpawnCtl
from store import Store

hosta = "hosta"
HOSTC = "hostc"
CONTRACT_DIR = "/tmp/pentacle-auth-context"
CONTRACT_BYTES = b"provider_auth_context_unavailable\n"


@pytest.fixture(autouse=True)
def configured_marker_host(monkeypatch):
    # Pin the fixture's explicit opt-in without changing the process environment
    # or leaking the setting into other tests that imported spawnctl already.
    monkeypatch.setattr(spawnctl_mod, "AUTH_CONTEXT_MARKER_HOST", HOSTC)


class _Tmux:
    def __init__(self) -> None:
        self.alive = False
        self.new_sessions = 0
        self.pastes = 0

    async def has_session(self, _name: str) -> bool:
        return self.alive

    async def new_session(self, _name: str, _command: str, cwd=None, env=None) -> None:
        self.alive = True
        self.new_sessions += 1

    async def session_state(self, _name: str) -> str:
        return "alive" if self.alive else "gone"

    async def capture(self, _name: str) -> str:
        return "Loading Claude…"

    async def kill_session(self, _name: str) -> None:
        self.alive = False

    async def paste(self, _name: str, _text: str) -> None:
        self.pastes += 1

    async def run(self, *_args: str, **_kwargs: object) -> tuple[int, str]:
        return 0, ""


class _Hosts:
    local_host = hosta

    def __init__(
        self, tmux: _Tmux, *, clear_results: list[int], read_results: list[tuple[int, str]],
    ) -> None:
        self.tmux = tmux
        self.clear_results = list(clear_results)
        self.read_results = list(read_results)
        self.commands: list[tuple[str, tuple[str, ...], float]] = []

    async def ensure_reachable(self, host: str, what: str) -> None:
        assert (host, what) == (HOSTC, "spawn")

    def tmux_for(self, host: str) -> _Tmux:
        assert host == HOSTC
        return self.tmux

    async def run_command(self, host: str, *args: str, timeout: float = 10.0) -> tuple[int, str]:
        assert host == HOSTC
        self.commands.append((host, args, timeout))
        if args[:3] == ("/bin/rm", "-f", "--"):
            return self.clear_results.pop(0), ""
        if args[:3] == ("/usr/bin/head", "-c", "64"):
            return self.read_results.pop(0)
        raise AssertionError(f"unexpected remote command: {args!r}")


def _path(name: str) -> str:
    digest = hashlib.sha256(f"hostc:{name}".encode("utf-8")).hexdigest()
    return f"{CONTRACT_DIR}/{digest}.code"


def _resolve(_msg: dict, _host: str, _name: str):
    async def _inner():
        return "claude --tui", {"resolved_launch_tuple": {"provider": "claude"}}, {}

    return _inner()


async def _no_sleep(_delay: float) -> None:
    return None


async def _dispatch_spawn(hosts: _Hosts, name: str) -> tuple[dict, dict | None, _Tmux]:
    store = Store(":memory:")
    store.start()
    try:
        sessions = Sessions(store, tmux=hosts.tmux, local_host=hosta)
        ctl = SpawnCtl(store, sessions, tmux=hosts.tmux, hosts=hosts)
        ctl._resolve_launch = _resolve
        server = Server(store=store, sessions=sessions, spawnctl=ctl, local_host=hosta)
        (reply,) = await server._dispatch(json.dumps({"objective": "Exercise the existing spawn contract",
            "type": "spawn",
            "host": HOSTC,
            "provider": "claude",
            "session_name": name,
            "request_id": f"request-{name}",
            "initial_prompt": "do not paste",
        }))
        outcome = await store.get_spawn_outcome(HOSTC, name)
        return reply, outcome, hosts.tmux
    finally:
        store.stop()


def _patch_fast_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spawnctl_mod, "BOOT_READY_HARD_DEADLINE_S", 0.01)
    monkeypatch.setattr(spawnctl_mod.asyncio, "sleep", _no_sleep)


def test_marker_constants_are_the_pinned_path_and_exact_34_byte_payload() -> None:
    assert AUTH_CONTEXT_MARKER_DIR == CONTRACT_DIR
    assert AUTH_CONTEXT_MARKER_BYTES == CONTRACT_BYTES
    assert len(AUTH_CONTEXT_MARKER_BYTES) == 34
    unsafe_name = "v2-name with / punctuation"
    assert SpawnCtl._auth_context_marker_path(unsafe_name) == _path(unsafe_name)


def test_marker_fence_path_bytes_and_public_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fast_boot(monkeypatch)
    name = "v2-auth-context"
    hosts = _Hosts(
        _Tmux(),
        clear_results=[0],
        read_results=[(0, CONTRACT_BYTES.decode("ascii"))],
    )

    reply, outcome, tmux = asyncio.run(_dispatch_spawn(hosts, name))

    assert reply["type"] == "spawn.error"
    assert reply["error_code"] == "provider_auth_context_unavailable"
    assert reply["initial_prompt_delivery"]["failure_code"] == "provider_auth_context_unavailable"
    assert outcome is not None and outcome["reason"].startswith("provider_auth_context_unavailable:")
    assert outcome["delivery_receipt"]["failure_code"] == "provider_auth_context_unavailable"
    assert tmux.new_sessions == 1 and tmux.pastes == 0
    assert hosts.commands == [
        (HOSTC, ("/bin/rm", "-f", "--", _path(name)), 5.0),
        (HOSTC, ("/usr/bin/head", "-c", "64", _path(name)), 5.0),
    ]


@pytest.mark.parametrize(
    "read_result",
    [
        (1, ""),
        (0, "provider_auth_context_unavailable:"),
        (0, "provider_auth_context_unavailable\nextra"),
    ],
)
def test_absent_or_nonexact_marker_is_generic_boot_failure(
    monkeypatch: pytest.MonkeyPatch, read_result: tuple[int, str],
) -> None:
    _patch_fast_boot(monkeypatch)
    name = "v2-auth-generic"
    hosts = _Hosts(_Tmux(), clear_results=[0, 0], read_results=[read_result, read_result])

    reply, outcome, tmux = asyncio.run(_dispatch_spawn(hosts, name))

    assert reply["error_code"] == "boot_not_ready"
    assert outcome is not None and outcome["reason"].startswith("boot_not_ready:")
    assert tmux.new_sessions == spawnctl_mod.PROMPT_SPAWN_RETRY_ATTEMPTS
    clear_calls = [args for _host, args, _timeout in hosts.commands if args[0] == "/bin/rm"]
    read_calls = [args for _host, args, _timeout in hosts.commands if args[0] == "/usr/bin/head"]
    assert clear_calls == [("/bin/rm", "-f", "--", _path(name))] * 2
    assert read_calls == [("/usr/bin/head", "-c", "64", _path(name))] * 2


def test_clear_failure_is_generic_and_never_reads_a_stale_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fast_boot(monkeypatch)
    name = "v2-auth-stale-fence"
    hosts = _Hosts(
        _Tmux(),
        clear_results=[1, 1],
        # A pre-existing exact marker must not matter after a failed clear.
        read_results=[(0, CONTRACT_BYTES.decode("ascii"))],
    )

    reply, outcome, tmux = asyncio.run(_dispatch_spawn(hosts, name))

    assert reply["error_code"] == "boot_not_ready"
    assert outcome is not None and outcome["reason"].startswith("boot_not_ready:")
    assert tmux.new_sessions == 0
    assert [args for _host, args, _timeout in hosts.commands] == [
        ("/bin/rm", "-f", "--", _path(name)),
        ("/bin/rm", "-f", "--", _path(name)),
    ]
