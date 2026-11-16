"""Closed, inert file metadata contract shared by daemon and CLI.

Extension selects the canonical type; a claimed content-type is never an input.
This validates format prefixes only, not arbitrary file content or secrets.
"""
from __future__ import annotations

import re
import unicodedata

ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024
CHAT_ATTACHMENT_PURPOSE = "chat_attachment"
MEDIA_TYPES = {
    ".png": ("image/png", b"\x89PNG"),
    ".jpg": ("image/jpeg", b"\xff\xd8\xff"),
    ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
    ".pdf": ("application/pdf", b"%PDF"),
    ".zip": ("application/zip", b"PK\x03\x04"),
    ".3mf": ("model/3mf", b"PK\x03\x04"),
    ".stl": ("model/stl", None),
    ".step": ("model/step", None),
    ".stp": ("model/step", None),
    ".scad": ("application/x-openscad", None),
}


class AttachmentError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def sanitized_filename(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise AttachmentError("unsupported_type")
    name = unicodedata.normalize("NFKC", value).replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if unicodedata.category(c)[0] != "C")
    name = re.sub(r'[<>:"|?*]', "_", name).strip(" .")
    if not name or len(name.encode("utf-8")) > 255:
        raise AttachmentError("attachment_filename_invalid")
    extension = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if extension not in MEDIA_TYPES:
        raise AttachmentError("unsupported_type")
    return name


def validated_media_type(filename: str, prefix: bytes) -> str:
    name = sanitized_filename(filename)
    extension = "." + name.rsplit(".", 1)[-1].lower()
    mime, magic = MEDIA_TYPES[extension]
    if magic is not None and not prefix.startswith(magic):
        raise AttachmentError("type_mismatch")
    return mime


def _identity_text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 512 and value == value.strip() and not any(ord(c) < 33 or ord(c) == 127 for c in value)


def verified_uploader(auth: object) -> dict:
    """Accept server-internal auth only; wire fields are stripped by dispatch.

    Seat identity always wins, including operator-equivalent seats. A partial
    seat identity is refused rather than downgraded to a credential principal.
    """
    if not isinstance(auth, dict):
        raise AttachmentError("authentication_required")
    stream, generation = auth.get("stream_id"), auth.get("session_generation")
    seat_claim = auth.get("token_verified") is True or bool(stream) or generation is not None
    if seat_claim:
        if auth.get("token_verified") is not True or not _identity_text(stream) or ":" not in stream or not all(stream.split(":", 1)) or not _identity_text(generation):
            raise AttachmentError("upload_identity_invalid")
        return {"auth_kind": "seat", "principal_id": stream, "seat_stream_id": stream,
                "seat_generation": generation, "credential_id": None, "assistant_scope": None}
    if auth.get("scoped_principal") is True:
        credential, scope = auth.get("credential_id"), auth.get("scope_stream")
        if not _identity_text(credential) or not _identity_text(scope):
            raise AttachmentError("upload_identity_invalid")
        return {"auth_kind": "scoped", "principal_id": "credential:" + credential,
                "seat_stream_id": None, "seat_generation": None,
                "credential_id": credential, "assistant_scope": scope}
    if auth.get("operator_authenticated") is True:
        principal = auth.get("operator_principal")
        if not _identity_text(principal) or not principal.startswith("operator:"):
            raise AttachmentError("upload_identity_invalid")
        credential = principal[len("operator:"):]
        if not _identity_text(credential) or auth.get("credential_id") not in (None, credential):
            raise AttachmentError("upload_identity_invalid")
        return {"auth_kind": "operator", "principal_id": principal,
                "seat_stream_id": None, "seat_generation": None,
                "credential_id": credential, "assistant_scope": None}
    raise AttachmentError("authentication_required")
