"""Diagnostic RED: real Store/Comms tell, delayed native-ingest counterpart only."""
import asyncio
from types import SimpleNamespace

import pytest

import comms as comms_module
from comms import Comms
from provider_cli_initial import input_obligations
from sessions import Sessions
from spawnctl import SpawnCtl
from store import Store
from submission_events import EventWatermark
from test_tell_immediate_delivery import StoreTailProofCodex


@pytest.mark.parametrize('timing', ['immediate', 'after_initial_reply'])
def test_exact_late_native_input_proof_settles_the_same_tell_without_resubmission(monkeypatch, timing):
    monkeypatch.setattr(comms_module, 'SUBMISSION_EVIDENCE_TIMEOUT_S', 0.01)
    monkeypatch.setattr(comms_module, 'SUBMISSION_EVIDENCE_POLL_S', 0.001)

    async def journey():
        store = Store(':memory:')
        store.start()
        target = 'localhost:v2-proof-ack-diagnostic'
        tmux = StoreTailProofCodex(store, target, append_on_paste=timing == 'immediate')
        sessions = Sessions(store, tmux=tmux, local_host='localhost')
        comms = Comms(store, sessions, SpawnCtl(store, sessions, tmux=tmux))
        try:
            await sessions.open('localhost', 'v2-proof-ack-diagnostic', provider='codex')
            watermark = await comms.submission_proof.watermark(target)
            assert watermark.state == 'reachable'
            request = {'stream_id': target, 'message': 'diagnostic exact input',
                       'tell_id': 'diagnostic-one-tell'}
            first = await comms.tell(request)
            if timing == 'after_initial_reply':
                assert first['delivery_status'] == 'committed_pending_proof'
                assert first['do_not_resubmit'] is True
                # Real Store ingest append after the initial confirmation window.
                # The only fake is native latency; no product callback is patched.
                await store.append_session_event(target, {
                    'stream_id': target, 'provider': 'codex', 'kind': 'USER',
                    'text': tmux.pastes[0], 'timestamp': '2026-10-30T10:00:00Z',
                }, identity='diagnostic-exact-late-input', limit=500)
            proof = await comms.submission_proof.lookup(
                target, expected_text=tmux.pastes[0], watermark=watermark)
            assert proof.proven, 'The authoritative same-stream post-watermark event is present'
            replay = await comms.tell(request)
            assert replay['duplicate'] is True
            assert len(tmux.pastes) == 1 and tmux.enter_only == 0
            saved = await store.get_tell_delivery(request['tell_id'])
            obligations = await store.submit(lambda conn: input_obligations(SimpleNamespace(conn=conn)))
            # The documented evidence-only reconciliation must settle the original
            # receipt once exact input proof exists; never make a second paste.
            assert saved['reply']['delivery_status'] == 'delivered', {
                'timing': timing, 'proof_state': proof.state,
                'stored_delivery': saved['reply']['delivery_status'],
                'replay_delivery': replay['delivery_status'],
                'stored_watermark': saved['delivery'].get('proof_watermark'),
                'remaining_obligations': len(obligations), 'pastes': len(tmux.pastes),
            }
            assert not obligations
        finally:
            store.stop()

    asyncio.run(journey())
