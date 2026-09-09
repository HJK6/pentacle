from __future__ import annotations

import pytest

from agent_orch import prompt_protocol


def test_build_choice_envelope_defaults_dedup_and_options():
    envelope = prompt_protocol.build_envelope(
        title="Choose path",
        body="Which implementation path?",
        response_mode="single_choice",
        raw_options=["Client only=client", "Wait=wait"],
        question_id="q-test",
        producer_stream_id="hostb:codex-a",
        spec_id="example__prompt_protocol",
        now="2026-07-02T05:00:00Z",
    )

    assert envelope["schema_version"] == 1
    assert envelope["question_id"] == "q-test"
    assert envelope["dedup_key"] == "agent-question:hostb:codex-a:q-test"
    assert envelope["response_mode"] == "single_choice"
    assert envelope["options"] == [
        {"label": "Client only", "value": "client"},
        {"label": "Wait", "value": "wait"},
    ]
    assert envelope["answer"] is None


def test_ack_mode_is_rejected():
    with pytest.raises(prompt_protocol.PromptValidationError, match="response_mode"):
        prompt_protocol.build_envelope(
            title="Confirm",
            body="Acknowledge this.",
            response_mode="ack",
            question_id="q-ack",
            now="2026-07-02T05:00:00Z",
        )
    assert "ack" not in prompt_protocol.RESPONSE_MODES


def test_free_text_envelope_has_no_options_or_actions():
    envelope = prompt_protocol.build_envelope(
        title="Explain",
        body="Why?",
        response_mode="free_text",
        question_id="q-free",
        now="2026-07-02T05:00:00Z",
    )

    assert envelope["response_mode"] == "free_text"
    assert envelope["options"] == []
    assert prompt_protocol.notification_actions(envelope) == []


def test_free_text_rejects_options():
    with pytest.raises(prompt_protocol.PromptValidationError, match="cannot define"):
        prompt_protocol.build_envelope(
            title="Explain",
            body="Why?",
            response_mode="free_text",
            raw_options=["Option=option"],
        )


def test_option_json_description_allow_custom_and_order_preserve_backward_shape():
    described = prompt_protocol.parse_option_json(
        '{"label":"Proceed","value":"proceed","description":"Ship the branch"}'
    )
    default_value = prompt_protocol.parse_option_json('{"label":"Custom"}')

    envelope = prompt_protocol.build_envelope(
        title="Choose path",
        body="Which implementation path?",
        response_mode="single_choice",
        raw_options=["Wait=wait", described, default_value],
        allow_custom=True,
        question_id="q-options",
        now="2026-07-02T05:00:00Z",
    )

    assert envelope["options"] == [
        {"label": "Wait", "value": "wait"},
        {"label": "Proceed", "value": "proceed", "description": "Ship the branch"},
        {"label": "Custom", "value": "Custom"},
    ]
    assert envelope["allow_custom"] is True
    assert prompt_protocol.notification_actions(envelope)[1]["value"]["answer"] == "proceed"


def test_allow_custom_is_canonical_on_every_mode():
    # Every question admits free text: allow_custom is stamped true regardless of
    # mode or the (now ignored) allow_custom argument.
    for mode, opts in (("single_choice", ["Yes=yes"]),
                       ("multi_choice", ["Yes=yes", "No=no"]),
                       ("free_text", [])):
        envelope = prompt_protocol.build_envelope(
            title="Q", body="Body here", response_mode=mode,
            raw_options=opts, allow_custom=False,
        )
        assert envelope["allow_custom"] is True


def test_option_label_and_description_must_be_single_line():
    with pytest.raises(prompt_protocol.PromptValidationError, match="single line"):
        prompt_protocol.build_envelope(
            title="Q", body="Body", response_mode="single_choice",
            raw_options=[prompt_protocol.parse_option_json(
                '{"label":"Line one\\nLine two","value":"a"}')],
        )
    with pytest.raises(prompt_protocol.PromptValidationError, match="single line"):
        prompt_protocol.build_envelope(
            title="Q", body="Body", response_mode="single_choice",
            raw_options=[prompt_protocol.parse_option_json(
                '{"label":"Ok","value":"a","description":"desc\\nwrapped"}')],
        )


def test_option_json_rejects_invalid_shape_and_duplicate_mixed_values():
    with pytest.raises(prompt_protocol.PromptValidationError, match="requires string label"):
        prompt_protocol.parse_option_json('{"value":"x"}')
    with pytest.raises(prompt_protocol.PromptValidationError, match="invalid"):
        prompt_protocol.parse_option_json("{not json")
    with pytest.raises(prompt_protocol.PromptValidationError, match="unique"):
        prompt_protocol.build_envelope(
            title="Choose",
            body="Pick one",
            response_mode="single_choice",
            raw_options=[
                "Plain=dup",
                prompt_protocol.parse_option_json('{"label":"JSON","value":"dup"}'),
            ],
        )


def test_inline_fallback_block_contains_retry_command_and_warning():
    envelope = prompt_protocol.build_envelope(
        title="Choose path",
        body="Which implementation path?",
        response_mode="single_choice",
        raw_options=["Client only=client"],
        question_id="q-test",
        producer_stream_id="hostb:codex-a",
        now="2026-07-02T05:00:00Z",
    )

    response = prompt_protocol.fallback_response(
        envelope,
        error_code=prompt_protocol.SERVER_HELD_ERROR,
        message="held",
    )

    block = response["inline_prompt"]
    assert response["type"] == "prompt.fallback"
    assert response["ok"] is False
    assert "AGENT_QUESTION_V1" in block
    assert "durability: inline fallback only" in block
    assert "agent-orch prompt ask" in block
