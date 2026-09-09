"""Fail-closed authority and record identity for every asset by-ID verb."""

from __future__ import annotations

import asyncio
import json

import pytest

from assets import Assets


OWNER = "hosta:owner"
TARGET = "hosta:target"
OTHER = "hosta:other"
ASSET_ID = "shared-asset-id"
SPEC_ID = "spec_shared"

BY_ID_CASES = (
    ("asset.get", {}),
    ("asset.delete", {}),
    ("asset.comment.add", {"section_id": "s1", "block_id": "b1", "body": "new"}),
    ("asset.comment.edit", {"comment_id": "comment-1", "body": "edited"}),
    ("asset.comment.delete", {"comment_id": "comment-1"}),
    ("asset.comment.resolve", {"comment_id": "comment-1", "resolved": True}),
    ("asset.comments.list", {}),
    ("asset.read.set", {"read": True}),
    ("asset.review.set", {"review_status": "approved"}),
    ("asset.comments.send_to_chat", {}),
)

MOBILE_BY_ID_CASES = tuple(
    case for case in BY_ID_CASES
    if case[0] not in {"asset.comment.edit", "asset.comment.resolve", "asset.review.set"}
)


class _FakeSessions:
    def __init__(self, rows: dict[str, dict] | None = None) -> None:
        self._rows = rows or {}

    def get(self, stream_id: str):
        row = self._rows.get(stream_id)
        return dict(row) if row else None


def _report_body(title: str) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "title": title,
            "sections": [
                {
                    "id": "s1",
                    "title": "Findings",
                    "status": "reference",
                    "blocks": [
                        {
                            "id": "b1",
                            "type": "para",
                            "section_id": "s1",
                            "runs": [title],
                        }
                    ],
                }
            ],
        }
    )


async def _publish(
    assets: Assets,
    stream_id: str,
    title: str,
    *,
    asset_id: str = ASSET_ID,
    spec_id: str | None = None,
) -> dict:
    message = {
        "type": "asset.publish",
        "request_id": f"publish-{stream_id}",
        "stream_id": stream_id,
        "asset_id": asset_id,
        "title": title,
        "content_type": "report",
        "body": _report_body(title),
    }
    if spec_id is not None:
        message["spec_id"] = spec_id
    reply = await assets.asset(message)
    assert reply["type"] == "asset.publish.ok", reply
    return reply["asset"]


async def _seed_comment(assets: Assets, stream_id: str, asset_id: str = ASSET_ID) -> None:
    host, _, session_name = stream_id.partition(":")
    await assets._call(
        "add_comment",
        host=host,
        session_name=session_name,
        asset_id=asset_id,
        section_id="s1",
        block_id="b1",
        body="original",
        author="operator",
        comment_id="comment-1",
    )


async def _force_record_spec(assets: Assets, stream_id: str, spec_id: str) -> None:
    """Model a valid historical collision the publish de-duplicator no longer creates."""
    host, _, session_name = stream_id.partition(":")

    def update() -> None:
        assert assets._store is not None
        with assets._store._lock:
            assets._store._conn.execute(
                "UPDATE assets SET spec_id = ? WHERE host = ? AND session_name = ? AND asset_id = ?",
                (spec_id, host, session_name, ASSET_ID),
            )
            assets._store._conn.commit()

    await assets._run(update)


def _run(tmp_path, scenario, *, sessions: _FakeSessions | None = None):
    async def main():
        assets = Assets(str(tmp_path / "assets.db"), sessions=sessions or _FakeSessions())
        await assets.start()
        try:
            return await scenario(assets)
        finally:
            await assets.stop()

    return asyncio.run(main())


@pytest.mark.parametrize(("verb", "extra"), BY_ID_CASES)
def test_every_by_id_verb_refuses_an_unauthenticated_asset_id(
    tmp_path, verb: str, extra: dict
) -> None:
    """Knowing an ID alone neither reads nor mutates its record."""

    async def scenario(assets: Assets):
        before = await _publish(assets, TARGET, "protected")
        await _seed_comment(assets, TARGET)
        comments_before = await assets._call(
            "list_comments",
            host="hosta",
            session_name="target",
            asset_id=ASSET_ID,
            unresolved_only=False,
        )
        reply = await assets.asset(
            {
                "type": verb,
                "request_id": "unauthorized",
                "asset_id": ASSET_ID,
                "stream_id": TARGET,
                **extra,
                "_auth_context": {
                    "operator_authenticated": False,
                    "token_verified": False,
                    "stream_id": "",
                    "reason_code": "expired",
                },
            }
        )
        after = await assets._call(
            "get_asset", host="hosta", session_name="target", asset_id=ASSET_ID
        )
        comments_after = await assets._call(
            "list_comments",
            host="hosta",
            session_name="target",
            asset_id=ASSET_ID,
            unresolved_only=False,
        )
        return reply, before, after, comments_before, comments_after

    reply, before, after, comments_before, comments_after = _run(tmp_path, scenario)
    assert reply["error_code"] == "asset_unauthorized", reply
    assert after == before
    assert comments_after == comments_before


@pytest.mark.parametrize(("verb", "extra"), BY_ID_CASES)
def test_authenticated_operator_exact_target_supports_every_by_id_verb(
    tmp_path, verb: str, extra: dict
) -> None:
    """The desktop's ten scoped verbs use the exact selected record."""

    async def scenario(assets: Assets):
        await _publish(assets, OTHER, "wrong row")
        await _publish(assets, TARGET, "selected row")
        if "comment_id" in extra:
            await _seed_comment(assets, TARGET)
        return await assets.asset(
            {
                "type": verb,
                "request_id": "operator",
                "asset_id": ASSET_ID,
                "stream_id": TARGET,
                **extra,
                "_auth_context": {
                    "operator_authenticated": True,
                    # A bad simultaneous token cannot subtract operator authority.
                    "token_verified": False,
                    "stream_id": "",
                    "reason_code": "wrong_seat",
                },
            }
        )

    reply = _run(tmp_path, scenario)
    assert reply["type"] == f"{verb}.ok", reply
    if "asset" in reply:
        assert reply["asset"]["title"] == "selected row"
    if "session_key" in reply:
        assert reply["session_key"]["stream_id"] == TARGET


@pytest.mark.parametrize(("verb", "extra"), MOBILE_BY_ID_CASES)
def test_verified_owner_exact_target_supports_every_mobile_by_id_verb(
    tmp_path, verb: str, extra: dict
) -> None:
    """The mobile client's seven shipped target-session shapes remain valid."""

    async def scenario(assets: Assets):
        await _publish(assets, OWNER, "owner row", spec_id=SPEC_ID)
        if "comment_id" in extra:
            await _seed_comment(assets, OWNER)
        return await assets.asset(
            {
                "type": verb,
                "request_id": "owner",
                "asset_id": ASSET_ID,
                "stream_id": OWNER,
                "spec_id": SPEC_ID,
                **extra,
                "_auth_context": {
                    "operator_authenticated": False,
                    "token_verified": True,
                    "stream_id": OWNER,
                },
            }
        )

    reply = _run(tmp_path, scenario, sessions=_FakeSessions({OWNER: {"spec_id": SPEC_ID}}))
    assert reply["type"] == f"{verb}.ok", reply


def test_operator_requires_target_and_never_falls_through_same_id(tmp_path) -> None:
    async def scenario(assets: Assets):
        await _publish(assets, OTHER, "other")
        auth = {"operator_authenticated": True, "token_verified": False, "stream_id": ""}
        missing_target = await assets.asset(
            {"type": "asset.get", "request_id": "missing", "asset_id": ASSET_ID,
             "_auth_context": auth}
        )
        exact_miss = await assets.asset(
            {"type": "asset.get", "request_id": "miss", "asset_id": ASSET_ID,
             "stream_id": TARGET, "_auth_context": auth}
        )
        conflicting_target = await assets.asset(
            {"type": "asset.get", "request_id": "conflict", "asset_id": ASSET_ID,
             "stream_id": OTHER, "host": "hosta", "session_name": "target",
             "_auth_context": auth}
        )
        return missing_target, exact_miss, conflicting_target

    missing_target, exact_miss, conflicting_target = _run(tmp_path, scenario)
    assert missing_target["error_code"] == "asset_invalid"
    assert exact_miss["error_code"] == "asset_not_found"
    assert conflicting_target["error_code"] == "asset_invalid"


def test_verified_owner_and_explicit_shared_spec_target_are_authorized(tmp_path) -> None:
    sessions = _FakeSessions({OWNER: {"spec_ids": json.dumps([SPEC_ID])}})

    async def scenario(assets: Assets):
        await _publish(assets, OWNER, "owner")
        await _publish(assets, TARGET, "shared", spec_id=SPEC_ID)
        await _publish(assets, OTHER, "unrelated")
        auth = {"operator_authenticated": False, "token_verified": True, "stream_id": OWNER}
        owner = await assets.asset(
            {"type": "asset.get", "request_id": "owner", "asset_id": ASSET_ID,
             "stream_id": OWNER, "_auth_context": auth}
        )
        shared = await assets.asset(
            {"type": "asset.get", "request_id": "shared", "asset_id": ASSET_ID,
             "stream_id": TARGET, "spec_id": SPEC_ID, "_auth_context": auth}
        )
        refused = await assets.asset(
            {"type": "asset.get", "request_id": "other", "asset_id": ASSET_ID,
             "stream_id": OTHER, "spec_id": SPEC_ID, "_auth_context": auth}
        )
        return owner, shared, refused

    owner, shared, refused = _run(tmp_path, scenario, sessions=sessions)
    assert owner["asset"]["title"] == "owner"
    assert shared["asset"]["title"] == "shared"
    assert refused["error_code"] == "asset_unauthorized"


@pytest.mark.parametrize("authorized_matches", (0, 1, 2))
def test_targetless_verified_owner_shared_spec_fallback_is_unique(
    tmp_path, authorized_matches: int
) -> None:
    sessions = _FakeSessions({OWNER: {"spec_id": SPEC_ID}})

    async def scenario(assets: Assets):
        for index in range(authorized_matches):
            stream_id = f"hosta:shared-{index}"
            await _publish(assets, stream_id, f"shared-{index}")
            await _force_record_spec(assets, stream_id, SPEC_ID)
        await _publish(assets, OTHER, "unrelated", spec_id="spec_other")
        return await assets.asset(
            {
                "type": "asset.get",
                "request_id": "fallback",
                "asset_id": ASSET_ID,
                "_auth_context": {
                    "operator_authenticated": False,
                    "token_verified": True,
                    "stream_id": OWNER,
                },
            }
        )

    reply = _run(tmp_path, scenario, sessions=sessions)
    if authorized_matches == 0:
        assert reply["error_code"] == "asset_unauthorized"
    elif authorized_matches == 1:
        assert reply["type"] == "asset.get.ok"
        assert reply["asset"]["title"] == "shared-0"
    else:
        assert reply["error_code"] == "asset_spec_ambiguous"


def test_targetless_owner_row_wins_before_shared_spec_collisions(tmp_path) -> None:
    sessions = _FakeSessions({OWNER: {"spec_id": SPEC_ID}})

    async def scenario(assets: Assets):
        await _publish(assets, TARGET, "shared-1")
        await _force_record_spec(assets, TARGET, SPEC_ID)
        await _publish(assets, OTHER, "shared-2")
        await _force_record_spec(assets, OTHER, SPEC_ID)
        await _publish(assets, OWNER, "owner")
        return await assets.asset(
            {"type": "asset.get", "request_id": "owner", "asset_id": ASSET_ID,
             "_auth_context": {"operator_authenticated": False, "token_verified": True,
                               "stream_id": OWNER}}
        )

    reply = _run(tmp_path, scenario, sessions=sessions)
    assert reply["type"] == "asset.get.ok"
    assert reply["asset"]["title"] == "owner"


def test_wire_spec_claim_without_server_owned_spec_grants_nothing(tmp_path) -> None:
    sessions = _FakeSessions({OWNER: {"spec_id": "spec_owner"}})

    async def scenario(assets: Assets):
        await _publish(assets, TARGET, "claimed", spec_id=SPEC_ID)
        return await assets.asset(
            {"type": "asset.get", "request_id": "claim", "asset_id": ASSET_ID,
             "stream_id": TARGET, "spec_id": SPEC_ID,
             "_auth_context": {"operator_authenticated": False, "token_verified": True,
                               "stream_id": OWNER}}
        )

    reply = _run(tmp_path, scenario, sessions=sessions)
    assert reply["error_code"] == "asset_unauthorized"


def test_conflicting_or_unverified_identity_claim_fails_closed(tmp_path) -> None:
    async def scenario(assets: Assets):
        await _publish(assets, TARGET, "protected")
        return await assets.asset(
            {
                "type": "asset.get",
                "request_id": "wrong-seat",
                "asset_id": ASSET_ID,
                "stream_id": TARGET,
                "from_stream_id": TARGET,
                "_auth_context": {
                    "operator_authenticated": False,
                    "token_verified": False,
                    "stream_id": "",
                    "reason_code": "wrong_seat",
                },
            }
        )

    reply = _run(tmp_path, scenario)
    assert reply["error_code"] == "asset_unauthorized"
