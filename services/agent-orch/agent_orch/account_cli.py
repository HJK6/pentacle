"""Account controls using the existing authenticated daemon transport."""
import asyncio
import json
import uuid


def register(subparsers):
    parser = subparsers.add_parser("account", help="daemon-owned provider account controls")
    parser.add_argument("action", choices=("default", "status", "relogin", "probe", "report-failure", "observe-failure"))
    parser.add_argument("--provider", choices=("codex", "claude"))
    parser.add_argument("--account")
    parser.add_argument("default_action", nargs="?", choices=("set",))
    parser.add_argument("--host")
    parser.add_argument("--reason")
    parser.add_argument("--kind", choices=("auth", "quota", "capacity", "network", "permission"))
    parser.add_argument("--login-generation")
    parser.add_argument("--operation-id")
    parser.add_argument("--from", dest="from_stream_id")
    parser.add_argument("--timeout", type=float, default=None,
                        help="RPC deadline seconds (probe: 210; other actions: 30)")
    parser.set_defaults(func=execute)


def execute(args):
    from .config import load_config
    from .stream_id import discover_leader_stream_id_short
    from .wsclient import account_once
    from .cli import _print_response, _direct_rpc_transport_error
    config = load_config()
    actor = args.from_stream_id or discover_leader_stream_id_short(config)
    payload = {"type": "account", "action": "default-set" if args.action == "default" else args.action, "from_stream_id": actor,
               "request_id": "account-" + uuid.uuid4().hex}
    for field in ("provider", "account", "host", "reason", "kind", "login_generation", "operation_id"):
        value = getattr(args, field, None)
        if value is not None: payload[field] = value
    if (args.action == "default") != (args.default_action == "set"):
        _print_response({"type": "account.error", "error_code": "account_action_invalid"}); return 2
    if args.action != "status":
        payload.setdefault("login_generation", "active")
    try:
        timeout = args.timeout if args.timeout is not None else (210 if args.action == "probe" else 30)
        reply = asyncio.run(account_once(config, payload, timeout=timeout))
    except Exception as exc:
        reply, code, _ = _direct_rpc_transport_error("account", payload["request_id"], exc)
        _print_response(reply); return code
    _print_response(reply)
    return 0 if reply.get("type") == "account.ok" else 1


def usage(args):
    from .config import load_config
    from .stream_id import discover_leader_stream_id_short
    from .wsclient import account_once
    from .cli import _print_response, _direct_rpc_transport_error
    config = load_config()
    payload = {"type": "account", "action": "status", "from_stream_id": discover_leader_stream_id_short(config),
               "request_id": "account-usage-" + uuid.uuid4().hex}
    try: reply = asyncio.run(account_once(config, payload, timeout=30))
    except Exception as exc:
        reply, code, _ = _direct_rpc_transport_error("account", payload["request_id"], exc)
        _print_response(reply); return code
    if reply.get("type") != "account.ok": _print_response(reply); return 1
    accounts = [row for row in reply.get("accounts", []) if row.get("alias") == args.account
                and (not args.host or row.get("host") == args.host)]
    pools = {(row["provider"], row["pool_id"]) for row in accounts}
    result = {"enabled": reply.get("enabled") is True, "accounts": accounts,
              "quota_pools": [row for row in reply.get("quota_pools", []) if (row["provider"], row["pool_id"]) in pools]}
    if args.json:
        print(json.dumps(result, separators=(",", ":")))
    else:
        print("PROVIDER ACCOUNT HOST GENERATION POOL HEALTH")
        for row in accounts:
            print(" ".join(str(row.get(key) or "unknown") for key in ("provider", "alias", "host", "generation", "pool_id", "health")))
        for pool in result["quota_pools"]:
            print(f'{pool["provider"]} {pool["pool_id"]}: ' + ("fresh" if pool["fresh"] else "unknown/stale") +
                  f' (age_seconds={pool.get("age_seconds")})')
    return 0
