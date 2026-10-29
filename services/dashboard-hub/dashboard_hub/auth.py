"""Authenticate the phone with its Pentacle device token.

The token is the same `pentacle-auth-v2:` envelope the phone uses for the
daemon. We re-run the daemon's challenge/response locally against the operator
credential registry, so revoking the phone in Pentacle also locks it out here.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SHARED = Path(__file__).resolve().parents[2] / "_shared"
if str(_SHARED.parent) not in sys.path:
    sys.path.insert(0, str(_SHARED.parent))

from _shared import operator_auth  # noqa: E402

ALLOWED_CLIENT_KIND = "pentacle-mobile"


class Unauthorized(Exception):
    pass


def verify_token(token: str | None, registry: operator_auth.OperatorCredentialRegistry | None = None) -> str:
    """Return the credential id for a valid, unrevoked mobile token."""
    if not token:
        raise Unauthorized("missing token")
    try:
        envelope = operator_auth.decode_envelope(token)
        if envelope["client_kind"] != ALLOWED_CLIENT_KIND:
            raise Unauthorized("wrong client kind")
        nonce, _expires = operator_auth.new_nonce()
        proof = operator_auth.make_proof(
            envelope["proof_key"], nonce, envelope["credential_id"], envelope["client_kind"]
        )
        trust = (registry or operator_auth.OperatorCredentialRegistry()).verify(
            envelope["credential_id"], envelope["client_kind"], nonce, proof
        )
    except operator_auth.OperatorAuthError as exc:
        raise Unauthorized(str(exc)) from exc
    return trust.credential_id


def bearer(header: str | None) -> str | None:
    if header and header.startswith("Bearer "):
        return header[len("Bearer "):].strip()
    return None
