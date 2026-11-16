"""Regression coverage for v2 use of the established mobile enrollment registry."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from enrollment import EnrollmentRegistry
from uiverbs import UIVerbs

from _shared import operator_auth


def _secure_dir(path: Path) -> Path:
    path.mkdir()
    os.chmod(path, 0o700)
    return path


def _registry(tmp_path: Path, payload: dict[str, object]) -> tuple[EnrollmentRegistry, Path]:
    mobile = _secure_dir(tmp_path / "pentacle-mobile")
    stream = _secure_dir(tmp_path / "example-stream")
    codes_path = mobile / "enrollment-codes.json"
    operator_auth.atomic_write_json(codes_path, {"TEST2345": payload})
    credentials = operator_auth.OperatorCredentialRegistry(stream / "operator-credentials.json")
    return EnrollmentRegistry(codes_path=codes_path, credential_registry=credentials), codes_path


def _v2_code(*, expires_at: float | None = None) -> dict[str, object]:
    return {
        "created_at": "2026-08-20T00:00:00Z",
        "expires_at": expires_at if expires_at is not None else time.time() + 60,
        "used_at": "",
        "label": "simulator test",
        "protocol_version": 2,
        "scheme": operator_auth.AUTH_SCHEME,
        "client_kind": "pentacle-mobile",
        "replaces_credential_id": None,
    }


def _enroll(registry: EnrollmentRegistry, **overrides: object) -> dict[str, object]:
    request: dict[str, object] = {
        "type": "enroll",
        "client": "pentacle-mobile",
        "code": "TEST2345",
        "protocol_version": 2,
        "scheme": operator_auth.AUTH_SCHEME,
    }
    request.update(overrides)
    return asyncio.run(UIVerbs(None, None, None, enrollment_registry=registry).enroll(request))


def test_v2_enroll_consumes_existing_code_and_returns_a_mobile_credential(tmp_path: Path) -> None:
    registry, codes_path = _registry(tmp_path, _v2_code())

    response = _enroll(registry)

    assert response["type"] == "enroll.ok"
    assert response["protocol_version"] == 2
    assert response["scheme"] == operator_auth.AUTH_SCHEME
    assert response["client_kind"] == "pentacle-mobile"
    assert str(response["token"]).startswith(operator_auth.ENVELOPE_PREFIX)
    assert operator_auth.decode_envelope(response["token"])["client_kind"] == "pentacle-mobile"
    codes, _signature = operator_auth.read_secure_json(codes_path)
    assert codes["TEST2345"]["used_at"]

    replay = _enroll(registry)

    assert replay == {"type": "enroll.error", "error": "Enrollment code has already been used"}


def test_v2_enroll_rejects_a_mismatched_request_without_consuming_the_code(tmp_path: Path) -> None:
    registry, codes_path = _registry(tmp_path, _v2_code())

    response = _enroll(registry, scheme="shared-bearer-v1")

    assert response == {"type": "enroll.error", "error": "Enrollment protocol mismatch"}
    codes, _signature = operator_auth.read_secure_json(codes_path)
    assert codes["TEST2345"]["used_at"] == ""


def test_v2_enroll_rejects_expired_existing_registry_code(tmp_path: Path) -> None:
    registry, _codes_path = _registry(tmp_path, _v2_code(expires_at=time.time() - 1))

    response = _enroll(registry)

    assert response == {"type": "enroll.error", "error": "Enrollment code is invalid or expired"}
