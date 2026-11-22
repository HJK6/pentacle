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
    monkeypatch.setattr(cli, "_send_image_to_composite", lambda *a, **k: None)

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
    monkeypatch.setattr(cli, "_send_image_to_composite", lambda *a, **k: None)


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


# --- front desk: the image must reach the composite the operator reads -------

def _publish_wire(monkeypatch, publish_response, *, bound=True, binding_error=False):
    calls = []

    async def upload(config, data, **kw):
        calls.append(("upload", kw))
        return {
            "type": "upload_blob.ok", "upload_id": "up-1",
            "blob_sha": hashlib.sha256(data).hexdigest(), "bytes": len(data),
            "media_type": "image/png", "filename": kw.get("filename", "x.png"),
            "uploader": "localhost:seat", "generation": "g1",
            "uploaded_at": "2026-01-01T00:00:00+00:00", "auth_kind": "seat",
            "seat_stream_id": "localhost:seat", "seat_generation": "g1",
            "credential_id": None, "assistant_scope": None,
        }

    async def assistant(config, payload, **kw):
        if payload["type"] == "assistant.binding":
            calls.append(("binding", payload))
            if binding_error:
                raise OSError("down")
            return {"type": "assistant.binding.ok", "composite_stream_id": "bound:assistant",
                    "stream_id": "localhost:seat" if bound else "localhost:other"}
        calls.append(("publish", payload))
        return publish_response

    async def legacy(config, payload, **kw):
        calls.append(("legacy", payload))
        return {"type": "send_image.ok", "ok": True, "stream_id": "localhost:seat", "event_id": 1}

    monkeypatch.delenv("PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID", raising=False)
    monkeypatch.setattr(cli, "load_config", lambda: object())
    monkeypatch.setattr(cli, "upload_blob_once", upload)
    monkeypatch.setattr(cli, "assistant_once", assistant)
    monkeypatch.setattr(cli, "send_image_once", legacy)
    return calls


def _fd_args(path, **kw):
    ns = _args(path, caption=kw.get("caption", "cap"))
    ns.to = kw.get("to")
    ns.note_type = kw.get("note_type")
    ns.ref = kw.get("ref")
    ns.why = kw.get("why")
    return ns


def _kinds(calls):
    return [c[0] for c in calls]


def test_bound_seat_resolves_composite_from_binding_and_publishes(tmp_path, monkeypatch, capsys):
    p = tmp_path / "x.png"
    p.write_bytes(PNG)
    calls = _publish_wire(monkeypatch, {"type": "assistant.publish.ok", "event_id": 7})
    assert cli.send_image(_fd_args(p, note_type="result", ref="report:r1")) == 0
    assert _kinds(calls) == ["binding", "upload", "publish"]
    payload = calls[2][1]
    assert payload["composite_stream_id"] == "bound:assistant"
    assert payload["attachment_ids"] == ["up-1"]
    assert payload["message"] == "cap"
    assert (payload["note_type"], payload["ref"]) == ("result", "report:r1")
    assert '"event_id": 7' in capsys.readouterr().out


def test_to_and_env_override_the_binding(tmp_path, monkeypatch):
    p = tmp_path / "x.png"
    p.write_bytes(PNG)
    calls = _publish_wire(monkeypatch, {"type": "assistant.publish.ok", "event_id": 3})
    assert cli.send_image(_fd_args(p, to="flag:assistant", note_type="result", ref="report:r1")) == 0
    assert calls[-1][1]["composite_stream_id"] == "flag:assistant"
    calls = _publish_wire(monkeypatch, {"type": "assistant.publish.ok", "event_id": 3})
    monkeypatch.setenv("PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID", "env:assistant")
    assert cli.send_image(_fd_args(p, note_type="result", ref="report:r1")) == 0
    assert calls[-1][1]["composite_stream_id"] == "env:assistant"


def test_bound_seat_without_note_fields_fails_loudly(tmp_path, monkeypatch, capsys):
    p = tmp_path / "x.png"
    p.write_bytes(PNG)
    calls = _publish_wire(monkeypatch, {
        "type": "assistant.publish.error", "error_code": "assistant_publish_note_type_required"})
    assert cli.send_image(_fd_args(p)) == 2
    assert "legacy" not in _kinds(calls)
    assert "--note-type" in capsys.readouterr().err


def test_bound_seat_never_falls_back_to_own_chat(tmp_path, monkeypatch):
    p = tmp_path / "x.png"
    p.write_bytes(PNG)
    for code in ("assistant_publish_unauthorized", "publish_not_authorized", "something_else",
                 "assistant_publish_repeat"):
        calls = _publish_wire(monkeypatch, {"type": "assistant.publish.error", "error_code": code})
        assert cli.send_image(_fd_args(p, note_type="result", ref="report:r1")) != 0
        assert "legacy" not in _kinds(calls)


def test_bound_seat_preflight_refusal_is_nonzero(tmp_path, monkeypatch):
    p = tmp_path / "x.png"
    p.write_bytes(PNG)
    calls = _publish_wire(monkeypatch, {"type": "assistant.publish.ok"})
    monkeypatch.setattr("agent_orch.file_delivery.bounded_read",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("blocked")))
    assert cli.send_image(_fd_args(p)) != 0
    assert "legacy" not in _kinds(calls)


def test_binding_unreachable_sends_nothing(tmp_path, monkeypatch):
    p = tmp_path / "x.png"
    p.write_bytes(PNG)
    calls = _publish_wire(monkeypatch, {"type": "assistant.publish.ok"}, binding_error=True)
    assert cli.send_image(_fd_args(p)) == 1
    assert _kinds(calls) == ["binding"]


def test_other_seat_keeps_own_conversation_behaviour(tmp_path, monkeypatch, capsys):
    p = tmp_path / "x.png"
    p.write_bytes(PNG)
    calls = _publish_wire(monkeypatch, {"type": "assistant.publish.ok"}, bound=False)
    assert cli.send_image(_fd_args(p)) == 0
    assert _kinds(calls) == ["binding", "upload", "legacy"]
    assert capsys.readouterr().err == ""


def test_other_seat_with_explicit_target_falls_back_on_refusal(tmp_path, monkeypatch):
    p = tmp_path / "x.png"
    p.write_bytes(PNG)
    for code in ("assistant_publish_unauthorized", "unknown_composite", "something_else"):
        calls = _publish_wire(monkeypatch, {"type": "assistant.publish.error", "error_code": code}, bound=False)
        assert cli.send_image(_fd_args(p, to="x:assistant")) == 0
        assert _kinds(calls)[-1] == "legacy"


def test_send_design_link_uses_verified_preview_and_explicit_request_id(tmp_path, monkeypatch, capsys):
    p = tmp_path / "phone.png"
    p.write_bytes(PNG)
    sha = hashlib.sha256(PNG).hexdigest()
    link = {
        "dashboard": "design", "target": {"project_id": "sample", "design_id": "board", "view_id": "phone"},
        "title": "Phone", "subtitle": "Board", "revision_id": "a" * 64,
    }
    payloads = []

    async def upload(config, data, *, timeout):
        assert data == PNG
        return {"type": "upload_blob.ok", "blob_sha": sha}

    async def post(config, payload, *, timeout):
        payloads.append(payload)
        return {"type": "send_image.ok", "stream_id": "localhost:seat", "event_id": 7}

    monkeypatch.setattr(cli, "load_config", lambda: object())
    monkeypatch.setattr(cli, "upload_blob_once", upload)
    monkeypatch.setattr(cli, "send_image_once", post)
    monkeypatch.setattr(cli, "_send_image_to_composite", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_resolve_design_preview", lambda *a: (p, sha, link))
    args = cli.build_parser().parse_args([
        "send-design-link", "sample", "board", "phone", "--caption", "Review",
        "--request-id", "fixture-1", "--from-stream-id", "localhost:seat",
    ])
    assert args.func(args) == 0
    assert payloads[0]["dashboard_link"] == link
    assert payloads[0]["request_id"] == "send-design-link:fixture-1"
    assert payloads[0]["caption"] == "Review"
    assert payloads[0]["attachments"] == [{"key": sha, "mime": "image/png", "bytes": len(PNG)}]
    assert str(p) not in capsys.readouterr().out
