"""Manual close uses the full indexed lane in the versioned write transaction."""
from contextlib import asynccontextmanager
import hashlib
import json

import pytest

from server import Server
from sessions import Sessions
from store import STREAM_TOKEN_HASH_VERSION
from test_work_lane_completion import complete, seed_route
from test_work_lane_goals import adopt_with_goal, reconcile, scanner, write_goal, write_item
from test_work_lane_progress_socket import cli_call
from test_work_lanes import ASSISTANT, FD, run
from work_lanes_projection import WorkLanesInventory


@asynccontextmanager
async def lane_cli(env, tmp_path, source):
    digest = hashlib.sha256(b"fixture-lane-token").hexdigest()
    assert await env.store.grant_stream_token("fixture-root", "visible", digest, STREAM_TOKEN_HASH_VERSION) == "ok"
    sessions = Sessions(env.store, tmux=None, local_host="fixture-root")
    await sessions.refresh()
    server = Server(host="127.0.0.1", port=0, store=env.store, sessions=sessions, local_host="fixture-root")
    server.assistant_composite = env.composite
    inventory = WorkLanesInventory(env.store, sessions, server.broadcast, specs=source, settle_s=0, refresh_min_s=0)
    server.work_lanes = inventory
    try:
        port = await server.bind()

        async def cli(*args, **kwargs):
            return await cli_call(port, tmp_path, *args, **kwargs)
        yield cli, inventory
    finally:
        await inventory.stop()
        await server.close()


def mutation(verb, lid, version, request, *args):
    return [verb, lid, "--expected-version", str(version), "--request-id", request, *args]


@pytest.mark.parametrize("owner", ["fd", "operator"])
def test_close_park_repair_and_reopen_before_next_refresh(tmp_path, owner):
    root = tmp_path / "memory"
    source = scanner(root)

    async def body(env):
        lead = await env.seat("fixture-lead", role="lead", parent_stream_id=FD)
        lane = await adopt_with_goal(env, owner=owner, state="paused", lead=lead)
        lid = lane["lane_id"]
        write_goal(root, lid)
        for i in range(8):
            write_item(root, f"n{i}", status="completed")
        open_dir = write_item(root, "n8", status="in_progress")
        await reconcile(env.store, source)
        async with lane_cli(env, tmp_path, source) as (cli, inv):
            close = mutation("set-state", lid, 1, "close", "--to", "done", "--outcome", "Work complete",
                             "--reason", "Verified outcome")
            listed = await cli("list", "--members", "--json")
            preview = next(row for row in listed["lanes"] if row["lane_id"] == lid)
            assert preview["members_total"] == 9
            assert "spec_demo__n8" not in [m["spec_id"] for m in preview["members"]]
            refused = await cli(*close, expected_status=1)
            assert refused["error_code"] == "work_lane_close_unfinished"
            assert (refused["lane_version"], refused["member_count"], refused["goal_count"]) == (1, 1, 1)
            assert [(m["spec_id"], m["source"], m["epic_id"]) for m in refused["members"]] == [
                ("spec_demo__n8", "goal", "epic_fixture")]
            shown = await cli("show", lid, "--members", "--json")
            assert shown["members_total"] == 9 and shown["members"][-1]["spec_id"] == "spec_demo__n8"
            assert shown["lane"]["version"] == 1 and all(e["next_state"] != "done" for e in shown["events"])
            parked = await cli(*mutation("set-state", lid, 1, "park", "--to", "paused", "--parked",
                                         "--reason", "Waiting for final work"))
            assert parked["lane"]["version"] == 2
            shown = await cli("show", lid, "--json")
            assert shown["projection"]["parked"]["reason"] == "Waiting for final work"

            open_dir.rename(root / "work" / "completed" / open_dir.name)
            await reconcile(env.store, source)
            stale = await cli(*close, expected_status=1)
            assert stale["error_code"] == "assistant_lane_version_conflict"
            close = mutation("set-state", lid, 2, "finished", "--to", "done", "--outcome", "Work complete",
                             "--reason", "Verified outcome")
            done = await cli(*close)
            assert (done["lane"]["work_state"], done["lane"]["work_state_reason"]) == ("done", "goals_done")
            assert done["event"]["actor_stream_id"] == FD and done["event"]["payload"]["reason"] == "Verified outcome"
            if owner == "operator":
                assert done["event"]["payload"]["override"]["actor_generation"] == env.gen
            write_item(root, "after_close", status="in_progress")
            inv._running = True
            await inv.refresh_for_close()
            replay = await cli(*close)
            assert replay["duplicate"] and replay["event"] == done["event"]
            shown = await cli("show", lid, "--json")
            assert (shown["lane"]["work_state"], shown["lane"]["work_state_reason"]) == ("paused", "reopened")
            assert sum(e["next_state"] == "done" for e in shown["events"]) == 1

    run(body, str(tmp_path / "state.db"))


BANNER_ID = "spec_demo_mobile__duplicate_banners"


def test_missing_members_and_source_repair(tmp_path):
    root = tmp_path / "memory"
    source = scanner(root)
    typo = BANNER_ID.replace("_mobile", "-mobile")

    async def body(env):
        lane = await adopt_with_goal(env, owner="operator", state="paused",
                                     members=["spec_demo__keep", typo])
        lid = lane["lane_id"]
        charter = write_goal(root, lid)
        write_item(root, "keep", status="completed", epic=None)
        banner = write_item(root, "banner", status="completed", epic=None) / "spec.md"
        banner.write_text(banner.read_text().replace("spec_demo__banner", BANNER_ID))
        path = write_item(root, "derived", status="completed")
        await reconcile(env.store, source)
        async with lane_cli(env, tmp_path, source) as (cli, _):
            def close(version):
                return mutation("set-state", lid, version, "repair-close", "--to", "done",
                                "--outcome", "Completed", "--reason", "Verified source")
            missing = await cli(*close(1), expected_status=1)
            assert missing["error_code"] == "work_lane_close_uncertain"
            assert [(m["spec_id"], m["source"]) for m in missing["members"]] == [(typo, "explicit")]
            shown = await cli("show", lid, "--members", "--json")
            explicit = shown["lane"]["explicit_members"]
            assert explicit == ["spec_demo__keep", typo]
            ids = explicit + [spec for spec in shown["lane"]["members"] if spec not in explicit]
            replace = mutation("set-members", lid, 1, "correct-id", "--reason", "Verified canonical banner ID")
            for spec in ids:
                replace.extend(["--member", BANNER_ID if spec == typo else spec])
            repaired = await cli(*replace)
            assert repaired["lane"]["version"] == 2
            assert (await cli(*replace))["duplicate"]
            shown = await cli("show", lid, "--members", "--json")
            assert shown["lane"]["explicit_members"] == ["spec_demo__keep", BANNER_ID]
            assert shown["projection"]["items_unresolved"] == 0

            held = tmp_path / "unreadable-item"
            path.rename(held)
            await reconcile(env.store, source)
            missing = await cli(*close(2), expected_status=1)
            assert missing["error_code"] == "work_lane_close_uncertain"
            assert [(m["spec_id"], m["source"], m["epic_id"]) for m in missing["members"]] == [
                ("spec_demo__derived", "goal", "epic_fixture")]
            held.rename(path)
            original = charter.read_text()
            charter.write_text("---\nid: [invalid\n---\n")
            await reconcile(env.store, source)
            unavailable = await cli(*close(2), expected_status=1)
            assert unavailable["error_code"] == "work_lane_close_uncertain"
            assert unavailable["goals"][0]["epic_id"] == "epic_fixture"
            charter.write_text(original)
            await env.store.mark_work_index_pending()
            unavailable = await cli(*close(2), expected_status=1)
            assert unavailable["error_code"] == "work_lane_close_uncertain" and not unavailable["index_available"]
            await reconcile(env.store, source)
            shown = await cli("show", lid, "--members", "--json")
            assert shown["projection"]["items_unresolved"] == 0 and shown["lane"]["version"] == 2
            assert (await cli(*close(2)))["lane"]["work_state_reason"] == "goals_done"

    run(body, str(tmp_path / "state.db"))


@pytest.mark.parametrize("operation", ["lane.close", "lane.decision"])
def test_terminal_routing_checks_adopted_lane_and_keeps_phase_effect(tmp_path, operation):
    root = tmp_path / "memory"
    source = scanner(root)

    async def body(env):
        lead = await env.seat("routing-lead", role="lead", parent_stream_id=FD)
        lane = await adopt_with_goal(env, owner="fd", state="paused", lead=lead)
        lid = lane["lane_id"]
        write_goal(root, lid)
        dispatch = await seed_route(env, lane, lead)
        if operation == "lane.close":
            await complete(env, lane, lead, dispatch)
            payload = {"completion_message_id": "synthetic-report", "completion_disposition": "accepted"}
            phase, target = "completed", "closed"
        else:
            phase, target = "execution", "cancelled"
            payload = {"transition": "cancel", "decision_id": "cancel", "from_phase": phase,
                       "to_phase": target, "operator_basis_message_ids": [dispatch]}
        (root / "work").mkdir()
        await reconcile(env.store, source)
        before = await env.store.get_work_lane(lid)
        version = before["lane"]["version"]
        args = ["operation", "--operation", operation, "--lane-id", lid, "--expected-lane-version", str(version),
                "--composite-stream-id", ASSISTANT, "--dispatch-id", dispatch,
                "--request-id", "route-final", "--payload", json.dumps(payload)]
        async with lane_cli(env, tmp_path, source) as (cli, _):
            blocked = await cli(*args, command="assistant", expected_status=1)
            assert blocked["error_code"] == "work_lane_close_unfinished", blocked
            assert blocked["member_count"] == 0 and blocked["goals"][0]["epic_id"] == "epic_fixture"
            actual = await env.store.get_work_lane(lid)
            assert actual["lane"] == before["lane"] and actual["events"] == before["events"]
            write_item(root, "resolved", status="completed")
            await reconcile(env.store, source)
            ok = await cli(*args, command="assistant")
            assert ok["lane"]["phase"] == target
            assert (await cli("show", lid, "--json"))["lane"]["work_state"] == "paused"

    run(body, str(tmp_path / "state.db"))


def test_second_open_membership_and_empty_transferred_goal(tmp_path):
    root = tmp_path / "memory"
    source = scanner(root)

    async def body(env):
        spec = "spec_demo__continuing"
        old = await adopt_with_goal(env, state="paused", owner="operator", members=[spec])
        other = await adopt_with_goal(env, key="request:other", name="elsewhere", state="paused", owner="operator")
        original = old["lane_id"]
        write_goal(root, original)
        write_goal(root, other["lane_id"], "elsewhere")
        write_item(root, "continuing", status="in_progress", epic=None)
        # Retained legacy overlapping memberships predate the current exclusivity rule.
        def legacy_overlap(conn):
            conn.execute("UPDATE v2_assistant_composite_lanes SET members_json=? WHERE lane_id=?",
                         (json.dumps([spec]), other["lane_id"]))
            conn.commit()
        await env.store.submit(legacy_overlap)
        await reconcile(env.store, source)
        close = mutation("set-state", original, 1, "final", "--to", "done",
                         "--outcome", "Work moved", "--reason", "Transfer checked")
        async with lane_cli(env, tmp_path, source) as (cli, _):
            blocked = await cli(*close, expected_status=1)
            assert blocked["members"][0]["spec_id"] == spec
            assert blocked["error_code"] == "work_lane_close_unfinished"
            # The authoritative pair moves the continuing item and supersedes its old explicit entry.
            write_item(root, "continuing", status="in_progress", epic="epic_elsewhere")
            await reconcile(env.store, source)
            empty = await cli(*close, expected_status=1)
            assert empty["member_count"] == 0 and [g["epic_id"] for g in empty["goals"]] == ["epic_fixture"]
            write_goal(root, other["lane_id"])
            await reconcile(env.store, source)
            assert (await cli(*close))["lane"]["work_state_reason"] == "goals_done"
            destination = await cli("show", other["lane_id"], "--members", "--json")
            assert destination["lane"]["members"] == [spec] and destination["members"][0]["status"] == "in_progress"

    run(body, str(tmp_path / "state.db"))
