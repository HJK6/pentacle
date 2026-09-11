from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from datetime import datetime, timedelta, timezone

from .config import load_config
from . import prompt_protocol, triage
from . import role_baseline, schema
from .stream_id import discover_leader_stream_id_short, env_stream_id
from .wsclient import (
    asset_comment_resolve_once,
    asset_comments_list_once,
    asset_get_once,
    asset_health_once,
    asset_list_once,
    asset_publish_once,
    await_spawn_once,
    await_report_once,
    close_once,
    coordination_once,
    drain_sessions_once,
    fetch_snapshot,
    grant_token_once,
    inbound_audit_once,
    inspect_stream_once,
    ledger_get_once,
    investigation_once,
    nexus_once,
    repo_once,
    notification_await_once,
    notification_create_once,
    notification_resolve_by_dedup_once,
    park_once,
    prompt_answer_once,
    prompt_ask_once,
    prompt_cancel_once,
    prompt_list_once,
    prompt_status_once,
    reconcile_status_once,
    reparent_once,
    report_once,
    rename_once,
    role_get_once,
    role_set_once,
    schedule_once,
    send_cancel_once,
    send_receipt_once,
    send_once,
    set_visibility_once,
    spec_update_once,
    status_card_once,
    SPAWN_RPC_TIMEOUT_DEFAULT_S,
    spawn_once,
    spawn_cancel_once,
    spawn_status_once,
    spawn_freeze_once,
    spawn_catalog_get_once,
    stream_token_from_env,
    tell_once,
    upload_blob_once,
    upload_prompt_blob_once,
)

SERVICES_ROOT = Path(__file__).resolve().parents[2]
if str(SERVICES_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICES_ROOT))
from _shared.report_payload_v1 import SchemaError, validate as validate_report_payload
from _shared.asset_schema import (
    AssetBodyTooLarge,
    AssetValidationError,
    normalize_tags,
    validate_asset_payload,
)
from _shared.spawn_profiles import (
    HANDOFF_TUPLE_FIELDS,
    SpawnProfileError,
    resolve_handoff,
    resolve_spawn,
)


INITIAL_PROMPT_INLINE_CAP_BYTES = 16 * 1024
TELL_TTL_MIN_SECONDS = 1
TELL_TTL_MAX_SECONDS = 3600
# msg_id-mode `await` keeps the 30s default (a coordinated send/report is
# usually imminent). Stream-mode `await` (no --msg-id) is the fire-and-forget
# "spawn it, then await it" case where the worker runs for minutes, so a 30s
# default would time out almost every time — default much higher.
AWAIT_MSG_ID_MODE_DEFAULT_TIMEOUT = 30.0
AWAIT_STREAM_MODE_DEFAULT_TIMEOUT = 900.0
SPEC_ID_RE = re.compile(r"^[A-Za-z0-9_-]+__[A-Za-z0-9_-]+$")
SCHEDULE_MIN_DELAY_SECONDS = 60
SCHEDULE_MAX_DELAY_SECONDS = 365 * 24 * 60 * 60
SCHEDULE_STATES = (
    "pending",
    "retry_pending",
    "firing",
    "fired",
    "cancelled",
    "failed",
    "indeterminate",
    "expired",
)


def _report_payload_example(status: str) -> dict[str, object]:
    if status in {"done", "error", "aborted"}:
        payload: dict[str, object] = {
            "summary": "Completed the assigned work.",
            "findings": [
                {
                    "severity": "info",
                    "where": "services/agent-orch/agent_orch/cli.py:1",
                    "issue": "No blocking issues found.",
                    "suggested_fix": "n/a",
                }
            ],
            "next_action": "lead_merge",
        }
        if status in {"error", "aborted"}:
            payload["reason"] = "Unable to complete because the required prerequisite is unavailable."
        return payload
    return {"summary": "Work is still in progress."}


def _report_payload_shape(status: str) -> str:
    finding_shape = (
        "each findings[] object requires severity (blocking|major|minor|info), "
        "where (string), issue (string), and suggested_fix (string or null)"
    )
    if status in {"done", "error", "aborted"}:
        reason_requirement = (
            " and reason (non-empty string)" if status in {"error", "aborted"} else ""
        )
        return (
            f"ReportPayloadV1 for --status {status} requires summary (non-empty string), "
            f"findings (array), and next_action (non-empty string){reason_requirement}; "
            f"{finding_shape}. Optional fields: details (string/object/array), extras (object)."
        )
    return (
        "ReportPayloadV1 for --status progress has no required payload fields. "
        "Optional fields: summary, findings, next_action, details, extras; "
        f"if findings is present, {finding_shape}."
    )


def _format_report_payload_validation_failure(exc: BaseException, status: str, *, msg_id: int | None = None) -> str:
    if isinstance(exc, SchemaError):
        detail = str(exc).strip()
        header = f"{exc.code}: {detail}" if detail and detail != exc.code else exc.code
        violations = exc.violations
    else:
        header = str(exc).strip() or "schema_error"
        violations = []

    lines = [header]
    if violations:
        lines.append("Violations:")
        for violation in violations:
            field = str(violation.get("field", "payload"))
            detail = str(violation.get("detail", "")).strip()
            lines.append(f"- {field}: {detail or violation.get('code', 'invalid')}")
    lines.append(_report_payload_shape(status))

    example = json.dumps(_report_payload_example(status), separators=(",", ":"))
    lines.append(f"Minimal valid --result for --status {status}:")
    lines.append(example)
    lines.append("Copy-paste command:")
    msg_id_arg = str(msg_id) if msg_id is not None else "N"
    lines.append(f"agent-orch report --msg-id {msg_id_arg} --status {status} --result '{example}'")
    return "\n".join(lines)


class AgentOrchArgumentParser(argparse.ArgumentParser):
    def parse_args(self, args: list[str] | None = None, namespace: argparse.Namespace | None = None) -> argparse.Namespace:
        parsed = super().parse_args(args, namespace)
        if getattr(parsed, "command", None) == "report" and getattr(parsed, "terminate", False) and parsed.status == "progress":
            self.error("report: --terminate is incompatible with --status=progress")
        if getattr(parsed, "command", None) == "await":
            legacy_stream_id = getattr(parsed, "legacy_stream_id", None)
            legacy_msg_id = getattr(parsed, "legacy_msg_id", None)
            flag_stream_id = getattr(parsed, "from_stream_id", None)
            flag_msg_id = getattr(parsed, "flag_msg_id", None)
            legacy_used = legacy_stream_id is not None or legacy_msg_id is not None
            flag_used = flag_stream_id is not None or flag_msg_id is not None
            if legacy_used and flag_used:
                self.error("await: use either legacy positional <stream_id> <msg_id> or --from <stream_id> [--msg-id <N>]")
            if legacy_used:
                if legacy_stream_id is None or legacy_msg_id is None:
                    self.error("await: legacy positional form requires <stream_id> and <msg_id>")
                parsed.stream_id = legacy_stream_id
                parsed.msg_id = legacy_msg_id
            elif flag_used:
                if flag_stream_id is None:
                    # --msg-id without --from is meaningless (nothing to await on).
                    self.error("await: flag form requires --from <stream_id>")
                parsed.stream_id = flag_stream_id
                # --msg-id is optional: omit it for stream mode (wait on the
                # stream's terminal report for any msg_id, or on close).
                parsed.msg_id = flag_msg_id
            else:
                self.error("await: expected <stream_id> <msg_id> or --from <stream_id> [--msg-id <N>]")
            # Mode-aware default timeout: msg_id mode keeps 30s for back-compat;
            # stream mode defaults large because fire-and-forget workers run for
            # minutes. An explicit --timeout always wins.
            if getattr(parsed, "timeout", None) is None:
                parsed.timeout = (
                    AWAIT_MSG_ID_MODE_DEFAULT_TIMEOUT
                    if parsed.msg_id is not None
                    else AWAIT_STREAM_MODE_DEFAULT_TIMEOUT
                )
        return parsed


def _ttl_arg(value: str) -> int:
    try:
        ttl = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("ttl must be an integer") from exc
    if ttl < TELL_TTL_MIN_SECONDS or ttl > TELL_TTL_MAX_SECONDS:
        raise argparse.ArgumentTypeError("ttl must be between 1 and 3600")
    return ttl


def _spec_id_arg(value: str) -> str:
    if "\x00" in value or value.count("__") != 1 or not SPEC_ID_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("spec_id must match <repo>__<topic> using only letters, digits, underscore, and dash")
    return value


def _step_done_arg(value: str) -> list[int]:
    raw_steps = value.split(",")
    if any(not item.strip() for item in raw_steps):
        raise argparse.ArgumentTypeError("step-done must be one or more positive integers separated by commas")
    try:
        steps = [int(item) for item in raw_steps]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("step-done must be one or more positive integers separated by commas") from exc
    if any(step < 1 for step in steps):
        raise argparse.ArgumentTypeError("step-done values must be positive integers")
    return steps


def _positive_int_arg(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


_STREAM_ID_RE = re.compile(r"^[^:\s]+:[^:\s]+$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")


def _classify_id_shape(value: str) -> str:
    if _STREAM_ID_RE.fullmatch(value):
        return "stream id"
    if value.startswith("spec_") or value.count("__") == 1:
        return "spec id"
    if value.startswith(("tell-", "send-")):
        return "tell id"
    if value.startswith(("report-", "late-report-")):
        return "report/request id"
    if value.startswith(("spawn-", "req-", "request-", "await-spawn-")):
        return "request id"
    if _UUID_RE.fullmatch(value):
        return "opaque UUID (commonly a report or tell id)"
    if ":" in value:
        return "malformed stream id"
    return "non-stream id"


def _require_stream_id(command: str, value: str) -> bool:
    shape = _classify_id_shape(value)
    if shape == "stream id":
        return True
    correction = "use the target's canonical <host>:<session> stream id"
    print(
        f"agent-orch {command}: validation failed: expected stream id, got {shape} {value!r}; {correction}",
        file=sys.stderr,
    )
    return False


class _PromptOptionAction(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | list[str] | None,
        option_string: str | None = None,
    ) -> None:
        raw = values[0] if isinstance(values, list) else values
        kind = "json" if option_string == "--option-json" else "legacy"
        ordered = list(getattr(namespace, self.dest, None) or [])
        ordered.append((kind, str(raw or "")))
        setattr(namespace, self.dest, ordered)


def start(args: argparse.Namespace) -> int:
    print("agent-orch no longer requires a local wrapper; verbs connect direct to chat_streamd.")
    return 0


def stop(args: argparse.Namespace) -> int:
    print("agent-orch no longer runs a local wrapper; there is nothing to stop.")
    return 0


def _print_response(response: dict[str, object]) -> None:
    print(json.dumps(response, separators=(",", ":")))
    if response.get("error") == "daemon_died":
        print(str(response.get("recovery")), file=sys.stderr)


def _spawn_ack_schemas() -> dict[str, object]:
    return {
        "inbox_v1": {
            "required_fields": list(schema.REQUIRED_INBOX_FIELDS),
        },
    }


def _compose_role_baseline_prompt(baseline_content: str, prompt: str | None) -> str:
    """Put the resolved role contract ahead of the caller's first prompt."""
    if prompt is None:
        return baseline_content
    separator = "\n" if baseline_content.endswith(("\n", "\r")) else "\n\n"
    return f"{baseline_content}{separator}{prompt}"


def _initial_prompt_payload(
    args: argparse.Namespace,
    config,
    *,
    baseline_content: str | None = None,
) -> tuple[dict[str, object], int]:
    if getattr(args, "initial_prompt", None) is not None:
        text = args.initial_prompt
        if baseline_content is not None:
            text = _compose_role_baseline_prompt(baseline_content, text)
        data = text.encode("utf-8")
        if len(data) <= INITIAL_PROMPT_INLINE_CAP_BYTES:
            return {"initial_prompt": text}, len(data)
        upload = asyncio.run(upload_prompt_blob_once(config, data, timeout=float(getattr(args, "timeout", 30.0))))
        if upload.get("type") != "upload_prompt_blob.ok":
            raise RuntimeError(str(upload.get("error_code") or upload.get("type") or "upload_prompt_blob_failed"))
        return {"initial_prompt_blob_sha": upload.get("prompt_blob_sha")}, len(data)
    if getattr(args, "initial_prompt_file", None) is None:
        if baseline_content is None:
            return {}, 0
        data = baseline_content.encode("utf-8")
        if len(data) <= INITIAL_PROMPT_INLINE_CAP_BYTES:
            return {"initial_prompt": baseline_content}, len(data)
        upload = asyncio.run(upload_prompt_blob_once(config, data, timeout=float(getattr(args, "timeout", 30.0))))
        if upload.get("type") != "upload_prompt_blob.ok":
            raise RuntimeError(str(upload.get("error_code") or upload.get("type") or "upload_prompt_blob_failed"))
        return {"initial_prompt_blob_sha": upload.get("prompt_blob_sha")}, len(data)
    path = Path(args.initial_prompt_file).expanduser()
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise FileNotFoundError(f"initial_prompt_file_unreadable: {path}") from exc
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("prompt_blob_invalid_utf8") from exc
    if baseline_content is not None:
        text = _compose_role_baseline_prompt(baseline_content, text)
        data = text.encode("utf-8")
    if len(data) <= INITIAL_PROMPT_INLINE_CAP_BYTES:
        return {"initial_prompt": text}, len(data)
    upload = asyncio.run(upload_prompt_blob_once(config, data, timeout=float(getattr(args, "timeout", 30.0))))
    if upload.get("type") != "upload_prompt_blob.ok":
        raise RuntimeError(str(upload.get("error_code") or upload.get("type") or "upload_prompt_blob_failed"))
    return {"initial_prompt_blob_sha": upload.get("prompt_blob_sha")}, len(data)


def _capture_initial_prompt(
    args: argparse.Namespace,
    config,
    *,
    baseline_content: str | None = None,
) -> tuple[dict[str, object] | None, int]:
    """Capture a prompt and render the established CLI failure envelope."""
    try:
        if baseline_content is None:
            payload, _size = _initial_prompt_payload(args, config)
        else:
            payload, _size = _initial_prompt_payload(
                args, config, baseline_content=baseline_content,
            )
        return payload, 0
    except FileNotFoundError as exc:
        code = "initial_prompt_file_not_found"
        exit_code = 2
        prefix = "validation failed"
        detail = str(exc)
    except ValueError as exc:
        code = "initial_prompt_invalid"
        exit_code = 2
        prefix = "validation failed"
        detail = str(exc)
    except RuntimeError as exc:
        code = "initial_prompt_upload_failed"
        exit_code = 5
        prefix = "upload failed"
        detail = str(exc)
    _print_response({
        "type": "spawn.error",
        "ok": False,
        "error_code": code,
        "error": code,
        "no_spawn_attempted": True,
    })
    print(f"agent-orch spawn: {prefix}: {detail}", file=sys.stderr)
    return None, exit_code


def _fresh_schedule_validation_error(code: str, detail: str) -> int:
    _print_response({
        "type": "schedule.insert.error",
        "ok": False,
        "error_code": code,
        "error": detail,
        "no_spawn_attempted": True,
    })
    print(f"agent-orch spawn: validation failed: {code}: {detail}", file=sys.stderr)
    return 2


def _parse_iso8601_with_offset(value: str) -> datetime:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("--at must be an ISO 8601 timestamp with an explicit offset") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--at must include an explicit UTC offset")
    return parsed.astimezone(timezone.utc)


def _parse_delay_duration(value: str) -> timedelta:
    raw = value.strip().lower()
    if re.fullmatch(r"\d+", raw):
        return timedelta(seconds=int(raw))
    pattern = re.compile(r"(\d+)([hms])")
    pos = 0
    seconds = 0
    for match in pattern.finditer(raw):
        if match.start() != pos:
            raise ValueError("--delay must be Go-style duration like 3h, 45m, 2h30m, or integer seconds")
        amount = int(match.group(1))
        unit = match.group(2)
        seconds += amount * {"h": 3600, "m": 60, "s": 1}[unit]
        pos = match.end()
    if pos != len(raw) or seconds <= 0:
        raise ValueError("--delay must be Go-style duration like 3h, 45m, 2h30m, or integer seconds")
    return timedelta(seconds=seconds)


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _resolve_schedule_fire_time(args: argparse.Namespace, *, now: datetime | None = None) -> str:
    at = getattr(args, "at", None)
    delay = getattr(args, "delay", None)
    if at and delay:
        raise ValueError("--at and --delay are mutually exclusive")
    if not at and not delay:
        raise ValueError("internal error: no schedule time supplied")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if at:
        fires_at = _parse_iso8601_with_offset(str(at))
    else:
        fires_at = current + _parse_delay_duration(str(delay))
    min_bound = current + timedelta(seconds=SCHEDULE_MIN_DELAY_SECONDS)
    max_bound = current + timedelta(seconds=SCHEDULE_MAX_DELAY_SECONDS)
    if fires_at <= min_bound and not getattr(args, "allow_past_time", False):
        raise ValueError(
            f"--at/--delay must resolve after now + {SCHEDULE_MIN_DELAY_SECONDS}s "
            f"({ _utc_iso(min_bound) }); use --allow-past-time to override"
        )
    if fires_at > max_bound and not getattr(args, "allow_far_future", False):
        raise ValueError(
            f"--at/--delay must resolve on or before now + 365d "
            f"({ _utc_iso(max_bound) }); use --allow-far-future to override"
        )
    return _utc_iso(fires_at)


def _prompt_preview(row: dict[str, object], limit: int = 60) -> str:
    preview = str(row.get("prompt_preview") or "")
    if not preview and row.get("initial_prompt_inline"):
        raw = row.get("initial_prompt_inline")
        if isinstance(raw, str):
            preview = raw
    preview = " ".join(preview.split())
    if len(preview) <= limit:
        return preview
    return preview[: max(0, limit - 3)] + "..."


def _print_schedule_table(rows: list[dict[str, object]]) -> None:
    print("schedule_id                            state              fires_at_utc               target_host  provider  prompt")
    for row in rows:
        sid = str(row.get("schedule_id") or "")
        print(
            f"{sid:<38} "
            f"{str(row.get('state') or ''):<18} "
            f"{str(row.get('fires_at_utc') or ''):<26} "
            f"{str(row.get('target_host') or ''):<12} "
            f"{str(row.get('provider') or ''):<9} "
            f"{_prompt_preview(row)}"
        )


_RECEIPT_PHASES_BY_VERB: dict[str, tuple[str, tuple[str, ...]]] = {
    "schedule.insert": ("schedule", ("row_committed",)),
    "schedule.cancel": ("schedule", ("terminal",)),
    "schedule.reschedule": ("schedule", ("row_committed",)),
    "schedule.run": ("schedule", ("spawn_delivered", "terminal")),
}


def _receipt_recovery_for_payload(payload: dict[str, object]) -> dict[str, object] | None:
    contract = _RECEIPT_PHASES_BY_VERB.get(str(payload.get("type") or ""))
    request_id = payload.get("request_id")
    if contract is None or not isinstance(request_id, str) or not request_id:
        return None
    surface, phases = contract
    return {
        "surface": surface,
        "operation_request_id": request_id,
        "measured_receipt_phases": list(phases),
        "receipt_commands": [
            f"agent-orch {surface} receipt {request_id} --phase {phase}"
            for phase in phases
        ],
    }


def _receipt_recovery_guidance(recovery: dict[str, object] | None) -> str | None:
    if recovery is None:
        return None
    phases = ",".join(str(value) for value in recovery["measured_receipt_phases"])
    commands = "; ".join(str(value) for value in recovery["receipt_commands"])
    return (
        f"recovery_surface={recovery['surface']} "
        f"operation_request_id={recovery['operation_request_id']} "
        f"measured_receipt_phases={phases}; recover with {commands}"
    )


def _direct_rpc_transport_error(
    prefix: str, request_id: str | None, exc: BaseException, *,
    recovery: dict[str, object] | None = None,
) -> tuple[dict[str, object], int, str]:
    if isinstance(exc, TimeoutError):
        response: dict[str, object] = {
            "type": f"{prefix}.error",
            "request_id": request_id,
            "error_code": "timeout",
            "error": "timeout",
            "raw_error": str(exc),
        }
        if recovery is not None:
            response["recovery"] = recovery
        guidance = _receipt_recovery_guidance(recovery)
        return (
            response,
            67,
            f"timeout waiting for {prefix} response" + (f"; {guidance}" if guidance else ""),
        )
    if isinstance(exc, PermissionError):
        return (
            {
                "type": f"{prefix}.error",
                "request_id": request_id,
                "error_code": "auth_failed",
                "error": "auth_failed",
                "raw_error": str(exc),
            },
            66,
            f"auth failed: {exc}",
        )
    if isinstance(exc, OSError):
        return (
            {
                "type": f"{prefix}.error",
                "request_id": request_id,
                "error_code": "daemon_unreachable",
                "error": "daemon_unreachable",
                "raw_error": str(exc),
            },
            64,
            f"chat_streamd unreachable: {exc}",
        )
    response = {
        "type": f"{prefix}.error" if prefix == "spawn" else f"{prefix}.indeterminate",
        "request_id": request_id,
        "error_code": "connection_dropped",
        "error": "connection_dropped",
        "message": str(exc),
        "raw_error": str(exc),
    }
    if recovery is not None:
        response["recovery"] = recovery
    guidance = _receipt_recovery_guidance(recovery)
    return (
        response,
        65,
        f"connection dropped after request may have been sent: {exc}"
        + (f"; {guidance}" if guidance else ""),
    )


def _schedule_ok(response: dict[str, object]) -> bool:
    return response.get("ok") is True or str(response.get("type") or "").startswith("schedule.") and str(response.get("type") or "").endswith(".ok")


def _run_schedule_once(payload: dict[str, object], timeout: float) -> tuple[dict[str, object], int | None, str | None]:
    try:
        return asyncio.run(schedule_once(load_config(), payload, timeout=timeout)), None, None
    except Exception as exc:
        response, exit_code, message = _direct_rpc_transport_error(
            "schedule",
            payload.get("request_id") if isinstance(payload.get("request_id"), str) else None,
            exc,
            recovery=_receipt_recovery_for_payload(payload),
        )
        return response, exit_code, message


def _schedule_actor_payload(
    args: argparse.Namespace, payload: dict[str, object], *, mutation: bool = False,
) -> dict[str, object]:
    actor = getattr(args, "from_stream_id", None)
    if not actor and hasattr(args, "from_stream_id"):
        try:
            actor = discover_leader_stream_id_short(load_config())
        except Exception:
            actor = None
    if actor:
        payload["from_stream_id"] = actor
    if mutation:
        payload["request_id"] = getattr(args, "request_id", None) or str(uuid.uuid4())
    return payload


def schedule_list(args: argparse.Namespace) -> int:
    payload = _schedule_actor_payload(args, {"type": "schedule.list"})
    if getattr(args, "state", None):
        payload["state"] = args.state
    response, transport_exit, transport_message = _run_schedule_once(payload, timeout=float(getattr(args, "timeout", 30.0) or 30.0))
    if getattr(args, "json", False):
        _print_response(response)
    else:
        if _schedule_ok(response):
            rows = response.get("schedules") if isinstance(response.get("schedules"), list) else []
            _print_schedule_table([row for row in rows if isinstance(row, dict)])
        else:
            _print_response(response)
    if transport_exit is not None:
        print(f"agent-orch schedule list: {transport_message}", file=sys.stderr)
        return transport_exit
    return 0 if _schedule_ok(response) else 1


def schedule_get(args: argparse.Namespace) -> int:
    payload = _schedule_actor_payload(args, {"type": "schedule.get", "schedule_id": args.schedule_id})
    response, transport_exit, transport_message = _run_schedule_once(payload, timeout=float(getattr(args, "timeout", 30.0) or 30.0))
    if getattr(args, "json", False) or not _schedule_ok(response):
        _print_response(response)
    else:
        row = response.get("schedule") if isinstance(response.get("schedule"), dict) else {}
        _print_schedule_table([row])
        prompt_b64 = row.get("initial_prompt_b64")
        if isinstance(prompt_b64, str) and prompt_b64:
            try:
                prompt = base64.b64decode(prompt_b64).decode("utf-8")
            except Exception:
                prompt = "<prompt decode failed>"
            print()
            print(prompt)
    if transport_exit is not None:
        print(f"agent-orch schedule get: {transport_message}", file=sys.stderr)
        return transport_exit
    return 0 if _schedule_ok(response) else 1


def schedule_cancel(args: argparse.Namespace) -> int:
    payload = _schedule_actor_payload(
        args, {"type": "schedule.cancel", "schedule_id": args.schedule_id}, mutation=True,
    )
    response, transport_exit, transport_message = _run_schedule_once(payload, timeout=float(getattr(args, "timeout", 30.0) or 30.0))
    _print_response(response)
    if transport_exit is not None:
        print(f"agent-orch schedule cancel: {transport_message}", file=sys.stderr)
        return transport_exit
    return 0 if _schedule_ok(response) else 1


def schedule_reschedule(args: argparse.Namespace) -> int:
    try:
        fires_at_utc = _resolve_schedule_fire_time(args)
    except ValueError as exc:
        print(f"agent-orch schedule reschedule: validation failed: {exc}", file=sys.stderr)
        return 2
    payload = _schedule_actor_payload(args, {
        "type": "schedule.reschedule",
        "schedule_id": args.schedule_id,
        "fires_at_utc": fires_at_utc,
    }, mutation=True)
    response, transport_exit, transport_message = _run_schedule_once(payload, timeout=float(getattr(args, "timeout", 30.0) or 30.0))
    _print_response(response)
    if transport_exit is not None:
        print(f"agent-orch schedule reschedule: {transport_message}", file=sys.stderr)
        return transport_exit
    return 0 if _schedule_ok(response) else 1


def schedule_run(args: argparse.Namespace) -> int:
    payload = _schedule_actor_payload(
        args, {"type": "schedule.run", "schedule_id": args.schedule_id}, mutation=True,
    )
    response, transport_exit, transport_message = _run_schedule_once(payload, timeout=float(getattr(args, "timeout", 30.0) or 30.0))
    _print_response(response)
    if transport_exit is not None:
        print(f"agent-orch schedule run: {transport_message}", file=sys.stderr)
        return transport_exit
    return 0 if _schedule_ok(response) else 1


def schedule_receipt(args: argparse.Namespace) -> int:
    payload = _schedule_actor_payload(args, {
        "type": "schedule.receipt",
        "operation_request_id": args.operation_request_id,
        "phase": args.phase,
    })
    response, transport_exit, transport_message = _run_schedule_once(
        payload, timeout=float(getattr(args, "timeout", 30.0) or 30.0),
    )
    _print_response(response)
    if transport_exit is not None:
        print(f"agent-orch schedule receipt: {transport_message}", file=sys.stderr)
        return transport_exit
    return 0 if _schedule_ok(response) else 1


def _handoff_source_row(config, stream_id: str, *, timeout: float) -> dict[str, object]:
    snapshot = fetch_snapshot(config, timeout=timeout)
    for row in snapshot.get("sessions") or []:
        if isinstance(row, dict) and row.get("stream_id") == stream_id:
            return row
    raise SpawnProfileError(
        "handoff_source_not_found",
        f"retiring stream row not found: {stream_id}",
    )


def _decorate_role_baseline_ack(
    response: dict[str, object],
    *,
    role: str | None,
    role_source: str | None,
    loaded_baseline: dict[str, str] | None,
) -> None:
    if role is None:
        return
    response["role_baseline"] = loaded_baseline
    if role_source is not None:
        response["role_source"] = role_source
    if loaded_baseline is None:
        print(
            f"agent-orch spawn: role_baseline missing for role '{role}'; "
            "continuing without baseline",
            file=sys.stderr,
        )


def _resolve_self_close_on_completion(
    args: argparse.Namespace, *, visibility: str | None, parent: Any, handoff: bool
) -> bool | None:
    """Resolve the durable `self_close_on_completion` bit for a spawn payload.

    Hidden seats (`--visibility hidden`) with a real parent self-close by
    default so a worker that finishes without an explicit `close`/`report
    --terminate` is still reaped by the daemon; the caller opts out with
    `--no-self-close-on-completion`. Visible/nested seats stay default-off. An
    explicit flag is honoured only with parent-or-handoff lineage (the flag is
    meaningless without it — the daemon rejects a leaderless flag). Returns
    None only when no explicit bit is needed on the wire (default-off)."""
    scoc = getattr(args, "self_close_on_completion", None)
    # A blank/whitespace `--parent` is NOT lineage: the daemon strips it, so the
    # flag would land on a leaderless payload and wrongly win the top-level
    # refusal. Treat it like an absent parent everywhere below.
    has_parent = bool(parent.strip()) if isinstance(parent, str) else parent is not None
    if scoc is True:
        return True if (has_parent or handoff) else None
    if scoc is False:
        # Explicit opt-out must travel as a value. Omitting it lets a later
        # hidden-seat default re-assert self-close during spawn admission.
        return False if (has_parent or handoff) else None
    # Implicit default: a hidden seat with a real parent self-closes so a worker
    # that finishes without an explicit close is still reaped by the daemon.
    return True if (visibility == "hidden" and has_parent) else None


def _derive_default_idempotency_key(
    payload: dict[str, object],
    initial_prompt_payload: dict[str, object] | None,
) -> str:
    """Deterministic key for a spawn with no explicit --idempotency-key or
    --request-id: sha256 of the logical spawn inputs (parent, host, provider,
    model, effort, role, spec_ids, brief), truncated to 32 hex chars.

    Stable across a cross-process retry of the SAME logical spawn so the daemon
    dedups it. `initial_prompt_payload` carries the brief as inline text or a
    content-addressed blob sha -- both deterministic for a given brief -- so the
    brief participates in the key without re-reading the file.
    """
    spec_ids = payload.get("spec_ids") or (
        [payload["spec_id"]] if payload.get("spec_id") else []
    )
    canonical = {
        "parent_stream_id": payload.get("parent_stream_id", ""),
        "host": payload.get("host", ""),
        "provider": payload.get("provider", ""),
        "model": payload.get("model", ""),
        "effort": payload.get("effort", ""),
        "role": payload.get("role", ""),
        "spec_ids": sorted(str(s) for s in spec_ids),
        "brief": initial_prompt_payload or {},
        "objective": payload.get("objective"),
    }
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    return digest[:32]


def spawn(args: argparse.Namespace) -> int:
    """Spawn directly through chat_streamd."""
    from _shared.spawn_objective import resolve_objective
    # An objective is required only for a parented child spawn (it feeds the
    # parent's roster); it is validated below once lineage is known.
    objective = getattr(args, "objective", None)
    config = load_config()
    host = args.host or config.host_id
    handoff = bool(getattr(args, "handoff", False))
    top_level = bool(getattr(args, "top_level", False))
    scheduled = bool(getattr(args, "at", None) or getattr(args, "delay", None))
    if getattr(args, "at", None) and getattr(args, "delay", None):
        print("agent-orch spawn: validation failed: --at and --delay are mutually exclusive", file=sys.stderr)
        return 2
    if scheduled and not handoff:
        if getattr(args, "resume", None) is not None:
            return _fresh_schedule_validation_error(
                "resume_not_schedulable", "--resume cannot be scheduled",
            )
        if getattr(args, "confirm_model_change", False):
            return _fresh_schedule_validation_error(
                "handoff_only_flag", "--confirm-model-change applies only to --handoff",
            )
        if getattr(args, "idempotency_key", None) is not None:
            return _fresh_schedule_validation_error(
                "idempotency_key_not_schedulable",
                "--idempotency-key cannot override the schedule generation key",
            )
        if top_level and getattr(args, "self_close_on_completion", None) is not None:
            return _fresh_schedule_validation_error(
                "self_close_requires_parent",
                "--self-close-on-completion requires parent lineage",
            )
    if handoff and args.parent is not None:
        print("agent-orch spawn: validation failed: --handoff is incompatible with --parent", file=sys.stderr)
        return 2
    caller_stream_id = discover_leader_stream_id_short(config) if (
        scheduled or handoff or (args.parent is None and not top_level)
    ) else None
    handoff_from_stream_id = caller_stream_id if handoff else None
    parent = args.parent if args.parent is not None else (
        None if (handoff or top_level) else caller_stream_id
    )
    # Objectives are required only for parented child spawns; the daemon derives
    # one for a top-level/handoff seat. A present objective is still shape-checked.
    _, _, objective_error_code = resolve_objective(
        objective, objective_supported=True, parent_stream_id=parent,
    )
    if objective_error_code:
        print(f"agent-orch spawn: {objective_error_code}", file=sys.stderr)
        return 2
    resume_session_id = getattr(args, "resume", None)
    if top_level and (handoff or args.parent is not None):
        print("agent-orch spawn: validation failed: --top-level is incompatible with --handoff/--parent", file=sys.stderr)
        return 2
    if resume_session_id is not None:
        # Resume reopens the original session's identity from the daemon's
        # fetched row; it cannot also re-lineage the stream, so --parent /
        # --handoff are rejected client-side, and only claude supports
        # `--resume`.
        if args.provider != "claude":
            print("agent-orch spawn: validation failed: --resume requires --provider claude", file=sys.stderr)
            return 2
        if handoff or args.parent is not None:
            print("agent-orch spawn: validation failed: --resume is incompatible with --handoff/--parent", file=sys.stderr)
            return 2
    provider = getattr(args, "provider", None)
    model = getattr(args, "model", None)
    effort = getattr(args, "effort", None)
    requested_spawn_tuple = {
        "requested_provider": provider,
        "requested_model": model,
        "requested_effort": effort,
    }
    if not handoff and not provider:
        print("agent-orch spawn: validation failed: --provider is required unless --handoff is used", file=sys.stderr)
        return 2
    if model is not None and provider is not None and provider not in {"claude", "codex"}:
        print("agent-orch spawn: validation failed: --model requires --provider claude or codex", file=sys.stderr)
        return 2
    try:
        if handoff:
            if not handoff_from_stream_id:
                raise SpawnProfileError("handoff_source_unknown", "retiring stream id is unavailable")
            source_row = _handoff_source_row(
                config,
                handoff_from_stream_id,
                timeout=float(getattr(args, "timeout", 30.0) or 30.0),
            )
            handoff_resolution = resolve_handoff(
                source_provider=source_row.get("provider"),
                source_model=source_row.get("effective_model"),
                source_effort=source_row.get("effective_effort"),
                source_role=source_row.get("role"),
                provider=provider,
                model=model,
                effort=effort,
                role=args.role,
                host=host,
            )
            resolved_spawn = handoff_resolution.spawn
            if handoff_resolution.changed:
                requested_tuple = {
                    field: resolved_spawn[field] for field in HANDOFF_TUPLE_FIELDS
                }
                if not getattr(args, "confirm_model_change", False):
                    changed_fields = ",".join(handoff_resolution.changed_fields)
                    print(
                        "agent-orch spawn: WARNING: changed tuple fields: "
                        f"{changed_fields}; prior tuple={handoff_resolution.source}; "
                        f"requested tuple={requested_tuple}; "
                        "proceeding (pass --confirm-model-change to suppress this warning)",
                        file=sys.stderr,
                    )
        else:
            resolved_spawn = resolve_spawn(provider=provider, model=model, effort=effort, host=host)
    except SpawnProfileError as exc:
        print(f"agent-orch spawn: validation failed: {exc.code}: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"agent-orch spawn: handoff lookup failed: {exc}", file=sys.stderr)
        return 5
    requested_role = getattr(args, "role", None)
    resolved_role = resolved_spawn.get("role") if handoff else requested_role
    role_source = None
    loaded_baseline = None
    if resolved_role is not None:
        role_source = "handoff" if handoff and not requested_role else "spawn"
        loaded_baseline = role_baseline.load_role_baseline(config, str(resolved_role))
    baseline_content = loaded_baseline.get("content") if loaded_baseline else None
    visibility = args.visibility
    if visibility is None:
        visibility = "default" if (handoff or args.role == "nexus" or parent is None) else None
    raw_spec_ids = getattr(args, "spec_id", None)
    spec_ids = [raw_spec_ids] if isinstance(raw_spec_ids, str) else list(raw_spec_ids or [])
    if scheduled:
        try:
            fires_at_utc = _resolve_schedule_fire_time(args)
        except ValueError as exc:
            print(f"agent-orch spawn: validation failed: {exc}", file=sys.stderr)
            return 2
        payload: dict[str, object] = {
            "type": "schedule.insert",
            "objective": objective,
            "objective_supported": True,
            "no_watch": bool(getattr(args, "no_watch", False)),
            "request_id": getattr(args, "request_id", None) or str(uuid.uuid4()),
            "fires_at_utc": fires_at_utc,
            "provider": resolved_spawn["provider"],
            "target_host": host,
            "role": resolved_role,
            "visibility": visibility,
            "created_by_stream_id": caller_stream_id,
            "from_stream_id": caller_stream_id,
            "handoff": handoff,
            **resolved_spawn,
        }
        # Presence is part of the audit truth: JSON null means the caller omitted
        # that override, while the resolved tuple below records what will launch.
        payload.update(requested_spawn_tuple)
        if handoff_from_stream_id is not None:
            payload["handoff_from_stream_id"] = handoff_from_stream_id
        if not handoff:
            payload["phase"] = args.phase
            if parent is not None:
                payload["parent_stream_id"] = parent
            if getattr(args, "reparent_children", None) is False:
                payload["reparent_children"] = False
        resolved_self_close = _resolve_self_close_on_completion(
            args, visibility=visibility, parent=parent, handoff=handoff
        )
        if resolved_self_close is not None:
            payload["self_close_on_completion"] = resolved_self_close
        if spec_ids:
            payload["spec_id"] = spec_ids[0]
            if len(spec_ids) > 1:
                payload["spec_ids"] = spec_ids
        if getattr(args, "disposition_waived_reason", None):
            payload["disposition_waived_reason"] = args.disposition_waived_reason
        if handoff and getattr(args, "confirm_model_change", False):
            payload["confirm_model_change"] = True
        initial_prompt_payload, prompt_exit = _capture_initial_prompt(
            args, config, baseline_content=baseline_content,
        )
        if initial_prompt_payload is None:
            return prompt_exit
        payload.update(initial_prompt_payload)
        try:
            response = asyncio.run(schedule_once(config, payload, timeout=float(getattr(args, "timeout", 30.0) or 30.0)))
        except Exception as exc:
            response, exit_code, message = _direct_rpc_transport_error(
                "schedule",
                payload.get("request_id") if isinstance(payload.get("request_id"), str) else None,
                exc,
                recovery=_receipt_recovery_for_payload(payload),
            )
            _print_response(response)
            print(f"agent-orch spawn: schedule insert failed: {message}", file=sys.stderr)
            return exit_code
        if _schedule_ok(response):
            _decorate_role_baseline_ack(
                response,
                role=resolved_role,
                role_source=role_source,
                loaded_baseline=loaded_baseline,
            )
        _print_response(response)
        return 0 if _schedule_ok(response) else 1
    initial_prompt_payload, prompt_exit = _capture_initial_prompt(
        args, config, baseline_content=baseline_content,
    )
    if initial_prompt_payload is None:
        return prompt_exit
    payload: dict[str, object] = {
        "type": "spawn",
        "objective": objective,
        "objective_supported": True,
        "no_watch": bool(getattr(args, "no_watch", False)),
        "provider": resolved_spawn["provider"],
        "host": host,
        "role": resolved_role,
        "phase": args.phase,
        "visibility": visibility,
        **resolved_spawn,
    }
    if spec_ids:
        payload["spec_id"] = spec_ids[0]
        if len(spec_ids) > 1:
            payload["spec_ids"] = spec_ids
    if resume_session_id is not None:
        payload["resume_session_id"] = resume_session_id
    if handoff:
        payload["handoff"] = True
        if handoff_from_stream_id is not None:
            payload["handoff_from_stream_id"] = handoff_from_stream_id
        if getattr(args, "disposition_waived_reason", None):
            payload["disposition_waived_reason"] = args.disposition_waived_reason
        if getattr(args, "confirm_model_change", False):
            payload["confirm_model_change"] = True
        # Default-on; only put the opt-out on the wire so existing handoff
        # spawns keep auto-re-parenting without a contract bump.
        if getattr(args, "reparent_children", None) is False:
            payload["reparent_children"] = False
    payload.update(initial_prompt_payload)
    if parent is not None:
        payload["parent_stream_id"] = parent
    # Hidden seats self-close by default (opt out with
    # --no-self-close-on-completion); visible/nested stay default-off. The flag
    # only rides a spawn with real lineage (parent or handoff): a
    # leaderless/top-level spawn must never carry it — Case A.5 in
    # _classify_self_terminate is checked before the top_level_refused path, so
    # the flag would otherwise let a top-level operator session wrongly
    # self-close.
    resolved_self_close = _resolve_self_close_on_completion(
        args, visibility=visibility, parent=parent, handoff=handoff
    )
    if resolved_self_close is not None:
        payload["self_close_on_completion"] = resolved_self_close
    explicit_request_id = getattr(args, "request_id", None)
    payload["request_id"] = explicit_request_id or f"spawn-{uuid.uuid4()}"
    # Idempotency key precedence (the retry-dedup contract):
    #   1. explicit --idempotency-key wins;
    #   2. else an explicit --request-id is the caller's stable handle -> key;
    #   3. else a DETERMINISTIC sha256[:32] of the logical spawn inputs (NOT the
    #      volatile per-attempt request_id), so a cross-process retry of the same
    #      logical spawn carries the same key and the daemon returns the existing
    #      seat instead of minting a duplicate.
    explicit_key = getattr(args, "idempotency_key", None)
    if explicit_key:
        payload["idempotency_key"] = explicit_key
    elif explicit_request_id:
        payload["idempotency_key"] = explicit_request_id
    else:
        payload["idempotency_key"] = _derive_default_idempotency_key(
            payload, initial_prompt_payload
        )
    # Print the key BEFORE the RPC: a caller interrupted before the reply still
    # sees it on stderr and can retry (same key), `spawn status <key>`, or
    # `spawn cancel <key>`.
    print(f"spawn key: {payload['idempotency_key']}", file=sys.stderr)
    try:
        response = asyncio.run(spawn_once(
            config,
            payload,
            timeout=float(getattr(args, "timeout", None) or SPAWN_RPC_TIMEOUT_DEFAULT_S),
        ))
    except Exception as exc:
        response, exit_code, message = _direct_rpc_transport_error(
            "spawn",
            payload.get("request_id") if isinstance(payload.get("request_id"), str) else None,
            exc,
        )
        _print_response(response)
        print(f"agent-orch spawn: {message}", file=sys.stderr)
        return exit_code
    spawn_success = response.get("type") == "spawn.ok" and response.get("ok") is not False
    if spawn_success or response.get("type") == "spawn.indeterminate":
        response["schemas"] = _spawn_ack_schemas()
        _decorate_role_baseline_ack(
            response,
            role=resolved_role,
            role_source=role_source,
            loaded_baseline=loaded_baseline,
        )
    _print_response(response)
    if response.get("type") == "spawn.indeterminate":
        stream_id = str(response.get("stream_id") or "")
        print(
            "agent-orch spawn: admitted pane outcome is indeterminate for "
            f"{stream_id}; inspect with: agent-orch inspect {stream_id}",
            file=sys.stderr,
        )
        return 3
    return 0 if spawn_success else 1


def spawn_cancel(args: argparse.Namespace) -> int:
    payload: dict[str, object] = {"type": "spawn_cancel", "target": args.target}
    if getattr(args, "host", None):
        payload["host"] = args.host
    try:
        response = asyncio.run(spawn_cancel_once(
            load_config(), payload, timeout=float(getattr(args, "timeout", 30.0) or 30.0),
        ))
    except Exception as exc:
        response, exit_code, message = _direct_rpc_transport_error("spawn_cancel", None, exc)
        _print_response(response)
        print(f"agent-orch spawn cancel: {message}", file=sys.stderr)
        return exit_code
    _print_response(response)
    return 0 if response.get("type") == "spawn_cancel.ok" else 1


def spawn_status(args: argparse.Namespace) -> int:
    payload: dict[str, object] = {"type": "spawn_status", "target": args.target}
    if getattr(args, "host", None):
        payload["host"] = args.host
    try:
        response = asyncio.run(spawn_status_once(
            load_config(), payload, timeout=float(getattr(args, "timeout", 30.0) or 30.0),
        ))
    except Exception as exc:
        response, exit_code, message = _direct_rpc_transport_error("spawn_status", None, exc)
        _print_response(response)
        print(f"agent-orch spawn status: {message}", file=sys.stderr)
        return exit_code
    _print_response(response)
    return 0 if response.get("type") == "spawn_status.ok" else 1


def spawn_freeze(args: argparse.Namespace) -> int:
    payload: dict[str, object] = {"type": "spawn_freeze", "reason": getattr(args, "reason", "") or ""}
    if getattr(args, "host", None):
        payload["host"] = args.host
    if getattr(args, "ttl", None) is not None:
        payload["ttl_s"] = float(args.ttl)
    try:
        response = asyncio.run(spawn_freeze_once(
            load_config(), payload, timeout=float(getattr(args, "timeout", 30.0) or 30.0),
        ))
    except Exception as exc:
        response, exit_code, message = _direct_rpc_transport_error("spawn_freeze", None, exc)
        _print_response(response)
        print(f"agent-orch spawn freeze: {message}", file=sys.stderr)
        return exit_code
    _print_response(response)
    return 0 if response.get("type") == "spawn_freeze.ok" else 1


def spawn_unfreeze(args: argparse.Namespace) -> int:
    payload: dict[str, object] = {"type": "spawn_unfreeze"}
    if getattr(args, "host", None):
        payload["host"] = args.host
    try:
        response = asyncio.run(spawn_freeze_once(
            load_config(), payload, timeout=float(getattr(args, "timeout", 30.0) or 30.0),
        ))
    except Exception as exc:
        response, exit_code, message = _direct_rpc_transport_error("spawn_unfreeze", None, exc)
        _print_response(response)
        print(f"agent-orch spawn unfreeze: {message}", file=sys.stderr)
        return exit_code
    _print_response(response)
    return 0 if response.get("type") == "spawn_freeze.ok" else 1


def send(args: argparse.Namespace) -> int:
    if not _require_stream_id("send", args.stream_id):
        return 2
    config = load_config()
    from_stream_id = discover_leader_stream_id_short(config)
    if not from_stream_id:
        print("agent-orch send: validation failed: stream_id_unknown", file=sys.stderr)
        return 2
    parsed_stream = args.stream_id.split(":", 1)
    if len(parsed_stream) != 2 or not parsed_stream[0] or not parsed_stream[1]:
        print("agent-orch send: validation failed: invalid_stream_id", file=sys.stderr)
        return 2
    host, session_name = parsed_stream
    try:
        inbox_payload = schema.build_inbox_payload(
            msg_id=args.msg_id,
            from_stream_id=from_stream_id,
            to_stream_id=args.stream_id,
            task=args.prompt_text,
        )
        schema.inline_inbox_json(inbox_payload)
    except schema.InboxValidationError as exc:
        print(f"agent-orch send: validation failed: {exc.code}: {exc}", file=sys.stderr)
        return 2
    request: dict[str, object] = {
        "type": "send",
        "host": host,
        "session_name": session_name,
        "text": args.prompt_text,
        "from_stream_id": from_stream_id,
        "msg_id": args.msg_id,
        "retry": bool(args.retry),
        "inbox": inbox_payload,
    }
    timeout = float(getattr(args, "timeout", 30.0) or 30.0)
    try:
        response = asyncio.run(send_once(config, request, timeout=timeout))
    except TimeoutError:
        _print_response({"type": "send.error", "ok": False, "error_code": "timeout", "error": "timeout"})
        print("agent-orch send: timeout waiting for send response", file=sys.stderr)
        return 67
    except PermissionError as exc:
        _print_response({"type": "send.error", "ok": False, "error_code": "auth_failed", "error": "auth_failed", "message": str(exc)})
        print(f"agent-orch send: auth failed: {exc}", file=sys.stderr)
        return 66
    except OSError as exc:
        _print_response({"type": "send.error", "ok": False, "error_code": "chat_streamd_unreachable", "error": "chat_streamd_unreachable", "message": str(exc)})
        print(f"agent-orch send: chat_streamd unreachable: {exc}", file=sys.stderr)
        return 64
    except Exception as exc:
        _print_response({"type": "send.error", "ok": False, "error_code": "connection_dropped", "error": "connection_dropped", "message": str(exc)})
        print(f"agent-orch send: connection dropped after request may have been sent: {exc}", file=sys.stderr)
        return 65
    if response.get("type") == "send.result":
        response.setdefault("ok", response.get("delivery") == "landed")
    elif str(response.get("type", "")).startswith("send."):
        response.setdefault("ok", False)
    if not getattr(args, "quiet", False):
        for frame in response.get("progress") or []:
            if isinstance(frame, dict):
                print(
                    "send progress: "
                    f"msg_id={frame.get('msg_id')} state={frame.get('state')} "
                    f"attempt={frame.get('attempt')} reason={frame.get('reason')} "
                    f"next_in_ms={frame.get('next_in_ms')}",
                    file=sys.stderr,
                )
    exit_code = 1
    if response.get("type") == "send.result" and response.get("delivery") == "landed":
        exit_code = 0
    elif (
        response.get("type") == "send.result"
        and response.get("delivery") == COMMITTED_PENDING_PROOF
        and response.get("do_not_resubmit") is True
    ):
        # Durably committed (paste left the composer); only the async proof is
        # late. Non-fatal: never re-evaluate or resend — reconcile the receipt.
        response["ok"] = True
        print(
            "agent-orch send: COMMITTED - target received the message; proof is "
            "pending and reconciles asynchronously. Do NOT resend; reconcile with "
            f"{response.get('reconcile_command') or ('agent-orch send-receipt ' + str(response.get('to_stream_id') or ''))}.",
            file=sys.stderr,
        )
        exit_code = 0
    elif response.get("type") == "send.result" and response.get("delivery") == "not_landed":
        reason = str(response.get("reason") or "not_landed")
        if response.get("queued_for_redelivery") is True:
            print(
                "agent-orch send: DEFERRED - target pane not ready or delivery was not confirmed; "
                f"message QUEUED for redelivery. Do NOT resend; await the report "
                f"(agent-orch await --msg-id {args.msg_id}).",
                file=sys.stderr,
            )
            exit_code = 75
        else:
            print(
                f"agent-orch send: NOT DELIVERED ({reason}) and NOT queued - re-evaluate.",
                file=sys.stderr,
            )
            exit_code = 76
    _print_response(response)
    return exit_code


def send_receipt(args: argparse.Namespace) -> int:
    """Print the daemon's single latest durable receipt projection."""
    if not _require_stream_id("send-receipt", args.stream_id):
        return 2
    try:
        response = asyncio.run(
            send_receipt_once(
                load_config(), args.stream_id, args.request_id,
                timeout=float(getattr(args, "timeout", 30.0) or 30.0),
            )
        )
    except Exception as exc:
        _print_response({
            "type": "send.receipt.get.error", "ok": False,
            "error_code": "chat_streamd_unreachable", "error": str(exc),
        })
        return 64
    _print_response(response)
    return 0 if response.get("type") == "send.receipt.get.ok" else 1


def send_cancel(args: argparse.Namespace) -> int:
    try:
        response = asyncio.run(send_cancel_once(load_config(), args.msg_id, timeout=float(getattr(args, "timeout", 30.0) or 30.0)))
    except Exception as exc:
        response, exit_code, message = _direct_rpc_transport_error("send.cancel", None, exc)
        _print_response(response)
        print(f"agent-orch send-cancel: {message}", file=sys.stderr)
        return exit_code
    _print_response(response)
    return 0 if response.get("type") == "send.cancel.ok" else 1


def tell(args: argparse.Namespace) -> int:
    if not _require_stream_id("tell", args.peer_stream_id):
        return 2
    ttl_seconds = int(args.ttl)
    if ttl_seconds < TELL_TTL_MIN_SECONDS or ttl_seconds > TELL_TTL_MAX_SECONDS:
        print("agent-orch tell: validation failed: invalid_ttl", file=sys.stderr)
        return 2
    config = load_config()
    from_stream_id = args.from_stream_id or discover_leader_stream_id_short(config)
    if not from_stream_id:
        print("agent-orch tell: validation failed: stream_id_unknown", file=sys.stderr)
        return 2
    tell_id = args.tell_id or str(uuid.uuid4())
    request = {
        "type": "tell",
        "tell_id": tell_id,
        "from_stream_id": from_stream_id,
        "to_stream_id": args.peer_stream_id,
        "text": args.text,
        "ttl_seconds": ttl_seconds,
    }
    if getattr(args, "urgent", False):
        request["urgent"] = True
    if getattr(args, "sanitize", False):
        request["sanitize"] = True
    try:
        response = asyncio.run(tell_once(config, request, timeout=float(args.timeout)))
    except TimeoutError:
        print("agent-orch tell: timeout waiting for tell response", file=sys.stderr)
        return 67
    except PermissionError as exc:
        print(f"agent-orch tell: auth failed: {exc}", file=sys.stderr)
        return 66
    except OSError as exc:
        print(f"agent-orch tell: chat_streamd unreachable: {exc}", file=sys.stderr)
        return 64
    except Exception as exc:
        print(f"agent-orch tell: connection dropped after request may have been sent: {exc}", file=sys.stderr)
        return 65
    print(json.dumps(response, separators=(",", ":")))
    if response.get("type") == "tell.ok":
        return 0
    error_code = str(response.get("error_code") or "tell_error")
    print(f"agent-orch tell: tell rejected: {error_code}", file=sys.stderr)
    if error_code == "tell_id_replay_conflict":
        return 4
    if error_code in {"peer_session_unknown_from", "peer_session_unknown_to", "peer_session_closed"}:
        return 3
    return 2


def _park_rpc(args: argparse.Namespace, *, command: str) -> int:
    config = load_config()
    from_stream_id = args.from_stream_id or discover_leader_stream_id_short(config)
    request: dict[str, object] = {
        "type": command,
        "stream_id": args.stream_id,
        "from_stream_id": from_stream_id,
    }
    if command in {"park", "unpark"}:
        request["reason"] = args.reason
    if command == "unpark" and getattr(args, "event_id", None):
        request["event_id"] = args.event_id
    try:
        response = asyncio.run(park_once(config, request, timeout=float(args.timeout)))
    except Exception as exc:
        response, exit_code, message = _direct_rpc_transport_error(command, request.get("request_id") if isinstance(request.get("request_id"), str) else None, exc)
        _print_response(response)
        print(f"agent-orch {command}: {message}", file=sys.stderr)
        return exit_code
    _print_response(response)
    return 0 if response.get("type") == f"{command}.ok" else 1


def park(args: argparse.Namespace) -> int:
    return _park_rpc(args, command="park")


def unpark(args: argparse.Namespace) -> int:
    return _park_rpc(args, command="unpark")


def _coordination_request(args: argparse.Namespace, payload: dict[str, object], *, needs_actor: bool = True) -> int:
    config = load_config()
    actor = getattr(args, "from_stream_id", None) or discover_leader_stream_id_short(config)
    if needs_actor and not actor:
        print("agent-orch coordination: validation failed: stream_id_unknown", file=sys.stderr)
        return 2
    if actor:
        payload["from_stream_id"] = actor
    try:
        response = asyncio.run(coordination_once(config, payload, timeout=float(getattr(args, "timeout", 30.0) or 30.0)))
    except TimeoutError:
        guidance = _receipt_recovery_guidance(_receipt_recovery_for_payload(payload))
        print(
            "agent-orch coordination: timeout"
            + (f"; {guidance}" if guidance else " waiting for response"),
            file=sys.stderr,
        )
        return 67
    except PermissionError as exc:
        print(f"agent-orch coordination: auth failed: {exc}", file=sys.stderr)
        return 66
    except OSError as exc:
        print(f"agent-orch coordination: chat_streamd unreachable: {exc}", file=sys.stderr)
        return 64
    except Exception as exc:
        guidance = _receipt_recovery_guidance(_receipt_recovery_for_payload(payload))
        print(
            f"agent-orch coordination: connection dropped: {exc}"
            + (f"; {guidance}" if guidance else ""),
            file=sys.stderr,
        )
        return 65
    print(json.dumps(response, separators=(",", ":")))
    return 0 if str(response.get("type") or "").endswith(".ok") else 1


def _ttl_expires_at(ttl_seconds: int | None) -> str | None:
    if ttl_seconds is None:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat().replace("+00:00", "Z")


def hold(args: argparse.Namespace) -> int:
    command = str(getattr(args, "hold_command", ""))
    if command == "acquire":
        payload: dict[str, object] = {
            "type": "coordination.hold.acquire",
            "resource": args.resource,
            "reason": args.reason,
        }
        expires_at = _ttl_expires_at(getattr(args, "ttl", None))
        if expires_at:
            payload["expires_at"] = expires_at
        return _coordination_request(args, payload)
    if command == "release":
        target = str(args.resource)
        return _coordination_request(
            args,
            {
                "type": "coordination.hold.release",
                **({"hold_id": target} if re.fullmatch(r"hold-[0-9a-f]{32}", target) else {"resource": target}),
                "force": bool(getattr(args, "force", False)),
            },
        )
    if command == "list":
        return _coordination_request(
            args,
            {
                "type": "coordination.hold.list",
                **({"resource": args.resource} if getattr(args, "resource", None) else {}),
            },
            needs_actor=False,
        )
    print("agent-orch hold: unknown command", file=sys.stderr)
    return 2


def oblige(args: argparse.Namespace) -> int:
    expires_at = _ttl_expires_at(getattr(args, "expires_in", None))
    return _coordination_request(
        args,
        {
            "type": "coordination.obligation.create",
            "target_stream": args.stream,
            "text": args.text,
            **({"spec_id": args.spec_id} if getattr(args, "spec_id", None) else {}),
            **({"expires_at": expires_at} if expires_at else {}),
        },
    )


def obligation(args: argparse.Namespace) -> int:
    command = str(getattr(args, "obligation_command", ""))
    if command == "list":
        return _coordination_request(
            args,
            {
                "type": "coordination.obligation.list",
                **({"stream": args.stream} if getattr(args, "stream", None) else {}),
                **({"status": args.status} if getattr(args, "status", None) else {}),
            },
            needs_actor=False,
        )
    if command == "waive":
        return _coordination_request(
            args,
            {
                "type": "coordination.obligation.waive",
                "obligation_id": args.obligation_id,
                "reason": args.reason,
            },
        )
    print("agent-orch obligation: unknown command", file=sys.stderr)
    return 2


def spec_issue(args: argparse.Namespace) -> int:
    command = str(getattr(args, "spec_issue_command", ""))
    if command == "list":
        return _coordination_request(
            args,
            {
                "type": "coordination.spec_issue.list",
                **({"stream": args.stream} if getattr(args, "stream", None) else {}),
            },
            needs_actor=False,
        )
    if command == "clear":
        return _coordination_request(
            args,
            {
                "type": "coordination.spec_issue.clear",
                "stream": args.stream,
                "spec_id": args.spec_id,
            },
        )
    print("agent-orch spec-issue: unknown command", file=sys.stderr)
    return 2


def _notify_button_actions(labels: list[str]) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []
    for index, label in enumerate(labels):
        text = str(label).strip()
        if not text:
            raise ValueError("--button labels must be non-empty")
        lowered = text.lower()
        if lowered in {"yes", "y", "true", "approve", "approved", "ok"}:
            choice = True
        elif lowered in {"no", "n", "false", "deny", "denied", "cancel"}:
            choice = False
        else:
            choice = index == 0
        actions.append(
            {
                "kind": "yes_no",
                "action_id": f"a{index}",
                "label": text,
                "choice": choice,
            }
        )
    return actions


def _notify_actions_from_args(args: argparse.Namespace) -> list[dict[str, object]]:
    if getattr(args, "actions", None):
        try:
            decoded = json.loads(args.actions)
        except json.JSONDecodeError as exc:
            raise ValueError(f"--actions must be JSON: {exc}") from exc
        if not isinstance(decoded, list) or not all(isinstance(item, dict) for item in decoded):
            raise ValueError("--actions must be a JSON list of action objects")
        actions = [dict(item) for item in decoded]
    else:
        actions = _notify_button_actions(list(getattr(args, "button", None) or []))
    for index, action in enumerate(actions):
        action.setdefault("action_id", f"a{index}")
    return actions


def _notification_create_payload_from_args(
    args: argparse.Namespace, *, caller_stream_id: str | None
) -> dict[str, object]:
    producer = str(getattr(args, "producer", None) or caller_stream_id or "agent-orch")
    severity = str(getattr(args, "severity", None) or "info")
    ttl = getattr(args, "ttl", None)
    payload: dict[str, object] = {
        "type": "notification.create",
        "producer": producer,
        "severity": severity,
        "actions": [],
    }
    if getattr(args, "message", None) is not None:
        message = str(args.message)
        payload["title"] = str(getattr(args, "title", None) or message)
        if getattr(args, "title", None):
            payload["body"] = message
    else:
        ask = str(getattr(args, "ask", "") or "")
        payload["title"] = str(getattr(args, "title", None) or ask)
        payload["body"] = ask
        payload["actions"] = _notify_actions_from_args(args)
    if ttl is not None:
        payload["ttl_seconds"] = int(ttl)
    dedup_key = getattr(args, "dedup_key", None)
    if dedup_key:
        payload["dedup_key"] = str(dedup_key)
    if getattr(args, "message", None) is not None and getattr(args, "actions", None):
        payload["actions"] = _notify_actions_from_args(args)
    if getattr(args, "await_answer", False):
        if not caller_stream_id:
            raise ValueError("--await-answer requires a discoverable caller stream; pass --from")
        payload["answer_to_stream_id"] = caller_stream_id
    return payload


def notify(args: argparse.Namespace) -> int:
    try:
        config = load_config()
        caller_stream_id = getattr(args, "from_stream_id", None) or discover_leader_stream_id_short(config)
        if getattr(args, "resolve_dedup_key", None):
            resolve_dedup_key = str(args.resolve_dedup_key)
            producer = getattr(args, "producer", None)
            if not producer:
                producer = "agent_question.v1" if resolve_dedup_key.startswith("agent-question:") else "memory-cadence"
            producer = str(producer)
            timeout = float(getattr(args, "timeout", 30.0) or 30.0)
            response = asyncio.run(
                notification_resolve_by_dedup_once(
                    config,
                    {
                        "type": "notification.resolve_by_dedup",
                        "producer": producer,
                        "dedup_key": resolve_dedup_key,
                        "by": caller_stream_id or "system",
                    },
                    timeout=timeout,
                )
            )
            _print_response(response)
            return 0 if response.get("type") == "notification.resolve_by_dedup.ok" else 1
        payload = _notification_create_payload_from_args(args, caller_stream_id=caller_stream_id)
        timeout = float(getattr(args, "timeout", 30.0) or 30.0)
        create_response = asyncio.run(
            notification_create_once(config, payload, timeout=timeout)
        )
        if not (
            create_response.get("type") == "notification.create.ok"
            and isinstance(create_response.get("notification"), dict)
        ):
            _print_response(create_response)
            return 1
        if not getattr(args, "await_answer", False):
            _print_response(create_response)
            return 0
        notification = create_response["notification"]
        notification_id = str(notification.get("notification_id") or "")
        answer_response = asyncio.run(
            notification_await_once(config, notification_id, timeout=timeout)
        )
        if answer_response.get("type") == "notification.await.ok" and isinstance(
            answer_response.get("answer"), dict
        ):
            print(json.dumps(answer_response["answer"], separators=(",", ":")))
            return 0
        _print_response(answer_response)
        return 1
    except ValueError as exc:
        print(f"agent-orch notify: validation failed: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        response, exit_code, message = _direct_rpc_transport_error(
            "notification", None, exc
        )
        _print_response(response)
        print(f"agent-orch notify: {message}", file=sys.stderr)
        return exit_code


def investigation(args: argparse.Namespace) -> int:
    command = str(getattr(args, "investigation_command", ""))
    try:
        config = load_config()
        timeout = float(getattr(args, "timeout", 30.0) or 30.0)
        if command == "decide":
            decision: dict[str, object] = {"decision": args.decision}
            if getattr(args, "rationale", None):
                decision["rationale"] = args.rationale
            if getattr(args, "proposed_fix", None):
                decision["proposed_fix"] = args.proposed_fix
            if getattr(args, "affected_subsystem", None):
                decision["affected_subsystem"] = args.affected_subsystem
            if getattr(args, "question_id", None):
                decision["question_id"] = args.question_id
            response = asyncio.run(
                investigation_once(
                    config,
                    {
                        "type": "investigation.decide",
                        "investigation_id": args.investigation_id,
                        "decision": decision,
                    },
                    timeout=timeout,
                )
            )
            _print_response(response)
            return 0 if response.get("type") == "investigation.decide.ok" else 1
        if command == "list":
            payload: dict[str, object] = {"type": "investigation.list"}
            if getattr(args, "status", None):
                payload["statuses"] = list(args.status)
            response = asyncio.run(investigation_once(config, payload, timeout=timeout))
            _print_response(response)
            return 0 if response.get("type") == "investigation.list.ok" else 1
        if command == "drain":
            response = asyncio.run(
                investigation_once(config, {"type": "investigation.drain"}, timeout=timeout)
            )
            _print_response(response)
            return 0 if response.get("type") == "investigation.drain.ok" else 1
        if command == "split":
            response = asyncio.run(
                investigation_once(
                    config,
                    {
                        "type": "investigation.split",
                        "investigation_id": args.investigation_id,
                        "event_id": args.event_id,
                    },
                    timeout=timeout,
                )
            )
            _print_response(response)
            return 0 if response.get("type") == "investigation.split.ok" else 1
        print("agent-orch investigation: unknown command", file=sys.stderr)
        return 2
    except Exception as exc:
        response, exit_code, message = _direct_rpc_transport_error("investigation", None, exc)
        _print_response(response)
        print(f"agent-orch investigation: {message}", file=sys.stderr)
        return exit_code


def nexus(args: argparse.Namespace) -> int:
    command = str(getattr(args, "nexus_command", ""))
    try:
        config = load_config()
        timeout = float(getattr(args, "timeout", 30.0) or 30.0)
        if command == "list":
            payload: dict[str, object] = {"type": "nexus.list", "all": bool(args.all)}
            if args.scope:
                if ":" not in args.scope:
                    raise ValueError("--scope must be scope_type:scope_key")
                scope_type, scope_key = args.scope.split(":", 1)
                payload["scope"] = {"scope_type": scope_type, "scope_key": scope_key}
            response = asyncio.run(
                nexus_once(config, payload, timeout=timeout)
            )
        elif command == "context":
            payload = {"type": "nexus.context"}
            if args.since is not None:
                payload["since"] = args.since
            response = asyncio.run(nexus_once(config, payload, timeout=timeout))
        elif command == "updates":
            payload = {"type": "nexus.updates"}
            if args.since is not None:
                payload["since"] = args.since
            if args.ack_revision is not None:
                payload["ack_revision"] = args.ack_revision
            response = asyncio.run(nexus_once(config, payload, timeout=timeout))
        elif command == "metrics":
            response = asyncio.run(nexus_once(config, {"type": "nexus.metrics"}, timeout=timeout))
        elif command == "inspect":
            response = asyncio.run(
                nexus_once(
                    config,
                    {"type": "nexus.inspect", "identifier": args.identifier},
                    timeout=timeout,
                )
            )
        elif command in {"register", "unregister", "claim", "release", "resolve", "archive"}:
            payload: dict[str, object] = {
                "type": f"nexus.{command}",
                "domain_id": args.domain_id,
            }
            if command == "register":
                payload["mode"] = args.mode
            if command in {"release", "resolve", "archive"} and getattr(args, "epoch", None) is not None:
                payload["epoch"] = args.epoch
            if command == "claim":
                payload["override"] = bool(args.override)
                if args.confirmation_token:
                    payload["confirmation_token"] = args.confirmation_token
            response = asyncio.run(nexus_once(config, payload, timeout=timeout))
        elif command == "route":
            payload = {
                "type": "nexus.route",
                "domain_id": args.domain_id or "",
                "kind": args.kind,
                "message": args.message,
            }
            if bool(getattr(args, "auto", False)):
                payload["auto"] = True
            if getattr(args, "scope", None):
                if ":" not in args.scope:
                    raise ValueError("--scope must be scope_type:scope_key")
                scope_type, scope_key = args.scope.split(":", 1)
                payload["scope"] = {"scope_type": scope_type, "scope_key": scope_key}
            if args.route_id:
                payload["route_id"] = args.route_id
            response = asyncio.run(nexus_once(config, payload, timeout=timeout))
        elif command == "declare":
            bindings = []
            for raw in args.binding or []:
                if ":" not in raw:
                    raise ValueError("--binding must be scope_type:scope_key")
                scope_type, scope_key = raw.split(":", 1)
                bindings.append({"scope_type": scope_type, "scope_key": scope_key})
            definition = {
                "schema_version": 1,
                "id": args.domain_id,
                "title": args.title,
                "charter": args.charter,
                "parent_id": args.parent,
                "audience": args.audience,
                "bindings": bindings,
                "spawn_profile": {
                    "role": "nexus",
                    "visibility": "visible",
                    "provider": args.provider,
                    **({"host": args.host} if args.host else {}),
                },
                "aliases": list(args.alias or []),
            }
            payload = {"type": "nexus.declare", "definition": definition}
            if args.epoch is not None:
                payload["epoch"] = args.epoch
            if args.confirm_distinct:
                payload["confirm_distinct"] = True
            response = asyncio.run(nexus_once(config, payload, timeout=timeout))
        else:
            print("agent-orch nexus: unknown command", file=sys.stderr)
            return 2
        _print_response(response)
        return 0 if response.get("type") == f"nexus.{command}.ok" else 1
    except Exception as exc:
        response, exit_code, message = _direct_rpc_transport_error("nexus", None, exc)
        _print_response(response)
        print(f"agent-orch nexus: {message}", file=sys.stderr)
        return exit_code


def repo(args: argparse.Namespace) -> int:
    command = str(getattr(args, "repo_command", ""))
    try:
        config = load_config()
        timeout = float(getattr(args, "timeout", 30.0) or 30.0)
        payload: dict[str, object] = {"type": f"repo.{command}"}
        if command in {"register", "refresh"}:
            machine = args.machine or config.host_id
            facts = _repo_facts(args.path) if machine == config.host_id else {}
            payload.update({
                "path": args.path,
                "remote": facts.get("remote"),
                "local_id": args.local_id,
                "branch": facts.get("branch"),
                "head": facts.get("head"),
                "upstream": facts.get("upstream"),
                "machine": machine,
                "source": "local_probe" if machine == config.host_id else "remote_probe",
            })
            if args.spec_id is not None:
                payload["spec_ids"] = list(args.spec_id)
            if args.intent is not None:
                payload["intent"] = args.intent
        elif command == "release":
            payload["registration_id"] = args.registration_id
        elif command == "list":
            payload.update({"mine": bool(args.mine), "machine": args.machine, "active": bool(args.active)})
        elif command == "who":
            payload["repo_id"] = args.repo_id
        elif command == "history":
            payload.update({"repo_id": args.repo_id, "machine": args.machine, "spec_id": args.spec_id, "since": args.since})
        elif command == "context":
            payload.update({"identifier": args.identifier, "machine": args.machine})
        elif command == "metrics":
            pass
        else:
            print("agent-orch repo: unknown command", file=sys.stderr)
            return 2
        response = asyncio.run(repo_once(config, payload, timeout=timeout))
        _print_response(response)
        return 0 if response.get("type") == f"repo.{command}.ok" else 1
    except Exception as exc:
        response, exit_code, message = _direct_rpc_transport_error("repo", None, exc)
        _print_response(response)
        print(f"agent-orch repo: {message}", file=sys.stderr)
        return exit_code


def _repo_facts(path: str) -> dict[str, str | None]:
    """Collect only Git identity facts locally; never read file contents."""
    worktree = Path(path).expanduser().resolve()
    if not worktree.is_dir():
        raise ValueError("repo path is not a directory")

    def git(*args: str) -> str | None:
        result = subprocess.run(["git", "-C", str(worktree), *args], capture_output=True, text=True, check=False)
        value = result.stdout.strip()
        return value or None

    if git("rev-parse", "--is-inside-work-tree") != "true":
        raise ValueError("repo path is not a Git worktree")
    return {
        "remote": git("config", "--get", "remote.origin.url"),
        "branch": git("symbolic-ref", "--quiet", "--short", "HEAD"),
        "head": git("rev-parse", "HEAD"),
        "upstream": git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"),
    }


def _prompt_envelope_from_args(args: argparse.Namespace) -> dict[str, object]:
    producer_stream_id = (
        getattr(args, "from_stream_id", None)
        or os.environ.get("PENTACLE_STREAM_ID")
        or os.environ.get("AGENT_ORCH_STREAM_ID")
    )
    raw_options: list[prompt_protocol.PromptOption] = []
    for kind, raw in list(getattr(args, "prompt_options", None) or []):
        if kind == "json":
            raw_options.append(prompt_protocol.parse_option_json(raw))
        else:
            raw_options.append(prompt_protocol.parse_option(raw))
    envelope = prompt_protocol.build_envelope(
        title=str(getattr(args, "title", "") or ""),
        body=str(getattr(args, "body", "") or ""),
        context=getattr(args, "context", None),
        response_mode=str(getattr(args, "response_mode", "single_choice") or "single_choice"),
        raw_options=raw_options,
        question_id=getattr(args, "question_id", None),
        producer_stream_id=producer_stream_id,
        producer_provider=getattr(args, "provider", None),
        spec_id=getattr(args, "spec_id", None),
        dedup_key=getattr(args, "dedup_key", None),
        ttl_seconds=getattr(args, "ttl", None),
        allow_custom=bool(getattr(args, "allow_custom", False)),
    )
    try:
        context = json.loads(str(envelope.get("context") or ""))
    except ValueError:
        context = None
    if isinstance(context, dict) and context.get("schema") == "HandoffModelChangeApprovalV1":
        envelope["dedup_key"] = None
    return envelope


def prompt_ask(args: argparse.Namespace) -> int:
    try:
        envelope = _prompt_envelope_from_args(args)
        payload = {
            "type": "prompt.ask",
            "envelope": envelope,
            "actions": prompt_protocol.notification_actions(envelope),
            "severity": "info",
        }
        config = load_config()
        timeout = float(getattr(args, "timeout", 30.0) or 30.0)
        response = asyncio.run(prompt_ask_once(config, payload, timeout=timeout))
        if response.get("type") != "prompt.ask.ok":
            error_code = str(response.get("error_code") or response.get("error") or "prompt_publish_failed")
            if error_code in {"notification_store_unavailable", "notification_store_error"}:
                fallback = prompt_protocol.fallback_response(
                    envelope,
                    error_code=error_code,
                    message=str(response.get("message") or "durable prompt publish failed before creation"),
                )
                print(json.dumps(fallback, separators=(",", ":")))
                print(fallback["inline_prompt"], file=sys.stderr)
                return 0
            print(json.dumps(response, separators=(",", ":")))
            return 2 if error_code == "prompt_invalid" else 1
        notice_delivery = (
            response.get("notice_delivery")
            if isinstance(response.get("notice_delivery"), dict)
            else None
        )
        notice_target = str(
            response.get("to_stream_id")
            or (notice_delivery or {}).get("to_stream_id")
            or ""
        )
        if notice_target and not (
            notice_delivery is not None
            and _notice_delivery_is_authoritative(
                notice_delivery, to_stream_id=notice_target,
            )
        ):
            print(json.dumps(response, separators=(",", ":")))
            print(
                "agent-orch prompt ask: live-session notice not delivered: "
                f"{(notice_delivery or {}).get('error_code') or (notice_delivery or {}).get('delivery_status') or 'missing_notice_delivery'} "
                f"to {notice_target} "
                f"correlation {(notice_delivery or {}).get('tell_id') or 'unknown'}; reconcile with prompt status",
                file=sys.stderr,
            )
            return 1
        if not getattr(args, "await_answer", False):
            print(json.dumps(response, separators=(",", ":")))
            return 0
        question = response.get("question") if isinstance(response.get("question"), dict) else {}
        notification_id = str(question.get("notification_id") or "")
        if not notification_id:
            print(json.dumps(response, separators=(",", ":")))
            return 1
        await_response = asyncio.run(
            notification_await_once(config, notification_id, timeout=timeout)
        )
        if await_response.get("type") != "notification.await.ok":
            print(json.dumps(await_response, separators=(",", ":")))
            return 1
        status_response = asyncio.run(
            prompt_status_once(config, str(envelope["question_id"]), timeout=timeout)
        )
        print(json.dumps(status_response, separators=(",", ":")))
        return 0 if status_response.get("type") == "prompt.status.ok" else 1
    except prompt_protocol.PromptValidationError as exc:
        response = {
            "type": "prompt.error",
            "ok": False,
            "error_code": "prompt_invalid",
            "error": "prompt_invalid",
            "message": str(exc),
        }
        print(json.dumps(response, separators=(",", ":")))
        print(f"agent-orch prompt ask: validation failed: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        envelope = _prompt_envelope_from_args(args)
        response, _exit_code, message = _direct_rpc_transport_error("prompt", None, exc)
        fallback = prompt_protocol.fallback_response(
            envelope,
            error_code=str(response.get("error_code") or response.get("error") or "daemon_unreachable"),
            message=message,
        )
        print(json.dumps(fallback, separators=(",", ":")))
        print(fallback["inline_prompt"], file=sys.stderr)
        return 0


def prompt_status(args: argparse.Namespace) -> int:
    try:
        response = asyncio.run(
            prompt_status_once(
                load_config(),
                args.question_id,
                timeout=float(getattr(args, "timeout", 30.0) or 30.0),
            )
        )
        print(json.dumps(response, separators=(",", ":")))
        return 0 if response.get("type") == "prompt.status.ok" else 1
    except Exception as exc:
        response, exit_code, message = _direct_rpc_transport_error("prompt", None, exc)
        response["question_id"] = args.question_id
        print(json.dumps(response, separators=(",", ":")))
        print(f"agent-orch prompt status: {message}", file=sys.stderr)
        return exit_code


def prompt_answer(args: argparse.Namespace) -> int:
    # Canonical answer payload (D3): {question_id, selections?: [str], text?: str}.
    # A choice takes selections and/or text; free text takes text only.
    selections = list(getattr(args, "selection", None) or [])
    text = getattr(args, "text", None)
    if not selections and (text is None or not str(text).strip()):
        print(
            "agent-orch prompt answer: provide --select and/or --text",
            file=sys.stderr,
        )
        return 2
    payload: dict[str, object] = {
        "type": "prompt.answer",
        "question_id": args.question_id,
    }
    claimed_by = getattr(args, "by", None)
    if claimed_by:
        payload["by"] = claimed_by
    if selections:
        payload["selections"] = selections
    if text is not None:
        payload["text"] = text
    try:
        response = asyncio.run(
            prompt_answer_once(
                load_config(),
                payload,
                timeout=float(getattr(args, "timeout", 30.0) or 30.0),
            )
        )
        print(json.dumps(response, separators=(",", ":")))
        return 0 if response.get("type") == "prompt.answer.ok" else 1
    except Exception as exc:
        response, exit_code, message = _direct_rpc_transport_error("prompt", None, exc)
        response["question_id"] = args.question_id
        print(json.dumps(response, separators=(",", ":")))
        print(f"agent-orch prompt answer: {message}", file=sys.stderr)
        return exit_code


def prompt_cancel(args: argparse.Namespace) -> int:
    payload: dict[str, object] = {
        "type": "prompt.cancel",
        "question_id": args.question_id,
    }
    claimed_by = getattr(args, "by", None)
    if claimed_by:
        payload["by"] = claimed_by
    if getattr(args, "note", None) is not None:
        payload["note"] = args.note
    try:
        response = asyncio.run(
            prompt_cancel_once(
                load_config(),
                payload,
                timeout=float(getattr(args, "timeout", 30.0) or 30.0),
            )
        )
        print(json.dumps(response, separators=(",", ":")))
        return 0 if response.get("type") == "prompt.cancel.ok" else 1
    except Exception as exc:
        response, exit_code, message = _direct_rpc_transport_error("prompt", None, exc)
        response["question_id"] = args.question_id
        print(json.dumps(response, separators=(",", ":")))
        print(f"agent-orch prompt cancel: {message}", file=sys.stderr)
        return exit_code


def prompt_list(args: argparse.Namespace) -> int:
    payload: dict[str, object] = {"type": "prompt.list"}
    if getattr(args, "from_stream_id", None):
        payload["producer_stream_id"] = args.from_stream_id
    if getattr(args, "spec_id", None):
        payload["spec_id"] = args.spec_id
    if getattr(args, "open", False):
        payload["open"] = True
    if getattr(args, "limit", None) is not None:
        payload["limit"] = args.limit
    try:
        response = asyncio.run(
            prompt_list_once(
                load_config(),
                payload,
                timeout=float(getattr(args, "timeout", 30.0) or 30.0),
            )
        )
        print(json.dumps(response, separators=(",", ":")))
        return 0 if response.get("type") == "prompt.list.ok" else 1
    except Exception as exc:
        response, exit_code, message = _direct_rpc_transport_error("prompt", None, exc)
        print(json.dumps(response, separators=(",", ":")))
        print(f"agent-orch prompt list: {message}", file=sys.stderr)
        return exit_code


def triage_scan_publish(args: argparse.Namespace) -> int:
    memory_root = Path(args.memory_root).expanduser()
    items = triage.discover_items(memory_root)
    published: list[dict[str, object]] = []
    config = None if getattr(args, "dry_run", False) else load_config()
    answer_to_stream_id = getattr(args, "answer_to_stream_id", None)
    if not getattr(args, "dry_run", False):
        answer_to_stream_id = answer_to_stream_id or discover_leader_stream_id_short(config)
        if not answer_to_stream_id:
            raise ValueError("triage scan-publish requires a discoverable answer route; pass --answer-to-stream-id")
    limit = int(getattr(args, "limit", 20) or 20)
    for item in items:
        if len(published) >= limit:
            break
        state = triage.ensure_state(item, write=not getattr(args, "dry_run", False))
        if not triage.item_is_publishable(item, state):
            continue
        payload = triage.notification_payload(item, state, answer_to_stream_id=answer_to_stream_id)
        if getattr(args, "dry_run", False):
            response = {"type": "dry_run", "notification": payload}
        else:
            response = asyncio.run(
                notification_create_once(config, payload, timeout=float(getattr(args, "timeout", 30.0)))
            )
            if response.get("type") == "notification.create.ok":
                state["last_notified_at"] = triage.utc_now()
                triage.write_json(item.triage_path, state)
        published.append({"spec_id": item.spec_id, "state": state, "response": response})
    print(json.dumps({"published": published}, separators=(",", ":")))
    return 0


def _answer_action_and_spec(answer: dict[str, object]) -> tuple[str, str | None]:
    value = answer.get("value")
    if isinstance(value, dict):
        spec_id = value.get("spec_id")
        action = value.get("action")
        target_spec_id = value.get("target_spec_id")
        if not isinstance(action, str) or not action:
            raise ValueError("triage answer value object must contain action")
        if action == "merge":
            if not isinstance(target_spec_id, str) or not target_spec_id:
                raise ValueError("triage merge answer value object must contain target_spec_id")
            action_value = f"merge:{target_spec_id}"
        else:
            action_value = action
        return action_value, spec_id if isinstance(spec_id, str) and spec_id else None
    if isinstance(value, str) and value:
        return value, None
    action_id = answer.get("action_id")
    if isinstance(action_id, str) and action_id:
        if action_id in {"keep", "defer", "deprecate"}:
            return action_id, None
        if action_id.startswith("merge-"):
            return "merge:" + action_id.removeprefix("merge-"), None
    raise ValueError("answer must contain a triage action value or known action_id")


def triage_apply_answer(args: argparse.Namespace) -> int:
    memory_root = Path(args.memory_root).expanduser()
    if getattr(args, "answer_json", None):
        answer = json.loads(args.answer_json)
        if not isinstance(answer, dict):
            raise ValueError("--answer-json must decode to an object")
        action_value, answer_spec_id = _answer_action_and_spec(answer)
    else:
        action_value = str(args.action)
        answer_spec_id = None
    spec_id = getattr(args, "spec_id", None) or answer_spec_id
    if not spec_id:
        raise ValueError("--spec-id is required unless --answer-json value contains spec_id")
    state = triage.apply_action(
        memory_root,
        spec_id=str(spec_id),
        action_value=action_value,
        decided_by=str(args.decided_by),
        reason=getattr(args, "reason", None),
        defer_until=getattr(args, "defer_until", None),
    )
    print(json.dumps(state, separators=(",", ":")))
    return 0



def _asset_publish_payload_from_args(
    args: argparse.Namespace, *, caller_stream_id: str | None
) -> dict[str, object]:
    session_stream_id = str(getattr(args, "session", None) or caller_stream_id or "")
    if not session_stream_id:
        raise ValueError("--session is required when caller stream discovery fails")
    content_path = Path(str(getattr(args, "content_file", "")))
    try:
        raw_body = content_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"--content-file must be UTF-8 text: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"--content-file is unreadable: {exc}") from exc
    content_type = str(getattr(args, "content_type", "") or "")
    body = validate_asset_payload(content_type, raw_body)
    payload: dict[str, object] = {
        "type": "asset.publish",
        "stream_id": session_stream_id,
        "from_stream_id": session_stream_id,
        "producer": caller_stream_id or session_stream_id,
        "title": str(getattr(args, "title", "") or ""),
        "content_type": content_type,
        "body": body,
        "tags": normalize_tags(getattr(args, "tags", None)),
    }
    asset_id = getattr(args, "asset_id", None)
    if asset_id:
        payload["asset_id"] = str(asset_id)
    spec_id = getattr(args, "spec_id", None)
    if spec_id:
        payload["spec_id"] = str(spec_id)
    return payload


def _asset_comment_public_view(comment: object) -> dict[str, object]:
    if not isinstance(comment, dict):
        return {
            "comment_id": "",
            "section_id": "",
            "block_id": "",
            "run_index": None,
            "excerpt": None,
            "body": "",
            "author": "",
            "created_at": "",
            "resolved": False,
            "resolution_note": None,
        }
    return {
        "comment_id": comment.get("comment_id") or "",
        "section_id": comment.get("section_id") or "",
        "block_id": comment.get("block_id") or "",
        "run_index": comment.get("run_index"),
        "excerpt": comment.get("excerpt"),
        "body": comment.get("body") or "",
        "author": comment.get("author") or "",
        "created_at": comment.get("created_at") or "",
        "resolved": bool(comment.get("resolved")),
        "resolution_note": comment.get("resolution_note"),
    }


def _asset_session_payload_from_args(
    args: argparse.Namespace,
    *,
    caller_stream_id: str | None,
    require_stream_id: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {}
    stream_id = str(getattr(args, "session", None) or caller_stream_id or "")
    if stream_id:
        payload["stream_id"] = stream_id
    elif require_stream_id:
        raise ValueError("--session is required when caller stream discovery fails")
    if caller_stream_id:
        payload["from_stream_id"] = caller_stream_id
    spec_id = getattr(args, "spec_id", None)
    if spec_id:
        payload["spec_id"] = str(spec_id)
    return payload


def asset(args: argparse.Namespace) -> int:
    timeout = float(getattr(args, "timeout", 30.0) or 30.0)
    command = getattr(args, "asset_command", None) or "publish"
    try:
        config = load_config()
        caller_stream_id = discover_leader_stream_id_short(config)
        if command == "health":
            response = asyncio.run(asset_health_once(config, {"type": "asset.health"}, timeout=timeout))
            _print_response(response)
            return 0 if response.get("type") == "asset.health.ok" else 1
        if command in {"list", "get"}:
            spec_only_list = (
                command == "list"
                and bool(getattr(args, "spec_id", None))
                and not bool(getattr(args, "session", None))
            )
            require_stream_id = not bool(getattr(args, "spec_id", None))
            payload = _asset_session_payload_from_args(
                args,
                caller_stream_id=caller_stream_id,
                require_stream_id=require_stream_id,
            )
            payload["type"] = f"asset.{command}"
            if command == "get":
                asset_id = getattr(args, "asset_id_arg", None) or getattr(args, "asset_id", None)
                if not asset_id:
                    raise ValueError("asset get requires an asset id")
                payload["asset_id"] = str(asset_id)
            response = asyncio.run(
                (asset_list_once if command == "list" else asset_get_once)(config, payload, timeout=timeout)
            )
            _print_response(response)
            return 0 if response.get("type") == f"asset.{command}.ok" else 1
        if command == "comments":
            subcommand_or_asset_id = getattr(args, "asset_id_arg", None)
            extra = list(getattr(args, "asset_extra_args", None) or [])
            if subcommand_or_asset_id == "resolve":
                if len(extra) != 2:
                    raise ValueError("asset comments resolve requires <asset_id> <comment_id>")
                payload = _asset_session_payload_from_args(
                    args,
                    caller_stream_id=caller_stream_id,
                    require_stream_id=not bool(getattr(args, "spec_id", None)),
                )
                payload.update(
                    {
                        "type": "asset.comment.resolve",
                        "asset_id": str(extra[0]),
                        "comment_id": str(extra[1]),
                        "resolved": True,
                        "from_stream_id": caller_stream_id,
                        "resolved_by": caller_stream_id,
                    }
                )
                if getattr(args, "note", None):
                    payload["note"] = str(getattr(args, "note"))
                response = asyncio.run(
                    asset_comment_resolve_once(config, payload, timeout=timeout)
                )
                _print_response(response)
                return 0 if response.get("type") == "asset.comment.resolve.ok" else 1
            if not subcommand_or_asset_id or extra:
                raise ValueError("asset comments requires <asset_id>")
            payload = _asset_session_payload_from_args(
                args,
                caller_stream_id=caller_stream_id,
                require_stream_id=not bool(getattr(args, "spec_id", None)),
            )
            payload.update(
                {
                    "type": "asset.comments.list",
                    "asset_id": str(subcommand_or_asset_id),
                    "unresolved": bool(getattr(args, "unresolved", False)),
                }
            )
            response = asyncio.run(
                asset_comments_list_once(config, payload, timeout=timeout)
            )
            if response.get("type") == "asset.comments.list.ok":
                comments = [
                    _asset_comment_public_view(comment)
                    for comment in response.get("comments", [])
                ]
                print(json.dumps(comments, separators=(",", ":")))
                return 0
            _print_response(response)
            return 1
        if command != "publish":
            raise ValueError(f"unknown asset command: {command}")
        for field, flag in (("title", "--title"), ("content_type", "--type"), ("content_file", "--content-file")):
            if not getattr(args, field, None):
                raise ValueError(f"asset publish requires {flag}")
        payload = _asset_publish_payload_from_args(
            args, caller_stream_id=caller_stream_id
        )
        response = asyncio.run(asset_publish_once(config, payload, timeout=timeout))
        _print_response(response)
        return 0 if response.get("type") == "asset.publish.ok" else 1
    except AssetBodyTooLarge as exc:
        response = {
            "type": "asset.error",
            "error_code": "asset_body_too_large",
            "error": "asset_body_too_large",
            "message": str(exc),
        }
        _print_response(response)
        print(f"agent-orch asset: asset_body_too_large: {exc}", file=sys.stderr)
        return 2
    except (AssetValidationError, ValueError) as exc:
        response = {
            "type": "asset.error",
            "error_code": "asset_invalid",
            "error": "asset_invalid",
            "message": str(exc),
        }
        _print_response(response)
        print(f"agent-orch asset: asset_invalid: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        response, exit_code, message = _direct_rpc_transport_error("asset", None, exc)
        _print_response(response)
        print(f"agent-orch asset: {message}", file=sys.stderr)
        return exit_code


def title(args: argparse.Namespace) -> int:
    config = load_config()
    stream_id = discover_leader_stream_id_short(config)
    if not stream_id:
        print("agent-orch title: validation failed: stream_id_unknown", file=sys.stderr)
        return 2
    if ":" not in stream_id:
        print("agent-orch title: validation failed: invalid_stream_id", file=sys.stderr)
        return 2
    host, session_name = stream_id.split(":", 1)
    if not host or not session_name:
        print("agent-orch title: validation failed: invalid_stream_id", file=sys.stderr)
        return 2
    display_name = " ".join(args.text) if isinstance(args.text, list) else str(args.text)
    try:
        response = asyncio.run(
            rename_once(
                config,
                host,
                session_name,
                display_name,
                source="agent",
                timeout=float(getattr(args, "timeout", 30.0) or 30.0),
            )
        )
    except TimeoutError:
        print("agent-orch title: timeout waiting for rename response", file=sys.stderr)
        return 67
    except PermissionError as exc:
        print(f"agent-orch title: auth failed: {exc}", file=sys.stderr)
        return 66
    except OSError as exc:
        print(f"agent-orch title: chat_streamd unreachable: {exc}", file=sys.stderr)
        return 64
    except Exception as exc:
        print(f"agent-orch title: connection dropped after request may have been sent: {exc}", file=sys.stderr)
        return 65
    print(json.dumps(response, separators=(",", ":")))
    return 0 if response.get("type") == "rename.ok" else 1


def role_set(args: argparse.Namespace) -> int:
    config = load_config()
    stream_id = str(getattr(args, "stream_id", "") or "").strip()
    if ":" not in stream_id:
        print("agent-orch role set: validation failed: invalid_stream_id", file=sys.stderr)
        return 2
    role = str(getattr(args, "role", "") or "").strip()
    if not role:
        print("agent-orch role set: validation failed: role_required", file=sys.stderr)
        return 2
    # Load the frontmatter-stripped baseline client-side (same source as spawn
    # --role); a missing file passes None so the daemon reports role_baseline: null.
    baseline = role_baseline.load_role_baseline(config, role)
    baseline_content = baseline.get("content") if isinstance(baseline, dict) else None
    try:
        response = asyncio.run(
            role_set_once(
                config, stream_id, role,
                baseline_content=baseline_content,
                timeout=float(getattr(args, "timeout", 30.0) or 30.0),
            )
        )
    except TimeoutError:
        print("agent-orch role set: timeout waiting for role.set response", file=sys.stderr)
        return 67
    except PermissionError as exc:
        print(f"agent-orch role set: auth failed: {exc}", file=sys.stderr)
        return 66
    except OSError as exc:
        print(f"agent-orch role set: chat_streamd unreachable: {exc}", file=sys.stderr)
        return 64
    except Exception as exc:
        print(f"agent-orch role set: connection dropped after request may have been sent: {exc}", file=sys.stderr)
        return 65
    print(json.dumps(response, separators=(",", ":")))
    return 0 if response.get("type") == "role.set.ok" else 1


def role_get(args: argparse.Namespace) -> int:
    config = load_config()
    stream_id = str(getattr(args, "stream_id", "") or "").strip()
    if ":" not in stream_id:
        print("agent-orch role get: validation failed: invalid_stream_id", file=sys.stderr)
        return 2
    try:
        response = asyncio.run(
            role_get_once(
                config, stream_id,
                timeout=float(getattr(args, "timeout", 30.0) or 30.0),
            )
        )
    except TimeoutError:
        print("agent-orch role get: timeout waiting for role.get response", file=sys.stderr)
        return 67
    except PermissionError as exc:
        print(f"agent-orch role get: auth failed: {exc}", file=sys.stderr)
        return 66
    except OSError as exc:
        print(f"agent-orch role get: chat_streamd unreachable: {exc}", file=sys.stderr)
        return 64
    except Exception as exc:
        print(f"agent-orch role get: connection dropped after request may have been sent: {exc}", file=sys.stderr)
        return 65
    print(json.dumps(response, separators=(",", ":")))
    return 0 if response.get("type") == "role.get.ok" else 1


def status(args: argparse.Namespace) -> int:
    config = load_config()
    stream_id = discover_leader_stream_id_short(config)
    if not stream_id:
        print("agent-orch status: validation failed: stream_id_unknown", file=sys.stderr)
        return 2
    if ":" not in stream_id or not all(stream_id.split(":", 1)):
        print("agent-orch status: validation failed: invalid_stream_id", file=sys.stderr)
        return 2
    fields: dict[str, object] = {}
    if args.goal is not None:
        fields["goal"] = args.goal
    if args.plan:
        fields["plan"] = list(args.plan)
    raw_step_done = args.step_done
    step_done = []
    if raw_step_done is not None:
        for item in raw_step_done if isinstance(raw_step_done, list) else [raw_step_done]:
            step_done.extend(item if isinstance(item, list) else [item])
        fields["step_done"] = step_done[0]
    if args.update is not None:
        fields["update"] = args.update
    if args.handoff_planned is not None:
        fields["handoff_planned"] = args.handoff_planned
    if not fields:
        print(
            "agent-orch status: validation failed: no_fields "
            "(pass at least one of --goal/--plan/--step-done/--update/--handoff-planned)",
            file=sys.stderr,
        )
        return 2
    try:
        response = {}
        updates = [fields, *({"step_done": step} for step in step_done[1:])]
        for update_fields in updates:
            response = asyncio.run(
                status_card_once(
                    config,
                    stream_id,
                    update_fields,
                    timeout=float(getattr(args, "timeout", 30.0) or 30.0),
                )
            )
            if response.get("type") != "status_card.ok":
                break
    except Exception as exc:
        response, exit_code, message = _direct_rpc_transport_error("status_card", None, exc)
        _print_response(response)
        print(f"agent-orch status: {message}", file=sys.stderr)
        return exit_code
    _print_response(response)
    return 0 if response.get("type") == "status_card.ok" else 1


# --- Worker self-terminate gating (Spec A) ----------------------------------

WORKER_SELF_TERMINATE_GRACE_S_DEFAULT = 60.0


def _worker_self_terminate_grace_s() -> float:
    raw = os.environ.get("PENTACLE_WORKER_SELF_TERMINATE_GRACE_S")
    if raw is None:
        return WORKER_SELF_TERMINATE_GRACE_S_DEFAULT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return WORKER_SELF_TERMINATE_GRACE_S_DEFAULT
    if value < 0:
        return WORKER_SELF_TERMINATE_GRACE_S_DEFAULT
    return value


def _iso_to_epoch(iso_str: object) -> float | None:
    if not isinstance(iso_str, str) or not iso_str:
        return None
    text = iso_str.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _session_offline_for_seconds(session: dict[str, object] | None, now: float) -> float | None:
    """Return seconds the session has been offline, or None if unknown.

    Prefers ``offline_since_ts`` (epoch seconds; populated by Spec B) when
    available; falls back to ``closed_at`` (ISO timestamp).
    """
    if not isinstance(session, dict):
        return None
    offline_ts = session.get("offline_since_ts")
    if isinstance(offline_ts, (int, float)) and offline_ts > 0:
        return max(0.0, now - float(offline_ts))
    closed_at = session.get("closed_at")
    epoch = _iso_to_epoch(closed_at)
    if epoch is not None:
        return max(0.0, now - epoch)
    return None


def _emit_self_terminate_decision(
    *,
    path: str,
    stream_id: str,
    parent: str | None,
    parent_offline_for_s: float | None,
    operator_confirm_auto: bool,
) -> None:
    offline_str = (
        f"{parent_offline_for_s:.1f}" if isinstance(parent_offline_for_s, (int, float)) else "null"
    )
    print(
        f"self_terminate_decision path={path} stream={stream_id} "
        f"parent={parent or 'null'} parent_offline_for_s={offline_str} "
        f"operator_confirm_auto={'true' if operator_confirm_auto else 'false'}",
        file=sys.stderr,
        flush=True,
    )


def _classify_self_terminate(
    sessions_by_stream: dict[str, dict[str, object]],
    self_stream_id: str,
    *,
    grace_s: float,
    now: float | None = None,
) -> dict[str, object]:
    """Classify a worker's self-terminate request.

    Returns a dict with:
      - ``path``: one of ``handoff_final``, ``orphan_after_grace``,
        ``orphan_grace_pending``, ``top_level_refused``, ``live_leader_refused``.
      - ``operator_confirm_auto``: whether the CLI should auto-set
        ``operator_confirm=True`` on the self-close.
      - ``error_code``: typed error code to surface when ``operator_confirm_auto``
        is False (terminate_requires_leader_close / terminate_requires_operator_close
        / terminate_grace_pending); None for the auto-set paths.
      - ``parent_stream_id``: parent stream_id (string) or None.
      - ``parent_offline_for_s``: float or None.
    """
    if now is None:
        now = time.time()
    self_summary = sessions_by_stream.get(self_stream_id) or {}
    parent_stream_id = self_summary.get("parent_stream_id") or None
    handoff_from = self_summary.get("handoff_from_stream_id") or None
    parent_stream_id_str = str(parent_stream_id) if parent_stream_id else None
    handoff_from_str = str(handoff_from) if handoff_from else None

    # Case A — handoff-final-node: no live parent, came from a handoff.
    if not parent_stream_id_str and handoff_from_str:
        return {
            "path": "handoff_final",
            "operator_confirm_auto": True,
            "error_code": None,
            "parent_stream_id": None,
            "parent_offline_for_s": None,
        }

    # Case A.5 — leader-authorized self-close: the spawning leader set
    # `--self-close-on-completion`, persisted as `self_close_on_completion=True`
    # on the session row. Wins over every refusal path below (top_level_refused,
    # live_leader_refused, orphan_grace_pending). Falsy / missing flag falls
    # through to the existing Case B–D classifications, so a stale snapshot or
    # pre-migration row degrades to refusal (fail-safe).
    if self_summary.get("self_close_on_completion") is True:
        return {
            "path": "worker_authorized_self_close",
            "operator_confirm_auto": True,
            "error_code": None,
            "parent_stream_id": parent_stream_id_str,
            "parent_offline_for_s": None,
        }

    # Case C — top-level operator session.
    if not parent_stream_id_str and not handoff_from_str:
        return {
            "path": "top_level_refused",
            "operator_confirm_auto": False,
            "error_code": "terminate_requires_operator_close",
            "parent_stream_id": None,
            "parent_offline_for_s": None,
        }

    # Parent exists — Case B or Case D.
    parent_summary = sessions_by_stream.get(parent_stream_id_str)
    parent_online = bool(parent_summary.get("online")) if isinstance(parent_summary, dict) else False
    parent_closed_at = parent_summary.get("closed_at") if isinstance(parent_summary, dict) else None
    parent_offline_for_s = _session_offline_for_seconds(parent_summary, now)

    # If parent is unknown to the snapshot OR explicitly online → live leader.
    if parent_online and parent_summary is not None and not parent_closed_at:
        return {
            "path": "live_leader_refused",
            "operator_confirm_auto": False,
            "error_code": "terminate_requires_leader_close",
            "parent_stream_id": parent_stream_id_str,
            "parent_offline_for_s": None,
        }

    # Parent offline: only treat as orphan when we can measure how long it has been offline.
    if parent_offline_for_s is None:
        # Conservative: refuse with grace_pending; the daemon reaper (Spec B) is the
        # authoritative cleanup path when the offline duration is indeterminate.
        return {
            "path": "orphan_grace_pending",
            "operator_confirm_auto": False,
            "error_code": "terminate_grace_pending",
            "parent_stream_id": parent_stream_id_str,
            "parent_offline_for_s": None,
        }

    if parent_offline_for_s <= grace_s:
        return {
            "path": "orphan_grace_pending",
            "operator_confirm_auto": False,
            "error_code": "terminate_grace_pending",
            "parent_stream_id": parent_stream_id_str,
            "parent_offline_for_s": parent_offline_for_s,
        }

    return {
        "path": "orphan_after_grace",
        "operator_confirm_auto": True,
        "error_code": None,
        "parent_stream_id": parent_stream_id_str,
        "parent_offline_for_s": parent_offline_for_s,
    }


def _resolve_self_terminate(config, self_stream_id: str) -> dict[str, object]:
    """Fetch a fresh snapshot and classify the worker's self-terminate request."""
    snapshot = fetch_snapshot(config, events_mode="summary")
    sessions_by_stream: dict[str, dict[str, object]] = {}
    for session in snapshot.get("sessions", []) or []:
        if isinstance(session, dict) and isinstance(session.get("stream_id"), str):
            sessions_by_stream[session["stream_id"]] = session
    return _classify_self_terminate(
        sessions_by_stream,
        self_stream_id,
        grace_s=_worker_self_terminate_grace_s(),
    )


def close(args: argparse.Namespace) -> int:
    """Close (delete) a chat_streamd session by connecting directly to the daemon.

    Mirrors ``report()``: this does NOT route through the local agent-orch
    wrapper socket, so it works from any context that has a valid config —
    including spawned workers and top-level agents that never ran
    ``agent-orch start``.
    ``agent-orch close --operator-confirm <own_stream>`` is the universal
    self-close: the daemon authorizes a caller closing its own stream
    (caller == target). The actual session/tmux teardown happens daemon-side in
    chat_streamd's ``_perform_close`` regardless of how the close RPC arrives.

    Because the CLI no longer routes through the wrapper's ``_close`` handler,
    chat_streamd is the authoritative owner of session lifecycle and a
    ``close.ok`` is durable daemon-side.
    """
    if not _require_stream_id("close", args.stream_id):
        return 2
    config = load_config()
    caller_stream_id = (
        getattr(args, "caller_stream_id", None)
        or getattr(args, "from_stream_id", None)
        or discover_leader_stream_id_short(config)
    )
    # The daemon's operator-confirm identity check reads ``from_stream_id`` as
    # the caller's identity (chat_streamd ``_check_close_caller_identity``), so a
    # self-close needs from_stream_id == target. Default it to the resolved
    # caller identity, matching the report --terminate self-close follow-up.
    from_stream_id = getattr(args, "from_stream_id", None) or caller_stream_id
    timeout = float(getattr(args, "timeout", 30.0) or 30.0)
    try:
        response = asyncio.run(
            close_once(
                config,
                args.stream_id,
                reason=args.reason or "manual",
                timeout=timeout,
                operator_confirm=bool(getattr(args, "operator_confirm", False)),
                force=bool(getattr(args, "force", False)),
                defer_if_working=bool(getattr(args, "defer_if_working", False)),
                from_stream_id=from_stream_id,
                caller_stream_id=caller_stream_id,
                progeny_stream_id=getattr(args, "progeny", None),
                disposition_waived_reason=getattr(args, "disposition_waived_reason", None),
            )
        )
    except ValueError as exc:
        print(f"agent-orch close: validation failed: {exc}", file=sys.stderr)
        return 2
    except TimeoutError:
        print("agent-orch close: timeout waiting for close response", file=sys.stderr)
        return 67
    except PermissionError as exc:
        print(f"agent-orch close: auth failed: {exc}", file=sys.stderr)
        return 66
    except OSError as exc:
        print(f"agent-orch close: chat_streamd unreachable: {exc}", file=sys.stderr)
        return 64
    except Exception as exc:
        print(f"agent-orch close: connection dropped after request may have been sent: {exc}", file=sys.stderr)
        return 65
    _print_response(response)
    return 0 if response.get("type") in {"close.ok", "close.already_closed"} else 1


def drain_sessions(args: argparse.Namespace) -> int:
    if not bool(getattr(args, "operator_confirm", False)):
        print("agent-orch drain-sessions: validation failed: --operator-confirm is required", file=sys.stderr)
        return 2
    timeout = float(getattr(args, "timeout", 30.0) or 30.0)
    try:
        response = asyncio.run(
            drain_sessions_once(
                load_config(),
                list(args.stream_ids),
                timeout=timeout,
                operator_confirm=True,
            )
        )
    except ValueError as exc:
        print(f"agent-orch drain-sessions: validation failed: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        response, exit_code, message = _direct_rpc_transport_error("drain_sessions", None, exc)
        _print_response(response)
        print(f"agent-orch drain-sessions: {message}", file=sys.stderr)
        return exit_code
    _print_response(response)
    return 0 if response.get("type") == "drain_sessions.ok" else 1


def reconcile_status(args: argparse.Namespace) -> int:
    timeout = float(getattr(args, "timeout", 30.0) or 30.0)
    try:
        response = asyncio.run(
            reconcile_status_once(
                load_config(),
                host=getattr(args, "host", None),
                timeout=timeout,
            )
        )
    except Exception as exc:
        response, exit_code, message = _direct_rpc_transport_error("reconcile.status", None, exc)
        _print_response(response)
        print(f"agent-orch reconcile status: {message}", file=sys.stderr)
        return exit_code
    if bool(getattr(args, "json", False)):
        print(json.dumps(response, separators=(",", ":")))
    else:
        counts = response.get("counts") if isinstance(response.get("counts"), dict) else {}
        print(
            " ".join(
                f"{key}={counts.get(key, 0)}"
                for key in (
                    "row_open_session_dead",
                    "row_closed_tree_alive",
                    "row_open_host_unreachable",
                    "unmanaged_tree",
                )
            )
        )
        details = response.get("details") if isinstance(response.get("details"), list) else []
        for item in details:
            if not isinstance(item, dict):
                continue
            print(f"{item.get('class')} {item.get('stream_id')}")
    return 0 if response.get("type") == "reconcile.status.ok" else 1


def reparent(args: argparse.Namespace) -> int:
    """Direct-connect ``agent-orch reparent <worker> --to <new-parent>`` (P8).

    Mirrors ``close()``: connects straight to chat_streamd via ``reparent_once``
    (no wrapper socket), resolves the caller identity the same way close does
    (``--from-stream-id`` / ``--caller-stream-id`` default to the local leader),
    and maps the exit code off the daemon ``reparent.*`` response.
    """
    config = load_config()
    caller_stream_id = (
        getattr(args, "caller_stream_id", None)
        or getattr(args, "from_stream_id", None)
        or discover_leader_stream_id_short(config)
    )
    from_stream_id = getattr(args, "from_stream_id", None) or caller_stream_id
    timeout = float(getattr(args, "timeout", 30.0) or 30.0)
    try:
        response = asyncio.run(
            reparent_once(
                config,
                args.stream_id,
                args.new_parent,
                reason=args.reason or "reparent",
                timeout=timeout,
                from_stream_id=from_stream_id,
                caller_stream_id=caller_stream_id,
            )
        )
    except ValueError as exc:
        print(f"agent-orch reparent: validation failed: {exc}", file=sys.stderr)
        return 2
    except TimeoutError:
        print("agent-orch reparent: timeout waiting for reparent response", file=sys.stderr)
        return 67
    except PermissionError as exc:
        print(f"agent-orch reparent: auth failed: {exc}", file=sys.stderr)
        return 66
    except OSError as exc:
        print(f"agent-orch reparent: chat_streamd unreachable: {exc}", file=sys.stderr)
        return 64
    except Exception as exc:
        print(f"agent-orch reparent: connection dropped after request may have been sent: {exc}", file=sys.stderr)
        return 65
    _print_response(response)
    return 0 if response.get("type") == "reparent.ok" else 1


def await_command(args: argparse.Namespace) -> int:
    if not _require_stream_id("await", args.stream_id):
        return 2
    try:
        config = load_config()
        response = asyncio.run(
            await_report_once(
                config,
                args.stream_id,
                args.msg_id,
                timeout=float(args.timeout),
                include_details=bool(args.include_details),
                include_extras=bool(args.include_extras),
            )
        )
    except TimeoutError:
        print("agent-orch await: timeout waiting for await_report response", file=sys.stderr)
        return 67
    except PermissionError as exc:
        print(f"agent-orch await: auth failed: {exc}", file=sys.stderr)
        return 66
    except OSError as exc:
        print(f"agent-orch await: chat_streamd unreachable: {exc}", file=sys.stderr)
        return 64
    except Exception as exc:
        print(f"agent-orch await: connection dropped after request may have been sent: {exc}", file=sys.stderr)
        return 65
    _print_response(response)
    if response.get("type") == "await_report.closed_without_report" or response.get("reason") == "closed_without_report":
        print(
            "agent-orch await: stream closed without a terminal report",
            file=sys.stderr,
        )
        return 69
    return 0 if response.get("ok") else 1


def _merge_report_structured_flags(payload: dict[str, object], args: argparse.Namespace) -> None:
    completion_kind = getattr(args, "completion_kind", None)
    qa_verdict = getattr(args, "qa_verdict", None)
    target_sha = getattr(args, "target_sha", None)
    attestation_stream_id = getattr(args, "qa_attestation_stream_id", None)
    attestation_report_id = getattr(args, "qa_attestation_report_id", None)
    violations: list[dict[str, object]] = []

    if (attestation_stream_id is None) != (attestation_report_id is None):
        missing = "--qa-attestation-report-id" if attestation_stream_id else "--qa-attestation-stream-id"
        violations.append({
            "field": "qa_attestation",
            "code": "missing_field",
            "detail": f"qa attestation requires both flags; missing {missing}",
        })
    if completion_kind is not None and "completion_kind" in payload:
        violations.append({
            "field": "completion_kind",
            "code": "flag_payload_conflict",
            "detail": "--completion-kind conflicts with completion_kind in --result or --result-file",
        })
    if qa_verdict is not None and "qa_verdict" in payload:
        violations.append({
            "field": "qa_verdict",
            "code": "flag_payload_conflict",
            "detail": "--qa-verdict conflicts with qa_verdict in --result or --result-file",
        })
    if target_sha is not None and "target_sha" in payload:
        violations.append({
            "field": "target_sha",
            "code": "flag_payload_conflict",
            "detail": "--target-sha conflicts with target_sha in --result or --result-file",
        })
    if attestation_stream_id is not None and "qa_attestation" in payload:
        violations.append({
            "field": "qa_attestation",
            "code": "flag_payload_conflict",
            "detail": "qa attestation flags conflict with qa_attestation in --result or --result-file",
        })
    if violations:
        raise SchemaError("schema_error", "report structured-flag validation failed", violations=violations)
    if completion_kind is not None:
        payload["completion_kind"] = completion_kind
    if qa_verdict is not None:
        payload["qa_verdict"] = qa_verdict
    if target_sha is not None:
        payload["target_sha"] = target_sha
    if attestation_stream_id is not None:
        payload["qa_attestation"] = {
            "stream_id": attestation_stream_id,
            "report_id": attestation_report_id,
        }


def _report_payload_from_args(args: argparse.Namespace) -> tuple[dict[str, object], bytes | None]:
    if args.result is not None:
        try:
            payload = json.loads(args.result)
        except json.JSONDecodeError as exc:
            raise ValueError(f"schema_error: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("schema_error: --result must be a JSON object")
        if args.reason is not None:
            payload["reason"] = args.reason
        _merge_report_structured_flags(payload, args)
        validate_report_payload(payload, args.status, enforce_inline_caps=True)
        return payload, None
    if args.result_file is not None:
        path = Path(args.result_file).expanduser()
        try:
            stat = path.stat()
        except OSError as exc:
            raise FileNotFoundError(f"result_file_unreadable: {path}") from exc
        max_bytes = 64 * 1024 * 1024
        if stat.st_size > max_bytes:
            raise OverflowError("result_file_too_large")
        try:
            with path.open("rb") as handle:
                data = handle.read()
        except OSError as exc:
            raise FileNotFoundError(f"result_file_unreadable: {path}") from exc
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"schema_error: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("schema_error: result file must be a JSON object")
        _merge_report_structured_flags(payload, args)
        if any(getattr(args, field, None) is not None for field in (
            "completion_kind", "qa_attestation_stream_id",
        )):
            raise SchemaError(
                "schema_error",
                "report structured-flag validation failed",
                violations=[{
                    "field": "result_file",
                    "code": "unsupported_combination",
                    "detail": "completion and attestation flags require inline --result",
                }],
            )
        if getattr(args, "qa_verdict", None) is not None or getattr(args, "target_sha", None) is not None:
            validate_report_payload(payload, args.status, enforce_inline_caps=True)
            return payload, None
        validate_report_payload(payload, args.status, enforce_inline_caps=False)
        return {}, data
    payload: dict[str, object] = {}
    if args.reason is not None:
        payload["reason"] = args.reason
    _merge_report_structured_flags(payload, args)
    validate_report_payload(payload, args.status, enforce_inline_caps=True)
    return payload, None


def _governance_echo_mismatch(
    submitted: dict[str, object], response: dict[str, object],
) -> str | None:
    durable = response.get("report") if isinstance(response.get("report"), dict) else response
    for field in ("completion_kind", "qa_verdict", "target_sha", "qa_attestation"):
        if field in submitted and durable.get(field) != submitted[field]:
            return field
    return None


def _governance_echo_is_durable(
    submitted: dict[str, object], response: dict[str, object],
) -> bool:
    mismatch = _governance_echo_mismatch(submitted, response)
    if mismatch is None:
        return True
    print(f"agent-orch report: durable_governance_field_mismatch: {mismatch}", file=sys.stderr)
    return False


def _verify_report_durability_after_transport_error(
    config,
    *,
    from_stream_id: str,
    report_id: str,
    timeout: float,
) -> dict[str, object] | None:
    try:
        inspect = asyncio.run(
            inspect_stream_once(
                config,
                from_stream_id,
                report_id=report_id,
                event_tail=0,
                timeout=max(1.0, min(timeout, 5.0)),
            )
        )
    except Exception as exc:
        print(
            f"agent-orch report: report durability unknown for report_id={report_id}; "
            f"stream-mode inspect failed: {exc}",
            file=sys.stderr,
        )
        return None
    report = inspect.get("existing_report") if isinstance(inspect, dict) else None
    if isinstance(report, dict) and report.get("report_id") == report_id:
        print(
            f"agent-orch report: report durability confirmed by stream-mode inspect "
            f"for report_id={report_id}",
            file=sys.stderr,
        )
        if isinstance(report.get("ledger_row_id"), int):
            return report
        print(
            f"agent-orch report: durable report lacked ledger row id for report_id={report_id}",
            file=sys.stderr,
        )
        return None
    print(
        f"agent-orch report: report may not be durable for report_id={report_id}; "
        "retry with the same --report-id or inspect the stream without --msg-id",
        file=sys.stderr,
    )
    return None


def _emit_durable_confirmed(
    report_id: str,
    report: dict[str, object],
    *,
    state: str,
    notice_delivery: dict[str, object],
) -> None:
    print(json.dumps({
        "type": "report.durable_confirmed",
        "report_id": report_id,
        "ledger_row_id": report["ledger_row_id"],
        "state": state,
        "notice_delivery": notice_delivery,
    }, separators=(",", ":")))


def _child_report_ready_tell_ids(report_id: str) -> tuple[str, ...]:
    digest = hashlib.sha256(report_id.encode("utf-8")).hexdigest()
    current = f"child-report-ready-v2-{digest}"
    safe_id = re.sub(r"[^A-Za-z0-9_.:-]+", "-", report_id).strip("-")
    legacy = f"child-report-ready-{safe_id}" if safe_id else ""
    return (current, legacy) if legacy and legacy != current else (current,)


def _notice_delivery_from_ledger_response(
    response: dict[str, object],
    *,
    tell_id: str,
    report_id: str,
    from_stream_id: str,
    to_stream_id: str,
) -> dict[str, object] | None:
    tell = response.get("tell") if isinstance(response.get("tell"), dict) else None
    if (
        response.get("type") != "ledger_get.ok"
        or response.get("tell_id") != tell_id
        or tell is None
        or tell.get("tell_id") != tell_id
        or tell.get("reason_key") != "child_report_ready"
        or tell.get("from_stream_id") != from_stream_id
        or tell.get("to_stream_id") != to_stream_id
        or f"report_id={report_id}" not in str(tell.get("text") or "").splitlines()
    ):
        return None
    delivery_ack_at = tell.get("delivery_ack_at") or tell.get("delivered_at")
    fields = (
        "tell_id",
        "ledger_row_id",
        "delivery_status",
        "delivery_note",
        "to_stream_id",
        "error_code",
    )
    notice_delivery = {
        field: tell[field]
        for field in fields
        if field in tell and tell[field] is not None
    }
    if delivery_ack_at is not None:
        notice_delivery["delivery_ack_at"] = delivery_ack_at
    return notice_delivery


def _reconcile_report_notice_delivery(
    config,
    *,
    from_stream_id: str,
    to_stream_id: str,
    report_id: str,
    timeout: float,
) -> dict[str, object]:
    tell_ids = _child_report_ready_tell_ids(report_id)
    for index, tell_id in enumerate(tell_ids):
        try:
            response = asyncio.run(
                ledger_get_once(
                    config,
                    tell_id,
                    timeout=max(1.0, min(timeout, 5.0)),
                )
            )
        except Exception as exc:
            return {
                "delivery_status": "failed",
                "tell_id": tell_ids[0],
                "to_stream_id": to_stream_id,
                "error_code": f"notice_receipt_read_failed:{exc}",
            }
        notice_delivery = _notice_delivery_from_ledger_response(
            response,
            tell_id=tell_id,
            report_id=report_id,
            from_stream_id=from_stream_id,
            to_stream_id=to_stream_id,
        )
        if notice_delivery is not None:
            return notice_delivery
        error_code = str(response.get("error_code") or response.get("error") or "")
        if error_code == "tell_not_found" and index + 1 < len(tell_ids):
            continue
        return {
            "delivery_status": "failed",
            "tell_id": tell_ids[0],
            "to_stream_id": to_stream_id,
            "error_code": error_code or "notice_receipt_mismatch",
        }
    return {
        "delivery_status": "failed",
        "tell_id": tell_ids[0],
        "to_stream_id": to_stream_id,
        "error_code": "notice_receipt_missing",
    }


COMMITTED_PENDING_PROOF = "committed_pending_proof"


def _notice_delivery_is_authoritative(
    notice_delivery: dict[str, object],
    *,
    to_stream_id: str,
) -> bool:
    has_ids = (
        isinstance(notice_delivery.get("tell_id"), str)
        and bool(notice_delivery.get("tell_id"))
        and isinstance(notice_delivery.get("ledger_row_id"), int)
        and notice_delivery.get("to_stream_id") == to_stream_id
    )
    if not has_ids:
        return False
    status = notice_delivery.get("delivery_status")
    if status == "delivered":
        return True
    # A committed-but-late-proof notice is delivered-enough: the durable card
    # exists and the paste left the composer. It reconciles asynchronously, so
    # it is authoritative only when it carries the do_not_resubmit commitment.
    return status == COMMITTED_PENDING_PROOF and notice_delivery.get("do_not_resubmit") is True


def _finish_durable_report_recovery(
    config,
    *,
    from_stream_id: str,
    report_id: str,
    timeout: float,
    payload: dict[str, object],
    durable_report: dict[str, object],
    state: str,
) -> int:
    if not _governance_echo_is_durable(payload, durable_report):
        return 2
    to_stream_id = durable_report.get("to_stream_id")
    if not isinstance(to_stream_id, str) or not to_stream_id:
        print(
            "agent-orch report: durable parent target missing; "
            "notice delivery cannot be reconciled",
            file=sys.stderr,
        )
        return 2
    notice_delivery = _reconcile_report_notice_delivery(
        config,
        from_stream_id=from_stream_id,
        to_stream_id=to_stream_id,
        report_id=report_id,
        timeout=timeout,
    )
    _emit_durable_confirmed(
        report_id,
        durable_report,
        state=state,
        notice_delivery=notice_delivery,
    )
    if _notice_delivery_is_authoritative(notice_delivery, to_stream_id=to_stream_id):
        return 0
    print(
        "agent-orch report: parent notice not delivered: "
        f"{notice_delivery.get('error_code') or notice_delivery.get('delivery_status') or 'unknown'} "
        f"to {to_stream_id} "
        f"correlation {notice_delivery.get('tell_id') or 'unknown'}; report remains durable",
        file=sys.stderr,
    )
    return 7


def _best_effort_record_report_rejection(status: str, msg_id: int | None) -> None:
    """pop2: a terminal report rejected client-side by schema validation dies
    here (exit 2) and never reaches the daemon, so a finished self-close seat
    that botched its payload leaves NO durable completion signal. Still transmit
    a minimal frame so the daemon can durably record the rejected attempt (and
    the reconciler sweep can reap the seat). Best-effort and never raises: the
    seat's UX — the printed copy-paste fix and exit 2 — is unchanged whether or
    not the daemon is reachable. Only terminal statuses (which authorize a
    self-close) are worth recording; a rejected `progress` is just noise."""
    if status not in {"done", "error", "aborted"}:
        return
    from_stream_id = env_stream_id()
    if not from_stream_id:
        return
    try:
        config = load_config()
        request: dict[str, object] = {
            "type": "report",
            "from_stream_id": from_stream_id,
            "caller_stream_id": from_stream_id,
            "msg_id": msg_id if isinstance(msg_id, int) else 0,
            "status": status,
        }
        # The daemon re-validates, records the rejection for a self-close seat,
        # then returns its own schema error — which we discard.
        asyncio.run(report_once(config, request, timeout=5.0))
    except Exception:  # noqa: BLE001 - best-effort; the schema rejection stays primary
        pass


def report(args: argparse.Namespace) -> int:
    if getattr(args, "terminate", False) and args.status == "progress":
        print("agent-orch report: validation failed: report: --terminate is incompatible with --status=progress", file=sys.stderr)
        return 2
    # Omitted --msg-id means a proactive stream report. The daemon accepts
    # msg_id=0 as the conventional self/proactive key; awaited reports still
    # pass the specific inbox msg_id they answer.
    if args.msg_id is None:
        args.msg_id = 0
    if args.status in {"error", "aborted"} and not args.reason and args.result is None and args.result_file is None:
        exc = SchemaError(
            "missing_reason",
            "reason is required for error and aborted reports",
            violations=[{"field": "reason", "code": "missing_field", "detail": "reason is required", "missing": True}],
        )
        message = _format_report_payload_validation_failure(exc, args.status, msg_id=args.msg_id)
        print(f"agent-orch report: validation failed:\n{message}", file=sys.stderr)
        # pop2: this early error/aborted rejection is also a terminal report that
        # would otherwise vanish for a self-close seat — record it too.
        _best_effort_record_report_rejection(args.status, args.msg_id)
        return 2
    try:
        payload, blob_bytes = _report_payload_from_args(args)
    except FileNotFoundError as exc:
        print(f"agent-orch report: validation failed: {exc}", file=sys.stderr)
        return 2
    except OverflowError as exc:
        print(f"agent-orch report: validation failed: {exc}", file=sys.stderr)
        return 2
    except (ValueError, SchemaError) as exc:
        message = _format_report_payload_validation_failure(exc, args.status, msg_id=args.msg_id)
        print(f"agent-orch report: validation failed:\n{message}", file=sys.stderr)
        _best_effort_record_report_rejection(args.status, args.msg_id)
        return 2

    config = load_config()
    from_stream_id = getattr(args, "from_stream_id", None) or discover_leader_stream_id_short(config)
    if not from_stream_id:
        print("agent-orch report: validation failed: stream_id_unknown", file=sys.stderr)
        return 2
    if not _require_stream_id("report", from_stream_id):
        return 2
    report_id = args.report_id or str(uuid.uuid4())
    terminate_decision: dict[str, object] | None = None
    if getattr(args, "terminate", False):
        try:
            terminate_decision = _resolve_self_terminate(config, from_stream_id)
        except Exception as exc:
            print(
                f"agent-orch report: terminate classification failed: {exc}",
                file=sys.stderr,
            )
            return 5
        _emit_self_terminate_decision(
            path=str(terminate_decision.get("path") or ""),
            stream_id=from_stream_id,
            parent=terminate_decision.get("parent_stream_id") if isinstance(terminate_decision.get("parent_stream_id"), str) else None,
            parent_offline_for_s=terminate_decision.get("parent_offline_for_s") if isinstance(terminate_decision.get("parent_offline_for_s"), (int, float)) else None,
            operator_confirm_auto=bool(terminate_decision.get("operator_confirm_auto")),
        )

    request: dict[str, object] = {
        "type": "report",
        "report_id": report_id,
        "from_stream_id": from_stream_id,
        "msg_id": args.msg_id,
        "status": args.status,
        **payload,
    }
    # The authenticated-caller identity (env only — never snapshot discovery, so
    # an explicit --from-stream-id override still skips the snapshot). The daemon
    # rejects a from_stream_id that does not match this caller, so a misused
    # `--from-stream-id <other-stream>` can no longer attribute or terminate
    # another seat. Omitted when unset: a tokenless caller asserts no identity.
    caller_stream_id = env_stream_id()
    if caller_stream_id:
        request["caller_stream_id"] = caller_stream_id
    if getattr(args, "terminate", False):
        request["terminate"] = True
        if terminate_decision and terminate_decision.get("operator_confirm_auto"):
            request["close_on_ingest"] = True
    if getattr(args, "discharges", None):
        request["discharges"] = list(getattr(args, "discharges"))
    timeout = float(args.timeout)
    try:
        if blob_bytes is not None:
            upload = asyncio.run(upload_blob_once(config, blob_bytes, timeout=timeout))
            if upload.get("type") != "upload_blob.ok":
                print(f"agent-orch report: upload failed: {upload.get('error_code') or upload.get('type')}", file=sys.stderr)
                return 5
            request["result_blob_sha"] = upload.get("blob_sha")
        response = asyncio.run(report_once(config, request, timeout=timeout))
    except TimeoutError:
        print("agent-orch report: timeout waiting for report response", file=sys.stderr)
        durable_report = _verify_report_durability_after_transport_error(
            config,
            from_stream_id=from_stream_id,
            report_id=report_id,
            timeout=timeout,
        )
        if durable_report is not None:
            return _finish_durable_report_recovery(
                config,
                from_stream_id=from_stream_id,
                report_id=report_id,
                timeout=timeout,
                payload=payload,
                durable_report=durable_report,
                state="confirmed_after_timeout",
            )
        return 67
    except PermissionError as exc:
        print(f"agent-orch report: auth failed: {exc}", file=sys.stderr)
        return 66
    except OSError as exc:
        print(f"agent-orch report: chat_streamd unreachable: {exc}", file=sys.stderr)
        durable_report = _verify_report_durability_after_transport_error(
            config,
            from_stream_id=from_stream_id,
            report_id=report_id,
            timeout=timeout,
        )
        if durable_report is not None:
            return _finish_durable_report_recovery(
                config,
                from_stream_id=from_stream_id,
                report_id=report_id,
                timeout=timeout,
                payload=payload,
                durable_report=durable_report,
                state="confirmed_after_transport_error",
            )
        return 64
    except Exception as exc:
        print(f"agent-orch report: connection dropped after request may have been sent: {exc}", file=sys.stderr)
        durable_report = _verify_report_durability_after_transport_error(
            config,
            from_stream_id=from_stream_id,
            report_id=report_id,
            timeout=timeout,
        )
        if durable_report is not None:
            return _finish_durable_report_recovery(
                config,
                from_stream_id=from_stream_id,
                report_id=report_id,
                timeout=timeout,
                payload=payload,
                durable_report=durable_report,
                state="confirmed_after_transport_error",
            )
        return 65
    if response.get("type") == "report.indeterminate":
        durable_report = _verify_report_durability_after_transport_error(
            config,
            from_stream_id=from_stream_id,
            report_id=report_id,
            timeout=timeout,
        )
        if durable_report is not None:
            return _finish_durable_report_recovery(
                config,
                from_stream_id=from_stream_id,
                report_id=report_id,
                timeout=timeout,
                payload=payload,
                durable_report=durable_report,
                state="confirmed_after_indeterminate",
            )
    if response.get("type") == "report.ok":
        if not _governance_echo_is_durable(payload, response):
            return 2
    print(json.dumps(response, separators=(",", ":")))
    for warning in response.get("warnings", []):
        if not isinstance(warning, dict):
            continue
        print(
            "agent-orch report: warning: "
            f"{warning.get('code') or 'report_warning'}: {warning.get('message') or ''}",
            file=sys.stderr,
        )
    if response.get("type") == "report.ok":
        notice_delivery = (
            response.get("notice_delivery")
            if isinstance(response.get("notice_delivery"), dict)
            else None
        )
        notice_target = str(
            response.get("to_stream_id")
            or (notice_delivery or {}).get("to_stream_id")
            or ""
        )
        if args.status in {"done", "error", "aborted"} and notice_target and not (
            notice_delivery is not None
            and _notice_delivery_is_authoritative(
                notice_delivery, to_stream_id=notice_target,
            )
        ):
            print(
                "agent-orch report: parent notice not delivered: "
                f"{(notice_delivery or {}).get('error_code') or (notice_delivery or {}).get('delivery_status') or 'missing_notice_delivery'} "
                f"to {notice_target} "
                f"correlation {(notice_delivery or {}).get('tell_id') or 'unknown'}; report remains durable",
                file=sys.stderr,
            )
            return 7
        if getattr(args, "terminate", False):
            if response.get("durability_ack") is not True:
                print(
                    "agent-orch report: terminate refused: report_durability_ack_missing",
                    file=sys.stderr,
                )
                return 5
            if not terminate_decision or not terminate_decision.get("operator_confirm_auto"):
                error_code = str((terminate_decision or {}).get("error_code") or "terminate_refused")
                print(
                    f"agent-orch report: terminate refused: {error_code}",
                    file=sys.stderr,
                )
                # report.ok still ingested daemon-side; CLI returns nonzero so the
                # caller (leader / operator) knows to take over the close.
                return 6
            if response.get("closed") is True:
                return 0
            if response.get("closed") is False:
                print(
                    f"agent-orch report: terminate close failed: {response.get('close_error') or response.get('close_reason') or 'close_failed'}",
                    file=sys.stderr,
                )
                return 5
            try:
                close_response = asyncio.run(
                    close_once(
                        config,
                        from_stream_id,
                        reason="report_terminate",
                        timeout=timeout,
                        operator_confirm=True,
                        from_stream_id=from_stream_id,
                        caller_stream_id=from_stream_id,
                        disposition_waived_reason=getattr(args, "disposition_waived_reason", None),
                    )
                )
            except Exception as exc:
                print(f"agent-orch report: terminate close failed: {exc}", file=sys.stderr)
                return 5
            if close_response.get("type") != "close.ok":
                print(
                    f"agent-orch report: terminate close failed: {close_response.get('error') or close_response.get('error_code') or close_response.get('type')}",
                    file=sys.stderr,
                )
                return 5
        return 0
    error_code = str(response.get("error_code") or "report_error")
    print(f"agent-orch report: report rejected: {error_code}", file=sys.stderr)
    if response.get("hint"):
        print(f"agent-orch report: hint: {response['hint']}", file=sys.stderr)
    if error_code in {"report_id_replay_conflict"}:
        return 4
    if error_code in {"stream_unknown", "result_blob_unknown"}:
        return 3
    return 2


def grant_self_token(args: argparse.Namespace) -> int:
    """Bootstrap-once: ask chat_streamd to mint an AGENT_ORCH_STREAM_TOKEN for
    this session's stream id and print the plaintext to stdout. Use as:

        export AGENT_ORCH_STREAM_TOKEN=$(agent-orch grant-self-token)

    Refused if the session already has a token (`token_already_set`). Typed
    exit codes mirror the other direct verbs: 0 ok, 1 grant_token.error,
    2 invalid stream id, 64 daemon unreachable, 67 timeout.
    """
    config = load_config()
    stream_id = (
        getattr(args, "stream_id", None)
        or os.environ.get("AGENT_ORCH_STREAM_ID")
        or os.environ.get("PENTACLE_STREAM_ID")
    )
    if not stream_id or ":" not in stream_id:
        print(
            "agent-orch grant-self-token: stream id required "
            "(env AGENT_ORCH_STREAM_ID/PENTACLE_STREAM_ID or --stream <host:session>)",
            file=sys.stderr,
        )
        return 2
    try:
        response = asyncio.run(
            grant_token_once(config, stream_id, timeout=args.timeout)
        )
    except TimeoutError:
        print("grant_token: timeout", file=sys.stderr)
        return 67
    except OSError as exc:
        print(f"grant_token: chat_streamd unreachable: {exc}", file=sys.stderr)
        return 64
    if response.get("type") == "grant_token.ok":
        token = response.get("stream_token") or ""
        if not token:
            print("grant_token.ok missing stream_token", file=sys.stderr)
            return 1
        # Plaintext to stdout only — caller captures via $(...) and exports.
        print(token)
        return 0
    err = response.get("error") or "grant_token_error"
    msg = response.get("message") or err
    print(f"grant_token: {err}: {msg}", file=sys.stderr)
    return 1


def await_spawn(args: argparse.Namespace) -> int:
    payload: dict[str, object] = {
        "type": "await_spawn",
        "timeout": args.timeout,
    }
    if args.request_id:
        payload["spawn_request_id"] = args.request_id
    if args.stream_id:
        payload["stream_id"] = args.stream_id
    try:
        response = asyncio.run(
            await_spawn_once(
                load_config(),
                payload,
                timeout=max(30.0, float(args.timeout) + 5.0),
            )
        )
    except Exception as exc:
        response, exit_code, message = _direct_rpc_transport_error(
            "await_spawn",
            payload.get("request_id") if isinstance(payload.get("request_id"), str) else None,
            exc,
        )
        _print_response(response)
        print(f"agent-orch await-spawn: {message}", file=sys.stderr)
        return exit_code
    _print_response(response)
    return 0 if response.get("ok") else 1


def list_sessions(args: argparse.Namespace) -> int:
    try:
        timeout = getattr(args, "timeout", None)
        snapshot = fetch_snapshot(
            load_config(),
            timeout=float(timeout) if timeout is not None else None,
            events_mode="summary",
        )
    except Exception as exc:
        response, exit_code, message = _direct_rpc_transport_error("snapshot", None, exc)
        _print_response(response)
        print(f"agent-orch list: {message}", file=sys.stderr)
        return exit_code
    sessions = snapshot.get("sessions", [])
    print(json.dumps(sessions if isinstance(sessions, list) else [], separators=(",", ":")))
    return 0


def models(args: argparse.Namespace) -> int:
    try:
        response = asyncio.run(
            spawn_catalog_get_once(
                load_config(),
                timeout=float(getattr(args, "timeout", 30.0) or 30.0),
            )
        )
    except Exception as exc:
        print(f"agent-orch models: chat_streamd unavailable: {exc}", file=sys.stderr)
        return 64
    _print_response(response)
    return 0 if response.get("type") == "spawn_catalog_get.ok" else 1


def visibility_set(args: argparse.Namespace) -> int:
    try:
        response = asyncio.run(
            set_visibility_once(
                load_config(),
                args.stream_id,
                args.value,
                timeout=float(getattr(args, "timeout", 30.0) or 30.0),
            )
        )
    except ValueError as exc:
        print(f"agent-orch visibility set: validation failed: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        response, exit_code, message = _direct_rpc_transport_error("set_visibility", None, exc)
        _print_response(response)
        print(f"agent-orch visibility set: {message}", file=sys.stderr)
        return exit_code
    print(json.dumps(response, separators=(",", ":")))
    return 0 if response.get("type") == "set_visibility.ok" else 1


def spec_update(args: argparse.Namespace) -> int:
    config = load_config()
    caller_stream_id = getattr(args, "from_stream_id", None) or discover_leader_stream_id_short(config)
    try:
        response = asyncio.run(
            spec_update_once(
                config,
                args.stream_id,
                args.spec_command,
                args.spec_id,
                timeout=float(getattr(args, "timeout", 30.0) or 30.0),
                from_stream_id=caller_stream_id,
            )
        )
    except ValueError as exc:
        print(f"agent-orch spec {args.spec_command}: validation failed: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        response, exit_code, message = _direct_rpc_transport_error("session.spec_update", None, exc)
        _print_response(response)
        print(f"agent-orch spec {args.spec_command}: {message}", file=sys.stderr)
        return exit_code
    print(json.dumps(response, separators=(",", ":")))
    return 0 if response.get("type") == "session.spec_update.ok" else 1


def _truncate_inspect_text(text: str, max_text: int | None) -> str:
    if max_text is None or len(text) <= max_text:
        return text
    if max_text <= 3:
        return "." * max_text
    return text[: max_text - 3] + "..."


def _print_inspect_pretty(response: dict[str, object], *, max_text: int | None = 160) -> None:
    inspect = response.get("inspect") if isinstance(response.get("inspect"), dict) else response
    if not isinstance(inspect, dict):
        print(json.dumps(response, separators=(",", ":")))
        return
    print(f"stream_id: {inspect.get('stream_id')}")
    session = inspect.get("session") if isinstance(inspect.get("session"), dict) else {}
    print("session:")
    for key in (
        "status",
        "online",
        "role",
        "role_source",
        "opened_at",
        "closed_at",
        "close_kind",
        "self_close_on_completion",
        "requested_model",
        "requested_effort",
        "effective_model",
        "effective_effort",
        "routing_integrity",
        "routing_integrity_reason",
        "routing_integrity_updated_at",
        "bootstrap_state",
    ):
        print(f"  {key}: {session.get(key)}")
    attribution = inspect.get("attribution") if isinstance(inspect.get("attribution"), dict) else {}
    if attribution.get("suspected_cause"):
        print("Attribution:")
        for key in (
            "suspected_cause",
            "correlated_audit_id",
            "correlated_request_id",
            "actor_stream_id",
            "actor_trusted",
            "death_audit_id",
            "last_seen_pane_pid",
        ):
            if key in attribution:
                print(f"  {key}: {attribution.get(key)}")
    close_audit = inspect.get("close_audit") if isinstance(inspect.get("close_audit"), dict) else {}
    if close_audit:
        print("close_audit:")
        for key in (
            "disposition",
            "close_kind",
            "actor_kind",
            "closed_by",
            "auth_kind",
            "reason",
            "request_id",
            "created_at",
        ):
            if key in close_audit:
                print(f"  {key}: {close_audit.get(key)}")
    deferred = inspect.get("deferred_reap")
    if isinstance(deferred, dict):
        print("deferred_reap:")
        for key in ("host", "requested_at", "attempts", "last_error", "done_at", "exhausted_at"):
            print(f"  {key}: {deferred.get(key)}")
    events = inspect.get("recent_events") if isinstance(inspect.get("recent_events"), list) else []
    print(f"recent_events: {len(events)}")
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("kind") or event.get("type") or "event"
        text = str(event.get("text") or event.get("content") or "")
        text = _truncate_inspect_text(text, max_text)
        print(f"  - {kind}: {text}")
    report = inspect.get("existing_report")
    if isinstance(report, dict):
        print("existing_report:")
        for key in (
            "report_id",
            "ledger_row_id",
            "from_stream_id",
            "recovery_for_stream_id",
            "msg_id",
            "status",
            "synthesis_kind",
            "degraded",
            "degradation_reason",
            "missing_fields",
            "summary",
            "reason",
        ):
            if key in report:
                print(f"  {key}: {report.get(key)}")
    else:
        print("existing_report: null")
    send_frames = inspect.get("send_frames") if isinstance(inspect.get("send_frames"), list) else []
    print(f"send_frames: {len(send_frames)}")
    for frame in send_frames:
        if not isinstance(frame, dict):
            continue
        frame_type = frame.get("type") or frame.get("kind") or "send.frame"
        delivery = frame.get("delivery")
        reason = frame.get("reason")
        state = frame.get("state")
        attempt = frame.get("attempt") or frame.get("attempts")
        parts = [str(frame_type)]
        for label, value in (("delivery", delivery), ("reason", reason), ("state", state), ("attempt", attempt)):
            if value is not None:
                parts.append(f"{label}={value}")
        print(f"  - {' '.join(parts)}")


def thread(args: argparse.Namespace) -> int:
    from .wsclient import _one_shot_rpc
    token = stream_token_from_env()
    if not token:
        print("agent-orch thread: unauthorized (verified seat token required)", file=sys.stderr)
        return 2
    payload = {"type": "thread.read", "request_id": f"thread-{uuid.uuid4()}",
               "child_stream_id": args.child, "stream_token": token}
    if args.limit is not None:
        payload["limit"] = args.limit
    if args.cursor is not None:
        payload["cursor"] = args.cursor
    try:
        response = asyncio.run(_one_shot_rpc(load_config(), payload, prefix="thread", timeout=args.timeout))
    except Exception as exc:
        response, code, _ = _direct_rpc_transport_error("thread.read", payload["request_id"], exc)
        _print_response(response)
        return code
    _print_response(response)
    return 0 if response.get("type") == "thread.read.ok" else 1


def inspect(args: argparse.Namespace) -> int:
    if not _require_stream_id("inspect", args.stream_id):
        return 2
    try:
        config = load_config()
        response = asyncio.run(
            inspect_stream_once(
                config,
                args.stream_id,
                msg_id=args.msg_id,
                event_tail=args.event_tail,
                timeout=float(getattr(args, "timeout", 30.0) or 30.0),
            )
        )
    except Exception as exc:
        response, exit_code, message = _direct_rpc_transport_error("inspect_stream", None, exc)
        _print_response(response)
        print(f"agent-orch inspect: {message}", file=sys.stderr)
        return exit_code
    if args.json:
        raw = response.get("inspect") if isinstance(response.get("inspect"), dict) else response
        print(json.dumps(raw, separators=(",", ":")))
    elif response.get("type") == "inspect_stream.ok" or response.get("ok"):
        max_text = None if getattr(args, "full", False) else getattr(args, "max_text", None) or 160
        _print_inspect_pretty(response, max_text=max_text)
    else:
        print(json.dumps(response, separators=(",", ":")), file=sys.stderr)
    return 0 if response.get("type") == "inspect_stream.ok" or response.get("ok") else 1


def ledger_get(args: argparse.Namespace) -> int:
    try:
        response = asyncio.run(
            ledger_get_once(
                load_config(),
                args.tell_id,
                timeout=float(getattr(args, "timeout", 30.0) or 30.0),
            )
        )
    except Exception as exc:
        response, exit_code, message = _direct_rpc_transport_error("ledger_get", None, exc)
        _print_response(response)
        print(f"agent-orch ledger get: {message}", file=sys.stderr)
        return exit_code
    _print_response(response)
    return 0 if response.get("type") == "ledger_get.ok" else 1


def inbound_audit(args: argparse.Namespace) -> int:
    try:
        config = load_config()
        stream_id = args.stream_id or discover_leader_stream_id_short(config)
        if not stream_id:
            print("agent-orch audit inbound: validation failed: stream_id_unknown", file=sys.stderr)
            return 2
        response = asyncio.run(
            inbound_audit_once(
                config,
                stream_id,
                limit=int(args.limit),
                timeout=float(getattr(args, "timeout", 30.0) or 30.0),
            )
        )
    except Exception as exc:
        response, exit_code, message = _direct_rpc_transport_error("inbound_audit", None, exc)
        _print_response(response)
        print(f"agent-orch audit inbound: {message}", file=sys.stderr)
        return exit_code
    _print_response(response)
    return 0 if response.get("type") == "inbound_audit.ok" else 1


# The env names propagated by `agent-orch ssh`. Documented invariant:
# any addition requires a spec (see spec_pentacle_agent_orch_leader_auto_bind_2026_05_18).
SSH_SEND_ENV_NAMES = (
    "PENTACLE_STREAM_ID",
    "AGENT_ORCH_STREAM_ID",
    "AGENT_ORCH_STREAM_TOKEN_FILE",
    "AGENT_ORCH_STREAM_TOKEN",
)


def ssh_passthrough(ssh_args: list[str]) -> int:
    """Transparently exec ssh(1) with Pentacle stream env SendEnv hints
    injected immediately after the ssh argv, then all user-supplied tokens verbatim.

    Uses os.execvp so signals, exit codes, and tty handling are identical to invoking
    ssh directly. Does not return on success; on OSError (ssh not on PATH) the caller
    surfaces the error like any other CLI failure.
    """
    send_env = "SendEnv=" + " ".join(SSH_SEND_ENV_NAMES)
    argv = ["ssh", "-o", send_env, *ssh_args]
    os.execvp("ssh", argv)
    # execvp does not return on success; this line is unreachable in practice.
    return 1


def watch_wake(args: argparse.Namespace) -> int:
    kind = args.watch_wake_kind
    target = args.watch_wake_target
    verb = target if target in {"list", "cancel"} else "register"
    payload: dict[str, object] = {"type": f"{kind}.{verb}",
        "request_id": args.request_id or str(uuid.uuid4())}
    if verb == "cancel":
        if not args.watch_wake_id:
            print(f"agent-orch {kind} cancel: id required", file=sys.stderr)
            return 2
        payload["id"] = args.watch_wake_id
    elif verb == "register":
        if kind == "wake":
            payload.update({"in": args.wake_in, "at": args.wake_at,
                            "note": args.note, "urgent": args.urgent})
        else:
            payload.update(child_stream_id=target, on=args.on, repeat=args.repeat)
    return _coordination_request(args, payload)


def build_parser() -> argparse.ArgumentParser:
    parser = AgentOrchArgumentParser(prog="agent-orch")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start", help="Deprecated no-op; verbs connect direct to chat_streamd.")
    start_parser.add_argument("--workspace", help=argparse.SUPPRESS)
    start_parser.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
    start_parser.add_argument("--as", dest="as_override", help=argparse.SUPPRESS)
    start_parser.set_defaults(func=start)

    stop_parser = subparsers.add_parser("stop", help="Deprecated no-op; no local wrapper is running.")
    stop_parser.add_argument("--workspace", help=argparse.SUPPRESS)
    stop_parser.add_argument("--kill-children", action="store_true", help=argparse.SUPPRESS)
    stop_parser.set_defaults(func=stop)

    for kind in ("wake", "watch"):
        timer_parser = subparsers.add_parser(kind)
        timer_parser.add_argument("watch_wake_target", nargs="?", default="register",
                                  help="child stream id (watch), list, or cancel")
        timer_parser.add_argument("watch_wake_id", nargs="?")
        timer_parser.add_argument("--request-id")
        timer_parser.add_argument("--timeout", type=float, default=30.0)
        timer_parser.set_defaults(func=watch_wake, watch_wake_kind=kind)
        if kind == "wake":
            times = timer_parser.add_mutually_exclusive_group()
            times.add_argument("--in", dest="wake_in")
            times.add_argument("--at", dest="wake_at")
            timer_parser.add_argument("--note", default="")
            timer_parser.add_argument("--urgent", action="store_true")
        else:
            timer_parser.add_argument("--on")
            timer_parser.add_argument("--repeat", action="store_true")

    spawn_parser = subparsers.add_parser("spawn")
    spawn_parser.add_argument("--no-watch", action="store_true", help="Skip the default parent watch")
    spawn_parser.add_argument("--provider", help="Required except for --handoff, which inherits the retiring stream provider.")
    spawn_parser.add_argument(
        "--model",
        help="Per-spawn model override. Supports claude aliases and codex model ids.",
    )
    spawn_parser.add_argument("--effort", help="Per-spawn reasoning effort for Claude or Codex; defaults are resolved by the agent_orch profile.")
    spawn_parser.add_argument("--host")
    spawn_parser.add_argument("--role")
    spawn_parser.add_argument("--phase")
    spawn_parser.add_argument(
        "--spec-id",
        type=_spec_id_arg,
        action="append",
        help="Work-folder spec id to tag the spawned stream; repeat for multiple tags.",
    )
    thread_parser = subparsers.add_parser("thread")
    thread_parser.add_argument("child")
    thread_parser.add_argument("--limit", type=int)
    thread_parser.add_argument("--cursor")
    thread_parser.add_argument("--timeout", type=float, default=30.0)
    thread_parser.set_defaults(func=thread)
    spawn_parser.add_argument("--objective", help="Immutable one-line objective, at most 120 code points; required for parented child spawns, optional otherwise (top-level/--top-level/--handoff derive one)")
    spawn_parser.add_argument("--visibility", choices=["default", "nested", "hidden"])
    spawn_parser.add_argument("--parent")
    spawn_parser.add_argument("--handoff", action="store_true")
    spawn_parser.add_argument(
        "--confirm-model-change",
        dest="confirm_model_change",
        action="store_true",
        help="Suppress the compatibility warning for a changed handoff tuple; never required for execution.",
    )
    spawn_parser.add_argument("--disposition-waived", dest="disposition_waived_reason", help="Waive handoff in_progress spec disposition gate with a required reason.")
    spawn_parser.add_argument(
        "--resume",
        metavar="SESSION_ID",
        help=(
            "Relaunch a dead/closed claude session by its session_id via "
            "`claude --resume`, reusing the original stream_id so the SAME "
            "dashboard row reopens and the existing transcript is continued. "
            "Requires --provider claude; mutually exclusive with --handoff/--parent."
        ),
    )
    spawn_parser.add_argument(
        "--top-level",
        action="store_true",
        help=(
            "Suppress parent auto-inference for an explicit top-level spawn. "
            "Useful with --resume from inside a registered session; incompatible with --handoff/--parent."
        ),
    )
    schedule_time_group = spawn_parser.add_mutually_exclusive_group()
    schedule_time_group.add_argument("--at", help="Schedule handoff for an ISO 8601 timestamp with explicit offset.")
    schedule_time_group.add_argument("--delay", help="Schedule handoff after a duration like 3h, 45m, 2h30m, or integer seconds.")
    spawn_parser.add_argument("--allow-past-time", action="store_true")
    spawn_parser.add_argument("--allow-far-future", action="store_true")
    spawn_parser.add_argument(
        "--no-reparent-children",
        dest="reparent_children",
        action="store_false",
        default=None,
        help=(
            "On --handoff, do NOT auto-re-parent the retiring leader's same-host "
            "direct children to the successor. By default a handoff moves them so "
            "they are not left orphaned on the retiring leader. Cross-host children "
            "are never auto-re-parented in v1 regardless of this flag."
        ),
    )
    spawn_parser.add_argument(
        "--self-close-on-completion",
        dest="self_close_on_completion",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Authorize the spawned worker to self-close via `agent-orch report --terminate` "
            "(default: disabled). When enabled, a worker whose leader is still live may self-close "
            "after its terminal report instead of being refused with terminate_requires_leader_close — "
            "pass this flag deliberately when that lifecycle is wanted. "
            "Pass --no-self-close-on-completion to keep the worker open for follow-up sends. "
            "Only applies to spawns with --parent or --handoff lineage."
        ),
    )
    initial_prompt_group = spawn_parser.add_mutually_exclusive_group()
    initial_prompt_group.add_argument("--initial-prompt")
    initial_prompt_group.add_argument("--initial-prompt-file")
    spawn_parser.add_argument("--timeout", type=float)
    spawn_parser.add_argument("--request-id")
    spawn_parser.add_argument(
        "--idempotency-key",
        help=(
            "Stable dedup key for this logical spawn. Default: sha256 of the "
            "spawn inputs, printed as `spawn key: <key>` on stderr before the "
            "RPC. A retry with the same key returns the existing seat instead "
            "of a duplicate; use it with `spawn cancel`/`spawn status`."
        ),
    )
    spawn_parser.set_defaults(func=spawn)

    # `spawn cancel`/`spawn status` are surfaced as these sibling verbs; a
    # pre-argparse shim in main() rewrites the `spawn <sub>` form to them.
    spawn_cancel_parser = subparsers.add_parser(
        "spawn-cancel",
        description="Cancel an in-flight spawn by idempotency key or request id before it binds.",
    )
    spawn_cancel_parser.add_argument("target", help="idempotency key or request id")
    spawn_cancel_parser.add_argument("--host")
    spawn_cancel_parser.add_argument("--timeout", type=float, default=30.0)
    spawn_cancel_parser.set_defaults(func=spawn_cancel)

    spawn_status_parser = subparsers.add_parser(
        "spawn-status",
        description="Show the outcome/reservation rows and any admission hold for a spawn key or request id.",
    )
    spawn_status_parser.add_argument("target", help="idempotency key or request id")
    spawn_status_parser.add_argument("--host")
    spawn_status_parser.add_argument("--timeout", type=float, default=30.0)
    spawn_status_parser.set_defaults(func=spawn_status)

    spawn_freeze_parser = subparsers.add_parser(
        "spawn-freeze",
        description="Freeze daemon spawn admission for a deploy window (refuses new spawns with spawn_frozen).",
    )
    spawn_freeze_parser.add_argument("--host")
    spawn_freeze_parser.add_argument("--reason", default="")
    spawn_freeze_parser.add_argument("--ttl", type=float, default=900.0, help="hold TTL seconds; self-expires.")
    spawn_freeze_parser.add_argument("--timeout", type=float, default=30.0)
    spawn_freeze_parser.set_defaults(func=spawn_freeze)

    spawn_unfreeze_parser = subparsers.add_parser(
        "spawn-unfreeze",
        description="Clear a daemon spawn admission freeze.",
    )
    spawn_unfreeze_parser.add_argument("--host")
    spawn_unfreeze_parser.add_argument("--timeout", type=float, default=30.0)
    spawn_unfreeze_parser.set_defaults(func=spawn_unfreeze)

    schedule_parser = subparsers.add_parser("schedule")
    schedule_sub = schedule_parser.add_subparsers(dest="schedule_command", required=True)
    schedule_list_parser = schedule_sub.add_parser("list")
    schedule_list_parser.add_argument("--json", action="store_true")
    schedule_list_parser.add_argument("--state", choices=list(SCHEDULE_STATES))
    schedule_list_parser.add_argument("--from", dest="from_stream_id")
    schedule_list_parser.add_argument("--timeout", type=float, default=30.0)
    schedule_list_parser.set_defaults(func=schedule_list)

    schedule_get_parser = schedule_sub.add_parser("get")
    schedule_get_parser.add_argument("schedule_id")
    schedule_get_parser.add_argument("--json", action="store_true")
    schedule_get_parser.add_argument("--from", dest="from_stream_id")
    schedule_get_parser.add_argument("--timeout", type=float, default=30.0)
    schedule_get_parser.set_defaults(func=schedule_get)

    schedule_cancel_parser = schedule_sub.add_parser("cancel")
    schedule_cancel_parser.add_argument("schedule_id")
    schedule_cancel_parser.add_argument("--request-id")
    schedule_cancel_parser.add_argument("--from", dest="from_stream_id")
    schedule_cancel_parser.add_argument("--timeout", type=float, default=30.0)
    schedule_cancel_parser.set_defaults(func=schedule_cancel)

    schedule_reschedule_parser = schedule_sub.add_parser("reschedule")
    schedule_reschedule_parser.add_argument("schedule_id")
    schedule_reschedule_time = schedule_reschedule_parser.add_mutually_exclusive_group(required=True)
    schedule_reschedule_time.add_argument("--at")
    schedule_reschedule_time.add_argument("--delay")
    schedule_reschedule_parser.add_argument("--allow-past-time", action="store_true")
    schedule_reschedule_parser.add_argument("--allow-far-future", action="store_true")
    schedule_reschedule_parser.add_argument("--request-id")
    schedule_reschedule_parser.add_argument("--from", dest="from_stream_id")
    schedule_reschedule_parser.add_argument("--timeout", type=float, default=30.0)
    schedule_reschedule_parser.set_defaults(func=schedule_reschedule)

    schedule_run_parser = schedule_sub.add_parser("run")
    schedule_run_parser.add_argument("schedule_id")
    schedule_run_parser.add_argument("--request-id")
    schedule_run_parser.add_argument("--from", dest="from_stream_id")
    schedule_run_parser.add_argument("--timeout", type=float, default=30.0)
    schedule_run_parser.set_defaults(func=schedule_run)
    schedule_receipt_parser = schedule_sub.add_parser("receipt")
    schedule_receipt_parser.add_argument("operation_request_id")
    schedule_receipt_parser.add_argument("--phase", required=True)
    schedule_receipt_parser.add_argument("--from", dest="from_stream_id")
    schedule_receipt_parser.add_argument("--timeout", type=float, default=30.0)
    schedule_receipt_parser.set_defaults(func=schedule_receipt)

    send_parser = subparsers.add_parser(
        "send",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Deliver a prompt to a sub-agent. Response shapes by retry class:\n"
            "  ok:true, delivery=landed              chat_streamd confirmed the prompt landed.\n"
            "  ok:false, delivery=not_landed, queued_for_redelivery=true\n"
            "                                        target pane was not ready or readback was unconfirmed;\n"
            "                                        queued for redelivery, do not resend; await the report.\n"
            "  ok:false, delivery=not_landed, queued_for_redelivery=false\n"
            "                                        prompt did not land and could not be queued; re-evaluate.\n"
            "  ok:false, error=transport_failed      local transport failed before chat_streamd accepted the send; retry-immediate is safe.\n"
            "  ok:false, error=msg_id_replay_conflict the daemon already accepted this target/msg_id with different payload.\n"
            "  ok:false, error=peer_session_closed   (or host_offline, host_unknown, peer_session_unknown, inbox_invalid) the\n"
            "                                        target is gone or the inputs are wrong; do not retry, re-evaluate.\n"
            "  ok:false, error=send_error            residual catch-all for chat_streamd codes not yet stable; inspect for the\n"
            "                                        underlying error_code before deciding.\n"
            "\n"
            "Note: error=transmit_failed is the prior name for transport_failed; it remains accepted as an alias on input\n"
            "but new code emits transport_failed.\n"
            "\n"
            "During daemon retries, progress is printed to stderr unless --quiet is set.\n"
            "Only the terminal delivery result controls the exit code.\n"
            "Exit codes: 0 landed, 1 send error, 64 unreachable, 65 connection dropped, 66 auth failed,\n"
            "67 timeout, 75 accepted but deferred/queued, 76 not delivered and not queued."
        ),
    )
    send_parser.add_argument(
        "--retry",
        action="store_true",
        help=(
            "Retry the same msg_id after a previous not_landed or transport failure."
        ),
    )
    send_parser.add_argument(
        "--inline-inbox",
        dest="inline_inbox",
        action="store_true",
        help="Deprecated no-op; send always carries validated INBOX_V1 inline.",
    )
    send_parser.add_argument("--quiet", action="store_true", help="Suppress retry progress lines on stderr.")
    send_parser.add_argument("--timeout", type=float, default=30.0)
    send_parser.add_argument("stream_id")
    send_parser.add_argument("msg_id", type=int)
    send_parser.add_argument("prompt_text")
    send_parser.set_defaults(func=send)

    send_receipt_parser = subparsers.add_parser(
        "send-receipt", help="read one durable latest receipt for a sent request",
    )
    send_receipt_parser.add_argument("--timeout", type=float, default=30.0)
    send_receipt_parser.add_argument("stream_id")
    send_receipt_parser.add_argument("request_id")
    send_receipt_parser.set_defaults(func=send_receipt)

    send_cancel_parser = subparsers.add_parser(
        "send-cancel",
        description="Ask chat_streamd to cancel an in-flight send retry loop for a msg_id.",
    )
    send_cancel_parser.add_argument("stream_id")
    send_cancel_parser.add_argument("msg_id", type=int)
    send_cancel_parser.add_argument("--timeout", type=float, default=30.0)
    send_cancel_parser.set_defaults(func=send_cancel)

    tell_parser = subparsers.add_parser("tell")
    tell_parser.add_argument("peer_stream_id")
    tell_parser.add_argument("text")
    tell_parser.add_argument("--from", dest="from_stream_id")
    tell_parser.add_argument("--ttl", type=_ttl_arg, default=300)
    tell_parser.add_argument("--tell-id")
    tell_parser.add_argument(
        "--urgent",
        action="store_true",
        help="reserve the safety order, issue one guarded interrupt, then submit it",
    )
    tell_parser.add_argument(
        "--sanitize",
        action="store_true",
        help="strip ANSI/ESC control sequences from the message instead of "
             "rejecting it — for relaying captured terminal output",
    )
    tell_parser.add_argument("--timeout", type=float, default=30.0)
    tell_parser.set_defaults(func=tell)

    park_parser = subparsers.add_parser("park")
    park_parser.add_argument("stream_id")
    park_parser.add_argument("--reason", required=True)
    park_parser.add_argument("--from", dest="from_stream_id")
    park_parser.add_argument("--timeout", type=float, default=30.0)
    park_parser.set_defaults(func=park)

    unpark_parser = subparsers.add_parser("unpark")
    unpark_parser.add_argument("stream_id")
    unpark_parser.add_argument("--event-id")
    unpark_parser.add_argument("--reason")
    unpark_parser.add_argument("--from", dest="from_stream_id")
    unpark_parser.add_argument("--timeout", type=float, default=30.0)
    unpark_parser.set_defaults(func=unpark)

    hold_parser = subparsers.add_parser("hold")
    hold_sub = hold_parser.add_subparsers(dest="hold_command", required=True)
    hold_acquire = hold_sub.add_parser("acquire")
    hold_acquire.add_argument("resource")
    hold_acquire.add_argument("--reason", required=True)
    hold_acquire.add_argument("--ttl", type=int)
    hold_acquire.add_argument("--from", dest="from_stream_id")
    hold_acquire.add_argument("--timeout", type=float, default=30.0)
    hold_acquire.set_defaults(func=hold)
    hold_release = hold_sub.add_parser("release")
    hold_release.add_argument("resource", metavar="RESOURCE_OR_HOLD_ID")
    hold_release.add_argument("--force", action="store_true")
    hold_release.add_argument("--from", dest="from_stream_id")
    hold_release.add_argument("--timeout", type=float, default=30.0)
    hold_release.set_defaults(func=hold)
    hold_list = hold_sub.add_parser("list")
    hold_list.add_argument("--resource")
    hold_list.add_argument("--json", action="store_true")
    hold_list.add_argument("--from", dest="from_stream_id")
    hold_list.add_argument("--timeout", type=float, default=30.0)
    hold_list.set_defaults(func=hold)

    oblige_parser = subparsers.add_parser("oblige")
    oblige_parser.add_argument("stream")
    oblige_parser.add_argument("text")
    oblige_parser.add_argument("--spec-id", type=_spec_id_arg)
    oblige_parser.add_argument("--expires-in", dest="expires_in", type=int, help="obligation deadline in seconds; unfulfilled expiry sets the target's spec-issue flag")
    oblige_parser.add_argument("--from", dest="from_stream_id")
    oblige_parser.add_argument("--timeout", type=float, default=30.0)
    oblige_parser.set_defaults(func=oblige)

    obligation_parser = subparsers.add_parser("obligation")
    obligation_sub = obligation_parser.add_subparsers(dest="obligation_command", required=True)
    obligation_list = obligation_sub.add_parser("list")
    obligation_list.add_argument("--stream")
    obligation_list.add_argument("--status", choices=["open", "discharged", "waived"])
    obligation_list.add_argument("--json", action="store_true")
    obligation_list.add_argument("--from", dest="from_stream_id")
    obligation_list.add_argument("--timeout", type=float, default=30.0)
    obligation_list.set_defaults(func=obligation)
    obligation_waive = obligation_sub.add_parser("waive")
    obligation_waive.add_argument("obligation_id")
    obligation_waive.add_argument("--reason", required=True)
    obligation_waive.add_argument("--from", dest="from_stream_id")
    obligation_waive.add_argument("--timeout", type=float, default=30.0)
    obligation_waive.set_defaults(func=obligation)

    spec_issue_parser = subparsers.add_parser("spec-issue")
    spec_issue_sub = spec_issue_parser.add_subparsers(dest="spec_issue_command", required=True)
    spec_issue_list = spec_issue_sub.add_parser("list")
    spec_issue_list.add_argument("--stream")
    spec_issue_list.add_argument("--json", action="store_true")
    spec_issue_list.add_argument("--from", dest="from_stream_id")
    spec_issue_list.add_argument("--timeout", type=float, default=30.0)
    spec_issue_list.set_defaults(func=spec_issue)
    spec_issue_clear = spec_issue_sub.add_parser("clear")
    spec_issue_clear.add_argument("--stream", required=True)
    spec_issue_clear.add_argument("--spec-id", required=True, type=_spec_id_arg)
    spec_issue_clear.add_argument("--from", dest="from_stream_id")
    spec_issue_clear.add_argument("--timeout", type=float, default=30.0)
    spec_issue_clear.set_defaults(func=spec_issue)

    notify_parser = subparsers.add_parser("notify")
    notify_mode = notify_parser.add_mutually_exclusive_group(required=True)
    notify_mode.add_argument("--message")
    notify_mode.add_argument("--ask")
    notify_mode.add_argument("--resolve-dedup-key", dest="resolve_dedup_key")
    notify_parser.add_argument("--title")
    notify_parser.add_argument("--producer")
    notify_parser.add_argument(
        "--severity", default="info", choices=("info", "warning", "critical")
    )
    notify_parser.add_argument("--ttl", type=int)
    notify_parser.add_argument("--dedup-key")
    notify_parser.add_argument("--button", action="append", default=[])
    notify_parser.add_argument("--actions")
    notify_parser.add_argument("--await-answer", action="store_true", dest="await_answer")
    notify_parser.add_argument("--timeout", type=float, default=30.0)
    notify_parser.add_argument("--from", dest="from_stream_id")
    notify_parser.set_defaults(func=notify)

    investigation_parser = subparsers.add_parser("investigation")
    investigation_sub = investigation_parser.add_subparsers(dest="investigation_command", required=True)
    investigation_decide = investigation_sub.add_parser("decide")
    investigation_decide.add_argument("investigation_id")
    investigation_decide.add_argument(
        "--decision",
        required=True,
        choices=("false_positive", "real_issue", "needs_more_information"),
    )
    investigation_decide.add_argument("--rationale")
    investigation_decide.add_argument("--proposed-fix", dest="proposed_fix")
    investigation_decide.add_argument("--affected-subsystem", dest="affected_subsystem")
    investigation_decide.add_argument("--question-id")
    investigation_decide.add_argument("--timeout", type=float, default=30.0)
    investigation_decide.set_defaults(func=investigation)
    investigation_list = investigation_sub.add_parser("list")
    investigation_list.add_argument("--status", action="append")
    investigation_list.add_argument("--timeout", type=float, default=30.0)
    investigation_list.set_defaults(func=investigation)
    investigation_drain = investigation_sub.add_parser("drain")
    investigation_drain.add_argument("--timeout", type=float, default=30.0)
    investigation_drain.set_defaults(func=investigation)
    investigation_split = investigation_sub.add_parser("split")
    investigation_split.add_argument("investigation_id")
    investigation_split.add_argument("--event-id", required=True)
    investigation_split.add_argument("--timeout", type=float, default=30.0)
    investigation_split.set_defaults(func=investigation)

    role_parser = subparsers.add_parser("role")
    role_sub = role_parser.add_subparsers(dest="role_command", required=True)
    role_set_parser = role_sub.add_parser("set")
    role_set_parser.add_argument("stream_id")
    role_set_parser.add_argument("role")
    role_set_parser.add_argument("--timeout", type=float, default=30.0)
    role_set_parser.set_defaults(func=role_set)
    role_get_parser = role_sub.add_parser("get")
    role_get_parser.add_argument("stream_id")
    role_get_parser.add_argument("--timeout", type=float, default=30.0)
    role_get_parser.set_defaults(func=role_get)

    nexus_parser = subparsers.add_parser("nexus")
    nexus_sub = nexus_parser.add_subparsers(dest="nexus_command", required=True)
    nexus_list = nexus_sub.add_parser("list")
    nexus_list.add_argument("--all", action="store_true")
    nexus_list.add_argument("--scope")
    nexus_list.add_argument("--timeout", type=float, default=30.0)
    nexus_list.set_defaults(func=nexus)
    nexus_context = nexus_sub.add_parser("context")
    nexus_context.add_argument("--since", type=int)
    nexus_context.add_argument("--timeout", type=float, default=30.0)
    nexus_context.set_defaults(func=nexus)
    nexus_inspect = nexus_sub.add_parser("inspect")
    nexus_inspect.add_argument("identifier")
    nexus_inspect.add_argument("--timeout", type=float, default=30.0)
    nexus_inspect.set_defaults(func=nexus)
    nexus_route = nexus_sub.add_parser("route")
    nexus_route.add_argument("domain_id")
    nexus_route.add_argument("--kind", choices=("decision", "issue", "idea", "task", "alert"), required=True)
    nexus_route.add_argument("--message", required=True)
    nexus_route.add_argument("--route-id")
    nexus_route.add_argument("--timeout", type=float, default=30.0)
    nexus_route.set_defaults(func=nexus)

    prompt_parser = subparsers.add_parser("prompt")
    prompt_sub = prompt_parser.add_subparsers(dest="prompt_command", required=True)
    prompt_ask_parser = prompt_sub.add_parser("ask")
    prompt_ask_parser.add_argument("--title", required=True)
    prompt_ask_parser.add_argument("--body", required=True)
    prompt_ask_parser.add_argument("--context")
    prompt_ask_parser.add_argument("--question-id")
    prompt_ask_parser.add_argument("--spec-id", type=_spec_id_arg)
    prompt_ask_parser.add_argument("--from", dest="from_stream_id")
    prompt_ask_parser.add_argument("--provider")
    prompt_ask_parser.add_argument("--dedup-key")
    prompt_ask_parser.add_argument("--ttl", type=int)
    prompt_ask_parser.add_argument(
        "--allow-custom",
        action="store_true",
        help="Allow a custom free-text answer alongside predefined choice options.",
    )
    prompt_ask_parser.add_argument("--await-answer", action="store_true", dest="await_answer")
    prompt_ask_parser.add_argument("--timeout", type=float, default=30.0)
    prompt_ask_parser.add_argument(
        "--response-mode",
        "--mode",
        dest="response_mode",
        default="single_choice",
        choices=["single_choice", "multi_choice", "free_text"],
    )
    prompt_ask_parser.add_argument(
        "--option",
        action=_PromptOptionAction,
        dest="prompt_options",
        default=[],
        help="Choice option as LABEL or LABEL=VALUE (1-5). Required for choice modes; invalid for free_text.",
    )
    prompt_ask_parser.add_argument(
        "--option-json",
        action=_PromptOptionAction,
        dest="prompt_options",
        help='Choice option JSON: {"label":"Label","value":"value","description":"Optional detail"}.',
    )
    prompt_ask_parser.set_defaults(func=prompt_ask)

    prompt_status_parser = prompt_sub.add_parser("status")
    prompt_status_parser.add_argument("question_id")
    prompt_status_parser.add_argument("--timeout", type=float, default=30.0)
    prompt_status_parser.set_defaults(func=prompt_status)

    prompt_answer_parser = prompt_sub.add_parser("answer")
    prompt_answer_parser.add_argument("question_id")
    prompt_answer_parser.add_argument(
        "--text", help="Free-text answer (free_text questions, or custom text on a choice).")
    prompt_answer_parser.add_argument(
        "--selection", "--select", action="append",
        help="Selected option value (repeat for multi_choice). Combine with --text if desired.")
    prompt_answer_parser.add_argument(
        "--by", help="Compatibility claim only; the daemon derives actor provenance."
    )
    prompt_answer_parser.add_argument("--timeout", type=float, default=30.0)
    prompt_answer_parser.set_defaults(func=prompt_answer)

    prompt_cancel_parser = prompt_sub.add_parser("cancel")
    prompt_cancel_parser.add_argument("question_id")
    prompt_cancel_parser.add_argument("--note")
    prompt_cancel_parser.add_argument(
        "--by", help="Compatibility claim only; the daemon derives actor provenance."
    )
    prompt_cancel_parser.add_argument("--timeout", type=float, default=30.0)
    prompt_cancel_parser.set_defaults(func=prompt_cancel)

    prompt_list_parser = prompt_sub.add_parser("list")
    prompt_list_parser.add_argument("--from", dest="from_stream_id")
    prompt_list_parser.add_argument("--spec-id", type=_spec_id_arg)
    prompt_list_parser.add_argument("--open", action="store_true")
    prompt_list_parser.add_argument("--limit", type=int)
    prompt_list_parser.add_argument("--timeout", type=float, default=30.0)
    prompt_list_parser.set_defaults(func=prompt_list)

    triage_parser = subparsers.add_parser("triage")
    triage_sub = triage_parser.add_subparsers(dest="triage_command", required=True)
    triage_scan_parser = triage_sub.add_parser("scan-publish")
    triage_scan_parser.add_argument("--memory-root", required=True)
    triage_scan_parser.add_argument("--limit", type=int, default=20)
    triage_scan_parser.add_argument("--dry-run", action="store_true")
    triage_scan_parser.add_argument("--answer-to-stream-id")
    triage_scan_parser.add_argument("--timeout", type=float, default=30.0)
    triage_scan_parser.set_defaults(func=triage_scan_publish)

    triage_apply_parser = triage_sub.add_parser("apply-answer")
    triage_apply_parser.add_argument("--memory-root", required=True)
    triage_apply_parser.add_argument("--spec-id")
    action_group = triage_apply_parser.add_mutually_exclusive_group(required=True)
    action_group.add_argument("--action", choices=["keep", "defer", "deprecate"])
    action_group.add_argument("--merge-target")
    action_group.add_argument("--answer-json")
    triage_apply_parser.add_argument("--decided-by", default="operator")
    triage_apply_parser.add_argument("--reason")
    triage_apply_parser.add_argument("--defer-until")
    triage_apply_parser.set_defaults(
        func=lambda args: triage_apply_answer(
            argparse.Namespace(
                **{
                    **vars(args),
                    "action": f"merge:{args.merge_target}" if args.merge_target else args.action,
                }
            )
        )
    )


    asset_parser = subparsers.add_parser("asset")
    asset_parser.add_argument("asset_command", nargs="?", choices=["publish", "list", "get", "comments", "health"], default="publish")
    asset_parser.add_argument("asset_id_arg", nargs="?")
    asset_parser.add_argument("asset_extra_args", nargs="*")
    asset_parser.add_argument("--title")
    asset_parser.add_argument(
        "--type",
        dest="content_type",
        metavar="report",
        help="structured report JSON (the only supported publish type)",
    )
    asset_parser.add_argument("--content-file")
    asset_parser.add_argument("--tag", action="append", dest="tags", default=[])
    asset_parser.add_argument("--session")
    asset_parser.add_argument("--spec-id", type=_spec_id_arg)
    asset_parser.add_argument("--asset-id")
    asset_parser.add_argument("--unresolved", action="store_true")
    asset_parser.add_argument("--note")
    asset_parser.add_argument("--timeout", type=float, default=30.0)
    asset_parser.set_defaults(func=asset)

    title_parser = subparsers.add_parser("title")
    title_parser.add_argument("text", nargs="+")
    title_parser.add_argument("--timeout", type=float, default=30.0)
    title_parser.set_defaults(func=title)

    status_parser = subparsers.add_parser(
        "status",
        description=(
            "set/update this session's status card (goal, plan steps, latest update, "
            "handoff-planned flag); partial updates allowed"
        ),
    )
    status_parser.add_argument("--goal", default=None, help="the complete goal, 1-2 sentences (<=500 chars)")
    status_parser.add_argument(
        "--plan",
        action="append",
        default=None,
        help="one plan step per flag; providing --plan replaces the whole plan (<=20 steps)",
    )
    status_parser.add_argument(
        "--step-done",
        dest="step_done",
        type=_step_done_arg,
        action="append",
        default=None,
        help="mark 1-based plan step N done; repeat the flag or pass a comma-list",
    )
    status_parser.add_argument("--update", default=None, help="one-line latest update (<=300 chars)")
    status_parser.add_argument(
        "--handoff-planned",
        dest="handoff_planned",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="set/clear the handoff-planned flag",
    )
    status_parser.add_argument("--timeout", type=float, default=30.0)
    status_parser.set_defaults(func=status)

    close_parser = subparsers.add_parser("close")
    close_parser.add_argument("--reason", default="manual")
    close_parser.add_argument(
        "--operator-confirm",
        dest="operator_confirm",
        action="store_true",
        help="Mark this close as operator-authorized (required for operator-shape session names).",
    )
    close_mode = close_parser.add_mutually_exclusive_group()
    close_mode.add_argument(
        "--force",
        action="store_true",
        help="Override the daemon working-session close guard.",
    )
    close_mode.add_argument(
        "--defer-if-working",
        action="store_true",
        help="Persist an idempotent close intent when the target is still working.",
    )
    close_parser.add_argument(
        "--from-stream-id",
        dest="from_stream_id",
        default=None,
        help="Override the caller's stream_id used by the daemon's identity check (defaults to local leader).",
    )
    close_parser.add_argument("--caller-stream-id")
    close_parser.add_argument("--progeny", help="Successor stream id that should receive future messages for this stream.")
    close_parser.add_argument("--disposition-waived", dest="disposition_waived_reason", help="Waive in_progress spec disposition gate with a required reason.")
    close_parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for the chat_streamd close response (direct connection).",
    )
    close_parser.add_argument("stream_id")
    close_parser.set_defaults(func=close)

    drain_sessions_parser = subparsers.add_parser(
        "drain-sessions",
        description="operator-confirmed close of exact stale session-store rows",
    )
    drain_sessions_parser.add_argument(
        "--operator-confirm",
        dest="operator_confirm",
        action="store_true",
        help="Required confirmation for explicit session-store row drain.",
    )
    drain_sessions_parser.add_argument("--timeout", type=float, default=30.0)
    drain_sessions_parser.add_argument("stream_ids", nargs="+", help="exact host:session_name key to close")
    drain_sessions_parser.set_defaults(func=drain_sessions)

    reconcile_parser = subparsers.add_parser("reconcile")
    reconcile_sub = reconcile_parser.add_subparsers(dest="reconcile_command", required=True)
    reconcile_status_parser = reconcile_sub.add_parser("status")
    reconcile_status_parser.add_argument("--host")
    reconcile_status_parser.add_argument("--json", action="store_true")
    reconcile_status_parser.add_argument("--timeout", type=float, default=30.0)
    reconcile_status_parser.set_defaults(func=reconcile_status)
    reparent_parser = subparsers.add_parser(
        "reparent",
        description="move a direct-child worker to a new parent (same-daemon, same-host)",
    )
    reparent_parser.add_argument("stream_id", help="worker stream id to re-parent")
    reparent_parser.add_argument(
        "--to",
        dest="new_parent",
        required=True,
        help="new parent stream id the worker becomes a direct child of",
    )
    reparent_parser.add_argument(
        "--from-stream-id",
        dest="from_stream_id",
        default=None,
        help="caller's asserted stream id (token-verified by the daemon; defaults to local leader).",
    )
    reparent_parser.add_argument(
        "--caller-stream-id",
        default=None,
        help="caller stream id recorded as the audit actor (defaults to --from-stream-id).",
    )
    reparent_parser.add_argument("--reason", default="reparent")
    reparent_parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for the chat_streamd reparent response (direct connection).",
    )
    reparent_parser.set_defaults(func=reparent)

    await_parser = subparsers.add_parser("await")
    await_parser.add_argument("--read-outbox", action="store_true", help="Deprecated no-op; await uses chat_streamd await_report.")
    await_parser.add_argument("--include-details", action="store_true")
    await_parser.add_argument("--include-extras", action="store_true")
    # default None → resolved in parse_args to a mode-aware default (30s for
    # msg_id mode, larger for stream mode). An explicit value always wins.
    await_parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Seconds to wait. Defaults to 30s with --msg-id; larger in stream mode (no --msg-id) for long fire-and-forget workers.",
    )
    await_parser.add_argument("--from", dest="from_stream_id")
    await_parser.add_argument(
        "--msg-id",
        dest="flag_msg_id",
        type=int,
        help="Optional. Omit (stream mode) to resolve on the stream's terminal report for any msg_id or on close; give it to await a specific inbox msg_id.",
    )
    await_parser.add_argument("legacy_stream_id", nargs="?")
    await_parser.add_argument("legacy_msg_id", nargs="?", type=int)
    await_parser.set_defaults(func=await_command)

    report_parser = subparsers.add_parser("report")
    result_group = report_parser.add_mutually_exclusive_group()
    result_group.add_argument("--result")
    result_group.add_argument(
        "--result-file",
        help="legacy file-backed input; QA verdict/target flags compose inline, other file uploads remain unsupported in v2",
    )
    report_parser.add_argument(
        "--msg-id",
        type=int,
        default=None,
        help="Inbox msg_id this report answers. Omit for a proactive stream report; omitted defaults to msg_id=0.",
    )
    report_parser.add_argument("--status", choices=["done", "progress", "error", "aborted"], required=True)
    report_parser.add_argument("--reason")
    report_parser.add_argument("--report-id")
    report_parser.add_argument("--completion-kind", choices=["implementation_ready", "tracked"])
    report_parser.add_argument("--qa-verdict", choices=["accept", "reject"])
    report_parser.add_argument("--target-sha")
    report_parser.add_argument("--qa-attestation-stream-id")
    report_parser.add_argument("--qa-attestation-report-id")
    report_parser.add_argument("--from-stream-id", dest="from_stream_id")
    report_parser.add_argument("--timeout", type=float, default=30.0)
    report_parser.add_argument("--terminate", action="store_true")
    report_parser.add_argument("--discharges", action="append", default=[])
    report_parser.add_argument("--disposition-waived", dest="disposition_waived_reason", help="Waive in_progress spec disposition gate for the terminate close with a required reason.")
    report_parser.add_argument("--debug", action="store_true")
    report_parser.set_defaults(func=report)

    grant_self_token_parser = subparsers.add_parser(
        "grant-self-token",
        help="Bootstrap-once mint an AGENT_ORCH_STREAM_TOKEN for this session. "
             "Prints the plaintext to stdout; pipe to `export` via $(...). "
             "Refused if this session already has a token (one-time).",
    )
    grant_self_token_parser.add_argument(
        "--stream",
        dest="stream_id",
        default=None,
        help="Stream id to grant (defaults to $AGENT_ORCH_STREAM_ID / $PENTACLE_STREAM_ID).",
    )
    grant_self_token_parser.add_argument("--timeout", type=float, default=30.0)
    grant_self_token_parser.set_defaults(func=grant_self_token)

    await_spawn_parser = subparsers.add_parser("await-spawn")
    await_spawn_key = await_spawn_parser.add_mutually_exclusive_group(required=True)
    await_spawn_key.add_argument("--request-id")
    await_spawn_key.add_argument("--stream-id")
    await_spawn_parser.add_argument("--timeout", type=float, default=30.0)
    await_spawn_parser.set_defaults(func=await_spawn)

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--include-children", action="store_true", help="Deprecated no-op; direct snapshots include subagents.")
    list_parser.add_argument("--timeout", type=float, default=None)
    list_parser.set_defaults(func=list_sessions)

    models_parser = subparsers.add_parser(
        "models",
        aliases=["profiles"],
        help="show the daemon's current model/profile catalog",
    )
    models_parser.add_argument("--timeout", type=float, default=30.0)
    models_parser.set_defaults(func=models)

    visibility_parser = subparsers.add_parser("visibility")
    visibility_sub = visibility_parser.add_subparsers(dest="visibility_command", required=True)
    visibility_set_parser = visibility_sub.add_parser("set")
    visibility_set_parser.add_argument("stream_id")
    visibility_set_parser.add_argument("value", choices=["default", "nested", "hidden"])
    visibility_set_parser.add_argument("--timeout", type=float, default=30.0)
    visibility_set_parser.set_defaults(func=visibility_set)

    spec_parser = subparsers.add_parser("spec")
    spec_sub = spec_parser.add_subparsers(dest="spec_command", required=True)
    for _spec_cmd in ("attach", "detach"):
        spec_cmd_parser = spec_sub.add_parser(_spec_cmd)
        spec_cmd_parser.add_argument("stream_id")
        spec_cmd_parser.add_argument("spec_id", type=_spec_id_arg)
        spec_cmd_parser.add_argument("--from-stream-id")
        spec_cmd_parser.add_argument("--timeout", type=float, default=30.0)
        spec_cmd_parser.set_defaults(func=spec_update)

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("stream_id")
    inspect_parser.add_argument("--msg-id", type=int)
    inspect_parser.add_argument("--event-tail", type=int)
    inspect_text = inspect_parser.add_mutually_exclusive_group()
    inspect_text.add_argument("--full", action="store_true", help="print event text without truncation")
    inspect_text.add_argument("--max-text", type=_positive_int_arg, help="maximum event-text characters to print")
    inspect_parser.add_argument("--json", action="store_true")
    inspect_parser.add_argument("--timeout", type=float, default=30.0)
    inspect_parser.set_defaults(func=inspect)

    ledger_parser = subparsers.add_parser("ledger", description="read durable coordination ledger records")
    ledger_sub = ledger_parser.add_subparsers(dest="ledger_command", required=True)
    ledger_get_parser = ledger_sub.add_parser("get", help="retrieve a tell by id with full text and delivery metadata")
    ledger_get_parser.add_argument("tell_id")
    ledger_get_parser.add_argument("--timeout", type=float, default=30.0)
    ledger_get_parser.set_defaults(func=ledger_get)

    audit_parser = subparsers.add_parser(
        "audit", description="read-only views over already-delivered daemon records",
    )
    audit_sub = audit_parser.add_subparsers(dest="audit_command", required=True)
    audit_inbound_parser = audit_sub.add_parser(
        "inbound", help="list delivered tell frames addressed to a stream",
    )
    audit_inbound_parser.add_argument(
        "stream_id", nargs="?", help="recipient stream (defaults to this seat)",
    )
    audit_inbound_parser.add_argument("--limit", type=_positive_int_arg, default=50)
    audit_inbound_parser.add_argument("--json", action="store_true")
    audit_inbound_parser.add_argument("--timeout", type=float, default=30.0)
    audit_inbound_parser.set_defaults(func=inbound_audit)

    # Stub subparser so `agent-orch ssh` shows in `agent-orch --help`. The real
    # dispatch is the pre-argparse sys.argv intercept in main(); argparse never
    # actually runs this parser, so leading ssh flags (-i, -v, -h, -p, -J, --)
    # pass through to ssh untouched. add_help=False so a future code path can't
    # accidentally intercept `-h` here either.
    ssh_parser = subparsers.add_parser(
        "ssh",
        add_help=False,
        help="Passthrough to ssh(1) with SendEnv=PENTACLE_STREAM_ID AGENT_ORCH_STREAM_ID injected.",
        epilog=(
            "This is a passthrough to ssh(1). All arguments after `ssh` are forwarded "
            "verbatim. Run `ssh -h` for ssh's own flags."
        ),
    )
    ssh_parser.set_defaults(func=lambda _args: ssh_passthrough([]))

    return parser


def main(argv: list[str] | None = None) -> int:
    # Pre-argparse intercept: `agent-orch ssh ...` is a true ssh(1) passthrough.
    # argparse.REMAINDER does NOT work here -- it stops capturing on leading
    # flag-like tokens (-i, -v, -h, -p, -J), so we slice argv ourselves before
    # any argparse logic runs.
    effective_argv = sys.argv[1:] if argv is None else argv
    if effective_argv and effective_argv[0] == "ssh":
        return ssh_passthrough(list(effective_argv[1:]))
    # `agent-orch spawn cancel|status <...>` is surfaced UX for the
    # `spawn-cancel`/`spawn-status` sibling verbs (a flat `spawn` parser cannot
    # host subcommands). Rewrite only the exact two-token prefix; `spawn --help`
    # and normal spawns are untouched.
    if len(effective_argv) >= 2 and effective_argv[0] == "spawn" and effective_argv[1] in ("cancel", "status", "freeze", "unfreeze"):
        rewritten = [f"spawn-{effective_argv[1]}", *effective_argv[2:]]
        parser = build_parser()
        args = parser.parse_args(rewritten)
        try:
            return args.func(args)
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            return 1
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
