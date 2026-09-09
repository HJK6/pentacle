from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import stat
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping


AUTH_MODE_ENV = "PENTACLE_OPERATOR_AUTH_MODE"
AUTH_MODES = frozenset({"mixed", "enforced"})
AUTH_SCHEME = "hmac-sha256-v2"
AUTH_PROTOCOL_VERSION = 2
AUTH_TRANSCRIPT_PREFIX = b"pentacle-operator-v2\0chat-streamd\0"
AUTH_NONCE_BYTES = 32
AUTH_PROOF_BYTES = 32
AUTH_NONCE_TTL_SECONDS = 10.0
CLIENT_KINDS = frozenset({"pentacle", "pentacle-mobile"})
ENVELOPE_PREFIX = "pentacle-auth-v2:"
REGISTRY_VERSION = 1
REGISTRY_DIR_MODE = 0o700
REGISTRY_FILE_MODE = 0o600
REGISTRY_FIELDS = frozenset({"version", "credentials"})
CREDENTIAL_FIELDS = frozenset(
    {
        "client_kind",
        "proof_key",
        "label",
        "created_at",
        "revoked_at",
        "replaces_credential_id",
    }
)


class OperatorAuthError(ValueError):
    pass


class OperatorRegistryUnavailable(OperatorAuthError):
    pass


@dataclass(frozen=True)
class ConnectionTrust:
    transport: str
    credential_id: str
    client_kind: str
    operator_trusted: bool = True


@dataclass(frozen=True)
class RegistrySnapshot:
    signature: tuple[int, int, int, int, int, int]
    credentials: Mapping[str, Mapping[str, Any]]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise OperatorAuthError("invalid timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise OperatorAuthError("invalid timestamp") from exc
    if parsed.utcoffset() != timedelta(0) or parsed.isoformat() != value:
        raise OperatorAuthError("non-canonical timestamp")
    return value


def auth_mode(value: str | None = None) -> str:
    resolved = str(value if value is not None else os.environ.get(AUTH_MODE_ENV, "mixed")).strip().lower()
    if resolved not in AUTH_MODES:
        raise OperatorAuthError(f"{AUTH_MODE_ENV} must be mixed or enforced")
    return resolved


def encode_b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_b64url(value: object, *, expected_bytes: int | None = None) -> bytes:
    if not isinstance(value, str) or not value or "=" in value:
        raise OperatorAuthError("non-canonical base64url")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise OperatorAuthError("invalid base64url") from exc
    if encode_b64url(raw) != value:
        raise OperatorAuthError("non-canonical base64url")
    if expected_bytes is not None and len(raw) != expected_bytes:
        raise OperatorAuthError("invalid decoded length")
    return raw


def canonical_uuid(value: object) -> str:
    if not isinstance(value, str) or "\0" in value:
        raise OperatorAuthError("invalid credential id")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise OperatorAuthError("invalid credential id") from exc
    if str(parsed) != value:
        raise OperatorAuthError("non-canonical credential id")
    return value


def canonical_client_kind(value: object) -> str:
    if not isinstance(value, str) or value not in CLIENT_KINDS or "\0" in value:
        raise OperatorAuthError("invalid client kind")
    return value


def proof_transcript(nonce: str, credential_id: str, client_kind: str) -> bytes:
    decode_b64url(nonce, expected_bytes=AUTH_NONCE_BYTES)
    canonical_uuid(credential_id)
    canonical_client_kind(client_kind)
    return AUTH_TRANSCRIPT_PREFIX + nonce.encode() + b"\0" + credential_id.encode() + b"\0" + client_kind.encode()


def make_proof(proof_key: bytes, nonce: str, credential_id: str, client_kind: str) -> str:
    if len(proof_key) != AUTH_PROOF_BYTES:
        raise OperatorAuthError("invalid proof key")
    return encode_b64url(hmac.new(proof_key, proof_transcript(nonce, credential_id, client_kind), hashlib.sha256).digest())


def encode_envelope(credential_id: str, client_kind: str, proof_key: bytes) -> str:
    canonical_uuid(credential_id)
    canonical_client_kind(client_kind)
    if len(proof_key) != AUTH_PROOF_BYTES:
        raise OperatorAuthError("invalid proof key")
    payload = {
        "client_kind": client_kind,
        "credential_id": credential_id,
        "proof_key": encode_b64url(proof_key),
        "version": AUTH_PROTOCOL_VERSION,
    }
    encoded = encode_b64url(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    return ENVELOPE_PREFIX + encoded


def decode_envelope(value: object) -> dict[str, Any]:
    if not isinstance(value, str) or not value.startswith(ENVELOPE_PREFIX):
        raise OperatorAuthError("operator auth v2 envelope required")
    raw = decode_b64url(value[len(ENVELOPE_PREFIX) :])
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OperatorAuthError("invalid operator auth envelope") from exc
    if not isinstance(payload, dict) or set(payload) != {"version", "credential_id", "client_kind", "proof_key"}:
        raise OperatorAuthError("invalid operator auth envelope")
    if payload.get("version") != AUTH_PROTOCOL_VERSION:
        raise OperatorAuthError("unsupported operator auth envelope")
    credential_id = canonical_uuid(payload.get("credential_id"))
    client_kind = canonical_client_kind(payload.get("client_kind"))
    proof_key = decode_b64url(payload.get("proof_key"), expected_bytes=AUTH_PROOF_BYTES)
    if encode_envelope(credential_id, client_kind, proof_key) != value:
        raise OperatorAuthError("non-canonical operator auth envelope")
    return {"version": AUTH_PROTOCOL_VERSION, "credential_id": credential_id, "client_kind": client_kind, "proof_key": proof_key}


def _ensure_secure_parent(path: Path) -> None:
    try:
        if not path.parent.exists():
            path.parent.mkdir(parents=True, mode=REGISTRY_DIR_MODE)
        parent_stat = path.parent.lstat()
    except OSError as exc:
        raise OperatorRegistryUnavailable("operator credential directory unavailable") from exc
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or parent_stat.st_uid != os.getuid()
        or stat.S_IMODE(parent_stat.st_mode) != REGISTRY_DIR_MODE
    ):
        raise OperatorRegistryUnavailable("operator credential directory permissions invalid")


def _signature_from_stat(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode)


def read_secure_json(path: Path) -> tuple[Any, tuple[int, int, int, int, int, int]]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except (FileNotFoundError, OSError) as exc:
        raise OperatorRegistryUnavailable("secure store unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != REGISTRY_FILE_MODE:
            raise OperatorRegistryUnavailable("secure store permissions invalid")
        with os.fdopen(descriptor, "r") as handle:
            descriptor = -1
            try:
                payload = json.load(handle)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise OperatorRegistryUnavailable("secure store unreadable") from exc
        return payload, _signature_from_stat(info)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    _ensure_secure_parent(path)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), REGISTRY_FILE_MODE)
    except OSError as exc:
        raise OperatorRegistryUnavailable("secure lock file unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise OperatorRegistryUnavailable("secure lock file invalid")
        os.fchmod(descriptor, REGISTRY_FILE_MODE)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _ensure_secure_parent(path)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, REGISTRY_FILE_MODE)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, REGISTRY_FILE_MODE)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def migrate_owned_secret_file_permissions(path: Path) -> None:
    """One-time control-plane migration for a legacy owner-only secret store."""
    try:
        parent_info = path.parent.lstat()
        if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.getuid():
            raise OperatorRegistryUnavailable("legacy secret directory ownership invalid")
        if path.exists():
            file_info = path.lstat()
            if not stat.S_ISREG(file_info.st_mode) or file_info.st_uid != os.getuid():
                raise OperatorRegistryUnavailable("legacy secret file ownership invalid")
            os.chmod(path, REGISTRY_FILE_MODE)
        os.chmod(path.parent, REGISTRY_DIR_MODE)
    except OSError as exc:
        raise OperatorRegistryUnavailable("legacy secret permission migration failed") from exc


class OperatorCredentialRegistry:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path.home() / ".config/pentacle-stream/operator-credentials.json"
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def initialize(self) -> None:
        with file_lock(self.lock_path):
            if not self.path.exists():
                try:
                    atomic_write_json(self.path, {"version": REGISTRY_VERSION, "credentials": {}})
                except OSError as exc:
                    raise OperatorRegistryUnavailable("operator credential registry initialization failed") from exc

    def load(self) -> RegistrySnapshot:
        _ensure_secure_parent(self.path)
        try:
            payload, signature = read_secure_json(self.path)
        except OperatorRegistryUnavailable as exc:
            raise OperatorRegistryUnavailable("operator credential registry unavailable") from exc
        try:
            credentials = self._validate_payload(payload)
        except OperatorRegistryUnavailable:
            raise
        except (OperatorAuthError, TypeError, ValueError) as exc:
            raise OperatorRegistryUnavailable("operator credential registry schema invalid") from exc
        return RegistrySnapshot(signature=signature, credentials=credentials)

    def _validate_payload(self, payload: object) -> dict[str, dict[str, Any]]:
        if not isinstance(payload, dict) or set(payload) != REGISTRY_FIELDS or payload.get("version") != REGISTRY_VERSION:
            raise OperatorRegistryUnavailable("operator credential registry schema invalid")
        raw_credentials = payload.get("credentials")
        if not isinstance(raw_credentials, dict):
            raise OperatorRegistryUnavailable("operator credential registry schema invalid")
        normalized: dict[str, dict[str, Any]] = {}
        for raw_id, raw_record in raw_credentials.items():
            credential_id = canonical_uuid(raw_id)
            if not isinstance(raw_record, dict) or set(raw_record) != CREDENTIAL_FIELDS:
                raise OperatorRegistryUnavailable("operator credential record schema invalid")
            record = dict(raw_record)
            record["client_kind"] = canonical_client_kind(record.get("client_kind"))
            decode_b64url(record.get("proof_key"), expected_bytes=AUTH_PROOF_BYTES)
            if not isinstance(record.get("label"), str):
                raise OperatorRegistryUnavailable("operator credential record schema invalid")
            record["created_at"] = canonical_timestamp(record.get("created_at"))
            if record.get("revoked_at") is not None:
                record["revoked_at"] = canonical_timestamp(record.get("revoked_at"))
            replacement = record.get("replaces_credential_id")
            if replacement is not None:
                record["replaces_credential_id"] = canonical_uuid(replacement)
            normalized[credential_id] = record
        return normalized

    def _mutate(self, mutation) -> Any:
        with file_lock(self.lock_path):
            if not self.path.exists():
                atomic_write_json(self.path, {"version": REGISTRY_VERSION, "credentials": {}})
            snapshot = self.load()
            credentials = {key: dict(value) for key, value in snapshot.credentials.items()}
            result = mutation(credentials)
            atomic_write_json(self.path, {"version": REGISTRY_VERSION, "credentials": credentials})
            return result

    def issue(self, client_kind: str, *, label: str = "", replaces_credential_id: str | None = None) -> tuple[str, str]:
        client_kind = canonical_client_kind(client_kind)
        if replaces_credential_id is not None:
            replaces_credential_id = canonical_uuid(replaces_credential_id)
        credential_id = str(uuid.uuid4())
        proof_key = secrets.token_bytes(AUTH_PROOF_BYTES)

        def add(credentials: dict[str, dict[str, Any]]) -> None:
            if replaces_credential_id is not None:
                prior = credentials.get(replaces_credential_id)
                if prior is None or prior.get("revoked_at"):
                    raise OperatorAuthError("replacement credential unavailable")
                if prior.get("client_kind") != client_kind:
                    raise OperatorAuthError("replacement client kind mismatch")
            credentials[credential_id] = {
                "client_kind": client_kind,
                "proof_key": encode_b64url(proof_key),
                "label": str(label),
                "created_at": utc_now(),
                "revoked_at": None,
                "replaces_credential_id": replaces_credential_id,
            }

        self._mutate(add)
        return credential_id, encode_envelope(credential_id, client_kind, proof_key)

    def verify(self, credential_id: object, client_kind: object, nonce: object, proof: object) -> ConnectionTrust:
        credential_id = canonical_uuid(credential_id)
        client_kind = canonical_client_kind(client_kind)
        if not isinstance(nonce, str):
            raise OperatorAuthError("invalid nonce")
        supplied = decode_b64url(proof, expected_bytes=AUTH_PROOF_BYTES)
        record = self.load().credentials.get(credential_id)
        if record is None or record.get("revoked_at") or record.get("client_kind") != client_kind:
            raise OperatorAuthError("operator credential invalid")
        proof_key = decode_b64url(record.get("proof_key"), expected_bytes=AUTH_PROOF_BYTES)
        expected = hmac.new(proof_key, proof_transcript(nonce, credential_id, client_kind), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, supplied):
            raise OperatorAuthError("operator credential invalid")
        return ConnectionTrust(transport="v2", credential_id=credential_id, client_kind=client_kind)

    def revoke(self, credential_id: str) -> None:
        credential_id = canonical_uuid(credential_id)

        def revoke_record(credentials: dict[str, dict[str, Any]]) -> None:
            record = credentials.get(credential_id)
            if record is None:
                raise OperatorAuthError("unknown credential")
            if record.get("revoked_at") is None:
                record["revoked_at"] = utc_now()

        self._mutate(revoke_record)

    def delete(self, credential_id: str) -> None:
        credential_id = canonical_uuid(credential_id)

        def delete_record(credentials: dict[str, dict[str, Any]]) -> None:
            record = credentials.get(credential_id)
            if record is None:
                raise OperatorAuthError("unknown credential")
            if record.get("revoked_at") is None:
                raise OperatorAuthError("credential must be revoked before deletion")
            credentials.pop(credential_id)

        self._mutate(delete_record)

    def activate_replacement(self, credential_id: str) -> str | None:
        credential_id = canonical_uuid(credential_id)
        replaced: str | None = None

        def activate(credentials: dict[str, dict[str, Any]]) -> None:
            nonlocal replaced
            record = credentials.get(credential_id)
            if record is None or record.get("revoked_at"):
                raise OperatorAuthError("operator credential invalid")
            prior_id = record.get("replaces_credential_id")
            prior = credentials.get(prior_id) if prior_id else None
            if prior is not None and prior.get("revoked_at") is None:
                prior["revoked_at"] = utc_now()
                replaced = str(prior_id)

        self._mutate(activate)
        return replaced


def new_nonce() -> tuple[str, float]:
    return encode_b64url(secrets.token_bytes(AUTH_NONCE_BYTES)), time.time() + AUTH_NONCE_TTL_SECONDS


def credential_fingerprint(credential_id: str) -> str:
    return hashlib.sha256(credential_id.encode()).hexdigest()[:12]
