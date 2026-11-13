"""Strict publication suppresses implicit replies, independently of the mirror flag."""
import pytest
from test_assistant_dispatch_turn_final import run


def test_primary_turn_final_never_publishes_implicitly():
    async def body(h):
        route = await h.route("one-door")
        await h.dispatch_user(route, "one-door-user")
        await h.ingest("ASSIST_TEXT", "Unpublished turn prose", "one-door-final", final=True)
        assert await h.answers() == []
        assert await h.publications(route["dispatch_id"]) == []
    run(body)
