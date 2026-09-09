from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import shlex
import uuid
from typing import Any, Iterable


SCHEMA_VERSION = 1
INLINE_MARKER = "AGENT_QUESTION_V1"
# ack is removed (D3): a question is a choice or pure free text; every question
# admits free text via canonical allow_custom=true.
RESPONSE_MODES = frozenset({"single_choice", "multi_choice", "free_text"})
_CHOICE_MODES = frozenset({"single_choice", "multi_choice"})
SERVER_HELD_ERROR = "prompt_server_unimplemented"
_QUESTION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")

# Admission format caps (mirror the daemon's; the daemon is authoritative). The
# CLI enforces them too so an author gets the exact violation without a round trip.
TITLE_MAX = 90
BODY_MAX = 1200
BLOCK_MAX = 300
BLOCK_MAX_LINES = 3
OPTION_MIN = 1
OPTION_MAX = 5
OPTION_LABEL_MAX = 40
OPTION_DESC_MAX = 100
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")


def normalize_body(raw: str) -> str:
    """Normalize + enforce the body contract (mirror of the daemon admission)."""
    text = str(raw or "")
    if "\r\n" in text:
        text = text.replace("\r\n", "\n")
    if "\r" in text:
        raise PromptValidationError("body: bare carriage returns are not allowed; use LF newlines")
    body = text.strip()
    if not body:
        raise PromptValidationError("body is required")
    if len(body) > BODY_MAX:
        raise PromptValidationError(f"body: too long (limit={BODY_MAX}, actual={len(body)})")
    if "```" in body:
        raise PromptValidationError("body: code fences are not allowed")
    lines = body.split("\n")
    for prev, cur in zip(lines, lines[1:]):
        if "|" in prev and _TABLE_SEP_RE.match(cur):
            raise PromptValidationError("body: markdown tables are not allowed")
    block: list[str] = []
    blocks: list[list[str]] = []
    for line in lines:
        if line.strip() == "":
            if block:
                blocks.append(block)
                block = []
        else:
            block.append(line)
    if block:
        blocks.append(block)
    for entry in blocks:
        if len(entry) > BLOCK_MAX_LINES:
            raise PromptValidationError(
                f"body: a block has too many lines (limit={BLOCK_MAX_LINES}, actual={len(entry)})")
        length = len("\n".join(entry))
        if length > BLOCK_MAX:
            raise PromptValidationError(
                f"body: a block is too long (limit={BLOCK_MAX}, actual={length})")
    return body


class PromptValidationError(ValueError):
    pass


@dataclass(frozen=True)
class PromptOption:
    label: str
    value: str
    description: str | None = None

    def as_dict(self) -> dict[str, str]:
        payload = {"label": self.label, "value": self.value}
        if self.description is not None:
            payload["description"] = self.description
        return payload


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_option(raw: str) -> PromptOption:
    text = str(raw).strip()
    if not text:
        raise PromptValidationError("option labels must be non-empty")
    if "=" in text:
        label, value = text.split("=", 1)
        label = label.strip()
        value = value.strip()
    else:
        label = text
        value = text
    if not label:
        raise PromptValidationError("option labels must be non-empty")
    if not value:
        raise PromptValidationError("option values must be non-empty")
    return PromptOption(label=label, value=value)


def parse_option_json(raw: str) -> PromptOption:
    try:
        decoded = json.loads(str(raw))
    except json.JSONDecodeError as exc:
        raise PromptValidationError(f"option JSON is invalid: {exc.msg}") from exc
    if not isinstance(decoded, dict):
        raise PromptValidationError("option JSON must be an object")
    raw_label = decoded.get("label")
    if not isinstance(raw_label, str):
        raise PromptValidationError("option JSON requires string label")
    label = raw_label.strip()
    if not label:
        raise PromptValidationError("option labels must be non-empty")
    raw_value = decoded.get("value", label)
    if not isinstance(raw_value, str):
        raise PromptValidationError("option JSON value must be a string")
    value = raw_value.strip()
    if not value:
        raise PromptValidationError("option values must be non-empty")
    raw_description = decoded.get("description")
    if raw_description is None:
        description = None
    elif isinstance(raw_description, str):
        description = raw_description.strip() or None
    else:
        raise PromptValidationError("option JSON description must be a string")
    return PromptOption(label=label, value=value, description=description)


def normalize_options(
    raw_options: Iterable[str | PromptOption], *, response_mode: str
) -> list[PromptOption]:
    options = [
        raw if isinstance(raw, PromptOption) else parse_option(raw)
        for raw in raw_options
    ]
    if response_mode == "free_text":
        if options:
            raise PromptValidationError("free_text prompts cannot define --option")
        return []
    if not options:
        raise PromptValidationError(f"{response_mode} prompts require at least one --option")
    if len(options) > OPTION_MAX:
        raise PromptValidationError(
            f"too many options (limit={OPTION_MAX}, actual={len(options)})")
    values = [option.value for option in options]
    if len(set(values)) != len(values):
        raise PromptValidationError("option values must be unique")
    for option in options:
        if "\n" in option.label or "\r" in option.label:
            raise PromptValidationError("option label must be a single line")
        if len(option.label) > OPTION_LABEL_MAX:
            raise PromptValidationError(
                f"option label too long (limit={OPTION_LABEL_MAX}, actual={len(option.label)})")
        if option.description is not None:
            if "\n" in option.description or "\r" in option.description:
                raise PromptValidationError("option description must be a single line")
            if len(option.description) > OPTION_DESC_MAX:
                raise PromptValidationError(
                    f"option description too long (limit={OPTION_DESC_MAX}, "
                    f"actual={len(option.description)})")
    return options


def normalize_response_mode(value: str) -> str:
    mode = str(value or "").strip()
    if mode not in RESPONSE_MODES:
        expected = ", ".join(sorted(RESPONSE_MODES))
        raise PromptValidationError(f"response_mode must be one of: {expected}")
    return mode


def normalize_question_id(value: str | None) -> str:
    question_id = str(value or f"q-{uuid.uuid4()}").strip()
    if not _QUESTION_ID_RE.fullmatch(question_id):
        raise PromptValidationError("question_id must be 1-120 chars: letters, digits, underscore, dash, dot, colon")
    return question_id


def default_dedup_key(*, question_id: str, producer_stream_id: str | None, spec_id: str | None) -> str:
    scope = producer_stream_id or spec_id or "unbound"
    return f"agent-question:{scope}:{question_id}"


def build_envelope(
    *,
    title: str,
    body: str,
    response_mode: str,
    raw_options: Iterable[str] = (),
    question_id: str | None = None,
    producer_stream_id: str | None = None,
    producer_provider: str | None = None,
    spec_id: str | None = None,
    context: str | None = None,
    dedup_key: str | None = None,
    ttl_seconds: int | None = None,
    allow_custom: bool = False,
    now: str | None = None,
) -> dict[str, Any]:
    clean_title = str(title or "").strip()
    if not clean_title:
        raise PromptValidationError("title is required")
    if "\n" in clean_title or "\r" in clean_title:
        raise PromptValidationError("title must be a single raw line")
    if len(clean_title) > TITLE_MAX:
        raise PromptValidationError(
            f"title: too long (limit={TITLE_MAX}, actual={len(clean_title)})")
    clean_body = normalize_body(body)
    if context is not None and str(context).strip():
        raise PromptValidationError(
            "a separate --context is not allowed; put all readable context in --body")
    mode = normalize_response_mode(response_mode)
    normalized_question_id = normalize_question_id(question_id)
    options = normalize_options(raw_options, response_mode=mode)
    if ttl_seconds is not None and ttl_seconds <= 0:
        raise PromptValidationError("ttl_seconds must be positive")
    ts = now or utc_now()
    envelope: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "question_id": normalized_question_id,
        "producer_stream_id": producer_stream_id,
        "producer_provider": producer_provider,
        "spec_id": spec_id,
        "dedup_key": dedup_key
        or default_dedup_key(
            question_id=normalized_question_id,
            producer_stream_id=producer_stream_id,
            spec_id=spec_id,
        ),
        "title": clean_title,
        "body": clean_body,
        "context": None,
        "response_mode": mode,
        "options": [option.as_dict() for option in options],
        "default_action": None,
        "ttl_seconds": ttl_seconds,
        "created_at": ts,
        "updated_at": ts,
        "answered_at": None,
        "answer": None,
        # Canonical on every question: free text is always admissible.
        "allow_custom": True,
    }
    return envelope


def notification_actions(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for index, option in enumerate(envelope.get("options") or []):
        if not isinstance(option, dict):
            continue
        value = option.get("value")
        label = str(option.get("label") or value or f"Option {index + 1}")
        action: dict[str, Any] = {
            "kind": "yes_no",
            "action_id": f"a{index}",
            "label": label,
            "value": {
                "schema_version": SCHEMA_VERSION,
                "question_id": envelope["question_id"],
                "answer": value,
            },
            "choice": index == 0,
        }
        actions.append(action)
    return actions


def fallback_response(envelope: dict[str, Any], *, error_code: str, message: str) -> dict[str, Any]:
    return {
        "type": "prompt.fallback",
        "ok": False,
        "error_code": error_code,
        "error": error_code,
        "message": message,
        "question_id": envelope["question_id"],
        "dedup_key": envelope["dedup_key"],
        "envelope": envelope,
        "inline_prompt": inline_prompt_block(envelope),
    }


def unsupported_response(command: str) -> dict[str, Any]:
    return {
        "type": f"prompt.{command}.unsupported",
        "ok": False,
        "error_code": SERVER_HELD_ERROR,
        "error": SERVER_HELD_ERROR,
        "message": (
            "Durable prompt server RPCs are held until Lane E report-core merges "
            "and chat_streamd file boundaries are agreed."
        ),
    }


def inline_prompt_block(envelope: dict[str, Any]) -> str:
    lines = [
        INLINE_MARKER,
        f"question_id: {envelope['question_id']}",
    ]
    if envelope.get("producer_stream_id"):
        lines.append(f"producer_stream_id: {envelope['producer_stream_id']}")
    if envelope.get("spec_id"):
        lines.append(f"spec_id: {envelope['spec_id']}")
    lines.extend(
        [
            f"title: {envelope['title']}",
            f"response_mode: {envelope['response_mode']}",
        ]
    )
    if envelope.get("context"):
        lines.extend(["context:", _indent(str(envelope["context"]))])
    lines.extend(["question:", _indent(str(envelope["body"]))])
    options = envelope.get("options") or []
    if options:
        lines.append("options:")
        for option in options:
            if not isinstance(option, dict):
                continue
            lines.append(f"- {option.get('label')} [{option.get('value')}]")
    lines.append("durability: inline fallback only; this answer is not daemon-durable.")
    lines.append(f"retry_command: {retry_command(envelope)}")
    lines.append(f"END_{INLINE_MARKER}")
    return "\n".join(lines)


def retry_command(envelope: dict[str, Any]) -> str:
    argv = [
        "agent-orch",
        "prompt",
        "ask",
        "--question-id",
        str(envelope["question_id"]),
        "--title",
        str(envelope["title"]),
        "--body",
        str(envelope["body"]),
        "--response-mode",
        str(envelope["response_mode"]),
    ]
    if envelope.get("context"):
        argv.extend(["--context", str(envelope["context"])])
    if envelope.get("spec_id"):
        argv.extend(["--spec-id", str(envelope["spec_id"])])
    if envelope.get("producer_stream_id"):
        argv.extend(["--from", str(envelope["producer_stream_id"])])
    if envelope.get("ttl_seconds"):
        argv.extend(["--ttl", str(envelope["ttl_seconds"])])
    if envelope.get("allow_custom") is True:
        argv.append("--allow-custom")
    for option in envelope.get("options") or []:
        if not isinstance(option, dict):
            continue
        label = str(option.get("label") or "")
        value = str(option.get("value") or "")
        if option.get("description") is not None:
            raw_json = json.dumps(
                {
                    "label": label,
                    "value": value,
                    "description": str(option.get("description") or ""),
                },
                separators=(",", ":"),
            )
            argv.extend(["--option-json", raw_json])
        else:
            raw = label if label == value else f"{label}={value}"
            argv.extend(["--option", raw])
    return " ".join(shlex.quote(arg) for arg in argv)


def envelope_json(envelope: dict[str, Any]) -> str:
    return json.dumps(envelope, separators=(",", ":"), sort_keys=True)


def _indent(value: str) -> str:
    return "\n".join(f"  {line}" if line else "  " for line in value.splitlines())
