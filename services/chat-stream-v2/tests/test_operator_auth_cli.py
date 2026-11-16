"""Unit tests for the registry-backed operator-auth administration CLI."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

import operator_auth_cli
from _shared import operator_auth


def _registry_path(tmp_path: Path) -> Path:
    return tmp_path / "credentials" / "operator-credentials.json"


def _run(path: Path, capsys, *arguments: str) -> tuple[int, str, str]:
    result = operator_auth_cli.main(["--registry", str(path), *arguments])
    captured = capsys.readouterr()
    return result, captured.out, captured.err


def test_issue_list_revoke_and_rotate_use_the_temp_registry(tmp_path: Path, capsys) -> None:
    path = _registry_path(tmp_path)

    result, output, error = _run(
        path,
        capsys,
        "issue",
        "--client-kind",
        "pentacle",
        "--label",
        "friend desktop",
    )

    assert result == 0
    assert error == ""
    issued = json.loads(output)
    assert issued["status"] == "issued"
    assert issued["client_kind"] == "pentacle"
    assert issued["label"] == "friend desktop"
    assert uuid.UUID(issued["credential_id"])
    assert issued["code"].startswith(operator_auth.ENVELOPE_PREFIX)
    assert operator_auth.decode_envelope(issued["code"])["credential_id"] == issued["credential_id"]

    result, output, error = _run(path, capsys, "list")

    assert result == 0
    assert error == ""
    rows = json.loads(output)
    assert rows == [
        {
            "credential_id": issued["credential_id"],
            "credential_fingerprint": operator_auth.credential_fingerprint(issued["credential_id"]),
            "client_kind": "pentacle",
            "label": "friend desktop",
            "created_at": rows[0]["created_at"],
            "revoked_at": None,
            "replaces_fingerprint": None,
        }
    ]

    result, output, error = _run(path, capsys, "rotate", issued["credential_id"], "--label", "friend desktop 2")

    assert result == 0
    assert error == ""
    rotated = json.loads(output)
    assert rotated["status"] == "rotated"
    assert rotated["client_kind"] == "pentacle"
    assert rotated["label"] == "friend desktop 2"
    assert rotated["replaced_credential_id"] == issued["credential_id"]
    assert rotated["credential_id"] != issued["credential_id"]
    assert operator_auth.decode_envelope(rotated["code"])["credential_id"] == rotated["credential_id"]

    registry = operator_auth.OperatorCredentialRegistry(path)
    snapshot = registry.load()
    assert snapshot.credentials[issued["credential_id"]]["revoked_at"] is not None
    assert snapshot.credentials[rotated["credential_id"]]["revoked_at"] is None
    assert snapshot.credentials[rotated["credential_id"]]["replaces_credential_id"] == issued["credential_id"]

    result, output, error = _run(path, capsys, "revoke", rotated["credential_id"])

    assert result == 0
    assert error == ""
    assert json.loads(output) == {
        "status": "revoked",
        "credential_fingerprint": operator_auth.credential_fingerprint(rotated["credential_id"]),
    }
    assert registry.load().credentials[rotated["credential_id"]]["revoked_at"] is not None


def test_cli_refuses_unknown_and_invalid_credentials(tmp_path: Path, capsys) -> None:
    path = _registry_path(tmp_path)
    missing_id = str(uuid.uuid4())

    result, output, error = _run(path, capsys, "revoke", missing_id)
    assert result == 2
    assert output == ""
    assert error.strip() == "error: unknown credential"

    result, output, error = _run(path, capsys, "rotate", missing_id)
    assert result == 2
    assert output == ""
    assert error.strip() == "error: credential is unavailable for rotation"

    result, output, error = _run(path, capsys, "revoke", "not-a-uuid")
    assert result == 2
    assert output == ""
    assert error.strip() == "error: invalid credential id"


def test_cli_refuses_rotation_of_a_revoked_credential(tmp_path: Path, capsys) -> None:
    path = _registry_path(tmp_path)
    result, output, error = _run(path, capsys, "issue", "--client-kind", "pentacle-mobile")
    assert result == 0
    assert error == ""
    credential_id = json.loads(output)["credential_id"]

    result, output, error = _run(path, capsys, "revoke", credential_id)
    assert result == 0
    assert error == ""

    result, output, error = _run(path, capsys, "rotate", credential_id)
    assert result == 2
    assert output == ""
    assert error.strip() == "error: credential is unavailable for rotation"


def test_cli_refuses_an_unreadable_registry(tmp_path: Path, capsys) -> None:
    path = _registry_path(tmp_path)
    registry = operator_auth.OperatorCredentialRegistry(path)
    registry.initialize()
    os.chmod(path, 0o640)

    result, output, error = _run(path, capsys, "list")

    assert result == 2
    assert output == ""
    assert error.strip() == "error: operator credential registry unavailable"


def test_cli_rejects_an_unknown_client_kind(tmp_path: Path, capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        operator_auth_cli.main(
            ["--registry", str(_registry_path(tmp_path)), "issue", "--client-kind", "worker"]
        )

    assert raised.value.code == 2
