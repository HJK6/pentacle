"""The configured report-producer principal: hello + one constrained report publish only."""
from __future__ import annotations

import asyncio
import json

import pytest

from assets import Assets
from server import Server
from test_system_notification_producer import Peer, SocketPeer

BRIEF = 'examplehost:daily-report'
SPEC = 'spec_example__daily_reports'
TOKEN = 'synthetic-report-producer-token'
BACKUP_TOKEN = 'synthetic-wmi-backup-token'
CD_TOKEN = 'synthetic-independent-cd-token'
CUTOFF = '20261007'
PATTERN = r'^daily-report-([0-9]{8})(?:-r[0-9]+)?$'


def report_body(text='Synthetic knowledge line.'):
    return json.dumps({
        'schema_version': 1,
        'title': f'Daily report {CUTOFF}',
        'sections': [{'id': 'knowledge', 'title': 'Knowledge gained today', 'status': 'reference',
                      'blocks': [{'type': 'para', 'id': 'k1', 'runs': [{'type': 'text', 'text': text}]}]}],
    }, sort_keys=True, separators=(',', ':'))


@pytest.fixture
def credentials(monkeypatch, tmp_path):
    files = {}
    for name, env, value in (
        ('brief', None, TOKEN),
        ('backup', 'PENTACLE_WMI_BACKUP_STREAM_TOKEN_FILE', BACKUP_TOKEN),
        ('cd', 'PENTACLE_SYSTEM_PRODUCER_STREAM_TOKEN_FILE', CD_TOKEN),
    ):
        path = tmp_path / f'{name}-token'
        path.write_text(value)
        path.chmod(0o600)
        if env:
            monkeypatch.setenv(env, str(path))
        files[name] = path
    files['config'] = write_config(tmp_path, token_file=str(files['brief']))
    monkeypatch.setenv('PENTACLE_REPORT_PRODUCER_CONFIG', str(files['config']))
    monkeypatch.setenv('PENTACLE_SYSTEM_PRODUCER_STREAM_ID', 'altum-bot-cd')
    return files


def config(**overrides):
    return {'stream_id': BRIEF, 'token_file': '/unset', 'spec_id': SPEC, 'asset_id_pattern': PATTERN,
            'cutoff_format': '%Y%m%d', 'title_template': 'Daily report {cutoff}', 'tag': 'daily-report',
            'body_max_bytes': 64 * 1024, **overrides}


def write_config(tmp_path, mode=0o600, **overrides):
    path = tmp_path / 'report-producer.json'
    path.write_text(json.dumps(config(**overrides)))
    path.chmod(mode)
    return path


def frame(kind='asset.publish', **extra):
    payload = dict(type=kind, request_id='synthetic-brief-request', from_stream_id=BRIEF, stream_token=TOKEN)
    if kind == 'hello':
        payload['subscribe'] = {'snapshot': False, 'mode': 'rpc'}
    elif kind == 'asset.publish':
        # The exact shape `agent-orch asset publish` sends for this principal.
        payload.update(stream_id=BRIEF, producer=BRIEF, title=f'Daily report {CUTOFF}', content_type='report',
                       body=report_body(), tags=['daily-report'], asset_id=f'daily-report-{CUTOFF}', spec_id=SPEC)
    return {**payload, **extra}


async def dispatch(server, peer, payload):
    return (await server._dispatch(json.dumps(payload), websocket=peer))[0]


async def authenticate(server, peer):
    assert (await dispatch(server, peer, frame('hello')))['type'] == 'ready'


class Harness:
    def __init__(self, db):
        self.broadcasts = []
        self.server = Server()
        self.assets = Assets(db, broadcast=self._broadcast)

    async def _broadcast(self, msg):
        self.broadcasts.append(msg)

    async def __aenter__(self):
        await self.assets.start()
        self.server.handlers.update(self.assets.wire_handlers())
        return self

    async def __aexit__(self, *exc):
        await self.assets.stop()

    async def rows(self):
        return await self.assets._call('list_by_spec_id', spec_id=SPEC)


def test_publish_lists_under_spec_with_fixed_anchor_and_producer(credentials, tmp_path):
    async def run():
        async with Harness(str(tmp_path / 'assets.db')) as h:
            peer = Peer()
            await authenticate(h.server, peer)
            reply = await dispatch(h.server, peer, frame())
            assert reply['type'] == 'asset.publish.ok', reply
            assert 'unchanged' not in reply
            listed = await h.assets.asset({'type': 'asset.list', 'request_id': 'l', 'spec_id': SPEC})
            assert [a['asset_id'] for a in listed['assets']] == [f'daily-report-{CUTOFF}']
            assert listed['assets'][0]['producer'] == BRIEF
            row, = await h.rows()
            assert (row['host'], row['session_name'], row['stream_id']) == ('examplehost', 'daily-report', BRIEF)
            assert len(h.broadcasts) == 1
    asyncio.run(run())


def test_identical_retry_is_noop_and_changed_day_is_refused(credentials, tmp_path):
    async def run():
        async with Harness(str(tmp_path / 'assets.db')) as h:
            peer = Peer()
            await authenticate(h.server, peer)
            assert (await dispatch(h.server, peer, frame()))['type'] == 'asset.publish.ok'
            before, = await h.rows()
            h.broadcasts.clear()
            retry = await dispatch(h.server, peer, frame(request_id='synthetic-retry'))
            assert retry['type'] == 'asset.publish.ok' and retry['unchanged'] is True
            changed = await dispatch(h.server, peer, frame(body=report_body('Different text.')))
            assert changed['error_code'] == 'report_producer_immutable'
            after, = await h.rows()
            assert after == before
            assert h.broadcasts == []
            # A revision is a new asset id, accepted whether or not its base exists.
            revised = await dispatch(h.server, peer, frame(asset_id='daily-report-20261008-r2',
                                                           title='Daily report 20261008'))
            assert revised['type'] == 'asset.publish.ok'
            assert len(await h.rows()) == 2
    asyncio.run(run())


def test_different_title_for_existing_id_is_immutable(credentials, tmp_path):
    async def run():
        async with Harness(str(tmp_path / 'assets.db')) as h:
            auth = {'service_authenticated': True, 'service_actor': BRIEF}
            first = frame(_auth_context=auth)
            assert (await h.assets.asset(first))['type'] == 'asset.publish.ok'
            before = await h.rows()
            reply = await h.assets.asset({**first, 'title': 'Daily report other'})
            assert reply['error_code'] == 'report_producer_immutable'
            assert await h.rows() == before
    asyncio.run(run())


def test_other_callers_cannot_rewrite_a_brief_row(credentials, tmp_path):
    async def run():
        async with Harness(str(tmp_path / 'assets.db')) as h:
            assert (await h.assets.asset(frame(_auth_context={
                'service_authenticated': True, 'service_actor': BRIEF})))['type'] == 'asset.publish.ok'
            before = await h.rows()
            h.broadcasts.clear()
            seat = frame(from_stream_id='host-a:v2-seat', stream_id='host-a:v2-seat',
                         producer='host-a:v2-seat', body=report_body('Spoofed.'))
            seat.pop('stream_token')
            reply = await h.assets.asset({**seat, '_auth_context': {'token_verified': True,
                                                                     'stream_id': 'host-a:v2-seat'}})
            assert reply['error_code'] == 'asset_unauthorized'
            assert await h.rows() == before and h.broadcasts == []
    asyncio.run(run())


@pytest.mark.parametrize('patch', [
    {'content_type': 'markdown'}, {'spec_id': 'spec_other'}, {'spec_id': None},
    {'asset_id': 'daily-report-2026100'}, {'asset_id': 'daily-report-20261302'},
    {'asset_id': 'other-asset'}, {'asset_id': 'daily-report-20261007-rx'}, {'asset_id': None},
    {'producer': 'altum-bot-cd'}, {'producer': 'amaterasu:wmi-pg-dailybackup'},
    {'stream_id': 'host-a:v2-other'}, {'host': 'host-b'}, {'session_name': 'other'},
    {'tags': ['daily-report', 'extra']}, {'tags': []}, {'title': 'Anything else'},
    {'title': 'Daily report 20261008'}, {'body': ''}, {'body': None}, {'stream_id': None},
    {'body': 'x' * (64 * 1024 + 1)}, {'request_id': 'x' * 121}, {'review_status': 'approved'},
])
def test_payload_refused_with_zero_side_effects(credentials, tmp_path, patch):
    async def run():
        async with Harness(str(tmp_path / 'assets.db')) as h:
            peer = Peer()
            await authenticate(h.server, peer)
            payload = frame(**patch)
            if patch == {'stream_id': None}:
                payload.pop('stream_id')          # omitted, not merely null
            reply = await dispatch(h.server, peer, payload)
            assert reply['error_code'] == 'system_producer_payload_invalid', reply
            assert await h.rows() == [] and h.broadcasts == []
    asyncio.run(run())


def test_schema_invalid_body_refused_with_zero_side_effects(credentials, tmp_path):
    async def run():
        async with Harness(str(tmp_path / 'assets.db')) as h:
            peer = Peer()
            await authenticate(h.server, peer)
            reply = await dispatch(h.server, peer, frame(body=json.dumps({'schema_version': 1, 'unknown': 1})))
            assert reply['error_code'] == 'asset_invalid'
            assert await h.rows() == [] and h.broadcasts == []
    asyncio.run(run())


def test_forbidden_verbs_never_reach_a_handler(credentials):
    async def run():
        server = Server()
        peer = Peer()
        await authenticate(server, peer)
        calls = []
        for verb in ('asset.get', 'asset.list', 'asset.delete', 'asset.comment.add', 'asset.review.set',
                     'notification.create', 'tell', 'spawn', 'prompt.ask', 'grant_token', 'list_sessions'):
            async def handler(msg): calls.append(msg); return {'type': 'unexpected'}
            server.handlers[verb] = handler
            assert (await dispatch(server, peer, frame(verb)))['error_code'] == 'system_producer_forbidden'
        assert calls == []
    asyncio.run(run())


def test_backup_principal_still_cannot_publish(credentials):
    async def run():
        server = Server()
        calls = []
        async def handler(msg): calls.append(msg); return {'type': 'unexpected'}
        server.handlers['asset.publish'] = handler
        peer = Peer()
        backup = 'amaterasu:wmi-pg-dailybackup'
        assert (await dispatch(server, peer, frame('hello', from_stream_id=backup,
                                                   stream_token=BACKUP_TOKEN)))['type'] == 'ready'
        reply = await dispatch(server, peer, frame(from_stream_id=backup, stream_token=BACKUP_TOKEN,
                                                   producer=backup, stream_id=backup))
        assert reply['error_code'] == 'system_producer_forbidden' and calls == []
    asyncio.run(run())


@pytest.mark.parametrize('address', ['192.0.2.10', '127.0.0.1'])
@pytest.mark.parametrize('patch', [
    {'from_stream_id': 'amaterasu:wmi-pg-dailybackup'}, {'from_stream_id': 'unrelated'},
    {'stream_token': CD_TOKEN}, {'stream_token': BACKUP_TOKEN}, {'stream_token': ''},
    {'subscribe': {'snapshot': True, 'mode': 'rpc'}}, {'subscribe': {'snapshot': False, 'mode': 'full'}},
])
def test_wrong_identity_token_or_hello_fails_closed(credentials, address, patch):
    async def run():
        server = Server()
        peer = Peer(address)
        assert (await dispatch(server, peer, frame('hello', **patch)))['error_code'] == 'system_producer_auth_required'
        assert peer not in server._client_system_producers
    asyncio.run(run())


@pytest.mark.parametrize('state', ['unset', 'removed', 'empty', 'insecure', 'shared_cd', 'shared_backup'])
def test_absent_unsafe_or_shared_token_file_refuses_publish(credentials, monkeypatch, tmp_path, state):
    brief = credentials['brief']
    if state == 'unset': monkeypatch.delenv('PENTACLE_REPORT_PRODUCER_CONFIG')
    elif state == 'removed': brief.unlink()
    elif state == 'empty': brief.write_text('')
    elif state == 'insecure': brief.chmod(0o644)
    elif state == 'shared_cd': brief.write_text(CD_TOKEN)
    else: brief.write_text(BACKUP_TOKEN)
    token = {'shared_cd': CD_TOKEN, 'shared_backup': BACKUP_TOKEN}.get(state, TOKEN)
    async def run():
        async with Harness(str(tmp_path / 'assets.db')) as h:
            # Unset config = no principal at all: the claim is an ordinary unknown
            # identity (remote fails closed; loopback keeps its pre-existing local trust).
            addresses = ('192.0.2.10',) if state == 'unset' else ('192.0.2.10', '127.0.0.1')
            expected = {'authentication_required'} if state == 'unset' else {'system_producer_auth_required'}
            for address in addresses:
                peer = Peer(address)
                hello = await dispatch(h.server, peer, frame('hello', stream_token=token))
                assert hello['error_code'] in expected
                reply = await dispatch(h.server, peer, frame(stream_token=token))
                assert reply['error_code'] in expected
            assert await h.rows() == [] and h.broadcasts == []
    asyncio.run(run())


@pytest.mark.parametrize('mutation', ['remove', 'rotate'])
def test_revoked_file_invalidates_bound_and_fresh_connections(credentials, tmp_path, mutation):
    async def run():
        async with Harness(str(tmp_path / 'assets.db')) as h:
            peer = Peer()
            await authenticate(h.server, peer)
            if mutation == 'remove': credentials['brief'].unlink()
            else: credentials['brief'].write_text('synthetic-rotated-token')
            assert (await dispatch(h.server, peer, frame()))['error_code'] == 'system_producer_auth_required'
            tokenless = frame(); tokenless.pop('stream_token')
            assert (await dispatch(h.server, peer, tokenless))['error_code'] == 'system_producer_auth_required'
            assert (await dispatch(h.server, Peer(), frame('hello')))['error_code'] == 'system_producer_auth_required'
            assert await h.rows() == []
            if mutation == 'rotate':
                fresh = Peer()
                assert (await dispatch(h.server, fresh, frame('hello', stream_token='synthetic-rotated-token')))['type'] == 'ready'
                assert (await dispatch(h.server, fresh, frame(stream_token='synthetic-rotated-token')))['type'] == 'asset.publish.ok'
    asyncio.run(run())


def test_publish_survives_restart_as_noop(credentials, tmp_path):
    async def run():
        db = str(tmp_path / 'assets.db')
        async with Harness(db) as h:
            peer = SocketPeer('192.0.2.10')
            await authenticate(h.server, peer)
            assert (await dispatch(h.server, peer, frame()))['type'] == 'asset.publish.ok'
            before = await h.rows()
        async with Harness(db) as h:
            peer = Peer()
            await authenticate(h.server, peer)
            retry = await dispatch(h.server, peer, frame(request_id='after-restart'))
            assert retry['unchanged'] is True and await h.rows() == before
    asyncio.run(run())


def test_unregistered_verb_is_forbidden_not_unsupported(credentials):
    async def run():
        server = Server()
        peer = Peer()
        await authenticate(server, peer)
        for verb in ('asset.nonexistent', 'definitely.not.a.verb'):
            assert verb not in server.handlers
            assert (await dispatch(server, peer, frame(verb)))['error_code'] == 'system_producer_forbidden'
        # An unauthenticated socket still gets the ordinary unsupported reply.
        other = await dispatch(server, Peer(), {'type': 'definitely.not.a.verb', 'request_id': 'x'})
        assert other.get('error_code') != 'system_producer_forbidden'
    asyncio.run(run())


def test_malformed_retry_of_a_published_id_is_immutable(credentials, tmp_path):
    async def run():
        async with Harness(str(tmp_path / 'assets.db')) as h:
            peer = Peer()
            await authenticate(h.server, peer)
            assert (await dispatch(h.server, peer, frame()))['type'] == 'asset.publish.ok'
            before = await h.rows()
            bad = json.dumps({'schema_version': 1, 'unknown': 1})
            assert (await dispatch(h.server, peer, frame(body=bad)))['error_code'] == 'report_producer_immutable'
            assert await h.rows() == before
    asyncio.run(run())


def test_concurrent_principal_and_other_caller_cannot_both_write(credentials, tmp_path):
    async def run():
        for attempt in range(10):
            async with Harness(str(tmp_path / f'assets-{attempt}.db')) as h:
                principal = frame(_auth_context={'service_authenticated': True, 'service_actor': BRIEF})
                seat = frame(from_stream_id='host-a:v2-seat', stream_id='host-a:v2-seat',
                             producer='host-a:v2-seat', body=report_body('Forged.'),
                             _auth_context={'token_verified': True, 'stream_id': 'host-a:v2-seat'})
                seat.pop('stream_token')
                order = (principal, seat) if attempt % 2 else (seat, principal)
                replies = await asyncio.gather(*(h.assets.asset(m) for m in order))
                assert sum(r['type'] == 'asset.publish.ok' for r in replies) == 1, replies
                assert {r.get('error_code') for r in replies} - {None} <= {'asset_unauthorized', 'report_producer_immutable'}
                row, = await h.rows()
                if row['producer'] == BRIEF:
                    assert 'Forged.' not in row['body']
                else:
                    assert row['producer'] == 'host-a:v2-seat'
    asyncio.run(run())


@pytest.mark.parametrize('claim', ['producer', 'namespace'])
def test_no_other_caller_may_be_first_to_write_a_brief(credentials, tmp_path, claim):
    async def run():
        async with Harness(str(tmp_path / 'assets.db')) as h:
            seat = frame(from_stream_id='host-a:v2-seat', stream_id='host-a:v2-seat',
                         producer=BRIEF if claim == 'producer' else 'host-a:v2-seat', body=report_body('Forged.'),
                         _auth_context={'token_verified': True, 'stream_id': 'host-a:v2-seat'})
            seat.pop('stream_token')
            if claim == 'producer':
                seat.update(asset_id='other-id', spec_id='spec_other', title='Anything', tags=['x'])
            assert (await h.assets.asset(seat))['error_code'] == 'asset_unauthorized'
            assert await h.rows() == [] and h.broadcasts == []
            principal = frame(_auth_context={'service_authenticated': True, 'service_actor': BRIEF})
            assert (await h.assets.asset(principal))['type'] == 'asset.publish.ok'
            row, = await h.rows()
            assert row['producer'] == BRIEF and row['stream_id'] == BRIEF and 'Forged.' not in row['body']
    asyncio.run(run())


def test_other_assets_under_the_spec_are_unaffected(credentials, tmp_path):
    async def run():
        async with Harness(str(tmp_path / 'assets.db')) as h:
            seat = {'type': 'asset.publish', 'request_id': 'r', 'stream_id': 'host-a:v2-seat', 'title': 'QA notes',
                    'content_type': 'report', 'body': report_body('notes'), 'spec_id': SPEC, 'asset_id': 'qa-notes',
                    '_auth_context': {'token_verified': True, 'stream_id': 'host-a:v2-seat'}}
            assert (await h.assets.asset(seat))['type'] == 'asset.publish.ok'
    asyncio.run(run())


def test_cd_and_backup_unknown_verbs_keep_unsupported_reply(credentials):
    async def run():
        server = Server()
        peer = Peer()
        backup = 'amaterasu:wmi-pg-dailybackup'
        assert (await dispatch(server, peer, frame('hello', from_stream_id=backup,
                                                   stream_token=BACKUP_TOKEN)))['type'] == 'ready'
        reply = await dispatch(server, peer, frame('definitely.not.a.verb', from_stream_id=backup,
                                                   stream_token=BACKUP_TOKEN))
        assert reply.get('error_code') != 'system_producer_forbidden'
    asyncio.run(run())


@pytest.mark.parametrize('broken', [
    {'mode': 0o644}, {'mode': 0o640}, {'stream_id': 'altum-bot-cd'}, {'stream_id': 'amaterasu:wmi-pg-dailybackup'},
    {'stream_id': 'no-colon'}, {'token_file': 'relative/path'}, {'asset_id_pattern': '^daily-report-[0-9]{8}$'},
    {'asset_id_pattern': '^(a)(b)$'}, {'asset_id_pattern': '('}, {'title_template': 'Daily report'},
    {'title_template': '{cutoff} {cutoff}'}, {'tag': ''}, {'tag': 'bad tag'}, {'body_max_bytes': 0},
    {'body_max_bytes': 2 * 1024 * 1024}, {'body_max_bytes': True}, {'spec_id': ''}, {'extra_key': 1},
    {'cutoff_format': 7}, {'cutoff_format': '%Q'}, {'cutoff_format': '%'}, {'cutoff_format': '%Y'},
    {'cutoff_format': '%m%d'}, {'cutoff_format': '%y%m'}, {'stream_token': TOKEN},
])
def test_invalid_or_unsafe_config_disables_the_principal(credentials, tmp_path, broken):
    import report_producer
    broken = dict(broken)
    mode = broken.pop('mode', 0o600)
    path = write_config(tmp_path, mode=mode, **{'token_file': str(credentials['brief']), **broken})
    assert report_producer.load() is None
    async def run():
        async with Harness(str(tmp_path / 'assets.db')) as h:
            reply = await dispatch(h.server, Peer(), frame('hello'))
            assert reply.get('type') != 'ready'
            assert await h.rows() == []
    asyncio.run(run())
    path.unlink()


def test_config_is_reread_and_names_only_a_path(credentials, tmp_path, monkeypatch):
    import report_producer
    producer = report_producer.load()
    assert producer is not None and producer.anchor == ('examplehost', 'daily-report')
    assert producer.cutoff('daily-report-20261007-r3') == '20261007' and producer.cutoff('daily-report-20261399') is None
    assert TOKEN not in credentials['config'].read_text()          # the config holds a path, never a token
    async def run():
        async with Harness(str(tmp_path / 'assets.db')) as h:
            peer = Peer()
            await authenticate(h.server, peer)
            credentials['config'].unlink()                          # removal disables the bound socket at once
            assert (await dispatch(h.server, peer, frame()))['error_code'] in {
                'system_producer_auth_required', 'system_producer_forbidden', 'system_producer_payload_invalid'}
            assert await h.rows() == []
    asyncio.run(run())


def test_other_fixed_principals_match_the_server_constants():
    import report_producer
    import server
    assert report_producer.OTHER_FIXED_PRINCIPALS == {server.FIXED_SYSTEM_PRODUCER_STREAM_ID,
                                                       server.WMI_BACKUP_PRODUCER_STREAM_ID}


# sha256 digests of the deployed private binding's principal, spec id and id prefix:
# the binding lives in runtime config, so these public sources must not contain it.
PRIVATE_BINDING_DIGESTS = {
    '363e0c879c76a6d5f4e6fc099acd52a4d532a674227f53aeed1914dc8944a3d2',
    '93af1649e439842da5c2f7944220f0defb891a17f6d1c355af9edc83a8ba1532',
    '027b9a74c6f635a2d3c490f9c712d7d2bedf0889b53ff3e9c430fe097f814c53',
}


def test_public_sources_carry_no_private_producer_binding():
    import hashlib
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for name in ('server.py', 'assets.py', 'report_producer.py', '../agent-orch/agent_orch/wsclient.py',
                 '../agent-orch/README.md', '../../docs/report_assets.md'):
        words = set(re.findall(r'[A-Za-z0-9_:.-]+', (root / name).read_text()))
        pieces = words | {w.rsplit('-', 1)[0] for w in words} | {w[:i] for w in words for i in range(1, len(w))}
        assert not {hashlib.sha256(w.encode()).hexdigest() for w in pieces} & PRIVATE_BINDING_DIGESTS, name


@pytest.mark.parametrize('fmt', ['%Y%m%d', '%Y%m%dT1300Z', '%Y-%m-%d'])
def test_full_date_cutoff_formats_load(credentials, tmp_path, fmt):
    import report_producer
    write_config(tmp_path, token_file=str(credentials['brief']), cutoff_format=fmt)
    assert report_producer.load() is not None


def test_fixed_width_revision_grammar_from_config(credentials, tmp_path):
    """A deployment may pin revisions to a fixed width (e.g. -rNN) and a fixed time suffix purely by config."""
    import report_producer
    write_config(tmp_path, token_file=str(credentials['brief']),
                 asset_id_pattern=r'^daily-report-([0-9]{8}T1300Z)(?:-r[0-9]{2})?$',
                 cutoff_format='%Y%m%dT1300Z', title_template='Daily report {cutoff}')
    producer = report_producer.load()
    assert producer.cutoff('daily-report-20261007T1300Z') == '20261007T1300Z'
    assert producer.cutoff('daily-report-20261007T1300Z-r02') == '20261007T1300Z'
    for outside in ('daily-report-20261007T1300Z-r1', 'daily-report-20261007T1300Z-r123', 'daily-report-20261007T0000Z'):
        assert producer.cutoff(outside) is None and not producer.owns_asset_id(outside)
    async def run():
        async with Harness(str(tmp_path / 'assets.db')) as h:
            peer = Peer()
            await authenticate(h.server, peer)
            ok = await dispatch(h.server, peer, frame(asset_id='daily-report-20261007T1300Z-r02',
                                                      title='Daily report 20261007T1300Z'))
            assert ok['type'] == 'asset.publish.ok'
            short = await dispatch(h.server, peer, frame(asset_id='daily-report-20261007T1300Z-r1',
                                                         title='Daily report 20261007T1300Z'))
            assert short['error_code'] == 'system_producer_payload_invalid'
            assert [r['asset_id'] for r in await h.rows()] == ['daily-report-20261007T1300Z-r02']
    asyncio.run(run())


def test_reader_list_filtered_by_a_system_producer_id_is_a_filter_not_a_login(credentials, tmp_path):
    """A report board lists a spec filtered by its producer id. `producer` on
    asset.list selects rows; it is not an identity claim, so an unauthenticated
    reader gets the producer's rows, and gains nothing else by sending it."""
    async def run():
        async with Harness(str(tmp_path / 'assets.db')) as h:
            producer_peer = Peer()
            await authenticate(h.server, producer_peer)
            assert (await dispatch(h.server, producer_peer, frame()))['type'] == 'asset.publish.ok'
            reader = Peer('127.0.0.1')  # the web host reads over loopback
            window = dict(type='asset.list', request_id='reader-list', spec_id=SPEC,
                          asset_id_prefix='daily-report-', sort='asset_id_desc', limit=120)
            unfiltered = await dispatch(h.server, reader, window)
            assert [a['asset_id'] for a in unfiltered['assets']] == [f'daily-report-{CUTOFF}']
            for system_id in (BRIEF, 'altum-bot-cd'):
                listed = await dispatch(h.server, reader, {**window, 'producer': system_id})
                assert listed['type'] == 'asset.list.ok', listed
                want = [f'daily-report-{CUTOFF}'] if system_id == BRIEF else []
                assert [a['asset_id'] for a in listed['assets']] == want
            # The filter bound nothing: this socket is still not the producer.
            assert reader not in h.server._client_system_producers
            refused = await dispatch(h.server, reader, frame(stream_token='wrong-token'))
            assert refused['error_code'] == 'system_producer_auth_required'
            tokenless = frame()
            del tokenless['stream_token']
            refused = await dispatch(h.server, reader, tokenless)
            assert refused['error_code'] == 'system_producer_auth_required'
            assert len(await h.rows()) == 1
    asyncio.run(run())
