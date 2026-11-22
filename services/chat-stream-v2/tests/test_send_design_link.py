"""The existing image verb carries a typed link without introducing a second send rail."""
import pytest

from sessions import VerbError
from test_send_image import FakeTmux, STREAM, _att, _new_comms, _open, _run, _server


LINK = {
    "dashboard": "design",
    "target": {"project_id": "sample", "design_id": "orders", "view_id": "phone"},
    "title": "Phone",
    "subtitle": "Orders",
    "revision_id": "a" * 64,
}


def test_design_link_is_projected_once_with_caption_and_png():
    tmux = FakeTmux()
    comms, store, sessions = _new_comms(tmux)
    server, frames = _server(comms, store, sessions)

    async def run():
        try:
            await _open(sessions)
            payload = {
                "type": "send_image", "request_id": "fixture-design",
                "caption": "Review the phone screen", "attachments": [_att()],
                "dashboard_link": LINK,
                "_auth_context": {"token_verified": True, "stream_id": STREAM},
            }
            first = await server._on_send_image(payload)
            retry = await server._on_send_image(payload)
            events = [f["event"] for f in frames if f.get("type") == "chat.event"]
            assert first["duplicate"] is False and retry["duplicate"] is True
            assert len(events) == 1
            event = events[0]
            assert event["raw"]["dashboard_link"] == LINK
            assert event["text"] == "Review the phone screen"
            assert event["attachments"] == [_att()]
            assert not tmux.pastes
        finally:
            store.stop()

    _run(run())


@pytest.mark.parametrize("change, code", [
    ({"dashboard_link": {**LINK, "target": {**LINK["target"], "view_id": "%70hone"}}},
     "dashboard_link_invalid"),
    ({"dashboard_link": None}, "dashboard_link_invalid"),
    ({"attachments": [_att("image/jpeg")]}, "dashboard_link_png_required"),
    ({"attachments": [_att(), _att()]}, "dashboard_link_png_required"),
])
def test_design_link_refuses_invalid_target_or_preview(change, code):
    comms, store, sessions = _new_comms(FakeTmux())
    server, frames = _server(comms, store, sessions)

    async def run():
        try:
            await _open(sessions)
            with pytest.raises(VerbError) as exc:
                await server._on_send_image({
                    "request_id": "fixture-refusal", "attachments": [_att()],
                    "dashboard_link": LINK,
                    "_auth_context": {"token_verified": True, "stream_id": STREAM},
                    **change,
                })
            assert exc.value.code == code
            assert not [f for f in frames if f.get("type") == "chat.event"]
        finally:
            store.stop()

    _run(run())
