"""Request a host's screen before cooperative desktop automation."""

from .send_boundary import ServiceCredentialUnavailable

import asyncio
import json
import sys


def cmd_screen(args):
    from agent_orch import cli

    payload = {"type": "screen.lease." + args.screen_command}
    for key in ("host", "reason", "ttl", "lease_id", "force"):
        value = getattr(args, key, None)
        if value is not None and (key != "force" or value):
            payload[key] = value
    try:
        response = asyncio.run(
            cli.screen_lease_once(cli.load_config(), payload, timeout=args.timeout)
        )
    except ServiceCredentialUnavailable:
        raise
    except Exception as exc:
        print(f"agent-orch screen: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if (
        args.json
        or args.screen_command != "status"
        or not str(response.get("type", "")).endswith(".ok")
    ):
        print(json.dumps(response, separators=(",", ":"), default=str))
    else:
        for host in response["hosts"]:
            lease = host["lease"]
            detail = (
                f" {lease['holder_stream_id']} {lease['lease_id']} expires={lease['expires_at']}"
                if lease
                else ""
            )
            print(f"{host['host']}: {host['state']}{detail}")
    return 0 if str(response.get("type", "")).endswith(".ok") else 1


def add_parser(subparsers):
    parser = subparsers.add_parser(
        "screen", help="acquire, renew, release or inspect a screen lease"
    )
    sub = parser.add_subparsers(dest="screen_command", required=True)
    for verb in ("acquire", "renew", "release", "status"):
        p = sub.add_parser(verb)
        p.add_argument("--json", action="store_true")
        p.add_argument("--timeout", type=float, default=30.0)
        p.set_defaults(func=cmd_screen)
        if verb == "acquire":
            p.add_argument("--host", required=True)
            p.add_argument("--reason", required=True)
        if verb in ("acquire", "renew"):
            p.add_argument("--ttl", type=int)
        if verb == "renew":
            p.add_argument("lease_id")
        if verb == "release":
            target = p.add_mutually_exclusive_group(required=True)
            target.add_argument("lease_id", nargs="?")
            target.add_argument("--host")
            p.add_argument("--force", action="store_true")
        if verb == "status":
            p.add_argument("--host")
