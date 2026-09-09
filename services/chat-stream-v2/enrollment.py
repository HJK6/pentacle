"""v2 adapter for the existing one-time mobile enrollment registry."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from _shared import operator_auth
from v2_runtime import iso_now


ENROLLMENT_CODE_RE = re.compile(r"[A-Z2-9]{8}")
ENROLLMENT_CODES_PATH = Path.home() / ".config/pentacle-mobile/enrollment-codes.json"


class EnrollmentError(ValueError):
    """A safe, actionable error that can be returned to the enrollment client."""


class EnrollmentRegistry:
    """Consume v1-issued mobile enrollment codes and mint v2 credentials."""

    def __init__(
        self,
        *,
        codes_path: Path | None = None,
        credential_registry: operator_auth.OperatorCredentialRegistry | None = None,
    ) -> None:
        self.codes_path = codes_path or ENROLLMENT_CODES_PATH
        self.credential_registry = credential_registry or operator_auth.OperatorCredentialRegistry()

    def exchange(self, message: dict[str, Any]) -> dict[str, Any]:
        provided = str(message.get("code") or "").strip().upper()
        if not ENROLLMENT_CODE_RE.fullmatch(provided):
            raise EnrollmentError("Enrollment code must be 8 characters")
        try:
            client_kind = operator_auth.canonical_client_kind(message.get("client"))
        except operator_auth.OperatorAuthError as exc:
            raise EnrollmentError("Enrollment client kind is invalid") from exc

        try:
            with operator_auth.file_lock(self.codes_path.with_suffix(self.codes_path.suffix + ".lock")):
                codes = self._load_codes()
                payload = codes.get(provided)
                if payload is None:
                    raise EnrollmentError("Enrollment code is invalid or expired")
                if str(payload.get("used_at") or ""):
                    raise EnrollmentError("Enrollment code has already been used")
                self._validate_request(message, payload, client_kind)
                payload["used_at"] = iso_now()
                codes[provided] = payload
                operator_auth.atomic_write_json(self.codes_path, codes)
        except operator_auth.OperatorRegistryUnavailable as exc:
            raise EnrollmentError("Enrollment code store is unavailable") from exc

        try:
            _credential_id, token = self.credential_registry.issue(
                client_kind,
                label=str(payload.get("label") or ""),
                replaces_credential_id=payload.get("replaces_credential_id"),
            )
        except operator_auth.OperatorRegistryUnavailable as exc:
            raise EnrollmentError("Operator credential registry is unavailable") from exc
        except operator_auth.OperatorAuthError as exc:
            raise EnrollmentError("Operator credential issuance failed") from exc
        return {
            "type": "enroll.ok",
            "token": token,
            "protocol_version": 2,
            "scheme": operator_auth.AUTH_SCHEME,
            "client_kind": client_kind,
            "expires_at": payload["expires_at"],
            "label": payload["label"],
        }

    def _load_codes(self) -> dict[str, dict[str, object]]:
        if not self.codes_path.exists():
            return {}
        raw, _signature = operator_auth.read_secure_json(self.codes_path)
        if not isinstance(raw, dict):
            raise EnrollmentError("Enrollment code store is invalid")
        now = time.time()
        normalized: dict[str, dict[str, object]] = {}
        for raw_code, raw_payload in raw.items():
            if not isinstance(raw_code, str) or not ENROLLMENT_CODE_RE.fullmatch(raw_code.upper()):
                continue
            if not isinstance(raw_payload, dict):
                continue
            try:
                expires_at = float(raw_payload.get("expires_at") or 0)
                protocol_version = int(raw_payload.get("protocol_version") or 1)
                client_kind = operator_auth.canonical_client_kind(raw_payload.get("client_kind") or "pentacle-mobile")
            except (TypeError, ValueError, operator_auth.OperatorAuthError):
                continue
            if expires_at and expires_at < now or protocol_version not in {1, 2}:
                continue
            expected_scheme = operator_auth.AUTH_SCHEME if protocol_version == 2 else "shared-bearer-v1"
            if str(raw_payload.get("scheme") or expected_scheme) != expected_scheme:
                continue
            replaces_credential_id = raw_payload.get("replaces_credential_id")
            if replaces_credential_id is not None:
                try:
                    replaces_credential_id = operator_auth.canonical_uuid(replaces_credential_id)
                except operator_auth.OperatorAuthError:
                    continue
            normalized[raw_code.upper()] = {
                "created_at": str(raw_payload.get("created_at") or iso_now()),
                "expires_at": expires_at,
                "used_at": str(raw_payload.get("used_at") or ""),
                "label": str(raw_payload.get("label") or ""),
                "protocol_version": protocol_version,
                "scheme": expected_scheme,
                "client_kind": client_kind,
                "replaces_credential_id": replaces_credential_id,
            }
        return normalized

    @staticmethod
    def _validate_request(message: dict[str, Any], payload: dict[str, object], client_kind: str) -> None:
        if payload["protocol_version"] != 2:
            raise EnrollmentError("Enrollment protocol mismatch")
        if message.get("protocol_version") != 2 or message.get("scheme") != operator_auth.AUTH_SCHEME:
            raise EnrollmentError("Enrollment protocol mismatch")
        if payload["client_kind"] != client_kind:
            raise EnrollmentError("Enrollment client kind mismatch")
