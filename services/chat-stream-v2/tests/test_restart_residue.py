"""Regression coverage for the bounded daemon-v2 restart-residue batch."""

from __future__ import annotations

import asyncio
from pathlib import Path

from daemon_lifecycle import DaemonLifecycle  # noqa: E402
from ledger import Ledger  # noqa: E402
from _shared.notifications_store import NotificationStore, _iso_now  # noqa: E402
from notify import Notify  # noqa: E402
from presence import PresenceConfig, RemotePresence  # noqa: E402
from reconciler import ReconcileConfig, SessionReconciler  # noqa: E402
from sessions import Sessions  # noqa: E402
from store import Store  # noqa: E402


class _DeadTmux:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, *_args: str, **_kwargs: object) -> tuple[int, str]:
        self.calls += 1
        return 1, "no server running on socket"

    async def session_state(self, _name: str) -> str:
        return "gone"


class _LiveOnReprobeTmux(_DeadTmux):
    async def session_state(self, _name: str) -> str:
        return "alive"


class _Hosts:
    local_host = "hosta"

    def __init__(self, tmux: _DeadTmux) -> None:
        self.peers = {"hostb": object()}
        self.tmux = tmux

    def is_online(self, host: str) -> bool:
        return host == "hosta" or host == "hostb"

    def tmux_for(self, _host: str) -> _DeadTmux:
        return self.tmux


class _NotifyCapture:
    def __init__(self) -> None:
        self.records: list[dict] = []

    async def notification(self, message: dict) -> dict:
        self.records.append(message)
        return {"type": "notification.create.ok"}


def test_notification_startup_recovers_stale_claim_but_not_active_claim(tmp_path: Path) -> None:
    async def go() -> None:
        path = tmp_path / "notifications.db"
        seeded = NotificationStore(str(path))
        recent_now = _iso_now()
        old = seeded.create_notification(
            producer="test", title="old", dedup_key="old", now="2020-01-01T00:00:00Z"
        )
        recent = seeded.create_notification(
            producer="test", title="recent", dedup_key="recent", now=recent_now
        )
        intent = {"effective_spawn_spec": {"provider": "codex", "host": "hosta"}}
        seeded.claim_external_resolution(
            old["notification_id"], canonical_intent=intent, by="worker", action_kind="spawn_worker",
            now="2020-01-01T00:00:00Z",
        )
        seeded.claim_external_resolution(
            recent["notification_id"], canonical_intent=intent, by="worker", action_kind="spawn_worker",
            now=recent_now,
        )
        seeded.close()

        notify = Notify(str(path), recovery_stale_after_s=60)
        await notify.start()
        try:
            old_after = await notify._db.call("get_notification", old["notification_id"])
            recent_after = await notify._db.call("get_notification", recent["notification_id"])
            assert old_after["state"] == "indeterminate"
            assert old_after["resolution"]["indeterminate_reason"] == "stale_external_resolution_claim"
            assert recent_after["state"] == "running"
        finally:
            await notify.stop()

    asyncio.run(go())


def test_daemon_lifecycle_keeps_process_identity_local(tmp_path: Path) -> None:
    async def go() -> None:
        path = str(tmp_path / "sessions.db")
        store = Store(path)
        store.start()
        first = DaemonLifecycle(store, host="hostb", pid=101, instance_id="first")
        await first.start()
        await first.stop(reason="signal")
        try:
            snapshot = await first.snapshot()
            assert snapshot["instance_id"] == "first"
            assert [event["event_type"] for event in snapshot["events"]] == [
                "daemon_stop", "daemon_start"
            ]
        finally:
            store.stop()

    asyncio.run(go())


def test_terminal_report_honors_self_close_on_completion() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host="hosta")
            await store.open_session(
                "hosta", "auto-close", parent_stream_id="hosta:leader",
                visibility="hidden", self_close_on_completion=True,
            )
            report = await Ledger(store, sessions=sessions).report(
                {
                    "from_stream_id": "hosta:auto-close",
                    "status": "done",
                    "summary": "finished",
                    "findings": [],
                    "next_action": "done",
                }
            )
            assert report["closed"] is True
            row = await store.fetch_session("hosta", "auto-close")
            assert row is not None and row["status"] == "closed"
        finally:
            store.stop()

    asyncio.run(go())
