"""Deterministic replay harness for the daemon-owned assistant router.

The harness deliberately keeps corpus construction, router-input construction,
adapter execution, scoring, and miss curation separate.  A run may write a
manifest, report, and misses file, but it never mutates the checked-in fixture
unless the explicit ``--merge-misses`` command is used.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import statistics
import sys
import time
from typing import Any, Awaitable, Callable, Iterable


CHAT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = CHAT_DIR.parents[1]
if str(CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(CHAT_DIR))

from assistant_composite import AssistantComposite, AssistantCompositeConfig  # noqa: E402
from assistant_router import AssistantRouterAdapter  # noqa: E402
from claude_jsonl_norm import normalize_claude_jsonl_records  # noqa: E402
from store import Store  # noqa: E402


HARNESS_SCHEMA = "pentacle.assistant-router-replay/v1"
PARSER_VERSION = "claude-jsonl-normalizer-v1"
REDACTOR_VERSION = "ordered-redactor-v3"
GENERATOR_VERSION = "interleaved-thread-generator-v1"
LABELS = ("lane", "new_topic", "clarify", "conversation", "defer")
DISPOSITIONS = set(LABELS)
PUBLISH_KINDS = {"prose", "question", "result", "status"}
ROUTER_FIELDS = {
    "schema_version", "disposition", "lane_id", "depends_on_message_id", "reason",
}
_PASTED_CONTENT_RE = re.compile(
    r"<pasted_content(?:\s+id=(?:\"[^\"]*\"|'[^']*'))?\s*>(?P<body>[\s\S]*?)"
    r"</pasted_content(?:\s+id=(?:\"[^\"]*\"|'[^']*'))?\s*>",
    re.IGNORECASE,
)
_ASSIGNMENT_RE = re.compile(
    r"""(?ix)
    (?P<prefix>
        (?<![A-Za-z0-9])
        (?:
            (?:export\s+)?[A-Za-z_][A-Za-z0-9_.-]*\s*(?:=|:)\s*
            |["'`][A-Za-z_][A-Za-z0-9_.-]*["'`]\s*:\s*
        )
    )
    (?:
        (?P<quote>["'`])(?P<quoted>[\s\S]*?)(?P=quote)
        |(?P<bare>[^\s,;}\]><"'`]+)
    )
    """
)
_SECRET_TOKEN_RE = re.compile(r"[A-Za-z0-9._~+/=-]{16,}")
_SECRET_PREFIXES = (
    "sk-", "ghp_", "github_pat_", "AKIA", "xox", "ya29", "-----BEGIN",
)


class HarnessError(RuntimeError):
    """A deterministic harness failure, safe to expose as a type only."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_value(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


class OrderedRedactor:
    """Fail-closed, deterministic redaction for every persisted sink."""

    _PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
        (
            "private_key",
            re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.I),
            "[REDACTED_PRIVATE_KEY]",
        ),
        (
            "bearer",
            re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I),
            "Bearer [REDACTED_BEARER]",
        ),
        (
            "jwt",
            re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
            "[REDACTED_JWT]",
        ),
        (
            "aws_access",
            re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
            "[REDACTED_AWS_ACCESS]",
        ),
        (
            "github_or_openai",
            re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_-]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})\b"),
            "[REDACTED_API_KEY]",
        ),
        (
            "absolute_path",
            re.compile(r"(?<![A-Za-z0-9])(?:/Users|/home|/private/tmp|/var/folders)/[^\s\"'<>`]+"),
            "[REDACTED_ABSOLUTE_PATH]",
        ),
    )

    @staticmethod
    def _secret_shaped(value: str) -> bool:
        candidate = str(value).strip()
        return bool(candidate) and (
            bool(_SECRET_TOKEN_RE.fullmatch(candidate))
            or any(candidate.startswith(prefix) for prefix in _SECRET_PREFIXES)
        )

    @classmethod
    def _redact_assignment(cls, match: re.Match[str]) -> str:
        quote = match.group("quote")
        value = match.group("quoted") if quote else match.group("bare")
        if not cls._secret_shaped(value or ""):
            return match.group(0)
        replacement = "[REDACTED_SECRET]"
        if quote:
            return f"{match.group('prefix')}{quote}{replacement}{quote}"
        return f"{match.group('prefix')}{replacement}"

    def redact_text(self, text: str) -> str:
        result = str(text)
        try:
            result = _ASSIGNMENT_RE.sub(self._redact_assignment, result)
            for _name, pattern, replacement in self._PATTERNS:
                result = pattern.sub(replacement, result)
            for canary in _known_canaries():
                result = result.replace(canary, "[REDACTED_CANARY]")
        except Exception as exc:  # pragma: no cover - regexes are static
            raise HarnessError("redaction_failed") from exc
        return result

    def redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, list):
            return [self.redact_value(item) for item in value]
        if isinstance(value, dict):
            return {str(key): self.redact_value(item) for key, item in value.items()}
        return value

    def assert_canary_absent(self, value: Any, canaries: Iterable[str]) -> None:
        """Assert that an already-redacted sink output contains no canary."""
        rendered = canonical_bytes(value).decode("utf-8")
        for canary in canaries:
            if canary in rendered:
                raise HarnessError("redaction_canary_leaked")


REDACTOR = OrderedRedactor()


def strip_pasted_content(text: str) -> str:
    """Remove only the Claude pasted-content envelope, retaining its body."""
    return _PASTED_CONTENT_RE.sub(lambda match: match.group("body"), str(text or ""))


def _safe_text(text: str) -> str:
    return REDACTOR.redact_text(strip_pasted_content(text))


def _safe_path(path: Path) -> str:
    return REDACTOR.redact_text(str(path.resolve()))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise HarnessError("transcript_read_failed") from exc
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (TypeError, json.JSONDecodeError) as exc:
            raise HarnessError("transcript_invalid_json") from exc
        if not isinstance(value, dict):
            raise HarnessError("transcript_record_invalid")
        records.append(value)
    return records


def _normalise_transcript(path: Path, *, host: str, session_name: str) -> list[dict[str, Any]]:
    records = _read_jsonl(path)
    try:
        events = normalize_claude_jsonl_records(records, host=host, session_name=session_name)
    except Exception as exc:
        raise HarnessError("transcript_normalization_failed") from exc
    turns: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        if str(event.get("kind") or "") != "USER":
            continue
        text = _safe_text(str(event.get("text") or "")).strip()
        if not text:
            continue
        raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
        record_id = str(raw.get("jsonl_record_uuid") or f"event-{index}")
        turns.append({
            "turn_id": f"{record_id}:{index}",
            "text": text,
            "timestamp": str(event.get("timestamp") or ""),
        })
    return turns


def _session_record(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except (OSError, IOError) as exc:
        raise HarnessError("transcript_read_failed") from exc
    session_name = path.stem
    host = "transcript"
    turns = _normalise_transcript(path, host=host, session_name=session_name)
    file_digest = sha256_bytes(raw)
    identity = sha256_value({"name": session_name, "file_sha256": file_digest})[:24]
    return {
        "session_id": identity,
        "session_name": session_name,
        "source_path": _safe_path(path),
        "source_path_sha256": sha256_bytes(str(path.resolve()).encode("utf-8")),
        "file_sha256": file_digest,
        "turns": turns,
    }


def _manifest_corpus_digest(sessions: list[dict[str, Any]]) -> str:
    material = [
        {
            "session_id": session.get("session_id"),
            "file_sha256": session.get("file_sha256"),
            "turns": session.get("turns", []),
        }
        for session in sessions
    ]
    return sha256_value(material)


def build_manifest(
    transcript_root: str | os.PathLike[str], *, seed: int, threads: int,
) -> dict[str, Any]:
    root = Path(transcript_root).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise HarnessError("transcript_root_unavailable")
    paths = sorted(path for path in root.rglob("*.jsonl") if path.is_file())
    if not paths:
        raise HarnessError("transcript_population_empty")
    sessions = [_session_record(path) for path in paths]
    if len(sessions) < 2:
        raise HarnessError("transcript_population_too_small")
    rng = random.Random(seed)
    count = min(max(2, int(threads)), 4, len(sessions))
    selected = sorted(rng.sample(sessions, count), key=lambda item: str(item["session_id"]))
    generator_config = {
        "version": GENERATOR_VERSION,
        "max_turns_per_session": 12,
        "short_replies": ["yes", "continue", "do it", "not that one"],
        "synthetic_bursts": 2,
        "synthetic_gaps": [0.25, 1.0, 3.0],
    }
    manifest: dict[str, Any] = {
        "schema_version": HARNESS_SCHEMA,
        "transcript_root": REDACTOR.redact_text(str(root)),
        "seed": int(seed),
        "thread_count": count,
        "parser_version": PARSER_VERSION,
        "redactor_version": REDACTOR_VERSION,
        "generator": generator_config,
        "sessions": selected,
    }
    manifest["corpus_digest"] = _manifest_corpus_digest(selected)
    sanitized = REDACTOR.redact_value(manifest)
    sanitized["manifest_sha256"] = sha256_value(sanitized)
    return sanitized


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarnessError("manifest_invalid") from exc
    if not isinstance(value, dict) or value.get("schema_version") != HARNESS_SCHEMA:
        raise HarnessError("manifest_schema_invalid")
    saved_digest = value.get("manifest_sha256")
    if saved_digest and saved_digest != sha256_value({k: v for k, v in value.items() if k != "manifest_sha256"}):
        raise HarnessError("manifest_digest_mismatch")
    REDACTOR.assert_canary_absent(value, _known_canaries())
    sessions = value.get("sessions")
    if not isinstance(sessions, list) or not sessions:
        raise HarnessError("manifest_sessions_invalid")
    if value.get("corpus_digest") != _manifest_corpus_digest(sessions):
        raise HarnessError("manifest_corpus_digest_mismatch")
    return value


def verify_manifest_sources(manifest: dict[str, Any], transcript_root: Path) -> None:
    """Re-read selected source files by digest and reject corpus drift.

    The manifest deliberately does not persist an unredacted absolute path.
    A candidate therefore resolves each selected file by its immutable raw
    SHA-256 under the configured transcript root, then repeats normalization
    and redaction before comparing the frozen turns.
    """
    root = transcript_root.expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise HarnessError("transcript_root_unavailable")
    by_digest: dict[str, list[Path]] = defaultdict(list)
    for path in sorted(root.rglob("*.jsonl")):
        if not path.is_file():
            continue
        try:
            digest = sha256_bytes(path.read_bytes())
        except (OSError, IOError) as exc:
            raise HarnessError("transcript_read_failed") from exc
        by_digest[digest].append(path)
    for session in manifest.get("sessions", []):
        if not isinstance(session, dict):
            raise HarnessError("manifest_sessions_invalid")
        digest = str(session.get("file_sha256") or "")
        path_digest = str(session.get("source_path_sha256") or "")
        matches = [
            path for path in by_digest.get(digest, [])
            if sha256_bytes(str(path.resolve()).encode("utf-8")) == path_digest
        ]
        if len(matches) != 1:
            raise HarnessError("transcript_corpus_drift")
        refreshed = _session_record(matches[0])
        if refreshed.get("turns") != session.get("turns"):
            raise HarnessError("transcript_corpus_drift")


def write_json(path: Path, value: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        sanitized = REDACTOR.redact_value(value)
        REDACTOR.assert_canary_absent(sanitized, _known_canaries())
        path.write_bytes(canonical_bytes(sanitized) + b"\n")
    except (OSError, IOError) as exc:
        raise HarnessError("artifact_write_failed") from exc


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        sanitized_rows = []
        for row in rows:
            sanitized = REDACTOR.redact_value(row)
            REDACTOR.assert_canary_absent(sanitized, _known_canaries())
            sanitized_rows.append(sanitized)
        path.write_text(
            "".join(
                json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n"
                for row in sanitized_rows
            ),
            encoding="utf-8",
        )
    except (OSError, IOError) as exc:
        raise HarnessError("artifact_write_failed") from exc


def _fixture_cases(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarnessError("fixture_invalid") from exc
    if not isinstance(value, list):
        raise HarnessError("fixture_invalid")
    REDACTOR.assert_canary_absent(value, _known_canaries())
    return [item for item in value if isinstance(item, dict)]


def _label_for_decision(decision: dict[str, Any]) -> str:
    disposition = str(decision.get("disposition") or "")
    return disposition if disposition in LABELS else "conversation"


def _decision_for_turn(text: str, lane_ids: list[str], unresolved_ids: list[str]) -> dict[str, Any]:
    lowered = text.casefold()
    short = lowered.strip().rstrip(".!?")
    if unresolved_ids and any(token in lowered for token in ("pending", "answer", "resolve that", "use the result")):
        return {
            "schema_version": "assistant-router/v1", "disposition": "defer",
            "lane_id": None, "depends_on_message_id": unresolved_ids[0],
            "reason": "direct pending dependency",
        }
    if short in {"yes", "continue", "continue it", "do it", "not that one", "approve it"}:
        return {
            "schema_version": "assistant-router/v1", "disposition": "clarify",
            "lane_id": None, "depends_on_message_id": None,
            "reason": "short reply needs a grounded referent",
        }
    for lane_id in lane_ids:
        tokens = [token for token in re.findall(r"[a-z0-9]+", lane_id.casefold()) if len(token) >= 4]
        if tokens and all(token in lowered for token in tokens):
            return {
                "schema_version": "assistant-router/v1", "disposition": "lane",
                "lane_id": lane_id, "depends_on_message_id": None,
                "reason": "explicit lane reference",
            }
    if re.search(r"\b(start|create|build|plan|review|investigate|fix|resolve|design|choose|prioritize)\b", lowered):
        return {
            "schema_version": "assistant-router/v1", "disposition": "new_topic",
            "lane_id": None, "depends_on_message_id": None,
            "reason": "new work topic",
        }
    return {
        "schema_version": "assistant-router/v1", "disposition": "conversation",
        "lane_id": None, "depends_on_message_id": None,
        "reason": "ordinary conversation",
    }


def _case_key(prefix: str, value: Any, index: int = 0) -> str:
    material = {"value": value, "index": index}
    return f"{prefix}-{sha256_value(material)[:20]}"


def generate_cases(manifest: dict[str, Any], fixture_path: Path) -> list[dict[str, Any]]:
    """Create stable labelled cases, retaining a full expected wire oracle."""
    rng = random.Random(int(manifest.get("seed") or 0))
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    fixture = _fixture_cases(fixture_path)

    def add_case(case: dict[str, Any]) -> None:
        key = str(case.get("case_key") or "")
        if not key or key in seen:
            return
        expected = case.get("expected")
        if not isinstance(expected, dict) or str(expected.get("disposition") or "") not in DISPOSITIONS:
            return
        case["label"] = _label_for_decision(expected)
        case["expected"] = REDACTOR.redact_value(expected)
        seen.add(key)
        cases.append(REDACTOR.redact_value(case))

    for index, item in enumerate(fixture):
        expected = item.get("expected")
        if not isinstance(expected, dict):
            continue
        add_case({
            "case_key": _case_key("fixture", item.get("name") or item.get("input"), index),
            "source": "checked-in-fixture",
            "thread_id": "fixture",
            "input_text": _safe_text(str(item.get("input") or "")),
            "lane_ids": [str(value) for value in item.get("lanes", []) if isinstance(value, str)],
            "unresolved_ids": [str(value) for value in item.get("unresolved", []) if isinstance(value, str)],
            "expected": expected,
            "synthetic_offset_s": float(index % 4),
        })

    selected_sessions = manifest.get("sessions", [])
    for session in selected_sessions:
        if not isinstance(session, dict):
            continue
        session_id = str(session.get("session_id") or "")
        turns = session.get("turns", [])
        if not session_id or not isinstance(turns, list):
            continue
        lane_ids = [f"thread-{session_id[:8]}"]
        for index, turn in enumerate(turns[:12]):
            if not isinstance(turn, dict):
                continue
            text = _safe_text(str(turn.get("text") or "")).strip()
            if not text:
                continue
            unresolved = [f"pending-{session_id[:8]}"] if "pending" in text.casefold() else []
            expected = _decision_for_turn(text, lane_ids, unresolved)
            add_case({
                "case_key": _case_key("transcript", f"{session_id}:{turn.get('turn_id')}", index),
                "source": "transcript",
                "thread_id": session_id,
                "input_text": text,
                "lane_ids": lane_ids if expected["disposition"] == "lane" else [],
                "unresolved_ids": unresolved,
                "expected": expected,
                "synthetic_offset_s": float((index * 0.25) % 3),
            })

    # Ensure every class has a useful denominator even when the selected real
    # transcripts are sparse.  These are generated after a question/burst and
    # are deliberately explicit in the manifest-backed case list.
    templates = {
        "lane": [
            ("Continue the payments migration.", ["payments"], []),
            ("For payments, check the receipt.", ["payments"], []),
            ("Give the mobile lane status.", ["mobile"], []),
            ("For incident-42, check the error budget.", ["incident-42"], []),
            ("Summarize the payments work.", ["payments"], []),
        ],
        "new_topic": [
            ("Start a production migration plan.", [], []),
            ("Review the authentication boundary.", [], []),
            ("Investigate the customer outage.", [], []),
            ("Design the full system boundary.", [], []),
            ("Resolve a conflict between these specifications.", [], []),
        ],
        "clarify": [
            ("Yes, do it.", ["payments", "mobile"], []),
            ("Continue it.", ["payments", "mobile"], []),
            ("Do it.", ["payments", "mobile"], []),
            ("Not that one.", ["payments", "mobile"], []),
            ("Approve it.", ["payments", "mobile"], []),
        ],
        "conversation": [
            ("How are you?", [], []),
            ("Explain TCP and UDP.", [], []),
            ("Describe this attachment.", ["payments"], []),
            ("What is the weather tomorrow?", [], ["pending-1"]),
            ("Make this paragraph friendlier.", [], []),
        ],
        "defer": [
            ("Use the answer to the pending admission.", [], ["pending-1"]),
            ("Resolve that pending input.", [], ["pending-2"]),
            ("Use the result of pending-3.", [], ["pending-3"]),
            ("Before we continue, resolve the pending input.", [], ["pending-4"]),
            ("Answer the pending request first.", [], ["pending-5"]),
        ],
    }
    for label in LABELS:
        current = sum(1 for case in cases if case.get("label") == label)
        for index, (text, lanes, unresolved) in enumerate(templates[label]):
            if current >= 5:
                break
            if label == "lane":
                expected = {
                    "schema_version": "assistant-router/v1", "disposition": "lane",
                    "lane_id": lanes[0], "depends_on_message_id": None,
                    "reason": "synthetic lane follow-up",
                }
            elif label == "new_topic":
                expected = {
                    "schema_version": "assistant-router/v1", "disposition": "new_topic",
                    "lane_id": None, "depends_on_message_id": None,
                    "reason": "synthetic new work topic",
                }
            elif label == "clarify":
                expected = {
                    "schema_version": "assistant-router/v1", "disposition": "clarify",
                    "lane_id": None, "depends_on_message_id": None,
                    "reason": "synthetic ambiguous short reply",
                }
            elif label == "defer":
                expected = {
                    "schema_version": "assistant-router/v1", "disposition": "defer",
                    "lane_id": None, "depends_on_message_id": unresolved[0],
                    "reason": "synthetic pending dependency",
                }
            else:
                expected = {
                    "schema_version": "assistant-router/v1", "disposition": "conversation",
                    "lane_id": None, "depends_on_message_id": None,
                    "reason": "synthetic ordinary conversation",
                }
            add_case({
                "case_key": _case_key("synthetic", f"{label}:{text}", index),
                "source": "synthetic-burst",
                "thread_id": f"synthetic-{label}",
                "input_text": text,
                "lane_ids": lanes,
                "unresolved_ids": unresolved,
                "expected": expected,
                "synthetic_offset_s": float(rng.randrange(0, 12)) / 4.0,
            })
            current += 1
    if not cases:
        raise HarnessError("generated_population_empty")
    return cases


async def _open_backend(store: Store, stream_id: str) -> dict[str, Any]:
    host, name = stream_id.split(":", 1)
    return await store.open_session(host, name, provider="codex")


async def _seed_router_input(case: dict[str, Any]) -> dict[str, Any]:
    """Seed durable state and call the daemon's actual router-input builder."""
    stream_id = "replay-host-chat:assistant"
    authority_id = "replay-host-authority:authority"
    luna_id = "replay-host-conversation:conversation"
    store = Store(":memory:")
    store.start()
    try:
        authority = await _open_backend(store, authority_id)
        await _open_backend(store, luna_id)
        composite = AssistantComposite(
            store,
            config=AssistantCompositeConfig(
                enabled=True,
                stream_id=stream_id,
                router_endpoint="ssh://replay-router/assistant-router-v1",
                astra_stream_id=authority_id,
                luna_stream_id=luna_id,
            ),
        )
        await composite.ensure_projection()
        lane_ids = [str(value) for value in case.get("lane_ids", []) if str(value)]
        for index, lane_id in enumerate(dict.fromkeys(lane_ids)):
            await store.apply_assistant_composite_operation(
                stream_id=stream_id,
                operation_id=f"admit-{case['case_key']}-{index}",
                operation="lane.admit",
                lane_id=lane_id,
                dispatch_id=f"admit-dispatch-{case['case_key']}-{index}",
                actor_stream_id=authority_id,
                payload={
                    "mode": "new", "subject": f"{lane_id} work",
                    "request_message_id": f"seed-{case['case_key']}-{index}",
                },
            )
            seed_identity = f"seed-{case['case_key']}-{index}"
            seed = await store.admit_assistant_composite_input(
                stream_id=stream_id, input_identity=seed_identity, input_request_id=seed_identity,
                body=f"Start {lane_id} work.", attachments=[], reply_to_message_id=None,
                reply_to_question_id=None, actor_stream_id="replay:seed",
            )
            seed_dispatch = f"seed-dispatch-{case['case_key']}-{index}"
            await store.update_assistant_composite_route(
                seed["route_id"], routing_state="resolved", delivery_state="landed",
                dispatch_id=seed_dispatch, route_target=authority_id,
                route_target_generation=authority["session_generation"], route_payload={
                    "schema_version": "assistant-router/v1", "disposition": "lane",
                    "lane_id": lane_id, "depends_on_message_id": None, "reason": "seed",
                },
            )
            message = f"Latest durable {lane_id} result."
            await store.record_assistant_composite_publication(
                stream_id=stream_id, publication_key=f"publication-{case['case_key']}-{index}",
                dispatch_id=seed_dispatch, reply_to_message_id=seed_identity,
                reply_to_question_id=None, publish_kind="prose", attachment_ids=[], evidence_refs=[],
                canonical_payload={
                    "composite_stream_id": stream_id, "dispatch_id": seed_dispatch,
                    "reply_to_message_id": seed_identity, "reply_to_question_id": None,
                    "publish_kind": "prose", "message": message,
                    "attachment_ids": [], "evidence_refs": [],
                },
                event={
                    "stream_id": stream_id, "provider": "composite", "kind": "ASSIST_TEXT",
                    "text": message, "message_id": f"publication:publication-{case['case_key']}-{index}",
                    "reply_to_message_id": seed_identity, "reply_to_question_id": None,
                    "publish_kind": "prose", "attachments": [],
                    "timestamp": "2026-09-21T00:00:00Z",
                    "raw": {"assistant_composite": True, "publish_kind": "prose",
                            "dispatch_id": seed_dispatch, "reply_to_message_id": seed_identity},
                }, actor_stream_id=authority_id, actor_generation=None,
            )
        for index, dependency in enumerate(case.get("unresolved_ids", [])):
            identity = str(dependency)
            await store.admit_assistant_composite_input(
                stream_id=stream_id, input_identity=identity, input_request_id=identity,
                body=f"Pending input {identity}.", attachments=[], reply_to_message_id=None,
                reply_to_question_id=None, actor_stream_id="replay:seed",
            )
        current_identity = f"case-{case['case_key']}"
        current = await store.admit_assistant_composite_input(
            stream_id=stream_id, input_identity=current_identity, input_request_id=current_identity,
            body=str(case.get("input_text") or ""), attachments=[], reply_to_message_id=None,
            reply_to_question_id=None, actor_stream_id="replay:operator",
        )
        return await composite._router_input(current)
    finally:
        store.stop()


async def materialize_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    materialized: list[dict[str, Any]] = []
    for case in cases:
        router_input = await _seed_router_input(case)
        frozen = canonical_bytes(router_input)
        item = dict(case)
        item["router_input"] = router_input
        item["router_input_canonical"] = frozen.decode("utf-8")
        item["router_input_sha256"] = sha256_bytes(frozen)
        materialized.append(item)
    return materialized


def _validate_decision(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != ROUTER_FIELDS:
        raise ValueError("router_result_invalid")
    if value.get("schema_version") != "assistant-router/v1":
        raise ValueError("router_result_invalid")
    if value.get("disposition") not in DISPOSITIONS:
        raise ValueError("router_result_invalid")
    if value.get("lane_id") is not None and not isinstance(value.get("lane_id"), str):
        raise ValueError("router_result_invalid")
    if value.get("depends_on_message_id") is not None and not isinstance(value.get("depends_on_message_id"), str):
        raise ValueError("router_result_invalid")
    if not isinstance(value.get("reason"), str) or len(value["reason"]) > 240:
        raise ValueError("router_result_invalid")
    return dict(value)


def _decision_key(value: dict[str, Any]) -> tuple[Any, ...]:
    return (
        value.get("schema_version"), value.get("disposition"),
        value.get("lane_id"), value.get("depends_on_message_id"),
    )


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _error_class(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, (ValueError, json.JSONDecodeError)):
        return "invalid_json"
    if isinstance(exc, (OSError, ConnectionError, RuntimeError)):
        return "transport"
    return "harness"


def _harness_config(manifest: dict[str, Any], cases: list[dict[str, Any]], *, endpoint: str, action_path: str, fixture_path: Path) -> dict[str, Any]:
    return {
        "schema_version": HARNESS_SCHEMA,
        "generator_version": GENERATOR_VERSION,
        "parser_version": manifest.get("parser_version"),
        "redactor_version": manifest.get("redactor_version"),
        "seed": manifest.get("seed"),
        "thread_count": manifest.get("thread_count"),
        "generator": manifest.get("generator"),
        "manifest_corpus_digest": manifest.get("corpus_digest"),
        "case_keys_digest": sha256_value([case.get("case_key") for case in cases]),
        "fixture_sha256": sha256_bytes(fixture_path.read_bytes()) if fixture_path.exists() else None,
        "transport": {"endpoint": endpoint, "action_path": REDACTOR.redact_text(action_path)},
        "router_schema": "assistant-router/v1",
        "sequential": True,
        "oracle": "full-router-input-json-bytes-per-case",
    }


Classifier = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class ReplayResult:
    report: dict[str, Any]
    misses: list[dict[str, Any]]


async def run_cases(
    cases: list[dict[str, Any]],
    classifier: Classifier,
    *,
    harness_config: dict[str, Any],
    baseline_report: dict[str, Any] | None = None,
) -> ReplayResult:
    config_digest = sha256_value(harness_config)
    if baseline_report is not None:
        if baseline_report.get("harness_config_sha256") != config_digest:
            report = {
                "schema_version": HARNESS_SCHEMA, "status": "HARNESS_ERROR",
                "bar": "fail", "error_classes": {"harness_config_mismatch": 1},
                "harness_config": harness_config, "harness_config_sha256": config_digest,
                "cases": [], "next_action": "rerun with identical harness configuration",
            }
            return ReplayResult(REDACTOR.redact_value(report), [])
        baseline_cases = {item.get("case_key"): item for item in baseline_report.get("cases", [])}
        for case in cases:
            prior = baseline_cases.get(case.get("case_key"))
            if prior is None or prior.get("router_input_sha256") != case.get("router_input_sha256"):
                report = {
                    "schema_version": HARNESS_SCHEMA, "status": "HARNESS_ERROR",
                    "bar": "fail", "error_classes": {"router_input_oracle_drift": 1},
                    "harness_config": harness_config, "harness_config_sha256": config_digest,
                    "cases": [], "next_action": "freeze the manifest and per-case router inputs again",
                }
                return ReplayResult(REDACTOR.redact_value(report), [])

    results: list[dict[str, Any]] = []
    misses: list[dict[str, Any]] = []
    latencies: list[float] = []
    error_classes: Counter[str] = Counter()
    confusion: dict[str, Counter[str]] = {label: Counter() for label in LABELS}
    exact_count = 0
    wrong_lane_count = 0
    for index, case in enumerate(cases):
        expected = case["expected"]
        result: dict[str, Any] = {
            "case_key": case["case_key"], "label": case["label"],
            "router_input_sha256": case["router_input_sha256"],
            "expected": REDACTOR.redact_value(expected),
            "warmup": index < 2,
        }
        frozen = str(case.get("router_input_canonical") or "").encode("utf-8")
        if sha256_bytes(frozen) != case.get("router_input_sha256"):
            result.update({"status": "error", "error_class": "router_input_oracle_drift"})
            error_classes["router_input_oracle_drift"] += 1
            results.append(result)
            misses.append({"case_key": case["case_key"], "error_class": "router_input_oracle_drift"})
            continue
        started = time.perf_counter()
        try:
            observed = _validate_decision(await classifier(json.loads(frozen.decode("utf-8"))))
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            result["latency_ms"] = round(elapsed_ms, 3)
            if index >= 2:
                latencies.append(elapsed_ms)
            result["observed"] = REDACTOR.redact_value(observed)
            result["status"] = "ok"
            result["exact"] = _decision_key(observed) == _decision_key(expected)
            result["target_class"] = observed.get("disposition")
            confusion[case["label"]][str(observed.get("disposition"))] += 1
            if result["exact"]:
                exact_count += 1
            if expected.get("disposition") == "lane" and (
                observed.get("disposition") != "lane" or observed.get("lane_id") != expected.get("lane_id")
            ):
                wrong_lane_count += 1
            if not result["exact"]:
                misses.append({
                    "case_key": case["case_key"], "label": case["label"],
                    "router_input": REDACTOR.redact_value(case["router_input"]),
                    "router_input_sha256": case["router_input_sha256"],
                    "expected": REDACTOR.redact_value(expected),
                    "observed": REDACTOR.redact_value(observed),
                })
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            result.update({"status": "error", "error_class": _error_class(exc), "latency_ms": round(elapsed_ms, 3)})
            error_classes[result["error_class"]] += 1
            confusion[case["label"]]["error"] += 1
            misses.append({
                "case_key": case["case_key"], "label": case["label"],
                "router_input": REDACTOR.redact_value(case["router_input"]),
                "router_input_sha256": case["router_input_sha256"],
                "expected": REDACTOR.redact_value(expected),
                "error_class": result["error_class"],
            })
        results.append(result)

    denominators = {label: sum(row.values()) for label, row in confusion.items()}
    exact_by_class = {
        label: confusion[label].get(label, 0) for label in LABELS
    }
    eligible = all(denominators[label] >= 5 for label in LABELS)
    total = len(cases)
    micro = exact_count / total if total else 0.0
    macro_values = [exact_by_class[label] / denominators[label] for label in LABELS if denominators[label]]
    warm_p50 = _percentile(latencies, 0.50)
    warm_p95 = _percentile(latencies, 0.95)
    bars = {
        "all_classes_eligible": eligible,
        "errors_zero": not error_classes,
        "micro_accuracy_at_least_90_percent": micro >= 0.90,
        "wrong_lane_zero": wrong_lane_count == 0,
        "warm_p50_under_3000_ms": warm_p50 is not None and warm_p50 < 3000.0,
    }
    report: dict[str, Any] = {
        "schema_version": HARNESS_SCHEMA,
        "status": "HARNESS_ERROR" if error_classes else "MEASURED",
        "bar": "pass" if all(bars.values()) else "fail",
        "harness_config": REDACTOR.redact_value(harness_config),
        "harness_config_sha256": config_digest,
        "oracle": {
            "kind": "full-router-input-json-bytes-per-case",
            "case_count": total,
            "case_oracle_sha256": sha256_value({case["case_key"]: case["router_input_sha256"] for case in cases}),
        },
        "scoring": {
            "micro_exact": exact_count, "total": total,
            "micro_accuracy": round(micro, 6),
            "class_denominators": denominators,
            "class_exact": exact_by_class,
            "eligible_macro_accuracy": round(statistics.fmean(macro_values), 6) if macro_values else 0.0,
            "wrong_lane_count": wrong_lane_count,
            "error_count": sum(error_classes.values()),
            "confusion": {label: {column: confusion[label].get(column, 0) for column in (*LABELS, "error")} for label in LABELS},
        },
        "latency": {
            "warm": {"p50_ms": warm_p50, "p95_ms": warm_p95, "sample_count": len(latencies), "warmups_discarded": 2},
            "cold": {"status": "unobserved", "sample_count": 0},
        },
        "error_classes": dict(error_classes),
        "bars": bars,
        "cases": results,
        "next_action": "merge only explicitly reviewed misses" if misses else "retain the measured run and fixture digest",
    }
    sanitized_report = REDACTOR.redact_value(report)
    sanitized_misses = REDACTOR.redact_value(misses)
    REDACTOR.assert_canary_absent(sanitized_report, _known_canaries())
    REDACTOR.assert_canary_absent(sanitized_misses, _known_canaries())
    return ReplayResult(sanitized_report, sanitized_misses)


def _known_canaries() -> tuple[str, ...]:
    return (
        "AKIAIOSFODNN7EXAMPLE", "sk-live-router-replay-secret", "Bearer router-replay-secret",
        "aws-env-router-replay-secret", "openai-env-router-replay-secret",
        "anthropic-env-router-replay-secret", "github-env-router-replay-secret",
        "aws-quoted-structured-sentinel-123456789", "openai-quoted-structured-sentinel-123456789",
        "anthropic-quoted-structured-sentinel-123456789", "github-quoted-structured-sentinel-123456789",
        "-----BEGIN PRIVATE KEY-----", "password=router-replay-secret",
        "/Users/example/private/router-replay-secret",
    )


def merge_misses(misses_path: Path, fixture_path: Path) -> dict[str, Any]:
    try:
        misses = [json.loads(line) for line in misses_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        existing = json.loads(fixture_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarnessError("miss_merge_input_invalid") from exc
    if not isinstance(existing, list) or not all(isinstance(item, dict) for item in misses):
        raise HarnessError("miss_merge_input_invalid")
    existing = REDACTOR.redact_value(existing)
    before = sha256_bytes(fixture_path.read_bytes())
    by_key = {str(item.get("name") or item.get("case_key")): item for item in existing}
    added = 0
    for miss in misses:
        key = str(miss.get("case_key") or "")
        if not key or key in by_key:
            continue
        candidate = REDACTOR.redact_value({
            "name": key,
            "input": str((miss.get("router_input") or {}).get("body_excerpt") or ""),
            "expected": miss.get("expected"),
            "router_input_sha256": miss.get("router_input_sha256"),
        })
        by_key[key] = candidate
        existing.append(candidate)
        added += 1
    write_json(fixture_path, existing)
    after = sha256_bytes(fixture_path.read_bytes())
    return {"before_sha256": before, "after_sha256": after, "added": added}


async def _run_adapter(
    cases: list[dict[str, Any]], *, endpoint: str, action_path: str, timeout_s: float,
    harness_config: dict[str, Any], baseline_report: dict[str, Any] | None,
) -> ReplayResult:
    adapter = AssistantRouterAdapter(
        endpoint, timeout_s=timeout_s, action_path=action_path,
    )
    return await run_cases(
        cases, adapter.classify, harness_config=harness_config,
        baseline_report=baseline_report,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--transcript-root", type=Path, default=Path("~/.claude/projects"))
    parser.add_argument("--seed", type=int, default=20260921)
    parser.add_argument("--threads", type=int, default=3)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--misses", type=Path)
    parser.add_argument("--baseline-report", type=Path)
    parser.add_argument("--fixture", type=Path, default=CHAT_DIR / "tests/fixtures/assistant_router_v1_cases.json")
    parser.add_argument("--endpoint", default=os.environ.get("PENTACLE_ASSISTANT_ROUTER_ENDPOINT", ""))
    parser.add_argument("--action-path", default=os.environ.get("PENTACLE_ASSISTANT_ROUTER_ACTION_PATH", ""))
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--merge-misses", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.merge_misses:
            if args.misses is None:
                raise HarnessError("misses_path_required")
            receipt = merge_misses(args.misses, args.fixture)
            print(json.dumps(REDACTOR.redact_value(receipt), sort_keys=True))
            return 0
        if args.manifest is None:
            raise HarnessError("manifest_path_required")
        if args.manifest.exists():
            manifest = load_manifest(args.manifest)
            if int(manifest.get("seed")) != int(args.seed) or int(manifest.get("thread_count")) != min(max(2, args.threads), 4, len(manifest.get("sessions", []))):
                raise HarnessError("manifest_seed_or_thread_mismatch")
        else:
            manifest = build_manifest(args.transcript_root, seed=args.seed, threads=args.threads)
            write_json(args.manifest, manifest)
        verify_manifest_sources(manifest, args.transcript_root)
        REDACTOR.assert_canary_absent(manifest, _known_canaries())
        cases = generate_cases(manifest, args.fixture)
        cases = asyncio.run(materialize_cases(cases))
        if not args.endpoint or not args.action_path:
            raise HarnessError("adapter_config_missing")
        baseline_report = None
        if args.baseline_report is not None:
            try:
                baseline_report = json.loads(args.baseline_report.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise HarnessError("baseline_report_invalid") from exc
            if not isinstance(baseline_report, dict):
                raise HarnessError("baseline_report_invalid")
        config = _harness_config(
            manifest, cases, endpoint=args.endpoint, action_path=args.action_path,
            fixture_path=args.fixture,
        )
        result = asyncio.run(_run_adapter(
            cases, endpoint=args.endpoint, action_path=args.action_path,
            timeout_s=args.timeout, harness_config=config, baseline_report=baseline_report,
        ))
        if args.report is None or args.misses is None:
            raise HarnessError("report_and_misses_required")
        write_json(args.report, result.report)
        write_jsonl(args.misses, result.misses)
        print(json.dumps({
            "report": str(args.report), "misses": str(args.misses),
            "status": result.report.get("status"), "bar": result.report.get("bar"),
            "harness_config_sha256": result.report.get("harness_config_sha256"),
        }, sort_keys=True))
        return 0 if result.report.get("status") != "HARNESS_ERROR" else 2
    except HarnessError as exc:
        safe = {"status": "HARNESS_ERROR", "error_class": str(exc)}
        if args.report is not None:
            write_json(args.report, safe)
        if args.misses is not None:
            write_jsonl(args.misses, [])
        print(json.dumps(safe, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
