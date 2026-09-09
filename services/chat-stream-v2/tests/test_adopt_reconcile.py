"""Adoption of a live pane reconciles to a definite outcome. Outcomes are
exactly delivered or failed:

  transcript needle present   -> delivered/transcript   (submitted before crash)
  transcript located, absent  -> failed/transcript_absent
  no transcript by boot line  -> failed/agent_never_started (live pane, no provider)
  dead pane                   -> failed (in the caller)
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path

import pytest

import spawnctl as spawnctl_mod  # noqa: E402
from sessions import Sessions  # noqa: E402
from spawnctl import SpawnCtl  # noqa: E402
from store import Store  # noqa: E402
from submission_events import DurableUserEventProof, EventProof  # noqa: E402

HOST = "localhost"
BRIEF = "please investigate the synthetic sample task"
#: A pid that holds nothing open — no transcript is locatable through it.
DEAD_PID = "2147480000"


class AdoptTmux:
    """A live (or dead) pane with controllable pid, capture, and paste."""

    def __init__(self, *, alive: bool, pane_pid: str, screen: str = "$ ") -> None:
        self.alive = alive
        self._pid = pane_pid
        self.screen = screen
        self.pasted: list[str] = []
        self.keys: list[tuple] = []

    async def has_session(self, name: str) -> bool:
        return self.alive

    async def session_state(self, name: str) -> str:
        return "alive" if self.alive else "gone"

    async def capture(self, name: str) -> str:
        return self.screen

    async def pane_pid(self, name: str) -> str:
        return self._pid

    async def kill_session(self, name: str) -> None:
        raise AssertionError("adoption must never kill a pane")

    async def run(self, *args: str, **_kw: object) -> tuple[int, str]:
        self.keys.append(args)
        return 0, ""

    async def paste(self, name: str, text: str) -> None:
        self.pasted.append(text)
        self.screen += f"\nECHO {text}"


class RegistrationFailureTmux(AdoptTmux):
    """A reserved pane whose session-row registration is forced to fail."""

    def __init__(self) -> None:
        super().__init__(alive=True, pane_pid="4321")
        self.killed = False

    async def session_state(self, name: str) -> str:
        return "alive" if await self.has_session(name) else "gone"

    async def kill_session(self, name: str) -> None:
        self.alive = False
        self.killed = True


class PeerEventTmux(AdoptTmux):
    """Peer-shaped pane: the pane is live and its provider is still loading,
    while the provider transcript is intentionally not discoverable by lsof."""

    async def run(self, *args: str, **kwargs: object) -> tuple[int, str]:
        if args and args[0] == "show-environment":
            self.keys.append(args)
        return 0, f"{spawnctl_mod.PANE_NONCE_ENV}=TEST\n"
        return await super().run(*args, **kwargs)


async def _adopt(name: str, tmux: AdoptTmux) -> tuple[dict | None, dict]:
    store = Store(":memory:")
    store.start()
    try:
        sessions = Sessions(store, tmux=tmux, local_host=HOST)
        spawnctl = SpawnCtl(store, sessions, tmux=tmux)
        # Adoption now binds the live pane to the reservation by its creation
        # The reservation carries a nonce and the live pane
        # exposes the same one in its environment. Stub the tmux read so the
        # settlement paths below (transcript / absent / never-started) are what
        # is under test, not the OS probe.
        async def _pane_nonce(*_args: object) -> tuple[bool, str]:
            return (True, "TEST")

        spawnctl._tmux_nonce = _pane_nonce  # type: ignore[method-assign]
        # The crash-safe intent a spawn writes before `tmux new-session`.
        assert await store.reserve_stream_id(
            HOST, name, ttl_s=0.01, request_id="r1", nonce="TEST"
        )
        await store.record_spawn_intent(HOST, name, {"open_fields": {"objective": "Exercise interrupted spawn adoption", }, "brief": BRIEF})
        await store.mark_tmux_created(HOST, name)
        await asyncio.sleep(0.03)  # a DELAYED restart: the TTL lapsed while down
        result = await spawnctl.reconcile_spawn_intents()
        reply = await spawnctl.await_spawn({"stream_id": f"{HOST}:{name}"})
        return await store.get_spawn_outcome(HOST, name), {"reconcile": result, "await": reply}
    finally:
        store.stop()


def _held_transcript(tmp_path: Path, content: str):
    """A provider `.jsonl` THIS process holds open (so lsof against our own pid —
    handed in as the pane pid — locates it), seeded with `content`."""
    log = tmp_path / "transcripts" / "example" / "sess.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(content)
    return open(log)  # noqa: SIM115 - held open for the probe's lifetime


def test_transcript_present_settles_delivered_transcript(tmp_path: Path) -> None:
    if shutil.which("lsof") is None or shutil.which("ps") is None:
        pytest.skip("transcript probe needs lsof + ps")
    fh = _held_transcript(tmp_path, json.dumps({"type": "user", "message": {"content": BRIEF}}) + "\n")
    try:
        tmux = AdoptTmux(alive=True, pane_pid=str(os.getpid()), screen="nothing on screen")
        outcome, extra = asyncio.run(_adopt("s-transcript", tmux))
    finally:
        fh.close()
    assert extra["reconcile"]["adopted"] == 1
    assert outcome is not None
    assert outcome["state"] == "delivered"
    assert outcome["delivery_evidence"] == "transcript"
    assert tmux.pasted == []  # a submitted brief is NEVER re-sent
    assert extra["await"]["type"] == "await_spawn.ok"
    assert extra["await"]["delivery_evidence"] == "transcript"


def test_transcript_absent_fails_without_repaste(tmp_path: Path) -> None:
    if shutil.which("lsof") is None or shutil.which("ps") is None:
        pytest.skip("transcript probe needs lsof + ps")
    # A transcript is located without the brief; adoption must not re-paste it.
    fh = _held_transcript(tmp_path, json.dumps({"type": "user", "message": {"content": "boot"}}) + "\n")
    try:
        tmux = AdoptTmux(alive=True, pane_pid=str(os.getpid()), screen="$ ")
        outcome, extra = asyncio.run(_adopt("s-absent", tmux))
    finally:
        fh.close()
    assert extra["reconcile"] == {"adopted": 1, "released": 0}
    assert outcome is not None
    assert outcome["state"] == "failed"
    assert outcome["delivery_evidence"] == "transcript_absent"
    assert tmux.pasted == []
    assert not any(a[0] == "send-keys" and "C-u" in a for a in tmux.keys)
    assert extra["await"]["type"] == "await_spawn.error"


def test_no_transcript_by_boot_deadline_is_agent_never_started(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_BOOT_DEADLINE_S", 0.3)
    # A live pane whose pid holds no transcript, and none ever appears.
    tmux = AdoptTmux(alive=True, pane_pid=DEAD_PID, screen="$ ")
    outcome, extra = asyncio.run(_adopt("s-noboot", tmux))
    assert extra["reconcile"]["adopted"] == 1
    assert outcome is not None
    assert outcome["state"] == "failed"
    assert outcome["delivery_evidence"] == "agent_never_started"
    assert tmux.pasted == []  # never re-paste blind when non-delivery is unproven
    assert extra["await"]["type"] == "await_spawn.error"
    assert extra["await"]["delivery_evidence"] == "agent_never_started"


def test_native_initial_prompt_adoption_converges_without_re_admission() -> None:
    peer_host = "hostb"
    peer_name = "v2-c2be3193"
    stream_id = f"{peer_host}:{peer_name}"
    brief = "native initial prompt"

    async def run() -> tuple[dict[str, object], dict[str, object], dict[str, object], PeerEventTmux]:
        store = Store(":memory:")
        store.start()
        try:
            tmux = PeerEventTmux(alive=True, pane_pid="3251384", screen="model loading")
            class PeerHosts:
                peers = {peer_host: object()}

                def is_online(self, host: str) -> bool:
                    return host == peer_host

                def tmux_for(self, host: str) -> PeerEventTmux:
                    assert host == peer_host
                    return tmux

            peer_hosts = PeerHosts()
            sessions = Sessions(store, tmux=tmux, local_host="hosta", hosts=peer_hosts)
            async def no_duplicate_admission(*_args: object, **_kwargs: object) -> dict[str, object]:
                raise AssertionError("durable live row must not be admitted a second time")

            sessions.open = no_duplicate_admission  # type: ignore[method-assign]
            proof = DurableUserEventProof(store, local_host="hosta")
            ctl = SpawnCtl(
                store, sessions, tmux=tmux, hosts=peer_hosts, submission_proof=proof,
            )
            assert await store.reserve_stream_id(
                peer_host, peer_name, ttl_s=60.0,
                request_id="spawn-canary", nonce="TEST",
            )
            await store.open_session(
                peer_host, peer_name, provider="codex",
                pane_pid=tmux._pid, pane_status="pane_alive", bootstrap_state="unproven",
            )
            lifecycle = await store.fetch_open_session_lifecycle(
                stream_id, pane_pid=tmux._pid,
            )
            assert lifecycle is not None
            assert await store.append_session_events_lifecycle_cas([
                {
                    "stream_id": stream_id,
                    "event": {
                        "stream_id": stream_id, "kind": "USER", "text": brief,
                        "timestamp": "2026-08-28T11:26:39.537Z",
                    },
                    "identity": "hostb-example-user-1",
                    "lifecycle": lifecycle,
                },
                {
                    "stream_id": stream_id,
                    "event": {
                        "stream_id": stream_id, "kind": "ASSIST_TEXT", "text": "assistant work",
                        "timestamp": "2026-08-28T11:26:44.379Z",
                    },
                    "identity": "hostb-example-assistant-1",
                    "lifecycle": lifecycle,
                },
                {
                    "stream_id": stream_id,
                    "event": {
                        "stream_id": stream_id, "kind": "TOOL_RESULT", "text": "tool work",
                        "timestamp": "2026-08-28T11:26:50.212Z",
                    },
                    "identity": "hostb-example-tool-1",
                    "lifecycle": lifecycle,
                },
            ], limit=500)
            await store.record_spawn_intent(peer_host, peer_name, {
                "open_fields": {"objective": "Exercise interrupted spawn adoption", "provider": "codex"},
                "brief": brief,
                "delivery_receipt": {
                    "transport": "native_argv",
                    "state": "requested",
                    "delivery_status": "pending",
                },
            })
            await store.mark_tmux_created(
                peer_host, peer_name, pane_pid=tmux._pid,
            )
            reconciled = await ctl.reconcile_spawn_intents(limit=1)
            outcome = await store.get_spawn_outcome(peer_host, peer_name)
            reply = await ctl.await_spawn({"stream_id": stream_id})
            assert outcome is not None
            return reconciled, outcome, reply, tmux
        finally:
            store.stop()

    reconciled, outcome, reply, tmux = asyncio.run(run())
    assert reconciled == {"adopted": 1, "released": 0}
    assert outcome["state"] == "delivered"
    assert outcome["delivery_evidence"] == "native_argv"
    assert reply["type"] == "await_spawn.ok"
    assert reply["delivery_evidence"] == "native_argv"
    assert tmux.pasted == []


def test_event_store_user_arriving_during_slow_transcript_probe_wins_final_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_BOOT_DEADLINE_S", 0.03)
    brief = "[example-service-notice:late-event]\narrived during transcript probe"

    class LateHostStore:
        def __init__(self) -> None:
            self.event_ready = False
            self.calls = 0

        async def fetch_session_event_tail(self, stream_id: str, *, limit: int) -> list[dict]:
            self.calls += 1
            if not self.event_ready:
                return []
            return [{
                "daemon_seq": 11,
                "stream_id": stream_id,
                "kind": "USER",
                "text": brief,
            }]

    async def run() -> tuple[str, str, str, AdoptTmux, LateHostStore]:
        db = Store(":memory:")
        db.start()
        try:
            event_store = LateHostStore()
            proof = DurableUserEventProof(event_store, local_host="hosta")
            tmux = AdoptTmux(alive=True, pane_pid=DEAD_PID, screen="model loading")
            sessions = Sessions(db, tmux=tmux, local_host="hosta")
            ctl = SpawnCtl(db, sessions, tmux=tmux, submission_proof=proof)

            async def slow_transcript(*_args: object, **_kwargs: object) -> str:
                # The peer USER lands while the advisory peer probe is
                # still in flight; the final authoritative lookup must see it.
                event_store.event_ready = True
                await asyncio.sleep(1.0)
                return "no_transcript"

            ctl._transcript_status = slow_transcript  # type: ignore[method-assign]
            return await ctl._settle_adoption(
                "hostb", "late-event", brief, tmux,
                delivery_receipt={
                    "proof_watermark": 0,
                    "proof_watermark_state": "reachable",
                },
            ) + (tmux, event_store)
        finally:
            db.stop()

    result = asyncio.run(run())
    assert result[:3] == (
        "delivered", "event_store",
        "adopted_after_restart: exact post-watermark USER event present in generation-fenced Store",
    )
    assert result[3].pasted == []
    assert result[4].calls >= 2


def test_slow_transcript_probe_keeps_absolute_deadline_and_runs_final_store_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deadline_s = 0.05
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_BOOT_DEADLINE_S", deadline_s)
    brief = "[example-service-notice:deadline]\nfinal read remains bounded"

    class PendingProof:
        def __init__(self) -> None:
            self.calls: list[tuple[float, float]] = []

        async def lookup(self, stream_id: str, *, expected_text: str,
                         watermark: object, timeout_s: float) -> EventProof:
            self.calls.append((time.monotonic(), timeout_s))
            return EventProof("pending", stream_id, 0)

    async def run() -> tuple[tuple[str, str, str], float, list[tuple[float, float]], float]:
        db = Store(":memory:")
        db.start()
        try:
            proof = PendingProof()
            tmux = AdoptTmux(alive=True, pane_pid=DEAD_PID, screen="model loading")
            sessions = Sessions(db, tmux=tmux, local_host="hosta")
            ctl = SpawnCtl(db, sessions, tmux=tmux, submission_proof=proof)
            probe_started: list[float] = []

            async def slow_transcript(*_args: object, **_kwargs: object) -> str:
                probe_started.append(time.monotonic())
                await asyncio.sleep(1.0)
                return "no_transcript"

            ctl._transcript_status = slow_transcript  # type: ignore[method-assign]
            started = time.monotonic()
            result = await ctl._settle_adoption(
                "hostb", "deadline", brief, tmux,
                delivery_receipt={
                    "proof_watermark": 0,
                    "proof_watermark_state": "reachable",
                },
            )
            return result, time.monotonic() - started, proof.calls, probe_started[0]
        finally:
            db.stop()

    result, elapsed, calls, probe_started = asyncio.run(run())
    assert result[:2] == ("failed", "agent_never_started")
    assert len(calls) == 2
    assert calls[1][0] >= probe_started
    assert calls[1][1] > 0
    assert elapsed < deadline_s + 0.25


def test_final_store_slice_survives_advisory_probe_scheduler_overrun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final durable read uses its reserved slice even after a late wakeup."""
    deadline_s = 0.03
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_BOOT_DEADLINE_S", deadline_s)
    brief = "[example-service-notice:late-final-proof]\\nreserved lookup remains mandatory"

    class PendingProof:
        def __init__(self) -> None:
            self.calls: list[float] = []

        async def lookup(self, stream_id: str, *, expected_text: str,
                         watermark: object, timeout_s: float) -> EventProof:
            self.calls.append(timeout_s)
            return EventProof("pending", stream_id, 0)

    async def run() -> tuple[tuple[str, str, str], list[float]]:
        db = Store(":memory:")
        db.start()
        try:
            proof = PendingProof()
            tmux = AdoptTmux(alive=True, pane_pid=DEAD_PID, screen="model loading")
            sessions = Sessions(db, tmux=tmux, local_host="hosta")
            ctl = SpawnCtl(db, sessions, tmux=tmux, submission_proof=proof)

            async def late_probe(*_args: object, **_kwargs: object) -> str:
                await asyncio.sleep(deadline_s * 2)
                return "no_transcript"

            ctl._bounded_adoption_transcript_status = late_probe  # type: ignore[method-assign]
            result = await ctl._settle_adoption(
                "hostb", "late-final-proof", brief, tmux,
                delivery_receipt={
                    "proof_watermark": 0,
                    "proof_watermark_state": "reachable",
                },
            )
            return result, proof.calls
        finally:
            db.stop()

    result, calls = asyncio.run(run())
    assert result[:2] == ("failed", "agent_never_started")
    assert len(calls) == 2
    assert calls[1] > 0


def test_unreachable_native_initial_event_proof_is_indeterminate_without_paste(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_BOOT_DEADLINE_S", 0.03)
    peer_host = "hostb"
    peer_name = "store-unavailable-edge"
    stream_id = f"{peer_host}:{peer_name}"
    brief = "[example-service-notice:store-unavailable]\nkeep reservation"

    class UnavailableProof:
        def __init__(self) -> None:
            self.calls = 0

        async def wait_for_initial_user_event(self, stream_id: str, *, expected_text: str,
                                              timeout_s: float) -> EventProof:
            self.calls += 1
            return EventProof("unreachable", stream_id, 0, reason="event_store_timeout")

    async def run() -> tuple[dict[str, int], dict[str, object], list[dict], object, AdoptTmux, UnavailableProof]:
        db = Store(":memory:")
        db.start()
        try:
            tmux = PeerEventTmux(alive=True, pane_pid="3251384", screen="model loading")

            class PeerHosts:
                peers = {peer_host: object()}

                def is_online(self, host: str) -> bool:
                    return host == peer_host

                def tmux_for(self, host: str) -> PeerEventTmux:
                    assert host == peer_host
                    return tmux

            hosts = PeerHosts()
            proof = UnavailableProof()
            sessions = Sessions(db, tmux=tmux, local_host="hosta", hosts=hosts)
            ctl = SpawnCtl(db, sessions, tmux=tmux, hosts=hosts, submission_proof=proof)
            assert await db.reserve_stream_id(
                peer_host, peer_name, ttl_s=60.0,
                request_id="store-unavailable-request", nonce="TEST",
            )
            await db.record_spawn_intent(peer_host, peer_name, {
                "open_fields": {"objective": "Exercise interrupted spawn adoption", "provider": "codex"},
                "brief": brief,
                "delivery_receipt": {
                    "transport": "native_argv",
                    "state": "requested",
                    "delivery_status": "pending",
                },
            })
            await db.mark_tmux_created(peer_host, peer_name, pane_pid=tmux._pid)
            reconciled = await ctl.reconcile_spawn_intents(limit=1)
            outcome = await db.get_spawn_outcome(peer_host, peer_name)
            reservations = await db.reservations(include_expired=True)
            assert outcome is not None
            return reconciled, outcome, reservations, await db.fetch_session(peer_host, peer_name), tmux, proof
        finally:
            db.stop()

    reconciled, outcome, reservations, row, tmux, proof = asyncio.run(run())
    assert reconciled == {"adopted": 0, "released": 0}
    assert outcome["state"] == "indeterminate"
    assert outcome["delivery_evidence"] == "live_pane_unproven"
    assert row is not None and row["bootstrap_state"] == "starting"
    assert len(reservations) == 1
    assert tmux.pasted == []
    assert proof.calls == 1


def test_post_restart_adoption_does_not_fail_a_live_seat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_BOOT_DEADLINE_S", 0.03)

    class PendingProof:
        async def wait_for_initial_user_event(
            self, stream_id: str, *, expected_text: str, timeout_s: float,
        ) -> EventProof:
            return EventProof("pending", stream_id, 0, reason="provider_initializing")

    async def run() -> tuple[dict[str, int], dict, dict, list[dict]]:
        store = Store(":memory:")
        store.start()
        try:
            host, name = "hostb", "post-restart-pending"
            tmux = PeerEventTmux(alive=True, pane_pid="3251384", screen="model loading")

            class PeerHosts:
                peers = {host: object()}

                def is_online(self, candidate: str) -> bool:
                    return candidate == host

                def tmux_for(self, candidate: str) -> PeerEventTmux:
                    assert candidate == host
                    return tmux

            hosts = PeerHosts()
            sessions = Sessions(store, tmux=tmux, local_host="hosta", hosts=hosts)
            ctl = SpawnCtl(store, sessions, tmux=tmux, hosts=hosts, submission_proof=PendingProof())
            assert await store.reserve_stream_id(
                host, name, ttl_s=60, request_id="post-restart-request", nonce="TEST",
            )
            await store.record_spawn_intent(host, name, {
                "open_fields": {"objective": "Exercise interrupted spawn adoption", "provider": "codex"},
                "brief": "native pending prompt",
                "delivery_receipt": {"transport": "native_argv", "state": "requested"},
            })
            await store.mark_tmux_created(host, name, pane_pid=tmux._pid)
            reconciled = await ctl.reconcile_spawn_intents(limit=1)
            return (
                reconciled,
                await store.fetch_session(host, name) or {},
                await store.get_spawn_outcome(host, name) or {},
                await store.reservations(include_expired=True),
            )
        finally:
            store.stop()

    reconciled, row, outcome, reservations = asyncio.run(run())
    assert reconciled == {"adopted": 0, "released": 0}
    assert row["status"] == "open"
    assert row["bootstrap_state"] == "starting"
    assert outcome["state"] == "indeterminate"
    assert outcome["delivery_evidence"] == "live_pane_unproven"
    receipt = outcome["delivery_receipt"]
    assert receipt["failure_code"] == "native_initial_prompt_delivery_unproven"
    assert receipt["failure_reason"] == outcome["reason"]
    assert receipt["delivery_failed_at"]
    assert reservations


def test_transcript_absent_with_unreachable_store_never_repastes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_BOOT_DEADLINE_S", 0.2)
    brief = "[example-service-notice:absent-unavailable]\nnot safe to resend"

    class UnavailableProof:
        async def lookup(self, stream_id: str, *, expected_text: str,
                         watermark: object, timeout_s: float) -> EventProof:
            return EventProof("unreachable", stream_id, 0, reason="event_store_unreachable")

    async def run() -> tuple[str, str, str, AdoptTmux]:
        db = Store(":memory:")
        db.start()
        try:
            tmux = AdoptTmux(alive=True, pane_pid=DEAD_PID, screen="$ ")
            proof = UnavailableProof()
            sessions = Sessions(db, tmux=tmux, local_host="hosta")
            ctl = SpawnCtl(db, sessions, tmux=tmux, submission_proof=proof)

            async def transcript_absent(*_args: object, **_kwargs: object) -> str:
                return "absent"

            ctl._transcript_status = transcript_absent  # type: ignore[method-assign]
            return await ctl._settle_adoption(
                "hostc", "absent-unavailable", brief, tmux,
                delivery_receipt={
                    "proof_watermark": 0,
                    "proof_watermark_state": "reachable",
                },
            ) + (tmux,)
        finally:
            db.stop()

    result = asyncio.run(run())
    assert result[:3] == (
        "indeterminate", "live_pane_unproven",
        "event_store_unreachable: adoption reconciliation deferred",
    )
    assert result[3].pasted == []


def test_peer_no_event_and_no_transcript_still_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_BOOT_DEADLINE_S", 0.3)
    brief = "[example-service-notice:no-event]\nnever submitted"

    async def run() -> tuple[str, str, str]:
        store = Store(":memory:")
        store.start()
        try:
            tmux = PeerEventTmux(alive=True, pane_pid=DEAD_PID, screen="model loading")
            sessions = Sessions(store, tmux=tmux, local_host="hosta")
            proof = DurableUserEventProof(store, local_host="hosta")
            ctl = SpawnCtl(store, sessions, tmux=tmux, hosts=None, submission_proof=proof)
            return await ctl._settle_adoption(
                "hostc", "no-event", brief, tmux,
                delivery_receipt={
                    "proof_watermark": 0,
                    "proof_watermark_state": "reachable",
                },
            )
        finally:
            store.stop()

    assert asyncio.run(run()) == (
        "failed", "agent_never_started",
        "boot deadline expired with no provider transcript ever created",
    )


def test_dead_pane_settles_failed() -> None:
    tmux = AdoptTmux(alive=False, pane_pid=DEAD_PID)
    outcome, extra = asyncio.run(_adopt("s-dead", tmux))
    assert extra["reconcile"]["released"] == 1
    assert outcome is not None
    assert outcome["state"] == "failed"
    assert extra["await"]["type"] == "await_spawn.error"


def test_ephemeral_probe_reservation_is_never_adopted() -> None:
    """A `usage-check-*` reservation (which should never exist — probes bypass
    spawnctl) is released, never adopted into the registry, and tmux is never
    consulted for it (test case: a probe pane must never surface as a chat)."""
    tmux = AdoptTmux(alive=True, pane_pid=str(os.getpid()), screen="$ ")
    consulted: list[str] = []
    original_has_session = tmux.has_session

    async def _recording_has_session(name: str) -> bool:
        consulted.append(name)
        return await original_has_session(name)

    tmux.has_session = _recording_has_session  # type: ignore[method-assign]
    outcome, extra = asyncio.run(_adopt("usage-check-1754433564", tmux))
    assert extra["reconcile"]["adopted"] == 0
    assert extra["reconcile"]["released"] == 1
    assert consulted == []  # probe reservation released before any tmux query
    assert tmux.pasted == []
    assert outcome is not None
    assert outcome["state"] == "failed"
    assert "probe" in outcome["reason"]


def test_registration_failure_does_not_leave_a_live_unregistered_claude_pane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed row write after pane creation must fail closed.

    The reservation proves that this pane belongs to the interrupted spawn. If
    registration cannot complete, reconciliation must roll that pane back and
    settle the durable spawn outcome; otherwise a Claude seat remains live but
    invisible to every session verb.
    """

    async def _fail_registration(*_args, **_kwargs):
        raise RuntimeError("sessions db unavailable")

    monkeypatch.setattr(Sessions, "open", _fail_registration)

    async def _go() -> tuple[
        RegistrationFailureTmux, dict, dict | None, dict | None, list[dict], Exception | None
    ]:
        store = Store(":memory:")
        store.start()
        try:
            tmux = RegistrationFailureTmux()
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            spawnctl = SpawnCtl(store, sessions, tmux=tmux)

            async def _pane_nonce(*_args: object) -> tuple[bool, str]:
                return (True, "TEST")

            spawnctl._tmux_nonce = _pane_nonce  # type: ignore[method-assign]
            assert await store.reserve_stream_id(
                HOST, "claude-registration-failure", ttl_s=60.0, request_id="r-fail",
                nonce="TEST",
            )
            await store.record_spawn_intent(
                HOST,
                "claude-registration-failure",
                {"open_fields": {"objective": "Exercise interrupted spawn adoption", "provider": "claude"}, "brief": ""},
            )
            await store.mark_tmux_created(HOST, "claude-registration-failure")
            reconcile_error = None
            try:
                reconciled = await spawnctl.reconcile_spawn_intents()
            except Exception as exc:  # the cleanup path must not escape
                reconcile_error = exc
                reconciled = {"adopted": 0, "released": 0}
            return (
                tmux,
                reconciled,
                await store.fetch_session(HOST, "claude-registration-failure"),
                await store.get_spawn_outcome(HOST, "claude-registration-failure"),
                await store.reservations(include_expired=True),
                reconcile_error,
            )
        finally:
            store.stop()

    tmux, reconciled, row, outcome, reservations, reconcile_error = asyncio.run(_go())
    assert reconcile_error is None, (
        f"reconcile escaped after registration failure: {reconcile_error}; "
        f"pane_alive={tmux.alive}, row={row}, reservations={reservations}"
    )
    assert reconciled == {"adopted": 0, "released": 1}
    assert tmux.killed is True
    assert row is None
    assert reservations == []
    assert outcome is not None
    assert outcome["state"] == "failed"
    assert "session registration failed" in outcome["reason"]
