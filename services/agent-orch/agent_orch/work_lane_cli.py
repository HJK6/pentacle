"""`agent-orch work-lane`: read and maintain first-class work lanes.

Reads go to the daemon's `work_lanes.*` verbs.  Mutators are thin wrappers
over `assistant.operation --operation work_lane.*` with the same stable
request-id and expected-version discipline; the daemon refuses non-FD actors.
Contract: services/chat-stream-v2/docs/work-lanes.md.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

DEFAULT_COMPOSITE = "bart:assistant"
CONFIRM_ACTIONS = ("set_state:done", "set_owner:fd", "lane.close", "lane.decision:cancel")


def _cli():
    from agent_orch import cli
    return cli


def _call(args: argparse.Namespace, payload: dict[str, Any]) -> dict[str, Any]:
    cli = _cli()
    return asyncio.run(cli.assistant_once(cli.load_config(), payload,
                                          timeout=float(getattr(args, "timeout", 30.0) or 30.0)))


def _finish(response: dict[str, Any]) -> int:
    print(json.dumps(response, separators=(",", ":"), default=str))
    return 0 if str(response.get("type") or "").endswith(".ok") else 1


def _lane_line(lane: dict[str, Any]) -> str:
    lead = lane.get("lead") or {}
    chat = lane.get("visible_chat") or {}
    presence = (lead.get("presence") or {})
    lead_text = (f"{lead.get('stream_id')}{'' if lead.get('qualifies') else ' (not qualifying)'}"
                 f"{' online' if presence.get('online') else ''}") if lead else "-"
    state = lane.get("state")
    if lane.get("state_reason") in ("lead_lost", "lead_lost_unreconciled"):
        state = f"{state}/{lane['state_reason']}"
    if lane.get("blocker"):
        state = f"{state}: {lane['blocker']}"
    return (f"{lane.get('lane_id')}  v{lane.get('version')}  [{state}]  {lane.get('owner_kind')}  "
            f"{lane.get('title')}\n    lead {lead_text}  chat {chat.get('stream_id')} ({chat.get('available')})")


def _print_members(members):
    for member in members:
        ac = (f"{member['ac_checked']}/{member['ac_total']}" if member.get("ac_total") is not None else "unknown")
        estimate = member.get("estimate")
        hours = f"{estimate['p25']}–{estimate['p75']}h (median {estimate['median']})" if estimate else "unknown"
        print(f"    {member['spec_id']}  {member.get('status')}  AC {ac}  estimate {hours}  "
              f"[{(member.get('observation') or {}).get('quality', 'unknown')}]")
        if member.get("next_action_text"):
            print(f"      next: {member['next_action_text']}")


def cmd_list(args: argparse.Namespace) -> int:
    try:
        response = _call(args, {"type": "work_lanes.list", "include_done": bool(args.include_done),
                                "limit": args.limit, **({"members": True} if args.members else {})})
    except Exception as exc:  # noqa: BLE001
        print(f"agent-orch work-lane list: {exc}", file=sys.stderr)
        return 1
    if args.json or not str(response.get("type") or "").endswith(".ok"):
        return _finish(response)
    lanes = response.get("lanes") or []
    open_count = sum(1 for lane in lanes if lane.get("state") != "done")
    print(f"{open_count} open lane(s)")
    for lane in lanes:
        print(_lane_line(lane))
        if args.members:
            _print_members(lane.get("members", []))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    try:
        response = _call(args, {"type": "work_lanes.show", "lane_id": args.lane_id,
                                **({"members": True} if args.members else {})})
    except Exception as exc:  # noqa: BLE001
        print(f"agent-orch work-lane show: {exc}", file=sys.stderr)
        return 1
    if args.json or not str(response.get("type") or "").endswith(".ok"):
        return _finish(response)
    lane = response.get("projection") or {}
    stored = response.get("lane") or {}
    print(_lane_line(lane) if lane else stored.get("lane_id"))
    print(f"    stored {stored.get('work_state')} ({stored.get('work_state_reason')})  "
          f"owner_kind={stored.get('owner_kind')}  version={stored.get('version')}")
    if args.members:
        _print_members(response.get("members", lane.get("members", [])))
    for event in response.get("events") or []:
        update = f"  -> {event.get('update_kind')} {event.get('update_id')}" if event.get("update_kind") else ""
        print(f"    {event.get('created_at')}  {event.get('operation')}  "
              f"{event.get('prior_state')}->{event.get('next_state')}  {event.get('event_id')}{update}")
    return 0


def _operation(args: argparse.Namespace, operation: str, payload: dict[str, Any], *,
               lane: bool = True) -> int:
    message: dict[str, Any] = {
        "type": "assistant.operation", "request_id": args.request_id,
        "composite_stream_id": args.composite_stream_id, "dispatch_id": "none",
        "operation": "work_lane." + operation, "payload": payload,
    }
    if lane:
        message["lane_id"] = args.lane_id
        message["expected_lane_version"] = args.expected_version
    try:
        return _finish(_call(args, message))
    except Exception as exc:  # noqa: BLE001
        print(f"agent-orch work-lane: {exc}", file=sys.stderr)
        return 1


def _confirmation(args: argparse.Namespace, payload: dict[str, Any]) -> dict[str, Any]:
    if getattr(args, "confirmation_question_id", None):
        payload["operator_confirmation"] = {"question_id": args.confirmation_question_id}
    return payload


def cmd_set_state(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {"to": args.to}
    for key in ("blocker", "outcome", "resolution", "reason"):
        if getattr(args, key):
            payload[key] = getattr(args, key)
    return _operation(args, "set_state", _confirmation(args, payload))


def _pointer(stream_id: str | None, generation: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {"stream_id": stream_id}
    if generation:
        out["generation"] = generation
    return out


def cmd_set_lead(args: argparse.Namespace) -> int:
    if args.clear and args.lead_stream_id:
        print("agent-orch work-lane set-lead: use --clear or --lead-stream-id, not both", file=sys.stderr)
        return 2
    if not args.clear and not (args.lead_stream_id and args.lead_generation):
        print("agent-orch work-lane set-lead: --lead-stream-id and --lead-generation are required", file=sys.stderr)
        return 2
    payload: dict[str, Any] = {"lead": None if args.clear else
                               {"stream_id": args.lead_stream_id, "generation": args.lead_generation}}
    if args.chat_stream_id:
        payload["visible_chat"] = _pointer(args.chat_stream_id, args.chat_generation)
    return _operation(args, "set_lead", payload)


def cmd_set_chat(args: argparse.Namespace) -> int:
    return _operation(args, "set_chat", {"visible_chat": _pointer(args.chat_stream_id, args.chat_generation)})


def cmd_set_text(args: argparse.Namespace) -> int:
    payload = {k: getattr(args, k) for k in ("title", "summary") if getattr(args, k) is not None}
    if not payload:
        print("agent-orch work-lane set-text: give --title and/or --summary", file=sys.stderr)
        return 2
    return _operation(args, "set_text", payload)


def cmd_set_members(args: argparse.Namespace) -> int:
    members = args.member or []
    return _operation(args, "set_members", {"members": members, "no_spec_reason": args.no_spec_reason})


def cmd_set_owner(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {"to": args.to}
    if args.reason:
        payload["reason"] = args.reason
    return _operation(args, "set_owner", _confirmation(args, payload))


def cmd_update(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {"kind": args.kind, "source_id": args.source_id, "summary": args.summary}
    if args.grouped_source_ids:
        payload["grouped_source_ids"] = [s for s in args.grouped_source_ids.split(",") if s]
    return _operation(args, "update", payload)


def cmd_adopt(args: argparse.Namespace) -> int:
    if bool(args.preview) == bool(args.apply):
        print("agent-orch work-lane adopt: give exactly one of --preview or --apply <json-file>", file=sys.stderr)
        return 2
    if args.epic and not args.preview:
        print("agent-orch work-lane adopt: --epic requires --preview", file=sys.stderr)
        return 2
    if args.preview:
        try:
            return _finish(_call(args, {"type": "work_lanes.adopt_preview",
                                        "composite_stream_id": args.composite_stream_id,
                                        **({"epic": args.epic} if args.epic else {})}))
        except Exception as exc:  # noqa: BLE001
            print(f"agent-orch work-lane adopt: {exc}", file=sys.stderr)
            return 1
    try:
        entries = json.loads(Path(args.apply).read_text())
    except (OSError, ValueError) as exc:
        print(f"agent-orch work-lane adopt: cannot read {args.apply}: {exc}", file=sys.stderr)
        return 2
    if isinstance(entries, dict):
        entries = entries.get("candidates")
    if not isinstance(entries, list):
        print("agent-orch work-lane adopt: --apply file must be a list (or {candidates:[...]})", file=sys.stderr)
        return 2
    allowed = {"adoption_key", "title", "summary", "owner_kind", "work_state", "blocker", "lead",
               "visible_chat", "lane_id", "emit_started", "members", "no_spec_reason"}
    failures = 0
    for entry in entries:
        if not isinstance(entry, dict):
            failures += 1
            continue
        payload = {k: v for k, v in entry.items() if k in allowed and v is not None}
        message = {"type": "assistant.operation", "request_id": "adopt:" + str(entry.get("adoption_key") or ""),
                   "composite_stream_id": args.composite_stream_id, "dispatch_id": "none",
                   "operation": "work_lane.adopt", "payload": payload}
        try:
            response = _call(args, message)
        except Exception as exc:  # noqa: BLE001
            response = {"type": "error", "error": str(exc)}
        print(json.dumps({"adoption_key": entry.get("adoption_key"), "response": response},
                         separators=(",", ":"), default=str))
        if not str(response.get("type") or "").endswith(".ok"):
            failures += 1
    return 0 if failures == 0 else 1


def cmd_request_confirmation(args: argparse.Namespace) -> int:
    """Ask the operator a lane+action-scoped question; its id is the confirmation."""
    cli = _cli()
    prompt_protocol = cli.prompt_protocol
    ns = argparse.Namespace(
        title=args.title, body=args.body, context=None, question_id=args.question_id,
        from_stream_id=None, provider=None, spec_id=None,
        dedup_key=f"work-lane-confirm:{args.lane_id}:{args.action}", ttl=None, allow_custom=False,
        response_mode="single_choice",
        prompt_options=[("plain", "Confirm"), ("plain", "Not yet")],
    )
    envelope = cli._prompt_envelope_from_args(ns)
    envelope["context"] = {"schema": "WorkLaneConfirmationV1", "lane_id": args.lane_id, "action": args.action}
    payload = {"type": "prompt.ask", "envelope": envelope,
               "actions": prompt_protocol.notification_actions(envelope), "severity": "info"}
    try:
        response = asyncio.run(cli.prompt_ask_once(cli.load_config(), payload,
                                                   timeout=float(args.timeout or 30.0)))
    except Exception as exc:  # noqa: BLE001
        print(f"agent-orch work-lane request-confirmation: {exc}", file=sys.stderr)
        return 1
    return _finish(response)


def add_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser("work-lane", help="read and maintain first-class work lanes")
    sub = parser.add_subparsers(dest="work_lane_command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--composite-stream-id", default=DEFAULT_COMPOSITE)
        p.add_argument("--timeout", type=float, default=30.0)

    def mutator(name: str, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("lane_id")
        p.add_argument("--expected-version", type=int, required=True)
        p.add_argument("--request-id", required=True, help="stable id; retry with the same id")
        common(p)
        return p

    p = sub.add_parser("list", help="open lanes in server order")
    p.add_argument("--include-done", action="store_true")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--json", action="store_true")
    p.add_argument("--members", action="store_true", help="include member work facts")
    common(p)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="one lane with its events and updates")
    p.add_argument("lane_id")
    p.add_argument("--json", action="store_true")
    p.add_argument("--members", action="store_true", help="include member work facts")
    common(p)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("adopt", help="preview adoption candidates or apply an FD-edited list")
    p.add_argument("--preview", action="store_true")
    p.add_argument("--apply", metavar="JSON_FILE")
    p.add_argument("--epic", help="expand this epic once in the preview")
    common(p)
    p.set_defaults(func=cmd_adopt)

    p = mutator("set-state", "FD: set active|paused|blocked|done")
    p.add_argument("--to", required=True, choices=("active", "paused", "blocked", "done"))
    p.add_argument("--blocker")
    p.add_argument("--outcome")
    p.add_argument("--resolution")
    p.add_argument("--reason", help="required for a bound-FD override of an operator-owned lane")
    p.add_argument("--confirmation-question-id")
    p.set_defaults(func=cmd_set_state)

    p = mutator("set-lead", "FD: link the visible lead (never resumes)")
    p.add_argument("--lead-stream-id")
    p.add_argument("--lead-generation")
    p.add_argument("--clear", action="store_true")
    p.add_argument("--chat-stream-id")
    p.add_argument("--chat-generation")
    p.set_defaults(func=cmd_set_lead)

    p = mutator("set-chat", "FD: set the visible-chat destination")
    p.add_argument("--chat-stream-id", required=True)
    p.add_argument("--chat-generation")
    p.set_defaults(func=cmd_set_chat)

    p = mutator("set-text", "FD: lane title/summary")
    p.add_argument("--title")
    p.add_argument("--summary")
    p.set_defaults(func=cmd_set_text)

    p = mutator("set-members", "FD: replace ordered lane membership")
    p.add_argument("--member", action="append", help="canonical spec id; repeat in membership order")
    p.add_argument("--no-spec-reason", help="required when the member list is empty")
    p.set_defaults(func=cmd_set_members)

    p = mutator("set-owner", "FD: owner kind (operator->fd needs a reason or confirmation)")
    p.add_argument("--to", required=True, choices=("fd", "operator"))
    p.add_argument("--reason", help="required for a bound-FD override to fd")
    p.add_argument("--confirmation-question-id")
    p.set_defaults(func=cmd_set_owner)

    p = mutator("update", "FD: explicit major_decision or milestone update")
    p.add_argument("--kind", required=True, choices=("major_decision", "milestone"))
    p.add_argument("--source-id", required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--grouped-source-ids", help="comma-separated folded milestone ids")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("request-confirmation", help="FD: ask the operator to confirm one guarded lane action")
    p.add_argument("lane_id")
    p.add_argument("--action", required=True, choices=CONFIRM_ACTIONS)
    p.add_argument("--title", required=True)
    p.add_argument("--body", required=True)
    p.add_argument("--question-id")
    p.add_argument("--timeout", type=float, default=30.0)
    p.set_defaults(func=cmd_request_confirmation)
