"""A non-empty Claude thinking block is user-facing prose, not hidden reasoning.

Claude Code stores real model reasoning as a signature-only thinking block
(empty ``thinking``) and renders it as a "Thinking" placeholder. A thinking
block that carries text is a short mid-turn progress update that the TUI prints
as an ordinary ``●`` paragraph (Claude Code 2.1.x). Chat must show it as
assistant text.
Spec: spec_pentacle__chat_queued_message_state_2026_10 (dropped-prose scope).
"""

from claude_jsonl_norm import normalize_claude_jsonl_record


def _assistant(blocks: list[dict]) -> dict:
    return {
        "type": "assistant",
        "uuid": "00000000-0000-4000-8000-000000000001",
        "sessionId": "fixture-session",
        "timestamp": "2026-01-01T00:00:00.000Z",
        "thinkingDurationMs": 1,
        "message": {"id": "msg_fixture", "role": "assistant", "model": "claude-fixture",
                    "stop_reason": "tool_use", "content": blocks},
    }


PROSE = ("Checked the fixture inputs and they line up with the expected layout. "
         "I'm running the next step now and will report the result.")


def test_nonempty_thinking_block_is_assistant_text() -> None:
    events = normalize_claude_jsonl_record(
        _assistant([{"type": "thinking", "thinking": PROSE, "signature": "sig"}]),
        host="fixture", session_name="seat",
    )
    assert [e["kind"] for e in events] == ["ASSIST_TEXT"]
    assert events[0]["text"] == PROSE
    assert events[0]["raw"]["claude_block_type"] == "thinking"


def test_signature_only_thinking_block_stays_thinking_placeholder() -> None:
    events = normalize_claude_jsonl_record(
        _assistant([{"type": "thinking", "thinking": "", "signature": "sig"}]),
        host="fixture", session_name="seat",
    )
    assert [(e["kind"], e["text"]) for e in events] == [("THINKING", "Thinking")]


def test_whitespace_only_thinking_block_stays_thinking_placeholder() -> None:
    events = normalize_claude_jsonl_record(
        _assistant([{"type": "thinking", "thinking": " \n ", "signature": "sig"}]),
        host="fixture", session_name="seat",
    )
    assert [e["kind"] for e in events] == ["THINKING"]
