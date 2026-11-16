"""Authenticated, ownership-safe activation windows for live daemon harnesses."""

from .core import (
    LiveWindow,
    LiveWindowError,
    OwnedSession,
    OwnedSessionRegistry,
    OwnershipError,
    TeardownError,
    authenticated_operator_connection,
    operator_hello,
    write_receipt,
)

__all__ = [
    "LiveWindow",
    "LiveWindowError",
    "OwnedSession",
    "OwnedSessionRegistry",
    "OwnershipError",
    "TeardownError",
    "authenticated_operator_connection",
    "operator_hello",
    "write_receipt",
]
