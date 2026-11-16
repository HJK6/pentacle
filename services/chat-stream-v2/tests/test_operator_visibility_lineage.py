"""Composed admission, first-open, launch and inventory visibility contract."""
from __future__ import annotations

import asyncio
import json

import pytest

from server import Server
from sessions import Sessions
from spawnctl import SpawnCtl
from store import Store
from test_assistant_role import _FdCatalog, FD_SPEC, OPERATOR, _front_desk_composite, _spy_operator_facing
from test_spawn_resume import _machine
from test_spawn_starting_durable_handle import StartingPaneTmux


class RecordingTmux(StartingPaneTmux):
    def __init__(self):
        super().__init__()
        self.commands = []
        self.screen = "READY"
        self.names = set()

    async def new_session(self, name, command, cwd=None, env=None):
        self.commands.append(command)
        await super().new_session(name, command, cwd=cwd, env=env)
        self.names.add(name)

    async def has_session(self, name):
        return name in self.names

    async def session_state(self, name):
        return "alive" if name in self.names else "gone"

    async def kill_session(self, name):
        self.names.discard(name)

    async def stage_text(self, path, data):
        pass

    async def paste(self, name, text):
        await super().paste(name, text)
        if getattr(self, "store", None) is not None:
            await self.store.append_session_event(f"localhost:{name}", {
                "stream_id": f"localhost:{name}", "provider": "claude", "kind": "USER", "text": text,
                "timestamp": "2026-11-08T12:00:00Z",
            }, identity=f"recorded-input:{name}", limit=500)


@pytest.mark.parametrize("source_visibility,requested,parented,role,recovery", [
    ("default", "default", False, "planner", True),  # legacy CLI-shaped observed defect
    ("default", None, False, "planner", True),
    ("hidden", None, False, "worker", False),
    ("hidden", "default", False, "worker", False),
    ("default", "hidden", False, "worker", False),
    ("default", "nested", False, "worker", False),
    (None, "default", True, "worker", False),
    (None, None, True, "worker", False),
    (None, None, True, "nexus", False),
    (None, None, False, "worker", False),
])
def test_composed_visibility(monkeypatch, tmp_path, source_visibility, requested, parented, role, recovery):
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)
    monkeypatch.delenv("PENTACLE_ASSISTANT_DAFF_ROLE", raising=False)
    launches = _spy_operator_facing(monkeypatch)

    async def run():
        store = Store(":memory:")
        store.start()
        try:
            tmux = RecordingTmux()
            sessions = Sessions(store, tmux=tmux, local_host="localhost")
            composite = _front_desk_composite(store, sessions)
            await store.open_session("localhost", "fd", visibility="hidden")
            source = await store.open_session(
                "localhost", "old", provider="claude", role=role,
                parent_stream_id="localhost:fd", visibility=source_visibility or "hidden",
                effective_model="claude-opus-5-5", effective_effort="medium",
                **({"spec_id": FD_SPEC, "spec_ids": [FD_SPEC], "qualified_spec_ids": [FD_SPEC],
                    "spec_binding_provenance": [{"spec_id": FD_SPEC, "provenance": "parent_inherited",
                                                "granting_principal": "localhost:fd",
                                                "granted_at": "2026-11-08T00:00:00Z"}],
                    "assigned_spec_id": "spec_example__ui"} if recovery else {}),
            )
            await sessions.refresh()
            ctl = SpawnCtl(store, sessions, tmux=tmux, machine=_machine(tmp_path), specs=_FdCatalog())
            commands = tmux.commands
            first_open = []
            retained = []
            real_open = sessions.open

            async def record_open(host, name, **fields):
                first_open.append(dict(fields))
                reservations = await store.reservations(include_expired=True)
                retained.append(next(r["payload"]["open_fields"] if isinstance(r["payload"], dict)
                                     else json.loads(r["payload"])["open_fields"]
                                     for r in reservations if r["session_name"] == name))
                return await real_open(host, name, **fields)

            monkeypatch.setattr(sessions, "open", record_open)
            msg = {"provider": "claude", "model": "claude-opus-5-5", "effort": "medium",
                   "role": role, "ready_marker": "READY", "request_id": "visibility-journey", "_auth_context": OPERATOR,
                   "objective": "Preserve the requested conversation"}
            if source_visibility is not None:
                msg.update(handoff=True, handoff_from_stream_id="localhost:old")
            if parented:
                msg["parent_stream_id"] = "localhost:old"
            if requested is not None:
                msg["visibility"] = requested
            result = await ctl.spawn(dict(msg), "localhost")
            await asyncio.gather(*list(ctl._background_spawns))
            row = await store.fetch_session(*result["stream_id"].split(":", 1))
            server = Server(store=store, sessions=sessions, local_host="localhost")
            server.assistant_composite = composite
            ordinary = server._filter_sessions_for_client(sessions.list_open(), False, None, True)
            diagnostic = server._filter_sessions_for_client(sessions.list_open(), True, None, True)
            expected = requested or source_visibility or ("hidden" if parented else "default")
            trace = {"request": msg, "first_open": first_open, "launch_operator_facing": launches,
                     "reply": result, "row": row, "inventory": ordinary, "diagnostic": diagnostic}
            print("VISIBILITY_TRACE=" + json.dumps(trace, default=str))
            assert result["type"] == "spawn.ok"
            assert first_open[0]["visibility"] == expected
            assert retained[0]["visibility"] == expected
            assert row["visibility"] == result["session"]["visibility"] == expected
            assert launches == [expected == "default"]
            assert (result["stream_id"] in {r["stream_id"] for r in ordinary}) == (expected == "default")
            assert {r["stream_id"]: r["visibility"] for r in diagnostic}[result["stream_id"]] == expected
            assert not sessions.assistant.is_front_desk_seat(result["stream_id"], row)
            if recovery:
                before = await composite.store.get_assistant_binding(env_binding={})
                with pytest.raises(ValueError, match="assistant_rebind_unauthorized"):
                    await store.rebind_assistant(
                        env_binding={}, actor_stream_id=row["stream_id"], actor_generation=row["session_generation"],
                        target_stream_id=row["stream_id"], target_generation=row["session_generation"],
                        request_id="refuse-inherited-successor", expected_revision=before["revision"],
                        authorized_spec_ids=frozenset({FD_SPEC}))
                assert await store.get_assistant_binding(env_binding={}) == before
            # Changing the predecessor after admission cannot change replay's initial decision.
            await store.submit(lambda c: (c.execute("UPDATE sessions SET visibility='nested' WHERE session_name='old'"), c.commit()))
            restarted = SpawnCtl(store, sessions, tmux=tmux, machine=ctl.machine, specs=_FdCatalog())
            replay = await restarted.spawn(dict(msg), "localhost")
            assert replay["stream_id"] == result["stream_id"]
            assert replay["initial_prompt_delivery"] == (await store.get_spawn_outcome(
                *result["stream_id"].split(":", 1)))["delivery_receipt"]
            assert len(commands) == len(first_open) == 1
            assert (await store.fetch_session(*result["stream_id"].split(":", 1)))["visibility"] == expected
            await store.submit(lambda c: (c.execute(
                "UPDATE sessions SET visibility='nested' WHERE host=? AND session_name=?",
                result["stream_id"].split(":", 1)), c.commit()))
            await sessions.refresh()
            current_replay = await restarted.spawn(dict(msg), "localhost")
            assert current_replay["stream_id"] == result["stream_id"]
            assert current_replay["initial_prompt_delivery"] == replay["initial_prompt_delivery"]
            assert (await store.fetch_session(*result["stream_id"].split(":", 1)))["visibility"] == "nested"
            assert len(commands) == 1
        finally:
            store.stop()

    asyncio.run(run())


UI_SPEC = "spec_example__ui"


class LineageCatalog(_FdCatalog):
    @staticmethod
    def resolution_for(spec_id):
        return "resolved" if spec_id in {FD_SPEC, UI_SPEC} else "zero_matches"


async def scheduled_fixture(monkeypatch, tmp_path, *, recovery_spec=FD_SPEC, specs=None):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from window_schedule import WindowSchedule
    from work_lane_link import link_specs

    store = Store(":memory:")
    store.start()
    tmux = RecordingTmux()
    tmux.store = store
    sessions = Sessions(store, tmux=tmux, local_host="localhost")
    _front_desk_composite(store, sessions)
    if recovery_spec != FD_SPEC:
        from assistant_composite import AssistantComposite, AssistantCompositeConfig
        sessions.assistant.front_desk = AssistantComposite(store, config=AssistantCompositeConfig(
            enabled=True, stream_id="fixture-chat:assistant",
            rebind_authorized_spec_ids=frozenset({recovery_spec})))
    owner = await store.open_session(
        "localhost", "owner", provider="claude", visibility="default", pane_status="pane_alive",
        effective_model="claude-opus-5-5", effective_effort="medium",
        spec_id=UI_SPEC, spec_ids=[UI_SPEC, recovery_spec], qualified_spec_ids=[UI_SPEC, recovery_spec],
        spec_binding_provenance=[{"spec_id": sid, "provenance": "parent_inherited",
                                  "granting_principal": "localhost:fd", "granted_at": "2026-11-08T00:00:00Z"}
                                 for sid in (UI_SPEC, recovery_spec)], assigned_spec_id=UI_SPEC)
    await link_specs(store, recovery_spec, UI_SPEC)
    await sessions.refresh()
    ctl = SpawnCtl(store, sessions, tmux=tmux, machine=_machine(tmp_path), specs=specs or LineageCatalog())
    ctl.visibility_launches = _spy_operator_facing(monkeypatch)
    ctl.visibility_opens = []
    real_open = sessions.open
    async def record_open(host, name, **fields):
        retained = next(r for r in await store.reservations(include_expired=True) if r["session_name"] == name)
        payload = retained["payload"]
        payload = json.loads(payload) if isinstance(payload, str) else payload
        ctl.visibility_opens.append({"first_open": dict(fields), "retained_intent": payload["open_fields"]})
        return await real_open(host, name, **fields)
    monkeypatch.setattr(sessions, "open", record_open)
    # The external provider is recorded. Admission, intent, generation fencing,
    # first open, native USER-event delivery proof and receipts all run normally.
    monkeypatch.setattr(ctl, "_await_provider_ready", AsyncMock(return_value=SimpleNamespace(ready=True)))
    surface = WindowSchedule(store, sessions, None, ctl, local_host="localhost")
    surface.mark_store_ready()
    auth = {"token_verified": True, "stream_id": "localhost:owner", "session_generation": owner["session_generation"]}
    return store, sessions, ctl, surface, auth


async def scheduled_child(surface, ctl, sessions, schedule_id):
    result = await surface._fire_schedule(schedule_id)
    await asyncio.gather(*list(ctl._background_spawns))
    # Resolve the actual spawned artifact, not the independent lane owner that
    # now also belongs to the full live-session census.
    child = sessions.get(result["spawn"]["stream_id"])
    assert child is not None and child["host"] == "localhost"
    assert len(ctl.visibility_opens) == 1
    return child


@pytest.mark.parametrize("mode", ["once", "recurring-create", "recurring-update"])
@pytest.mark.parametrize("folder_repo,declared_repo", [
    ("example-recovery", "example_recovery"),
    ("example_recovery", "example-recovery"),
])
@pytest.mark.parametrize("prefix", ["", "spec_"])
def test_scheduled_recovery_alias_retains_original_selection(
    monkeypatch, tmp_path, mode, folder_repo, declared_repo, prefix,
):
    """Use a real document identity snapshot, not a family-equivalence stub."""
    import uuid
    from datetime import datetime, timedelta, timezone
    from _shared.specs_service import SpecsSubsystem

    recovery = f"spec_{declared_repo}__cold"
    alias = f"{prefix}{folder_repo}__cold"
    memory = tmp_path / "alias-memory"
    for folder, identity in [("example__ui", UI_SPEC), (f"{folder_repo}__cold", recovery)]:
        path = memory / "work" / "in_progress" / folder / "spec.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nid: {identity}\n---\n")
    specs = SpecsSubsystem(memory_root=memory.resolve(), session_summaries=lambda: [],
                           changed_callback=lambda ids: None)

    async def run():
        store, sessions, ctl, surface, auth = await scheduled_fixture(
            monkeypatch, tmp_path, recovery_spec=recovery, specs=specs)
        try:
            base = {"from_stream_id": "localhost:owner", "_auth_context": auth}
            request = {**base, "request_id": str(uuid.uuid4()), "provider": "claude",
                       "model": "claude-opus-5-5", "effort": "medium", "visibility": "default",
                       "spec_ids": [UI_SPEC, alias], "objective": "Run explicitly selected recovery",
                       "initial_prompt": "Continue the admitted recovery."}
            if mode == "once":
                request["fires_at_utc"] = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
                created = await surface.schedule_insert(request)
                schedule_id = created["schedule"]["schedule_id"]
            else:
                request.update(name="Explicit recovery alias", every="daily", at_time="02:00", parent="none")
                if mode == "recurring-update":
                    request.pop("spec_ids")  # Owner fallback must not prove recovery.
                created = await surface.recurring_create(request)
                recurring_id = created["recurring"]["recurring_id"]
                if mode == "recurring-update":
                    original = await surface._get_receipt(request["request_id"], "row_committed")
                    assert json.loads(original["measured_state_json"])["configured_recovery_explicit_spec_ids"] == []
                    await surface.recurring_update({**base, "recurring_id": recurring_id,
                                                    "spec_ids": [UI_SPEC, alias]})
                occurrence = await surface.recurring_run({**base, "recurring_id": recurring_id})
                schedule_id = occurrence["schedule_id"]
            child = await scheduled_child(surface, ctl, sessions, schedule_id)
            assert child["qualified_spec_ids"] == [UI_SPEC, recovery]
            assert child["visibility"] == "hidden"
            assert ctl.visibility_launches == [False]
            assert ctl.visibility_opens[0]["first_open"]["visibility"] == "hidden"
            assert ctl.visibility_opens[0]["retained_intent"]["visibility"] == "hidden"
            provenance = {entry["spec_id"]: entry["provenance"] for entry in child["spec_binding_provenance"]}
            assert provenance[recovery] == "spawn_explicit"
            assert child["assigned_spec_id"] == UI_SPEC
            before = await store.get_assistant_binding(env_binding={})
            rebound = await store.rebind_assistant(
                env_binding={}, actor_stream_id=child["stream_id"], actor_generation=child["session_generation"],
                target_stream_id=child["stream_id"], target_generation=child["session_generation"],
                request_id="explicit-alias-recovery", expected_revision=before["revision"],
                authorized_spec_ids=frozenset({recovery}))
            assert rebound["new_binding"]["stream_id"] == child["stream_id"]
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("explicit,corruption", [
    (False, None), (True, None), (True, "missing"), (True, "expired"),
    (True, "generation"), (True, "target"), (True, "template"), (True, "malformed"),
])
def test_scheduled_fresh_recovery_requires_original_receipt_selection(monkeypatch, tmp_path, explicit, corruption):
    import uuid
    from datetime import datetime, timedelta, timezone

    async def run():
        store, sessions, ctl, surface, auth = await scheduled_fixture(monkeypatch, tmp_path)
        try:
            request = {"request_id": str(uuid.uuid4()), "from_stream_id": "localhost:owner", "_auth_context": auth,
                       "objective": "Open the scheduled conversation", "provider": "claude",
                       "model": "claude-opus-5-5", "effort": "medium", "visibility": "default",
                       "fires_at_utc": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
            if explicit:
                request["spec_ids"] = [UI_SPEC, FD_SPEC]
            created = await surface.schedule_insert(request)
            sid = created["schedule"]["schedule_id"]
            receipt = await surface._get_receipt(request["request_id"], "row_committed")
            measured = json.loads(receipt["measured_state_json"])
            assert measured["configured_recovery_explicit_spec_ids"] == ([FD_SPEC] if explicit else [])
            if corruption:
                def corrupt(conn):
                    if corruption == "missing":
                        measured.pop("configured_recovery_explicit_spec_ids")
                    elif corruption == "generation":
                        measured["owner_session_generation"] = "wrong-generation"
                    elif corruption == "template":
                        measured["configured_recovery_template_sha256"] = "0" * 64
                    elif corruption == "malformed":
                        measured["configured_recovery_explicit_spec_ids"] = {"forged": FD_SPEC}
                    conn.execute("UPDATE v2_operation_receipts SET measured_state_json=? WHERE receipt_id=?",
                                 (json.dumps(measured), receipt["receipt_id"]))
                    if corruption == "expired":
                        conn.execute("UPDATE v2_operation_receipts SET retain_until='2000-01-01' WHERE receipt_id=?", (receipt["receipt_id"],))
                    if corruption == "target":
                        conn.execute("UPDATE v2_operation_receipts SET target_id='foreign' WHERE receipt_id=?", (receipt["receipt_id"],))
                    conn.commit()
                await store.submit(corrupt)
            child = await scheduled_child(surface, ctl, sessions, sid)
            proved = explicit and corruption is None
            assert child["visibility"] == ("hidden" if proved else "default")
            assert ctl.visibility_launches == [not proved]
            assert ctl.visibility_opens[0]["first_open"]["visibility"] == child["visibility"]
            assert ctl.visibility_opens[0]["retained_intent"]["visibility"] == child["visibility"]
            provenance = {e["spec_id"]: e["provenance"] for e in child["spec_binding_provenance"]}
            assert provenance[FD_SPEC] == ("spawn_explicit" if proved else "parent_inherited")
            assert provenance[UI_SPEC] == "spawn_explicit"
            assert child["assigned_spec_id"] == UI_SPEC
            assert len(ctl.tmux.commands) == 1
            from sessions import VerbError
            with pytest.raises(VerbError, match="already claimed"):
                await surface._fire_schedule(sid)
            assert len(ctl.tmux.commands) == 1
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("explicit,update_selection,damage", [
    (False, None, None), (True, None, None), (False, True, None), (True, False, None),
    (True, None, "missing"), (True, None, "expired"), (True, None, "owner-generation"),
    (True, None, "occurrence"),
])
def test_recurring_update_preserves_only_proved_explicit_subset(monkeypatch, tmp_path, explicit, update_selection, damage):
    import uuid

    async def run():
        store, sessions, ctl, surface, auth = await scheduled_fixture(monkeypatch, tmp_path)
        try:
            base = {"from_stream_id": "localhost:owner", "_auth_context": auth}
            request = {**base, "request_id": str(uuid.uuid4()), "name": "Recovery selection control",
                       "every": "daily", "at_time": "02:00", "parent": "none", "provider": "claude",
                       "model": "claude-opus-5-5", "effort": "medium", "visibility": "default",
                       "initial_prompt": "Continue the admitted ordinary work.", "objective": "Run the admitted template"}
            if explicit:
                request["spec_ids"] = [UI_SPEC, FD_SPEC]
            created = await surface.recurring_create(request)
            rid = created["recurring"]["recurring_id"]
            if damage and damage != "occurrence":
                receipt = await surface._get_receipt(request["request_id"], "row_committed")
                measured = json.loads(receipt["measured_state_json"])
                if damage == "missing":
                    measured.pop("configured_recovery_explicit_spec_ids")
                elif damage == "owner-generation":
                    measured["owner_session_generation"] = "wrong-generation"
                await store.submit(lambda conn: (conn.execute(
                    "UPDATE v2_operation_receipts SET measured_state_json=?,retain_until=? WHERE receipt_id=?",
                    (json.dumps(measured), "2000-01-01" if damage == "expired" else receipt["retain_until"],
                     receipt["receipt_id"])), conn.commit()))
            edit = {**base, "recurring_id": rid, "initial_prompt": "Run the updated ordinary work."}
            if update_selection is not None:
                edit["spec_ids"] = [UI_SPEC, FD_SPEC] if update_selection else [UI_SPEC]
            await surface.recurring_update(edit)
            occurrence = await surface.recurring_run({**base, "recurring_id": rid})
            if damage == "occurrence":
                def corrupt(conn):
                    receipt = conn.execute("SELECT * FROM v2_operation_receipts WHERE target_id=? AND phase='row_committed'",
                                           (occurrence["schedule_id"],)).fetchone()
                    measured = json.loads(receipt["measured_state_json"])
                    measured["recurring_id"] = str(uuid.uuid4())
                    conn.execute("UPDATE v2_operation_receipts SET measured_state_json=? WHERE receipt_id=?",
                                 (json.dumps(measured), receipt["receipt_id"]))
                    conn.commit()
                await store.submit(corrupt)
            child = await scheduled_child(surface, ctl, sessions, occurrence["schedule_id"])
            proved = (explicit if update_selection is None else update_selection) and damage is None
            assert child["visibility"] == ("hidden" if proved else "default")
            assert ctl.visibility_launches == [not proved]
            assert ctl.visibility_opens[0]["first_open"]["visibility"] == child["visibility"]
            assert ctl.visibility_opens[0]["retained_intent"]["visibility"] == child["visibility"]
            provenance = {e["spec_id"]: e["provenance"] for e in child["spec_binding_provenance"]}
            if FD_SPEC in provenance:
                assert provenance[FD_SPEC] == ("spawn_explicit" if proved else "parent_inherited")
            assert child["assigned_spec_id"] == UI_SPEC
            assert len(ctl.tmux.commands) == 1
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("visibility,requested,bound", [
    ("default", None, False), ("hidden", None, False), ("hidden", "default", False),
    ("default", "hidden", False), ("hidden", "nested", False), ("default", "default", False),
    ("hidden", None, True),
])
def test_scheduled_handoff_uses_inherited_presentation_and_bound_identity(monkeypatch, tmp_path, visibility, requested, bound):
    import uuid
    from datetime import datetime, timedelta, timezone

    async def run():
        store, sessions, ctl, surface, auth = await scheduled_fixture(monkeypatch, tmp_path)
        try:
            await store.submit(lambda conn: (conn.execute(
                "UPDATE sessions SET visibility=? WHERE session_name='owner'", (visibility,)), conn.commit()))
            await sessions.refresh()
            if bound:
                await store.update_session("localhost", "owner", assigned_spec_id="")
                _front_desk_composite(store, sessions, bound=("localhost:owner", auth["session_generation"]))
            request = {"request_id": str(uuid.uuid4()), "from_stream_id": "localhost:owner", "_auth_context": auth,
                       "objective": "Continue the scheduled conversation", "handoff": True,
                       "handoff_from_stream_id": "localhost:owner", "visibility": requested,
                       "fires_at_utc": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
            created = await surface.schedule_insert(request)
            child = await scheduled_child(surface, ctl, sessions, created["schedule"]["schedule_id"])
            expected = "hidden" if bound else requested or visibility
            assert child["visibility"] == expected
            assert ctl.visibility_launches == [expected == "default"]
            assert ctl.visibility_opens[0]["first_open"]["visibility"] == expected
            assert ctl.visibility_opens[0]["retained_intent"]["visibility"] == expected
            if bound:
                proof = await store.submit(lambda conn: conn.execute(
                    "SELECT 1 FROM v2_assistant_direct_handoff_proofs WHERE predecessor_stream_id=? AND predecessor_generation=? "
                    "AND successor_stream_id=? AND successor_generation=?",
                    ("localhost:owner", auth["session_generation"], child["stream_id"], child["session_generation"])).fetchone())
                assert proof is not None
        finally:
            store.stop()
    asyncio.run(run())


def test_untrusted_recovery_projection_cannot_authorize_schedule_fallback(monkeypatch, tmp_path):
    async def run():
        store, sessions, ctl, surface, auth = await scheduled_fixture(monkeypatch, tmp_path)
        try:
            msg = {"spec_ids": [UI_SPEC, FD_SPEC], "configured_recovery_explicit_spec_ids": [FD_SPEC],
                   "_configured_recovery_explicit_spec_ids": [FD_SPEC],
                   "_auth_context": {"service_authenticated": True, "service_actor": "daemon:scheduler"}}
            resolved = await ctl._resolve_spec_binding(msg, "localhost", "scheduled")
            by_id = {e["spec_id"]: e["provenance"] for e in resolved["spec_binding_provenance"]}
            assert by_id[FD_SPEC] == "parent_inherited"
            assert by_id[UI_SPEC] == "spawn_explicit"
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("prior_visibility,requested", [
    ("hidden", None), ("default", None), ("hidden", "default"),
    ("default", "hidden"), ("default", "nested"),
])
def test_native_resume_uses_effective_visibility_at_launch_and_first_open(monkeypatch, tmp_path, prior_visibility, requested):
    from test_spawn_resume import RESUME_ID, _closed_prior, _write_transcript

    launches = _spy_operator_facing(monkeypatch)

    async def run():
        store = Store(":memory:")
        store.start()
        try:
            machine = _machine(tmp_path)
            path = _write_transcript(machine)
            old = await _closed_prior(store, path)
            await store.submit(lambda conn: (conn.execute(
                "UPDATE sessions SET visibility=? WHERE session_name=?", (prior_visibility, old["session_name"])), conn.commit()))
            tmux = RecordingTmux()
            sessions = Sessions(store, tmux=tmux, local_host="localhost")
            await sessions.refresh()
            ctl = SpawnCtl(store, sessions, tmux=tmux, machine=machine)
            fields = []
            real_open = sessions.open
            async def record_open(host, name, **values):
                fields.append(dict(values))
                return await real_open(host, name, **values)
            monkeypatch.setattr(sessions, "open", record_open)
            msg = {"provider": "claude", "model": "claude-opus-5-5", "effort": "medium",
                   "resume_session_id": RESUME_ID, "request_id": "resume-visibility", "ready_marker": "READY",
                   "_auth_context": OPERATOR}
            if requested is not None:
                msg["visibility"] = requested
            result = await ctl.spawn(msg, "localhost")
            await asyncio.gather(*list(ctl._background_spawns))
            expected = requested or prior_visibility
            assert result["stream_id"] == old["stream_id"]
            assert fields[0]["visibility"] == sessions.get(old["stream_id"])["visibility"] == expected
            assert launches == [expected == "default"]
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("requested", ["default", "nested", None])
@pytest.mark.parametrize("handoff", [False, True])
def test_genuine_daff_hides_without_rewriting_request_identity(monkeypatch, tmp_path, requested, handoff):
    monkeypatch.setenv("PENTACLE_ASSISTANT_DAFF_ROLE", "partner-assistant")
    launches = _spy_operator_facing(monkeypatch)

    async def run():
        store = Store(":memory:")
        store.start()
        try:
            tmux = RecordingTmux()
            sessions = Sessions(store, tmux=tmux, local_host="localhost")
            ctl = SpawnCtl(store, sessions, tmux=tmux, machine=_machine(tmp_path))
            msg = {"provider": "claude", "model": "claude-opus-5-5", "effort": "medium",
                   "role": "partner-assistant", "objective": "Continue the protected assistant",
                   "ready_marker": "READY", "request_id": "hidden-daff-wire", "_auth_context": OPERATOR}
            if handoff:
                # Historical protected rows can still carry visible presentation.
                await sessions.open("localhost", "prior-daff", role="partner-assistant", visibility="default",
                                    provider="claude", effective_model="claude-opus-5-5", effective_effort="medium")
                msg.pop("role")
                msg.update(handoff=True, handoff_from_stream_id="localhost:prior-daff")
            if requested is not None:
                msg["visibility"] = requested
            original_hash = ctl._spawn_payload_hash(msg)
            reply = await ctl.spawn(msg, "localhost")
            await asyncio.gather(*list(ctl._background_spawns))
            outcome = await store.get_spawn_outcome(*reply["stream_id"].split(":", 1))
            assert outcome["request_payload_hash"] == original_hash
            assert reply["session"]["visibility"] == "hidden"
            assert launches == [False]
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("provenance,principal,qualified,handoff", [
    ("parent_inherited", "operator", [FD_SPEC], False),
    ("handoff_inherited", "operator", [FD_SPEC], False),
    ("operator_v2", "operator", [FD_SPEC], False),
    ("spawn_explicit", " ", [FD_SPEC], False),
    (["spawn_explicit"], "operator", [FD_SPEC], False),
    ("spawn_explicit", "operator", [UI_SPEC], False),
    ("spawn_explicit", "operator", [FD_SPEC], True),
])
def test_cold_recovery_rejects_unproved_identity(monkeypatch, provenance, principal, qualified, handoff):
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, local_host="localhost")
            _front_desk_composite(store, sessions)
            fields = {"visibility": "default", "qualified_spec_ids": qualified,
                      "spec_binding_provenance": [{"spec_id": FD_SPEC, "provenance": provenance,
                                                   "granting_principal": principal,
                                                   "granted_at": "2026-11-08T00:00:00Z"}]}
            if handoff:
                fields["handoff_from_stream_id"] = "localhost:unrelated"
            assert not sessions.assistant.is_front_desk_seat("localhost:candidate", fields)
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("provenance,handoff,same_seat,retarget,clear,allowed", [
    ("handoff_inherited", False, False, False, False, False),
    ("parent_inherited", False, False, False, False, False),
    ("spawn_explicit", True, False, False, False, False),
    ("drive_explicit", True, False, False, False, False),
    ("handoff_inherited", True, True, False, False, True),
    ("handoff_inherited", True, True, True, False, False),
    ("handoff_inherited", True, True, False, True, False),
    ("spawn_explicit", False, False, False, False, True),
])
def test_transactional_recovery_boundary(provenance, handoff, same_seat, retarget, clear, allowed):
    from test_assistant_hot_rebind import _persist_recovery_claim

    async def run():
        store = Store(":memory:")
        store.start()
        try:
            old = await store.open_session("localhost", "old", pane_status="pane_alive")
            actor_id = "localhost:old" if same_seat else "localhost:other"
            actor = await store.open_session(
                *actor_id.split(":", 1), provider="claude", pane_status="pane_alive", visibility="hidden",
                effective_model="claude-opus-5-5", effective_effort="high",
                handoff_from_stream_id="localhost:previous" if handoff else None)
            await _persist_recovery_claim(store, actor_id, [FD_SPEC], [{
                "spec_id": FD_SPEC, "provenance": provenance, "granting_principal": "operator",
                "granted_at": "2026-11-08T00:00:00Z"}])
            kwargs = dict(env_binding={"stream_id": "localhost:old", "generation": old["session_generation"]},
                          actor_stream_id=actor_id, actor_generation=actor["session_generation"],
                          target_stream_id="localhost:target" if retarget else actor_id,
                          target_generation=actor["session_generation"], clear=clear,
                          request_id="recovery-boundary", expected_revision=0,
                          authorized_spec_ids=frozenset({FD_SPEC}))
            if allowed:
                result = await store.rebind_assistant(**kwargs)
                assert result["new_binding"]["stream_id"] == actor_id
            else:
                before = await store.get_assistant_binding(env_binding=kwargs["env_binding"])
                with pytest.raises(ValueError, match="assistant_rebind_unauthorized"):
                    await store.rebind_assistant(**kwargs)
                assert await store.get_assistant_binding(env_binding=kwargs["env_binding"]) == before
        finally:
            store.stop()
    asyncio.run(run())
