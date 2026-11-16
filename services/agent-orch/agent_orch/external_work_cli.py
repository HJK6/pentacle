"""`agent-orch external-work`: read and record the front desk's external-work check.

Both verbs authenticate with the calling seat's existing stream token; the
daemon refuses any seat that is not the current front desk. The record file
holds the observation only: the wrapper adds the wire type and the token.
Contract: services/chat-stream-v2/docs/external-work.md.
"""
from __future__ import annotations

from .send_boundary import ServiceCredentialUnavailable

import argparse
import asyncio
import json
import sys
import uuid
from typing import Any


def _call(args: argparse.Namespace, payload: dict[str, Any]) -> int:
    from agent_orch import cli
    try:
        response = asyncio.run(cli.external_work_once(
            cli.load_config(), payload, timeout=float(getattr(args, "timeout", 30.0) or 30.0)))
    except ServiceCredentialUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - one readable error line, never the payload
        print(f"agent-orch external-work: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(response, separators=(",", ":"), default=str))
    return 0 if str(response.get("type") or "").endswith(".ok") else 1


def cmd_show(args: argparse.Namespace) -> int:
    return _call(args, {"type": "external_work.show"})


def cmd_record(args: argparse.Namespace) -> int:
    try:
        with open(args.file, encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError) as exc:
        print(f"agent-orch external-work record: unreadable record file: {type(exc).__name__}", file=sys.stderr)
        return 2
    # The daemon owns validation; the client never repairs or guesses a field.
    request_id = record.get("request_id") if isinstance(record, dict) else None
    return _call(args, {
        "type": "external_work.record", "record": record,
        "request_id": request_id if isinstance(request_id, str) and request_id else f"external-work-{uuid.uuid4()}",
    })


def cmd_packet(args):
    payload = {"type": "external_work.packet", "op": args.packet_op, "tag": args.tag}
    if args.packet_op == "open":
        payload["spec_id"] = args.spec_id
        if args.queued:
            payload["state"] = "queued"
    elif args.packet_op in {"working", "queued", "review"}:
        payload.update(op="set", state=args.packet_op)
    return _call(args, payload)


def add_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser("external-work", help="front-desk external-work liveness check")
    sub = parser.add_subparsers(dest="external_work_command", required=True)
    show = sub.add_parser("show", help="print the durable check state, deadlines and reasons")
    show.add_argument("--timeout", type=float, default=30.0)
    show.set_defaults(func=cmd_show)
    record = sub.add_parser("record", help="record one observation from a JSON file")
    record.add_argument("--file", required=True, help="JSON file holding the record payload")
    record.add_argument("--timeout", type=float, default=30.0)
    record.set_defaults(func=cmd_record)

    packet = sub.add_parser("packet", help="manage the external packet ledger")
    operations = packet.add_subparsers(dest="packet_op", required=True)
    for op in ("open", "working", "queued", "review", "close", "ignore"):
        command = operations.add_parser(op)
        command.add_argument("tag")
        command.add_argument("--timeout", type=float, default=30.0)
        if op == "open":
            command.add_argument("--spec-id", required=True)
            command.add_argument("--queued", action="store_true")
        command.set_defaults(func=cmd_packet)
