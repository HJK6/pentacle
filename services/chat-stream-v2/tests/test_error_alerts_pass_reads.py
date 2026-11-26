"""One notice read per reconcile pass (R-024), with no duplicate or missed alert.

The pass reads every fact's notices once. A fact acts on its own fresh rows,
so a change made by another writer between that read and the fact's turn
must leave the outcome what a per-fact fresh read gives. Each mid-pass
scenario runs in both modes; ``fresh`` disables the pass read and is the
prior behavior, and both must match it after the pass and after delivery.
"""

import time

import pytest

import error_adapters
import error_alerts
from error_adapters import ErrorFact
from error_alerts import ErrorAlerts, metadata
from test_error_alerts import subject  # noqa: F401  (shared fixture)
from test_error_alerts_core import rebind_to


@pytest.fixture
def probe_family(monkeypatch):
    monkeypatch.setitem(error_adapters.FAMILY_CODES, "probe.v1", frozenset({"probe_failed", "probe_other"}))


async def emit(s, episode, condition="active", code="probe_failed"):
    return await s.service.emit(ErrorFact("probe.v1", code, episode, condition=condition))


def count_reads(monkeypatch):
    calls = []
    original = ErrorAlerts.notice_rows

    async def counted(self, **kwargs):
        calls.append(kwargs)
        return await original(self, **kwargs)

    monkeypatch.setattr(ErrorAlerts, "notice_rows", counted)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("facts", [2, 8])
async def test_a_settled_pass_reads_notices_once_whatever_the_fact_count(
    subject, probe_family, monkeypatch, facts,
):
    s = subject
    monkeypatch.setenv("PENTACLE_FRONT_DESK_DIGEST_S", "0")  # held rows fold
    for i in range(facts):
        await emit(s, f"e{i}")
    await s.service.reconcile()  # enqueues; acting facts read their own rows
    assert len(await s.notify._db.call("error_rows")) == facts
    for _ in range(2):
        await s.queue.drain_once()  # deliver and fold: a pending transport re-reads
    calls = count_reads(monkeypatch)
    await s.service.reconcile()
    # The pass read, the pending rebind read and the prune read; on the prior
    # code each fact added its own read (facts + 2).
    assert len(calls) == 3, [sorted(c) for c in calls]


# -- Mid-pass changes: the same outcome as a per-fact fresh read.

async def outcome(s, nids):
    """Notices and fact state, with ids replaced by the fact's position."""
    index = {nid: i for i, nid in enumerate(nids)}
    rows = await s.service.notice_rows()
    notices = sorted((
        (index.get(metadata(r).get("notification_id"), "digest" if metadata(r).get("typed_digest") else "other"),
         r["kind"], metadata(r).get("intent_kind"), bool(metadata(r).get("awareness")),
         bool(r["delivered_at"]), r["terminal_reason"])
        for r in rows), key=repr)
    facts = sorted((
        (index[f["notification_id"]], f["error_context"]["condition"],
         f["error_context"].get("disposition"), f["error_context"]["desired_notice_revision"],
         f["error_context"]["enqueued_notice_revision"],
         (f["error_context"].get("notice_intent") or {}).get("kind"),
         len(f["error_context"].get("notice_ids", ())))
        for f in await s.notify._db.call("error_rows")), key=repr)
    return notices, facts


def before_turn_of(monkeypatch, nid, change):
    """Run `change` once, after the pass read and just before `nid`'s turn."""
    original = ErrorAlerts._reconcile_fact
    fired = []

    async def hooked(self, row, now, *args, **kwargs):
        if row["notification_id"] == nid and not fired:
            fired.append(True)
            await change()
        return await original(self, row, now, *args, **kwargs)

    monkeypatch.setattr(ErrorAlerts, "_reconcile_fact", hooked)
    return fired


def use_mode(monkeypatch, mode):
    if mode == "fresh":
        async def no_snapshot(self):
            return None
        monkeypatch.setattr(ErrorAlerts, "_notice_snapshot", no_snapshot, raising=False)


async def notices_of(s, nid):
    return [r for r in await s.service.notice_rows()
            if metadata(r).get("notification_id") == nid]


async def claim(s, row):
    """The delivery task claims a notice; its delivery is still in flight."""
    assert await s.store.claim_outbound_notice(
        row["notice_id"], owner=s.queue.owner, lease_s=60, force=True) is not None

    async def lands():
        assert await s.store.complete_outbound_notice(row["notice_id"], owner=s.queue.owner)

    return lands


async def fold(s):
    """The front-desk digest folds the held rows that are due."""
    await s.queue.front_desk_digest.tick(now=time.time() + 10 * 3600)


async def fold_and_deliver(s):
    """The front-desk digest folds held rows and is delivered."""
    await fold(s)
    for digest in [r for r in await s.service.notice_rows() if metadata(r).get("typed_digest")]:
        await s.queue.deliver_now(digest["notice_id"])


async def scenario_delivered(s, monkeypatch):
    """A delivery claimed before the fact recovered lands during the pass:
    the recovery notice still follows (never suppressed as unattempted)."""
    a = await emit(s, "a")
    b = await emit(s, "b", code="probe_other")  # its own fingerprint: sent, not held
    await s.service.reconcile()
    (notice,) = await notices_of(s, b)
    assert notice["kind"] == "error_alert" and not notice["delivered_at"]
    lands = await claim(s, notice)
    await emit(s, "b", condition="recovered", code="probe_other")
    return [a, b], before_turn_of(monkeypatch, b, lands)


async def scenario_rebind_chain(s, monkeypatch):
    """The same on a predecessor chain: a rebind pass superseded the first
    notice; the successor's delivery lands during the next pass."""
    a = await emit(s, "a")
    b = await emit(s, "b", code="probe_other")
    await s.service.reconcile()
    await rebind_to(s, "v2-next")
    await s.service.reconcile()  # _rebind_pending supersedes and links successors
    (successor,) = [r for r in await notices_of(s, b) if not r["terminal_at"]]
    assert metadata(successor).get("predecessor_notice_id")
    lands = await claim(s, successor)
    await emit(s, "b", condition="recovered", code="probe_other")
    return [a, b], before_turn_of(monkeypatch, b, lands)


async def scenario_digest_folded(s, monkeypatch):
    """A held notice is folded into a typed digest that is delivered during
    the pass: the fact's transport row becomes the digest."""
    nids = [await emit(s, f"e{i}") for i in range(4)]  # the repeats are held
    await s.service.reconcile()
    assert [r["kind"] for r in await notices_of(s, nids[3])] == ["front_desk_held"]
    return nids, before_turn_of(monkeypatch, nids[3], lambda: fold_and_deliver(s))


async def scenario_policy_folded(s, monkeypatch):
    """The delivery policy changes, and the held notice it would suppress is
    folded into a pending digest during the pass: no second initial alert."""
    a = await emit(s, "a")
    b = await emit(s, "b")  # the same fingerprint: held
    await s.service.reconcile()
    (held,) = await notices_of(s, b)
    assert held["kind"] == "front_desk_held"
    monkeypatch.setitem(error_alerts.POLICY, "effective_delivery_mode", "digest")
    return [a, b], before_turn_of(monkeypatch, b, lambda: fold(s))


SCENARIOS = {
    "delivered": scenario_delivered,
    "digest_folded": scenario_digest_folded,
    "policy_folded": scenario_policy_folded,
    "rebind_chain": scenario_rebind_chain,
}

# What the prior per-fact fresh read gives (mode ``fresh`` reproduces it).
# Notices: (fact or digest, kind, intent, awareness, delivered, terminal reason).
# Facts: (fact, condition, disposition, desired, enqueued, intent, linked notices).
EXPECTED = {
    'delivered': (
        # after the pass
        ([
            (0, 'error_alert', 'initial', False, False, None),
            (1, 'error_alert', 'initial', False, True, None),
            (1, 'front_desk_held', 'recovery', False, False, None),
        ],
         [
            (0, 'active', None, 1, 1, 'initial', 1),
            (1, 'recovered', None, 2, 2, 'recovery', 2),
        ]),
        # after two drains: notices, facts, pastes
        ([
            (0, 'error_alert', 'initial', False, True, None),
            (1, 'error_alert', 'initial', False, True, None),
            (1, 'front_desk_held', 'recovery', False, False, None),
        ],
         [
            (0, 'active', None, 1, 1, 'initial', 1),
            (1, 'recovered', None, 2, 2, 'recovery', 2),
        ],
         1),
    ),
    'digest_folded': (
        # after the pass
        ([
            ('digest', 'error_alert', 'initial', False, True, None),
            (0, 'error_alert', 'initial', False, False, None),
            (1, 'front_desk_held', 'initial', False, False, 'folded_into_digest'),
            (2, 'front_desk_held', 'initial', False, False, 'folded_into_digest'),
            (3, 'front_desk_held', 'initial', False, False, 'folded_into_digest'),
        ],
         [
            (0, 'active', None, 1, 1, 'initial', 1),
            (1, 'active', None, 1, 1, 'initial', 1),
            (2, 'active', None, 1, 1, 'initial', 1),
            (3, 'active', None, 1, 1, 'initial', 1),
        ]),
        # after two drains: notices, facts, pastes
        ([
            ('digest', 'error_alert', 'initial', False, True, None),
            (0, 'error_alert', 'initial', False, True, None),
            (1, 'front_desk_held', 'initial', False, False, 'folded_into_digest'),
            (2, 'front_desk_held', 'initial', False, False, 'folded_into_digest'),
            (3, 'front_desk_held', 'initial', False, False, 'folded_into_digest'),
        ],
         [
            (0, 'active', None, 1, 1, 'initial', 1),
            (1, 'active', None, 1, 1, 'initial', 1),
            (2, 'active', None, 1, 1, 'initial', 1),
            (3, 'active', None, 1, 1, 'initial', 1),
        ],
         2),
    ),
    'policy_folded': (
        # after the pass
        ([
            ('digest', 'error_alert', 'initial', False, False, None),
            (0, 'error_alert', 'initial', False, False, None),
            (1, 'front_desk_held', 'initial', False, False, 'folded_into_digest'),
        ],
         [
            (0, 'active', None, 1, 1, 'initial', 1),
            (1, 'active', None, 1, 1, 'initial', 1),
        ]),
        # after two drains: notices, facts, pastes
        ([
            ('digest', 'error_alert', 'initial', False, False, 'suppressed_policy'),
            (0, 'error_alert', 'initial', False, False, 'suppressed_policy'),
            (0, 'front_desk_held', 'initial', False, False, None),
            (1, 'front_desk_held', 'initial', False, False, 'folded_into_digest'),
            (1, 'front_desk_held', 'initial', False, False, None),
        ],
         [
            (0, 'active', None, 2, 2, 'initial', 2),
            (1, 'active', None, 2, 2, 'initial', 2),
        ],
         0),
    ),
    'rebind_chain': (
        # after the pass
        ([
            (0, 'error_alert', 'initial', False, False, 'superseded_binding'),
            (0, 'error_alert', 'initial', False, False, None),
            (1, 'error_alert', 'initial', False, False, 'superseded_binding'),
            (1, 'error_alert', 'initial', False, True, None),
            (1, 'front_desk_held', 'recovery', False, False, None),
        ],
         [
            (0, 'active', None, 1, 1, 'initial', 2),
            (1, 'recovered', None, 2, 2, 'recovery', 3),
        ]),
        # after two drains: notices, facts, pastes
        ([
            (0, 'error_alert', 'initial', False, False, 'superseded_binding'),
            (0, 'error_alert', 'initial', False, False, None),
            (1, 'error_alert', 'initial', False, False, 'superseded_binding'),
            (1, 'error_alert', 'initial', False, True, None),
            (1, 'front_desk_held', 'recovery', False, False, None),
        ],
         [
            (0, 'active', None, 1, 1, 'initial', 2),
            (1, 'recovered', None, 2, 2, 'recovery', 3),
        ],
         1),
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["batched", "fresh"])
@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_a_mid_pass_change_gives_the_fresh_read_outcome(
    subject, probe_family, monkeypatch, name, mode,
):
    s = subject
    use_mode(monkeypatch, mode)
    nids, fired = await SCENARIOS[name](s, monkeypatch)
    await s.service.reconcile()
    assert fired == [True]  # the hook fires once; later passes run unhooked
    after_pass = await outcome(s, nids)  # the decision in that pass itself
    for _ in range(2):
        await s.queue.drain_once()
    notices, facts = await outcome(s, nids)
    got = (after_pass, (notices, facts, len(s.provider.pastes)))
    assert got == EXPECTED[name], got


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["batched", "fresh"])
async def test_a_row_inserted_mid_pass_is_linked_by_the_next_pass(
    subject, probe_family, monkeypatch, mode,
):
    """The bound of the pass read. No pass code inserts a row for another
    fact (only the fact itself, and the rebind after the fact loop), so this
    writer is contrived: a successor inserted during the pass, unlinked,
    after the fact's alert was delivered (a pending transport re-reads). The
    fresh read links it in that pass; the pass read, which found nothing to
    do, links it in the next. No notice or alert differs at any point."""
    s = subject
    use_mode(monkeypatch, mode)
    a = await emit(s, "a")
    b = await emit(s, "b", code="probe_other")
    await s.service.reconcile()
    await s.queue.drain_once()  # deliver both alerts: the transports settle
    (old,) = await notices_of(s, b)
    new = await rebind_to(s, "v2-next")

    async def insert_successor():
        fact = await s.notify._db.call("error_get", b)
        binding = {"stream_id": "fixture:v2-next", "generation": new["session_generation"]}
        await s.service._enqueue(fact, fact["error_context"], binding, time.time(),
                                 predecessor=old["notice_id"])

    fired = before_turn_of(monkeypatch, b, insert_successor)
    await s.service.reconcile()
    assert fired == [True]
    after_pass = await outcome(s, [a, b])
    await s.service.reconcile()
    after_next = await outcome(s, [a, b])
    for _ in range(2):
        await s.queue.drain_once()
    got = (after_pass, after_next, await outcome(s, [a, b]), len(s.provider.pastes))
    expected = BOUND
    if mode == "batched":  # the one difference: b's successor is linked a pass later
        notices, facts = expected[0]
        facts = [f[:-1] + (1,) if f[0] == 1 else f for f in facts]
        expected = ((notices, facts),) + expected[1:]
    assert got == expected, got


# The fresh read's outcome after the pass, after the next pass, after two
# drains, and pastes.
BOUND = (
    ([
        (0, 'error_alert', 'initial', False, True, None),
        (1, 'error_alert', 'initial', False, False, None),
        (1, 'error_alert', 'initial', False, True, None),
    ],
     [
        (0, 'active', None, 1, 1, 'initial', 1),
        (1, 'active', None, 1, 1, 'initial', 2),
    ]),
    ([
        (0, 'error_alert', 'initial', False, True, None),
        (1, 'error_alert', 'initial', False, False, None),
        (1, 'error_alert', 'initial', False, True, None),
    ],
     [
        (0, 'active', None, 1, 1, 'initial', 1),
        (1, 'active', None, 1, 1, 'initial', 2),
    ]),
    ([
        (0, 'error_alert', 'initial', False, True, None),
        (1, 'error_alert', 'initial', False, False, None),
        (1, 'error_alert', 'initial', False, True, None),
    ],
     [
        (0, 'active', None, 1, 1, 'initial', 1),
        (1, 'active', None, 1, 1, 'initial', 2),
    ]),
    3,
)


def initial_pastes_naming(s, nid):
    return sum(f"BLOCKER probe.v1 probe_failed alert={nid}" in str(paste) for paste in s.provider.pastes)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["batched", "fresh"])
async def test_a_mixed_digest_rejected_mid_pass_still_alerts_the_active_member(
    subject, probe_family, monkeypatch, mode,
):
    """A and B are folded into one digest and A recovers. Delivery rejects
    the digest (suppressed_members_changed) just before B's turn in the
    sweep's pass: B, still active, needs its replacement initial alert in
    that pass, before it too can recover (review 329bbcf0, finding 1)."""
    s = subject
    use_mode(monkeypatch, mode)
    monkeypatch.setenv("PENTACLE_FRONT_DESK_DIGEST_S", "0")
    monkeypatch.setitem(error_alerts.POLICY, "effective_delivery_mode", "digest")
    a = await emit(s, "a")
    b = await emit(s, "b")
    await s.service.reconcile()
    await fold(s)
    (digest,) = [r for r in await s.service.notice_rows() if metadata(r).get("typed_digest")]
    assert set(metadata(digest)["members"]) == {a, b}
    await emit(s, "a", condition="recovered")

    async def reject():
        await s.queue.deliver_now(digest["notice_id"])
        (after,) = [r for r in await s.service.notice_rows() if r["notice_id"] == digest["notice_id"]]
        assert after["terminal_reason"] == "suppressed_members_changed"

    fired = before_turn_of(monkeypatch, b, reject)
    await s.queue.drain_once()
    assert fired == [True]
    assert initial_pastes_naming(s, b) == 1
    await emit(s, "b", condition="recovered")
    for _ in range(3):
        await s.queue.drain_once()
    assert initial_pastes_naming(s, b) == 1


def count_metadata(monkeypatch):
    visits = [0]
    original = error_alerts.metadata

    def counted(row):
        visits[0] += 1
        return original(row)

    monkeypatch.setattr(error_alerts, "metadata", counted)
    return visits


@pytest.mark.asyncio
@pytest.mark.parametrize("facts", [4, 16, 32])
async def test_a_settled_pass_visits_each_notice_a_bounded_number_of_times(
    subject, probe_family, monkeypatch, facts,
):
    """Each fact sees only its own notices: metadata decodes in a settled
    pass grow with facts plus notices, not with their product (review
    329bbcf0, finding 2: 1,018 facts x 1,055 notices stalled the loop)."""
    s = subject
    monkeypatch.setenv("PENTACLE_FRONT_DESK_DIGEST_S", "0")  # held rows fold
    for i in range(facts):
        await emit(s, f"e{i}")
    await s.service.reconcile()
    for _ in range(2):
        await s.queue.drain_once()  # deliver and fold: every transport settles
    notices = len(await s.service.notice_rows())
    visits = count_metadata(monkeypatch)
    await s.service.reconcile()
    # About 7 decodes per fact and notice (the pass read's closure, the
    # index, each fact's own rows, the prune); 32 facts gave 1,244 when each
    # fact scanned every notice.
    assert visits[0] <= 8 * (facts + notices), (facts, notices, visits[0])
