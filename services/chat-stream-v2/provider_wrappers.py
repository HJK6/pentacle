"""Exact provider presentation envelopes; source authentication is caller-owned.

Grammar provenance is not cryptographic provenance. Only the authenticated
Claude transcript route opts in; matching ordinary strings does not authenticate
them. Strip one layer so an operator's nested literal example survives.
"""
from __future__ import annotations

import logging
import re


PROVIDER_WRAPPERS = (
    ("claude", "claude_pasted_content", re.compile(
        r'\n\n<pasted_content id="(?P<id>[0-9a-f]+)">\n'
        r'(?P<body>[\s\S]*)\n</pasted_content id="(?P=id)">\n'
    )),
)


def normalize_provider_user_text(
    text: str, *, provider: str, authenticated: bool = False,
) -> tuple[str, dict[str, str] | None]:
    """Return display text and an optional v1 wrapper tag, preserving body bytes."""
    if not authenticated:
        return text, None
    for registered_provider, kind, pattern in PROVIDER_WRAPPERS:
        if provider != registered_provider:
            continue
        match = pattern.fullmatch(text)
        if match is not None:
            logging.getLogger("chat_streamd_v2.provider_wrappers").info(
                "subsystem=provider_wrapper bug_ref=spec_pentacle__claude_pasted_content_envelope_2026_09 "
                "normalized kind=%s id=%s", kind, match["id"],
                extra={"subsystem": "provider_wrapper", "bug_ref":
                       "spec_pentacle__claude_pasted_content_envelope_2026_09"},
            )
            return match["body"], {
                "kind": kind, "id": match["id"], "provenance": "grammar",
            }
    return text, None
