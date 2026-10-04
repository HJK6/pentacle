"""Cosmo E2E disposable Foundation harness — composed test-only builder.

Stands up the REAL public Foundation server (server.Server) + store + sessions +
blob store + auth + scoped handlers on an allocated loopback-only port with
owned, disposable DB/blob/config roots and disposable credentials, plus a daff
``AssistantComposite`` whose backend delivery (``dispatch``) and external sinks
(transcriber backend poster, Cosmo push transport) are the ONLY scripted
boundaries.

Functional journeys are driven over a REAL bound WebSocket:
  * the Cosmo mobile client authenticates with its ISSUED scoped credential via
    the real ``auth_v2`` challenge/proof hello (``operator_credential_registry``);
  * the daff backend seat authenticates with a real ``stream_token`` hello and
    publishes replies ONLY through the authorized ``assistant.publish`` verb.
No ``_auth_context``/``ConnectionTrust`` is fabricated for a functional case; no
frame is written to a client directly.

This module is TEST-ONLY.  It is never imported by ``main.py``/``launch.py``
(asserted by tests/test_cosmo_e2e_isolation.py) and introduces no production
env flag.  No tmux/spawn/fleet/egress/real-transcriber/APNs/model.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import socket as _socket
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

import websockets

from _shared import operator_auth
from assistant_composite import AssistantComposite, AssistantCompositeConfig
from blobs import BlobStore, DEFAULT_BLOB_ROOT
from comms import Comms
from cosmo_push import CosmoPush
from server import Server
from sessions import Sessions
from store import Store, STREAM_TOKEN_HASH_VERSION
from transcribe import Transcriber

SERVICE_DIR = Path(__file__).resolve().parents[2]
MANIFEST_PATH = Path(__file__).resolve().parent / "cases.json"

DAFF_CHAT = "daff:assistant"
DAFF_SEAT = "fixture-daff-seat:visible"
LOCAL = "fixture-cosmo-e2e"
MOBILE_KIND = "pentacle-mobile"
_CHUNK = 512 * 1024


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text())


# --------------------------------------------------------------------------- #
# Isolation guards: refuse live/default data paths, occupied ports, prod creds #
# --------------------------------------------------------------------------- #
class IsolationError(RuntimeError):
    """Raised when the harness is asked to use a non-disposable resource."""


def _resolve(p: str | os.PathLike[str]) -> Path:
    return Path(p).expanduser().resolve()


def _disposable_base() -> Path:
    return Path(tempfile.gettempdir()).resolve()


#: Production roots that must never host harness state even if passed as the
#: tmp_root.  Derived from the real code (imported constant / env), never a
#: hard-coded host path, so the public-residue gate stays clean.
def _protected_roots() -> list[Path]:
    roots = [_resolve(DEFAULT_BLOB_ROOT)]
    for env_key in ("PENTACLE_CONFIG_ROOT", "PENTACLE_DB", "PENTACLE_DATA_ROOT"):
        val = os.environ.get(env_key)
        if val and val != ":memory:":
            roots.append(_resolve(val))
    return roots


def _disposable_bases() -> list[Path]:
    """Directories under which a disposable harness root is permitted: the OS
    temp base, plus any EXPLICITLY-designated gate/test temp base from the
    environment.  The v2 gate puts its pytest ``--basetemp`` under
    ``V2_GATE_EVIDENCE_DIR`` when the caller sets it (``_run_tier`` copies the
    full env into the pytest subprocess), and otherwise under the OS temp base
    via ``mkdtemp`` — so both real invocation paths are covered WITHOUT trusting
    an arbitrary ``pytest``-named path component (e.g. one crafted under $HOME)."""
    bases = [_disposable_base()]
    for env_key in ("V2_GATE_EVIDENCE_DIR", "COSMO_E2E_DISPOSABLE_BASE"):
        val = os.environ.get(env_key)
        if val:
            bases.append(_resolve(val))
    return bases


def _is_disposable_location(root: Path) -> bool:
    """A disposable root must live under the OS temp base or an explicitly
    designated gate/test temp base (see ``_disposable_bases``).  A path is NOT
    disposable merely because some component is named ``pytest`` — that earlier
    rule accepted e.g. ``$HOME/pytest-x/...`` and is the F1 hole this closes."""
    for base in _disposable_bases():
        if root == base or base in root.parents:
            return True
    return False


def assert_disposable_root(tmp_root: str | os.PathLike[str]) -> Path:
    """Validate the harness ROOT itself before anything is created under it.

    The root must be a disposable location (OS temp base or a pytest tmp dir) and
    must not be, or be inside, a protected live runtime/config/blob root."""
    root = _resolve(tmp_root)
    if not _is_disposable_location(root):
        raise IsolationError(f"refusing non-disposable harness root: {root}")
    for protected in _protected_roots():
        if root == protected or protected in root.parents:
            raise IsolationError(f"refusing harness root colliding with protected root {protected}: {root}")
    return root


def assert_disposable_db(db_path: str, tmp_root: str | os.PathLike[str]) -> None:
    if db_path == ":memory:":
        raise IsolationError("refusing :memory: DB (the validation plan requires an owned disposable file)")
    root = _resolve(tmp_root)
    target = _resolve(db_path)
    if root not in target.parents and target != root:
        raise IsolationError(f"refusing non-disposable DB path outside tmp root: {db_path}")


def assert_disposable_blob_root(blob_root: str, tmp_root: str | os.PathLike[str]) -> None:
    root = _resolve(tmp_root)
    target = _resolve(blob_root)
    if target == _resolve(DEFAULT_BLOB_ROOT):
        raise IsolationError("refusing the production DEFAULT_BLOB_ROOT")
    if root not in target.parents and target != root:
        raise IsolationError(f"refusing non-disposable blob root outside tmp root: {blob_root}")


def assert_disposable_creds(creds_path: str, tmp_root: str | os.PathLike[str]) -> None:
    root = _resolve(tmp_root)
    target = _resolve(creds_path)
    if root not in target.parents and target != root:
        raise IsolationError(f"refusing credentials store outside tmp root: {creds_path}")


def assert_free_port(port: int) -> None:
    if port == 0:
        return
    with closing(_socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError as exc:
            raise IsolationError(f"refusing occupied port {port}") from exc


# --------------------------------------------------------------------------- #
# Labelled stubs for the ONLY two scripted external sinks                      #
# --------------------------------------------------------------------------- #
def _ok_transcript_body(text: str = "cosmo-e2e stub transcript", vocab: str = "fleet-stub") -> bytes:
    return json.dumps({
        "text": text, "language": "en", "duration_s": 1.0, "model": "stub",
        "compute": "stub", "segments": [{"start": 0.0, "end": 1.0, "text": text}],
        "vocabulary_version": vocab,
    }).encode()


class StubTranscriberPoster:
    """Labelled stub for the transcriber backend HTTP poster.  The REAL
    BlobStore/Transcriber are used; only this backend call is scripted."""

    label = "STUB:transcriber-backend-poster"

    def __init__(self, scripted: list | None = None) -> None:
        self.calls: list[dict] = []
        self._scripted = list(scripted or [])
        self._i = 0

    def __call__(self, url, *, data, content_type, timeout):
        self.calls.append({"url": url, "content_type": content_type})
        if self._i < len(self._scripted):
            item = self._scripted[self._i]
            self._i += 1
            if isinstance(item, Exception):
                raise item
            return item
        return (200, _ok_transcript_body())


class FakePushTable:
    """Labelled in-memory stand-in for the CosmoPushTokens DynamoDB table."""

    label = "STUB:cosmo-push-table"

    def __init__(self, items: list[dict] | None = None) -> None:
        self.items = list(items or [])
        self.deleted: list[str] = []

    def scan(self):
        return {"Items": list(self.items)}

    def delete_item(self, Key):
        self.deleted.append(Key["push_token"])
        self.items = [i for i in self.items if i.get("push_token") != Key["push_token"]]

    def put_item(self, Item):
        self.items.append(dict(Item))


def make_push_transport(sent: list):
    """Labelled stub Expo transport (no real notification egress)."""

    def _t(url, data):
        sent.append(data)
        return {"data": {"status": "ok", "id": "stub-ticket"}}

    _t.label = "STUB:expo-push-transport"
    return _t


# --------------------------------------------------------------------------- #
# Real-WebSocket clients (authenticated over the wire)                         #
# --------------------------------------------------------------------------- #
async def _recv_reply(sock, request_id: str, timeout: float = 10.0) -> dict:
    async with asyncio.timeout(timeout):
        while True:
            frame = json.loads(await sock.recv())
            if frame.get("request_id") == request_id:
                return frame


class ScopedMobileClient:
    """A real Cosmo mobile WebSocket client authenticated with its issued scoped
    credential via the real auth_v2 challenge/proof hello."""

    def __init__(self, url: str, envelope: str) -> None:
        self._url = url
        self._env = operator_auth.decode_envelope(envelope)
        self._sock = None

    async def __aenter__(self) -> "ScopedMobileClient":
        self._sock = await websockets.connect(self._url)
        welcome = json.loads(await self._sock.recv())
        assert welcome["type"] == "welcome", welcome
        nonce = welcome["auth"]["operator"]["nonce"]
        proof = operator_auth.make_proof(self._env["proof_key"], nonce,
                                         self._env["credential_id"], MOBILE_KIND)
        await self._sock.send(json.dumps({
            "type": "hello", "client": MOBILE_KIND,
            "capabilities": {"assistant_composite_v1": True},
            "auth_v2": {"scheme": operator_auth.AUTH_SCHEME,
                        "credential_id": self._env["credential_id"], "proof": proof},
        }))
        frames = [json.loads(await self._sock.recv()) for _ in range(2)]
        assert any(f["type"] == "snapshot" for f in frames), frames
        return self

    async def __aexit__(self, *exc) -> None:
        if self._sock is not None:
            await self._sock.close()

    async def rpc(self, verb: str, *, request_id: str, timeout: float = 10.0, **fields) -> dict:
        await self._sock.send(json.dumps({"type": verb, "request_id": request_id, **fields}))
        return await _recv_reply(self._sock, request_id, timeout)

    async def recv_until(self, predicate: Callable[[dict], bool], timeout: float = 10.0) -> dict:
        async with asyncio.timeout(timeout):
            while True:
                frame = json.loads(await self._sock.recv())
                if predicate(frame):
                    return frame

    async def stream_events(self, *, request_id: str = "rse") -> list[dict]:
        """Read the scope stream's events over the wire (request_stream_events)."""
        host, name = DAFF_CHAT.split(":", 1)
        reply = await self.rpc("request_stream_events", request_id=request_id,
                               host=host, session_name=name)
        assert reply["type"] == "request_stream_events.ok", reply
        return reply.get("events", [])


class BackendSeatClient:
    """A real daff-seat WebSocket client authenticated with a stream_token hello;
    it publishes replies ONLY through the authorized assistant.publish verb."""

    def __init__(self, url: str, token: str, seat_stream_id: str) -> None:
        self._url = url
        self._token = token
        self._seat = seat_stream_id
        self._sock = None

    async def __aenter__(self) -> "BackendSeatClient":
        self._sock = await websockets.connect(self._url)
        welcome = json.loads(await self._sock.recv())
        assert welcome["type"] == "welcome", welcome
        await self._sock.send(json.dumps({
            "type": "hello", "client": "cosmo-e2e-backend",
            "stream_token": self._token, "from_stream_id": self._seat,
        }))
        frames = [json.loads(await self._sock.recv()) for _ in range(2)]
        assert any(f["type"] == "snapshot" for f in frames), frames
        return self

    async def __aexit__(self, *exc) -> None:
        if self._sock is not None:
            await self._sock.close()

    async def publish(self, *, dispatch_id: str, reply_to_message_id: str, message: str,
                      attachment_ids: list[str] | None = None, request_id: str | None = None) -> dict:
        rid = request_id or ("publish:" + dispatch_id)
        # No from_stream_id/stream_token: the authenticated hello cached this
        # connection's binding, and composite.publish rejects any non-allowlist
        # field.  The tokenless follow-up RPC reuses the cached binding.
        await self._sock.send(json.dumps({
            "type": "assistant.publish", "request_id": rid,
            "composite_stream_id": DAFF_CHAT, "dispatch_id": dispatch_id,
            "reply_to_message_id": reply_to_message_id, "reply_to_question_id": None,
            "publish_kind": "prose", "response_state": "final", "message": message,
            "attachment_ids": attachment_ids or [], "evidence_refs": [],
        }))
        return await _recv_reply(self._sock, rid)


class FoundationHarness:
    """Composed real Foundation server + daff composite with scripted boundaries."""

    def __init__(self, tmp_root: str | os.PathLike[str]) -> None:
        # Guard the ROOT itself before creating anything under it.
        self.tmp_root = assert_disposable_root(tmp_root)
        os.makedirs(self.tmp_root, exist_ok=True)
        self.db_path = str(self.tmp_root / "sessions.db")
        self.blob_root = str(self.tmp_root / "blobs")
        self.creds_path = str(self.tmp_root / "creds" / "creds.json")
        self.config_root = str(self.tmp_root / "config")
        assert_disposable_db(self.db_path, self.tmp_root)
        assert_disposable_blob_root(self.blob_root, self.tmp_root)
        assert_disposable_creds(self.creds_path, self.tmp_root)
        os.makedirs(self.config_root, exist_ok=True)

        self.store: Store | None = None
        self.server: Server | None = None
        self.composite: AssistantComposite | None = None
        self.blob_store: BlobStore | None = None
        self.transcriber_stub = StubTranscriberPoster()
        self.push_sent: list[dict] = []
        self.push_table = FakePushTable([
            {"push_token": "t-cosmo", "scope_stream": DAFF_CHAT, "credential_id": None, "active": True},
        ])
        self.cosmo_push: CosmoPush | None = None
        self.scoped_envelope: str | None = None
        self.seat_token: str | None = None
        self.dispatched: list[dict] = []
        self._fail_dispatch_once = False
        self._dispatch_failures = 0
        self._port: int | None = None

    # -- lifecycle -------------------------------------------------------- #
    async def start(self, *, port: int = 0, bind: bool = True) -> "FoundationHarness":
        assert_free_port(port)
        store = Store(self.db_path)
        store.start()
        self.store = store
        seat = await store.open_session(
            *DAFF_SEAT.split(":", 1), provider="codex", role="assistant",
            visibility="default", pane_status="pane_alive",
            effective_model="gpt-6-sol", effective_effort="high")
        # Backend seat authenticates via a disposable stream token (real hello).
        self.seat_token = "cosmo-e2e-seat-token"
        await store.grant_stream_token(*DAFF_SEAT.split(":", 1),
                                       hashlib.sha256(self.seat_token.encode()).hexdigest(),
                                       STREAM_TOKEN_HASH_VERSION)
        config = AssistantCompositeConfig.from_env({
            "PENTACLE_ASSISTANT_DAFF_COMPOSITE_ENABLED": "1",
            "PENTACLE_ASSISTANT_DAFF_COMPOSITE_STREAM_ID": DAFF_CHAT,
            "PENTACLE_ASSISTANT_DAFF_DIRECT_PRIMARY_STREAM_ID": DAFF_SEAT,
            "PENTACLE_ASSISTANT_DAFF_DIRECT_PRIMARY_GENERATION": seat["session_generation"],
        }, name="daff", env_prefix="DAFF_")

        async def dispatch(route):
            self.dispatched.append(dict(route))
            if self._fail_dispatch_once and self._dispatch_failures == 0:
                self._dispatch_failures += 1
                raise RuntimeError("cosmo-e2e injected first-dispatch failure")
            return {"delivery": "landed"}

        self.cosmo_push = CosmoPush(
            table_factory=lambda: self.push_table,
            transport=make_push_transport(self.push_sent),
            revoked=lambda cid: False,
        )
        composite = AssistantComposite(store, config=config, dispatch=dispatch)
        composite.reply_push = self.cosmo_push.push_reply
        await composite.load_binding()
        await composite.ensure_projection()
        self.composite = composite

        sessions = Sessions(store, tmux=None, local_host=LOCAL)
        await sessions.refresh()
        blob_store = BlobStore(self.blob_root)
        await blob_store.start()
        self.blob_store = blob_store
        comms = Comms(store, sessions, None, blob_store=blob_store)
        server = Server(host="127.0.0.1", port=port, store=store, sessions=sessions,
                        comms=comms, local_host=LOCAL)
        server.assistant_composite = composite
        server.assistant_composites = {"daff": composite}
        server.handlers.update(blob_store.wire_handlers())
        server._transcriber = Transcriber(blob_store, mic_api="http://127.0.0.1:7780",
                                          http_post=self.transcriber_stub)
        # Disposable scoped credential registry (the Cosmo mobile client).
        reg = operator_auth.OperatorCredentialRegistry(Path(self.creds_path))
        _cid, envelope = reg.issue(MOBILE_KIND, label="cosmo-e2e", scope={"stream": DAFF_CHAT})
        self.scoped_envelope = envelope
        server.operator_credential_registry = reg
        # Wire the authorized publication-attachment boundary (the same
        # content-addressed blob path main.py binds), so a reply can resolve a
        # real uploaded attachment through server.comms.blob_store.
        composite.publication_attachments = server._assistant_publication_attachments
        self.server = server
        self._port = await server.bind()
        return self

    @property
    def port(self) -> int | None:
        return self._port

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self._port}"

    def mobile_client(self) -> ScopedMobileClient:
        return ScopedMobileClient(self.url, self.scoped_envelope)

    def backend_seat(self) -> BackendSeatClient:
        return BackendSeatClient(self.url, self.seat_token, DAFF_SEAT)

    async def stop(self) -> None:
        if self.server is not None:
            try:
                await self.server.close()
            except Exception:
                pass
        if self.store is not None:
            self.store.stop()

    def destroy(self) -> None:
        """Remove the disposable root entirely (cleanup proof)."""
        import shutil
        shutil.rmtree(self.tmp_root, ignore_errors=True)

    # -- helpers ---------------------------------------------------------- #
    async def await_route(self, input_identity: str, predicate: Callable[[dict], bool], tries: int = 300) -> dict | None:
        route = None
        for _ in range(tries):
            route = await self.store.get_assistant_composite_route(stream_id=DAFF_CHAT, input_identity=input_identity)
            if route and predicate(route):
                return route
            await asyncio.sleep(0.01)
        return route

    async def resolved_dispatch_id(self, input_identity: str) -> str:
        route = await self.await_route(input_identity,
                                       lambda r: r["routing_state"] == "resolved" and bool(self.dispatched))
        assert route is not None and route["routing_state"] == "resolved", route
        return route["dispatch_id"]

    async def upload_blob(self, client: ScopedMobileClient, content: bytes) -> str:
        rid = "upload-" + hashlib.sha256(content).hexdigest()[:8]
        init = await client.rpc("upload_blob_init", request_id=rid, size_hint_bytes=len(content))
        assert init["type"] == "upload_blob.init.ok", init
        offsets = list(range(0, max(len(content), 1), _CHUNK)) or [0]
        ok = None
        for i, off in enumerate(offsets):
            ok = await client.rpc("upload_blob_chunk", request_id=rid,
                                  data_b64=base64.b64encode(content[off:off + _CHUNK]).decode(),
                                  final=(i == len(offsets) - 1))
        assert ok["type"] == "upload_blob.ok", ok
        return ok["blob_sha"]

    def set_fail_dispatch_once(self, value: bool) -> None:
        self._fail_dispatch_once = value


def materialize_fixture(key: str) -> bytes:
    """Read a checked-in media fixture and assert its manifest sha256."""
    manifest = load_manifest()
    spec = manifest["fixtures"][key]
    path = SERVICE_DIR / spec["path"]
    data = path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != spec["sha256"]:
        raise IsolationError(f"fixture {key} sha256 mismatch: {actual} != {spec['sha256']}")
    return data


# --------------------------------------------------------------------------- #
# Cosmo server half (real create_app + TestClient + advanceable clock)        #
# --------------------------------------------------------------------------- #
class AdvanceableClock:
    def __init__(self, start: float = 1_793_000_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def cosmo_server_dir() -> str | None:
    """Discover the Cosmo server package dir without a host-private literal.

    Order: explicit env override, then sibling worktrees next to this repo."""
    env = os.environ.get("COSMO_SERVER_DIR")
    if env:
        return env
    # SERVICE_DIR = <repos>/worktrees/<harness-worktree>/services/chat-stream-v2.
    worktrees_base = SERVICE_DIR.parents[2]   # .../repos/worktrees
    repos_base = SERVICE_DIR.parents[3]       # .../repos
    for cand in (worktrees_base / "cosmo-e2e-server" / "server",  # sibling worktree
                 repos_base / "cosmo" / "server"):                # main cosmo checkout
        if (cand / "cosmo_server" / "app.py").exists():
            return str(cand)
    return None


def build_cosmo(tmp_root: str | os.PathLike[str], *, clock: AdvanceableClock | None = None):
    """Return (TestClient, clock, tokens) for the REAL disposable Cosmo server.

    Raises ImportError if cosmo_server is unavailable (caller skips)."""
    from cosmo_server.app import create_app  # noqa: PLC0415 (optional dep)
    from fastapi.testclient import TestClient  # noqa: PLC0415

    clock = clock or AdvanceableClock()
    tokens = {"phone": "cosmo-e2e-phone", "daff": "cosmo-e2e-daff", "tv": "cosmo-e2e-tv"}
    app = create_app(Path(tmp_root) / "cosmo.db", tokens=tokens, weather_enabled=False, clock=clock)
    return TestClient(app), clock, tokens
