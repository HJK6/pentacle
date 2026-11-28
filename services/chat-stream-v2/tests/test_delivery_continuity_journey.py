"""Bounded focused-stream continuity for a throttled mobile-shaped client."""
import asyncio
from collections import Counter
from contextlib import AsyncExitStack, suppress
import json
import logging
import os
from pathlib import Path
import secrets

import pytest
from tools.disposable_smoke import disposable_composite
from tools.proactive_note_probe import connect_client, note, prepare


class CloseRecords(logging.Handler):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.changed = asyncio.Event()
    def emit(self, record):
        message = record.getMessage()
        if message.startswith('conn_diag '):
            row = json.loads(message[len('conn_diag '):])
            if row['event'] == 'close':
                self.rows.append(row)
                self.changed.set()

    async def close_for(self, conn_id):
        while True:
            self.changed.clear()
            found = next((r for r in self.rows if r['conn_id'] == conn_id), None)
            if found:
                return found
            await self.changed.wait()


class Forwarder:
    def __init__(self, target):
        self.target = target
        self.backlog = asyncio.Event()
        self.bytes = 0
        self.writers = set()
        self.tasks = set()

    async def start(self):
        self.listener = await asyncio.start_server(self.forward, '127.0.0.1', 0)
        return self.listener.sockets[0].getsockname()[1]

    async def forward(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        upstream_reader, upstream_writer = await asyncio.open_connection('127.0.0.1', self.target)
        self.writers.update((writer, upstream_writer))

        async def up():
            while data := await reader.read(65536):
                upstream_writer.write(data)
                await upstream_writer.drain()

        async def down():
            while data := await upstream_reader.read(65536):
                if len(data) > 6250:
                    self.backlog.set()
                for offset in range(0, len(data), 6250):
                    writer.write(data[offset:offset + 6250])
                    await writer.drain()
                    self.bytes += len(data[offset:offset + 6250])
                    try:
                        await asyncio.wait_for(asyncio.Event().wait(), 0.1)
                    except TimeoutError:
                        pass

        try:
            jobs = [asyncio.create_task(up()), asyncio.create_task(down())]
            await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            for stream in (writer, upstream_writer):
                self.writers.discard(stream)
                stream.close()
                with suppress(Exception):
                    await stream.wait_closed()
            self.tasks.discard(task)

    async def cut(self):
        for writer in list(self.writers):
            writer.transport.abort()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)

    async def close(self):
        self.listener.close()
        await self.listener.wait_closed()
        await self.cut()


class MobileShaped:
    def __init__(self, harness, port, mode):
        self.harness, self.port, self.mode = harness, port, mode
        self.rows, self.received = [], []
        self.last_inbound = asyncio.Event()

    def apply(self, event):
        if event.get('stream_id') != self.harness['target']:
            return
        seq = event['daemon_seq']
        self.received.append(seq)
        for index, row in enumerate(self.rows):
            if row['daemon_seq'] == seq:
                self.rows[index] = event
                break
        else:
            self.rows.append(event)
        self.rows.sort(key=lambda row: row['daemon_seq'])

    async def open(self, stack):
        self.frames = asyncio.Queue()
        server = self.harness['server']
        self.harness['cli_env']['AGENT_ORCH_WS_URL'] = f'ws://127.0.0.1:{self.port}'
        self.socket = await connect_client(stack, self.harness, 'pentacle-mobile')
        peer = next(peer for peer, trust in server._connection_trust.items()
                    if trust.client_kind == 'pentacle-mobile' and server._operator_authenticated(peer))
        self.conn_id = server._connection_diagnostics[peer].conn_id
        self.reader = asyncio.create_task(self.read())
        self.heartbeat = asyncio.create_task(self.ping()) if self.mode == 'combined' else None

    async def read(self):
        try:
            async for raw in self.socket:
                self.last_inbound.set()
                frame = json.loads(raw)
                if frame.get('type') == 'chat.event':
                    self.apply(frame['event'])
                if frame.get('type') in ('request_stream_events.chunk', 'request_stream_events.ok'):
                    for row in frame.get('events', []):
                        self.apply(row)
                await self.frames.put(frame)
        except Exception:
            pass
        finally:
            await self.frames.put({'type': 'socket.closed'})

    async def ping(self):
        while True:
            try:
                await asyncio.wait_for(asyncio.Event().wait(), 1.5)
            except TimeoutError:
                pass
            self.last_inbound.clear()
            await self.socket.send(json.dumps({'type': 'ping'}))
            try:
                await asyncio.wait_for(self.last_inbound.wait(), 1)
            except TimeoutError:
                await self.socket.close(code=4000, reason='focused_heartbeat_timeout')
                return

    async def wait_for(self, predicate):
        while True:
            frame = await self.frames.get()
            if predicate(frame):
                return frame
            if frame['type'] == 'socket.closed':
                raise AssertionError('connection closed while waiting for frame')

    async def history(self):
        await self.socket.send(json.dumps({'type': 'request_stream_events', 'request_id': 'focused',
            'stream_id': self.harness['target'], 'limit': 300, 'order': 'newest_first'}))
        result = await self.wait_for(lambda f: f.get('type') == 'request_stream_events.ok' and f.get('request_id') == 'focused')
        assert result.get('complete', True) and not result.get('rehydrate_failed')

    async def stop(self):
        if self.heartbeat:
            self.heartbeat.cancel()
        self.reader.cancel()
        await asyncio.gather(self.reader, *([self.heartbeat] if self.heartbeat else []), return_exceptions=True)


@pytest.mark.parametrize('mode', ['combined', 'transport_only'])
def test_mobile_shaped_bounded_focused_delivery_continuity(mode, caplog):
    """A mobile-shaped client merges the bounded focused tail across a bulk-send drop."""
    async def journey():
        records = CloseRecords()
        logger = logging.getLogger('chat_streamd_v2.server')
        logger.addHandler(records)
        caplog.set_level(logging.INFO, logger='chat_streamd_v2.server')
        evidence = {'mode': mode, 'expected': [], 'received': [], 'applied': [], 'outcomes': {}}
        proxy = client = None
        try:
            async with asyncio.timeout(180), disposable_composite(operator_composite='operator-excluded:assistant') as harness:
                await prepare(harness)
                port = harness['server']._ws_server.sockets[0].getsockname()[1]
                proxy = Forwarder(port)
                proxy_port = await proxy.start()
                client = MobileShaped(harness, proxy_port, mode)
                async with AsyncExitStack() as stack:
                    await client.open(stack)
                    await client.history()

                    async def publish(label):
                        payload = note(harness, 'continuity-' + label)
                        ack = await harness['rpc'](payload)
                        assert ack['type'] == 'assistant.publish.ok' and ack['duplicate'] is False
                        evidence['expected'].append({'id': ack['event_id'], 'text': payload['message']})
                        return ack['event_id']

                    anchor = await publish('anchor')
                    await client.wait_for(lambda f: f.get('type') == 'chat.event' and f['event'].get('daemon_seq') == anchor)
                    proxy.backlog.clear()
                    rows = [{'stream_id': harness['target'], 'session_kind': 'assistant_composite',
                             'last_text': secrets.token_hex(100)} for _ in range(1500)]
                    await harness['server'].broadcast({'type': 'session.inventory', 'sessions': rows})
                    await proxy.backlog.wait()
                    await publish('in-flight')
                    conn_id = client.conn_id
                    await proxy.cut()
                    close = await records.close_for(conn_id)
                    await client.stop()
                    await publish('offline')
                    await client.open(stack)
                    await client.history()
                    final = await publish('final')
                    await client.wait_for(lambda f: f.get('type') == 'chat.event' and f['event'].get('daemon_seq') == final)
                    history = await harness['store'].fetch_session_event_tail(harness['target'], limit=301)
                    assert sum(row['daemon_seq'] > anchor for row in history) <= 300
                    evidence['received'] = client.received
                    evidence['applied'] = [{'id': row['daemon_seq'], 'text': row.get('text'),
                                            'stream': row.get('stream_id')} for row in client.rows]
                    evidence['proxy_bytes'] = proxy.bytes
                    reason = close.get('close_reason')
                    evidence['drop'] = reason if reason in ('focused_heartbeat_timeout', 'keepalive_ping_timeout') else 'proxy_cut'
                    evidence['outcomes'] = {
                        'heartbeat': 'fired(' + reason + ')' if reason == 'focused_heartbeat_timeout' else ('not_fired' if mode == 'combined' else 'not_observed'),
                        'keepalive': 'fired(' + reason + ')' if reason == 'keepalive_ping_timeout' else 'not_observed'}
                    expected_ids = [row['id'] for row in evidence['expected']]
                    selected = [row for row in evidence['applied'] if row['id'] in expected_ids]
                    counts = Counter(row['id'] for row in selected)
                    assert [row['id'] for row in selected] == expected_ids and all(counts[i] == 1 for i in expected_ids) and all(
                        row['text'] == expected['text'] and row['stream'] == harness['target']
                        for row, expected in zip(selected, evidence['expected'])), json.dumps(evidence)
                    await client.stop()
        finally:
            if proxy:
                await proxy.close()
            logger.removeHandler(records)
            output = os.environ.get('PENTACLE_GATE_EVIDENCE_DIR')
            if output:
                Path(output, 'delivery-continuity-' + mode + '.json').write_text(json.dumps(evidence, indent=2))
    asyncio.run(journey())
