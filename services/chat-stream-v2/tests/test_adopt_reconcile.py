"""Adoption of a live pane reconciles to a definite outcome. Outcomes are
delivered or failed once authoritative evidence is reachable; a Store timeout
defers to the existing scheduled reconciliation:

  transcript needle present   -> delivered/transcript   (submitted before crash)
  transcript located, absent  -> failed/transcript_absent
  no transcript by boot line  -> failed/agent_never_started (live pane, no provider)
  dead pane                   -> failed (in the caller)
"""

from __future__ import annotations

from submission_store_stub import SubmissionReadStoreStub
import asyncio
import errno
import json
import os
import shutil
import time
from pathlib import Path

import pytest

import spawnctl as spawnctl_mod  # noqa: E402
from sessions import Sessions, VerbError  # noqa: E402
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
            HOST, name, ttl_s=30, request_id="r1", nonce="TEST"
        )
        assert await store.record_spawn_intent(HOST, name, {"open_fields": {"objective": "Exercise interrupted spawn adoption", }, "brief": BRIEF})
        await store.mark_tmux_created(HOST, name)
        # Model the delayed restart deterministically, after intent persistence.
        def expire(conn):
            conn.execute("UPDATE v2_stream_reservations SET expires_at=0 WHERE host=? AND session_name=?", (HOST, name))
            conn.commit()
        await store.submit(expire)
        result = await spawnctl.reconcile_spawn_intents()
        reply = await spawnctl.await_spawn({"stream_id": f"{HOST}:{name}"})
        return await store.get_spawn_outcome(HOST, name), {"reconcile": result, "await": reply}
    finally:
        store.stop()


def _held_transcript(tmp_path: Path, content: str):
    """A provider `.jsonl` THIS process holds open (so lsof against our own pid —
    handed in as the pane pid — locates it), seeded with `content`."""
    log = tmp_path / ".claude" / "projects" / "example" / "sess.jsonl"
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
    # Scaled to this test's short window: the production floor and expiry
    # transcript allowance would not fit inside it.
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_MIN_EVENT_LOOKUP_S", 0.01)
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_EXPIRY_TRANSCRIPT_OBSERVATION_S", 0.03)

    class LateHostStore(SubmissionReadStoreStub):
        def __init__(self) -> None:
            self.event_ready = False
            self.calls = 0

        async def fetch_session_event_tail(self, stream_id: str, *, limit: int, after_daemon_seq: int | None = None) -> list[dict]:
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
    # Scaled to this test's short window: the production floor and expiry
    # transcript allowance would not fit inside it.
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_MIN_EVENT_LOOKUP_S", 0.01)
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_EXPIRY_TRANSCRIPT_OBSERVATION_S", deadline_s)

    order: list[str] = []

    class PendingProof:
        def __init__(self) -> None:
            self.calls: list[tuple[float, float]] = []

        async def lookup(self, stream_id: str, *, expected_text: str,
                         watermark: object, timeout_s: float) -> EventProof:
            self.calls.append((time.monotonic(), timeout_s))
            order.append("store_lookup")
            return EventProof("pending", stream_id, 0)

    async def run() -> tuple[tuple[str, str, str], list[tuple[float, float]], float]:
        db = Store(":memory:")
        db.start()
        try:
            proof = PendingProof()
            tmux = AdoptTmux(alive=True, pane_pid=DEAD_PID, screen="model loading")
            sessions = Sessions(db, tmux=tmux, local_host="hosta")
            ctl = SpawnCtl(db, sessions, tmux=tmux, submission_proof=proof)
            probe_started: list[float] = []
            probe_release = asyncio.Event()

            async def slow_transcript(*_args: object, **_kwargs: object) -> str:
                probe_started.append(time.monotonic())
                try:
                    await probe_release.wait()  # released only after settlement returns
                    order.append("probe_completed")
                except asyncio.CancelledError:
                    order.append("probe_cancelled")
                    raise
                return "no_transcript"

            ctl._transcript_status = slow_transcript  # type: ignore[method-assign]
            # Stuck detector only: the absolute deadline is proven by order below.
            result = await asyncio.wait_for(ctl._settle_adoption(
                "hostb", "deadline", brief, tmux,
                delivery_receipt={
                    "proof_watermark": 0,
                    "proof_watermark_state": "reachable",
                },
            ), timeout=10.0)
            probe_release.set()
            return result, proof.calls, probe_started[0]
        finally:
            db.stop()

    result, calls, probe_started = asyncio.run(run())
    assert result[:2] == ("failed", "agent_never_started")
    # The deadline cancelled the slow probe, and the expiry observation's own
    # allowance cancelled the second, before the final Store read; an
    # abandoned or awaited probe cannot produce this order.
    assert order == ["store_lookup", "probe_cancelled", "probe_cancelled", "store_lookup"]
    assert len(calls) == 2
    assert calls[1][0] >= probe_started
    assert calls[1][1] > 0


def test_final_store_slice_survives_advisory_probe_scheduler_overrun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final durable read uses its reserved slice even after a late wakeup."""
    deadline_s = 0.03
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_BOOT_DEADLINE_S", deadline_s)
    brief = "[example-service-notice:late-final-proof]\\nreserved lookup remains mandatory"
    # Scaled to this test's short window: the production floor and expiry
    # transcript allowance would not fit inside it.
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_MIN_EVENT_LOOKUP_S", 0.01)
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_EXPIRY_TRANSCRIPT_OBSERVATION_S", deadline_s)

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


@pytest.mark.parametrize("delayed_final_read", [False, True], ids=["reachable", "delayed-final-read"])
def test_peer_no_event_and_no_transcript_still_fails_closed(
    monkeypatch: pytest.MonkeyPatch, delayed_final_read: bool,
) -> None:
    import threading
    from reconciler import SessionReconciler

    monkeypatch.setattr(spawnctl_mod, "ADOPTION_BOOT_DEADLINE_S", 0.3)
    brief = "[example-service-notice:no-event]\nnever submitted"
    started = time.perf_counter()
    host, name, request_id = "hostc", "no-event", "owned-no-event"
    # The hold targets the expiry gate: the first Store read after the expiry
    # observation starts, whatever its ordinal.
    control = {"applied": False, "expiry_phase": False}
    trace = {"schema": "adoption-scheduled-reconcile.v1", "delayed_final_read": delayed_final_read,
             "settlements": [], "proofs": [], "reconcile_calls": [], "control": control,
             "peer_probe_counts": {"birth": 0, "tree": 0, "lsof": 0}, "unexpected_commands": []}
    releases = []
    blocker_tasks = []

    async def run() -> tuple[str, str, str]:
        store = Store(":memory:")
        store.start()
        scheduled_task = None
        try:
            tmux = PeerEventTmux(alive=True, pane_pid=DEAD_PID, screen="model loading")

            class PeerHosts:
                local_host = "hosta"
                peers = {host: object()}

                def is_online(self, candidate):
                    return candidate in {self.local_host, host}

                def tmux_for(self, candidate):
                    assert candidate == host
                    return tmux

                async def run_command(self, candidate, *argv, **kwargs):
                    assert candidate == host
                    if argv == ("ps", "-o", "lstart=", "-p", DEAD_PID):
                        trace["peer_probe_counts"]["birth"] += 1
                        return 1, ""  # The synthetic pane has no readable process birth.
                    if argv == ("ps", "-eo", "pid=,ppid="):
                        trace["peer_probe_counts"]["tree"] += 1
                        return 0, ""  # No synthetic provider descendants exist.
                    if argv == ("lsof", "-p", DEAD_PID, "-Fn"):
                        trace["peer_probe_counts"]["lsof"] += 1
                        return 1, ""  # No process owns a provider transcript.
                    trace["unexpected_commands"].append({"program": argv[0] if argv else None, "argc": len(argv)})
                    raise AssertionError("unexpected owned peer probe")

            hosts = PeerHosts()
            sessions = Sessions(store, tmux=tmux, local_host="hosta", hosts=hosts)
            proof = DurableUserEventProof(store, local_host="hosta")
            ctl = SpawnCtl(store, sessions, tmux=tmux, hosts=hosts, submission_proof=proof)
            original_tail = store.fetch_session_event_tail
            original_proof = ctl._adoption_event_proof
            original_settle = ctl._settle_adoption
            original_reconcile = ctl.reconcile_spawn_intents
            tail_count = 0

            async def observed_tail(*args, **kwargs):
                nonlocal tail_count
                tail_count += 1
                release = None
                try:
                    if delayed_final_read and control["expiry_phase"] and not control["applied"]:
                        control["applied"] = True
                        release = threading.Event()
                        releases.append(release)
                        entered = asyncio.Event()
                        loop = asyncio.get_running_loop()

                        def hold_owned_worker(_conn):
                            control["worker_enter_s"] = time.perf_counter() - started
                            loop.call_soon_threadsafe(entered.set)
                            try:
                                release.wait()
                            finally:
                                control["worker_return_s"] = time.perf_counter() - started

                        blocker_tasks.append(asyncio.create_task(store.submit(hold_owned_worker)))
                        await entered.wait()
                        control["entered_ack_s"] = time.perf_counter() - started
                        control["progress_before_target"] = store.progress.snapshot()
                    return await original_tail(*args, **kwargs)
                except BaseException as exc:
                    if release is not None:
                        control["target_exception"] = type(exc).__name__
                    raise
                finally:
                    if release is not None:
                        control["progress_at_release"] = store.progress.snapshot()
                        control["release_s"] = time.perf_counter() - started
                        release.set()

            async def observed_proof(*args, **kwargs):
                value = await original_proof(*args, **kwargs)
                trace["proofs"].append({"timeout_s": kwargs["timeout_s"], **value.audit_fields()})
                return value

            async def observed_settle(*args, **kwargs):
                value = await original_settle(*args, **kwargs)
                trace["settlements"].append(value)
                return value

            async def observed_reconcile(*args, **kwargs):
                trace["reconcile_calls"].append(dict(kwargs))
                return await original_reconcile(*args, **kwargs)

            monkeypatch.setattr(store, "fetch_session_event_tail", observed_tail)
            monkeypatch.setattr(ctl, "_adoption_event_proof", observed_proof)
            monkeypatch.setattr(ctl, "_settle_adoption", observed_settle)
            monkeypatch.setattr(ctl, "reconcile_spawn_intents", observed_reconcile)
            original_observation = ctl._bounded_adoption_transcript_status

            async def observed_observation(*args, **kwargs):
                if kwargs.get("allowance_s") is not None:
                    control["expiry_phase"] = True
                return await original_observation(*args, **kwargs)

            monkeypatch.setattr(ctl, "_bounded_adoption_transcript_status", observed_observation)
            assert await store.reserve_stream_id(host, name, ttl_s=30, request_id=request_id, nonce="TEST")
            assert await store.record_spawn_intent(host, name, {
                "open_fields": {"objective": "Exercise interrupted spawn adoption", "provider": "claude"},
                "brief": brief,
                "delivery_receipt": {"proof_watermark": 0, "proof_watermark_state": "reachable"},
            })
            await store.mark_tmux_created(host, name, pane_pid=DEAD_PID)
            # main() performs startup reconciliation before scheduling run_forever().
            startup = await ctl.reconcile_spawn_intents()
            assert len(trace["settlements"]) == 1
            initial = trace["settlements"][0]
            trace["initial"] = initial
            if initial[0] == "indeterminate":
                assert initial == (
                    "indeterminate", "live_pane_unproven",
                    "event_store_timeout: adoption reconciliation deferred",
                )
                assert startup == {"adopted": 0, "released": 0}
                retained = await store.reservations(include_expired=True)
                assert len(retained) == 1 and retained[0]["request_id"] == request_id
                pending = await store.get_spawn_outcome(host, name)
                assert pending["state"] == "indeterminate"
                assert pending["reason"] == initial[2]
                assert pending["delivery_receipt"]["proof_state"] == "unreachable"
                scheduled = asyncio.get_running_loop().create_future()

                async def on_tick():
                    trace["scheduled_tick_completed"] = True
                    scheduled.set_result(None)

                reconciler = SessionReconciler(sessions, hosts, spawnctl=ctl, on_reconcile_tick=on_tick)
                trace["interval_s"] = reconciler.cfg.interval_s
                original_once = reconciler.reconcile_once

                async def observed_once():
                    try:
                        return await original_once()
                    except Exception as exc:
                        # Fail on the first broken pass instead of consuming error-backoff retries.
                        scheduled.set_exception(exc)
                        raise

                monkeypatch.setattr(reconciler, "reconcile_once", observed_once)
                scheduled_task = asyncio.create_task(reconciler.run_forever(), name="owned-adoption-first-pass")
                await scheduled  # The real first pass is immediate; later passes keep the 60 s interval.
                assert trace["reconcile_calls"] == [{}, {"limit": 1, "recurring": True}]
                assert len(trace["settlements"]) == 2
            else:
                assert startup == {"adopted": 1, "released": 0}
            outcome = await store.get_spawn_outcome(host, name)
            assert outcome is not None
            value = (outcome["state"], outcome["delivery_evidence"], outcome["reason"])
            trace["terminal"] = value
            trace["remaining_reservations"] = len(await store.reservations(include_expired=True))
            trace["tmux_pastes"] = len(tmux.pasted)
            return value
        finally:
            if scheduled_task is not None:
                scheduled_task.cancel()
                await asyncio.gather(scheduled_task, return_exceptions=True)
                trace["scheduled_task_cancelled"] = scheduled_task.cancelled()
            for gate in releases:
                gate.set()
            worker = store._thread
            trace["store_stopped"] = store.stop()
            await asyncio.gather(*blocker_tasks, return_exceptions=True)
            control["blocker_tasks_done"] = [task.done() for task in blocker_tasks]
            control["blocker_task_errors"] = [
                "CancelledError" if task.cancelled() else
                (type(task.exception()).__name__ if task.exception() else None)
                for task in blocker_tasks]
            trace["worker_alive_after_stop"] = bool(worker and worker.is_alive())
            trace["store_progress_after_stop"] = store.progress.snapshot()

    try:
        assert asyncio.run(run()) == (
            "failed", "agent_never_started",
            "boot deadline expired with no provider transcript ever created",
        )
    finally:
        trace["elapsed_s"] = time.perf_counter() - started
        evidence_root = os.environ.get("PENTACLE_GATE_EVIDENCE_DIR")
        if evidence_root:
            mode = "delayed-final-read" if delayed_final_read else "reachable"
            try:
                (Path(evidence_root) / "gate" / f"adoption-reconcile-{mode}.json").write_text(
                    json.dumps(trace, sort_keys=True) + "\n", encoding="utf-8")
            except OSError as exc:
                trace["persistence_error_type"] = type(exc).__name__
        print("ADOPTION_RECONCILE " + json.dumps(trace, sort_keys=True), flush=True)
    assert trace["remaining_reservations"] == 0
    assert trace["tmux_pastes"] == 0
    assert trace["unexpected_commands"] == []
    assert all(count > 0 for count in trace["peer_probe_counts"].values())
    assert trace["store_stopped"] is True
    assert trace["worker_alive_after_stop"] is False
    progress = trace["store_progress_after_stop"]
    assert progress["pending_count"] == 0
    assert progress["enqueued_seq"] == progress["started_seq"] == progress["finished_seq"]
    if delayed_final_read:
        assert control["applied"] is True
        assert trace["scheduled_tick_completed"] is True
        assert trace["scheduled_task_cancelled"] is True
        assert trace["interval_s"] == 60.0
        assert trace["initial"] == (
            "indeterminate", "live_pane_unproven",
            "event_store_timeout: adoption reconciliation deferred",
        )
        # In the first settlement every window read answered pending and only
        # the held expiry gate, its last read, timed out with its full grant.
        held = next(i for i, p in enumerate(trace["proofs"]) if p["proof_state"] != "pending")
        assert held >= 1 and all(p["proof_state"] == "pending" for p in trace["proofs"][:held])
        assert trace["proofs"][held]["proof_state"] == "unreachable"
        assert trace["proofs"][held]["proof_reason"] == "event_store_timeout"
        assert trace["proofs"][held]["timeout_s"] == spawnctl_mod.ADOPTION_MIN_EVENT_LOOKUP_S
        before, after = control["progress_before_target"], control["progress_at_release"]
        assert before["enqueued_seq"] == before["started_seq"] == before["finished_seq"] + 1
        assert after["enqueued_seq"] == before["enqueued_seq"] + 1
        assert after["started_seq"] == before["started_seq"]
        assert after["finished_seq"] == before["finished_seq"]
        assert after["pending_count"] == 1
        assert control["worker_enter_s"] <= control["entered_ack_s"] <= control["release_s"] <= control["worker_return_s"]
        assert control["target_exception"] == "CancelledError"
        assert control["blocker_tasks_done"] == [True]
        assert control["blocker_task_errors"] == [None]
    else:
        assert control["applied"] is False




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


class AperturePane(RegistrationFailureTmux):
    """Owned pane with observable nonce, readiness, and teardown uncertainty."""

    def __init__(self, *, screen="provider loading", nonce="APERTURE", cleanup_unknown=False):
        super().__init__()
        self.screen = screen
        self.nonce = nonce
        self.cleanup_unknown = cleanup_unknown
        self.captures = 0

    async def pane_identity(self, name):
        if not self.alive:
            return None
        return {"pane_id": "%1", "pane_pid": self._pid, "session_name": name}

    async def kill_pane(self, pane_id):
        assert pane_id == "%1"
        await self.kill_session("adoption-aperture")

    async def capture(self, name):
        self.captures += 1
        return self.screen

    async def run(self, *args, **kwargs):
        if args and args[0] == "show-environment":
            return 0, f"{spawnctl_mod.PANE_NONCE_ENV}={self.nonce}\n"
        return await super().run(*args, **kwargs)

    async def kill_session(self, name):
        if self.cleanup_unknown:
            raise RuntimeError("cleanup transport unavailable")
        await super().kill_session(name)


async def _aperture_reconcile(pane, *, prior=None, admitted=False, provider="codex",
                              receipt=None, unreadable=False, after_first=None, bootstrap="starting", tombstone=False):
    store = Store(":memory:")
    store.start()
    name = "adoption-aperture"
    try:
        sessions = Sessions(store, tmux=pane, local_host=HOST)
        ctl = SpawnCtl(store, sessions, tmux=pane)
        if prior:
            await sessions.open(HOST, name, session_generation="prior", pane_pid="1111")
            await store.update_session(HOST, name, status="closed", pane_status="pane_dead",
                                       close_kind=prior if prior != "retained" else None)
            await sessions.refresh()
        assert await store.reserve_stream_id(HOST, name, ttl_s=60, request_id="aperture-request",
                                             nonce="APERTURE")
        fields = {"objective": "Exercise interrupted adoption apertures", "provider": provider,
                  "session_generation": "intended", "bootstrap_state": bootstrap}
        intent = {"open_fields": fields, "brief": "",
                  "delivery_receipt": receipt or {"state": "not_requested"}}
        assert await store.record_spawn_intent(HOST, name, intent)
        if admitted:
            await sessions.open(HOST, name, **fields, pane_pid=await pane.pane_pid(name),
                                pane_status="pane_alive", fence="aperture-request")
        if prior == "newer":
            await store.open_session(HOST, name, session_generation="newer", pane_pid="9999")
            await sessions.refresh()
        if unreadable:
            await store.submit(lambda c: (c.execute("UPDATE v2_stream_reservations SET payload='unreadable'"), c.commit()))
        if tombstone:
            def closed_tombstone(conn):
                conn.execute("INSERT INTO v2_deferred_reap (stream_id,host,session_name,generation,requested_at,done_at) "
                             "VALUES (?,?,?,?,?,?)", (f"{HOST}:{name}", HOST, name, "intended", "closed", "done"))
                conn.commit()
            await store.submit(closed_tombstone)
        result = await ctl.reconcile_spawn_intents()
        first = {"outcome": await store.get_spawn_outcome(HOST, name),
                 "row": await store.fetch_session(HOST, name)}
        if after_first:
            await after_first(ctl, store, sessions, pane, name)
            result = await ctl.reconcile_spawn_intents()
        return {"first": first, "reconcile": result, "row": await store.fetch_session(HOST, name),
                "outcome": await store.get_spawn_outcome(HOST, name),
                "reservations": await store.reservations(include_expired=True),
                "inventory": sessions.list_open(),
                "await": await ctl.await_spawn({"stream_id": f"{HOST}:{name}"}),
                "status": await ctl.spawn_status({"host": HOST, "session_name": name}, HOST)}
    finally:
        store.stop()


def test_pane_before_generation_admission_cleans_exact_owned_pane():
    pane = AperturePane()
    result = asyncio.run(_aperture_reconcile(pane, prior="retained"))
    assert not pane.alive, "owned pane must not outlive a retired pre-admission intent"
    assert result["reconcile"] == {"adopted": 0, "released": 1}
    assert result["row"]["session_generation"] == "prior"
    assert result["row"]["status"] == "closed"
    assert result["inventory"] == []
    assert result["reservations"] == []
    assert result["outcome"]["state"] == "failed"
    assert result["outcome"]["delivery_evidence"] == "pane_cleanup_confirmed"
    assert result["await"]["type"] == "await_spawn.error"
    assert not pane.pasted


def test_pane_before_admission_unknown_cleanup_keeps_durable_handle():
    pane = AperturePane(cleanup_unknown=True)
    result = asyncio.run(_aperture_reconcile(pane, prior="retained"))
    assert pane.alive
    assert result["reconcile"] == {"adopted": 0, "released": 0}
    assert len(result["reservations"]) == 1
    assert result["outcome"]["state"] == "indeterminate"
    assert result["outcome"]["delivery_evidence"] == "pane_cleanup_unknown"
    assert result["row"]["session_generation"] == "prior"
    assert not pane.pasted


@pytest.mark.parametrize("prior,nonce", [("retained", "FOREIGN"), ("newer", "APERTURE"),
                                        ("operator", "APERTURE")])
def test_pane_before_admission_preserves_foreign_newer_and_closed(prior, nonce):
    pane = AperturePane(nonce=nonce)
    result = asyncio.run(_aperture_reconcile(pane, prior=prior))
    assert pane.alive and not pane.killed and not pane.pasted
    assert result["row"]["session_generation"] == ("newer" if prior == "newer" else "prior")
    assert result["outcome"]["state"] == "failed"


@pytest.mark.parametrize("screen,expected", [
    ("OpenAI Codex\n─────────\n› explain this codebase\n  gpt-5-codex high", "ready"),
    ("provider loading", "starting"),
])
def test_promptless_adoption_observes_bootstrap_separately(monkeypatch, screen, expected):
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_BOOT_DEADLINE_S", 0.01)
    pane = AperturePane(screen=screen)
    result = asyncio.run(_aperture_reconcile(pane, admitted=True))
    assert pane.captures > 0, "no brief does not prove provider readiness"
    assert result["row"]["bootstrap_state"] == expected
    assert result["await"]["state"] == expected
    assert result["inventory"][0]["bootstrap_state"] == expected
    assert not pane.pasted and not pane.killed
    assert result["outcome"]["delivery_receipt"]["state"] == "not_requested"
    assert len(result["inventory"]) == 1
    assert result["row"]["session_generation"] == "intended"
    if expected == "starting":
        assert result["outcome"]["state"] == "indeterminate"
        assert len(result["reservations"]) == 1
    else:
        assert result["outcome"]["state"] == "delivered"
        assert not result["reservations"]


@pytest.mark.parametrize("provider,screen", [
    ("codex", "OpenAI Codex\n─────────\n› explain this codebase\n  gpt-5-codex high"),
    ("claude", "──────────────────────────\n❯\n──────────────────────────\n  ⏵⏵ bypass permissions on"),
])
def test_promptless_pending_readiness_converges_once_without_input(monkeypatch, provider, screen):
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_BOOT_DEADLINE_S", 0.01)
    pane = AperturePane()

    async def become_ready(ctl, store, sessions, pane, name):
        pane.screen = screen

    result = asyncio.run(_aperture_reconcile(pane, admitted=True, provider=provider, after_first=become_ready))
    assert result["first"]["outcome"]["state"] == "indeterminate"
    assert result["first"]["row"]["bootstrap_state"] == "starting"
    assert result["row"]["bootstrap_state"] == "ready"
    assert result["outcome"]["state"] == "delivered"
    assert result["outcome"]["delivery_receipt"]["state"] == "not_requested"
    assert not pane.pasted and not pane.killed
    assert len(result["inventory"]) == 1 and not result["reservations"]


@pytest.mark.parametrize("guard", ["reset", "tombstone"])
def test_promptless_adoption_preserves_durable_readiness_blocks(guard):
    pane = AperturePane()
    result = asyncio.run(_aperture_reconcile(pane, admitted=True,
                        bootstrap="reset_blocked" if guard == "reset" else "starting",
                        tombstone=guard == "tombstone"))
    assert result["reconcile"] == {"adopted": 0, "released": 0}
    assert pane.captures == 0 and pane.alive
    assert not pane.pasted and not pane.killed
    assert len(result["reservations"]) == 1


def test_unreadable_pre_admission_intent_fails_closed_without_default_registration():
    pane = AperturePane()
    result = asyncio.run(_aperture_reconcile(pane, unreadable=True))
    assert not pane.alive and pane.killed
    assert result["row"] is None and result["inventory"] == []
    assert result["outcome"]["state"] == "failed"
    assert not result["reservations"] and not pane.pasted


def test_owned_pane_without_prior_row_is_admitted_once_and_ready():
    pane = AperturePane(screen="OpenAI Codex\n─────────\n› explain this codebase\n  gpt-5-codex high")
    result = asyncio.run(_aperture_reconcile(pane))
    assert result["reconcile"] == {"adopted": 1, "released": 0}
    assert pane.alive and not pane.killed and not pane.pasted
    assert len(result["inventory"]) == 1
    assert result["row"]["session_generation"] == "intended"
    assert result["row"]["bootstrap_state"] == result["await"]["state"] == "ready"
    assert result["outcome"]["delivery_receipt"]["state"] == "not_requested"
    assert "delivery_ack_at" not in result["outcome"]["delivery_receipt"]
    assert not result["reservations"]


def test_promptless_pane_dying_during_readiness_reports_failed(monkeypatch):
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_BOOT_DEADLINE_S", 0.01)

    class DyingPane(AperturePane):
        async def capture(self, name):
            self.alive = False
            return await super().capture(name)

    pane = DyingPane()
    result = asyncio.run(_aperture_reconcile(pane, admitted=True))
    assert result["row"]["bootstrap_state"] == "failed"
    assert result["outcome"]["state"] == "failed"
    assert result["outcome"]["delivery_evidence"] == "pane_died_during_boot"
    assert result["outcome"]["delivery_receipt"]["state"] == "not_requested"
    assert result["await"]["type"] == "await_spawn.error"
    assert not result["reservations"] and not pane.pasted


def test_unreadable_pane_nonce_retains_recovery_without_cleanup():
    class UnreadableNoncePane(AperturePane):
        async def run(self, *args, **kwargs):
            return 1, "nonce read unavailable"

    pane = UnreadableNoncePane()
    result = asyncio.run(_aperture_reconcile(pane, prior="retained"))
    assert pane.alive and not pane.killed and not pane.pasted
    assert result["reconcile"] == {"adopted": 0, "released": 0}
    assert result["outcome"] is None and len(result["reservations"]) == 1


@pytest.mark.parametrize("change", ["nonce", "pid", "dead"])
def test_promptless_ready_observation_rechecks_original_pane_identity(change):
    class ChangedDuringObservation(AperturePane):
        async def capture(self, name):
            if change == "nonce":
                self.nonce = "FOREIGN"
            elif change == "pid":
                self._pid = "9999"
            else:
                self.alive = False
            return "OpenAI Codex\n─────────\n› explain this codebase\n  gpt-5-codex high"

    pane = ChangedDuringObservation()
    result = asyncio.run(_aperture_reconcile(pane, admitted=True))
    assert result["await"]["state"] != "ready", "ready chrome alone must not retire the original identity fence"
    assert result["row"]["pane_pid"] == "4321"
    assert not pane.pasted and not pane.killed
    if change == "dead":
        assert result["outcome"]["state"] == "failed"
        assert result["row"]["bootstrap_state"] == "failed"
    else:
        assert result["outcome"]["state"] == "indeterminate"
        assert result["row"]["bootstrap_state"] == "starting"
        assert len(result["reservations"]) == 1


# -- Settle-step boundary at boot expiry --------------------------------------
#
# A virtual clock bound only to ``spawnctl.time`` (asyncio keeps the real
# clock) lets the post-paste loop reach its boot deadline in a fixed geometry:
# the transcript source steps the clock to set instants, and the Store double
# has a modelled service cost, so a grant shorter than that cost times out the
# way a slow real read would. No assertion here reads wall time.

SETTLE_EXPIRY_S = 20.0
SETTLE_STORE_FLOOR_S = 0.25  # expected minimum offered to any Store lookup
SETTLE_TRANSCRIPT_ALLOWANCE_S = 3.0  # expected final transcript observation allowance
LEGACY_RECEIPT = {"proof_watermark": 0, "proof_watermark_state": "reachable"}


class SettleClock:
    """``spawnctl.time`` stand-in: only ``monotonic`` is virtual."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.start = start

    def monotonic(self) -> float:
        return self.now

    def rel(self) -> float:
        return round(self.now - self.start, 6)

    def __getattr__(self, name: str):
        return getattr(time, name)


class CostedProof:
    """Store proof double with a finite service cost per lookup; reads from
    ``hold_from_s`` on wait on ``gate`` (released at teardown) instead."""

    def __init__(self, clock: SettleClock, *, cost_s: float = 0.010,
                 user_from_s: float | None = None, unreachable_from_s: float | None = None,
                 events: list | None = None) -> None:
        self.clock = clock
        self.cost_s = cost_s
        self.user_from_s = user_from_s
        self.unreachable_from_s = unreachable_from_s
        self.events = events if events is not None else []
        self.calls: list[dict] = []
        self.hold_from_s: float | None = None
        self.gate = asyncio.Event()

    async def lookup(self, stream_id: str, *, expected_text: str,
                     watermark: object, timeout_s: float) -> EventProof:
        call = {"at": self.clock.rel(), "timeout_s": timeout_s, "result": None}
        self.calls.append(call)
        self.events.append(("store", call))
        if self.hold_from_s is not None and call["at"] >= self.hold_from_s:
            call["result"] = "held"
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                call["result"] = "held-cancelled"
                raise
        if timeout_s < self.cost_s:
            call["result"] = "timeout"
            raise asyncio.TimeoutError
        self.clock.now += self.cost_s
        if self.unreachable_from_s is not None and call["at"] >= self.unreachable_from_s:
            call["result"] = "unreachable"
            return EventProof("unreachable", stream_id, 0, reason="event_store_unreachable")
        if self.user_from_s is not None and call["at"] >= self.user_from_s:
            call["result"] = "user"
            return EventProof("proven", stream_id, 0, event_id=11)
        call["result"] = "pending"
        return EventProof("pending", stream_id, 0)


class SteppedTranscript:
    """Underlying transcript source that steps the virtual clock to fixed instants."""

    def __init__(self, clock: SettleClock, stops: list[float], *,
                 found_from_s: float | None = None, absent_from_s: float | None = None,
                 pane_dies_at_s: float | None = None, after_stops_step_s: float = 0.05,
                 events: list | None = None) -> None:
        self.clock = clock
        self.stops = list(stops)
        self.found_from_s = found_from_s
        self.absent_from_s = absent_from_s
        self.pane_dies_at_s = pane_dies_at_s
        self.after_stops_step_s = after_stops_step_s
        self.events = events if events is not None else []
        self.calls: list[dict] = []

    async def __call__(self, *_args: object, absolute_deadline: float | None = None,
                       tmux: AdoptTmux | None = None, **_kw: object) -> str:
        at = self.clock.rel()
        call = {"at": at, "allowance_s": None if absolute_deadline is None
                else round(absolute_deadline - self.clock.now, 6)}
        self.calls.append(call)
        self.events.append(("transcript", call))
        found = self.found_from_s is not None and at >= self.found_from_s
        if self.stops:
            self.clock.now = self.clock.start + self.stops.pop(0)
        else:
            self.clock.now += self.after_stops_step_s
        ended = self.clock.rel()
        if tmux is not None and self.pane_dies_at_s is not None and ended >= self.pane_dies_at_s:
            tmux.alive = False
        if found:
            return "found"
        if self.absent_from_s is not None and ended >= self.absent_from_s:
            return "absent"
        return "no_transcript"


class LoggingTmux(AdoptTmux):
    def __init__(self, events: list, clock: SettleClock) -> None:
        super().__init__(alive=True, pane_pid=DEAD_PID, screen="model loading")
        self.events = events
        self.clock = clock

    async def session_state(self, name: str) -> str:
        self.events.append(("pane", {"at": self.clock.rel(), "alive": self.alive}))
        return "alive" if self.alive else "gone"


# Transcript observations end 20 ms before expiry; the next one lands on it.
SETTLE_STOPS = [5.0, 10.0, 15.0, 19.98, SETTLE_EXPIRY_S]


def _settle_at_expiry(monkeypatch: pytest.MonkeyPatch, proof: CostedProof, transcript: SteppedTranscript,
                      clock: SettleClock, *, receipt: dict | None = None,
                      events: list | None = None, tmux: AdoptTmux | None = None) -> tuple[str, str, str]:
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_BOOT_DEADLINE_S", SETTLE_EXPIRY_S)
    monkeypatch.setattr(spawnctl_mod, "time", clock)
    brief = "[example-service-notice:expiry]\nsettle at the boot line"

    async def run() -> tuple[str, str, str]:
        db = Store(":memory:")
        db.start()
        try:
            pane = tmux or (LoggingTmux(events, clock) if events is not None
                            else AdoptTmux(alive=True, pane_pid=DEAD_PID, screen="model loading"))
            sessions = Sessions(db, tmux=pane, local_host="hosta")
            ctl = SpawnCtl(db, sessions, tmux=pane, submission_proof=proof)
            ctl._transcript_status = transcript  # type: ignore[method-assign]
            try:
                return await asyncio.wait_for(ctl._settle_adoption(
                    "hostb", "expiry", brief, pane,
                    delivery_receipt=dict(receipt or LEGACY_RECEIPT),
                ), timeout=30.0)  # stuck detector only
            finally:
                proof.gate.set()
                assert pane.pasted == []
        finally:
            db.stop()

    return asyncio.run(run())


def _decisive(proof: CostedProof) -> dict:
    return proof.calls[-1]


def test_healthy_store_at_expiry_fails_closed_from_a_decisive_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SettleClock()
    proof = CostedProof(clock)
    result = _settle_at_expiry(monkeypatch, proof, SteppedTranscript(clock, SETTLE_STOPS), clock)
    decisive = _decisive(proof)
    assert [c for c in proof.calls if c["at"] >= SETTLE_EXPIRY_S] == [decisive], proof.calls
    assert decisive["timeout_s"] == SETTLE_STORE_FLOOR_S, proof.calls
    assert decisive["result"] == "pending", proof.calls
    assert result == ("failed", "agent_never_started",
                      "boot deadline expired with no provider transcript ever created")


def test_no_store_lookup_is_offered_less_than_the_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SettleClock()
    proof = CostedProof(clock)
    _settle_at_expiry(monkeypatch, proof, SteppedTranscript(clock, SETTLE_STOPS), clock)
    assert proof.calls
    assert all(c["timeout_s"] >= SETTLE_STORE_FLOOR_S for c in proof.calls), proof.calls


def test_final_transcript_observation_and_store_sample_each_get_their_allowance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = SettleClock()
    proof = CostedProof(clock)
    transcript = SteppedTranscript(clock, SETTLE_STOPS)
    _settle_at_expiry(monkeypatch, proof, transcript, clock)
    at_expiry = [c for c in transcript.calls if c["at"] >= SETTLE_EXPIRY_S]
    assert len(at_expiry) == 1, transcript.calls
    assert at_expiry[0]["allowance_s"] >= SETTLE_TRANSCRIPT_ALLOWANCE_S, transcript.calls
    decisive = _decisive(proof)
    assert decisive["at"] >= at_expiry[0]["at"], (proof.calls, transcript.calls)
    assert decisive["timeout_s"] >= SETTLE_STORE_FLOOR_S, proof.calls


def test_store_user_landing_in_the_tail_is_delivered(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SettleClock()
    # Visible from 19.99 s: after the last in-window read, before expiry.
    proof = CostedProof(clock, user_from_s=19.99)
    result = _settle_at_expiry(monkeypatch, proof, SteppedTranscript(clock, SETTLE_STOPS), clock)
    assert result[:2] == ("delivered", "event_store"), (result, proof.calls)


def test_transcript_landing_in_the_tail_is_seen_by_a_real_expiry_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = SettleClock()
    # An instant Store keeps answering pending, so only a real transcript
    # observation at expiry can see the brief.
    proof = CostedProof(clock, cost_s=0.0)
    transcript = SteppedTranscript(clock, SETTLE_STOPS, found_from_s=19.995)
    result = _settle_at_expiry(monkeypatch, proof, transcript, clock)
    assert any(c["at"] >= SETTLE_EXPIRY_S for c in transcript.calls), transcript.calls
    assert result[:2] == ("delivered", "transcript"), (result, transcript.calls, proof.calls)


def test_matching_user_on_the_decisive_sample_is_delivered(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SettleClock()
    proof = CostedProof(clock, user_from_s=SETTLE_EXPIRY_S)
    result = _settle_at_expiry(monkeypatch, proof, SteppedTranscript(clock, SETTLE_STOPS), clock)
    assert _decisive(proof)["result"] == "user", proof.calls
    assert result[:2] == ("delivered", "event_store")


def _expiry_call(proof: CostedProof, transcript: SteppedTranscript) -> dict:
    """The one Store call started at or after expiry, after the expiry observation."""
    observed = [c for c in transcript.calls if c["at"] >= SETTLE_EXPIRY_S]
    assert len(observed) == 1 and observed[0]["allowance_s"] == SETTLE_TRANSCRIPT_ALLOWANCE_S, transcript.calls
    late = [c for c in proof.calls if c["at"] >= SETTLE_EXPIRY_S]
    assert len(late) == 1 and late[0] is proof.calls[-1], proof.calls
    assert late[0]["at"] >= observed[0]["at"] and late[0]["timeout_s"] == SETTLE_STORE_FLOOR_S, proof.calls
    return late[0]


def test_decisive_sample_held_after_prior_pending_defers(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SettleClock()
    proof = CostedProof(clock)
    # Only a read started at or after expiry is held (until teardown).
    proof.hold_from_s = SETTLE_EXPIRY_S
    transcript = SteppedTranscript(clock, SETTLE_STOPS)
    result = _settle_at_expiry(monkeypatch, proof, transcript, clock)
    assert proof.calls[0]["result"] == "pending", proof.calls
    assert _expiry_call(proof, transcript)["result"] == "held-cancelled", proof.calls
    assert result == ("indeterminate", "live_pane_unproven",
                      "event_store_timeout: adoption reconciliation deferred")


def test_decisive_only_timeout_defers(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SettleClock()

    class ExpiryTimesOut(CostedProof):
        async def lookup(self, stream_id: str, **kwargs: object) -> EventProof:
            if self.clock.rel() >= SETTLE_EXPIRY_S:
                self.calls.append({"at": self.clock.rel(), "timeout_s": kwargs["timeout_s"], "result": "timeout"})
                raise asyncio.TimeoutError
            return await super().lookup(stream_id, **kwargs)  # type: ignore[arg-type]

    proof = ExpiryTimesOut(clock)
    transcript = SteppedTranscript(clock, SETTLE_STOPS)
    result = _settle_at_expiry(monkeypatch, proof, transcript, clock)
    assert proof.calls[0]["result"] == "pending", proof.calls
    assert _expiry_call(proof, transcript)["result"] == "timeout", proof.calls
    assert result == ("indeterminate", "live_pane_unproven",
                      "event_store_timeout: adoption reconciliation deferred")


def test_saved_staged_watermark_with_pending_store_stays_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SettleClock()
    proof = CostedProof(clock, cost_s=0.0)
    result = _settle_at_expiry(monkeypatch, proof, SteppedTranscript(clock, SETTLE_STOPS), clock,
                               receipt={**LEGACY_RECEIPT, "transport": "staged"})
    assert result == ("indeterminate", "live_pane_unproven",
                      "spawn_delivery_unknown: saved pre-paste watermark; no affirmative prompt delivery proof")


# -- One negative gate: no failure without a pending Store sample taken after
# the iteration's last evidence observation (transcript probe and pane check).

DELIVERED_EVENT = ("delivered", "event_store",
                   "adopted_after_restart: exact post-watermark USER event present in generation-fenced Store")


def test_user_landing_during_the_probe_wins_over_an_absent_transcript(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SettleClock()
    proof = CostedProof(clock, user_from_s=0.5)
    transcript = SteppedTranscript(clock, [1.0], absent_from_s=1.0)
    result = _settle_at_expiry(monkeypatch, proof, transcript, clock)
    assert proof.calls[0]["result"] == "pending", proof.calls
    assert result == DELIVERED_EVENT, (result, proof.calls)


def test_absent_transcript_below_the_floor_defers_to_an_unreachable_store(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SettleClock()
    # The 19.98 s iteration is below the floor, so it issues no lookup before
    # its probe; the probe then reports the transcript absent.
    proof = CostedProof(clock, unreachable_from_s=19.98)
    transcript = SteppedTranscript(clock, SETTLE_STOPS, absent_from_s=19.99)
    result = _settle_at_expiry(monkeypatch, proof, transcript, clock)
    assert result == ("indeterminate", "live_pane_unproven",
                      "event_store_unreachable: adoption reconciliation deferred"), (result, proof.calls)


def test_user_landing_before_the_pane_dies_is_delivered(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SettleClock()
    proof = CostedProof(clock, user_from_s=0.5)
    transcript = SteppedTranscript(clock, [1.0], pane_dies_at_s=1.0)
    result = _settle_at_expiry(monkeypatch, proof, transcript, clock)
    assert result == DELIVERED_EVENT, (result, proof.calls)


NEGATIVE_EXITS = {
    "transcript-absent": (dict(stops=[1.0], absent_from_s=1.0), "transcript_absent"),
    "pane-died": (dict(stops=[1.0], pane_dies_at_s=1.0), "pane_died_during_boot"),
    "expiry": (dict(stops=SETTLE_STOPS), "agent_never_started"),
}


@pytest.mark.parametrize("exit_name", sorted(NEGATIVE_EXITS))
def test_every_negative_exit_rests_on_a_pending_sample_after_the_last_observation(
    monkeypatch: pytest.MonkeyPatch, exit_name: str,
) -> None:
    kwargs, reason = NEGATIVE_EXITS[exit_name]
    clock = SettleClock()
    events: list = []
    proof = CostedProof(clock, events=events)
    stops = kwargs.pop("stops")
    transcript = SteppedTranscript(clock, stops, events=events, **kwargs)
    result = _settle_at_expiry(monkeypatch, proof, transcript, clock, events=events)
    assert result[:2] == ("failed", reason), (result, events)
    kinds = [kind for kind, _ in events]
    last_store = len(kinds) - 1 - kinds[::-1].index("store")
    last_observation = max(i for i, kind in enumerate(kinds) if kind in {"transcript", "pane"})
    assert last_store > last_observation, events
    sample = events[last_store][1]
    assert sample["result"] == "pending" and sample["timeout_s"] >= SETTLE_STORE_FLOOR_S, events


# -- Final-review oracles: one terminal gate for every exit, the expiry
# observation's allowance measured from its invocation, transcript-only parity
# with the pre-change loop, and tri-state pane liveness.

TRANSCRIPT_DELIVERED = ("delivered", "transcript")


class OfflineFromExpiry(CostedProof):
    """Answers pending until 19.98 s, then reports the Store unreachable."""

    async def lookup(self, stream_id: str, **kwargs: object) -> EventProof:
        if self.clock.rel() >= 19.98:
            self.calls.append({"at": self.clock.rel(), "timeout_s": kwargs["timeout_s"], "result": "unreachable"})
            return EventProof("unreachable", stream_id, 0, reason="event_store_unreachable")
        return await super().lookup(stream_id, **kwargs)  # type: ignore[arg-type]


class StatedTmux(AdoptTmux):
    """A pane whose tri-state liveness comes from ``state``; every read is logged."""

    def __init__(self, state, clock: SettleClock) -> None:
        super().__init__(alive=True, pane_pid=DEAD_PID, screen="model loading")
        self.state = state
        self.clock = clock
        self.reads: list[float] = []

    async def has_session(self, name: str) -> bool:
        return await self.session_state(name) == "alive"

    async def session_state(self, name: str) -> str:
        self.reads.append(self.clock.rel())
        return self.state(self) if callable(self.state) else self.state


@pytest.mark.parametrize("answer", ["found", "absent"])
def test_user_landing_during_an_in_window_probe_wins_over_its_answer(
    monkeypatch: pytest.MonkeyPatch, answer: str,
) -> None:
    clock = SettleClock()
    proof = CostedProof(clock, cost_s=0.0, user_from_s=0.5)

    async def transcript(*_a: object, **_kw: object) -> str:
        clock.now = clock.start + 1
        return answer

    result = _settle_at_expiry(monkeypatch, proof, transcript, clock)
    assert result[:2] == ("delivered", "event_store"), (result, proof.calls)


def test_store_proof_wins_over_a_transcript_found_at_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SettleClock()
    proof = CostedProof(clock, cost_s=0.0, user_from_s=19.995)
    transcript = SteppedTranscript(clock, SETTLE_STOPS, found_from_s=19.995)
    result = _settle_at_expiry(monkeypatch, proof, transcript, clock)
    assert result[:2] == ("delivered", "event_store"), (result, proof.calls, transcript.calls)


def test_absent_below_the_floor_after_prior_pending_defers(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SettleClock()
    proof = OfflineFromExpiry(clock, cost_s=0.0)

    async def transcript(*_a: object, **_kw: object) -> str:
        if clock.rel() < 19.98:
            clock.now = clock.start + 19.98
            return "no_transcript"
        clock.now = clock.start + SETTLE_EXPIRY_S
        return "absent"

    result = _settle_at_expiry(monkeypatch, proof, transcript, clock)
    assert result[:2] == ("indeterminate", "live_pane_unproven"), (result, proof.calls)


def test_expiry_observation_allowance_runs_from_its_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SettleClock()
    proof = CostedProof(clock, cost_s=0.0)
    # The in-window probe wakes late, 0.4 s past expiry.
    transcript = SteppedTranscript(clock, [20.4])
    _settle_at_expiry(monkeypatch, proof, transcript, clock)
    last = transcript.calls[-1]
    assert last["at"] == 20.4 and last["allowance_s"] == SETTLE_TRANSCRIPT_ALLOWANCE_S, transcript.calls


def test_late_expiry_observation_still_invokes_its_source(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SettleClock()
    proof = CostedProof(clock, cost_s=0.0)
    transcript = SteppedTranscript(clock, [SETTLE_EXPIRY_S], found_from_s=19.99)
    original = SpawnCtl._bounded_adoption_transcript_status

    async def delayed(self: SpawnCtl, *a: object, **kw: object) -> str:
        if clock.rel() >= SETTLE_EXPIRY_S:
            clock.now += 3.1  # a wake later than the whole allowance
        return await original(self, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(SpawnCtl, "_bounded_adoption_transcript_status", delayed)
    result = _settle_at_expiry(monkeypatch, proof, transcript, clock)
    assert [c["at"] for c in transcript.calls] == [0.0, 23.1], transcript.calls
    assert result[:2] == TRANSCRIPT_DELIVERED, (result, transcript.calls, proof.calls)


def test_liveness_wait_is_outside_the_expiry_allowances(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SettleClock()
    proof = CostedProof(clock, cost_s=0.0)

    def slow_at_expiry(pane: StatedTmux) -> str:
        if clock.rel() >= SETTLE_EXPIRY_S:
            clock.now += 4.0  # within tmux's ordinary ten-second allowance
        return "alive"

    pane = StatedTmux(slow_at_expiry, clock)
    transcript = SteppedTranscript(clock, [SETTLE_EXPIRY_S])
    result = _settle_at_expiry(monkeypatch, proof, transcript, clock, tmux=pane)
    observed = [c for c in transcript.calls if c["at"] >= SETTLE_EXPIRY_S]
    decisive = _decisive(proof)
    assert len(observed) == 1 and observed[0]["allowance_s"] == SETTLE_TRANSCRIPT_ALLOWANCE_S, transcript.calls
    # The decisive sample follows the slow liveness read with its own grant.
    assert decisive["at"] >= pane.reads[-1] + 4.0 and decisive["timeout_s"] == SETTLE_STORE_FLOOR_S, proof.calls
    assert result[:2] == ("failed", "agent_never_started")


def test_full_store_grant_after_an_exhausted_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SettleClock()
    proof = CostedProof(clock, cost_s=0.0)
    transcript = SteppedTranscript(clock, [SETTLE_EXPIRY_S], after_stops_step_s=SETTLE_TRANSCRIPT_ALLOWANCE_S)
    result = _settle_at_expiry(monkeypatch, proof, transcript, clock)
    assert proof.calls[-1]["at"] == SETTLE_EXPIRY_S + SETTLE_TRANSCRIPT_ALLOWANCE_S, proof.calls
    assert proof.calls[-1]["timeout_s"] == SETTLE_STORE_FLOOR_S, proof.calls
    assert result[:2] == ("failed", "agent_never_started")


def test_expiry_observation_that_times_out_counts_as_no_transcript(monkeypatch: pytest.MonkeyPatch) -> None:
    # The allowance is scaled down so its real timeout fires quickly; the
    # held probe is the expiry observation and nothing else.
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_EXPIRY_TRANSCRIPT_OBSERVATION_S", 0.05)
    clock = SettleClock()
    proof = CostedProof(clock, cost_s=0.0)
    stepped = SteppedTranscript(clock, [SETTLE_EXPIRY_S])
    held: list[str] = []

    async def transcript(*a: object, **kw: object) -> str:
        if clock.rel() < SETTLE_EXPIRY_S:
            return await stepped(*a, **kw)
        held.append("held")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            held.append("cancelled")
            raise
        raise AssertionError("held probe returned")

    result = _settle_at_expiry(monkeypatch, proof, transcript, clock)
    assert held == ["held", "cancelled"]
    assert _decisive(proof)["at"] >= SETTLE_EXPIRY_S and _decisive(proof)["timeout_s"] == SETTLE_STORE_FLOOR_S
    assert result[:2] == ("failed", "agent_never_started")


@pytest.mark.parametrize("receipt", [{"transport": "legacy"}, None], ids=["legacy-transport", "no-watermark"])
def test_transcript_only_liveness_crossing_matches_the_previous_loop(
    monkeypatch: pytest.MonkeyPatch, receipt: dict | None,
) -> None:
    # Transcript-only: one liveness read, then the deadline is read after it.
    clock = SettleClock()
    proof = CostedProof(clock, cost_s=0.0)
    transcript = SteppedTranscript(clock, [19.98])
    checks: list[float] = []

    def crossing(pane: StatedTmux) -> str:
        checks.append(clock.rel())
        clock.now = clock.start + 20.1
        return "alive" if len(checks) == 1 else "gone"

    pane = StatedTmux(crossing, clock)
    result = _settle_at_expiry(monkeypatch, proof, transcript, clock, receipt=receipt or {"transport": "direct"},
                               tmux=pane)
    assert checks == [19.98], checks
    assert proof.calls == []
    assert result == ("failed", "agent_never_started",
                      "boot deadline expired with no provider transcript ever created")


def test_original_short_window_fixture_runs_at_the_production_floor() -> None:
    # The 0.2 s fixture never scales the floor or the expiry allowance.
    class WindowOnly:
        def __init__(self) -> None:
            self.patched: list[str] = []

        def setattr(self, target: object, attr: str, value: object) -> None:
            self.patched.append(attr)
            setattr(target, attr, value)

    patch = WindowOnly()
    original = spawnctl_mod.ADOPTION_BOOT_DEADLINE_S
    try:
        test_transcript_absent_with_unreachable_store_never_repastes(patch)  # type: ignore[arg-type]
    finally:
        spawnctl_mod.ADOPTION_BOOT_DEADLINE_S = original
    assert patch.patched == ["ADOPTION_BOOT_DEADLINE_S"]


def test_recovery_paste_enters_the_saved_watermark_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    from submission_events import EventWatermark

    clock = SettleClock()
    monkeypatch.setattr(spawnctl_mod, "time", clock)
    proof = CostedProof(clock, cost_s=0.0)

    async def watermark(stream: str) -> EventWatermark:
        return EventWatermark(stream, 0, "reachable")

    proof.watermark = watermark  # type: ignore[attr-defined]

    async def run() -> None:
        db = Store(":memory:")
        db.start()
        try:
            pane = AdoptTmux(alive=True, pane_pid=DEAD_PID)
            ctl = SpawnCtl(db, Sessions(db, tmux=pane, local_host="hosta"), tmux=pane, submission_proof=proof)

            async def session(*_args: object) -> dict:
                return {"provider": "gemini"}

            monkeypatch.setattr(db, "fetch_session", session)

            async def ready(*_args: object, **_kwargs: object) -> object:
                return spawnctl_mod.BootReadyOutcome(True, 0, 1)

            monkeypatch.setattr(ctl, "_await_provider_ready", ready)
            ctl._transcript_status = SteppedTranscript(clock, [SETTLE_EXPIRY_S])  # type: ignore[method-assign]
            assert await db.reserve_stream_id("hosta", "restart", ttl_s=30, request_id="r", nonce="n")
            result = await ctl._settle_adoption(
                "hosta", "restart", "review synthetic brief", pane,
                delivery_receipt={"transport": "staged"},
                resume={"request_id": "r", "nonce": "n", "expires_at": clock.time() + 30},
            )
            saved = json.loads((await db.reservations(include_expired=True))[0]["payload"])["delivery_receipt"]
            assert len(pane.pasted) == 1 and saved["proof_watermark_state"] == "reachable", saved
            assert result[0] == "indeterminate" and result[2].startswith("spawn_delivery_unknown"), result
        finally:
            db.stop()

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["late_user_absent", "late_user_dead", "transcript_error",
                                  "store_error", "transcript_cancel", "store_cancel", "pane_cancel",
                                  "saved_outage_gone"])
def test_reconcile_journey_keeps_or_proves_the_handle(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    clock = SettleClock()
    monkeypatch.setattr(spawnctl_mod, "time", clock)

    async def run() -> None:
        db = Store(":memory:")
        db.start()
        try:
            host, name, request = "hosta", "synthetic-review", "r1"
            sid, brief = f"{host}:{name}", "QA synthetic user brief"
            pane = AdoptTmux(alive=True, pane_pid=DEAD_PID)
            proof = DurableUserEventProof(db, local_host=host)
            ctl = SpawnCtl(db, Sessions(db, tmux=pane, local_host=host), tmux=pane, submission_proof=proof)

            async def nonce(*_args: object) -> tuple[bool, str]:
                return True, "TEST"

            ctl._tmux_nonce = nonce  # type: ignore[method-assign]
            gate, held = asyncio.Event(), asyncio.Event()
            observed: list[str] = []
            original_state = pane.session_state

            async def session_state(name: str) -> str:
                if held.is_set():
                    observed.append("pane-after-cancel")
                if mode == "pane_cancel" and clock.rel() >= SETTLE_EXPIRY_S:
                    await hold()
                return await original_state(name)

            pane.session_state = session_state  # type: ignore[method-assign]

            async def hold() -> str:
                held.set()
                await gate.wait()
                raise AssertionError("hold returned unexpectedly")

            async def transcript(*_args: object, **_kwargs: object) -> str:
                if held.is_set():
                    observed.append("transcript-after-cancel")
                if mode == "saved_outage_gone":
                    pane.alive = False  # the pane dies after reconcile admitted it
                    return "no_transcript"
                if mode.startswith("late_user"):
                    lifecycle = await db.fetch_open_session_lifecycle(sid, pane_pid=DEAD_PID)
                    assert lifecycle is not None
                    assert await db.append_session_events_lifecycle_cas([{
                        "stream_id": sid, "event": {"stream_id": sid, "kind": "USER", "text": brief,
                                                    "timestamp": "2026-08-28T11:26:39.537Z"},
                        "identity": "review-user", "lifecycle": lifecycle}], limit=500)
                    clock.now = clock.start + 1
                    if mode == "late_user_dead":
                        pane.alive = False
                        return "no_transcript"
                    return "absent"
                if clock.rel() < SETTLE_EXPIRY_S:
                    clock.now = clock.start + SETTLE_EXPIRY_S
                    return "no_transcript"
                if mode == "transcript_error":
                    raise ValueError("synthetic observation failure")
                if mode == "transcript_cancel":
                    return await hold()
                return "no_transcript"

            ctl._transcript_status = transcript  # type: ignore[method-assign]
            original = proof.lookup

            async def lookup(*a: object, **kw: object) -> EventProof:
                if held.is_set():
                    observed.append("store-after-cancel")
                if mode == "saved_outage_gone":
                    raise asyncio.TimeoutError
                if clock.rel() >= SETTLE_EXPIRY_S:
                    if mode == "store_cancel":
                        await hold()
                    if mode == "store_error":
                        raise ValueError("synthetic read failure")
                return await original(*a, **kw)  # type: ignore[arg-type]

            proof.lookup = lookup  # type: ignore[method-assign]
            assert await db.reserve_stream_id(host, name, ttl_s=30, request_id=request, nonce="TEST")
            assert await db.record_spawn_intent(host, name, {
                "open_fields": {"objective": "Exercise interrupted spawn adoption", "provider": "claude"},
                "brief": brief, "delivery_receipt": (
                    {**LEGACY_RECEIPT, "transport": "staged"} if mode == "saved_outage_gone" else dict(LEGACY_RECEIPT))})
            await db.mark_tmux_created(host, name, pane_pid=DEAD_PID)
            task = asyncio.create_task(ctl.reconcile_spawn_intents())
            if mode.endswith("cancel"):
                await asyncio.wait_for(held.wait(), 10)  # stuck detector only
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 10)
                gate.set()
            else:
                await asyncio.wait_for(task, 10)  # stuck detector only
            row = await db.get_spawn_outcome(host, name) or {}
            reservations = await db.reservations(include_expired=True)
            events = await db.fetch_session_event_tail(sid, limit=10, after_daemon_seq=0)
            exact = any(e["kind"] == "USER" and e["text"] == brief for e in events)
            assert pane.pasted == []
            assert observed == [], observed  # nothing is observed after a cancellation
            if mode == "saved_outage_gone":
                # Difference 2: kept under the saved-watermark recovery classification.
                receipt = row.get("delivery_receipt") or {}
                assert row.get("state") == "indeterminate" and row["reason"].startswith("spawn_delivery_unknown"), row
                assert receipt.get("failure_code") == "spawn_delivery_unknown", receipt
                assert receipt.get("proof_state") == "pending", receipt
                assert len(reservations) == 1 and reservations[0]["request_id"] == request
            elif mode.startswith("late_user"):
                assert exact and not reservations
                assert (row.get("state"), row.get("delivery_evidence")) == ("delivered", "event_store"), row
            else:
                assert len(reservations) == 1 and reservations[0]["request_id"] == request
                assert row.get("state") != "failed", row
        finally:
            db.stop()

    asyncio.run(run())


def test_unreadable_remote_liveness_after_a_store_outage_defers(monkeypatch: pytest.MonkeyPatch) -> None:
    import tmux_transport

    clock = SettleClock()
    proof = OfflineFromExpiry(clock, cost_s=0.0)
    codes: list[int] = []

    class Peer(AdoptTmux):
        has_session = tmux_transport.Tmux.has_session

        async def run(self, *a: str, **_kw: object) -> tuple[int, str]:
            assert a[0] == "has-session"
            # Armed by the clock alone: SSH loses the connection at expiry.
            codes.append(255 if clock.rel() >= SETTLE_EXPIRY_S else 0)
            return codes[-1], ("synthetic connection lost" if codes[-1] else "")

        async def session_state(self, name: str) -> str:
            state = await tmux_transport.Tmux.session_state(self, name)
            states.append(state)
            return state

    states: list[str] = []
    pane = Peer(alive=True, pane_pid=DEAD_PID, screen="model loading")
    result = _settle_at_expiry(monkeypatch, proof, SteppedTranscript(clock, [SETTLE_EXPIRY_S]), clock, tmux=pane)
    # The crossing window pass's read is discarded; the expiry pass's read decides.
    assert codes == [255, 255] and states == ["unreachable", "unreachable"], (codes, states)
    assert result[:2] == ("indeterminate", "live_pane_unproven"), (result, proof.calls)


def test_unknown_liveness_with_a_pending_store_defers(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SettleClock()
    proof = CostedProof(clock, cost_s=0.0)

    def unknown_at_expiry(pane: StatedTmux) -> str:
        return "unreachable" if clock.rel() >= SETTLE_EXPIRY_S else "alive"

    pane = StatedTmux(unknown_at_expiry, clock)
    result = _settle_at_expiry(monkeypatch, proof, SteppedTranscript(clock, [SETTLE_EXPIRY_S]), clock, tmux=pane)
    assert _decisive(proof)["result"] == "pending" and _decisive(proof)["at"] >= pane.reads[-1], proof.calls
    assert result == ("indeterminate", "live_pane_unproven",
                      "pane_liveness_unknown: adoption reconciliation deferred")


# A source's own TimeoutError (or an OSError carrying ETIMEDOUT, which Python
# constructs as a TimeoutError) is T error, exactly like any other OSError.
TRANSCRIPT_FAULTS = {
    "os_error": lambda: OSError("synthetic pane PID read failure"),
    "source_timeout": lambda: TimeoutError("synthetic pane PID read timed out"),
    "etimedout": lambda: OSError(errno.ETIMEDOUT, "synthetic socket read timed out"),
}


@pytest.mark.parametrize("fault", list(TRANSCRIPT_FAULTS))
def test_positive_store_after_a_transcript_exception_is_delivered(monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    clock = SettleClock()
    proof = CostedProof(clock, cost_s=0.0, user_from_s=19.99)

    async def transcript(*_a: object, **_kw: object) -> str:
        if clock.rel() >= SETTLE_EXPIRY_S:
            raise TRANSCRIPT_FAULTS[fault]()
        clock.now = clock.start + SETTLE_EXPIRY_S
        return "no_transcript"

    result = _settle_at_expiry(monkeypatch, proof, transcript, clock)
    assert result[:2] == ("delivered", "event_store"), (result, proof.calls)


@pytest.mark.parametrize("fault", list(TRANSCRIPT_FAULTS))
def test_transcript_exception_without_store_proof_propagates(monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    clock = SettleClock()
    proof = CostedProof(clock, cost_s=0.0)
    error = TRANSCRIPT_FAULTS[fault]()

    async def transcript(*_a: object, **_kw: object) -> str:
        if clock.rel() >= SETTLE_EXPIRY_S:
            raise error
        clock.now = clock.start + SETTLE_EXPIRY_S
        return "no_transcript"

    with pytest.raises(OSError) as caught:
        _settle_at_expiry(monkeypatch, proof, transcript, clock)
    assert caught.value is error
    assert _decisive(proof)["at"] >= SETTLE_EXPIRY_S and _decisive(proof)["timeout_s"] == SETTLE_STORE_FLOOR_S


# -- Ports of the third-read probes: only the wrapper's own allowance is none.

@pytest.mark.parametrize("proven", [False, True])
def test_a_source_timeout_is_held_until_the_gate(monkeypatch: pytest.MonkeyPatch, proven: bool) -> None:
    clock = SettleClock()
    proof = CostedProof(clock, cost_s=0.0, user_from_s=SETTLE_EXPIRY_S if proven else None)
    error = TimeoutError("immediate transcript-source error")
    grants: list[float] = []

    async def transcript(*_a: object, absolute_deadline: float, **_kw: object) -> str:
        grants.append(absolute_deadline - clock.now)
        if clock.rel() == 0:
            clock.now += SETTLE_EXPIRY_S
            return "no_transcript"
        raise error

    if proven:
        result = _settle_at_expiry(monkeypatch, proof, transcript, clock)
        assert result[:2] == ("delivered", "event_store")
    else:
        with pytest.raises(TimeoutError) as caught:
            _settle_at_expiry(monkeypatch, proof, transcript, clock)
        assert caught.value is error
    assert grants == [SETTLE_EXPIRY_S, SETTLE_TRANSCRIPT_ALLOWANCE_S]
    assert proof.calls[-1]["timeout_s"] == SETTLE_STORE_FLOOR_S


@pytest.mark.parametrize("fault", ["source_timeout", "etimedout"])
def test_a_source_timeout_at_expiry_keeps_the_reservation(monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    clock = SettleClock()
    monkeypatch.setattr(spawnctl_mod, "time", clock)
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_BOOT_DEADLINE_S", SETTLE_EXPIRY_S)

    async def run() -> None:
        db = Store(":memory:")
        db.start()
        try:
            host, name, request = "hosta", "source-timeout", "r-source-timeout"
            pane = AdoptTmux(alive=True, pane_pid=DEAD_PID)
            proof = DurableUserEventProof(db, local_host=host)
            ctl = SpawnCtl(db, Sessions(db, tmux=pane, local_host=host), tmux=pane, submission_proof=proof)
            trace: list[tuple] = []
            real_source, real_pid, real_lookup = ctl._transcript_status, pane.pane_pid, proof.lookup
            in_probe = False

            async def nonce(*_args: object) -> tuple[bool, str]:
                return True, "TEST"

            async def source(*a: object, **kw: object) -> str:
                nonlocal in_probe
                in_probe = True
                try:
                    return await real_source(*a, **kw)
                finally:
                    in_probe = False

            async def pane_pid(*a: object, **kw: object) -> str | None:
                # The real source's first step. Reads by reconcile itself,
                # outside the probe, are untouched.
                if not in_probe:
                    return await real_pid(*a, **kw)
                trace.append(("transcript-pid", clock.rel()))
                if clock.rel() == 0:
                    clock.now += SETTLE_EXPIRY_S
                    return None
                raise TRANSCRIPT_FAULTS[fault]()

            async def lookup(*a: object, **kw: object) -> EventProof:
                result = await real_lookup(*a, **kw)
                trace.append(("store", clock.rel(), kw["timeout_s"], result.state))
                return result

            ctl._tmux_nonce = nonce  # type: ignore[method-assign]
            ctl._transcript_status = source  # type: ignore[method-assign]
            pane.pane_pid = pane_pid  # type: ignore[method-assign]
            proof.lookup = lookup  # type: ignore[method-assign]
            assert await db.reserve_stream_id(host, name, ttl_s=30, request_id=request, nonce="TEST")
            assert await db.record_spawn_intent(host, name, {
                "open_fields": {"objective": "Source timeout", "provider": "claude"},
                "brief": "synthetic brief", "delivery_receipt": dict(LEGACY_RECEIPT)})
            await db.mark_tmux_created(host, name, pane_pid=DEAD_PID)
            await asyncio.wait_for(ctl.reconcile_spawn_intents(), 30)  # stuck detector only
            row = await db.get_spawn_outcome(host, name) or {}
            kept = [r["request_id"] for r in await db.reservations(include_expired=True)]
            assert trace[-2:] == [("transcript-pid", SETTLE_EXPIRY_S),
                                  ("store", SETTLE_EXPIRY_S, SETTLE_STORE_FLOOR_S, "pending")], trace
            assert pane.pasted == []
            assert kept == [request] and row.get("state") != "failed", row
        finally:
            db.stop()

    asyncio.run(run())


@pytest.mark.parametrize("fault", ["source_timeout", "etimedout"])
def test_the_wrapper_re_raises_a_source_timeout(fault: str) -> None:
    async def run() -> None:
        ctl = SpawnCtl.__new__(SpawnCtl)
        error = TRANSCRIPT_FAULTS[fault]()

        async def source(*_a: object, **_kw: object) -> str:
            raise error

        ctl._transcript_status = source  # type: ignore[method-assign]
        with pytest.raises(TimeoutError) as caught:
            await ctl._bounded_adoption_transcript_status(
                "n", "needle", host=None, tmux=None, allowance_s=3.0)  # type: ignore[arg-type]
        assert caught.value is error

    asyncio.run(run())


def test_the_wrappers_own_elapsed_allowance_is_no_transcript() -> None:
    async def run() -> None:
        ctl = SpawnCtl.__new__(SpawnCtl)
        cancelled: list[bool] = []

        async def source(*_a: object, **_kw: object) -> str:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            return "found"

        ctl._transcript_status = source  # type: ignore[method-assign]
        assert await ctl._bounded_adoption_transcript_status(
            "n", "needle", host=None, tmux=None, allowance_s=0.05) == "no_transcript"  # type: ignore[arg-type]
        assert cancelled == [True]

    asyncio.run(run())

def test_short_window_fixture_with_an_injected_floor_reads_an_unreachable_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The companion of the unchanged 0.2 s fixture. A floor injected below the
    # window and a virtual clock fix the ordinary remainder at 0.2 s, so the
    # in-window read always happens, then the gate sample.
    clock = SettleClock()
    monkeypatch.setattr(spawnctl_mod, "time", clock)
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_BOOT_DEADLINE_S", 0.2)
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_MIN_EVENT_LOOKUP_S", 0.05)
    reads: list[float] = []

    class UnavailableProof:
        async def lookup(self, stream_id: str, *, expected_text: str,
                         watermark: object, timeout_s: float) -> EventProof:
            reads.append(timeout_s)
            return EventProof("unreachable", stream_id, 0, reason="event_store_unreachable")

    async def run() -> tuple[tuple[str, str, str], AdoptTmux]:
        db = Store(":memory:")
        db.start()
        try:
            tmux = AdoptTmux(alive=True, pane_pid=DEAD_PID, screen="$ ")
            ctl = SpawnCtl(db, Sessions(db, tmux=tmux, local_host="hosta"), tmux=tmux,
                           submission_proof=UnavailableProof())

            async def transcript_absent(*_args: object, **_kwargs: object) -> str:
                return "absent"

            ctl._transcript_status = transcript_absent  # type: ignore[method-assign]
            return await ctl._settle_adoption(
                "hostc", "absent-unavailable", "[example-service-notice:floor]\nnot safe to resend", tmux,
                delivery_receipt={"proof_watermark": 0, "proof_watermark_state": "reachable"},
            ), tmux
        finally:
            db.stop()

    result, tmux = asyncio.run(run())
    assert reads == pytest.approx([0.2, 0.05]), reads  # the in-window read, then the gate sample
    assert result == ("indeterminate", "live_pane_unproven",
                      "event_store_unreachable: adoption reconciliation deferred")
    assert tmux.pasted == []


TERMINAL_EXITS = {
    # name: (transcript kwargs, proof kwargs, expected (state, evidence))
    "absent-pending": (dict(stops=[1.0], absent_from_s=1.0), {}, ("failed", "transcript_absent")),
    "absent-unreachable": (dict(stops=[1.0], absent_from_s=1.0), dict(unreachable_from_s=0.5),
                           ("indeterminate", "live_pane_unproven")),
    "pane-died-unreachable": (dict(stops=[1.0], pane_dies_at_s=1.0), dict(unreachable_from_s=0.5),
                              ("indeterminate", "live_pane_unproven")),
    "found": (dict(stops=[1.0], found_from_s=0.0), {}, ("delivered", "transcript")),
    "expiry-unreachable": (dict(stops=SETTLE_STOPS), dict(unreachable_from_s=19.98),
                           ("indeterminate", "live_pane_unproven")),
}


@pytest.mark.parametrize("exit_name", sorted(TERMINAL_EXITS))
def test_no_observation_follows_the_terminal_gate_sample(monkeypatch: pytest.MonkeyPatch, exit_name: str) -> None:
    transcript_kwargs, proof_kwargs, expected = TERMINAL_EXITS[exit_name]
    clock = SettleClock()
    events: list = []
    proof = CostedProof(clock, events=events, **proof_kwargs)
    kwargs = dict(transcript_kwargs)
    stops = kwargs.pop("stops")
    transcript = SteppedTranscript(clock, stops, events=events, **kwargs)
    result = _settle_at_expiry(monkeypatch, proof, transcript, clock, events=events)
    assert result[:2] == expected, (result, events)
    kinds = [kind for kind, _ in events]
    assert kinds[-1] == "store", events
    assert events[-1][1]["timeout_s"] == SETTLE_STORE_FLOOR_S, events


# -- Amendment D: a straight-line pass decided once by a pure table.

PROOF_CAPABLE_ROWS = """
found alive proven no no D_EVENT
found gone proven no no D_EVENT
found unknown proven no no D_EVENT
absent alive proven no no D_EVENT
absent gone proven no no D_EVENT
absent unknown proven no no D_EVENT
none alive proven no no D_EVENT
none gone proven no no D_EVENT
none unknown proven no no D_EVENT
error alive proven no no D_EVENT
error gone proven no no D_EVENT
error unknown proven no no D_EVENT
found alive pending no no D_TRANSCRIPT
found gone pending no no D_TRANSCRIPT
found unknown pending no no D_TRANSCRIPT
absent alive pending no no F_ABSENT
absent gone pending no no F_ABSENT
absent unknown pending no no CONTINUE
none alive pending no no CONTINUE
none gone pending no no F_DIED_TRANSCRIPT
none unknown pending no no CONTINUE
error alive pending no no RAISE
error gone pending no no RAISE
error unknown pending no no RAISE
found alive unavailable no no D_TRANSCRIPT
found gone unavailable no no D_TRANSCRIPT
found unknown unavailable no no D_TRANSCRIPT
absent alive unavailable no no I_STORE
absent gone unavailable no no I_STORE
absent unknown unavailable no no I_STORE
none alive unavailable no no I_STORE
none gone unavailable no no I_STORE
none unknown unavailable no no I_STORE
error alive unavailable no no RAISE
error gone unavailable no no RAISE
error unknown unavailable no no RAISE
found alive proven yes no D_EVENT
found gone proven yes no D_EVENT
found unknown proven yes no D_EVENT
absent alive proven yes no D_EVENT
absent gone proven yes no D_EVENT
absent unknown proven yes no D_EVENT
none alive proven yes no D_EVENT
none gone proven yes no D_EVENT
none unknown proven yes no D_EVENT
error alive proven yes no D_EVENT
error gone proven yes no D_EVENT
error unknown proven yes no D_EVENT
found alive pending yes no D_TRANSCRIPT
found gone pending yes no D_TRANSCRIPT
found unknown pending yes no D_TRANSCRIPT
absent alive pending yes no I_SDU
absent gone pending yes no F_DIED_DELIVERY
absent unknown pending yes no I_SDU
none alive pending yes no CONTINUE
none gone pending yes no F_DIED_TRANSCRIPT
none unknown pending yes no CONTINUE
error alive pending yes no RAISE
error gone pending yes no RAISE
error unknown pending yes no RAISE
found alive unavailable yes no D_TRANSCRIPT
found gone unavailable yes no D_TRANSCRIPT
found unknown unavailable yes no D_TRANSCRIPT
absent alive unavailable yes no I_SDU
absent gone unavailable yes no I_SDU
absent unknown unavailable yes no I_SDU
none alive unavailable yes no I_SDU
none gone unavailable yes no I_SDU
none unknown unavailable yes no I_SDU
error alive unavailable yes no RAISE
error gone unavailable yes no RAISE
error unknown unavailable yes no RAISE
found alive proven no yes D_EVENT
found gone proven no yes D_EVENT
found unknown proven no yes D_EVENT
absent alive proven no yes D_EVENT
absent gone proven no yes D_EVENT
absent unknown proven no yes D_EVENT
none alive proven no yes D_EVENT
none gone proven no yes D_EVENT
none unknown proven no yes D_EVENT
error alive proven no yes D_EVENT
error gone proven no yes D_EVENT
error unknown proven no yes D_EVENT
found alive pending no yes D_TRANSCRIPT
found gone pending no yes D_TRANSCRIPT
found unknown pending no yes D_TRANSCRIPT
absent alive pending no yes F_ABSENT
absent gone pending no yes F_ABSENT
absent unknown pending no yes I_LIVENESS
none alive pending no yes F_NEVER
none gone pending no yes F_DIED_TRANSCRIPT
none unknown pending no yes I_LIVENESS
error alive pending no yes RAISE
error gone pending no yes RAISE
error unknown pending no yes RAISE
found alive unavailable no yes D_TRANSCRIPT
found gone unavailable no yes D_TRANSCRIPT
found unknown unavailable no yes D_TRANSCRIPT
absent alive unavailable no yes I_STORE
absent gone unavailable no yes I_STORE
absent unknown unavailable no yes I_STORE
none alive unavailable no yes I_STORE
none gone unavailable no yes I_STORE
none unknown unavailable no yes I_STORE
error alive unavailable no yes RAISE
error gone unavailable no yes RAISE
error unknown unavailable no yes RAISE
found alive proven yes yes D_EVENT
found gone proven yes yes D_EVENT
found unknown proven yes yes D_EVENT
absent alive proven yes yes D_EVENT
absent gone proven yes yes D_EVENT
absent unknown proven yes yes D_EVENT
none alive proven yes yes D_EVENT
none gone proven yes yes D_EVENT
none unknown proven yes yes D_EVENT
error alive proven yes yes D_EVENT
error gone proven yes yes D_EVENT
error unknown proven yes yes D_EVENT
found alive pending yes yes D_TRANSCRIPT
found gone pending yes yes D_TRANSCRIPT
found unknown pending yes yes D_TRANSCRIPT
absent alive pending yes yes I_SDU
absent gone pending yes yes F_DIED_DELIVERY
absent unknown pending yes yes I_SDU
none alive pending yes yes I_SDU
none gone pending yes yes F_DIED_DELIVERY
none unknown pending yes yes I_SDU
error alive pending yes yes RAISE
error gone pending yes yes RAISE
error unknown pending yes yes RAISE
found alive unavailable yes yes D_TRANSCRIPT
found gone unavailable yes yes D_TRANSCRIPT
found unknown unavailable yes yes D_TRANSCRIPT
absent alive unavailable yes yes I_SDU
absent gone unavailable yes yes I_SDU
absent unknown unavailable yes yes I_SDU
none alive unavailable yes yes I_SDU
none gone unavailable yes yes I_SDU
none unknown unavailable yes yes I_SDU
error alive unavailable yes yes RAISE
error gone unavailable yes yes RAISE
error unknown unavailable yes yes RAISE
"""
TRANSCRIPT_ONLY_ROWS = """
found alive no no no D_TRANSCRIPT
found gone no no no D_TRANSCRIPT
found unknown no no no D_TRANSCRIPT
absent alive no no no F_ABSENT
absent gone no no no F_ABSENT
absent unknown no no no F_ABSENT
none alive no no no CONTINUE
none gone no no no F_DIED_TRANSCRIPT
none unknown no no no CONTINUE
error alive no no no RAISE
error gone no no no RAISE
error unknown no no no RAISE
found alive no no yes D_TRANSCRIPT
found gone no no yes D_TRANSCRIPT
found unknown no no yes D_TRANSCRIPT
absent alive no no yes F_ABSENT
absent gone no no yes F_ABSENT
absent unknown no no yes F_ABSENT
none alive no no yes F_NEVER
none gone no no yes F_DIED_TRANSCRIPT
none unknown no no yes I_LIVENESS
error alive no no yes RAISE
error gone no no yes RAISE
error unknown no no yes RAISE
found alive no yes yes D_TRANSCRIPT
found gone no yes yes D_TRANSCRIPT
found unknown no yes yes D_TRANSCRIPT
absent alive no yes yes F_ABSENT
absent gone no yes yes F_ABSENT
absent unknown no yes yes F_ABSENT
none alive no yes yes F_NEVER
none gone no yes yes F_DIED_TRANSCRIPT
none unknown no yes yes I_LIVENESS
error alive no yes yes RAISE
error gone no yes yes RAISE
error unknown no yes yes RAISE
found alive yes no no D_TRANSCRIPT
found gone yes no no D_TRANSCRIPT
found unknown yes no no D_TRANSCRIPT
absent alive yes no no I_SDU
absent gone yes no no F_DIED_DELIVERY
absent unknown yes no no I_SDU
none alive yes no no CONTINUE
none gone yes no no F_DIED_TRANSCRIPT
none unknown yes no no CONTINUE
error alive yes no no RAISE
error gone yes no no RAISE
error unknown yes no no RAISE
found alive yes no yes D_TRANSCRIPT
found gone yes no yes D_TRANSCRIPT
found unknown yes no yes D_TRANSCRIPT
absent alive yes no yes I_SDU
absent gone yes no yes F_DIED_DELIVERY
absent unknown yes no yes I_SDU
none alive yes no yes I_SDU
none gone yes no yes F_DIED_TRANSCRIPT
none unknown yes no yes I_SDU
error alive yes no yes RAISE
error gone yes no yes RAISE
error unknown yes no yes RAISE
found alive yes yes yes D_TRANSCRIPT
found gone yes yes yes D_TRANSCRIPT
found unknown yes yes yes D_TRANSCRIPT
absent alive yes yes yes I_SDU
absent gone yes yes yes F_DIED_DELIVERY
absent unknown yes yes yes I_SDU
none alive yes yes yes I_SDU
none gone yes yes yes F_DIED_DELIVERY
none unknown yes yes yes I_SDU
error alive yes yes yes RAISE
error gone yes yes yes RAISE
error unknown yes yes yes RAISE
"""


def _table(rows: str) -> list[list[str]]:
    return [line.split() for line in rows.strip().splitlines()]


def test_every_proof_capable_row_of_the_decision_table() -> None:
    rows = _table(PROOF_CAPABLE_ROWS)
    seen = {tuple(r[:5]) for r in rows}
    assert len(rows) == len(seen) == 144
    for t, l, s, w, x, expected in rows:
        got = SpawnCtl._decide_adoption(t, l, s, w == "yes", x == "yes")
        assert got == expected, (t, l, s, w, x, got)
        # I1, I2, I6, I8 and the corrected I3, I4, I5, I7, I9 as properties.
        saved, expiry = w == "yes", x == "yes"
        if s == "proven":
            assert got == "D_EVENT"                                       # I1
        elif t == "found":
            assert got == "D_TRANSCRIPT"                                  # I2
        elif t == "error":
            assert got == "RAISE"                                         # I6
        if expiry:
            assert got != "CONTINUE"                                      # I8
        if got.startswith("F_"):                                          # I3
            assert s == "pending" and l in {"alive", "gone"} and t in {"absent", "none"}
        if s == "unavailable":                                            # I4
            assert got == {"found": "D_TRANSCRIPT", "error": "RAISE"}.get(t, "I_SDU" if saved else "I_STORE")
        if l == "unknown" and s == "pending" and t in {"absent", "none"}:  # I5
            if saved:
                assert got == ("I_SDU" if t == "absent" or expiry else "CONTINUE")
            else:
                assert got == ("I_LIVENESS" if expiry else "CONTINUE")
        if l == "unknown":
            assert not got.startswith("F_")
        if saved and s != "proven" and t in {"absent", "none"}:          # I7
            if s == "unavailable":
                assert got == "I_SDU"
            elif l == "gone":
                assert got == ("F_DIED_DELIVERY" if t == "absent" or expiry else "F_DIED_TRANSCRIPT")
            else:
                assert got == ("I_SDU" if t == "absent" or expiry else "CONTINUE")
        assert (got == "F_NEVER") == (                                    # I9
            not saved and expiry and t == "none" and l == "alive" and s == "pending")


def test_decision_codes_map_to_their_frozen_results() -> None:
    assert SpawnCtl._ADOPTION_RESULTS == {
        "D_EVENT": ("delivered", "event_store",
                    "adopted_after_restart: exact post-watermark USER event present in generation-fenced Store"),
        "D_TRANSCRIPT": ("delivered", "transcript", "adopted_after_restart: brief present in provider transcript"),
        "F_ABSENT": ("failed", "transcript_absent",
                     "adopted_after_restart: transcript located without brief; daemon does not re-paste"),
        "F_DIED_DELIVERY": ("failed", "pane_died_during_boot", "adopted pane died before delivery was proved"),
        "F_DIED_TRANSCRIPT": ("failed", "pane_died_during_boot", "adopted pane died before its transcript appeared"),
        "F_NEVER": ("failed", "agent_never_started", "boot deadline expired with no provider transcript ever created"),
        "I_SDU": ("indeterminate", "live_pane_unproven",
                  "spawn_delivery_unknown: saved pre-paste watermark; no affirmative prompt delivery proof"),
        "I_LIVENESS": ("indeterminate", "live_pane_unproven",
                       "pane_liveness_unknown: adoption reconciliation deferred"),
    }


def test_every_transcript_only_row_of_the_decision_table() -> None:
    rows = _table(TRANSCRIPT_ONLY_ROWS)
    assert len(rows) == len({tuple(r[:5]) for r in rows}) == 72
    for t, l, w, x_pre, x_post, expected in rows:
        got = SpawnCtl._decide_transcript_only_adoption(t, l, w == "yes", x_pre == "yes", x_post == "yes")
        assert got == expected, (t, l, w, x_pre, x_post, got)
        if l == "unknown" and w == "no" and t == "none":
            assert got == ("I_LIVENESS" if x_post == "yes" else "CONTINUE")
        assert (got == "F_NEVER") == (w == "no" and x_post == "yes" and t == "none" and l == "alive")
        if w == "no" and t == "absent":
            assert got == "F_ABSENT"  # located without the brief, whatever the pane (O6, O18, O30)


# "source_timeout" is a source raising TimeoutError itself: T error, like OSError.
SHAPE_TRANSCRIPTS = ["found", "absent", "none", "error", "source_timeout"]
SHAPE_PANES = ["alive", "gone", "unreachable", "verb_error", "os_error"]
SHAPE_STORES = ["proven", "pending", "unavailable"]


class ShapeRig:
    """Fixes one pass's observations (window or expiry) and records every
    observation in order; all other observations are neutral."""

    def __init__(self, clock: SettleClock, role: str, t: str, pane: str, store: str) -> None:
        self.clock, self.role, self.t, self.pane, self.store = clock, role, t, pane, store
        self.events: list[tuple[str, float]] = []
        self.transcripts = 0
        self.panes = 0
        self.stores = 0

    def _target_window(self) -> bool:
        return self.role == "window"

    async def transcript(self, *_a: object, **_kw: object) -> str:
        self.events.append(("transcript", self.clock.rel()))
        self.transcripts += 1
        if self.role == "window":
            if self.transcripts == 1:
                return self._answer()
            self.clock.now = self.clock.start + SETTLE_EXPIRY_S  # later passes run to expiry
            return "no_transcript"
        if self.clock.rel() < SETTLE_EXPIRY_S:
            self.clock.now = self.clock.start + SETTLE_EXPIRY_S  # the window pass crosses
            return "no_transcript"
        return self._answer()

    def _answer(self) -> str:
        if self.t == "error":
            raise OSError("synthetic transcript failure")
        if self.t == "source_timeout":
            raise TimeoutError("synthetic transcript source timeout")
        return {"none": "no_transcript"}.get(self.t, self.t)

    def target_pane(self) -> bool:
        self.panes += 1
        return self.panes == 1 if self.role == "window" else self.clock.rel() >= SETTLE_EXPIRY_S

    def target_store(self) -> bool:
        self.stores += 1
        return self.stores == 2 if self.role == "window" else self.clock.rel() >= SETTLE_EXPIRY_S


class ShapeTmux(AdoptTmux):
    def __init__(self, rig: ShapeRig) -> None:
        super().__init__(alive=True, pane_pid=DEAD_PID, screen="model loading")
        self.rig = rig

    async def session_state(self, name: str) -> str:
        rig = self.rig
        rig.events.append(("pane", rig.clock.rel()))
        if not rig.target_pane():
            return "alive"
        if rig.pane == "verb_error":
            raise VerbError("tmux_failed", "synthetic process error")
        if rig.pane == "os_error":
            raise OSError("synthetic pane failure")
        return rig.pane


class ShapeProof:
    def __init__(self, rig: ShapeRig) -> None:
        self.rig = rig
        self.gate = asyncio.Event()
        self.calls: list[dict] = []

    async def lookup(self, stream_id: str, *, expected_text: str, watermark: object,
                     timeout_s: float) -> EventProof:
        rig = self.rig
        rig.events.append(("store", rig.clock.rel()))
        self.calls.append({"at": rig.clock.rel(), "timeout_s": timeout_s})
        if rig.target_store():
            if rig.store == "proven":
                return EventProof("proven", stream_id, 0, event_id=11)
            if rig.store == "unavailable":
                return EventProof("unreachable", stream_id, 0, reason="event_store_unreachable")
        return EventProof("pending", stream_id, 0)


@pytest.mark.parametrize("store", SHAPE_STORES)
@pytest.mark.parametrize("pane", SHAPE_PANES)
@pytest.mark.parametrize("t", SHAPE_TRANSCRIPTS)
@pytest.mark.parametrize("role", ["window", "expiry"])
def test_each_pass_observes_in_order_then_decides_from_the_table(
    monkeypatch: pytest.MonkeyPatch, role: str, t: str, pane: str, store: str,
) -> None:
    clock = SettleClock()
    rig = ShapeRig(clock, role, t, pane, store)
    proof = ShapeProof(rig)
    tmux = ShapeTmux(rig)
    lane = pane if pane in {"alive", "gone"} else "unknown"
    expected = SpawnCtl._decide_adoption("error" if t == "source_timeout" else t, lane, store, False,
                                         role == "expiry")
    if expected == "RAISE":
        with pytest.raises(OSError):
            _settle_at_expiry(monkeypatch, proof, rig.transcript, clock, tmux=tmux)  # type: ignore[arg-type]
    else:
        result = _settle_at_expiry(monkeypatch, proof, rig.transcript, clock, tmux=tmux)  # type: ignore[arg-type]
    kinds = [kind for kind, _ in rig.events]
    if role == "window":
        first = kinds[:4]
        assert first == ["store", "transcript", "pane", "store"], rig.events
    else:
        # The crossing window pass takes no gate sample; the expiry pass
        # observes, reads the pane, then samples once.
        assert kinds == ["store", "transcript", "pane", "transcript", "pane", "store"], rig.events
    if expected == "CONTINUE":
        assert role == "window" and result[:2] == ("failed", "agent_never_started")
        return
    assert len(kinds) == (4 if role == "window" else 6), rig.events
    if expected != "RAISE":
        assert result[0] == SpawnCtl._ADOPTION_RESULTS.get(expected, ("indeterminate",))[0], result
        if expected == "I_STORE":
            assert result[2] == "event_store_unreachable: adoption reconciliation deferred"
        else:
            assert result == SpawnCtl._ADOPTION_RESULTS[expected]


@pytest.mark.parametrize("crossing", ["probe", "liveness"])
def test_a_window_pass_that_crosses_expiry_decides_nothing(monkeypatch: pytest.MonkeyPatch, crossing: str) -> None:
    clock = SettleClock()
    proof = CostedProof(clock, cost_s=0.0)
    transcript = SteppedTranscript(clock, [SETTLE_EXPIRY_S if crossing == "probe" else 19.0])
    reads: list[float] = []

    def liveness(pane: StatedTmux) -> str:
        reads.append(clock.rel())
        if crossing == "liveness" and len(reads) == 1:
            clock.now = clock.start + SETTLE_EXPIRY_S + 0.1
        return "alive"

    pane = StatedTmux(liveness, clock)
    result = _settle_at_expiry(monkeypatch, proof, transcript, clock, tmux=pane)
    # No lookup by the window pass after it saw the deadline; the only lookup
    # started at or after expiry is the expiry pass's gate sample.
    assert [c["at"] for c in proof.calls] == pytest.approx([0.0, transcript.calls[-1]["at"] + 0.05]), proof.calls
    assert transcript.calls[-1]["at"] >= SETTLE_EXPIRY_S and len(transcript.calls) == 2, transcript.calls
    assert proof.calls[-1]["timeout_s"] == SETTLE_STORE_FLOOR_S
    assert result[:2] == ("failed", "agent_never_started")


@pytest.mark.parametrize("carried", ["found", "error"])
def test_a_crossed_pass_keeps_its_transcript_evidence(monkeypatch: pytest.MonkeyPatch, carried: str) -> None:
    clock = SettleClock()
    proof = CostedProof(clock, cost_s=0.0)
    calls: list[float] = []

    async def transcript(*_a: object, **_kw: object) -> str:
        calls.append(clock.rel())
        if len(calls) == 1:
            if carried == "error":
                raise OSError("synthetic window probe failure")
            return "found"
        return "no_transcript"  # the fresh expiry observation sees nothing

    def liveness(pane: StatedTmux) -> str:
        if clock.rel() < SETTLE_EXPIRY_S:
            clock.now = clock.start + SETTLE_EXPIRY_S + 0.1  # the window pass crosses here
        return "alive"

    pane = StatedTmux(liveness, clock)
    if carried == "found":
        result = _settle_at_expiry(monkeypatch, proof, transcript, clock, tmux=pane)
        assert result[:2] == ("delivered", "transcript"), (result, proof.calls)
    else:
        with pytest.raises(OSError, match="window probe"):
            _settle_at_expiry(monkeypatch, proof, transcript, clock, tmux=pane)
    assert len(calls) == 2 and calls[1] >= SETTLE_EXPIRY_S, calls  # the expiry observation still ran
    assert [c["at"] for c in proof.calls if c["at"] >= SETTLE_EXPIRY_S] == [proof.calls[-1]["at"]]


def test_never_started_is_logged_only_after_the_gate_decides(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    clock = SettleClock()
    proof = CostedProof(clock, cost_s=0.0, user_from_s=19.99)
    result = _settle_at_expiry(monkeypatch, proof, SteppedTranscript(clock, [SETTLE_EXPIRY_S]), clock)
    assert result[:2] == ("delivered", "event_store")
    assert not any("agent_never_started" in r.getMessage() for r in caplog.records)


# -- Ports of the second-read probes.

@pytest.mark.parametrize("at_expiry", [False, True])
def test_observed_unknown_liveness_with_an_absent_transcript_keeps_the_handle(
    monkeypatch: pytest.MonkeyPatch, at_expiry: bool,
) -> None:
    clock = SettleClock()
    proof = CostedProof(clock, cost_s=0.0)
    # The window variant polls on (unknown never fails), so its second probe
    # steps the virtual clock to expiry, where the pass defers.
    transcript = SteppedTranscript(clock, [20] if at_expiry else [1, 20],
                                   absent_from_s=20.01 if at_expiry else 1)
    pane = StatedTmux(lambda _: "unreachable", clock)
    result = _settle_at_expiry(monkeypatch, proof, transcript, clock, tmux=pane)
    assert pane.reads and proof.calls[-1]["result"] == "pending"
    assert result[0] == "indeterminate", result


@pytest.mark.parametrize("state", ["pending", "proven"])
def test_a_liveness_verb_error_still_reaches_the_gate(monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    clock = SettleClock()
    proof = CostedProof(clock, cost_s=0.0, user_from_s=19.99 if state == "proven" else None)

    def liveness(_: StatedTmux) -> str:
        if clock.rel() > SETTLE_EXPIRY_S:
            raise VerbError("tmux_failed", "synthetic process error")
        return "alive"

    pane = StatedTmux(liveness, clock)
    result = _settle_at_expiry(monkeypatch, proof, SteppedTranscript(clock, [SETTLE_EXPIRY_S]), clock, tmux=pane)
    if state == "proven":
        assert result[:2] == ("delivered", "event_store")
    else:
        assert result[0] == "indeterminate" and result[2].startswith("pane_liveness_unknown"), result


class FirstLookupUnavailable(CostedProof):
    async def lookup(self, stream_id: str, **kwargs: object) -> EventProof:
        if not self.calls:
            self.calls.append({"at": self.clock.rel(), "timeout_s": kwargs["timeout_s"], "result": "unreachable"})
            return EventProof("unreachable", stream_id, 0, reason="event_store_unreachable")
        return await super().lookup(stream_id, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("gate_outage", [False, True])
def test_an_unavailable_early_answer_crossing_expiry_observes_once(
    monkeypatch: pytest.MonkeyPatch, gate_outage: bool,
) -> None:
    clock = SettleClock()
    proof = FirstLookupUnavailable(clock, cost_s=0.0, unreachable_from_s=20 if gate_outage else None)
    transcript = SteppedTranscript(clock, [20], found_from_s=19.99 if gate_outage else None)
    result = _settle_at_expiry(monkeypatch, proof, transcript, clock)
    assert len([c for c in transcript.calls if c["at"] >= 20]) == 1
    assert len([c for c in proof.calls if c["at"] >= 20]) == 1
    expected = ("delivered", "transcript") if gate_outage else ("failed", "agent_never_started")
    assert result[:2] == expected, (result, proof.calls)


def test_found_transcript_with_a_gate_outage_is_delivered(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SettleClock()
    proof = CostedProof(clock, cost_s=0.0, unreachable_from_s=0.5)
    transcript = SteppedTranscript(clock, [1], found_from_s=0)
    result = _settle_at_expiry(monkeypatch, proof, transcript, clock)
    assert result[:2] == ("delivered", "transcript")
    assert proof.calls[-1]["result"] == "unreachable"


def test_saved_watermark_with_unknown_liveness_on_pending_stays_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SettleClock()
    proof = CostedProof(clock, cost_s=0.0)
    pane = StatedTmux(lambda _: "unreachable", clock)
    result = _settle_at_expiry(monkeypatch, proof, SteppedTranscript(clock, [20]), clock, tmux=pane,
                               receipt={**LEGACY_RECEIPT, "transport": "staged"})
    assert result[0] == "indeterminate" and result[2].startswith("spawn_delivery_unknown"), result


def test_an_unobserved_crossing_still_terminates_at_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SettleClock()

    class Cross(CostedProof):
        async def lookup(self, *a: object, **kw: object) -> EventProof:
            result = await super().lookup(*a, **kw)  # type: ignore[arg-type]
            if len(self.calls) == 1:
                clock.now = clock.start + 20.25
            return result

    proof = Cross(clock, cost_s=0.0)
    transcript = SteppedTranscript(clock, [])
    result = _settle_at_expiry(monkeypatch, proof, transcript, clock)
    assert len(transcript.calls) == 1 and transcript.calls[0]["allowance_s"] == 3, transcript.calls
    assert result[:2] == ("failed", "agent_never_started")


def test_restart_with_unknown_liveness_and_an_absent_transcript_keeps_the_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tmux_transport

    # A short window so the unknown pane polls to expiry quickly; no timing is asserted.
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_BOOT_DEADLINE_S", 0.3)

    async def run() -> None:
        db = Store(":memory:")
        db.start()
        try:
            name, host, req = "qa-restart", "test-host", "req-qa"
            pane = AdoptTmux(alive=True, pane_pid=DEAD_PID)
            armed = False
            rc_log: list[int] = []

            async def transport(*args: str, **_kw: object) -> tuple[int, str]:
                assert args[0] == "has-session"
                rc_log.append(255 if armed else 0)
                return rc_log[-1], ""

            pane.run = transport  # type: ignore[method-assign]
            pane.session_state = tmux_transport.Tmux.session_state.__get__(pane)  # type: ignore[method-assign]
            ctl = SpawnCtl(db, Sessions(db, tmux=pane, local_host=host), tmux=pane)

            async def nonce(*_args: object) -> tuple[bool, str]:
                return True, "TEST"

            ctl._tmux_nonce = nonce  # type: ignore[method-assign]

            async def transcript(*_args: object, **_kwargs: object) -> str:
                nonlocal armed
                armed = True
                return "absent"

            ctl._transcript_status = transcript  # type: ignore[method-assign]
            assert await db.reserve_stream_id(host, name, ttl_s=30, request_id=req, nonce="TEST")
            assert await db.record_spawn_intent(host, name, {
                "open_fields": {"objective": "QA restart", "provider": "claude"},
                "brief": "synthetic brief", "delivery_receipt": dict(LEGACY_RECEIPT)})
            await db.mark_tmux_created(host, name, pane_pid=DEAD_PID)
            await ctl.reconcile_spawn_intents()
            row = await db.get_spawn_outcome(host, name)
            reservations = await db.reservations(include_expired=True)
            assert 255 in rc_log and not pane.pasted
            assert row["state"] == "indeterminate" and len(reservations) == 1, (row, rc_log)
        finally:
            db.stop()

    asyncio.run(run())


def test_found_at_expiry_still_reads_the_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SettleClock()
    events: list = []
    proof = CostedProof(clock, cost_s=0.0, events=events)
    transcript = SteppedTranscript(clock, [20], found_from_s=19.99, events=events)
    result = _settle_at_expiry(monkeypatch, proof, transcript, clock, events=events)
    assert result[:2] == ("delivered", "transcript")
    assert [name for name, _ in events][-3:] == ["transcript", "pane", "store"], events


def test_companion_fixture_is_independent_of_dispatch_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    clock = SettleClock()
    base = clock.now

    def monotonic() -> float:
        caller = sys._getframe(1)
        if caller.f_code.co_name == "_settle_adoption" and "deadline" in caller.f_locals:
            clock.now = base + 0.16
        return clock.now

    clock.monotonic = monotonic  # type: ignore[method-assign]
    monkeypatch.setattr(spawnctl_mod, "time", clock)
    test_short_window_fixture_with_an_injected_floor_reads_an_unreachable_store(monkeypatch)


def test_a_window_gate_sample_that_crosses_expiry_decides_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SettleClock()

    class CrossingGate(CostedProof):
        """The clock control: only the window gate entered at 19.99 advances
        it, to 20.10, and answers unavailable."""

        async def lookup(self, stream_id: str, **kwargs: object) -> EventProof:
            if self.clock.rel() == 19.99:
                self.calls.append({"at": 19.99, "timeout_s": kwargs["timeout_s"], "result": "unreachable"})
                self.clock.now = self.clock.start + 20.10
                return EventProof("unreachable", stream_id, 0, reason="event_store_unreachable")
            return await super().lookup(stream_id, **kwargs)  # type: ignore[arg-type]

    proof = CrossingGate(clock, cost_s=0.0)
    # The transcript and pane doubles never move the clock past 19.99; the
    # brief is visible from 19.995.
    transcript = SteppedTranscript(clock, [19.99], found_from_s=19.995, after_stops_step_s=0.0)
    result = _settle_at_expiry(monkeypatch, proof, transcript, clock)
    assert [c["at"] for c in transcript.calls] == [0.0, 20.10], transcript.calls
    assert [c["at"] for c in proof.calls if c["at"] >= SETTLE_EXPIRY_S] == [20.10], proof.calls
    assert result[:2] == ("delivered", "transcript"), (result, proof.calls)


@pytest.mark.parametrize("reason", ["event_store_unreachable", "event_store_timeout", ""])
def test_a_store_deferral_keeps_the_sample_reason_or_its_fallback(monkeypatch: pytest.MonkeyPatch, reason: str) -> None:
    clock = SettleClock()

    class Outage(CostedProof):
        async def lookup(self, stream_id: str, **kwargs: object) -> EventProof:
            self.calls.append({"at": self.clock.rel(), "timeout_s": kwargs["timeout_s"]})
            return EventProof("unreachable", stream_id, 0, reason=reason)

    proof = Outage(clock)
    result = _settle_at_expiry(monkeypatch, proof, SteppedTranscript(clock, [1.0], absent_from_s=1.0), clock)
    assert result == ("indeterminate", "live_pane_unproven",
                      f"{reason or 'event_store_unreachable'}: adoption reconciliation deferred")


@pytest.mark.parametrize("where", ["window", "expiry", "crossed", "both"])
def test_raise_re_raises_the_original_exception(monkeypatch: pytest.MonkeyPatch, where: str) -> None:
    clock = SettleClock()
    proof = CostedProof(clock, cost_s=0.0)
    raised: list[Exception] = []

    async def transcript(*_a: object, **_kw: object) -> str:
        crossing = clock.rel() < SETTLE_EXPIRY_S
        if crossing and where in {"expiry", "crossed", "both"}:
            clock.now = clock.start + SETTLE_EXPIRY_S  # this window pass crosses expiry
        if (where == "window" and crossing) or (where == "expiry" and not crossing) \
                or (where == "crossed" and crossing) or where == "both":
            raised.append(OSError(f"synthetic failure {len(raised)}"))
            raise raised[-1]
        return "no_transcript"

    with pytest.raises(OSError) as caught:
        _settle_at_expiry(monkeypatch, proof, transcript, clock)
    assert caught.value is raised[0], (caught.value, raised)  # the first if both raised


def test_an_early_proven_lookup_returns_before_any_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SettleClock()
    events: list = []
    proof = CostedProof(clock, cost_s=0.0, user_from_s=0.0, events=events)
    transcript = SteppedTranscript(clock, [1.0], events=events)
    result = _settle_at_expiry(monkeypatch, proof, transcript, clock, events=events)
    assert result == DELIVERED_EVENT
    assert [kind for kind, _ in events] == ["store"], events
    assert proof.calls[0]["timeout_s"] == pytest.approx(3.0)


def test_a_non_proven_early_answer_triggers_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SettleClock()
    events: list = []
    proof = FirstLookupUnavailable(clock, cost_s=0.0, events=events)
    transcript = SteppedTranscript(clock, SETTLE_STOPS, events=events)
    result = _settle_at_expiry(monkeypatch, proof, transcript, clock, events=events)
    # The unavailable early answer neither defers nor skips the pass's gate.
    assert proof.calls[0]["result"] == "unreachable" and proof.calls[1]["result"] == "pending", proof.calls
    assert result[:2] == ("failed", "agent_never_started")


def test_below_the_floor_the_early_read_is_withheld_but_the_gate_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SettleClock()
    proof = CostedProof(clock, cost_s=0.0)
    # Pass 1 ends at 19.85 s with its gate. Pass 2 starts there with 0.15 s
    # left, under the 0.25 s floor: no early read, then its gate at 19.90 s.
    transcript = SteppedTranscript(clock, [19.85, 19.90, SETTLE_EXPIRY_S])
    _settle_at_expiry(monkeypatch, proof, transcript, clock)
    window = [(round(c["at"], 6), c["timeout_s"]) for c in proof.calls if c["at"] < SETTLE_EXPIRY_S]
    assert window == [(0.0, pytest.approx(3.0)), (19.85, SETTLE_STORE_FLOOR_S), (19.9, SETTLE_STORE_FLOOR_S)], proof.calls


def test_the_short_window_without_an_injected_floor_withholds_the_ordinary_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # T5's withheld control: with the production 0.25 s floor and a 0.2 s
    # window under a product-local clock, there is no ordinary read, only one
    # full gate sample.
    clock = SettleClock()
    monkeypatch.setattr(spawnctl_mod, "time", clock)
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_BOOT_DEADLINE_S", 0.2)
    reads: list[float] = []

    class UnavailableProof:
        async def lookup(self, stream_id: str, *, expected_text: str,
                         watermark: object, timeout_s: float) -> EventProof:
            reads.append(timeout_s)
            return EventProof("unreachable", stream_id, 0, reason="event_store_unreachable")

    async def run() -> tuple[str, str, str]:
        db = Store(":memory:")
        db.start()
        try:
            tmux = AdoptTmux(alive=True, pane_pid=DEAD_PID, screen="$ ")
            ctl = SpawnCtl(db, Sessions(db, tmux=tmux, local_host="hosta"), tmux=tmux,
                           submission_proof=UnavailableProof())

            async def transcript_absent(*_args: object, **_kwargs: object) -> str:
                return "absent"

            ctl._transcript_status = transcript_absent  # type: ignore[method-assign]
            return await ctl._settle_adoption(
                "hostc", "absent-withheld", "[example-service-notice:withheld]\nnot safe to resend", tmux,
                delivery_receipt={"proof_watermark": 0, "proof_watermark_state": "reachable"},
            )
        finally:
            db.stop()

    result = asyncio.run(run())
    assert reads == [0.25], reads
    assert result == ("indeterminate", "live_pane_unproven",
                      "event_store_unreachable: adoption reconciliation deferred")


def test_a_transcript_only_unobserved_probe_counts_as_no_transcript(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SettleClock()
    proof = CostedProof(clock, cost_s=0.0)

    async def unobserved(self: SpawnCtl, *_a: object, **_kw: object) -> str:
        clock.now = clock.start + SETTLE_EXPIRY_S
        return "unobserved"

    monkeypatch.setattr(SpawnCtl, "_bounded_adoption_transcript_status", unobserved)
    result = _settle_at_expiry(monkeypatch, proof, SteppedTranscript(clock, []), clock,
                               receipt={"transport": "legacy"})
    assert result == ("failed", "agent_never_started",
                      "boot deadline expired with no provider transcript ever created")
    assert proof.calls == []


@pytest.mark.parametrize("role", ["window", "expiry"])
@pytest.mark.parametrize("wait", ["transcript", "pane", "gate"])
def test_cancellation_at_each_wait_propagates_and_observes_nothing_after(
    monkeypatch: pytest.MonkeyPatch, wait: str, role: str,
) -> None:
    clock = SettleClock()
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_BOOT_DEADLINE_S", SETTLE_EXPIRY_S)
    monkeypatch.setattr(spawnctl_mod, "time", clock)
    events: list[str] = []
    held = asyncio.Event()

    def target() -> bool:
        return (clock.rel() >= SETTLE_EXPIRY_S) == (role == "expiry")

    async def hold(kind: str) -> None:
        events.append(f"{kind}-held")
        held.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append(f"{kind}-cancelled")
            raise

    class Proof:
        async def lookup(self, stream_id: str, *, timeout_s: float, **_kw: object) -> EventProof:
            events.append("store")
            if wait == "gate" and timeout_s == SETTLE_STORE_FLOOR_S and target():
                await hold("gate")
            return EventProof("pending", stream_id, 0)

    class Pane(AdoptTmux):
        async def session_state(self, name: str) -> str:
            events.append("pane")
            if wait == "pane" and target():
                await hold("pane")
            return "alive"

    async def transcript(*_a: object, **_kw: object) -> str:
        events.append("transcript")
        if wait == "transcript" and target():
            await hold("transcript")
        if clock.rel() < SETTLE_EXPIRY_S and role == "expiry":
            clock.now = clock.start + SETTLE_EXPIRY_S
        return "no_transcript"

    async def run() -> None:
        db = Store(":memory:")
        db.start()
        try:
            pane = Pane(alive=True, pane_pid=DEAD_PID)
            ctl = SpawnCtl(db, Sessions(db, tmux=pane, local_host="hosta"), tmux=pane, submission_proof=Proof())
            ctl._transcript_status = transcript  # type: ignore[method-assign]
            task = asyncio.create_task(ctl._settle_adoption(
                "hostb", "cancel", "[example-service-notice:cancel]\nheld", pane,
                delivery_receipt=dict(LEGACY_RECEIPT)))
            await asyncio.wait_for(held.wait(), 10)  # stuck detector only
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10)
        finally:
            db.stop()

    asyncio.run(run())
    assert events[-2:] == [f"{wait}-held", f"{wait}-cancelled"], events


@pytest.mark.parametrize("ending", ["never_started", "delivered", "deferred", "raised", "continued_then_delivered"])
def test_the_never_started_warning_is_logged_once_and_only_for_that_result(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, ending: str,
) -> None:
    clock = SettleClock()
    proof = CostedProof(clock, cost_s=0.0,
                        user_from_s={"delivered": SETTLE_EXPIRY_S, "continued_then_delivered": 10.0}.get(ending),
                        unreachable_from_s=SETTLE_EXPIRY_S if ending == "deferred" else None)
    transcript = SteppedTranscript(clock, SETTLE_STOPS)

    async def raising(*a: object, **kw: object) -> str:
        if clock.rel() >= SETTLE_EXPIRY_S:
            raise OSError("synthetic expiry failure")
        return await transcript(*a, **kw)

    caplog.set_level("WARNING")
    if ending == "raised":
        with pytest.raises(OSError):
            _settle_at_expiry(monkeypatch, proof, raising, clock)
    else:
        _settle_at_expiry(monkeypatch, proof, transcript, clock)
    warnings = [r for r in caplog.records if "agent_never_started" in r.getMessage()]
    assert len(warnings) == (1 if ending == "never_started" else 0), warnings


def test_a_carried_error_is_outranked_by_a_proven_expiry_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SettleClock()
    proof = CostedProof(clock, cost_s=0.0, user_from_s=SETTLE_EXPIRY_S)
    calls: list[float] = []

    async def transcript(*_a: object, **_kw: object) -> str:
        calls.append(clock.rel())
        if len(calls) == 1:
            clock.now = clock.start + SETTLE_EXPIRY_S
            raise OSError("synthetic window probe failure")
        return "no_transcript"

    result = _settle_at_expiry(monkeypatch, proof, transcript, clock)
    assert result == DELIVERED_EVENT and len(calls) == 2


def test_a_gate_admission_read_equal_to_the_deadline_counts_as_expiry(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    clock = SettleClock()
    proof = CostedProof(clock, cost_s=0.0)

    def liveness(pane: StatedTmux) -> str:
        if len(pane.reads) == 1:
            clock.now = clock.start + SETTLE_EXPIRY_S  # exactly the deadline
        return "alive"

    pane = StatedTmux(liveness, clock)
    transcript = SteppedTranscript(clock, [19.5], after_stops_step_s=0.0)
    caplog.set_level("WARNING")
    result = _settle_at_expiry(monkeypatch, proof, transcript, clock, tmux=pane)
    # No window gate at 20.0; the expiry pass observes, reads the pane, samples once.
    assert [c["at"] for c in proof.calls] == [0.0, SETTLE_EXPIRY_S], proof.calls
    assert [c["at"] for c in transcript.calls] == [0.0, SETTLE_EXPIRY_S], transcript.calls
    assert result[:2] == ("failed", "agent_never_started")
    assert len([r for r in caplog.records if "agent_never_started" in r.getMessage()]) == 1
