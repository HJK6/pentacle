"""The one supported authenticated activation and teardown path.

The daemon's operator credential is intentionally broad.  This module narrows
that authority locally: only a session whose spawn request, idempotency key,
name, stream id, and observed generation are in the persisted owned registry
can reach a close frame.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import time
from typing import Any, Callable, Iterable
import uuid

from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosed

from _shared import operator_auth


_PUSH_TYPES = frozenset({
    "chat.event", "child_report_ready", "host.status", "hosts.stats",
    "limits.update", "notification", "schedule.inventory", "session.died",
    "session.inventory", "snapshot", "specs.changed", "updates", "welcome",
    "working.state",
})


class LiveWindowError(RuntimeError):
    """A supported live-window operation could not establish its contract."""


class OwnershipError(LiveWindowError):
    """A close target was not proven to be owned by this activation window."""


class TeardownError(LiveWindowError):
    """One or more library-owned sessions could not be proved gone."""


@dataclass(frozen=True)
class OwnedSession:
    request_id: str
    idempotency_key: str
    stream_id: str
    host: str
    session_name: str
    session_generation: str
    # A legacy daemon cannot atomically compare this generation at close.  The
    # fallback is only legal for a name derived from this activation's UUID.
    legacy_unique_name: bool = False


def _generation_close_supported(snapshot: dict[str, Any] | None) -> bool:
    """Return only an explicit protocol promise, never a version inference."""
    capabilities = snapshot.get("capabilities") if isinstance(snapshot, dict) else None
    return isinstance(capabilities, dict) and capabilities.get("close_expected_generation") is True


def _is_legacy_unique_name(session_name: str, idempotency_key: str) -> bool:
    """Require a UUID-derived suffix before admitting the legacy fallback."""
    normalized = "".join(char for char in idempotency_key.lower() if char.isalnum())
    return len(normalized) >= 12 and session_name.lower().endswith(normalized[:12])


class OwnedSessionRegistry:
    """Persist and close only sessions bound to a successful activation.

    ``LiveWindow`` uses a concrete daemon connection and local durable/pane
    predicates.  Some callers (notably fleet smoke) need a richer observation
    loop over the same authenticated connection.  This adapter gives those
    callers the *same* ownership fence without allowing them to recreate a
    close frame or a retry-close path themselves.
    """

    def __init__(self, state_path: Path, *, allow_legacy_close: bool = False) -> None:
        self.state_path = state_path
        self.allow_legacy_close = allow_legacy_close
        self._owned: dict[str, OwnedSession] = {}
        self._closed: set[str] = set()
        self._generation_close_supported: bool | None = None

    @classmethod
    def recover(
        cls, state_path: Path, *, allow_legacy_close: bool = False,
    ) -> "OwnedSessionRegistry":
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        if raw.get("version") != 1 or not isinstance(raw.get("owned"), list):
            raise OwnershipError("owned-session checkpoint is invalid")
        registry = cls(state_path, allow_legacy_close=allow_legacy_close)
        for item in raw["owned"]:
            try:
                owned = OwnedSession(**item)
            except (TypeError, ValueError) as exc:
                raise OwnershipError("owned-session checkpoint contains an invalid entry") from exc
            registry._owned[owned.stream_id] = owned
        return registry

    def configure_runtime(self, snapshot: dict[str, Any] | None) -> None:
        """Bind this registry to the authenticated runtime capability frame."""
        supported = _generation_close_supported(snapshot)
        if (
            self._generation_close_supported is not None
            and self._generation_close_supported is not supported
        ):
            raise OwnershipError("authenticated runtime close capability changed during one window")
        self._generation_close_supported = supported

    def prepare_spawn(self, frozen_payload: dict[str, object]) -> None:
        """Fail before activation if teardown could not be safely authorized."""
        if self._generation_close_supported is None:
            raise OwnershipError("runtime close capability was not authenticated before spawn")
        if self._generation_close_supported:
            return
        if not self.allow_legacy_close:
            raise OwnershipError(
                "legacy runtime lacks generation-fenced close; pass explicit allow_legacy_close",
            )
        session_name = str(frozen_payload.get("session_name") or "").strip()
        idempotency_key = str(frozen_payload.get("idempotency_key") or "").strip()
        if not _is_legacy_unique_name(session_name, idempotency_key):
            raise OwnershipError(
                "legacy runtime requires a UUID-derived unique session name before spawn",
            )

    def register_spawn(
        self,
        frozen_payload: dict[str, object],
        reply: dict[str, object],
        inventory_row: dict[str, object],
    ) -> OwnedSession:
        """Bind a spawn reply to its exact authoritative inventory identity."""
        self.prepare_spawn(frozen_payload)
        host = str(frozen_payload.get("host") or "").strip()
        session_name = str(frozen_payload.get("session_name") or "").strip()
        request_id = str(frozen_payload.get("request_id") or "").strip()
        idempotency_key = str(frozen_payload.get("idempotency_key") or "").strip()
        stream_id = str(reply.get("stream_id") or "").strip()
        expected_stream_id = f"{host}:{session_name}"
        if not all((host, session_name, request_id, idempotency_key)) or ":" in session_name:
            raise OwnershipError("owned spawn is missing immutable request identity")
        if stream_id != expected_stream_id:
            raise OwnershipError(
                f"spawn returned unbound stream_id {stream_id!r}, expected {expected_stream_id!r}",
            )
        if (
            str(inventory_row.get("stream_id") or "") != stream_id
            or str(inventory_row.get("host") or "") != host
            or str(inventory_row.get("session_name") or "") != session_name
        ):
            raise OwnershipError(f"spawn inventory row does not bind {stream_id}")
        generation = str(inventory_row.get("session_generation") or "").strip()
        if not generation:
            raise OwnershipError(f"owned spawn has no session_generation: {stream_id}")
        owned = OwnedSession(
            request_id=request_id,
            idempotency_key=idempotency_key,
            stream_id=stream_id,
            host=host,
            session_name=session_name,
            session_generation=generation,
            legacy_unique_name=not self._generation_close_supported,
        )
        self._owned[stream_id] = owned
        self.checkpoint()
        return owned

    def close(
        self,
        target: OwnedSession | str,
        *,
        inventory: Callable[[float | None], list[dict[str, object]]],
        send_close: Callable[[dict[str, object]], dict[str, object]],
        wait_gone: Callable[[OwnedSession, float], bool],
        timeout: float,
    ) -> None:
        """Close with a generation fence, then one library-owned escalation."""
        owned = self._owned_target(target)
        if owned.stream_id in self._closed:
            return
        self._assert_close_protocol(owned)
        try:
            self._assert_current_generation(owned, inventory(None))
        except OwnershipError:
            if self._mark_if_gone(owned, wait_gone, timeout):
                return
            raise
        try:
            self._send_close(owned, send_close)
        except (ConnectionError, OSError, ConnectionClosed):
            # The daemon may have committed the close before its reply was
            # dropped.  A caller's fresh authenticated rescue re-enters here;
            # resolve only a proven-gone target, never issue a blind retry.
            if self._mark_if_gone(owned, wait_gone, timeout):
                return
            raise
        deadline = time.monotonic() + timeout
        if not wait_gone(owned, deadline):
            try:
                self._assert_current_generation(owned, inventory(None))
            except OwnershipError:
                if self._mark_if_gone(owned, wait_gone, timeout):
                    return
                raise
            self._send_close(owned, send_close)
            if not wait_gone(owned, time.monotonic() + timeout):
                raise TeardownError(
                    f"owned close did not satisfy its teardown predicate: {owned.stream_id}",
                )
        self._mark_closed(owned)

    def checkpoint(self) -> None:
        _write_json(self.state_path, {
            "version": 1,
            "owned": [asdict(item) for item in self._owned.values() if item.stream_id not in self._closed],
        })

    def _owned_target(self, target: OwnedSession | str) -> OwnedSession:
        stream_id = target.stream_id if isinstance(target, OwnedSession) else str(target)
        owned = self._owned.get(stream_id)
        if owned is None or (isinstance(target, OwnedSession) and target != owned):
            raise OwnershipError(f"close denied for unowned session: {stream_id!r}")
        return owned

    def _assert_close_protocol(self, owned: OwnedSession) -> None:
        if self._generation_close_supported is True:
            return
        if self._generation_close_supported is None:
            raise OwnershipError("runtime close capability was not authenticated")
        if not self.allow_legacy_close or not owned.legacy_unique_name:
            raise OwnershipError("legacy runtime close requires explicit unique-name fallback")

    def _mark_if_gone(
        self,
        owned: OwnedSession,
        wait_gone: Callable[[OwnedSession, float], bool],
        timeout: float,
    ) -> bool:
        if not wait_gone(owned, time.monotonic() + timeout):
            return False
        self._mark_closed(owned)
        return True

    def _mark_closed(self, owned: OwnedSession) -> None:
        self._closed.add(owned.stream_id)
        self.checkpoint()

    @staticmethod
    def _assert_current_generation(owned: OwnedSession, rows: list[dict[str, object]]) -> None:
        current = next(
            (row for row in rows if str(row.get("stream_id") or "") == owned.stream_id), None,
        )
        if current is None:
            raise OwnershipError(f"close denied: owned session no longer open: {owned.stream_id}")
        if (
            str(current.get("host") or "") != owned.host
            or str(current.get("session_name") or "") != owned.session_name
            or str(current.get("session_generation") or "") != owned.session_generation
        ):
            raise OwnershipError(f"close denied: target generation changed: {owned.stream_id}")

    @staticmethod
    def _send_close(
        owned: OwnedSession,
        send_close: Callable[[dict[str, object]], dict[str, object]],
    ) -> None:
        reply = send_close({
            "type": "close",
            "host": owned.host,
            "session_name": owned.session_name,
            "operator_confirm": True,
            # Runtime support for an atomic fence is tracked separately.  The
            # inventory check immediately before this send is fail-closed here.
            "expected_generation": owned.session_generation,
        })
        if str(reply.get("type") or "").endswith((".error", ".failed")):
            raise TeardownError(f"owned close failed for {owned.stream_id}: {reply!r}")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def operator_hello(token_path: Path, welcome: dict[str, Any]) -> dict[str, object]:
    """Build the only supported authenticated operator hello frame."""
    envelope = token_path.read_text(encoding="utf-8").strip()
    credential = operator_auth.decode_envelope(envelope)
    if credential.get("client_kind") != "pentacle":
        raise LiveWindowError("live-window credential must be pentacle client kind")
    try:
        nonce = str(welcome["auth"]["operator"]["nonce"])
    except (KeyError, TypeError) as exc:
        raise LiveWindowError("operator nonce is unavailable") from exc
    return {
        "type": "hello",
        "client": "pentacle",
        "subscribe": {"include_subagents": True, "events_mode": "summary"},
        "auth_v2": {
            "scheme": operator_auth.AUTH_SCHEME,
            "credential_id": credential["credential_id"],
            "proof": operator_auth.make_proof(
                credential["proof_key"], nonce, credential["credential_id"], "pentacle",
            ),
        },
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def write_receipt(
    receipt_path: Path,
    *,
    candidate_sha: str,
    runtime_pid: int,
    helper_path: Path,
    evidence_paths: Iterable[str],
    payload: dict[str, Any],
) -> None:
    """Write a durable receipt that independently binds executable identity."""
    _write_json(receipt_path, {
        "candidate_sha": candidate_sha,
        "runtime_pid": runtime_pid,
        "helper_path": str(helper_path),
        "helper_sha256": _sha256(helper_path),
        "evidence_paths": list(evidence_paths),
        "written_at": _utc_now(),
        **payload,
    })


class _OperatorConnection(AbstractContextManager["_OperatorConnection"]):
    """A nonce-proof authenticated socket with request correlation."""

    def __init__(
        self,
        url: str,
        token_path: Path,
        timeout: float,
        sent_frame_observer: Callable[[dict[str, object]], None] | None,
    ) -> None:
        self.url = url
        self.token_path = token_path
        self.timeout = timeout
        self.sent_frame_observer = sent_frame_observer
        self._socket: Any | None = None
        self.snapshot: dict[str, Any] | None = None

    @property
    def socket(self) -> Any:
        if self._socket is None:
            raise ConnectionError("operator connection is closed")
        return self._socket

    def __enter__(self) -> "_OperatorConnection":
        self._socket = connect(self.url, open_timeout=self.timeout, close_timeout=self.timeout)
        welcome = self._receive(self.timeout)
        if welcome.get("type") != "welcome":
            raise LiveWindowError(f"operator connection expected welcome: {welcome!r}")
        hello = operator_hello(self.token_path, welcome)
        self._send(hello)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            frame = self._receive(max(0.05, deadline - time.monotonic()))
            if frame.get("type") == "hello.error":
                raise LiveWindowError(f"operator nonce proof rejected: {frame!r}")
            if frame.get("type") == "snapshot":
                self.snapshot = frame
                return self
        raise LiveWindowError("operator handshake did not yield a snapshot")

    def __exit__(self, *_unused: object) -> None:
        self.close()

    def close(self) -> None:
        if self._socket is not None:
            closer = getattr(self._socket, "close", None)
            if closer is not None:
                closer()
            elif hasattr(self._socket, "__exit__"):
                self._socket.__exit__(None, None, None)
            self._socket = None

    def _send(self, payload: dict[str, object]) -> None:
        if self._socket is None:
            raise ConnectionError("operator connection is closed")
        if self.sent_frame_observer is not None:
            self.sent_frame_observer(dict(payload))
        self._socket.send(json.dumps(payload))

    def _receive(self, timeout: float) -> dict[str, Any]:
        if self._socket is None:
            raise ConnectionError("operator connection is closed")
        frame = json.loads(self._socket.recv(timeout=timeout))
        if not isinstance(frame, dict):
            raise LiveWindowError("operator connection received a non-object frame")
        return frame

    def rpc(self, payload: dict[str, object]) -> dict[str, Any]:
        request_id = str(payload.get("request_id") or uuid.uuid4())
        request = {**payload, "request_id": request_id}
        self._send(request)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            frame = self._receive(max(0.05, deadline - time.monotonic()))
            if frame.get("type") in _PUSH_TYPES:
                continue
            if frame.get("request_id") != request_id:
                continue
            return frame
        raise TimeoutError(f"operator rpc timed out: {payload.get('type')}")


@contextmanager
def authenticated_operator_connection(
    url: str,
    token_path: Path,
    timeout: float,
) -> Any:
    """Yield one nonce-proof authenticated raw socket plus its snapshot.

    Legacy callers with specialized observation loops use this boundary instead
    of reimplementing the nonce/proof handshake.  New callers should use
    :class:`LiveWindow` so registry and teardown guarantees apply as well.
    """
    connection = _OperatorConnection(url, token_path, timeout, None)
    with connection:
        yield connection


class LiveWindow(AbstractContextManager["LiveWindow"]):
    """Own and tear down only the sessions activated through this instance."""

    def __init__(
        self,
        *,
        url: str,
        token_path: Path,
        timeout: float,
        state_path: Path,
        receipt_path: Path,
        session_db: Path,
        tmux_bin: str,
        candidate_sha: str,
        runtime_pid: int,
        helper_path: Path,
        evidence_paths: Iterable[str],
        sent_frame_observer: Callable[[dict[str, object]], None] | None = None,
        allow_legacy_close: bool = False,
    ) -> None:
        self.url = url
        self.token_path = token_path
        self.timeout = timeout
        self.state_path = state_path
        self.receipt_path = receipt_path
        self.session_db = session_db
        self.tmux_bin = tmux_bin
        self.candidate_sha = candidate_sha
        self.runtime_pid = runtime_pid
        self.helper_path = helper_path
        self.evidence_paths = list(evidence_paths)
        self.sent_frame_observer = sent_frame_observer
        self.allow_legacy_close = allow_legacy_close
        self._connection: _OperatorConnection | None = None
        self._owned: dict[str, OwnedSession] = {}
        self._closed: set[str] = set()
        self._session_receipts: list[dict[str, Any]] = []
        self._recovery = {"fresh_authenticated_connection": False, "orphan_checkpoint_loaded": 0}
        self._teardown_failures: list[str] = []

    def __enter__(self) -> "LiveWindow":
        return self.open()

    def __exit__(self, body_type: object, body: object, _traceback: object) -> None:
        try:
            self._teardown_failures = self.teardown()
            if self._teardown_failures and body_type is None:
                raise TeardownError("; ".join(self._teardown_failures))
        finally:
            self._write_receipt()
            self._close_connection()

    def open(self) -> "LiveWindow":
        if self._connection is None:
            connection = _OperatorConnection(
                self.url, self.token_path, self.timeout, self.sent_frame_observer,
            )
            self._connection = connection.__enter__()
        return self

    @classmethod
    def recover(cls, *, state_path: Path, **kwargs: Any) -> "LiveWindow":
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        if raw.get("version") != 1 or not isinstance(raw.get("owned"), list):
            raise OwnershipError("owned-session checkpoint is invalid")
        window = cls(state_path=state_path, **kwargs)
        for item in raw["owned"]:
            try:
                owned = OwnedSession(**item)
            except (TypeError, ValueError) as exc:
                raise OwnershipError("owned-session checkpoint contains an invalid entry") from exc
            window._owned[owned.stream_id] = owned
        window._recovery["orphan_checkpoint_loaded"] = len(window._owned)
        return window

    def spawn(self, frozen_payload: dict[str, object]) -> OwnedSession:
        host = str(frozen_payload.get("host") or "").strip()
        session_name = str(frozen_payload.get("session_name") or "").strip()
        if not host or not session_name or ":" in session_name:
            raise OwnershipError("spawn payload must name one valid host and session_name")
        request_id = str(frozen_payload.get("request_id") or uuid.uuid4())
        idempotency_key = str(frozen_payload.get("idempotency_key") or uuid.uuid4())
        legacy_unique_name = self._assert_activation_protocol(
            host, session_name, idempotency_key,
        )
        reply = self._rpc({
            **frozen_payload,
            "type": "spawn",
            "request_id": request_id,
            "idempotency_key": idempotency_key,
        })
        if reply.get("type") != "spawn.ok":
            raise LiveWindowError(f"owned spawn failed: {reply!r}")
        stream_id = str(reply.get("stream_id") or "")
        expected_stream_id = f"{host}:{session_name}"
        if stream_id != expected_stream_id:
            raise OwnershipError(
                f"spawn returned unbound stream_id {stream_id!r}, expected {expected_stream_id!r}",
            )
        row = self._wait_for_open_row(stream_id)
        generation = str(row.get("session_generation") or "").strip()
        if not generation:
            raise OwnershipError(f"owned spawn has no session_generation: {stream_id}")
        owned = OwnedSession(
            request_id=request_id,
            idempotency_key=idempotency_key,
            stream_id=stream_id,
            host=host,
            session_name=session_name,
            session_generation=generation,
            legacy_unique_name=legacy_unique_name,
        )
        self._owned[stream_id] = owned
        self.checkpoint_owned_sessions()
        return owned

    def close(self, target: OwnedSession | str) -> None:
        owned = self._owned_target(target)
        if owned.stream_id in self._closed:
            return
        self._assert_close_protocol(owned)
        try:
            self._assert_current_owned_generation(owned)
        except OwnershipError:
            if self._mark_if_proven_gone(owned):
                return
            raise
        try:
            reply = self._rpc({
                "type": "close",
                "host": owned.host,
                "session_name": owned.session_name,
                "operator_confirm": True,
                "expected_generation": owned.session_generation,
            })
        except (ConnectionError, OSError, ConnectionClosed):
            # A reply may be lost after the runtime has completed its CAS close.
            # Re-authenticate and accept only the durable closed-row + physical
            # tmux-absence predicate; otherwise surface the transport error for
            # teardown's one explicit rescue attempt.
            self._close_connection()
            self._recovery["fresh_authenticated_connection"] = True
            self.open()
            if self._mark_if_proven_gone(owned):
                return
            raise
        if str(reply.get("type") or "").endswith((".error", ".failed")):
            raise TeardownError(f"owned close failed for {owned.stream_id}: {reply!r}")
        if not self._mark_if_proven_gone(owned):
            raise TeardownError(f"teardown predicate failed for {owned.stream_id}")

    def _mark_if_proven_gone(self, owned: OwnedSession) -> bool:
        closed_row, tmux_absent = self._wait_for_gone(owned)
        if not (closed_row and tmux_absent):
            return False
        self._mark_closed(owned, closed_row=closed_row, tmux_absent=tmux_absent)
        return True

    def _mark_closed(self, owned: OwnedSession, *, closed_row: bool, tmux_absent: bool) -> None:
        if owned.stream_id in self._closed:
            return
        self._closed.add(owned.stream_id)
        self._session_receipts.append({
            "stream_id": owned.stream_id,
            "request_id": owned.request_id,
            "idempotency_key": owned.idempotency_key,
            "session_generation": owned.session_generation,
            "closed_row": closed_row,
            "tmux_absent": tmux_absent,
            "legacy_unique_name": owned.legacy_unique_name,
        })
        self.checkpoint_owned_sessions()

    def checkpoint_owned_sessions(self) -> None:
        _write_json(self.state_path, {
            "version": 1,
            "owned": [asdict(item) for item in self._owned.values() if item.stream_id not in self._closed],
        })

    def drop_primary_connection_for_recovery_test(self) -> None:
        """Deliberately sever this client socket; next teardown must rescue fresh."""
        if self._connection is not None:
            self._connection.close()

    def teardown(self) -> list[str]:
        failures: list[str] = []
        for owned in tuple(self._owned.values()):
            if owned.stream_id in self._closed:
                continue
            try:
                self.close(owned)
            except (ConnectionError, OSError, ConnectionClosed) as exc:
                self._close_connection()
                self._recovery["fresh_authenticated_connection"] = True
                try:
                    self.open()
                    self.close(owned)
                except Exception as rescue_error:  # noqa: BLE001 - receipt preserves both causes
                    failures.append(f"{owned.stream_id}: primary={exc!r}; rescue={rescue_error!r}")
            except Exception as exc:  # noqa: BLE001 - teardown must report every owned target
                failures.append(f"{owned.stream_id}: {exc!r}")
        return failures

    def _rpc(self, payload: dict[str, object]) -> dict[str, Any]:
        self.open()
        assert self._connection is not None
        return self._connection.rpc(payload)

    def _close_connection(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _owned_target(self, target: OwnedSession | str) -> OwnedSession:
        stream_id = target.stream_id if isinstance(target, OwnedSession) else str(target)
        owned = self._owned.get(stream_id)
        if owned is None or (isinstance(target, OwnedSession) and target != owned):
            raise OwnershipError(f"close denied for unowned session: {stream_id!r}")
        return owned

    def _runtime_generation_close_supported(self) -> bool:
        self.open()
        assert self._connection is not None
        return _generation_close_supported(self._connection.snapshot)

    def _assert_activation_protocol(
        self, host: str, session_name: str, idempotency_key: str,
    ) -> bool:
        """Return whether this activation uses the documented legacy fallback."""
        if self._runtime_generation_close_supported():
            return False
        if not self.allow_legacy_close:
            raise OwnershipError(
                "legacy runtime lacks generation-fenced close; pass explicit allow_legacy_close",
            )
        if not _is_legacy_unique_name(session_name, idempotency_key):
            raise OwnershipError(
                "legacy runtime requires a UUID-derived unique session name before spawn",
            )
        if not self.session_db.exists():
            raise OwnershipError("legacy runtime requires an accessible session database for name uniqueness")
        try:
            with closing(sqlite3.connect(f"file:{self.session_db}?mode=ro", uri=True)) as connection:
                seen = connection.execute(
                    "SELECT 1 FROM sessions WHERE host=? AND session_name=? LIMIT 1",
                    (host, session_name),
                ).fetchone()
        except sqlite3.Error as exc:
            raise OwnershipError(
                "legacy runtime requires a readable session database for name uniqueness",
            ) from exc
        if seen is not None:
            raise OwnershipError("legacy runtime unique session name has prior durable history")
        return True

    def _assert_close_protocol(self, owned: OwnedSession) -> None:
        if self._runtime_generation_close_supported():
            return
        if not self.allow_legacy_close or not owned.legacy_unique_name:
            raise OwnershipError("legacy runtime close requires explicit unique-name fallback")

    def _list_sessions(self) -> list[dict[str, Any]]:
        reply = self._rpc({"type": "list_sessions"})
        active = reply.get("active")
        if reply.get("type") != "list_sessions.ok" or not isinstance(active, list):
            raise LiveWindowError(f"invalid authoritative inventory: {reply!r}")
        if not all(isinstance(row, dict) for row in active):
            raise LiveWindowError("authoritative inventory contains a malformed row")
        return active

    def _wait_for_open_row(self, stream_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            for row in self._list_sessions():
                if str(row.get("stream_id") or "") == stream_id:
                    return row
            time.sleep(0.05)
        raise OwnershipError(f"spawned session is missing from authoritative inventory: {stream_id}")

    def _assert_current_owned_generation(self, owned: OwnedSession) -> None:
        current = next(
            (row for row in self._list_sessions() if str(row.get("stream_id") or "") == owned.stream_id),
            None,
        )
        if current is None:
            raise OwnershipError(f"close denied: owned session no longer open: {owned.stream_id}")
        if str(current.get("host") or "") != owned.host or str(current.get("session_name") or "") != owned.session_name:
            raise OwnershipError(f"close denied: target identity changed: {owned.stream_id}")
        if str(current.get("session_generation") or "") != owned.session_generation:
            raise OwnershipError(f"close denied: target generation changed: {owned.stream_id}")

    def _wait_for_gone(self, owned: OwnedSession) -> tuple[bool, bool]:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            inventory_gone = not any(
                str(row.get("stream_id") or "") == owned.stream_id
                for row in self._list_sessions()
            )
            closed_row = self._closed_row(owned)
            tmux_absent = self._tmux_absent(owned)
            if inventory_gone and closed_row and tmux_absent:
                return closed_row, tmux_absent
            time.sleep(0.05)
        return self._closed_row(owned), self._tmux_absent(owned)

    def _closed_row(self, owned: OwnedSession) -> bool:
        if not self.session_db.exists():
            return False
        try:
            with closing(sqlite3.connect(f"file:{self.session_db}?mode=ro", uri=True)) as connection:
                row = connection.execute(
                    "SELECT status FROM sessions WHERE host=? AND session_name=?",
                    (owned.host, owned.session_name),
                ).fetchone()
        except sqlite3.Error:
            return False
        return bool(row and row[0] == "closed")

    def _tmux_absent(self, owned: OwnedSession) -> bool:
        result = subprocess.run(
            [self.tmux_bin, "has-session", "-t", f"={owned.session_name}:"],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 1

    def _write_receipt(self) -> None:
        runtime_support = (
            _generation_close_supported(self._connection.snapshot)
            if self._connection is not None else None
        )
        write_receipt(
            self.receipt_path,
            candidate_sha=self.candidate_sha,
            runtime_pid=self.runtime_pid,
            helper_path=self.helper_path,
            evidence_paths=self.evidence_paths,
            payload={
                "runtime_generation_close_supported": runtime_support,
                "legacy_close_residual_race": any(
                    item.legacy_unique_name for item in self._owned.values()
                ),
                "recovery": self._recovery,
                "sessions": self._session_receipts,
                "teardown": {
                    "expected": len(self._owned),
                    "closed": len(self._closed),
                    "failures": self._teardown_failures,
                },
            },
        )
