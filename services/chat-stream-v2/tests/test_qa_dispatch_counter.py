"""Actual send admission journey; provider/tmux is the only fake counterpart."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from comms import Comms
from ledger import Ledger
from server import Server
from sessions import Sessions, VerbError
from spawnctl import SpawnCtl
from store import Store
from test_send_semantics import FakeTmux

SPEC = 'spec_pentacle__qa_counter_test'
LEAD = 'localhost:lead'
SHA = 'a' * 40
TOKEN = 'qa-counter-test-token'
META = {'qa_spec_id': SPEC, 'qa_surface': 'admission', 'qa_cycle': 1}


async def state(path=':memory:'):
    store = Store(str(path))
    store.set_spec_identity_resolver(lambda value: value if value == SPEC else None)
    store.start()
    tmux = FakeTmux()
    sessions = Sessions(store, tmux=tmux, local_host='localhost')
    await sessions.open('localhost', 'lead', role='lead', spec_id=SPEC, spec_ids=[SPEC],
                        token_hash=hashlib.sha256(TOKEN.encode()).hexdigest(), token_hash_version='sha256:v1')
    for name in ('qa1', 'qa2', 'qa3', 'worker'):
        await sessions.open('localhost', name, role='worker' if name == 'worker' else 'qa',
                            provider='claude', parent_stream_id=LEAD,
                            spec_id=SPEC, spec_ids=[SPEC], bootstrap_state='ready')
    ctl = SpawnCtl(store, sessions, tmux=tmux)
    comms = Comms(store, sessions, ctl)
    return store, sessions, tmux, comms, Ledger(store, sessions=sessions), Server(store=store, sessions=sessions)


def send_msg(name, msg_id=0, **overrides):
    return {'stream_id': f'localhost:{name}', 'text': f'review {name}',
            'from_stream_id': LEAD, 'stream_token': TOKEN, 'msg_id': msg_id,
            'request_id': f'send-{name}-{msg_id}', **META, **overrides}


def report_msg(name, **overrides):
    return {'report_id': f'reject-{name}', 'from_stream_id': f'localhost:{name}',
            'msg_id': 0, 'status': 'done', 'qa_verdict': 'reject', 'target_sha': SHA,
            'summary': 'In-scope admission defect', 'findings': [], 'next_action': 'repair',
            'extras': {'qa_review': {'reviewed_scope': 'admission', 'candidate_identity': SHA,
                                     'gate_evidence_digest': 'b' * 64}}, **overrides}


async def issue(server, verb, **fields):
    return (await server._dispatch(json.dumps({
        'type': f'coordination.spec_issue.{verb}', 'spec_id': SPEC, 'surface': 'admission',
        'cycle': 1, 'stream_token': TOKEN, 'from_stream_id': LEAD, **fields,
    })))[0]


async def two_rejects(comms, ledger, server):
    for name in ('qa1', 'qa2'):
        result = await comms.send(send_msg(name))
        assert result['delivery'] == 'landed'
        await ledger.ingest(report_msg(name))
        response = await issue(server, 'adjudicate', report_id=f'reject-{name}',
                               adjudicated_valid=True, reason='AC admission is violated')
        print('adjudication:', json.dumps(response, sort_keys=True))
        assert response['type'] == 'coordination.spec_issue.adjudicate.ok', response
    shown = await issue(server, 'show')
    assert shown['report_ids'] == ['reject-qa1', 'reject-qa2'], shown


def test_third_same_surface_send_is_refused_before_paste(monkeypatch):
    monkeypatch.setenv('PENTACLE_QA_DISPATCH_MODE', 'enforce')

    async def run():
        store, _, tmux, comms, ledger, server = await state()
        try:
            await two_rejects(comms, ledger, server)
            before = len(tmux.pastes)
            try:
                result = await comms.send(send_msg('qa3'))
            except VerbError as exc:
                assert exc.code == 'qa_dispatch_reject_limit'
                assert exc.extra['report_ids'] == ['reject-qa1', 'reject-qa2']
                assert len(tmux.pastes) == before
            else:
                pytest.fail(f'PRODUCT_FAIL: third same-surface QA admitted: {result["delivery"]}; '
                            f'new pastes={len(tmux.pastes)-before}')
        finally:
            store.stop()

    asyncio.run(run())


def test_third_same_surface_spawn_is_refused_before_pane(monkeypatch):
    from test_spec_binding_provenance import SpawnTmux
    monkeypatch.setenv('PENTACLE_QA_DISPATCH_MODE', 'enforce')

    class Catalog:
        def resolution_for(self, value):
            return 'resolved' if value == SPEC else 'zero_matches'

        def canonical_spec_identity(self, value):
            return value if value == SPEC else None

    async def run():
        store, sessions, _, comms, ledger, server = await state()
        tmux = SpawnTmux()
        ctl = SpawnCtl(store, sessions, tmux=tmux, specs=Catalog())
        try:
            await two_rejects(comms, ledger, server)
            try:
                result = await ctl.spawn({
                    'objective': 'Review same surface again', 'command': 'provider',
                    'session_name': 'qa-third-spawn', 'request_id': 'third-spawn',
                    'parent_stream_id': LEAD, 'from_stream_id': LEAD, 'stream_token': TOKEN,
                    'role': 'qa', 'spec_id': SPEC, **META,
                }, 'localhost')
            except VerbError as exc:
                assert exc.code == 'qa_dispatch_reject_limit'
                assert tmux.live == set()
            else:
                pytest.fail(f'PRODUCT_FAIL: third same-surface spawn admitted: {result["type"]}')
        finally:
            await asyncio.gather(*tuple(ctl._background_spawns), return_exceptions=True)
            for name in tuple(tmux.live):
                await tmux.kill_session(name)
            store.stop()

    asyncio.run(run())
