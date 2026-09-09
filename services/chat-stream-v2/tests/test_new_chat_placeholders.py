"""New Chat display placeholders.
Untitled rows render "New Chat - <Host>" numbered per host,
display-time only."""
from __future__ import annotations

from sessions import _new_chat_placeholder, _title_is_untitled, _with_display_placeholders


def _row(session_name, host, title=None, created_at=""):
    return {
        "stream_id": f"{host}:{session_name}",
        "session_name": session_name,
        "host": host,
        "title": title,
        "created_at": created_at,
    }


def test_untitled_detection_v1_vocabulary():
    for value in (None, "", "  ", "bash", "claude", "CODEX", "model-b", "0", "1.2", "new chat", "New Chat - Example Host 2"):
        assert _title_is_untitled(value), value
    for value in ("Review example input", "Example coordinator task", "custom title"):
        assert not _title_is_untitled(value), value


def test_placeholder_numbering_per_host():
    assert _new_chat_placeholder("hostb", 0) == "New Chat - hostb"
    assert _new_chat_placeholder("hostb", 1) == "New Chat - hostb 2"
    assert _new_chat_placeholder("", 0) == "New Chat - Unknown"


def test_pass_fills_untitled_only_and_is_stable():
    rows = [
        _row("v2-aaaa1111", "hostb", created_at="2026-08-05T02:00:00Z"),
        _row("v2-bbbb2222", "hostb", created_at="2026-08-05T01:00:00Z"),
        _row("v2-cccc3333", "hosta", created_at="2026-08-05T03:00:00Z"),
        _row("v2-dddd4444", "hostb", title="Custom Title", created_at="2026-08-05T00:00:00Z"),
    ]
    out = {r["session_name"]: r for r in _with_display_placeholders(rows)}
    # numbered by created_at within host, titled row untouched
    assert out["v2-bbbb2222"]["display_name"] == "New Chat - hostb"
    assert out["v2-aaaa1111"]["display_name"] == "New Chat - hostb 2"
    assert out["v2-cccc3333"]["display_name"] == "New Chat - hosta"
    assert out["v2-dddd4444"]["title"] == "Custom Title"
    assert "display_name" not in out["v2-dddd4444"]
    assert out["v2-bbbb2222"]["title_source"] == "placeholder"


def test_list_open_applies_placeholders_without_touching_inventory():
    import asyncio
    from sessions import Sessions

    class _Store:
        async def list_sessions(self, status="open"):
            return []

    s = Sessions.__new__(Sessions)
    s._inv = {
        "hostb:v2-eeee5555": _row("v2-eeee5555", "hostb", created_at="2026-08-05T04:00:00Z"),
    }
    out = s.list_open()
    assert out[0]["display_name"] == "New Chat - hostb"
    # the inventory row itself remains untitled; the placeholder is presentation only
    assert s._inv["hostb:v2-eeee5555"].get("title") is None
