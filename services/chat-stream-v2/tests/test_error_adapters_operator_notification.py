"""Warning/critical cards from any producer reach the FD through the registry."""
import json

import pytest

import _shared.notifications_store as notifications_store
from alerts import Alerts
from notify import Notify
from test_error_adapters import expected_episode
from test_error_alerts import subject  # noqa: F401  (shared fixture)

PRINCIPAL = "system:operator-notify"
FAMILY = "operator_notification.v1"


def frame(producer="red-probe", severity="critical", title="RED probe", dedup_key="red:1", **extra):
    return dict(type="notification.create", request_id="req-synthetic", producer=producer,
                severity=severity, title=title, body="probe body never copied",
                dedup_key=dedup_key, **extra)


async def create(s, **fields):
    reply = await s.notify.notification(frame(**fields))
    assert reply["type"] == "notification.create.ok"
    return reply["notification"]


async def facts(s):
    return [r for r in await s.notify._db.call("error_rows")
            if r["error_context"]["family"] == FAMILY]


@pytest.fixture
def wired(subject):  # noqa: F811
    alerts = Alerts(subject.store)
    alerts.sink = subject.service
    subject.notify.alerts = alerts
    return subject


@pytest.mark.asyncio
async def test_critical_card_records_linked_fact_and_blocker(wired):
    s = wired
    card = await create(s)
    (fact,) = await facts(s)
    ctx = fact["error_context"]
    assert ctx["principal"] == PRINCIPAL
    assert ctx["source_notification_id"] == card["notification_id"]
    assert (ctx["code"], ctx["stage"]) == ("critical", "red_probe")
    assert ctx["episode_id"] == expected_episode(
        "critical", {"producer": "red-probe", "key": "red:1"}, ("producer", "key"))
    assert fact["error_key"] == f"{FAMILY}:{PRINCIPAL}:{ctx['episode_id']}"
    stored = await s.notify._db.call("get_notification", card["notification_id"])
    assert stored.get("error_key") is None
    await s.service.reconcile()
    (notice,) = await s.service.notice_rows()
    assert notice["kind"] == "error_alert"
    assert notice["recipient_stream_id"] == "fixture:v2-test"
    assert json.loads(notice["metadata"])["notification_id"] == fact["notification_id"]
    assert notice["body"].startswith("BLOCKER critical from red-probe: RED probe "
                                     f"alert={fact['notification_id']} card={card['notification_id']} ")
    assert notice["body"].endswith("; the FD decides, no auto-repair.")
    assert "probe body never copied" not in notice["body"]
    await s.queue.drain_once()
    assert s.provider.pastes and s.provider.pastes[-1].endswith(notice["body"])


@pytest.mark.asyncio
async def test_title_is_bounded_and_stripped(wired):
    s = wired
    await create(s, title="line one\nline\ttwo\x07" + "x" * 300)
    await s.service.reconcile()
    (notice,) = await s.service.notice_rows()
    title = notice["body"].split(": ", 1)[1].split(" alert=", 1)[0]
    assert title == ("line oneline" + "two" + "x" * 300)[:200]


@pytest.mark.asyncio
async def test_missing_card_falls_back_to_generic_body(wired, monkeypatch):
    s = wired
    await create(s)
    monkeypatch.setattr(s.notify._db._store, "get_notification", lambda nid: None)
    await s.service.reconcile()
    (notice,) = await s.service.notice_rows()
    assert notice["body"].startswith(f"BLOCKER {FAMILY} critical alert=")


@pytest.mark.asyncio
async def test_info_records_no_fact(wired):
    s = wired
    await create(s, severity="info")
    assert await facts(s) == []
    await s.service.reconcile()
    assert await s.service.notice_rows() == []


@pytest.mark.asyncio
async def test_repeat_dedup_key_updates_fact_without_new_notice(wired):
    s = wired
    await create(s)
    await s.service.reconcile()
    await create(s)
    await s.service.reconcile()
    (fact,) = await facts(s)
    assert fact["firing_count"] == 2
    assert len(await s.service.notice_rows()) == 1


@pytest.mark.asyncio
async def test_one_producer_four_keys_one_immediate_three_held(wired):
    s = wired
    for i in range(4):
        await create(s, dedup_key=f"red:{i}")
        await s.service.reconcile()
    kinds = sorted(r["kind"] for r in await s.service.notice_rows())
    assert kinds == ["error_alert"] + ["front_desk_held"] * 3


@pytest.mark.asyncio
async def test_four_producers_three_immediates_one_held(wired):
    s = wired
    for producer in ("alpha", "bravo", "charlie", "delta"):
        await create(s, producer=producer)
        await s.service.reconcile()
    kinds = sorted(r["kind"] for r in await s.service.notice_rows())
    assert kinds == ["error_alert"] * 3 + ["front_desk_held"]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [True, False], ids=["legacy-row", "muted-by-new-code"])
async def test_muted_fact_stays_quiet_after_activation(wired, monkeypatch, legacy):
    s = wired
    monkeypatch.setenv("PENTACLE_ERROR_ALERTS_MODE", "off")
    await create(s)
    (fact,) = await facts(s)
    nid = fact["notification_id"]
    if legacy:
        # Pre-activation row as the installed daemon left it: muted, no revision.
        await s.notify._db.call("error_patch", nid, {"disposition": "muted"})
    else:
        await s.service.reconcile()
    ctx = (await s.notify._db.call("error_get", nid))["error_context"]
    assert ctx["disposition"] == "muted" and ctx["desired_notice_revision"] == 0
    assert ("muted_revision" in ctx) is not legacy
    monkeypatch.setenv("PENTACLE_ERROR_ALERTS_MODE", "on")
    await s.service.reconcile()
    await s.service.reconcile()
    assert await s.service.notice_rows() == []
    ctx = (await s.notify._db.call("error_get", nid))["error_context"]
    assert ctx["muted_revision"] == ctx["operation_revision"] == 1
    await create(s)  # a later emit for the same episode
    await s.service.reconcile()
    (notice,) = await s.service.notice_rows()
    assert notice["kind"] == "error_alert"
    assert notice["body"].startswith("BLOCKER critical from red-probe: RED probe")


class StubSink:
    def __init__(self):
        self.calls = []

    async def emit(self, fact, *, principal=None, source_notification_id=None):
        self.calls.append((fact, principal, source_notification_id))
        return "synthetic-fact-id"


@pytest.mark.asyncio
async def test_reply_bytes_identical_with_and_without_adapter(tmp_path, monkeypatch):
    monkeypatch.setattr(notifications_store, "_iso_now", lambda: "2026-01-15T00:00:00Z")
    monkeypatch.setattr(notifications_store.uuid, "uuid4", lambda: "00000000-0000-4000-8000-0000000000aa")
    replies = []
    for name, sink in (("off", None), ("on", StubSink()), ("raise", "raise")):
        alerts = None
        if sink is not None:
            alerts = Alerts()
            if sink == "raise":
                async def boom(*a, **k):
                    raise RuntimeError("synthetic")
                alerts.sink = StubSink()
                alerts.sink.emit = boom
            else:
                alerts.sink = sink
        notify = Notify(str(tmp_path / f"{name}.db"), alerts=alerts)
        await notify.start()
        try:
            replies.append(json.dumps(await notify.notification(frame()), sort_keys=True))
        finally:
            await notify.stop()
        if name == "on":
            ((fact, principal, source),) = sink.calls
            assert (fact.family, fact.code, principal, source) == (
                FAMILY, "critical", PRINCIPAL, "00000000-0000-4000-8000-0000000000aa")
    assert replies[0] == replies[1] == replies[2]


@pytest.mark.asyncio
async def test_pending_notice_rebinds_to_new_fd_generation(wired):
    from test_error_alerts_core import rebind_to
    s = wired
    await create(s)
    await s.service.reconcile()
    (old,) = await s.service.notice_rows()
    await rebind_to(s, "v2-next")
    await s.service.reconcile()
    rows = await s.service.notice_rows()
    (successor,) = [r for r in rows if r["recipient_stream_id"] == "fixture:v2-next"]
    assert json.loads(successor["metadata"])["predecessor_notice_id"] == old["notice_id"]
    assert successor["body"].startswith("BLOCKER critical from red-probe: RED probe")
    assert next(r for r in rows if r["notice_id"] == old["notice_id"])["terminal_reason"] == "superseded_binding"
