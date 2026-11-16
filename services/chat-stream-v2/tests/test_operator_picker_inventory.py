"""Desktop inventory through the real signed-operator dispatch, no listeners."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from account_runtime import AccountRuntime
from sessions import VerbError
from .test_account_rotation import binding
from .test_connection_logging import daemon, connected, signed_hello, ScriptedSocket


def test_signed_operator_picker_inventory_is_read_only(daemon):
    async def run():
        runtime = AccountRuntime(daemon.store, daemon.sessions, policy={'enabled': False})
        runtime.attach(daemon, SimpleNamespace(), SimpleNamespace())
        for alias in ('primary', 'spare'):
            await runtime.run(lambda accounts, alias=alias:
                accounts.register(binding(alias, host='fixture-host')))
        await runtime.run(lambda accounts: accounts.set_default(
            host='fixture-host', provider='codex', alias='primary'))
        async with connected(daemon, ScriptedSocket('198.51.100.20')) as (socket, welcome, _):
            _, hello = signed_hello(daemon, welcome, client='pentacle')
            assert (await socket.rpc(hello))['type'] == 'ready'
            auth = await daemon._auth_context(socket, {'type': 'account', 'action': 'status'})
            assert auth['operator_authenticated'] is True and auth['token_verified'] is False
            before = await runtime.run(lambda accounts: accounts.conn.total_changes)
            request_file = os.environ.get('PENTACLE_TEST_PICKER_REQUEST')
            request = json.loads(Path(request_file).read_text()) if request_file else {'type': 'account', 'action': 'status'}
            assert request == {'type': 'account', 'action': 'status'}
            response = await socket.rpc(request)
            assert response['type'] == 'account.ok', response
            assert {row['alias'] for row in response['accounts']} == {'primary', 'spare'}
            assert response['defaults'][0]['alias'] == 'primary'
            for untrusted in ({}, {'operator_authenticated': False},
                    {'operator_authenticated': 'true'}, {'token_verified': 'true'}, {'role': 'operator'}):
                with pytest.raises(VerbError, match='account_control_unauthorized'):
                    await runtime.operation({'action': 'status', '_auth_context': untrusted})
            for action in ('default-set', 'report-failure'):
                refused = await socket.rpc({'type': 'account', 'action': action,
                    'host': 'fixture-host', 'provider': 'codex', 'account': 'spare',
                    '_auth_context': {'token_verified': True, 'operator_authenticated': True}})
                assert refused['type'] == 'account.error', refused
                assert refused['error_code'] == 'account_control_unauthorized', refused
            assert await runtime.run(lambda accounts: accounts.conn.total_changes) == before
            output = os.environ.get('PENTACLE_TEST_PICKER_RESPONSE')
            if output:
                Path(output).write_text(json.dumps(response))
    asyncio.run(run())
