#!/usr/bin/env python3
"""Run the fixture daemon with its own real operator credential registry.

Only the registry's storage location is injected; challenge/proof verification
and command authorization use the production implementation unchanged.
"""
import os
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[3]
scratch = Path(sys.argv[1]).resolve()
owned_home = scratch / 'home'
owned_home.mkdir(mode=0o700, exist_ok=True)


def _expand_owned_home(value):
    raw = os.fspath(value)
    if raw == '~':
        return str(owned_home)
    if raw.startswith('~/'):
        return str(owned_home / raw[2:])
    if raw.startswith('~'):
        raise ValueError('named-user home expansion refused')
    return raw


# Redirect both process-local resolvers before any candidate module imports.
# Consent initialization must never read or create the real host admin token.
Path.home = classmethod(lambda cls: owned_home)
os.path.expanduser = _expand_owned_home
sys.path[:0] = [str(ROOT / 'services'), str(ROOT / 'services/chat-stream-v2')]
from _shared import operator_auth
import local_admin

assert local_admin.DEFAULT_PATH.resolve().is_relative_to(scratch)

auth_dir = scratch / 'operator-auth'
auth_dir.mkdir(mode=0o700, exist_ok=True)
registry_path = auth_dir / 'registry.json'
if sys.argv[2:] == ['--issue']:
    registry = operator_auth.OperatorCredentialRegistry(registry_path)
    _, envelope = registry.issue('pentacle', label='isolated web gate')
    token_path = auth_dir / 'token'
    descriptor = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as token:
        token.write(envelope)
else:
    original = operator_auth.OperatorCredentialRegistry

    class FixtureRegistry(original):
        def __init__(self, path=None):
            super().__init__(path or registry_path)

    # Real assistant-composite admission, binding validation, and USER storage;
    # only the downstream routing worker is inert. No provider or remote seat
    # may be contacted by this hermetic fixture.
    import json
    import assistant_composite
    manifest_path = scratch / 'voice-answers-fixture.json'
    if manifest_path.exists():
        voice_fixture = json.loads(manifest_path.read_text())
        if (voice_fixture.get('stream_id') != 'local:web-gate-assistant'
                or voice_fixture.get('producer_stream_id') != 'local:web-gate-voice-producer'
                or not voice_fixture.get('producer_generation')):
            raise ValueError('invalid isolated voice answers fixture')
        for key in list(os.environ):
            if key.startswith('PENTACLE_ASSISTANT_'):
                del os.environ[key]
        os.environ.update({
            'PENTACLE_ASSISTANT_COMPOSITE_ENABLED': '1',
            'PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID': voice_fixture['stream_id'],
            'PENTACLE_ASSISTANT_COMPOSITE_TITLE': 'Assistant Fixture',
            'PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID': voice_fixture['producer_stream_id'],
            'PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION': voice_fixture['producer_generation'],
            'PENTACLE_ASSISTANT_MIRROR_ENABLED': '0',
        })
        original_wake_worker = assistant_composite.AssistantComposite._wake_worker

        def fixture_wake_worker(self):
            if self.config.stream_id == voice_fixture['stream_id']:
                return
            return original_wake_worker(self)

        assistant_composite.AssistantComposite._wake_worker = fixture_wake_worker

    # BEGIN HOUSEHOLD FIXTURE: isolated loopback only; no product daemon changes.
    fixture_cosmo_url = os.environ.get('PENTACLE_WEB_GATE_COSMO_URL')
    if fixture_cosmo_url:
        from urllib.parse import urlsplit
        parsed_cosmo_url = urlsplit(fixture_cosmo_url)
        if (parsed_cosmo_url.hostname != '127.0.0.1'
                or parsed_cosmo_url.scheme not in ('http', 'https')
                or parsed_cosmo_url.username is not None
                or parsed_cosmo_url.password is not None):
            raise ValueError('household fixture URL must be loopback')
        fixture_token = scratch / 'cosmo.token'
        descriptor = os.open(fixture_token, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w') as token:
            token.write('synthetic-household-fixture-only')
        os.environ['COSMO_PENTACLE_TOKEN_FILE'] = str(fixture_token)
        os.environ['PENTACLE_COSMO_URL'] = fixture_cosmo_url
        os.environ['PENTACLE_COSMO_SELF'] = 'operator'
        os.environ['PENTACLE_HOUSEHOLD_PARTNER_NAME'] = 'Partner Fixture'
        import household
        original_household_init = household.Household.__init__

        def fixture_household_init(self, *args, **kwargs):
            kwargs['allow_insecure'] = True
            original_household_init(self, *args, **kwargs)

        household.Household.__init__ = fixture_household_init
    # END HOUSEHOLD FIXTURE

    # BEGIN ASR FIXTURE: test-only, in-process; the product backend is never called.
    # The stub records each call, then holds until the scenario writes the release file.
    # It holds up to 300 s, well past any scenario wait, so a slow reload can never
    # become a fixture-made transcription failure.
    asr_manifest = scratch / 'asr-stub.json'
    if asr_manifest.exists():
        import asyncio
        import transcribe
        asr = json.loads(asr_manifest.read_text())

        async def fixture_call_backend(self, *, data, mime):
            with (scratch / 'asr-calls.log').open('a') as calls:
                calls.write(mime + '\n')
            for _ in range(6000):
                if (scratch / 'asr-release').exists():
                    return {'text': asr['text'], 'duration_s': 1.0, 'model': 'fixture', 'vocabulary_version': ''}
                await asyncio.sleep(0.05)
            raise transcribe.VerbError('backend_unavailable', 'fixture ASR was never released')

        transcribe.Transcriber._call_backend = fixture_call_backend
    # END ASR FIXTURE

    operator_auth.OperatorCredentialRegistry = FixtureRegistry

    # The existing isolated daemon owns this synthetic input driver. It calls
    # production ingest/broadcast, including durable identity and inventory;
    # no snapshot, summary, history response, or chat event is manufactured.
    seam_manifest = scratch / 'client-seam-fixture.json'
    if seam_manifest.exists():
        import asyncio
        from datetime import datetime, timezone
        from server import Server
        from ingest import append_ingested_event
        from inventory import InventoryEmitter
        from seed_web_gate import _event, SEAM_STREAM

        seam = json.loads(seam_manifest.read_text())
        if seam['stream_id'] != SEAM_STREAM:
            raise ValueError('invalid isolated client seam fixture')
        original_bind, original_close = Server.bind, Server.close

        async def seam_driver(server):
            await server.inventory_ready.wait()
            emitter = InventoryEmitter(server.sessions, server.broadcast, min_interval_s=0)
            command = scratch / 'client-seam-command.json'
            try:
                while True:
                    if command.exists():
                        action = json.loads(command.read_text())['action']
                        command.unlink()
                        if action == 'update':
                            event = _event(SEAM_STREAM, 'ASSIST_TEXT', seam['update'], 2)
                            event['timestamp'] = datetime.now(timezone.utc).isoformat()
                            await append_ingested_event(server.store, server.broadcast, event,
                                                        recent_limit=500, sessions=server.sessions)
                        elif action == 'replay':
                            tail = await server.store.fetch_session_event_tail(SEAM_STREAM, limit=1)
                            await server.broadcast({'type': 'chat.event', 'event': tail[-1]})
                        else:
                            raise ValueError('unknown client seam action')
                        await emitter.emit_if_changed(immediate=True)
                        scratch.joinpath('client-seam-command.done').write_text(action)
                    await asyncio.sleep(0.05)
            finally:
                if emitter._flush_task is not None:
                    emitter._flush_task.cancel()
                    await asyncio.gather(emitter._flush_task, return_exceptions=True)

        async def seam_bind(self, **kwargs):
            port = await original_bind(self, **kwargs)
            self._web_gate_seam_task = asyncio.create_task(seam_driver(self))
            return port

        async def seam_close(self, **kwargs):
            task = getattr(self, '_web_gate_seam_task', None)
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            return await original_close(self, **kwargs)

        Server.bind, Server.close = seam_bind, seam_close
    sys.argv = [str(ROOT / 'services/chat-stream-v2/main.py'), *sys.argv[2:]]
    runpy.run_path(sys.argv[0], run_name='__main__')
