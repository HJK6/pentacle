"""The daemon's single registry for operator-facing message envelopes.

Each entry owns both sides of its wire contract: a builder used by producers
and an anchored matcher used at authenticated ingest.  The registry is kept
deliberately small and immutable so a new marker cannot silently become a
client-side regex-only convention.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import logging
import re
from typing import Any, Callable

from provider_wrappers import (
    PROVIDER_WRAPPERS,
    normalize_provider_user_text,
)


BUG_REF = "spec_pentacle__message_envelope_format_registry_2026_09"
SCHEMA_VERSION = 1
_MAX_UNTAGGED_MARKERS = 100_000
_untagged_marker_count = 0

log = logging.getLogger("chat_streamd_v2.message_envelopes")

BuildMessageEnvelope = Callable[..., str]
MatchMessageEnvelope = Callable[..., dict[str, Any] | None]


@dataclass(frozen=True)
class MessageEnvelopeEntry:
    """One immutable wire format and its client render policy."""

    kind: str
    build: BuildMessageEnvelope
    match: MatchMessageEnvelope
    render_policy: str


def _notice_marker(notice_id: object) -> str:
    value = str(notice_id or "")
    if not value or any(char.isspace() or char == "]" for char in value):
        raise ValueError("notice_id must be non-empty and whitespace-free")
    return f"[pentacle-notice:{value}]"


def _build_notice_marker(*, notice_id: object, **_: Any) -> str:
    return _notice_marker(notice_id)


_NOTICE_MARKER = re.compile(r"^\[pentacle-notice:(?P<id>[^\]\s]+)\]$")


def _match_notice_marker(text: str, **_: Any) -> dict[str, Any] | None:
    match = _NOTICE_MARKER.fullmatch(str(text or ""))
    return {"kind": "notice_marker", "id": match["id"]} if match else None


def _build_notification_answer(*, notice_id: object, body: object, **_: Any) -> str:
    return f"{_notice_marker(notice_id)}\n{str(body or '')}"


_NOTIFICATION_ANSWER = re.compile(
    r"^\[pentacle-notice:(?P<id>notification-answer-[^\]\s]+)\]\n"
    r"\[notification\.answer\]\n"
    r"notification_id=(?P<notification_id>[^\s\r\n]+)\n"
    r"(?P<fields>[\s\S]+)$"
)


def _match_notification_answer(text: str, **_: Any) -> dict[str, Any] | None:
    match = _NOTIFICATION_ANSWER.fullmatch(str(text or ""))
    if not match:
        return None
    marker_suffix = match["id"][len("notification-answer-"):]
    notification_id = match["notification_id"]
    if notification_id != marker_suffix and notification_id.removeprefix("notification-") != marker_suffix:
        return None
    lines = match["fields"].split("\n")
    # _answer_back_text emits one or more answer fields followed by the
    # authenticated human actor.  Requiring that terminal field keeps a
    # truncated marker from becoming a registered envelope.
    if len(lines) < 2 or not lines[-1].startswith("by=") or not lines[-1][3:]:
        return None
    fields: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if not separator or key not in {"answer", "text", "custom_text", "choice", "by"}:
            return None
        if not value or "\r" in value:
            return None
        fields[key] = value
    if not any(key in fields for key in ("answer", "text", "custom_text", "choice")):
        return None
    return {
        "kind": "notification_answer",
        "id": match["id"],
        "notification_id": match["notification_id"],
    }


_CHILD_STREAM = r"[A-Za-z0-9._-]+:[^\s=]+"
_D2_NOTICE = r"d2:[0-9a-f]{64}"
_CHILD_CLOSED = re.compile(
    rf"^\[pentacle-notice:(?P<id>{_D2_NOTICE})\]\n"
    rf"Child session (?P<child>{_CHILD_STREAM}) closed\.$"
)


def _build_child_session_closed(*, notice_id: object, child_stream_id: object, **_: Any) -> str:
    return f"{_notice_marker(notice_id)}\nChild session {child_stream_id} closed."


def _match_child_session_closed(text: str, **_: Any) -> dict[str, Any] | None:
    match = _CHILD_CLOSED.fullmatch(str(text or ""))
    if not match:
        return None
    return {
        "kind": "child_session_closed",
        "id": match["id"],
        "child_stream_id": match["child"],
    }


_ISO_UTC = r"\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,6})?Z"
_CHILD_INACTIVITY = re.compile(
    rf"^\[pentacle-notice:(?P<id>{_D2_NOTICE})\]\n"
    rf"Child session (?P<child>{_CHILD_STREAM}) reached an inactivity threshold at "
    rf"(?P<trigger>{_ISO_UTC})\.$"
)


def _valid_utc_timestamp(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _build_child_inactivity_threshold(
    *, notice_id: object, child_stream_id: object, trigger_at: object, **_: Any,
) -> str:
    return (
        f"{_notice_marker(notice_id)}\nChild session {child_stream_id} reached an inactivity "
        f"threshold at {trigger_at}."
    )


def _match_child_inactivity_threshold(text: str, **_: Any) -> dict[str, Any] | None:
    match = _CHILD_INACTIVITY.fullmatch(str(text or ""))
    if not match or not _valid_utc_timestamp(match["trigger"]):
        return None
    return {
        "kind": "child_inactivity_threshold",
        "id": match["id"],
        "child_stream_id": match["child"],
        "trigger_at": match["trigger"],
    }


def _report_notice_id(report_id: object) -> str:
    digest = hashlib.sha256(str(report_id or "").encode("utf-8")).hexdigest()
    return f"child-report-ready-v2-{digest}"


def _build_child_report_ready(
    *, report_id: object, ledger_row_id: object, child_stream_id: object,
    msg_id: object, status: object, summary: object, notice_id: object | None = None,
    qa_attestation_state: object | None = None,
    qa_attestation_reasons: object | None = None,
    effective_model: object | None = None, effective_effort: object | None = None,
    **_: Any,
) -> str:
    resolved_notice_id = str(notice_id or _report_notice_id(report_id))
    summary_text = str(summary or "").strip()
    if len(summary_text) > 1000:
        summary_text = summary_text[:997] + "..."
    lines = [
        "[child_report_ready]",
        f"report_id={report_id}",
        f"ledger_row_id={ledger_row_id}",
        f"child_stream_id={child_stream_id}",
        f"msg_id={msg_id}",
        f"status={status}",
    ]
    if qa_attestation_state == "unverified":
        reasons = qa_attestation_reasons or ""
        if isinstance(reasons, (list, tuple)):
            reasons = ",".join(str(reason) for reason in reasons)
        lines.extend(("qa_attestation_state=unverified", f"qa_attestation_reasons={reasons}"))
    lines.append(f"summary={summary_text}")
    if effective_model is not None:
        lines.append(f"effective_model={effective_model}")
    if effective_effort is not None:
        lines.append(f"effective_effort={effective_effort}")
    return f"{_notice_marker(resolved_notice_id)}\n" + "\n".join(lines)


_REPORT_ID = r"[^\s=]+"
_CHILD_REPORT = re.compile(
    rf"^\[pentacle-notice:(?P<id>child-report-ready-v2-(?P<digest>[0-9a-f]{{64}}))\]\n"
    rf"\[child_report_ready\]\n"
    rf"report_id=(?P<report_id>{_REPORT_ID})\n"
    rf"ledger_row_id=(?P<ledger_row_id>\d+)\n"
    rf"child_stream_id=(?P<child>{_CHILD_STREAM})\n"
    rf"msg_id=(?P<msg_id>[^\s=]+)\n"
    rf"status=(?P<status>done|error|aborted)\n"
    rf"(?:(?:qa_attestation_state=unverified)\nqa_attestation_reasons=(?P<qa_reasons>[A-Za-z0-9_.:-]+(?:,[A-Za-z0-9_.:-]+)*)\n)?"
    rf"summary=(?P<summary>[^\r\n]+)"
    rf"(?:\neffective_model=(?P<effective_model>[^\s=]+)\neffective_effort=(?P<effective_effort>[^\s=]+))?$"
)


def _match_child_report_ready(text: str, **_: Any) -> dict[str, Any] | None:
    match = _CHILD_REPORT.fullmatch(str(text or ""))
    if not match or _report_notice_id(match["report_id"]) != match["id"]:
        return None
    return {
        "kind": "child_report_ready",
        "id": match["id"],
        "report_id": match["report_id"],
        "ledger_row_id": int(match["ledger_row_id"]),
        "child_stream_id": match["child"],
        "msg_id": match["msg_id"],
        "status": match["status"],
    }


def _build_claude_pasted_content(*, id: object, body: object, **_: Any) -> str:
    wrapper_id = str(id)
    if not re.fullmatch(r"[0-9a-f]+", wrapper_id):
        raise ValueError("unsupported provider wrapper")
    # This is the registry builder for the grammar emitted by the authenticated
    # Claude USER path; matching remains delegated to the provider table below.
    return f'\n\n<pasted_content id="{wrapper_id}">\n{str(body)}\n</pasted_content id="{wrapper_id}">\n'


def _match_claude_pasted_content(
    text: str,
    *,
    provider: str | None = None,
    authenticated: bool | None = None,
    provider_wrapper: object = None,
    **_: Any,
) -> dict[str, Any] | None:
    # Direct fixture matching is intentionally grammar-only. Ingest supplies
    # provider/authentication context, which is required before this adapter
    # can classify a wrapper as an internal envelope.
    if provider is not None or authenticated is not None or provider_wrapper is not None:
        if provider != "claude" or authenticated is not True:
            return None
        if isinstance(provider_wrapper, dict):
            wrapper_kind = provider_wrapper.get("kind")
            wrapper_id = provider_wrapper.get("id")
            provenance = provider_wrapper.get("provenance")
            if (
                wrapper_kind == "claude_pasted_content"
                and provenance == "grammar"
                and isinstance(wrapper_id, str)
                and re.fullmatch(r"[0-9a-f]+", wrapper_id)
            ):
                return {"kind": wrapper_kind, "id": wrapper_id, "provenance": provenance}
        _display, normalized_tag = normalize_provider_user_text(
            str(text or ""), provider="claude", authenticated=True,
        )
        return normalized_tag
    for provider, kind, pattern in PROVIDER_WRAPPERS:
        if provider != "claude" or kind != "claude_pasted_content":
            continue
        match = pattern.fullmatch(str(text or ""))
        if match:
            return {"kind": kind, "id": match["id"], "provenance": "grammar"}
    return None


MESSAGE_ENVELOPES = (
    MessageEnvelopeEntry("notice_marker", _build_notice_marker, _match_notice_marker, "internal"),
    MessageEnvelopeEntry("notification_answer", _build_notification_answer, _match_notification_answer, "structured_card"),
    MessageEnvelopeEntry("child_session_closed", _build_child_session_closed, _match_child_session_closed, "structured_card"),
    MessageEnvelopeEntry("child_inactivity_threshold", _build_child_inactivity_threshold, _match_child_inactivity_threshold, "structured_card"),
    MessageEnvelopeEntry("child_report_ready", _build_child_report_ready, _match_child_report_ready, "structured_card"),
    MessageEnvelopeEntry("claude_pasted_content", _build_claude_pasted_content, _match_claude_pasted_content, "chat_prose"),
)

_ENTRIES_BY_KIND = {entry.kind: entry for entry in MESSAGE_ENVELOPES}


def build_message_envelope(kind: str, **fields: Any) -> str:
    """Build exact wire bytes for a registered kind."""
    try:
        entry = _ENTRIES_BY_KIND[kind]
    except KeyError as exc:
        raise ValueError(f"unknown message envelope kind: {kind}") from exc
    return entry.build(**fields)


def match_message_envelope(
    text: str,
    *,
    provider: str | None = None,
    authenticated: bool | None = None,
    provider_wrapper: object = None,
) -> dict[str, Any] | None:
    """Return a registered tag, with provider context where ingest has it."""
    candidate = str(text or "")
    for entry in MESSAGE_ENVELOPES:
        matched = entry.match(
            candidate,
            provider=provider,
            authenticated=authenticated,
            provider_wrapper=provider_wrapper,
        )
        if matched is not None:
            return matched
    return None


def build_notice_body(notice_id: object, body: object) -> str:
    """Preserve the historical marker primitive while routing construction here."""
    marker = build_message_envelope("notice_marker", notice_id=notice_id)
    text = str(body or "")
    return text if marker in text else f"{marker}\n{text}"


def get_untagged_marker_count() -> int:
    return _untagged_marker_count


def _has_marker_prefix(text: str) -> bool:
    return bool(re.match(r"^\[pentacle-notice:[^\]\s]+\](?:\n|$)", text))


def _record_untagged_marker(text: str) -> None:
    global _untagged_marker_count
    if _untagged_marker_count < _MAX_UNTAGGED_MARKERS:
        _untagged_marker_count += 1
    log.warning(
        "untagged marker-prefixed USER event",
        extra={"subsystem": "message_envelopes", "bug_ref": BUG_REF},
    )


def annotate_message_envelope(event: dict[str, Any]) -> dict[str, Any]:
    """Add the additive tag before projection, retaining original source bytes."""
    result = dict(event)
    raw = event.get("raw")
    clean_raw = dict(raw) if isinstance(raw, dict) else {}
    text = str(event.get("text") or "")
    provider_source = clean_raw.get("provider_content")
    source = provider_source if isinstance(provider_source, str) else text
    if str(event.get("kind") or "").upper() != "USER":
        return result
    provider = str(event.get("provider") or "") or None
    provider_wrapper = event.get("provider_wrapper")
    authenticated_wrapper = bool(
        provider == "claude"
        and isinstance(provider_wrapper, dict)
        and provider_wrapper.get("kind") == "claude_pasted_content"
        and provider_wrapper.get("provenance") == "grammar"
        and isinstance(provider_wrapper.get("id"), str)
        and re.fullmatch(r"[0-9a-f]+", provider_wrapper["id"]),
    )
    # A provider wrapper is authenticated first. A normalized event can carry
    # only its already-unwrapped text, while an ingress event retains the
    # provider bytes in raw.provider_content; both paths use the same adapter.
    matched = None
    if authenticated_wrapper:
        matched = match_message_envelope(
            source,
            provider=provider,
            authenticated=True,
            provider_wrapper=provider_wrapper,
        )
        if matched is None and source != text:
            matched = match_message_envelope(
                text,
                provider=provider,
                authenticated=True,
                provider_wrapper=provider_wrapper,
            )
    if matched is None:
        matched = match_message_envelope(
            text,
            provider=provider,
            authenticated=False,
        )
    if matched is not None:
        result["message_envelope"] = {"kind": matched["kind"], "id": matched["id"], "schema_version": SCHEMA_VERSION}
    elif _has_marker_prefix(text):
        _record_untagged_marker(text)
    clean_raw["envelope_source"] = source
    result["raw"] = clean_raw
    return result
