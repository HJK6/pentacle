"""Bounded recovery of deferred remote spawn intents on the existing reconciler."""

from __future__ import annotations

import asyncio

from reconciler import ReconcileConfig, SessionReconciler
from sessions import Sessions
from spawnctl import SpawnCtl
from store import Store


class _RemoteTmux:
    def __init__(self, nonces: dict[str, str | None], *, state: str = "alive") -> None:
        self.nonces = nonces
        self.state = state
        self.probed: list[str] = []
        self.killed: list[str] = []

    async def session_state(self, name: str) -> str:
        self.probed.append(name)
        return self.state

    async def has_session(self, name: str) -> bool:
        return self.state == "alive"

    async def pane_pid(self, name: str) -> str:
        return "42"

    async def run(self, *args: str, **_kwargs: object) -> tuple[int, str]:
        if args and args[0] == "show-environment":
            name = str(args[2]).removeprefix("=").removesuffix(":")
            nonce = self.nonces.get(name)
            if nonce is None:
                return 2, "transport unavailable"
            return 0, f"PENTACLE_SPAWN_NONCE={nonce}\n"
        return 0, ""

    async def kill_session(self, name: str) -> None:
        self.killed.append(name)
        self.state = "gone"


class _Hosts:
    local_host = "hosta"

    def __init__(self, tmux: _RemoteTmux, *, online: bool) -> None:
        self.peers = {"peer": object()}
        self.tmux = tmux
        self.online = online
        self.tmux_for_calls: list[str] = []
        self.commands: list[tuple[str, ...]] = []

    def is_online(self, host: str) -> bool:
        return host == self.local_host or self.online

    def tmux_for(self, host: str) -> _RemoteTmux:
        self.tmux_for_calls.append(host)
        return self.tmux

    async def run_command(self, host: str, *args: str, **_kwargs: object) -> tuple[int, str]:
        self.commands.append((host, *args))
        return 0, ""


async def _intent(store: Store, name: str, nonce: str, *, ttl_s: float = 60.0) -> None:
    assert await store.reserve_stream_id(
        "peer", name, ttl_s=ttl_s, request_id=f"req-{name}",
        nonce=nonce, owner_instance_id="dead-instance",
    )
    await store.record_spawn_intent("peer", name, {"open_fields": {"objective": "Exercise interrupted spawn adoption", }, "brief": ""})


def test_existing_reconciler_owns_one_recurring_intent_per_pass() -> None:
    class _IntentSpy:
        def __init__(self) -> None:
            self.calls: list[tuple[int, bool]] = []

        async def reconcile_spawn_intents(self, *, limit: int, recurring: bool) -> dict[str, int]:
            self.calls.append((limit, recurring))
            return {"adopted": 0, "released": 0}

    async def run() -> list[tuple[int, bool]]:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host="hosta")
            spy = _IntentSpy()
            reconciler = SessionReconciler(
                sessions, _Hosts(_RemoteTmux({}), online=False),
                spawnctl=spy, config=ReconcileConfig(max_rows_per_pass=200),
            )
            await reconciler.reconcile_once()
            return spy.calls
        finally:
            store.stop()

    assert asyncio.run(run()) == [(1, True)]


def test_offline_peer_defers_without_remote_transport_then_adopts_online() -> None:
    async def run() -> tuple[dict[str, int], dict[str, int], object, _Hosts]:
        store = Store(":memory:")
        store.start()
        try:
            tmux = _RemoteTmux({"seat": "nonce-seat"})
            hosts = _Hosts(tmux, online=False)
            sessions = Sessions(store, tmux=None, local_host="hosta", hosts=hosts)
            ctl = SpawnCtl(store, sessions, tmux=None, hosts=hosts, owner_instance_id="new-instance")
            await _intent(store, "seat", "nonce-seat")
            deferred = await ctl.reconcile_spawn_intents(limit=1, recurring=True)
            assert hosts.tmux_for_calls == []
            hosts.online = True
            adopted = await ctl.reconcile_spawn_intents(limit=1, recurring=True)
            return deferred, adopted, await store.fetch_session("peer", "seat"), hosts
        finally:
            store.stop()

    deferred, adopted, row, hosts = asyncio.run(run())
    assert deferred == {"adopted": 0, "released": 0}
    assert adopted == {"adopted": 1, "released": 0}
    assert row is not None
    assert hosts.tmux_for_calls == ["peer"]


def test_stale_terminal_outcome_does_not_suppress_new_request_generation() -> None:
    async def run() -> dict[str, int]:
        store = Store(":memory:")
        store.start()
        try:
            tmux = _RemoteTmux({"seat": "nonce-seat"})
            hosts = _Hosts(tmux, online=True)
            sessions = Sessions(store, tmux=None, local_host="hosta", hosts=hosts)
            ctl = SpawnCtl(store, sessions, tmux=None, hosts=hosts, owner_instance_id="daemon-B")
            assert await store.reserve_stream_id(
                "peer", "seat", ttl_s=60.0, request_id="new-request",
                nonce="nonce-seat", owner_instance_id="daemon-A",
            )
            await store.record_spawn_intent("peer", "seat", {"open_fields": {"objective": "Exercise interrupted spawn adoption", }, "brief": ""})
            await store.set_spawn_outcome(
                "peer", "seat", "failed", request_id="old-request", reason="old generation",
            )
            return await ctl.reconcile_spawn_intents(limit=1, recurring=True)
        finally:
            store.stop()

    assert asyncio.run(run()) == {"adopted": 1, "released": 0}


def test_empty_legacy_request_ids_do_not_suppress_recurring_reconciliation() -> None:
    async def run() -> dict[str, int]:
        store = Store(":memory:")
        store.start()
        try:
            tmux = _RemoteTmux({"seat": "nonce-seat"})
            hosts = _Hosts(tmux, online=True)
            sessions = Sessions(store, tmux=None, local_host="hosta", hosts=hosts)
            ctl = SpawnCtl(store, sessions, tmux=None, hosts=hosts, owner_instance_id="daemon-B")
            assert await store.reserve_stream_id(
                "peer", "seat", ttl_s=60.0, request_id="",
                nonce="nonce-seat", owner_instance_id="daemon-A",
            )
            await store.record_spawn_intent("peer", "seat", {"open_fields": {"objective": "Exercise interrupted spawn adoption", }, "brief": ""})
            await store.set_spawn_outcome(
                "peer", "seat", "failed", request_id="", reason="legacy generation",
            )
            return await ctl.reconcile_spawn_intents(limit=1, recurring=True)
        finally:
            store.stop()

    assert asyncio.run(run()) == {"adopted": 1, "released": 0}


def test_matching_terminal_outcome_still_suppresses_recurring_reconciliation() -> None:
    async def run() -> tuple[dict[str, int], list[str]]:
        store = Store(":memory:")
        store.start()
        try:
            tmux = _RemoteTmux({"seat": "nonce-seat"})
            hosts = _Hosts(tmux, online=True)
            sessions = Sessions(store, tmux=None, local_host="hosta", hosts=hosts)
            ctl = SpawnCtl(store, sessions, tmux=None, hosts=hosts, owner_instance_id="daemon-B")
            assert await store.reserve_stream_id(
                "peer", "seat", ttl_s=60.0, request_id="same-request",
                nonce="nonce-seat", owner_instance_id="daemon-A",
            )
            await store.record_spawn_intent("peer", "seat", {"open_fields": {"objective": "Exercise interrupted spawn adoption", }, "brief": ""})
            await store.set_spawn_outcome(
                "peer", "seat", "failed", request_id="same-request", reason="same generation",
            )
            return await ctl.reconcile_spawn_intents(limit=1, recurring=True), tmux.probed
        finally:
            store.stop()

    result, probed = asyncio.run(run())
    assert result == {"adopted": 0, "released": 0}
    assert probed == []


def test_startup_reconcile_keeps_terminal_and_indeterminate_outcomes() -> None:
    async def run() -> tuple[dict[str, int], dict, dict, list[str]]:
        store = Store(":memory:")
        store.start()
        try:
            tmux = _RemoteTmux({}, state="gone")
            hosts = _Hosts(tmux, online=True)
            sessions = Sessions(store, tmux=None, local_host="hosta", hosts=hosts)
            ctl = SpawnCtl(store, sessions, tmux=None, hosts=hosts, owner_instance_id="daemon-B")
            for name, request_id, ttl_s, state in (
                ("fresh-terminal", "request-fresh", 60.0, "indeterminate"),
                ("expired-terminal", "request-expired", -1.0, "failed"),
            ):
                assert await store.reserve_stream_id(
                    "peer", name, ttl_s=ttl_s, request_id=request_id,
                    nonce=f"nonce-{name}", owner_instance_id="daemon-A",
                )
                await store.record_spawn_intent("peer", name, {"open_fields": {"objective": "Exercise interrupted spawn adoption", }, "brief": ""})
                await store.set_spawn_outcome(
                    "peer", name, state, request_id=request_id, reason=f"{state} first",
                )
            result = await ctl.reconcile_spawn_intents(limit=None, recurring=False)
            return (
                result,
                await store.get_spawn_outcome("peer", "fresh-terminal") or {},
                await store.get_spawn_outcome("peer", "expired-terminal") or {},
                [row["session_name"] for row in await store.reservations(include_expired=True)],
            )
        finally:
            store.stop()

    result, fresh, expired, reservations = asyncio.run(run())
    assert result == {"adopted": 0, "released": 1}
    assert fresh["state"] == "indeterminate"
    assert expired["state"] == "failed"
    assert reservations == ["fresh-terminal"]


def test_past_deadline_failed_reservation_is_released() -> None:
    async def run() -> tuple[dict[str, int], dict, list[dict]]:
        store = Store(":memory:")
        store.start()
        try:
            tmux = _RemoteTmux({}, state="gone")
            hosts = _Hosts(tmux, online=True)
            sessions = Sessions(store, tmux=None, local_host="hosta", hosts=hosts)
            ctl = SpawnCtl(store, sessions, tmux=None, hosts=hosts, owner_instance_id="daemon-B")
            assert await store.reserve_stream_id(
                "peer", "expired-failed", ttl_s=-1.0, request_id="request-failed",
                nonce="nonce-expired", owner_instance_id="daemon-A",
            )
            await store.record_spawn_intent("peer", "expired-failed", {"open_fields": {"objective": "Exercise interrupted spawn adoption", }, "brief": ""})
            await store.set_spawn_outcome(
                "peer", "expired-failed", "failed", request_id="request-failed", reason="prior failure",
            )
            return (
                await ctl.reconcile_spawn_intents(limit=None, recurring=False),
                await store.get_spawn_outcome("peer", "expired-failed") or {},
                await store.reservations(include_expired=True),
            )
        finally:
            store.stop()

    result, outcome, reservations = asyncio.run(run())
    assert result == {"adopted": 0, "released": 1}
    assert outcome == {**outcome, "state": "failed", "reason": "prior failure"}
    assert reservations == []


def test_recurring_rotation_does_not_starve_later_ambiguous_intent() -> None:
    async def run() -> list[str]:
        store = Store(":memory:")
        store.start()
        try:
            tmux = _RemoteTmux({"a": None, "b": None})
            hosts = _Hosts(tmux, online=True)
            sessions = Sessions(store, tmux=None, local_host="hosta", hosts=hosts)
            ctl = SpawnCtl(store, sessions, tmux=None, hosts=hosts, owner_instance_id="new-instance")
            await _intent(store, "a", "nonce-a")
            await _intent(store, "b", "nonce-b")
            await ctl.reconcile_spawn_intents(limit=1, recurring=True)
            await ctl.reconcile_spawn_intents(limit=1, recurring=True)
            return tmux.probed
        finally:
            store.stop()

    assert asyncio.run(run()) == ["a", "b"]


def test_overlapping_passes_claim_one_adoption() -> None:
    async def run() -> int:
        store = Store(":memory:")
        store.start()
        try:
            tmux = _RemoteTmux({"seat": "nonce-seat"})
            hosts = _Hosts(tmux, online=True)
            sessions = Sessions(store, tmux=None, local_host="hosta", hosts=hosts)
            ctl = SpawnCtl(store, sessions, tmux=None, hosts=hosts, owner_instance_id="new-instance")
            await _intent(store, "seat", "nonce-seat")
            calls = 0

            async def adopt(*_args: object, **_kwargs: object) -> str:
                nonlocal calls
                calls += 1
                await asyncio.sleep(0)
                return "adopted"

            ctl._adopt_interrupted_spawn = adopt  # type: ignore[method-assign]
            await asyncio.gather(
                ctl.reconcile_spawn_intents(limit=1, recurring=True),
                ctl.reconcile_spawn_intents(limit=1, recurring=True),
            )
            return calls
        finally:
            store.stop()

    assert asyncio.run(run()) == 1


def test_startup_passes_from_two_daemon_instances_have_one_reservation_cas_winner() -> None:
    async def run() -> int:
        store = Store(":memory:")
        store.start()
        try:
            tmux = _RemoteTmux({"seat": "nonce-seat"})
            hosts = _Hosts(tmux, online=True)
            sessions = Sessions(store, tmux=None, local_host="hosta", hosts=hosts)
            first = SpawnCtl(store, sessions, tmux=None, hosts=hosts, owner_instance_id="first")
            second = SpawnCtl(store, sessions, tmux=None, hosts=hosts, owner_instance_id="second")
            await _intent(store, "seat", "nonce-seat")
            calls = 0

            async def adopt(*_args: object, **_kwargs: object) -> str:
                nonlocal calls
                calls += 1
                await asyncio.sleep(0.01)
                return "adopted"

            first._adopt_interrupted_spawn = adopt  # type: ignore[method-assign]
            second._adopt_interrupted_spawn = adopt  # type: ignore[method-assign]
            await asyncio.gather(
                first.reconcile_spawn_intents(limit=1, recurring=False),
                second.reconcile_spawn_intents(limit=1, recurring=False),
            )
            return calls
        finally:
            store.stop()

    assert asyncio.run(run()) == 1


def test_indeterminate_adoption_settlement_retains_reservation_and_restores_owner() -> None:
    async def run() -> tuple[dict[str, int], object, list[dict[str, object]]]:
        store = Store(":memory:")
        store.start()
        try:
            tmux = _RemoteTmux({"seat": "nonce-seat"})
            hosts = _Hosts(tmux, online=True)
            sessions = Sessions(store, tmux=None, local_host="hosta", hosts=hosts)
            ctl = SpawnCtl(store, sessions, tmux=None, hosts=hosts, owner_instance_id="new-instance")
            await _intent(store, "seat", "nonce-seat")

            async def indeterminate(*_args: object, **_kwargs: object) -> tuple[str, str, str]:
                raise TimeoutError("remote tmux did not answer")

            ctl._settle_adoption = indeterminate  # type: ignore[method-assign]
            result = await ctl.reconcile_spawn_intents(limit=1, recurring=False)
            return (
                result,
                await store.get_spawn_outcome("peer", "seat"),
                await store.reservations(include_expired=True),
            )
        finally:
            store.stop()

    result, outcome, reservations = asyncio.run(run())
    assert result == {"adopted": 0, "released": 0}
    assert outcome is None
    assert len(reservations) == 1
    assert reservations[0]["owner_instance_id"] == "dead-instance"


def test_remote_adoption_reads_process_and_transcript_on_peer() -> None:
    class _TranscriptHosts(_Hosts):
        async def run_command(
            self, host: str, *args: str, **_kwargs: object,
        ) -> tuple[int, str]:
            self.commands.append((host, *args))
            if args[:2] == ("ps", "-eo"):
                return 0, "42 1\n43 42\n"
            if args[:1] == ("lsof",):
                return 0, "n/Users/peer/.codex/sessions/seat.jsonl\n"
            if args[:1] == ("tail",):
                return 0, '{"type":"user","text":"deliver peer brief"}\n'
            return 1, ""

    async def run() -> tuple[tuple[str, str, str], list[tuple[str, ...]]]:
        store = Store(":memory:")
        store.start()
        try:
            tmux = _RemoteTmux({"seat": "nonce-seat"})
            hosts = _TranscriptHosts(tmux, online=True)
            sessions = Sessions(store, tmux=None, local_host="hosta", hosts=hosts)
            ctl = SpawnCtl(store, sessions, tmux=None, hosts=hosts, owner_instance_id="new")
            result = await ctl._settle_adoption("peer", "seat", "deliver peer brief", tmux)
            return result, hosts.commands
        finally:
            store.stop()

    result, commands = asyncio.run(run())
    assert result[0:2] == ("delivered", "transcript")
    assert [command[1] for command in commands] == ["ps", "lsof", "tail"]


def test_remote_registration_failure_rolls_back_on_peer_tmux() -> None:
    async def run() -> _RemoteTmux:
        store = Store(":memory:")
        store.start()
        try:
            tmux = _RemoteTmux({"seat": "nonce-seat"})
            hosts = _Hosts(tmux, online=True)
            sessions = Sessions(store, tmux=None, local_host="hosta", hosts=hosts)

            async def fail_open(*_args: object, **_kwargs: object) -> dict[str, object]:
                raise RuntimeError("registration failed")

            sessions.open = fail_open  # type: ignore[method-assign]
            ctl = SpawnCtl(store, sessions, tmux=None, hosts=hosts, owner_instance_id="new")
            # The reconciler supplies the original nonce-bearing reservation to
            # the adoption CAS; the fence fails closed without it, so seed the
            # matching reservation+intent (nonce == the pane's env nonce).
            await store.reserve_stream_id(
                "peer", "seat", ttl_s=600, request_id="req-seat",
                nonce="nonce-seat", owner_instance_id="new",
            )
            await store.record_spawn_intent("peer", "seat", {"open_fields": {"objective": "Exercise interrupted spawn adoption", }, "brief": ""})
            await ctl._adopt_interrupted_spawn(
                "peer", "seat", "req-seat", {"open_fields": {"objective": "Exercise interrupted spawn adoption", }, "brief": ""}, tmux,
            )
            return tmux
        finally:
            store.stop()

    assert asyncio.run(run()).killed == ["seat"]


def test_unreadable_live_remote_past_deadline_is_quarantined_not_released() -> None:
    async def run() -> tuple[object, list[dict[str, object]], _RemoteTmux]:
        store = Store(":memory:")
        store.start()
        try:
            tmux = _RemoteTmux({"seat": None})
            hosts = _Hosts(tmux, online=True)
            sessions = Sessions(store, tmux=None, local_host="hosta", hosts=hosts)
            ctl = SpawnCtl(store, sessions, tmux=None, hosts=hosts, owner_instance_id="new-instance")
            await _intent(store, "seat", "nonce-seat", ttl_s=0.01)
            await asyncio.sleep(0.03)
            await ctl.reconcile_spawn_intents(limit=1, recurring=True)
            return (
                await store.get_spawn_outcome("peer", "seat"),
                await store.reservations(include_expired=True),
                tmux,
            )
        finally:
            store.stop()

    outcome, reservations, tmux = asyncio.run(run())
    assert outcome is not None and outcome["state"] == "failed"
    assert "identity_unverifiable" in str(outcome["reason"])
    assert [row["session_name"] for row in reservations] == ["seat"]
    assert tmux.killed == []
