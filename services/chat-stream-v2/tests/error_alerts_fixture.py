"""Disposable real websocket/Store/Notify/outbox fixture; no model or live roots."""

from __future__ import annotations
import asyncio
import os
from pathlib import Path
from _shared import operator_auth
from assistant_composite import AssistantComposite, AssistantCompositeConfig
from blobs import BlobStore
from comms import Comms
from notify import Notify
from outbound_notices import OutboundNoticeQueue
from server import Server
from sessions import Sessions
from spawnctl import SpawnCtl
from store import Store
from transcribe import Transcriber
from cosmo_e2e.harness import (
    assert_disposable_root,
    ScopedMobileClient,
    StubTranscriberPoster,
)
from notification_answer_fixture import Provider


class ErrorAlertsHarness:
    def __init__(self, root, *, real_transport=False):
        self.root = assert_disposable_root(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.pump = None
        self.port = None
        self.server = self.notify = self.store = None
        self.cleanup = {}
        self.real_transport = real_transport
        self.provider = None
        self.ingest_task = None
        self.envelope = None
        self.requested_port = 0
        self.pump_enabled = True

    async def start(self):
        self.store = Store(str(self.root / "sessions.db"))
        self.store.start()
        if self.real_transport:
            from error_alerts_transport import OwnedProvider

            if self.provider is None:
                self.provider = OwnedProvider(self.root)
        else:
            self.provider = Provider(self.store, host="fixture")
        self.sessions = Sessions(self.store, tmux=self.provider, local_host="fixture")
        if self.real_transport and not self.provider.started:
            self.origin = await self.provider.start(self.store, self.sessions)
        else:
            self.origin = await self.store.fetch_session("fixture", "v2-test")
            if self.origin:
                await self.sessions.refresh()
            else:
                self.origin = await self.sessions.open(
                    "fixture", "v2-test", provider="codex", visibility="visible"
                )
        self.blobs = BlobStore(str(self.root / "blobs"), attachment_store=self.store)
        await self.blobs.start()
        self.comms = Comms(
            self.store,
            self.sessions,
            SpawnCtl(self.store, self.sessions, tmux=self.provider),
            blob_store=self.blobs,
            attachment_root=self.root / "attachments",
        )
        self.queue = OutboundNoticeQueue(self.store, self.comms)
        self.notify = Notify(
            str(self.root / "notifications.db"),
            comms=self.comms,
            sessions=self.sessions,
            notice_store=self.store,
            outbound=self.queue,
        )
        await self.notify.start()
        config = AssistantCompositeConfig.from_env(
            {
                "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
                "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": "fixture:assistant",
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": "fixture:v2-test",
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": self.origin[
                    "session_generation"
                ],
            }
        )
        self.composite = AssistantComposite(self.store, config=config)
        await self.composite.load_binding()
        self.digest = self.composite.front_desk_digest
        self.comms.assistant_ingress_policy = (
            self.composite.suppress_routine_backend_ingress
        )
        self.comms.front_desk_digest = self.digest
        self.queue.front_desk_digest = self.digest
        self.server = Server(
            host="127.0.0.1",
            port=self.requested_port,
            store=self.store,
            sessions=self.sessions,
            comms=self.comms,
            local_host="fixture",
        )
        self.server.notify = self.notify
        self.server.blobs = self.blobs
        self.server.assistant_composite = self.composite
        self.server.assistant_composites = {config.name: self.composite}
        self.server.handlers.update(self.notify.wire_handlers())
        self.server.handlers.update(self.blobs.wire_handlers())
        self.server._transcriber = Transcriber(
            self.blobs, mic_api="http://127.0.0.1:1", http_post=StubTranscriberPoster()
        )
        registry = operator_auth.OperatorCredentialRegistry(
            self.root / "creds" / "credentials.json"
        )
        if self.envelope is None:
            self.credential_id, self.envelope = registry.issue(
                "pentacle-mobile", label="fixture"
            )
            credential_file = self.root / "operator-envelope"
            fd = os.open(credential_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(self.envelope)
            self.web_credential_id, web_envelope = registry.issue(
                "pentacle", label="fixture-web"
            )
            fd = os.open(
                self.root / "web-operator-envelope",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(fd, "w") as handle:
                handle.write(web_envelope)
        self.server.operator_credential_registry = registry
        # Candidate uses the same production composition helper as main.
        if hasattr(self.server, "configure_error_alerts"):
            await self.server.configure_error_alerts(self.queue)
        self.port = await self.server.bind()
        self.url = f"ws://127.0.0.1:{self.port}"
        if self.real_transport:
            self.ingest = self.provider.ingest(
                self.store, self.sessions, self.server.broadcast
            )
            self.ingest_task = asyncio.create_task(self.ingest.run_forever())

        if self.pump_enabled:
            self.resume_pump()
        return self

    async def pause_pump(self):
        self.pump_enabled = False
        if self.pump:
            self.pump.cancel()
            try:
                await self.pump
            except asyncio.CancelledError:
                pass
            self.pump = None

    def resume_pump(self):
        if self.pump and not self.pump.done():
            raise RuntimeError("fixture already owns a queue pump")
        self.pump_enabled = True

        async def pump():
            while True:
                await self.queue.drain_once()
                await asyncio.sleep(5)

        self.pump = asyncio.create_task(pump())

    def client(self):
        return ScopedMobileClient(self.url, self.envelope)

    async def restart(self):
        self.requested_port = self.port
        await self.stop(keep_provider=True)
        await self.start()
        if self.port != self.requested_port:
            raise RuntimeError("fixture restart changed port")

    async def stop(self, *, keep_provider=False):
        errors = []
        if self.pump:
            self.pump.cancel()
            try:
                await self.pump
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                errors.append(type(exc).__name__)
        if self.ingest_task:
            self.ingest_task.cancel()
            try:
                await self.ingest_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                errors.append(type(exc).__name__)
            self.ingest_task = None
            from ingest import _close_stream

            for state in self.ingest._streams.values():
                _close_stream(state)
        for subject in (self.server, self.notify):
            if subject:
                try:
                    await (
                        subject.close() if subject is self.server else subject.stop()
                    )
                except Exception as exc:
                    errors.append(type(exc).__name__)
        if self.store:
            try:
                self.store.stop()
            except Exception as exc:
                errors.append(type(exc).__name__)
        if self.real_transport and self.provider and not keep_provider:
            try:
                await self.provider.stop()
            except Exception as exc:
                errors.append(type(exc).__name__)
        try:
            if self.port is None:
                raise ConnectionRefusedError()
            _, writer = await asyncio.open_connection("127.0.0.1", self.port)
        except (ConnectionRefusedError, OSError):
            pass
        else:
            writer.close()
            await writer.wait_closed()
            errors.append("listener_still_open")
        self.cleanup = {
            "owned_listeners": 0 if not errors else None,
            "owned_provider_processes": (
                (1 if keep_provider else 0) if not errors else None
            ),
            "errors": errors,
        }
        if errors:
            raise RuntimeError("cleanup_failed")
