"""Focused coverage for the `agent-orch send-image` CLI verb: client-side
preflight (type/size/existence) and the happy-path payload it sends."""

from __future__ import annotations

import argparse
import hashlib

import agent_orch.cli as cli


PNG = b"\x89PNG\r\n\x1a\nfixture"
JPEG = b"\xff\xd8\xff\xe0fixture"


def _args(path, **kw):
    ns = argparse.Namespace(
        path=str(path), caption=kw.get("caption", ""),
        from_stream_id=kw.get("from_stream_id", "localhost:seat"),
        timeout=5.0,
    )
    return ns


def test_detect_image_mime():
    assert cli._detect_image_mime(PNG) == "image/png"
    assert cli._detect_image_mime(JPEG) == "image/jpeg"
    assert cli._detect_image_mime(b"GIF89a...") is None
    assert cli._detect_image_mime(b"") is None


def test_missing_file_is_bad_input(tmp_path):
    rc = cli.send_image(_args(tmp_path / "nope.png"))
    assert rc == 2


def test_unsupported_type_is_bad_input(tmp_path):
    p = tmp_path / "x.gif"
    p.write_bytes(b"GIF89a not an image we accept")
    rc = cli.send_image(_args(p))
    assert rc == 2


def test_empty_file_is_bad_input(tmp_path):
    p = tmp_path / "empty.png"
    p.write_bytes(b"")
    rc = cli.send_image(_args(p))
    assert rc == 2


def test_oversize_is_rejected_before_upload(tmp_path, monkeypatch):
    p = tmp_path / "big.png"
    p.write_bytes(PNG)
    # Force the limit tiny so we exercise the oversize path without a huge file.
    monkeypatch.setattr(cli, "SEND_IMAGE_MAX_BYTES", 4)
    uploaded = {"called": False}

    def _no_upload(*a, **k):
        uploaded["called"] = True
        raise AssertionError("upload must not run for an oversize file")

    monkeypatch.setattr(cli, "upload_blob_once", _no_upload)
    rc = cli.send_image(_args(p))
    assert rc == 2
    assert uploaded["called"] is False


def test_no_stream_id_is_bad_input(tmp_path, monkeypatch):
    p = tmp_path / "x.png"
    p.write_bytes(PNG)
    monkeypatch.setattr(cli, "env_stream_id", lambda: None)
    ns = _args(p, from_stream_id=None)
    rc = cli.send_image(ns)
    assert rc == 2


def test_happy_path_uploads_and_sends_expected_payload(tmp_path, monkeypatch, capsys):
    p = tmp_path / "chart.png"
    p.write_bytes(PNG)
    sha = hashlib.sha256(PNG).hexdigest()
    captured: dict = {}

    async def fake_upload(config, data, *, timeout=30.0):
        assert data == PNG
        return {"type": "upload_blob.ok", "blob_sha": sha}

    async def fake_send_image(config, payload, *, timeout=30.0):
        captured["payload"] = payload
        return {
            "type": "send_image.ok", "ok": True, "stream_id": "localhost:seat",
            "event_id": 42, "receipt_id": f"img-{payload['request_id']}"
            if "request_id" in payload else "img-x",
            "content_kind": "image_and_text", "duplicate": False,
        }

    monkeypatch.setattr(cli, "load_config", lambda: object())
    monkeypatch.setattr(cli, "upload_blob_once", fake_upload)
    monkeypatch.setattr(cli, "send_image_once", fake_send_image)

    rc = cli.send_image(_args(p, caption="the chart"))
    assert rc == 0
    payload = captured["payload"]
    assert payload["type"] == "send_image"
    assert payload["from_stream_id"] == "localhost:seat"
    assert payload["caption"] == "the chart"
    assert payload["attachments"] == [{"key": sha, "mime": "image/png", "bytes": len(PNG)}]
    out = capsys.readouterr().out
    assert '"ok": true' in out
    assert sha in out  # source sha256 printed as a receipt field


def _mock_transport(monkeypatch, *, error_code):
    async def fake_upload(config, data, *, timeout=30.0):
        return {"type": "upload_blob.ok", "blob_sha": hashlib.sha256(PNG).hexdigest()}

    async def fake_send_image(config, payload, *, timeout=30.0):
        return {"type": "send_image.error", "error_code": error_code, "error": "x"}

    monkeypatch.setattr(cli, "load_config", lambda: object())
    monkeypatch.setattr(cli, "upload_blob_once", fake_upload)
    monkeypatch.setattr(cli, "send_image_once", fake_send_image)


def test_authorization_error_maps_to_exit_66(tmp_path, monkeypatch):
    # The daemon returns an auth failure as a TYPED send_image.error response
    # (not a raised PermissionError); the CLI must still map it to exit 66.
    p = tmp_path / "x.png"
    p.write_bytes(PNG)
    _mock_transport(monkeypatch, error_code="stream_ownership_unverified")
    assert cli.send_image(_args(p)) == 66


def test_generic_send_error_maps_to_exit_1(tmp_path, monkeypatch):
    p = tmp_path / "x.png"
    p.write_bytes(PNG)
    _mock_transport(monkeypatch, error_code="attachment_invalid")
    assert cli.send_image(_args(p)) == 1
