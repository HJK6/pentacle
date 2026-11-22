"""Hermetic browser inputs for the Next chat tools, inert without a scenario command."""
import asyncio
import json
from datetime import datetime, timezone

from ingest import append_ingested_event
from seed_web_gate import _event


def install(Server, UIVerbs, VerbError, scratch):
    command = scratch / 'next-chat-tools-command.json'
    done = scratch / 'next-chat-tools-done.json'
    log = scratch / 'next-chat-tools-calls.jsonl'
    host = 'local'
    names = {'web-gate-1', 'web-gate-survivor', 'web-gate-empty', 'web-gate-assistant'}
    settings = {}
    renames = {}
    closed = set()
    original_filter = Server._filter_sessions_for_client
    original_bind, original_close = Server.bind, Server.close
    original_rename, original_delete = Server._on_rename, Server._on_close
    original_send, original_interrupt = Server._on_send, UIVerbs.send_interrupt
    server = None

    def project(self, *args, **kwargs):
        values = original_filter(self, *args, **kwargs)
        rows = []
        for value in values:
            name = str(value.get('stream_id', '')).split(':', 1)[-1]
            active = value.get('host') == host and name in names
            if active and name in closed:
                continue
            if active and name in settings.get('rows', {}):
                value = {**value, **settings['rows'][name]}
            if active and name in renames:
                value = {**value, 'display_name': renames[name]}
            rows.append(value)
        return rows

    async def emit(self):
        await self.broadcast({'type': 'session.inventory', 'sessions': self.sessions.list_open()})

    def record(verb, message):
        keys = ('host', 'session_name', 'display_name', 'force', 'operator_confirm',
                'text', 'request_id', 'queued_send_now')
        with log.open('a') as out:
            out.write(json.dumps({'verb': verb, **{k: message[k] for k in keys if k in message}}) + '\n')

    def target(msg):
        if (settings.get('enabled') and msg.get('host') == host and msg.get('session_name') in names
                and (msg.get('_auth_context') or {}).get('operator_authenticated') is True):
            return msg['session_name']
        return None

    async def rename(self, msg):
        name = target(msg)
        if not name:
            return await original_rename(self, msg)
        record('rename', msg)
        if msg.get('display_name') == 'Refused name':
            raise VerbError('fixture_rename_refused', 'Fixture refuses this rename')
        renames[name] = msg['display_name']
        await emit(self)
        return {'type': 'rename.ok', 'ok': True}

    async def remove(self, msg):
        name = target(msg)
        if not name:
            return await original_delete(self, msg)
        record('close', msg)
        if settings.get('refuse_close') == name or not (msg.get('operator_confirm') and msg.get('force')):
            raise VerbError('fixture_close_refused', 'Fixture refuses this close')
        # Leave one stale inventory round until the scenario flushes the
        # removal, to prove the renderer hides immediately after acceptance.
        closed.add(name)
        return {'type': 'close.ok', 'ok': True}

    receipts = {}
    sent_text = {}
    echo_number = 12000

    async def send(self, msg):
        if not target(msg) or not str(msg.get('text') or '').startswith('Chat tools '):
            return await original_send(self, msg)
        record('send', msg)
        text = msg['text']
        request_id = msg['request_id']
        sent_text[request_id] = text
        receipts[request_id] = {'type': 'send.result', 'request_id': request_id,
                                'delivery': 'not_landed' if 'failed' in text else 'landed',
                                'provider_queued': 'queued' in text}
        return {'type': 'send.accepted', 'ok': True, 'request_id': request_id}

    async def interrupt(self, msg):
        selected = msg.get('queued_send_now')
        if not target(msg) or not isinstance(selected, dict):
            return await original_interrupt(self, msg)
        record('send.interrupt', msg)
        receipt = receipts.get(selected.get('original_request_id'))
        if not receipt:
            raise VerbError('fixture_unknown_queue', 'Unknown fixture queued receipt')
        # Native queue ownership ends only on the original correlated USER echo.
        nonlocal echo_number
        echo_number += 1
        original_id = selected['original_request_id']
        event = _event(host + ':' + msg['session_name'], 'USER', sent_text[original_id], echo_number)
        event['timestamp'] = datetime.now(timezone.utc).isoformat()
        event['request_id'] = original_id
        event['raw'].update({'request_id': original_id, 'receipt_state': 'landed'})
        await append_ingested_event(server.store, server.broadcast, event,
                                    recent_limit=500, sessions=server.sessions)
        return {'type': 'send.interrupt.ok', 'ok': True}

    async def driver(self):
        await self.inventory_ready.wait()
        while True:
            if command.exists():
                data = json.loads(command.read_text())
                command.unlink()
                try:
                    if data.get('reset'):
                        settings.clear()
                        renames.clear()
                        closed.clear()
                        receipts.clear()
                        sent_text.clear()
                        log.unlink(missing_ok=True)
                    settings.update(data.get('settings', {}))
                    if data.get('result'):
                        needle = data['result']
                        rows = [json.loads(line) for line in log.read_text().splitlines()]
                        request_id = next(row['request_id'] for row in reversed(rows)
                                          if row.get('text') == needle)
                        await self.broadcast(receipts[request_id])
                    else:
                        await emit(self)
                    for name, elapsed in data.get('elapsed', {}).items():
                        await self.broadcast({'type': 'working.state', 'stream_id': host + ':' + name,
                                              'timestamp': datetime.now(timezone.utc).isoformat(),
                                              'elapsed_ms': elapsed, 'tasks': [],
                                              'tokens_input': 0, 'tokens_output': 0,
                                              'tokens_cache_read': 0, 'tokens_cache_creation': 0,
                                              'tokens_phase': 'unknown', 'shell_count_started': 0,
                                              'task_summary': {'running': 0, 'completed': 0, 'failed': 0}})
                    result = {'nonce': data['nonce'], 'ok': True}
                except Exception as error:
                    result = {'nonce': data['nonce'], 'error': repr(error)}
                temp = done.with_suffix('.tmp')
                temp.write_text(json.dumps(result))
                temp.replace(done)
            await asyncio.sleep(.05)

    async def bind(self, *args, **kwargs):
        nonlocal server
        server = self
        port = await original_bind(self, *args, **kwargs)
        self._next_chat_tools_driver = asyncio.create_task(driver(self))
        return port

    async def close(self, *args, **kwargs):
        task = getattr(self, '_next_chat_tools_driver', None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return await original_close(self, *args, **kwargs)

    Server._filter_sessions_for_client = project
    Server._on_rename, Server._on_close = rename, remove
    Server._on_send, UIVerbs.send_interrupt = send, interrupt
    Server.bind, Server.close = bind, close
