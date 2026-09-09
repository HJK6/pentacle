"""Remote presence is bounded, named, and observation-only."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from presence import PresenceConfig, PresenceObservation, RemotePresence  # noqa: E402
from sessions import Sessions  # noqa: E402
from tmux_transport import Tmux  # noqa: E402
from store import Store  # noqa: E402

LOCAL = "hosta"


class _FakeTmux:
    def __init__(self, result: tuple[int, str]) -> None:
        self.result = result
        self.calls = 0

    async def run(self, *args: str, **kwargs: object) -> tuple[int, str]:
        self.calls += 1
        rc, output = self.result
        if '-F' in args and '|' in args[args.index('-F') + 1]:
            output = output.replace('\t', '|')
        return rc, output


class _PreviewTmux(_FakeTmux):
    def __init__(self, result: tuple[int, str], preview: str) -> None:
        super().__init__(result)
        self.preview = preview
        self.capture_calls = 0

    async def capture(self, _name: str) -> str:
        self.capture_calls += 1
        return self.preview


class _CheckedPreviewTmux(_PreviewTmux):
    def __init__(self, result: tuple[int, str], preview: str) -> None:
        super().__init__(result, preview)
        self.checked_ok = True
        self.checked_calls = 0

    async def capture_checked(self, _name: str, **kwargs: object) -> tuple[bool, str]:
        self.checked_calls += 1
        return self.checked_ok, self.preview


class _RaisingCheckedPreviewTmux(_CheckedPreviewTmux):
    async def capture_checked(self, _name: str, **kwargs: object) -> tuple[bool, str]:
        self.checked_calls += 1
        raise RuntimeError("remote capture transport failed")


class _BlockingCheckedPreviewTmux(_CheckedPreviewTmux):
    def __init__(self, result: tuple[int, str], preview: str) -> None:
        super().__init__(result, preview)
        self.capture_started = asyncio.Event()
        self.release_capture = asyncio.Event()

    async def capture_checked(self, _name: str, **kwargs: object) -> tuple[bool, str]:
        self.checked_calls += 1
        self.capture_started.set()
        await self.release_capture.wait()
        return self.checked_ok, self.preview


class _FakeHosts:
    def __init__(self, local_host: str, tmux_by_host: dict[str, _FakeTmux],
                 online: dict[str, bool], reasons: dict[str, str] | None = None) -> None:
        self.local_host = local_host
        self.peers = {h: object() for h in tmux_by_host}
        self._tmux = tmux_by_host
        self._online = online
        self._reasons = reasons or {}

    def is_online(self, host: str) -> bool:
        if host == self.local_host:
            return True
        return self._online.get(host, False)

    def tmux_for(self, host: str):
        return self._tmux[host]

    def snapshot(self) -> dict[str, dict[str, object]]:
        return {
            host: {
                "online": online,
                "host_status": "online" if online else (
                    "degraded" if self._reasons.get(host, "").endswith("_after_online")
                    else "offline"
                ),
                "host_status_reason": "" if online else self._reasons.get(host, "host_offline"),
            }
            for host, online in self._online.items()
        }


def _sessions_with(rows: list[dict[str, object]]) -> Sessions:
    s = Sessions(Store(":memory:"), tmux=None, local_host=LOCAL)
    s._inv = {str(r["stream_id"]): dict(r) for r in rows}
    return s


def _row(host: str, name: str) -> dict[str, object]:
    return {"host": host, "session_name": name, "stream_id": f"{host}:{name}"}


async def _observe_and_apply(presence: RemotePresence) -> int:
    observations = await presence.observe_once()
    stamped = sum(
        presence.apply_observation(rows, observations[host])
        for host, rows in presence.last_rows.items()
    )
    await presence.capture_previews()
    return stamped


@pytest.mark.parametrize('host,ssh_formatting', [('hosta', False), ('hostb', True)])
def test_observation_preserves_identity_when_ssh_tmux_rewrites_tabs(host, ssh_formatting):
    class FormattingTmux:
        async def run(self, *args, **kwargs):
            rendered = args[-1].replace('#{session_name}', 'v2smoke-owned-rwork').replace('#{pane_pid}', '2473467')
            return 0, (rendered.replace('\t', '_') if ssh_formatting else rendered) + '\n'

    sessions = _sessions_with([_row(host, 'v2smoke-owned-rwork')])
    presence = RemotePresence(sessions, _FakeHosts(LOCAL, {host: FormattingTmux()}, online={host: True}))
    observed = asyncio.run(presence._observe_host(host, {'v2smoke-owned-rwork'}))
    assert observed.state == 'host_online_tmux_present'
    assert observed.alive == {'v2smoke-owned-rwork': '2473467'}


@pytest.mark.parametrize('line', ['v2-owned_123', 'v2-owned|123|456', 'v2-owned|',
                                 'v2-owned|bad', 'v2-owned|0', 'v2-owned|１２３'])
def test_ambiguous_or_malformed_presence_record_cannot_become_a_live_pane(line):
    sessions = _sessions_with([_row('hostb', 'v2-owned')])
    presence = RemotePresence(sessions, _FakeHosts(LOCAL, {'hostb': _FakeTmux((0, line + '\n'))},
                                                  online={'hostb': True}))
    observed = asyncio.run(presence._observe_host('hostb', {'v2-owned'}))
    assert observed.state == 'ambiguous'
    assert not observed.alive and not observed.raw_sessions


def test_live_remote_pane_is_marked_online() -> None:
    """The core fix: a reachable peer with the pane alive gets online=True /
    pane_alive / pane_pid — no longer rendering as off."""
    sessions = _sessions_with([_row("hostc", "v2-cccccccc")])
    tmux = _FakeTmux((0, "v2-cccccccc\t2473467\n"))
    hosts = _FakeHosts(LOCAL, {"hostc": tmux}, online={"hostc": True})
    presence = RemotePresence(sessions, hosts)

    stamped = asyncio.run(_observe_and_apply(presence))

    assert stamped == 1
    row = sessions.get("hostc:v2-cccccccc")
    assert row["online"] is True
    assert row["pane_status"] == "pane_alive"
    assert row["pane_pid"] == "2473467"


def test_select_rows_admits_local_without_expanding_the_bounded_known_hosts() -> None:
    sessions = _sessions_with([
        _row(LOCAL, "v2-local-a"),
        _row(LOCAL, "v2-local-b"),
        _row("hostc", "v2-peer"),
        _row("unknown", "v2-outside-fleet"),
    ])
    tmux = _FakeTmux((0, ""))
    hosts = _FakeHosts(LOCAL, {"hostc": tmux}, online={"hostc": True})
    presence = RemotePresence(sessions, hosts, config=PresenceConfig(max_rows_per_pass=2))

    first = presence.select_rows()
    second = presence.select_rows()

    assert len(first) == len(second) == 2
    assert all(row["host"] in {LOCAL, "hostc"} for row in first + second)
    assert {row["stream_id"] for row in first + second} == {
        f"{LOCAL}:v2-local-a", f"{LOCAL}:v2-local-b", "hostc:v2-peer",
    }


def test_live_remote_pane_gets_bounded_cached_preview() -> None:
    """A live remote row should expose the same cheap preview surface as local
    mirror rows, without turning presence into an unbounded capture sweep."""
    sessions = _sessions_with([_row("hostc", "v2-cccccccc")])
    tmux = _PreviewTmux((0, "v2-cccccccc\t2473467\n"), "remote drift check\n")
    hosts = _FakeHosts(LOCAL, {"hostc": tmux}, online={"hostc": True})
    presence = RemotePresence(
        sessions, hosts, config=PresenceConfig(max_rows_per_pass=1),
    )

    assert asyncio.run(_observe_and_apply(presence)) == 1
    row = sessions.get("hostc:v2-cccccccc")
    assert row["preview"] == "remote drift check"
    assert tmux.capture_calls == 1
    assert asyncio.run(_observe_and_apply(presence)) == 1
    assert sessions.get("hostc:v2-cccccccc")["preview"] == "remote drift check"
    assert tmux.capture_calls == 1  # TTL cache, not one capture per list pass


def test_live_remote_pane_promotes_working_state_and_frame() -> None:
    """The peer equivalent of the local mirror's working-state path.

    This is the live regression: L12 captures the pane and exposes preview,
    but the peer row must also consume the inherited live parser/tracker and
    publish the v1-shaped working.state frame.
    """
    sessions = _sessions_with([{
        **_row("hostc", "v2-working"),
        "provider": "codex",
        "session_generation": "generation-1",
    }])
    tmux = _CheckedPreviewTmux(
        (0, "v2-working\t2473467\n"),
        "ready\n❯\nWorking (3s · thinking)\n",
    )
    hosts = _FakeHosts(LOCAL, {"hostc": tmux}, online={"hostc": True})
    frames: list[dict[str, object]] = []

    async def broadcast(frame: dict[str, object]) -> None:
        frames.append(frame)

    presence = RemotePresence(sessions, hosts, broadcast=broadcast)

    assert asyncio.run(_observe_and_apply(presence)) == 1

    row = sessions.get("hostc:v2-working")
    assert row["preview"] == "Working (3s · thinking)"
    assert row["working"] is True
    assert row["working_label"] == "Working 3s · thinking"
    working = next(frame for frame in frames if frame["type"] == "working.state")
    assert working["stream_id"] == "hostc:v2-working"
    for key in ("timestamp", "tokens_input", "tokens_output", "tokens_phase",
                "tasks", "task_summary", "elapsed_ms"):
        assert key in working
    assert working["tokens_phase"] == "down"
    inventory = next(frame for frame in frames if frame["type"] == "session.inventory")
    summary = next(item for item in inventory["sessions"] if item["stream_id"] == "hostc:v2-working")
    assert summary["working"] is True
    assert summary["working_label"] == "Working 3s · thinking"
    assert summary["preview"] == "Working (3s · thinking)"


def test_blank_capture_classifies_wedged_not_idle() -> None:
    sessions = _sessions_with([_row("hostc", "v2-wedged")])
    tmux = _CheckedPreviewTmux((0, "v2-wedged\t2473467\n"), "ready\n")
    hosts = _FakeHosts(LOCAL, {"hostc": tmux}, online={"hostc": True})
    presence = RemotePresence(
        sessions,
        hosts,
        config=PresenceConfig(preview_cache_ttl_s=0),
    )
    asyncio.run(_observe_and_apply(presence))
    tmux.preview = "   \n"
    asyncio.run(_observe_and_apply(presence))
    row = sessions.get("hostc:v2-wedged")
    assert row["capture_liveness"] == "wedged_unknown"
    assert row["working"] is False
    assert tmux.checked_calls == 3  # initial content, then two bounded blank probes


def test_transport_error_classifies_unknown() -> None:
    sessions = _sessions_with([_row("hostc", "v2-transport")])
    tmux = _RaisingCheckedPreviewTmux((0, "v2-transport\t2473467\n"), "ignored")
    hosts = _FakeHosts(LOCAL, {"hostc": tmux}, online={"hostc": True})
    presence = RemotePresence(sessions, hosts)
    asyncio.run(_observe_and_apply(presence))
    assert sessions.get("hostc:v2-transport")["capture_liveness"] == "transport_unknown"
    assert tmux.checked_calls == 1


def test_idle_capture_permits_reap() -> None:
    sessions = _sessions_with([_row("hostc", "v2-idle")])
    tmux = _CheckedPreviewTmux((0, "v2-idle\t2473467\n"), "ready\n")
    hosts = _FakeHosts(LOCAL, {"hostc": tmux}, online={"hostc": True})
    presence = RemotePresence(sessions, hosts)
    asyncio.run(_observe_and_apply(presence))
    assert sessions.get("hostc:v2-idle")["capture_liveness"] == "idle"


def test_remote_working_state_clears_after_grace_since_last_spinner() -> None:
    # The turn-end clear is time-based, not pass-count based: a spinner that
    # vanishes is held for the grace window (so a single missed capture frame
    # mid-turn cannot flap the indicator off), then clears once the grace since
    # the last observed spinner elapses. This replaces the earlier count-based
    # two-tick hold, which — at the 60 s reconcile cadence — could strand a
    # finished turn's working=true for minutes (the idle half of the working-
    # state-never-flips defect).
    sessions = _sessions_with([{
        **_row("hostc", "v2-working-idle"), "provider": "codex",
    }])
    tmux = _CheckedPreviewTmux(
        (0, "v2-working-idle\t2473467\n"),
        "ready\n❯\nWorking (3s · thinking)\n",
    )
    hosts = _FakeHosts(LOCAL, {"hostc": tmux}, online={"hostc": True})
    frames: list[dict[str, object]] = []

    async def broadcast(frame: dict[str, object]) -> None:
        frames.append(frame)

    presence = RemotePresence(
        sessions, hosts,
        config=PresenceConfig(preview_cache_ttl_s=0, working_clear_grace_ms=2000),
        broadcast=broadcast,
    )
    clock = {"ms": 1_000_000}
    presence._now_ms = lambda: clock["ms"]

    asyncio.run(_observe_and_apply(presence))
    assert sessions.get("hostc:v2-working-idle")["working"] is True

    tmux.preview = "ready\nidle\n"
    # Within the grace window the spinner is still held.
    clock["ms"] += 1000
    asyncio.run(_observe_and_apply(presence))
    assert sessions.get("hostc:v2-working-idle")["working"] is True

    # Once the grace since the last spinner elapses it clears.
    clock["ms"] += 2000
    asyncio.run(_observe_and_apply(presence))
    row = sessions.get("hostc:v2-working-idle")
    assert row["working"] is False
    assert row["working_label"] == ""
    idle = [
        frame for frame in frames
        if frame["type"] == "working.state"
        and frame["stream_id"] == "hostc:v2-working-idle"
        and frame["tokens_phase"] == "idle"
    ]
    assert idle, "remote turn did not emit the inherited idle working.state edge"


def test_stale_breaker_callback_cannot_overwrite_newer_recovery() -> None:
    sessions = _sessions_with([{
        **_row("hostc", "codex-pushed"),
        "pane_status": "pane_alive",
        "online": False,
        "host_status": "degraded",
    }])
    hosts = _FakeHosts(
        LOCAL,
        {"hostc": _FakeTmux((0, "codex-pushed\t2473467\n"))},
        online={"hostc": True},
    )
    presence = RemotePresence(sessions, hosts)

    asyncio.run(presence.host_status_changed({
        "host": "hostc",
        "online": False,
        "host_status": "degraded",
        "host_status_reason": "circuit_open_after_online",
    }))

    row = sessions.get("hostc:codex-pushed")
    assert row["online"] is True
    assert row["host_status"] == "online"
    assert row["host_status_reason"] == ""


def test_stale_tmux_success_cannot_overwrite_newer_open_breaker() -> None:
    row = {
        **_row("hostc", "codex-pushed"),
        "pane_status": "pane_alive",
        "online": True,
    }
    sessions = _sessions_with([row])
    hosts = _FakeHosts(
        LOCAL,
        {"hostc": _FakeTmux((0, "codex-pushed\t2473467\n"))},
        online={"hostc": False},
        reasons={"hostc": "circuit_open_after_online"},
    )
    presence = RemotePresence(sessions, hosts)

    stamped = presence.apply_observation([row], PresenceObservation(
        host="hostc",
        state="host_online_tmux_present",
        known=("codex-pushed",),
        alive={"codex-pushed": "2473467"},
    ))

    assert stamped == 1
    current = sessions.get("hostc:codex-pushed")
    assert current["online"] is False
    assert current["host_status"] == "degraded"
    assert current["host_status_reason"] == "circuit_open_after_online"


def test_preview_cache_is_generation_aware_and_bounded() -> None:
    sessions = _sessions_with([{
        **_row("hostc", "v2-reuse"), "session_generation": "generation-1",
    }])
    tmux = _PreviewTmux((0, "v2-reuse\t2473467\n"), "old preview")
    hosts = _FakeHosts(LOCAL, {"hostc": tmux}, online={"hostc": True})
    presence = RemotePresence(
        sessions,
        hosts,
        config=PresenceConfig(
            preview_cache_ttl_s=300, preview_cache_max_entries=1,
        ),
    )

    asyncio.run(_observe_and_apply(presence))
    sessions._inv["hostc:v2-reuse"]["session_generation"] = "generation-2"
    tmux.preview = "new preview"
    asyncio.run(_observe_and_apply(presence))

    row = sessions.get("hostc:v2-reuse")
    assert row["preview"] == "new preview"
    assert tmux.capture_calls == 2
    assert len(presence._preview_cache) <= 1


def test_failed_remote_preview_preserves_last_good_value() -> None:
    sessions = _sessions_with([_row("hostc", "v2-preview-failure")])
    tmux = _CheckedPreviewTmux((0, "v2-preview-failure\t2473467\n"), "last good")
    hosts = _FakeHosts(LOCAL, {"hostc": tmux}, online={"hostc": True})
    presence = RemotePresence(
        sessions, hosts, config=PresenceConfig(preview_cache_ttl_s=0),
    )

    asyncio.run(_observe_and_apply(presence))
    tmux.checked_ok = False
    tmux.preview = ""
    asyncio.run(_observe_and_apply(presence))

    assert sessions.get("hostc:v2-preview-failure")["preview"] == "last good"
    assert tmux.checked_calls == 2


def test_inflight_preview_cannot_cross_reused_generation() -> None:
    sessions = _sessions_with([{
        **_row("hostc", "v2-preview-race"),
        "session_generation": "generation-1",
        "preview": "old preview",
    }])
    tmux = _BlockingCheckedPreviewTmux(
        (0, "v2-preview-race\t2473467\n"), "new preview",
    )
    hosts = _FakeHosts(LOCAL, {"hostc": tmux}, online={"hostc": True})
    presence = RemotePresence(sessions, hosts, config=PresenceConfig(preview_cache_ttl_s=300))

    async def run() -> None:
        sweep = asyncio.create_task(_observe_and_apply(presence))
        await tmux.capture_started.wait()
        sessions._inv["hostc:v2-preview-race"]["session_generation"] = "generation-2"
        tmux.release_capture.set()
        await sweep

    asyncio.run(run())

    row = sessions.get("hostc:v2-preview-race")
    assert row["session_generation"] == "generation-2"
    assert row["preview"] == "old preview"
    assert all(key[1] != "generation-1" for key in presence._preview_cache)


def test_confirmed_remote_pane_death_emits_once_without_closing_row() -> None:
    sessions = _sessions_with([{
        **_row("hostc", "v2-remote-death"),
        "session_generation": "generation-1",
        "pane_status": "pane_alive",
        "online": True,
        "pane_pid": "100",
    }])
    tmux = _FakeTmux((0, "v2-remote-death\t100\n"))
    hosts = _FakeHosts(LOCAL, {"hostc": tmux}, online={"hostc": True})
    frames: list[dict[str, object]] = []

    async def broadcast(frame: dict[str, object]) -> None:
        frames.append(frame)

    presence = RemotePresence(sessions, hosts, broadcast=broadcast)
    asyncio.run(_observe_and_apply(presence))

    tmux.result = (0, "v2-other-live\t200\n")
    asyncio.run(_observe_and_apply(presence))
    asyncio.run(_observe_and_apply(presence))

    died = [frame for frame in frames if frame["type"] == "session.died"]
    assert len(died) == 1
    assert died[0]["stream_id"] == "hostc:v2-remote-death"
    assert died[0]["reason"] == "pane_pid_gone"
    assert sessions.get("hostc:v2-remote-death") is not None
    assert sessions.get("hostc:v2-remote-death")["pane_status"] == "pane_dead"


def test_tmux_run_cancellation_kills_and_reaps_child(tmp_path: Path) -> None:
    pid_path = tmp_path / "capture.pid"

    async def run() -> None:
        script = (
            "import os,time; "
            f"open({str(pid_path)!r}, 'w').write(str(os.getpid())); "
            "time.sleep(30)"
        )
        tmux = Tmux(bin_path=sys.executable)
        task = asyncio.create_task(tmux.run("-c", script, timeout=30))
        deadline = time.monotonic() + 2
        while not pid_path.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert pid_path.exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        pid = int(pid_path.read_text())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            await asyncio.sleep(0.01)
        pytest.fail(f"cancelled tmux subprocess {pid} survived")

    asyncio.run(run())


def test_offline_host_is_skipped_no_false_death() -> None:
    """A host breaker is the row's named liveness source, not a silent split-brain."""
    sessions = _sessions_with([_row("hostb", "v2-deadbeef")])
    tmux = _FakeTmux((0, "v2-deadbeef\t99\n"))
    hosts = _FakeHosts(LOCAL, {"hostb": tmux}, online={"hostb": False})
    presence = RemotePresence(sessions, hosts)

    stamped = asyncio.run(_observe_and_apply(presence))

    assert stamped == 1
    assert tmux.calls == 0  # no SSH to an offline host
    row = sessions.get("hostb:v2-deadbeef")
    assert row["online"] is False
    assert row["host_status"] == "offline"
    assert row["host_status_reason"] == "host_offline"


def test_transport_failure_stamps_nothing() -> None:
    """An ssh/tmux failure (rc != 0) is inconclusive — no stamping, no
    false-death — even on a host `is_online` currently believes reachable."""
    sessions = _sessions_with([_row("hostc", "v2-cccccccc")])
    tmux = _FakeTmux((255, ""))  # ssh no-route
    hosts = _FakeHosts(LOCAL, {"hostc": tmux}, online={"hostc": True})
    presence = RemotePresence(sessions, hosts)

    stamped = asyncio.run(_observe_and_apply(presence))

    assert stamped == 0
    assert "online" not in sessions.get("hostc:v2-cccccccc")


def test_rc_one_without_tmux_absence_signature_is_ambiguous() -> None:
    """A relayed permission/transport error must not become reboot evidence."""
    sessions = _sessions_with([_row("hostc", "v2-cccccccc")])
    tmux = _FakeTmux((1, "permission denied opening tmux socket\n"))
    hosts = _FakeHosts(LOCAL, {"hostc": tmux}, online={"hostc": True})
    presence = RemotePresence(sessions, hosts)

    assert asyncio.run(_observe_and_apply(presence)) == 0
    assert "online" not in sessions.get("hostc:v2-cccccccc")


class _RaisingTmux:
    """A tmux whose bulk read raises — a `tmux_timeout` VerbError or any exec
    failure. The presence pass must treat this as inconclusive, never a death."""

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, *args: str, **kwargs: object) -> tuple[int, str]:
        self.calls += 1
        raise RuntimeError("ssh hung / tmux timed out")


def test_raised_read_error_stamps_nothing() -> None:
    """A raised read (timeout/exec failure), not just a non-zero rc, is caught
    and treated as inconclusive — no stamping, no false-death — on a host
    `is_online` currently believes reachable."""
    sessions = _sessions_with([_row("hostc", "v2-cccccccc")])
    tmux = _RaisingTmux()
    hosts = _FakeHosts(LOCAL, {"hostc": tmux}, online={"hostc": True})
    presence = RemotePresence(sessions, hosts)

    stamped = asyncio.run(_observe_and_apply(presence))

    assert stamped == 0
    assert tmux.calls == 1  # it did try, then swallowed the raise
    assert "online" not in sessions.get("hostc:v2-cccccccc")


def test_absent_pane_in_nonempty_set_is_observed_dead_not_closed() -> None:
    """When tmux enumerated real sessions and this pane is absent, it is stamped
    online=False/pane_dead — but the row stays in the inventory (observation
    only: never a close or a kill)."""
    sessions = _sessions_with([
        _row("hostc", "v2-alive"),
        _row("hostc", "v2-gone"),
    ])
    tmux = _FakeTmux((0, "v2-alive\t100\n"))  # v2-gone absent
    hosts = _FakeHosts(LOCAL, {"hostc": tmux}, online={"hostc": True})
    presence = RemotePresence(sessions, hosts)

    stamped = asyncio.run(_observe_and_apply(presence))

    assert stamped == 2
    alive = sessions.get("hostc:v2-alive")
    gone = sessions.get("hostc:v2-gone")
    assert alive["online"] is True and alive["pane_status"] == "pane_alive"
    assert gone["online"] is False and gone["pane_status"] == "pane_dead"
    # Observation only: the dead row is NOT removed from the inventory.
    assert sessions.get("hostc:v2-gone") is not None


def test_empty_alive_set_is_inconclusive_no_false_death() -> None:
    """rc==0 but zero panes (e.g. a transient server-up/zero-panes race) must not
    false-death a whole host of rows — nothing is stamped."""
    sessions = _sessions_with([_row("hostc", "v2-cccccccc")])
    tmux = _FakeTmux((0, "\n"))
    hosts = _FakeHosts(LOCAL, {"hostc": tmux}, online={"hostc": True})
    presence = RemotePresence(sessions, hosts)

    stamped = asyncio.run(_observe_and_apply(presence))

    assert stamped == 0
    assert "online" not in sessions.get("hostc:v2-cccccccc")


def test_non_agent_peer_panes_never_contribute_to_presence() -> None:
    """A peer's `usage-check-*` probe and detached `sleep`/`python3` gate runners
    must never be observed as sessions (lane 19). Only the agent pane is kept;
    the non-agent panes are filtered before the alive set is built."""
    sessions = _sessions_with([_row("hostc", "v2-realchat")])
    listing = "usage-check-1754433564\t10\nsleep\t11\npython3\t12\nv2-realchat\t2473467\n"
    tmux = _FakeTmux((0, listing))
    hosts = _FakeHosts(LOCAL, {"hostc": tmux}, online={"hostc": True})
    presence = RemotePresence(sessions, hosts)

    stamped = asyncio.run(_observe_and_apply(presence))

    assert stamped == 1
    assert sessions.get("hostc:v2-realchat")["online"] is True


def test_custom_named_agent_row_is_preserved_by_known_set() -> None:
    """A caller-supplied spawn name carries no agent prefix, so the filter keeps
    it only because it is registry-known — it must not be dropped and then
    false-deathed."""
    sessions = _sessions_with([_row("hostc", "my-custom-lane")])
    tmux = _FakeTmux((0, "my-custom-lane\t555\nusage-check-9\t9\n"))
    hosts = _FakeHosts(LOCAL, {"hostc": tmux}, online={"hostc": True})
    presence = RemotePresence(sessions, hosts)

    stamped = asyncio.run(_observe_and_apply(presence))

    assert stamped == 1
    assert sessions.get("hostc:my-custom-lane")["online"] is True


def test_only_non_agent_panes_is_inconclusive_no_false_death() -> None:
    """A peer whose tmux has ONLY non-agent panes yields an empty filtered alive
    set — treated as inconclusive, so the agent row is not false-deathed."""
    sessions = _sessions_with([_row("hostc", "v2-realchat")])
    tmux = _FakeTmux((0, "usage-check-1\t1\nsleep\t2\n"))  # no agent panes
    hosts = _FakeHosts(LOCAL, {"hostc": tmux}, online={"hostc": True})
    presence = RemotePresence(sessions, hosts)

    stamped = asyncio.run(_observe_and_apply(presence))

    assert stamped == 0
    assert "online" not in sessions.get("hostc:v2-realchat")


def test_local_direct_capture_sets_and_clears_working() -> None:
    sessions = _sessions_with([{
        **_row(LOCAL, "v2-working-local"), "provider": "codex",
    }])
    tmux = _CheckedPreviewTmux(
        (0, "v2-working-local\t2473467\n"),
        "ready\n❯\nWorking (3s · thinking)\n",
    )
    hosts = _FakeHosts(LOCAL, {LOCAL: tmux}, online={LOCAL: True})
    presence = RemotePresence(
        sessions, hosts, config=PresenceConfig(preview_cache_ttl_s=0),
    )
    clock = {"ms": 1_000_000}
    presence._now_ms = lambda: clock["ms"]

    asyncio.run(_observe_and_apply(presence))
    row = sessions.get(f"{LOCAL}:v2-working-local")
    assert row["working"] is True

    tmux.preview = "ready\nidle\n"
    # Advance past the clear grace so the finished turn clears on the next read.
    clock["ms"] += presence.cfg.working_clear_grace_ms + 1000
    for _ in range(3):
        asyncio.run(_observe_and_apply(presence))

    row = sessions.get(f"{LOCAL}:v2-working-local")
    assert row["working"] is False
    assert row["working_label"] == ""


def test_admitted_event_surfaces_inflight_turn_before_a_reconcile_pass() -> None:
    """AC1 (working-state-never-flips defect): an in-flight tool turn must go
    working within one fast-refresh tick of its TOOL_USE event, and idle within
    one tick + grace of the reply — WITHOUT waiting for the ~60 s reconcile
    capture sweep that was the sole working owner.

    The single owner is unchanged (``refresh_active_working`` drives the same
    ``_capture_pane`` -> ``_apply_capture_state`` path the sweep uses); a genuine
    admitted event (``genuine_activity_at``, advanced by BOTH local and remote
    ingest for claude and codex) only marks the stream hot so that owner runs
    promptly. On origin/main there is no fast refresh, so the turn stays
    working=false until a reconcile pass — this test fails there, passes here.
    """
    gen = "generation-1"
    sessions = _sessions_with([{
        **_row(LOCAL, "v2-turn"), "provider": "codex", "session_generation": gen,
        # A genuine event just landed (works identically for a LOCAL hosta seat —
        # the seam r1 flagged the original _active_since path missed).
        "genuine_activity_at": time.time(),
        "genuine_activity_generation": gen,
    }])
    tmux = _CheckedPreviewTmux(
        (0, "v2-turn\t2473467\n"),
        "ready\n❯\nWorking (3s · thinking)\n",
    )
    hosts = _FakeHosts(LOCAL, {LOCAL: tmux}, online={LOCAL: True})
    frames: list[dict[str, object]] = []

    async def broadcast(frame: dict[str, object]) -> None:
        frames.append(frame)

    presence = RemotePresence(
        sessions, hosts,
        config=PresenceConfig(
            preview_cache_ttl_s=0, working_refresh_interval_s=2.0,
            working_clear_grace_ms=2000,
        ),
        broadcast=broadcast,
    )
    clock = {"ms": 1_000_000}
    presence._now_ms = lambda: clock["ms"]

    # No reconcile capture sweep has run: without the fast refresh the turn is
    # invisible.
    assert sessions.get(f"{LOCAL}:v2-turn").get("working") in (None, False)

    # One fast-refresh tick surfaces the turn.
    applied = asyncio.run(presence.refresh_active_working())
    assert applied >= 1
    row = sessions.get(f"{LOCAL}:v2-turn")
    assert row["working"] is True
    assert row["working_label"] == "Working 3s · thinking"
    assert any(
        f["type"] == "working.state" and f["stream_id"] == f"{LOCAL}:v2-turn"
        and f["tokens_phase"] == "down"
        for f in frames
    ), "fast refresh did not emit the working.state edge"

    # The reply lands: the pane goes idle. A still-working stream stays hot, so
    # the fast refresh keeps capturing it and clears once the grace elapses.
    tmux.preview = "ready\nidle\n"
    clock["ms"] += 1000
    asyncio.run(presence.refresh_active_working())
    assert sessions.get(f"{LOCAL}:v2-turn")["working"] is True  # held within grace

    clock["ms"] += 2000
    asyncio.run(presence.refresh_active_working())
    row = sessions.get(f"{LOCAL}:v2-turn")
    assert row["working"] is False
    assert row["working_label"] == ""


def test_terminal_idle_fits_locked_two_second_smoke_window() -> None:
    """A late spinner capture must clear before smoke convergence expires.

    The smoke's assistant marker can arrive while the pane still renders its
    last spinner. Model that marker at t=0, the final working capture at t=.9 s,
    and the convergence deadline at t=2 s. The old 1.5 s grace kept `working`
    true here (a self-host-only timing red); the daemon contract must clear it.
    """
    generation = "generation-smoke"
    stream_id = f"{LOCAL}:v2-smoke-idle"
    sessions = _sessions_with([{
        **_row(LOCAL, "v2-smoke-idle"), "provider": "codex",
        "session_generation": generation,
        "genuine_activity_at": time.time(),
        "genuine_activity_generation": generation,
    }])
    tmux = _CheckedPreviewTmux(
        (0, "v2-smoke-idle\t2473467\n"),
        "ready\n❯\nWorking (3s · thinking)\n",
    )
    hosts = _FakeHosts(LOCAL, {LOCAL: tmux}, online={LOCAL: True})
    presence = RemotePresence(sessions, hosts, config=PresenceConfig(preview_cache_ttl_s=0))
    clock = {"ms": 900}
    presence._now_ms = lambda: clock["ms"]

    asyncio.run(presence.refresh_active_working())
    assert sessions.get(stream_id)["working"] is True

    tmux.preview = "ready\nidle\n"
    clock["ms"] = 2_000
    asyncio.run(presence.refresh_active_working())
    assert sessions.get(stream_id)["working"] is False


def test_stale_capture_does_not_resurrect_working_on_dead_pane() -> None:
    # A same-generation pane death can land between a capture's SSH read and its
    # apply. The generation check alone would still apply the stale live pane; the
    # liveness fence must drop it so working is never resurrected on a dead pane.
    gen = "g1"
    sid = f"{LOCAL}:v2-dead"
    sessions = _sessions_with([{
        **_row(LOCAL, "v2-dead"), "provider": "codex", "session_generation": gen,
        "pane_status": "pane_dead", "online": False,
    }])
    hosts = _FakeHosts(LOCAL, {LOCAL: _CheckedPreviewTmux((0, "v2-dead\t1\n"), "")},
                       online={LOCAL: True})
    presence = RemotePresence(sessions, hosts, config=PresenceConfig(preview_cache_ttl_s=0))
    key = (sid, gen)
    stale = (
        key, "ready\n❯\nWorking (3s · thinking)\n", "Working (3s · thinking)",
        {"working": True, "working_label": "Working 3s · thinking"}, "idle",
    )
    applied = asyncio.run(presence._apply_capture_result(stale, {sid: key}))
    assert applied == 0
    assert not sessions.get(sid).get("working")


def test_working_refresh_defaults_meet_ac1_bounds() -> None:
    # AC1 (working-state-never-flips): working within ≤3 s of the tool event,
    # idle within ≤3 s of the reply. Pin those to the SHIPPED defaults so a later
    # tuning cannot silently regress the bound.
    cfg = PresenceConfig()
    # Idle worst case = one full grace since the last spinner + one tick to
    # observe and apply the clear.
    assert cfg.working_clear_grace_ms + cfg.working_refresh_interval_s * 1000 <= 3000
    # Working worst case ≈ one tick + a capture; keep the tick well under 3 s.
    assert 0 < cfg.working_refresh_interval_s <= 2.0
    # grace spans more than one tick so a single missed spinner frame mid-turn is
    # bridged rather than flapped off.
    assert cfg.working_clear_grace_ms > cfg.working_refresh_interval_s * 1000
