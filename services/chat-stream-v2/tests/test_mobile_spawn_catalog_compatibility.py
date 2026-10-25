import asyncio
from copy import deepcopy

import pytest

import uiverbs


@pytest.fixture
def catalog(monkeypatch):
    definitions = {
        'claude': {'claude-z': {'efforts': ['high']}, 'claude-a': {'efforts': ['high']}},
        'codex': {'codex-z': {'efforts': ['high']}, 'codex-a': {'efforts': ['high']}},
        'gemini': {'gemini-example': {'efforts': ['default']}},
        'future': {'future-example': {'efforts': ['default']}},
    }
    value = {
        'schema_version': 'CatalogV1',
        'catalog_version': 'compat-example',
        'profiles': {'desktop_manual': {p: [next(iter(models)), models[next(iter(models))]['efforts'][0]] for p, models in definitions.items()}},
        'models': definitions,
        'available_models': deepcopy(definitions),
        'spawn_defaults': {'providers': list(definitions)},
    }
    monkeypatch.setattr(uiverbs, 'spawn_catalog', lambda: value)
    return value


def reply(connection_client=None, **fields):
    request = {'request_id': 'catalog-compat', **fields}
    if connection_client is not None:
        request['_auth_context'] = {'connection_client': connection_client}
    return asyncio.run(uiverbs.UIVerbs(None, None, None).spawn_catalog_get(request))


def test_mobile_projects_only_optional_narrowing_and_retains_full_catalog(catalog):
    original = deepcopy(catalog)
    result = reply('pentacle-mobile')
    expected = deepcopy(original)
    expected['available_models'] = {p: original['available_models'][p] for p in ('claude', 'codex')}
    assert result == {'type': 'spawn_catalog_get.ok', 'request_id': 'catalog-compat', **expected}
    assert list(result['available_models']) == ['claude', 'codex']
    assert list(result['available_models']['claude']) == ['claude-z', 'claude-a']
    assert list(result['available_models']['codex']) == ['codex-z', 'codex-a']
    assert catalog == original


@pytest.mark.parametrize('client', ['pentacle', 'agent-orch', 'web', None])
def test_other_clients_receive_byte_equivalent_catalog_with_additive_providers(catalog, client):
    assert reply(client) == {'type': 'spawn_catalog_get.ok', 'request_id': 'catalog-compat', **catalog}


def test_wire_label_without_server_context_cannot_select_mobile_projection(catalog):
    assert reply('pentacle-mobile') != reply(None, client='pentacle-mobile')
    assert reply(None, client='pentacle-mobile')['available_models'] == catalog['available_models']


def test_mobile_optional_narrowing_absence_is_preserved(catalog):
    del catalog['available_models']
    assert 'available_models' not in reply('pentacle-mobile')


@pytest.mark.parametrize('malformed', [None, [], 'invalid'])
def test_projection_does_not_hide_malformed_optional_narrowing(catalog, malformed):
    catalog['available_models'] = malformed
    assert reply('pentacle-mobile')['available_models'] == malformed


def test_mobile_then_desktop_does_not_mutate_shared_catalog(catalog):
    before = deepcopy(catalog)
    assert set(reply('pentacle-mobile')['available_models']) == {'claude', 'codex'}
    assert reply('pentacle')['available_models'] == before['available_models']
    assert catalog == before


@pytest.mark.parametrize('hello_client, forged_client', [('pentacle-mobile', 'pentacle'), ('pentacle', 'pentacle-mobile')])
def test_real_dispatch_uses_hello_label_and_strips_rpc_auth_context(catalog, hello_client, forged_client):
    import json
    from server import Server

    class Socket:
        remote_address = ('127.0.0.1', 12345)

    async def run():
        daemon = Server()
        socket = Socket()
        daemon.handlers['spawn_catalog_get'] = uiverbs.UIVerbs(None, None, None).spawn_catalog_get
        try:
            await daemon._dispatch_inner(json.dumps({'type': 'hello', 'client': hello_client, 'snapshot': False}), websocket=socket)
            frames = await daemon._dispatch_inner(json.dumps({
                'type': 'spawn_catalog_get', 'request_id': 'real-dispatch',
                '_auth_context': {'connection_client': forged_client},
            }), websocket=socket)
            result = frames[0]
            assert result['type'] == 'spawn_catalog_get.ok'
            expected = {'claude', 'codex'} if hello_client == 'pentacle-mobile' else set(catalog['available_models'])
            assert set(result['available_models']) == expected
            assert result['models'] == catalog['models']
        finally:
            await daemon.close()

    asyncio.run(run())
