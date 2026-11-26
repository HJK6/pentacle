import asyncio
from assistant_composite import AssistantComposite, AssistantCompositeConfig
from sessions import VerbError
from test_account_self_recovery import _external
from test_account_schedule_journey import fixture, SPEC, LEAD
from test_account_rotation import binding

def test_implicit_provisioning_is_still_pending_at_health(tmp_path):
    async def run():
        store, runtime, pane, ctl, _, message = await fixture(tmp_path)
        try:
            host, name = LEAD.split(":")
            seat = await store.fetch_session(host, name)
            desk = AssistantComposite(store, config=AssistantCompositeConfig(
                enabled=True, name="reviewdesk", stream_id="desk:assistant", direct_primary_stream_id=LEAD,
                direct_primary_generation=seat["session_generation"]))
            await desk.load_binding()
            ctl.sessions.assistant.front_desk = desk
            a = await runtime.run(lambda db: db.bound(binding("spare", host="localhost").key))
            await runtime.record_failure(a, "auth")
            msg = message("spawn", spec_id=SPEC, provider="codex", account="spare", model="gpt-6.2-sol",
                          effort="high", role="lead", objective="review fixture")
            spec = await ctl._resolve_spec_binding(msg, "localhost", "review-lane")
            privileged = await ctl._lane_admission(msg, spec, "lead")
            assert privileged is True
            print("effective_provisioning", privileged, "explicit_flag", "provision_only" in msg)
            try:
                await _external(lambda value: ctl.spawn(value, "localhost"), "spawn", msg)
            except VerbError as exc:
                print("refused", str(exc), "marks", len(runtime._self_recovery_marks), "launches", pane.launches)
                assert exc.code == "account_unavailable"
            else:
                raise AssertionError("expected refusal")
            assert runtime._self_recovery_marks == {}
        finally:
            store.stop()
    asyncio.run(run())



def test_provisioning_form_matches_lane_admission():
    from itertools import product
    from spawnctl import SpawnCtl
    from test_account_schedule_journey import SPEC
    from store_specs import UNASSIGNED

    class Store:
        async def lane_provisioning_allowed(self, _auth):
            return True

    class Owner:
        store = Store()
        _prompt_requested = staticmethod(SpawnCtl._prompt_requested)
        _provisioning_form = SpawnCtl._provisioning_form
        _provisioning_applies = SpawnCtl._provisioning_applies

    owner = Owner()
    prompts = [{}, {"initial_prompt": ""}, {"initial_prompt": "work"}, {"brief": "work"}]
    commands = [{}, {"command": " "}, {"command": "run"}]
    bindings = [{"assigned_spec_id": SPEC}, {"spec_ids": [SPEC]}, None, {},
                {"assigned_spec_id": UNASSIGNED, "spec_ids": [SPEC]}, {"spec_ids": [SPEC, SPEC + "_other"]}]
    for role, prompt, command, binding in product(["lead", "nexus", "worker", "qa", None], prompts, commands, bindings):
        msg = {**prompt, **command}
        privileged = asyncio.run(SpawnCtl._lane_admission(owner, msg, binding, role))
        assert bool(privileged) == owner._provisioning_applies(msg, binding, role), (role, prompt, command, binding)
