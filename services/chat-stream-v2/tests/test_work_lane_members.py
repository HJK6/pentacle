import pytest

from work_lane_members import validate_members, validate_title


def test_order_bounds_unique_and_empty_reason():
    assert validate_members(["spec_demo__b", "spec_demo__a"], None) == (["spec_demo__b", "spec_demo__a"], None)
    assert validate_members([], "  A planning conversation.  ") == ([], "A planning conversation.")
    for members in (["spec_demo__a"] * 2, ["work_demo__a"], ["spec_"], [4], "spec_demo__a",
                    [f"spec_demo__item_{i}" for i in range(33)]):
        with pytest.raises(ValueError, match="work_lane_members_invalid"):
            validate_members(members, None)
    with pytest.raises(ValueError, match="work_lane_no_spec_reason_required"):
        validate_members([], None)
    with pytest.raises(ValueError, match="work_lane_no_spec_reason_invalid"):
        validate_members([], "x" * 281)
    assert validate_members(["spec_demo-kit__bridge"], None)[0] == ["spec_demo-kit__bridge"]
    assert len(validate_members([f"spec_demo__item_{i}" for i in range(32)], None)[0]) == 32


@pytest.mark.parametrize("title", ["Build v2-ab12cd34", "Tracking spec_demo__bridge", "wl-" + "a" * 24,
                                  "Track assistant-lane-ab12", "(spec_demo__bridge)"])
def test_id_like_titles_refused_on_title_writes(title):
    with pytest.raises(ValueError, match="work_lane_title_invalid"):
        validate_title(title)


@pytest.mark.parametrize("title", ["Version 2 release", "Paper bridge", "specification review", "Plan v2-GHIJKLMN"])
def test_narrow_title_matcher_accepts_normal_titles(title):
    assert validate_title(title) == title
def test_fresh_service_imports_do_not_require_pytest_path_bootstrap():
    import os
    from pathlib import Path
    import subprocess
    import sys
    service = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    for module in ("work_lane_members", "store_work_index"):
        subprocess.run([sys.executable, "-c", f"import {module}"], cwd=service,
                       env=env, check=True, capture_output=True, timeout=10)


def test_server_set_members_checks_only_new_catalog_members(tmp_path):
    from server import Server
    from sessions import Sessions, VerbError
    from _shared.specs_service import SpecsSubsystem
    from test_work_lanes import ASSISTANT, FD, run
    from work_lanes_projection import WorkLanesInventory

    folder = tmp_path / "work/in_progress/demo__resolved"
    folder.mkdir(parents=True)
    (folder / "spec.md").write_text("---\nid: spec_demo__resolved\ntitle: Resolved\n---\n")
    scanner = SpecsSubsystem(memory_root=str(tmp_path), session_summaries=lambda: [], changed_callback=lambda _: None)

    async def body(env):
        lane = (await env.adopt(state="paused", owner="operator", members=["spec_demo__existing_missing"]))["lane"]
        sessions = Sessions(env.store, tmux=None, local_host="fixture-root")
        await sessions.refresh()
        server = Server(store=env.store, sessions=sessions, local_host="fixture-root")
        server.assistant_composite = env.composite
        server.work_lanes = WorkLanesInventory(env.store, sessions, server.broadcast, specs=scanner)
        message = {"type": "assistant.operation", "request_id": "new-unresolved", "composite_stream_id": ASSISTANT,
                   "dispatch_id": "none", "operation": "work_lane.set_members", "lane_id": lane["lane_id"],
                   "expected_lane_version": lane["version"],
                   "payload": {"members": ["spec_demo__existing_missing", "spec_demo__unresolved"]},
                   "_auth_context": {"token_verified": True, "stream_id": FD, "session_generation": env.gen}}
        before = await env.store.get_work_lane(lane["lane_id"])
        with pytest.raises(VerbError) as error:
            await server._on_assistant_operation(message)
        assert error.value.code == "work_lane_member_unresolved"
        assert await env.store.get_work_lane(lane["lane_id"]) == before
        message.update(request_id="new-resolved", payload={"members": ["spec_demo__existing_missing", "spec_demo__resolved"]})
        reply = await server._on_assistant_operation(message)
        assert reply["lane"]["version"] == lane["version"] + 1
        assert reply["lane"]["members"] == ["spec_demo__existing_missing", "spec_demo__resolved"]
        # An exact retry replays before CAS, even if its catalog entry vanished.
        (folder / "spec.md").unlink()
        replay = await server._on_assistant_operation(message)
        assert replay["duplicate"] is True and replay["lane"]["version"] == reply["lane"]["version"]
        message.update(request_id="same-members", expected_lane_version=reply["lane"]["version"])
        with pytest.raises(VerbError) as error:
            await server._on_assistant_operation(message)
        assert error.value.code == "work_lane_members_unchanged"
    run(body)
