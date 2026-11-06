import asyncio
import hashlib
import json

import pytest

from store import Store
from test_assistant_lane_rulings import _rulings_rig, ROOT, ADVISOR


def test_authority_set_records_brief_and_seat_shape(tmp_path):
    brief = tmp_path / 'brief.md'
    brief.write_text('Restore advisor instructions')
    async def run():
        store = Store(':memory:')
        store.start()
        try:
            root, advisor, server, _ = await _rulings_rig(store)
            with pytest.raises(ValueError, match='assistant_authority_brief_required'):
                await server.lane_rulings.configure(ADVISOR, actor_stream_id=ROOT,
                                                     actor_generation=root['session_generation'])
            await server.lane_rulings.configure(ADVISOR, actor_stream_id=ROOT,
                actor_generation=root['session_generation'], brief_file=str(brief))
            shape = json.loads(await store.get('assistant.authority.shape'))
            assert shape['stream_id'] == ADVISOR
            assert shape['generation'] == advisor['session_generation']
            assert shape['provider'] == 'codex'
            assert shape['brief_sha256'] == hashlib.sha256(brief.read_bytes()).hexdigest()
            assert shape['account_observed'] is None
        finally:
            store.stop()
    asyncio.run(run())


def test_restore_waits_for_delivered_outcome_and_adopts(tmp_path):
    from assistant_authority_restore import AuthorityRestore
    from spawnctl import SpawnCtl
    from test_assistant_lane_rulings import _set_seat

    class FakeSpawn:
        _spawn_payload_hash = staticmethod(SpawnCtl._spawn_payload_hash)
        def __init__(self):
            self.calls, self.outcomes, self.reservations = [], [], []
        async def spawn(self, msg, host):
            self.calls.append(msg.copy())
            return {'type': 'spawn.ok'}
        async def spawn_status(self, msg, host):
            return {'outcomes': self.outcomes, 'reservations': self.reservations}

    async def run():
        store = Store(':memory:')
        store.start()
        try:
            fake = FakeSpawn()
            root, advisor, server, _ = await _rulings_rig(store, spawnctl=fake)
            server.local_host = 'fixture-advisor'
            server.sessions.tmux = AlivePane()
            brief = tmp_path / 'brief'
            brief.write_text('advisor instructions')
            await server.lane_rulings.configure(ADVISOR, actor_stream_id=ROOT,
                actor_generation=root['session_generation'], brief_file=str(brief))
            now = [1000.]
            restore = AuthorityRestore(server, inhibit_path=None, clock=lambda: now[0])
            server.authority_restore = restore
            await restore.initialize()
            await _set_seat(store, 'astra', pane_status='pane_dead')
            await server.lane_rulings._check_authority_loss()
            assert not await restore.current(ADVISOR, advisor['session_generation'])
            now[0] += 61
            await server.lane_rulings._check_authority_loss()
            if restore.task:
                await restore.task
            episode = await restore.current(ADVISOR, advisor['session_generation'])
            assert episode['state'] == 'spawning'
            assert len(fake.calls) == 1 and fake.calls[0]['initial_prompt'] == 'advisor instructions'
            assert episode['brief_sha256'] == hashlib.sha256(b'advisor instructions').hexdigest()
            assert episode['brief_bytes_ref'].endswith(':payload.initial_prompt')
            assert 'account' not in fake.calls[0]
            await store.open_session('fixture-advisor', 'new', provider='codex', pane_status='pane_alive',
                bootstrap_state='started', session_generation=episode['attempt_generation'])
            fake.reservations = [{'idempotency_key': episode['spawn_key'], 'stream_id': 'fixture-advisor:new'}]
            await restore.run()
            assert (await server.lane_rulings.binding())['stream_id'] == ADVISOR
            fake.reservations = []
            fake.outcomes = [{'idempotency_key': episode['spawn_key'], 'stream_id': 'fixture-advisor:new',
                              'state': 'delivered', 'request_payload_hash': fake._spawn_payload_hash(fake.calls[0])}]
            await restore.run()
            assert (await server.lane_rulings.binding())['stream_id'] == 'fixture-advisor:new'
            assert (await restore.current(ADVISOR, advisor['session_generation']))['state'] == 'restored'
        finally:
            store.stop()
    asyncio.run(run())


class AlivePane:
    async def session_state(self, name):
        return 'alive'


class RestoreSpawn:
    from spawnctl import SpawnCtl
    _spawn_payload_hash = staticmethod(SpawnCtl._spawn_payload_hash)
    def __init__(self):
        self.calls = []
        self.outcomes = []
        self.reservations = []
    async def spawn(self, msg, host):
        self.calls.append(dict(msg))
        return {'type': 'spawn.ok'}
    async def spawn_status(self, msg, host):
        outcomes = []
        for item in self.outcomes:
            outcome = dict(item)
            matching = next((call for call in self.calls if call['idempotency_key'] == outcome.get('idempotency_key')), None)
            if matching:
                outcome.setdefault('request_payload_hash', self._spawn_payload_hash(matching))
            outcomes.append(outcome)
        return {'outcomes': outcomes, 'reservations': list(self.reservations)}


async def restore_rig(tmp_path, account=None):
    from assistant_authority_restore import AuthorityRestore
    from test_assistant_lane_rulings import _set_seat
    store = Store(':memory:'); store.start()
    fake = RestoreSpawn()
    root, advisor, server, _ = await _rulings_rig(store, spawnctl=fake)
    if account:
        from test_account_rotation import binding
        advisor = await store.open_session('fixture-advisor', 'astra',
            session_generation='bound-advisor', provider='codex',
            account_binding=binding(account, host='fixture-advisor').public())
    server.local_host = 'fixture-advisor'
    server.sessions.tmux = AlivePane()
    brief = tmp_path / 'restore-brief'; brief.write_text('brief')
    await server.lane_rulings.configure(ADVISOR, actor_stream_id=ROOT,
        actor_generation=root['session_generation'], brief_file=str(brief))
    now = [1000.]
    restore = AuthorityRestore(server, inhibit_path=tmp_path / 'inhibit', clock=lambda: now[0])
    server.authority_restore = restore
    await restore.initialize()
    await _set_seat(store, 'astra', pane_status='pane_dead')
    await restore.observe(ADVISOR, advisor['session_generation'], 'pane_dead', advisor)
    now[0] += 61
    await restore.observe(ADVISOR, advisor['session_generation'], 'pane_dead', advisor)
    if restore.task: await restore.task
    return store, root, advisor, server, fake, restore, now, brief


@pytest.mark.parametrize('account', [None, 'spare'])
def test_never_admitted_restart_replays_frozen_payload_same_key(tmp_path, account):
    from assistant_authority_restore import AuthorityRestore
    async def run():
        store, root, advisor, server, fake, restore, now, brief = await restore_rig(tmp_path, account=account)
        try:
            before = fake.calls[0]
            assert before.get('account') == account
            if account:
                from account_runtime import AccountRuntime
                from provider_accounts import AccountError
                runtime = AccountRuntime(store, server.sessions, policy={'enabled': True})
                await runtime.authorize_choice(before)
                with pytest.raises(AccountError, match='account_control_unauthorized'):
                    await runtime.authorize_choice({**before, 'account': 'other'})
            brief.write_text('changed after admission')
            restarted = AuthorityRestore(server, inhibit_path=None, clock=lambda: now[0])
            server.authority_restore = restarted
            await restarted.initialize()
            restarted.loss = (ADVISOR, advisor['session_generation'])
            await restarted.run()
            assert len(fake.calls) == 2 and fake.calls[1] == before
            await restarted.run()
            assert len(fake.calls) == 2
        finally: store.stop()
    asyncio.run(run())


def test_unresolved_reservation_fences_retry_then_adopts_late(tmp_path):
    async def run():
        store, root, advisor, server, fake, restore, now, brief = await restore_rig(tmp_path)
        try:
            episode = await restore.current(ADVISOR, advisor['session_generation'])
            key = episode['spawn_key']
            fake.reservations = [{'idempotency_key': key, 'stream_id': 'fixture-advisor:late'}]
            now[0] += 181
            await restore.run()
            assert (await restore.current(ADVISOR, advisor['session_generation']))['state'] == 'unresolved'
            with pytest.raises(ValueError, match='attempt_unresolved'):
                await restore.manual('retry')
            assert len(fake.calls) == 1
            await store.open_session('fixture-advisor', 'late', provider='codex', pane_status='pane_alive',
                bootstrap_state='started', session_generation=episode['attempt_generation'])
            fake.reservations = []
            fake.outcomes = [{'idempotency_key': key, 'stream_id': 'fixture-advisor:late', 'state': 'delivered',
                              'request_payload_hash': fake._spawn_payload_hash(fake.calls[0])}]
            await restore.run()
            assert (await restore.current(ADVISOR, advisor['session_generation']))['state'] == 'restored'
            assert len(fake.calls) == 1
        finally: store.stop()
    asyncio.run(run())


def test_failed_closed_row_backoffs_exhaust_and_retry_uses_fresh_keys(tmp_path):
    async def run():
        store, root, advisor, server, fake, restore, now, brief = await restore_rig(tmp_path)
        try:
            keys = []
            for index, delay in enumerate((60, 180, None)):
                episode = await restore.current(ADVISOR, advisor['session_generation'])
                keys.append(episode['spawn_key'])
                if index == 0:
                    await store.open_session('fixture-advisor', 'failed', provider='codex',
                        session_generation=episode['attempt_generation'])
                    await store.submit(lambda c: (c.execute("UPDATE sessions SET status='closed' WHERE host='fixture-advisor' AND session_name='failed'"), c.commit()))
                fake.outcomes = [{'idempotency_key': keys[-1], 'state': 'failed', 'stream_id': 'fixture-advisor:failed'}]
                await restore.run()
                episode = await restore.current(ADVISOR, advisor['session_generation'])
                if delay:
                    assert episode['state'] == 'pending' and episode['next_attempt_at'] == now[0] + delay
                    fake.outcomes = []
                    now[0] += delay
                    await restore.run()
                else:
                    assert episode['state'] == 'exhausted'
            fake.outcomes = []
            await restore.manual('retry')
            if restore.task: await restore.task
            episode = await restore.current(ADVISOR, advisor['session_generation'])
            assert episode['budget_epoch'] == 2 and episode['attempt_seq'] == 4
            assert episode['spawn_key'] not in keys and len(set(keys)) == 3
            for delay in (60, 180, None):
                episode = await restore.current(ADVISOR, advisor['session_generation'])
                fake.outcomes = [{'idempotency_key': episode['spawn_key'], 'state': 'failed'}]
                await restore.run()
                if delay:
                    fake.outcomes = []; now[0] += delay
                    await restore.run()
            notices = await store.submit(lambda c: c.execute("SELECT count(*) FROM v2_outbound_notices WHERE notice_id LIKE 'authority-restore:authority_restore_exhausted:%'").fetchone()[0])
            assert notices == 2
        finally: store.stop()
    asyncio.run(run())


def test_real_spawnctl_preserves_service_generation_and_delivers_brief(tmp_path, monkeypatch):
    from spawnctl import SpawnCtl
    from test_spawn_error_no_phantom_row import BootReadyTmux
    from test_assistant_lane_rulings import _set_seat
    from assistant_authority_restore import AuthorityRestore
    async def run():
        store = Store(':memory:'); store.start()
        try:
            root, advisor, server, _ = await _rulings_rig(store)
            server.local_host = 'fixture-advisor'
            tmux = BootReadyTmux()
            server.sessions.tmux = tmux
            server.sessions.local_host = 'fixture-advisor'
            ctl = SpawnCtl(store, server.sessions, tmux=tmux)
            async def launch(msg, host, name):
                return 'run', {}, {'provider': 'codex'}
            async def delivery(*args, **kwargs):
                return True
            ctl._resolve_launch = launch
            monkeypatch.setattr(SpawnCtl, '_confirm_brief_delivery', delivery)
            server.spawnctl = ctl
            brief = tmp_path / 'brief-real'; brief.write_text('real service brief')
            await server.lane_rulings.configure(ADVISOR, actor_stream_id=ROOT,
                actor_generation=root['session_generation'], brief_file=str(brief))
            clock = [1000.]
            restore = AuthorityRestore(server, inhibit_path=None, clock=lambda: clock[0])
            server.authority_restore = restore
            await restore.initialize()
            await _set_seat(store, 'astra', pane_status='pane_dead')
            dead = await store.fetch_session('fixture-advisor', 'astra')
            await restore.observe(ADVISOR, advisor['session_generation'], 'pane_dead', dead)
            clock[0] += 61
            await restore.observe(ADVISOR, advisor['session_generation'], 'pane_dead', dead)
            if restore.task: await restore.task
            episode = await restore.current(ADVISOR, advisor['session_generation'])
            await restore.run()
            status = await ctl.spawn_status({'target': episode['spawn_key'], 'host': 'fixture-advisor'}, 'fixture-advisor')
            assert status['outcomes'][0]['state'] == 'delivered'
            host, name = status['outcomes'][0]['stream_id'].split(':', 1)
            row = await store.fetch_session(host, name)
            assert row['session_generation'] == episode['attempt_generation']
            assert (await restore.current(ADVISOR, advisor['session_generation']))['state'] == 'restored'
        finally: store.stop()
    asyncio.run(run())


def test_real_spawnctl_restart_adopts_delivered_started_seat(tmp_path):
    from types import SimpleNamespace
    from spawnctl import SpawnCtl
    from test_spawn_cancel_fence_holes import ReconTmux
    import tmux_transport
    async def run():
        store, root, advisor, server, fake, restore, now, brief = await restore_rig(tmp_path)
        try:
            episode = await restore.current(ADVISOR, advisor['session_generation'])
            payload = fake.calls[0]
            tmux = ReconTmux()
            name, nonce = 'adopted-seat', 'nonce-adopted-seat'
            tmux.live.add(name); tmux.nonces[name] = nonce
            server.sessions.tmux = tmux
            server.sessions.local_host = 'fixture-advisor'
            ctl = SpawnCtl(store, server.sessions, tmux=tmux)
            server.spawnctl = ctl
            assert await store.reserve_stream_id('fixture-advisor', name, ttl_s=600,
                request_id=episode['spawn_key'], idempotency_key=episode['spawn_key'], nonce=nonce,
                owner_instance_id='old-daemon', request_payload_hash=ctl._spawn_payload_hash(payload))
            fields = tmux_transport.open_fields({'objective': payload['objective'], 'provider': 'codex',
                'parent_stream_id': ROOT})
            fields['session_generation'] = episode['attempt_generation']
            assert await store.record_spawn_intent('fixture-advisor', name,
                {'open_fields': fields, 'brief': payload['initial_prompt'],
                 'delivery_receipt': {'state': 'indeterminate', 'transport': 'direct', 'proof_watermark_state': 'reachable'}},
                request_id=episode['spawn_key'], nonce=nonce)
            await store.mark_tmux_created('fixture-advisor', name, request_id=episode['spawn_key'])
            async def proof(stream_id, text, receipt, **kwargs):
                assert text == 'brief'
                return SimpleNamespace(proven=True)
            ctl._adoption_event_proof = proof
            assert (await ctl.reconcile_spawn_intents(recurring=False))['adopted'] == 1
            row = await store.fetch_session('fixture-advisor', name)
            assert row['session_generation'] == episode['attempt_generation'] and row['bootstrap_state'] == 'started'
            await restore.run()
            assert (await restore.current(ADVISOR, advisor['session_generation']))['state'] == 'restored'
            assert (await server.lane_rulings.binding())['stream_id'] == 'fixture-advisor:' + name
            assert tmux.created == 0
        finally: store.stop()
    asyncio.run(run())


def test_missing_shape_manual_restore_with_brief_bypasses_held_fd_spawn(tmp_path):
    from assistant_authority_restore import AuthorityRestore
    from test_assistant_lane_rulings import _set_seat, _spawn_msg
    async def run():
        store = Store(':memory:'); store.start()
        try:
            fake = RestoreSpawn()
            root, advisor, server, _ = await _rulings_rig(store, spawnctl=fake)
            server.local_host = 'fixture-advisor'; server.sessions.tmux = AlivePane()
            restore = AuthorityRestore(server, inhibit_path=None)
            server.authority_restore = restore; await restore.initialize()
            await _set_seat(store, 'astra', pane_status='pane_dead')
            held = await server._on_spawn(_spawn_msg(root, 'ordinary-held'))
            assert held['type'] == 'spawn.pending_ruling' and not fake.calls
            await server.lane_rulings._check_authority_loss()
            notices = await store.submit(lambda c: [dict(r) for r in c.execute(
                "SELECT body FROM v2_outbound_notices WHERE notice_id LIKE 'authority-restore:authority_restore_ineligible:%'")])
            assert len(notices) == 1 and '--restore --brief-file <path>' in notices[0]['body']
            brief = tmp_path / 'manual-brief'; brief.write_text('manual brief')
            await restore.manual('restore', brief_file=str(brief))
            if restore.task: await restore.task
            assert len(fake.calls) == 1 and fake.calls[0]['_auth_context']['service_authenticated']
            assert (await server.lane_rulings._fetch(held['ruling_request_id']))['state'] == 'pending'
            assert (await restore.shape())['generation'] == advisor['session_generation']
        finally: store.stop()
    asyncio.run(run())


def test_stale_shape_repaired_and_unavailable_row_refused(tmp_path):
    from assistant_authority_restore import AuthorityRestore
    from test_assistant_lane_rulings import _set_seat
    async def run():
        store = Store(':memory:'); store.start()
        try:
            root, advisor, server, _ = await _rulings_rig(store, spawnctl=RestoreSpawn())
            server.local_host = 'fixture-advisor'; server.sessions.tmux = AlivePane()
            brief = tmp_path / 'stale-brief'; brief.write_text('brief')
            await server.lane_rulings.configure(ADVISOR, actor_stream_id=ROOT,
                actor_generation=root['session_generation'], brief_file=str(brief))
            restore = AuthorityRestore(server, inhibit_path=None)
            server.authority_restore = restore; await restore.initialize()
            await store.submit(lambda c: (c.execute("UPDATE v2_session_generations SET generation='new-generation' WHERE host='fixture-advisor' AND session_name='astra'"), c.commit()))
            await _set_seat(store, 'astra', pane_status='pane_dead')
            row = await store.fetch_session('fixture-advisor', 'astra')
            with pytest.raises(ValueError, match='shape_generation_conflict'):
                await server.lane_rulings.record_dead_shape(ADVISOR, 'old-generation', row, str(brief))
            await restore.manual('restore', brief_file=str(brief))
            if restore.task: await restore.task
            assert (await restore.shape())['generation'] == 'new-generation'
            await store.submit(lambda c: (c.execute("DELETE FROM sessions WHERE host='fixture-advisor' AND session_name='astra'"), c.commit()))
            with pytest.raises(ValueError, match='shape_row_unavailable'):
                await restore.manual('restore', brief_file=str(brief))
        finally: store.stop()
    asyncio.run(run())


def test_inhibit_disabled_and_manual_set_supersede(tmp_path):
    async def run():
        store, root, advisor, server, fake, restore, now, brief = await restore_rig(tmp_path)
        try:
            episode = await restore.current(ADVISOR, advisor['session_generation'])
            restore.inhibit_path.write_text('hold')
            await restore.observe(ADVISOR, advisor['session_generation'], 'pane_dead', advisor)
            assert (await restore.status())['state'] == 'restoring'
            await store.open_session('fixture-advisor', 'replacement', provider='codex', pane_status='pane_alive')
            await server.lane_rulings.configure('fixture-advisor:replacement', actor_stream_id=ROOT,
                actor_generation=root['session_generation'], brief_file=str(brief))
            assert (await restore.current(ADVISOR, advisor['session_generation']))['state'] == 'superseded'
            assert (await restore.status())['state'] == 'healthy'
            restore.inhibit_path.unlink()
            restore.enabled = False
            assert (await restore.status())['state'] == 'disabled'
        finally:
            if restore.task: await restore.task
            store.stop()
    asyncio.run(run())


def test_unknown_row_uses_shape_generation_and_unbound_primary_holds(tmp_path):
    from assistant_authority_restore import AuthorityRestore
    async def run():
        store = Store(':memory:'); store.start()
        try:
            root, advisor, server, fake = await _rulings_rig(store, spawnctl=RestoreSpawn())
            server.local_host = 'fixture-advisor'; server.sessions.tmux = AlivePane()
            brief = tmp_path / 'unknown-brief'; brief.write_text('brief')
            await server.lane_rulings.configure(ADVISOR, actor_stream_id=ROOT,
                actor_generation=root['session_generation'], brief_file=str(brief))
            now = [0.]
            restore = AuthorityRestore(server, inhibit_path=None, clock=lambda: now[0])
            server.authority_restore = restore; await restore.initialize()
            await store.submit(lambda c: (c.execute("DELETE FROM sessions WHERE host='fixture-advisor' AND session_name='astra'"), c.commit()))
            original = restore.primary
            async def unbound(): return None
            restore.primary = unbound
            await server.lane_rulings._check_authority_loss()
            now[0] = 61
            await server.lane_rulings._check_authority_loss()
            assert not await restore.current(ADVISOR, advisor['session_generation'])
            notices = await store.submit(lambda c: c.execute("SELECT count(*) FROM v2_outbound_notices").fetchone()[0])
            assert notices == 0
            restore.primary = original
            await server.lane_rulings._check_authority_loss()
            if restore.task: await restore.task
            episode = await restore.current(ADVISOR, advisor['session_generation'])
            assert episode and episode['generation'] == advisor['session_generation'] and len(fake.calls) == 1
        finally: store.stop()
    asyncio.run(run())


def test_disabled_and_marker_do_not_start_auto_episodes(tmp_path):
    from assistant_authority_restore import AuthorityRestore
    from test_assistant_lane_rulings import _set_seat
    async def run():
        store = Store(':memory:'); store.start()
        try:
            root, advisor, server, fake = await _rulings_rig(store, spawnctl=RestoreSpawn())
            server.local_host = 'fixture-advisor'; server.sessions.tmux = AlivePane()
            brief = tmp_path / 'disable-brief'; brief.write_text('brief')
            await server.lane_rulings.configure(ADVISOR, actor_stream_id=ROOT,
                actor_generation=root['session_generation'], brief_file=str(brief))
            now = [0.]; marker = tmp_path / 'marker'; marker.write_text('hold')
            restore = AuthorityRestore(server, inhibit_path=marker, clock=lambda: now[0])
            server.authority_restore = restore; await restore.initialize()
            await _set_seat(store, 'astra', pane_status='pane_dead')
            await server.lane_rulings._check_authority_loss(); now[0] = 61
            await server.lane_rulings._check_authority_loss()
            assert (await restore.status())['state'] == 'inhibited'
            assert not await restore.current(ADVISOR, advisor['session_generation']) and not fake.calls
            count = await store.submit(lambda c: c.execute("SELECT count(*) FROM v2_outbound_notices WHERE notice_id LIKE 'authority-restore:authority_restore_inhibited:%'").fetchone()[0])
            assert count == 1
            marker.unlink(); restore.enabled = False
            await server.lane_rulings._check_authority_loss()
            assert (await restore.status())['state'] == 'disabled' and not fake.calls
        finally: store.stop()
    asyncio.run(run())


def test_rate_limit_fences_seventh_spawn(tmp_path):
    async def run():
        store, root, advisor, server, fake, restore, now, brief = await restore_rig(tmp_path)
        try:
            episode = await restore.current(ADVISOR, advisor['session_generation'])
            fake.outcomes = [{'idempotency_key': episode['spawn_key'], 'state': 'failed'}]
            await restore.run()
            now[0] += 60
            await store.submit(lambda c: (c.executemany(
                "INSERT INTO v2_authority_restore_audit(episode_id,attempt_seq,event,detail,created_at) VALUES(?,?,'attempt_started','{}',?)",
                [(episode['episode_id'], n, now[0]) for n in range(5)]), c.commit()))
            fake.outcomes = []
            await restore.run()
            assert len(fake.calls) == 1
            assert await store.submit(lambda c: c.execute("SELECT count(*) FROM v2_outbound_notices WHERE notice_id LIKE 'authority-restore:authority_restore_rate_limited:%'").fetchone()[0]) == 1
        finally: store.stop()
    asyncio.run(run())


def test_delivered_restoration_retargets_pending_and_notifies_once(tmp_path):
    from assistant_authority_restore import AuthorityRestore
    from test_assistant_lane_rulings import _set_seat, _spawn_msg
    async def run():
        store = Store(':memory:'); store.start()
        try:
            fake = RestoreSpawn()
            root, advisor, server, _ = await _rulings_rig(store, spawnctl=fake)
            server.local_host = 'fixture-advisor'; server.sessions.tmux = AlivePane()
            brief = tmp_path / 'retarget-brief'; brief.write_text('brief')
            await server.lane_rulings.configure(ADVISOR, actor_stream_id=ROOT,
                actor_generation=root['session_generation'], brief_file=str(brief))
            held = await server._on_spawn(_spawn_msg(root, 'held-before-loss'))
            await _set_seat(store, 'astra', pane_status='pane_dead')
            now = [0.]; restore = AuthorityRestore(server, inhibit_path=None, clock=lambda: now[0])
            server.authority_restore = restore; await restore.initialize()
            await server.lane_rulings._check_authority_loss(); now[0] = 61
            await server.lane_rulings._check_authority_loss()
            if restore.task: await restore.task
            loss = await store.submit(lambda c: c.execute("SELECT body FROM v2_outbound_notices WHERE notice_id LIKE 'assistant-authority-lost:%'").fetchone()[0])
            assert json.loads(loss.split('] ', 1)[1])['restore']['state'] == 'restoring'
            episode = await restore.current(ADVISOR, advisor['session_generation'])
            await store.open_session('fixture-advisor', 'successor', provider='codex', pane_status='pane_alive',
                bootstrap_state='started', session_generation=episode['attempt_generation'],
                account_binding={'alias': 'default-account', 'provider': 'codex', 'host': 'fixture-advisor'})
            fake.outcomes = [{'idempotency_key': episode['spawn_key'], 'stream_id': 'fixture-advisor:successor',
                'state': 'delivered', 'request_payload_hash': fake._spawn_payload_hash(fake.calls[0])}]
            await restore.run(); await restore.run()
            request = await server.lane_rulings._fetch(held['ruling_request_id'])
            assert request['authority_stream_id'] == 'fixture-advisor:successor'
            assert request['authority_generation'] == episode['attempt_generation'] and request['state'] == 'pending'
            packet_id = f"assistant-lane-ruling:{held['ruling_request_id']}:{episode['attempt_generation']}"
            assert await store.submit(lambda c: c.execute('SELECT 1 FROM v2_outbound_notices WHERE notice_id=?', (packet_id,)).fetchone())
            assert await store.submit(lambda c: c.execute("SELECT count(*) FROM v2_outbound_notices WHERE notice_id LIKE 'authority-restore:authority_restored:%'").fetchone()[0]) == 1
            assert (await restore.shape())['stream_id'] == 'fixture-advisor:successor'
            audit = await store.submit(lambda c: c.execute("SELECT detail FROM v2_authority_restore_audit WHERE event='adopted'").fetchone()[0])
            assert json.loads(audit)['account_binding']['alias'] == 'default-account'
            assert await store.submit(lambda c: c.execute('SELECT count(*) FROM v2_assistant_lane_rulings').fetchone()[0]) == 1
        finally: store.stop()
    asyncio.run(run())


def test_shape_maps_effective_tuple_and_observed_account(tmp_path):
    async def run():
        store = Store(':memory:'); store.start()
        try:
            root, advisor, server, _ = await _rulings_rig(store)
            await store.submit(lambda c: (c.execute("UPDATE sessions SET requested_model=?,effective_model=?,requested_effort=?,effective_effort=?,account_binding=? WHERE host='fixture-advisor' AND session_name='astra'", ('old','new','low','high',json.dumps({'alias':'ours','provider':'codex','host':'fixture-advisor','home':'private'}))), c.commit()))
            brief = tmp_path / 'shape-brief'; brief.write_text('tuple')
            await server.lane_rulings.configure(ADVISOR, actor_stream_id=ROOT,
                actor_generation=root['session_generation'], brief_file=str(brief))
            shape = json.loads(await store.get('assistant.authority.shape'))
            assert (shape['model'], shape['effort']) == ('new', 'high')
            assert shape['account_observed'] == {'alias':'ours', 'provider':'codex', 'host':'fixture-advisor'}
        finally: store.stop()
    asyncio.run(run())


def test_delivered_dead_holder_fails_and_never_binds(tmp_path):
    async def run():
        store, root, advisor, server, fake, restore, now, brief = await restore_rig(tmp_path)
        try:
            episode = await restore.current(ADVISOR, advisor['session_generation'])
            await store.open_session('fixture-advisor', 'died', provider='codex',
                session_generation=episode['attempt_generation'], pane_status='pane_dead')
            fake.outcomes = [{'idempotency_key': episode['spawn_key'], 'stream_id': 'fixture-advisor:died',
                'state': 'delivered', 'request_payload_hash': fake._spawn_payload_hash(fake.calls[0])}]
            await restore.run()
            episode = await restore.current(ADVISOR, advisor['session_generation'])
            assert episode['state'] == 'pending' and episode['last_error'] == 'holder_lost'
            assert (await server.lane_rulings.binding())['stream_id'] == ADVISOR
        finally: store.stop()
    asyncio.run(run())


def test_missing_brief_consumes_bounded_attempts_without_spawns(tmp_path):
    async def run():
        store, root, advisor, server, fake, restore, now, brief = await restore_rig(tmp_path)
        try:
            episode = await restore.current(ADVISOR, advisor['session_generation'])
            fake.outcomes = [{'idempotency_key': episode['spawn_key'], 'state': 'failed'}]
            await restore.run(); fake.outcomes = []
            brief.unlink(); now[0] += 60
            await restore.run(); now[0] += 180
            await restore.run()
            episode = await restore.current(ADVISOR, advisor['session_generation'])
            assert episode['state'] == 'exhausted' and episode['last_error'] == 'brief_unreadable'
            assert len(fake.calls) == 1
        finally: store.stop()
    asyncio.run(run())


def test_authority_set_requires_verified_primary_and_brief(tmp_path):
    from sessions import VerbError
    async def run():
        store = Store(':memory:'); store.start()
        try:
            root, advisor, server, _ = await _rulings_rig(store)
            auth = {'token_verified': True, 'stream_id': ROOT, 'session_generation': root['session_generation']}
            with pytest.raises(VerbError) as error:
                await server._on_assistant_authority({'action': 'set', 'value': ADVISOR, '_auth_context': auth})
            assert error.value.code == 'assistant_authority_brief_required' and '--brief-file' in str(error.value)
            brief = tmp_path / 'auth-brief'; brief.write_text('brief')
            with pytest.raises(VerbError, match='assistant_authority_config_unauthorized'):
                await server._on_assistant_authority({'action': 'set', 'value': ADVISOR, 'brief_file': str(brief),
                    '_auth_context': {**auth, 'stream_id': ADVISOR}})
        finally: store.stop()
    asyncio.run(run())


def test_original_holder_recovery_supersedes_outstanding_attempt(tmp_path):
    from test_assistant_lane_rulings import _set_seat
    async def run():
        store, root, advisor, server, fake, restore, now, brief = await restore_rig(tmp_path)
        try:
            await _set_seat(store, 'astra', pane_status='pane_alive')
            await server.lane_rulings._check_authority_loss()
            episode = await restore.current(ADVISOR, advisor['session_generation'])
            assert episode['state'] == 'superseded' and episode['last_error'] == 'holder_revived'
            assert (await restore.status())['state'] == 'healthy'
            assert len(fake.calls) == 1
        finally: store.stop()
    asyncio.run(run())


def test_disable_supersedes_active_episode(tmp_path):
    async def run():
        store, root, advisor, server, fake, restore, now, brief = await restore_rig(tmp_path)
        try:
            await server.lane_rulings.configure('disabled', actor_stream_id=ROOT,
                                                 actor_generation=root['session_generation'])
            assert (await restore.current(ADVISOR, advisor['session_generation']))['state'] == 'superseded'
            assert (await restore.status())['state'] == 'disabled'
        finally: store.stop()
    asyncio.run(run())


def test_generation_mismatch_is_ineligible_with_service_contingency(tmp_path):
    from assistant_authority_restore import AuthorityRestore
    from test_assistant_lane_rulings import _set_seat
    async def run():
        store = Store(':memory:'); store.start()
        try:
            root, advisor, server, fake = await _rulings_rig(store, spawnctl=RestoreSpawn())
            server.local_host = 'fixture-advisor'; server.sessions.tmux = AlivePane()
            brief = tmp_path / 'mismatch-brief'; brief.write_text('brief')
            await server.lane_rulings.configure(ADVISOR, actor_stream_id=ROOT,
                actor_generation=root['session_generation'], brief_file=str(brief))
            restore = AuthorityRestore(server, inhibit_path=None); server.authority_restore = restore
            await restore.initialize()
            await store.submit(lambda c: (c.execute("UPDATE v2_session_generations SET generation='different' WHERE host='fixture-advisor' AND session_name='astra'"), c.commit()))
            await _set_seat(store, 'astra', pane_status='pane_dead')
            await server.lane_rulings._check_authority_loss()
            result = await restore.status()
            assert result['state'] == 'ineligible' and not fake.calls
            body = await store.submit(lambda c: c.execute("SELECT body FROM v2_outbound_notices WHERE notice_id LIKE 'authority-restore:authority_restore_ineligible:%'").fetchone()[0])
            event = json.loads(body.split('] ', 1)[1])
            assert event['reason'] == 'shape_generation_mismatch' and '--restore --brief-file' in event['contingency']
        finally: store.stop()
    asyncio.run(run())


def test_no_row_contingency_and_non_fd_successor_with_marker(tmp_path):
    from assistant_authority_restore import AuthorityRestore
    from test_assistant_lane_rulings import _spawn_msg
    async def run():
        store = Store(':memory:'); store.start()
        try:
            fake = RestoreSpawn()
            root, advisor, server, _ = await _rulings_rig(store, spawnctl=fake)
            server.local_host = 'fixture-advisor'; server.sessions.tmux = AlivePane()
            marker = tmp_path / 'marker'
            restore = AuthorityRestore(server, inhibit_path=marker); server.authority_restore = restore
            await restore.initialize()
            await store.submit(lambda c: (c.execute("DELETE FROM sessions WHERE host='fixture-advisor' AND session_name='astra'"), c.commit()))
            await server.lane_rulings._check_authority_loss()
            body = await store.submit(lambda c: c.execute("SELECT body FROM v2_outbound_notices WHERE notice_id LIKE 'authority-restore:authority_restore_ineligible:%'").fetchone()[0])
            contingency = json.loads(body.split('] ', 1)[1])['contingency']
            assert 'non-FD owner' in contingency and '--set <live successor>' in contingency
            marker.write_text('hold')
            owner = await store.open_session('fixture-advisor', 'owner', provider='codex', pane_status='pane_alive')
            msg = _spawn_msg(root, 'non-fd-successor')
            msg['_auth_context'] = {'token_verified': True, 'stream_id': 'fixture-advisor:owner',
                                    'session_generation': owner['session_generation']}
            assert (await server._on_spawn(msg))['type'] == 'spawn.ok'
            assert len(fake.calls) == 1
            await store.open_session('fixture-advisor', 'successor', provider='codex', pane_status='pane_alive')
            brief = tmp_path / 'successor-brief'; brief.write_text('brief')
            await server.lane_rulings.configure('fixture-advisor:successor', actor_stream_id=ROOT,
                actor_generation=root['session_generation'], brief_file=str(brief))
            assert (await server.lane_rulings.binding())['state'] == 'ready' and marker.exists()
            marker.unlink()
            assert (await restore.status())['state'] == 'healthy'
        finally: store.stop()
    asyncio.run(run())


def test_payload_mismatch_cannot_bind_and_becomes_unresolved(tmp_path):
    async def run():
        store, root, advisor, server, fake, restore, now, brief = await restore_rig(tmp_path)
        try:
            episode = await restore.current(ADVISOR, advisor['session_generation'])
            await store.open_session('fixture-advisor', 'wrong', provider='codex', pane_status='pane_alive',
                bootstrap_state='ready', session_generation=episode['attempt_generation'])
            fake.outcomes = [{'idempotency_key': episode['spawn_key'], 'state': 'delivered',
                              'stream_id': 'fixture-advisor:wrong', 'request_payload_hash': 'wrong'}]
            now[0] += 181
            await restore.run()
            assert (await server.lane_rulings.binding())['stream_id'] == ADVISOR
            assert (await restore.current(ADVISOR, advisor['session_generation']))['state'] == 'unresolved'
        finally: store.stop()
    asyncio.run(run())


def test_authority_read_and_fd_inspect_expose_restore_state(tmp_path):
    async def run():
        store, root, advisor, server, fake, restore, now, brief = await restore_rig(tmp_path)
        try:
            auth = {'token_verified': True, 'stream_id': ROOT, 'session_generation': root['session_generation']}
            read = await server._on_assistant_authority({'action': 'read', '_auth_context': auth})
            inspect = await server._on_inspect_stream({'stream_id': ROOT, 'event_tail': 0})
            assert read['shape']['stream_id'] == ADVISOR
            assert read['restore']['state'] == inspect['restore']['state'] == 'restoring'
            assert read['restore']['episode_id'] == inspect['restore']['episode_id']
        finally: store.stop()
    asyncio.run(run())


def test_recovered_holder_can_open_new_episode_for_later_loss(tmp_path):
    from test_assistant_lane_rulings import _set_seat
    async def run():
        store, root, advisor, server, fake, restore, now, brief = await restore_rig(tmp_path)
        try:
            first = await restore.current(ADVISOR, advisor['session_generation'])
            await _set_seat(store, 'astra', pane_status='pane_alive')
            await server.lane_rulings._check_authority_loss()
            await _set_seat(store, 'astra', pane_status='pane_dead')
            await server.lane_rulings._check_authority_loss()
            now[0] += 61
            await server.lane_rulings._check_authority_loss()
            if restore.task: await restore.task
            second = await restore.current(ADVISOR, advisor['session_generation'])
            assert second['episode_id'] != first['episode_id']
            assert second['state'] == 'spawning' and len(fake.calls) == 2
        finally: store.stop()
    asyncio.run(run())


def test_internal_bind_fences_changed_primary(tmp_path):
    from assistant_authority_restore import SERVICE_CAPABILITY
    async def run():
        store, root, advisor, server, fake, restore, now, brief = await restore_rig(tmp_path)
        try:
            episode = await restore.current(ADVISOR, advisor['session_generation'])
            await store.open_session('fixture-advisor', 'would-bind', provider='codex', pane_status='pane_alive',
                bootstrap_state='ready', session_generation=episode['attempt_generation'])
            with pytest.raises(ValueError, match='assistant_authority_primary_changed'):
                await server.lane_rulings.configure('fixture-advisor:would-bind', actor_stream_id='daemon:authority-restore',
                    actor_generation=episode['attempt_generation'], service=SERVICE_CAPABILITY, brief_file=str(brief),
                    expected_old=(ADVISOR, advisor['session_generation']), frozen_brief_sha='known',
                    restore_episode=episode, expected_primary=(ROOT, 'stale-generation'))
            assert (await server.lane_rulings.binding())['stream_id'] == ADVISOR
            assert (await restore.current(ADVISOR, advisor['session_generation']))['state'] == 'spawning'
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('hold', ['inhibit', 'disabled'])
def test_authenticated_manual_restore_promotes_pending_auto_episode(tmp_path, hold):
    async def run():
        store, root, advisor, server, fake, restore, now, brief = await restore_rig(tmp_path)
        try:
            first = await restore.current(ADVISOR, advisor['session_generation'])
            fake.outcomes = [{'idempotency_key': first['spawn_key'], 'state': 'failed'}]
            await restore.run()
            fake.outcomes = []
            pending = await restore.current(ADVISOR, advisor['session_generation'])
            assert pending['state'] == 'pending' and pending['trigger'] == 'auto'
            if hold == 'inhibit':
                restore.inhibit_path.write_text('hold')
            else:
                restore.enabled = False
            now[0] += 61
            auth = {'token_verified': True, 'stream_id': ROOT, 'session_generation': root['session_generation']}
            reply = await server._on_assistant_authority({'action': 'restore', '_auth_context': auth})
            assert reply['type'] == 'assistant.authority.ok'
            if restore.task: await restore.task
            current = await restore.current(ADVISOR, advisor['session_generation'])
            assert current['episode_id'] == pending['episode_id'] and current['trigger'] == 'manual'
            assert current['attempt_seq'] == pending['attempt_seq'] + 1
            assert current['budget_epoch'] == pending['budget_epoch'] and current['budget_used'] == pending['budget_used'] + 1
            assert len(fake.calls) == 2
        finally:
            if restore.task: await restore.task
            store.stop()
    asyncio.run(run())


def test_present_dead_row_without_generation_never_uses_shape_fallback(tmp_path):
    from assistant_authority_restore import AuthorityRestore
    from test_assistant_lane_rulings import _set_seat
    async def run():
        store = Store(':memory:'); store.start()
        try:
            root, advisor, server, fake = await _rulings_rig(store, spawnctl=RestoreSpawn())
            server.local_host = 'fixture-advisor'; server.sessions.tmux = AlivePane()
            brief = tmp_path / 'present-row'; brief.write_text('brief')
            await server.lane_rulings.configure(ADVISOR, actor_stream_id=ROOT,
                actor_generation=root['session_generation'], brief_file=str(brief))
            now = [1000.]; restore = AuthorityRestore(server, inhibit_path=None, clock=lambda: now[0])
            server.authority_restore = restore; await restore.initialize()
            await _set_seat(store, 'astra', pane_status='pane_dead')
            await store.submit(lambda c: (c.execute("DELETE FROM v2_session_generations WHERE host='fixture-advisor' AND session_name='astra'"), c.commit()))
            await server.lane_rulings._check_authority_loss(); now[0] += 61
            await server.lane_rulings._check_authority_loss()
            if restore.task: await restore.task
            assert not fake.calls and (await restore.status())['state'] == 'ineligible'
            body = await store.submit(lambda c: c.execute("SELECT body FROM v2_outbound_notices WHERE notice_id LIKE 'authority-restore:authority_restore_ineligible:%'").fetchone()[0])
            assert json.loads(body.split('] ', 1)[1])['reason'] == 'shape_generation_mismatch'
        finally: store.stop()
    asyncio.run(run())
