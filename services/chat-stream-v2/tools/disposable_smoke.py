"""Owned loopback daemon for synthetic lanes and cards; no live configuration."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile
import uuid

import websockets

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from server import Server
from sessions import Sessions
from store import Store, STREAM_TOKEN_HASH_VERSION
from _shared.operator_auth import OperatorCredentialRegistry


def guard_target(target: str, operator_composite: str) -> None:
    if not operator_composite.strip() or target.strip() == operator_composite.strip():
        raise ValueError("smoke_operator_composite_forbidden")
    if not target.startswith("smoke-") or not target.endswith(":assistant"):
        raise ValueError("smoke_disposable_composite_required")


@asynccontextmanager
async def disposable_composite(*, operator_composite: str, target: str | None = None):
    """Create and finally destroy the daemon, composite and all owned state.

    The target is a test ID inside this daemon, never a live daemon API target.
    All listener/auth settings are explicit so inherited runtime settings cannot
    enable TLS listeners, operator privileges or external counterpart adapters.
    """
    target = target or f"smoke-{uuid.uuid4().hex}:assistant"
    guard_target(target, operator_composite)
    with tempfile.TemporaryDirectory(prefix="pentacle-smoke-composite-") as directory:
        store = Store(str(Path(directory) / "sessions.db"))
        composite = server = None
        store.start()
        try:
            actor = "smoke-fixture:fd"
            row = await store.open_session("smoke-fixture", "fd", provider="codex", role="assistant")
            token = secrets.token_urlsafe(32)
            await store.grant_stream_token("smoke-fixture", "fd", hashlib.sha256(token.encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION)
            config = AssistantCompositeConfig.from_env({
                "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
                "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": target,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": actor,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": row["session_generation"],
            })
            composite = AssistantComposite(store, config=config)
            await composite.ensure_projection()
            sessions = Sessions(store, local_host="smoke-fixture")
            await sessions.refresh()
            server = Server(host="127.0.0.1", port=0, store=store, sessions=sessions,
                local_host="smoke-fixture", dot_principal_stream_ids=[], dot_tls_port=0,
                dot_tls_cert="", dot_tls_key="", dot_tls_binds=["127.0.0.1"],
                dot_read_enabled=False, seat_operator_authority=False)
            server.assistant_composite = composite
            server.operator_credential_registry = OperatorCredentialRegistry(Path(directory) / "operator-credentials.json")
            port = await server.bind()
            async with websockets.connect(f"ws://127.0.0.1:{port}") as socket:
                await socket.recv()
                await socket.send(json.dumps({"type": "hello", "client": "disposable-smoke",
                    "stream_token": token, "from_stream_id": actor}))
                async with asyncio.timeout(5):
                    while json.loads(await socket.recv()).get("type") != "snapshot":
                        pass

                async def rpc(payload):
                    guard_target(target, operator_composite)
                    if payload.get("composite_stream_id") != target:
                        raise ValueError("smoke_target_mismatch")
                    await socket.send(json.dumps(payload))
                    async with asyncio.timeout(5):
                        while True:
                            result = json.loads(await socket.recv())
                            if result.get("request_id") == payload["request_id"]:
                                if result.get("type", "").endswith(".error"):
                                    raise RuntimeError(result)
                                return result

                yield {"target": target, "store": store, "rpc": rpc,
                    "state_path": Path(directory), "server": server,
                    "cli_env": dict(PATH=os.environ.get("PATH", "/usr/bin:/bin"),
                        PYTHONPATH=str(Path(__file__).resolve().parents[2] / "agent-orch"),
                        AGENT_ORCH_WS_URL=f"ws://127.0.0.1:{port}", AGENT_ORCH_TOKEN="",
                        AGENT_ORCH_HOST_ID="smoke-fixture", AGENT_ORCH_STREAM_ID=actor,
                        AGENT_ORCH_STREAM_TOKEN=token, AGENT_ORCH_RUNTIME_DIR=str(Path(directory)/"cli"),
                        AGENT_ORCH_MEMORY_REPO=str(Path(directory)/"memory"))}
        finally:
            try:
                if server is not None:
                    await server.close()
            finally:
                try:
                    if composite is not None:
                        await composite.stop()
                finally:
                    store.stop()


def publish_smoke_card(payload: dict) -> dict:
    """Publish a synthetic status card locally and return durable evidence.

    Callers retain the receipt in their normal JSON/log output. No notification
    is submitted over their live operator connection.
    """
    async def run():
        operator = os.environ.get("PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID", "")
        # No configured operator ID is needed to reach a live daemon: this
        # helper has no live URL. Use a separate reserved exclusion when absent.
        operator = operator or "operator:assistant"
        async with disposable_composite(operator_composite=operator) as harness:
            result = await harness["rpc"]({"type": "assistant.publish",
                "request_id": "smoke-card-" + uuid.uuid4().hex,
                "composite_stream_id": harness["target"], "publish_kind": "status",
                "message": str(payload["title"]) + "\n" + str(payload["body"])})
            receipt = {"composite_stream_id": harness["target"], "publication_type": result["type"],
                "producer": payload.get("producer"), "severity": payload.get("severity"),
                "title": payload["title"], "body": payload["body"],
                "operator_event_count": len(await harness["store"].fetch_session_event_tail(operator, limit=100))}
        receipt["cleaned_up"] = not harness["state_path"].exists() and harness["server"]._ws_server is None
        return receipt
    return asyncio.run(run())
