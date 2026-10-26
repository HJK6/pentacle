"""Identity-first credential resolution at the client's websocket egress.

Builders record credential *policies*, never credential values. Frame keeps
caller inputs out of the queued wire dictionary. Its small ordered edit list
preserves the existing seat/operator wire layout while evaluating loaders anew
at every send, including retries and persistent queue replay.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any, Mapping
from collections.abc import Mapping as MappingABC

from .config import Config

FIXED_SERVICE_IDENTITIES = {
    "altum-bot-cd": ("file", "AGENT_ORCH_STREAM_TOKEN_FILE"),
    "amaterasu:wmi-pg-dailybackup": ("file", "AGENT_ORCH_STREAM_TOKEN_FILE"),
    "work-report-reader": ("env", "PENTACLE_WORK_REPORT_READER_TOKEN"),
}
IDENTITY_ENV = ("AGENT_ORCH_INTERNAL_LEADER_STREAM_ID", "AGENT_ORCH_STREAM_ID", "PENTACLE_STREAM_ID")
CREDENTIAL_KEYS = frozenset({"token", "stream_token", "identity_token"})


@dataclass(frozen=True)
class OutboundIdentity:
    kind: str
    stream_id: str | None


def resolve_outbound_identity(explicit_from_stream_id: str | None = None,
                              environ: Mapping[str, str] | None = None) -> OutboundIdentity:
    environ = os.environ if environ is None else environ
    sources = [value for value in [explicit_from_stream_id, *(environ.get(name) for name in IDENTITY_ENV)] if value]
    services = [value for value in sources if value in FIXED_SERVICE_IDENTITIES]
    if services:
        service = services[0]
        return OutboundIdentity("ambiguous" if any(value != service for value in sources) else "service", service)
    return OutboundIdentity("seat", sources[0]) if sources else OutboundIdentity("operator", None)


class ServiceCredentialUnavailable(Exception):
    def __init__(self, identity: OutboundIdentity):
        # Paths are configuration metadata. Do not interpolate caller input or
        # loader exceptions (which can include credential contents).
        source = FIXED_SERVICE_IDENTITIES.get(identity.stream_id, ("file", "AGENT_ORCH_STREAM_TOKEN_FILE"))
        path = os.environ.get(source[1], "<unset>") if source[0] == "file" else source[1]
        self.identity = identity
        self.source = path
        super().__init__(f"service credential unavailable: identity={identity.stream_id or '<ambiguous>'} source={json.dumps(path)}")


class SeatCredentialUnavailable(Exception):
    pass


@dataclass(frozen=True)
class ResolvedOperatorToken:
    kind: str
    value: str = field(repr=False)
    form: str = "bearer"


def _token_path() -> Path:
    return Path("~/.config/pentacle-stream/token").expanduser()


def _file_token(path: Path | None = None) -> str | None:
    path = _token_path() if path is None else path
    return path.read_text(encoding="utf-8").strip() if path.exists() else None


def resolve_operator_token(config: Config) -> ResolvedOperatorToken:
    source = config.operator_token_source
    if source is None:
        kind, value = "explicit", config.token
    elif source.env_present:
        kind, value = "env", os.environ.get(source.env_name, "")
    else:
        loaded = _file_token(source.path)
        kind, value = ("file", loaded) if loaded is not None else ("none", "")
    # Recognizing the wire prefix must not make ordinary standalone CLI
    # installations depend on the optional dashboard proof implementation.
    return ResolvedOperatorToken(kind, value, "envelope" if value.startswith("pentacle-auth-v2:") else "bearer")


def _stream_token_from_file() -> str | None:
    token_file = os.environ.get("AGENT_ORCH_STREAM_TOKEN_FILE")
    if not token_file:
        return None
    try:
        path = Path(token_file).expanduser()
        info = path.stat()
        if not path.is_file() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return None
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError, UnicodeError):
        return None
    return value or None


def stream_token_from_env() -> str | None:
    if os.environ.get("AGENT_ORCH_STREAM_TOKEN_FILE"):
        return _stream_token_from_file()
    return os.environ.get("AGENT_ORCH_STREAM_TOKEN") or None


def seat_credential_source_present() -> bool:
    """Presence preflight only; the boundary decides whether it is usable."""
    return "AGENT_ORCH_STREAM_TOKEN_FILE" in os.environ or "AGENT_ORCH_STREAM_TOKEN" in os.environ


def configured_token_file_paths() -> list[Path]:
    """Metadata for attachment path refusal; never open a credential file."""
    value = os.environ.get("AGENT_ORCH_STREAM_TOKEN_FILE")
    return [Path(value).expanduser()] if value else []


def reader_identity_ready(explicit: str | None) -> OutboundIdentity:
    identity = resolve_outbound_identity(explicit)
    if identity.kind == "ambiguous":
        raise ServiceCredentialUnavailable(identity)
    if identity.kind == "service" and identity.stream_id == "work-report-reader":
        value = os.environ.get("PENTACLE_WORK_REPORT_READER_TOKEN")
        if not value or not value.strip():
            raise ServiceCredentialUnavailable(identity)
    return identity


def operator_auth_v2_hello_fields(resolved: ResolvedOperatorToken, challenge: dict) -> dict:
    from _shared import operator_auth
    credential = operator_auth.decode_envelope(resolved.value)
    return {"client": credential["client_kind"], "auth_v2": {
        "scheme": operator_auth.AUTH_SCHEME, "credential_id": credential["credential_id"],
        "proof": operator_auth.make_proof(credential["proof_key"], challenge.get("nonce"),
                                           credential["credential_id"], credential["client_kind"]),
    }}


class CallerInputs(MappingABC):
    """Keep caller input references opaque until the ordinary identity branch."""
    def __init__(self, source, keys):
        self.source = source
        self.keys = tuple(keys)

    def __iter__(self):
        return iter(self.keys)

    def __len__(self):
        return len(self.keys)

    def __getitem__(self, key):
        if key not in self.keys:
            raise KeyError(key)
        return self.source[key]


class Frame(dict):
    """A credential-free queued frame with non-serialized credential policy."""
    def __init__(self, payload=()):
        # Do not even read a supplied credential while building the wire
        # dictionary. Only the ordinary branch dereferences CallerInputs.
        super().__init__((key, payload[key]) for key in payload if key not in CREDENTIAL_KEYS)
        self.origin = payload.origin if isinstance(payload, Frame) else payload if isinstance(payload, dict) else None
        self.edits = []
        self.caller = CallerInputs(payload, [key for key in payload if key in CREDENTIAL_KEYS])
        self.hello_style = None
        self.explicit_identity = None
        self.infer_identity = True
        self.identity_token = None
        self.infer_identity_token = True
        self.require_seat_token = False
        if isinstance(payload, Frame):
            self.edits = list(payload.edits)
            self.caller = payload.caller
            for name in ("hello_style", "explicit_identity", "infer_identity", "identity_token", "infer_identity_token", "require_seat_token"):
                setattr(self, name, getattr(payload, name))
        else:
            before = []
            for key in payload:
                if key in CREDENTIAL_KEYS:
                    self.edits.append((tuple(before), "caller", key, None, False))
                before.append(key)

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if self.origin is not None and key not in CREDENTIAL_KEYS:
            self.origin[key] = value

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]


def prepare_frame(payload: dict) -> Frame:
    return payload if isinstance(payload, Frame) else Frame(payload)


def defer_stream_token(payload: dict, *, enabled=True, keep=True,
                       from_stream_id=None, identity_with_token=False, require=False) -> Frame:
    frame = prepare_frame(payload)
    if enabled:
        frame.edits.append((tuple(frame) + tuple(frame.caller), "stream", keep, from_stream_id, identity_with_token))
    frame.require_seat_token |= require
    return frame


def hello_frame(payload: dict, style: str, explicit=None, *, infer_identity=True,
                identity_token=None, infer_identity_token=True) -> Frame:
    frame = prepare_frame(payload)
    frame.hello_style = style
    frame.explicit_identity = explicit
    frame.infer_identity = infer_identity
    frame.identity_token = identity_token
    frame.infer_identity_token = infer_identity_token
    return frame


def caller_stream_token(frame: Frame) -> str | None:
    """Pass a caller credential between plans without putting it in a payload."""
    if resolve_outbound_identity(frame.get("from_stream_id")).kind in {"service", "ambiguous"}:
        return None
    return frame.caller.get("stream_token")


def _insert(frame: dict, before: tuple, fields: dict) -> dict:
    # Preserve the exact insertion order of the old builders, including keys
    # appended after the former attachment point (notably request_id).
    result = {}
    inserted = False
    for key, value in frame.items():
        if not inserted and key not in before:
            result.update((k, v) for k, v in fields.items() if k not in frame)
            inserted = True
        result[key] = fields.get(key, value)
    if not inserted:
        result.update((k, v) for k, v in fields.items() if k not in result)
    return result


def _ordinary_frame(frame: Frame, identity: OutboundIdentity, config: Config,
                    challenge: dict | None) -> dict:
    wire = dict(frame)
    token = None
    required_token = None
    if frame.require_seat_token:
        # thread's base preflight rejects an unusable seat credential before
        # loading any operator credential. Keep that order after identity has
        # resolved, and reuse this token for the frame being sent.
        required_token = stream_token_from_env()
        if not required_token:
            raise SeatCredentialUnavailable()
    if frame.hello_style:
        operator = resolve_operator_token(config)
        explicit = frame.explicit_identity
        stream_id = identity.stream_id if frame.infer_identity else explicit
        style = frame.hello_style
        if style in {"persistent", "snapshot"}:
            token = required_token if frame.require_seat_token else stream_token_from_env()
            if style == "snapshot" and operator.value:
                wire["token"] = operator.value
            if stream_id and token:
                wire.update(from_stream_id=stream_id, stream_token=token)
            if style == "persistent" and operator.value:
                wire["token"] = operator.value
        elif style == "normal":
            if stream_id:
                wire["from_stream_id"] = stream_id
                token = required_token if frame.require_seat_token else stream_token_from_env()
                if token:
                    wire["stream_token"] = token
            if operator.value:
                wire["token"] = operator.value
        else:  # rpc hello, including the dashboard envelope variant
            if operator.value:
                wire["token"] = operator.value
            token = frame.identity_token
            if token is None and frame.infer_identity_token:
                token = required_token if frame.require_seat_token else stream_token_from_env()
            if stream_id and (token or not frame.infer_identity_token):
                wire["from_stream_id"] = stream_id
                if token:
                    wire["stream_token"] = token
            if challenge is not None:
                wire.pop("token", None)
                wire.update(operator_auth_v2_hello_fields(operator, challenge))
        if frame.require_seat_token and not token:
            raise SeatCredentialUnavailable()
        return wire
    for before, action, argument, stream_id, identity_with_token in frame.edits:
        if action == "caller":
            wire = _insert(wire, before, {argument: frame.caller[argument]})
        else:
            token = required_token if frame.require_seat_token else stream_token_from_env()
            if token:
                fields = {}
                if identity_with_token:
                    fields["from_stream_id"] = stream_id
                if not argument or "stream_token" not in wire:
                    fields["stream_token"] = token
                wire = _insert(wire, before, fields)
    return wire


async def _refuse(ws, identity):
    ws._agent_orch_credential_failed = True
    try:
        await ws.close()
    finally:
        raise ServiceCredentialUnavailable(identity)


async def send_frame(ws, payload: dict, identity: OutboundIdentity | None = None, *,
                     config: Config, explicit_from_stream_id=None, challenge=None, compact=True):
    frame = prepare_frame(payload)
    explicit = explicit_from_stream_id or frame.explicit_identity or frame.get("from_stream_id")
    # Re-resolve even when the caller supplied an identity: queued/retried
    # frames must see the current environment, never a build-time decision.
    identity = resolve_outbound_identity(explicit if explicit is not None else (identity.stream_id if identity else None))
    if getattr(ws, "_agent_orch_credential_failed", False) or identity.kind == "ambiguous":
        await _refuse(ws, identity)
    if identity.kind == "service":
        source_kind, source_name = FIXED_SERVICE_IDENTITIES[identity.stream_id]
        token = _stream_token_from_file() if source_kind == "file" else os.environ.get(source_name)
        if not token or not token.strip():
            await _refuse(ws, identity)
        wire = dict(frame)
        if identity.stream_id != "work-report-reader" or wire.get("type") == "hello":
            wire.update(from_stream_id=identity.stream_id, stream_token=token)
    else:
        wire = _ordinary_frame(frame, identity, config, challenge)
    await ws.send(json.dumps(wire, separators=(",", ":") if compact else None))


async def send_uncredentialed(ws, frame: dict, *, compact=True):
    if getattr(ws, "_agent_orch_credential_failed", False):
        raise ServiceCredentialUnavailable(resolve_outbound_identity())
    assert CREDENTIAL_KEYS.isdisjoint(frame), "credential in uncredentialed frame"
    await ws.send(json.dumps(frame, separators=(",", ":") if compact else None))
