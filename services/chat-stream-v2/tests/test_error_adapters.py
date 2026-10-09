"""Synthetic mapper and detector seam proofs; no transport or live credentials."""
from __future__ import annotations

import asyncio
import hashlib
import unittest

from alerts import Alerts
from error_adapters import ADAPTERS, adapt

class FixturePeerHosts:
    local_host = 'fixture-primary'

    def __init__(self, tmux):
        self.tmux = tmux

    def known(self, host):
        return host == 'fixture-peer'

    def is_local(self, host):
        return host == self.local_host

    async def probe_once(self, host):
        return self.known(host)

    def tmux_for(self, host):
        assert self.known(host)
        return self.tmux


CASES = (
    ('reconciler_session_dead', 'session_lifecycle', 'session_dead', {'episode_id': 'ep-synthetic-1'}, ('episode_id',)),
    ('close_failed', 'session_lifecycle', 'close_failed', {'stream_id': 'fixture:v2-test', 'generation': 1, 'reason': 'timeout'}, ('stream_id', 'generation', 'reason')),
    ('close_carcass', 'session_lifecycle', 'close_carcass', {'stream_id': 'fixture:v2-test', 'generation': 1}, ('stream_id', 'generation')),
    ('deferred_reap_exhausted', 'session_lifecycle', 'reap_exhausted', {'stream_id': 'fixture:v2-test', 'generation': 1}, ('stream_id', 'generation')),
    ('reap_fenced', 'session_lifecycle', 'reap_fenced', {'stream_id': 'fixture:v2-test', 'generation': 1}, ('stream_id', 'generation')),
    ('pin_drift', 'integrity', 'pin_drift', {'host': 'fixture', 'pinned_sha': 'a'*40, 'daemon_sha': 'b'*40}, ('host', 'pinned_sha', 'daemon_sha')),
    ('close_claim_mismatch', 'integrity', 'close_claim_mismatch', {'report_id': 'report-synthetic-1'}, ('report_id',)),
    ('system_deploy_failed', 'system_deploy', 'deploy_failed', {'severity': 'critical', 'dedup_key': 'pipeline|reconciler|2026-01-15'}, ('dedup_key',)),
    ('system_backup_failed', 'system_backup', 'backup_failed', {'severity': 'critical', 'dedup_key': 'wmi-backup|archive_failed|2026-01-15'}, ('dedup_key',)),
    ('bot_messaging_failed', 'bot_messaging', 'registration_failed', {'step': 'registration', 'status_class': 'callback_timeout', 'operation_id': '00000000-0000-4000-8000-000000000001'}, ('step', 'operation_id')),
    ('bot_messaging_failed', 'bot_messaging', 'delivery_failed', {'step': 'delivery', 'status_class': 'handoff_failed', 'operation_id': '00000000-0000-4000-8000-000000000001'}, ('step', 'operation_id')),
)


def expected_episode(code, fields, keys):
    return code + ':' + hashlib.sha256('\0'.join(str(fields[key]) for key in keys).encode()).hexdigest()[:40]


class Recorder:
    def __init__(self):
        self.calls = []

    async def emit(self, fact, principal=None):
        await asyncio.sleep(0)
        self.calls.append((fact, principal))
        return 'synthetic-notification-id'


class MapperTests(unittest.TestCase):
    def test_A1_each_listed_kind_maps_to_exact_fact(self):
        for kind, family, code, fields, keys in CASES:
            with self.subTest(kind=kind, code=code):
                fact = adapt(kind, fields)
                self.assertIsNotNone(fact)
                self.assertEqual((fact.family, fact.code, fact.condition), (family, code, 'active'))
                self.assertEqual(fact.episode_id, expected_episode(code, fields, keys))
                self.assertEqual(fact.stage, fields.get('status_class'))

    def test_A2_replay_and_A3_different_keys(self):
        for kind, _, code, fields, keys in CASES:
            with self.subTest(kind=kind, code=code):
                first = adapt(kind, fields)
                self.assertEqual(first, adapt(kind, dict(fields)))
                changed = dict(fields)
                key = 'generation' if 'generation' in keys else keys[-1]
                if key == 'operation_id':
                    changed[key] = '00000000-0000-4000-8000-000000000002'
                elif key == 'dedup_key':
                    changed[key] = changed[key].replace('2026-01-15', '2026-01-16')
                else:
                    changed[key] = str(changed[key]) + '2'
                self.assertNotEqual(first.episode_id, adapt(kind, changed).episode_id)

    def test_A4_quiet_kinds_are_not_registered(self):
        for kind in ('operator_offline_close', 'close_deferred', 'reconciler_session_survivors', 'unlisted'):
            self.assertNotIn(kind, ADAPTERS)
            self.assertIsNone(adapt(kind, {}))
            with self.assertLogs('chat_streamd_v2.alerts', level='WARNING') as logs:
                Alerts().emit(kind, detail='raw-synthetic-field')
            self.assertIn("{'detail': 'raw-synthetic-field'}", logs.output[0])

    def test_A5_missing_empty_and_nonconvertible_keys_never_raise(self):
        class BadString:
            def __str__(self):
                raise ValueError('synthetic conversion failure')
        for kind, _, _, fields, keys in CASES:
            for key in keys:
                for value in (None, '', BadString()):
                    with self.subTest(kind=kind, key=key, value=type(value).__name__):
                        self.assertIsNone(adapt(kind, {**fields, key: value}))
                missing = dict(fields)
                del missing[key]
                self.assertIsNone(adapt(kind, missing))

    def test_A4_system_predicates_and_bot_fields(self):
        for kind in ('system_deploy_failed', 'system_backup_failed'):
            for severity in ('warning', 'info', None, [], {}):
                self.assertIsNone(adapt(kind, {'severity': severity, 'dedup_key': 'pipeline|reconciler|2026-01-15'}))
            for dedup in ('census|fn|invariant|2026-01-15', 'infra-census|fn|2026-01-15', None, [], {}, 'pipeline|bad/name|2026-01-15'):
                self.assertIsNone(adapt(kind, {'severity': 'critical', 'dedup_key': dedup}))
        base = CASES[-1][3]
        for field, value in (('step', 'other'), ('step', []), ('status_class', 'private text'), ('status_class', []), ('operation_id', 'invalid')):
            self.assertIsNone(adapt('bot_messaging_failed', {**base, field: value}))

    def test_A6_only_typed_ids_reach_facts(self):
        sentinel = 'synthetic-private-title-body-token-/path-https://example.invalid'
        for kind, _, _, fields, _ in CASES:
            fact = adapt(kind, {**fields, 'title': sentinel, 'body': sentinel, 'path': sentinel, 'stream_token': sentinel, 'url': sentinel})
            self.assertNotIn(sentinel, repr(fact))


class RecordTests(unittest.IsolatedAsyncioTestCase):
    async def test_A1_record_awaits_sink_and_A5_sink_none(self):
        for kind, _, _, fields, _ in CASES:
            alerts = Alerts()
            recorder = Recorder()
            alerts.sink = recorder
            self.assertEqual(await alerts.record(kind, **fields), 'synthetic-notification-id')
            self.assertEqual(recorder.calls, [(adapt(kind, fields), None)])
            alerts.sink = None
            self.assertIsNone(await alerts.record(kind, **fields))
            self.assertIsNone(await alerts.record(kind))


if __name__ == '__main__':
    unittest.main()

# Real detector caller paths: the sink deliberately suspends so a missing await
# cannot pass merely because a later event-loop turn eventually records a fact.
import pytest
from unittest.mock import AsyncMock


class GatedRecorder(Recorder):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def emit(self, fact, principal=None):
        self.entered.set()
        await self.release.wait()
        return await super().emit(fact, principal)


async def asserted_await(operation, recorder, code):
    task = asyncio.create_task(operation)
    entered = asyncio.create_task(recorder.entered.wait())
    try:
        done, _ = await asyncio.wait({task, entered}, timeout=3, return_when=asyncio.FIRST_COMPLETED)
        assert entered in done, 'detector returned without reaching the recording sink'
        assert not task.done(), 'detector must wait for durable sink completion'
        recorder.release.set()
        result = await task
        assert len(recorder.calls) == 1
        fact, principal = recorder.calls[0]
        assert fact.code == code and principal is None
        return result, fact
    finally:
        recorder.release.set()
        for pending in (entered, task):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(task, entered, return_exceptions=True)


def gated_alerts():
    alerts = Alerts()
    recorder = GatedRecorder()
    alerts.sink = recorder
    return alerts, recorder


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome,code', [('failed', 'close_failed'), ('carcass', 'close_carcass')])
async def test_A1_local_close_sites_await_sink(outcome, code):
    from sessions import Sessions
    from store import Store
    from test_close_ladder import LadderTmux
    store = Store(':memory:'); store.start()
    alerts, recorder = gated_alerts()
    sessions = Sessions(store, tmux=LadderTmux(), local_host='fixture', alerts=alerts)
    sessions._terminate_pane = AsyncMock(return_value=(outcome, 'synthetic-pid', {'reap_status': 'unknown', 'survivors': []}))
    try:
        row = await store.open_session('fixture', 'v2-test')
        await sessions.refresh()
        _, fact = await asserted_await(sessions.close('fixture', 'v2-test'), recorder, code)
        fields = {'stream_id': 'fixture:v2-test', 'generation': row['session_generation'], 'reason': 'no_deliverable_signal'}
        keys = ('stream_id', 'generation', 'reason') if outcome == 'failed' else ('stream_id', 'generation')
        assert fact.episode_id == expected_episode(code, fields, keys)
    finally:
        store.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('remote', [False, True])
async def test_A1_reap_fenced_direct_callers_await_sink(remote):
    from sessions import Sessions
    from store import Store
    from test_close_ladder import LadderTmux
    store = Store(':memory:'); store.start()
    alerts, recorder = gated_alerts()
    host = 'fixture-peer' if remote else 'fixture'
    tmux = LadderTmux()
    sessions = Sessions(store, tmux=tmux, hosts=FixturePeerHosts(tmux) if remote else None,
                        local_host='fixture-primary' if remote else 'fixture', alerts=alerts)
    try:
        row = await store.open_session(host, 'v2-test', visibility='hidden')
        await sessions.refresh()
        sessions.apply_live(host + ':v2-test', capture_liveness='wedged_unknown')
        result, fact = await asserted_await(sessions.reap_idle(host, 'v2-test'), recorder, 'reap_fenced')
        assert result['fenced']
        assert fact.episode_id == expected_episode('reap_fenced', {'stream_id': host + ':v2-test', 'generation': row['session_generation']}, ('stream_id', 'generation'))
    finally:
        store.stop()


@pytest.mark.asyncio
async def test_A1_remote_close_failed_awaits_sink(monkeypatch):
    from sessions import Sessions
    from store import Store
    monkeypatch.setattr('sessions.CLOSE_GRACEFUL_CONFIRM_S', 0)
    class Alive:
        async def session_state(self, name): return 'alive'
        async def kill_session(self, name): return None
        async def pane_identity(self, name): return {'pane_pid': '123', 'pane_id': '%1', 'session_name': name}
    store = Store(':memory:'); store.start()
    alerts, recorder = gated_alerts()
    sessions = Sessions(store, hosts=FixturePeerHosts(Alive()), local_host='fixture-primary', alerts=alerts)
    try:
        row = await store.open_session('fixture-peer', 'v2-test')
        await sessions.refresh()
        result, fact = await asserted_await(sessions.close('fixture-peer', 'v2-test'), recorder, 'close_failed')
        assert result['failed']
        assert fact.episode_id == expected_episode('close_failed', {'stream_id': 'fixture-peer:v2-test', 'generation': row['session_generation'], 'reason': 'pane_still_alive_after_kill'}, ('stream_id', 'generation', 'reason'))
    finally:
        store.stop()


@pytest.mark.asyncio
async def test_A1_deferred_reap_exhaustion_awaits_sink(monkeypatch):
    from test_offline_operator_close import build, frame
    monkeypatch.setattr('sessions.CLOSE_GRACEFUL_CONFIRM_S', 0)
    store, peer, sessions, server, row = await build()
    alerts, recorder = gated_alerts()
    sessions.alerts = alerts
    try:
        await server._on_close(frame(operator_confirm=True))
        peer.online = True
        peer.kill_session = AsyncMock()
        for _ in range(4):
            await sessions.reap_deferred(await store.get_deferred_reap('remote-peer:v2-offline'))
        assert recorder.calls == []
        _, fact = await asserted_await(sessions.reap_deferred(await store.get_deferred_reap('remote-peer:v2-offline')), recorder, 'reap_exhausted')
        assert fact.episode_id == expected_episode('reap_exhausted', {'stream_id': 'remote-peer:v2-offline', 'generation': row['session_generation']}, ('stream_id', 'generation'))
    finally:
        store.stop()


@pytest.mark.asyncio
async def test_A1_reconciler_dead_awaits_sink():
    from test_session_reconciler import _FakeTmux, _open_remote
    from reconciler import SessionReconciler, ReconcileConfig
    class ZombieTmux(_FakeTmux):
        async def session_state(self, name): return 'gone'
    store, sessions, hosts, original = await _open_remote(ZombieTmux((0, 'v2-other\t99\n')))
    alerts, recorder = gated_alerts()
    reconciler = SessionReconciler(sessions, hosts, presence=original.presence, alerts=alerts,
                                   config=ReconcileConfig(threshold_checks=2))
    try:
        await reconciler.reconcile_once()
        assert recorder.calls == []
        result, _ = await asserted_await(reconciler.reconcile_once(), recorder, 'session_dead')
        assert result['closed'] == 1
    finally:
        store.stop()


@pytest.mark.asyncio
async def test_A1_pin_detector_awaits_sink():
    from event_push import EventPush
    from store import Store
    store = Store(':memory:'); store.start()
    alerts, recorder = gated_alerts()
    try:
        await store.put('event_push.target_sha', 'a'*40)
        event_push = EventPush(store, AsyncMock(), alerts, recent_limit=20, daemon_sha='b'*40)
        result, fact = await asserted_await(event_push.check_pin_drift(), recorder, 'pin_drift')
        assert result
        assert fact.episode_id == expected_episode('pin_drift', {'host': 'daemon', 'pinned_sha': 'a'*40, 'daemon_sha': 'b'*40}, ('host', 'pinned_sha', 'daemon_sha'))
        assert await event_push.check_pin_drift()
        assert len(recorder.calls) == 1
    finally:
        store.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('via_server', [False, True])
async def test_A1_claim_mismatch_both_direct_callers_await_sink(via_server):
    from ledger import Ledger
    from sessions import Sessions
    from server import Server
    from store import Store
    from test_report_variants import _CloseImmediatelyTmux, _claim_report, _ac_claim
    store = Store(':memory:'); store.start()
    alerts, recorder = gated_alerts()
    sessions = Sessions(store, tmux=_CloseImmediatelyTmux(), local_host='alpha', alerts=alerts)
    ledger = Ledger(store, sessions=sessions)
    ledger.verify_ac_claim = AsyncMock(return_value=('mismatch', {'spec_sha': 'a'*40, 'unverified_indices': [1]}))
    try:
        await store.open_session('alpha', 'worker', visibility='hidden')
        await sessions.refresh()
        claim = _ac_claim('synthetic-spec', 'a'*40, True)
        if via_server:
            server = Server(store=store, sessions=sessions, local_host='alpha')
            server.ledger = ledger
            operation = server._on_close({'stream_id': 'alpha:worker', 'request_id': 'synthetic-close', 'report_id': 'report-synthetic', 'ac_claim': claim,
                '_auth_context': {'operator_authenticated': True, 'operator_principal': 'operator:synthetic', 'connection_client': 'desktop', 'transport': 'v2'}})
        else:
            operation = ledger.ingest(_claim_report('report-synthetic', claim), announce=False)
        _, fact = await asserted_await(operation, recorder, 'close_claim_mismatch')
        assert fact.episode_id == expected_episode('close_claim_mismatch', {'report_id': 'report-synthetic'}, ('report_id',))
    finally:
        store.stop()
