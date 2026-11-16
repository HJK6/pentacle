"""Public setup links must work with the existing mobile exchange protocol."""

import asyncio
import stat
from urllib.parse import parse_qs, urlsplit

import pytest

from _shared import operator_auth
from enrollment import EnrollmentRegistry
from mobile_enrollment_cli import issue_link
from uiverbs import UIVerbs


def test_setup_link_enrolls_once_and_preserves_other_pending_links(tmp_path):
    path = tmp_path / "mobile" / "enrollment-codes.json"
    endpoint = "wss://example.test/stream?route=phone&v=2"
    first = issue_link(endpoint, label="first", codes_path=path)
    second = issue_link(endpoint, label="second", codes_path=path)
    query = parse_qs(urlsplit(first["url"]).query)
    assert query["ws"] == [endpoint]
    codes, _ = operator_auth.read_secure_json(path)
    assert len(codes) == 2
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    registry = EnrollmentRegistry(codes_path=path, credential_registry=operator_auth.OperatorCredentialRegistry(
        tmp_path / "stream" / "operator-credentials.json"))
    ui = UIVerbs(None, None, None, enrollment_registry=registry)
    request = {"type": "enroll", "client": "pentacle-mobile", "code": query["code"][0],
               "protocol_version": 2, "scheme": operator_auth.AUTH_SCHEME}
    result = asyncio.run(ui.enroll(request))
    assert result["type"] == "enroll.ok"
    assert operator_auth.decode_envelope(result["token"])["client_kind"] == "pentacle-mobile"
    assert asyncio.run(ui.enroll(request))["type"] == "enroll.error"
    request["code"] = parse_qs(urlsplit(second["url"]).query)["code"][0]
    assert asyncio.run(ui.enroll(request))["type"] == "enroll.ok"


def test_issued_link_expires(tmp_path, monkeypatch):
    path = tmp_path / "mobile" / "enrollment-codes.json"
    result = issue_link("ws://127.0.0.1:7791", ttl_seconds=1, codes_path=path)
    monkeypatch.setattr("enrollment.time.time", lambda: result["expires_at"] + 1)
    request = {"client": "pentacle-mobile", "code": parse_qs(urlsplit(result["url"]).query)["code"][0],
               "protocol_version": 2, "scheme": operator_auth.AUTH_SCHEME}
    ui = UIVerbs(None, None, None, enrollment_registry=EnrollmentRegistry(codes_path=path))
    assert asyncio.run(ui.enroll(request))["type"] == "enroll.error"


@pytest.mark.parametrize("url,ttl", [("https://example.test", 600), ("ws://user:secret@example.test", 600),
                                     ("ws://example.test:bad", 600), ("ws://example.test", 0),
                                     ("ws://example.test", 3601)])
def test_invalid_setup_inputs_do_not_create_registry(tmp_path, url, ttl):
    path = tmp_path / "mobile" / "enrollment-codes.json"
    with pytest.raises(ValueError):
        issue_link(url, ttl_seconds=ttl, codes_path=path)
    assert not path.exists()
