#!/usr/bin/env python3
"""Issue and administer v2 operator credentials through the shared registry.

The value printed by ``issue`` and ``rotate`` is the v2 credential envelope. It
is the enrollment value a device stores in its private token file; this tool
does not create a second enrollment-code store.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


SERVICES_DIR = Path(__file__).resolve().parents[2]
if str(SERVICES_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICES_DIR))

from _shared import operator_auth  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry",
        type=Path,
        default=None,
        metavar="PATH",
        help="operator credential registry (default: the shared v2 registry path)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    issue = commands.add_parser("issue", help="issue an enrollment value")
    issue.add_argument(
        "--client-kind",
        choices=sorted(operator_auth.CLIENT_KINDS),
        default="pentacle-mobile",
        help="device kind to bind (default: pentacle-mobile)",
    )
    issue.add_argument("--label", default="", help="optional device label")

    commands.add_parser("list", help="list credential metadata")

    revoke = commands.add_parser("revoke", help="revoke a credential")
    revoke.add_argument("credential_id")

    rotate = commands.add_parser("rotate", help="issue and immediately activate a replacement")
    rotate.add_argument("credential_id")
    rotate.add_argument("--label", default="", help="optional replacement label")

    return parser


def _print_json(value: Any) -> None:
    print(json.dumps(value, sort_keys=True))


def _list_credentials(registry: operator_auth.OperatorCredentialRegistry) -> None:
    rows = []
    for credential_id, record in sorted(registry.load().credentials.items()):
        replacement = record.get("replaces_credential_id")
        rows.append(
            {
                "credential_id": credential_id,
                "credential_fingerprint": operator_auth.credential_fingerprint(credential_id),
                "client_kind": record["client_kind"],
                "label": record["label"],
                "created_at": record["created_at"],
                "revoked_at": record["revoked_at"],
                "replaces_fingerprint": (
                    operator_auth.credential_fingerprint(replacement) if replacement else None
                ),
            }
        )
    _print_json(rows)


def _issue(
    registry: operator_auth.OperatorCredentialRegistry,
    *,
    client_kind: str,
    label: str,
) -> None:
    credential_id, code = registry.issue(client_kind, label=label)
    _print_json(
        {
            "status": "issued",
            "credential_id": credential_id,
            "client_kind": client_kind,
            "label": label,
            "code": code,
        }
    )


def _revoke(registry: operator_auth.OperatorCredentialRegistry, credential_id: str) -> None:
    registry.revoke(credential_id)
    _print_json(
        {
            "status": "revoked",
            "credential_fingerprint": operator_auth.credential_fingerprint(credential_id),
        }
    )


def _rotate(
    registry: operator_auth.OperatorCredentialRegistry,
    *,
    credential_id: str,
    label: str,
) -> None:
    prior_id = operator_auth.canonical_uuid(credential_id)
    prior = registry.load().credentials.get(prior_id)
    if prior is None or prior.get("revoked_at"):
        raise operator_auth.OperatorAuthError("credential is unavailable for rotation")

    replacement_label = label or f"rotation-{operator_auth.credential_fingerprint(prior_id)}"
    replacement_id, code = registry.issue(
        str(prior["client_kind"]),
        label=replacement_label,
        replaces_credential_id=prior_id,
    )
    replaced_id = registry.activate_replacement(replacement_id)
    _print_json(
        {
            "status": "rotated",
            "credential_id": replacement_id,
            "client_kind": prior["client_kind"],
            "label": replacement_label,
            "code": code,
            "replaced_credential_id": replaced_id,
        }
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    registry = operator_auth.OperatorCredentialRegistry(
        args.registry.expanduser() if args.registry is not None else None
    )
    try:
        registry.initialize()
        if args.command == "issue":
            _issue(registry, client_kind=args.client_kind, label=args.label.strip())
        elif args.command == "list":
            _list_credentials(registry)
        elif args.command == "revoke":
            _revoke(registry, args.credential_id)
        else:
            _rotate(registry, credential_id=args.credential_id, label=args.label.strip())
    except operator_auth.OperatorAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
