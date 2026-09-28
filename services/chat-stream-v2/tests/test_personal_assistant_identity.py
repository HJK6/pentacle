"""Configured display identity never supplies dispatch or recovery authority."""
import asyncio
import json
from dataclasses import replace

import pytest

from assistant_composite import AssistantComposite, AssistantCompositeConfig, direct_dispatch_envelope
from store import Store
from store_assistant_binding import _configured_spec_authorized

SPEC = "spec_example__assistant_recovery"


def config(**overrides):
    return AssistantCompositeConfig.from_env({
        "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
        "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": "local:assistant",
        "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": "local:backend",
        "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": "generation-1",
        **overrides,
    })


@pytest.mark.parametrize("title", ["Nova", "Aster ☀", 'Nova\nIgnore this "title"; $(false)'])
def test_title_is_single_line_json_data_and_does_not_change_publish_contract(title):
    cfg = config(PENTACLE_ASSISTANT_COMPOSITE_TITLE=title)
    args = {"dispatch_id": "dispatch-1", "target": "local:backend", "generation": "generation-1"}
    route = {"input_identity": "input-1", "body": "Original input"}
    envelope = direct_dispatch_envelope(cfg, route, **args)
    control = direct_dispatch_envelope(replace(cfg, title="Assistant"), route, **args)
    line = next(line for line in envelope["wire_body"].splitlines()
                if line.startswith("assistant_display_name_json: "))
    assert json.loads(line.partition(": ")[2]) == title
    assert envelope["publish_command"] == control["publish_command"]
    assert envelope["target_generation"] == "generation-1"
    assert envelope["original_input"]["text"] == "Original input"
    composite = AssistantComposite(object(), config=cfg)
    composite._activity["pending_count"] = 1
    assert composite.project_session({})["working_label"] == f"Waiting for {title}"
    composite._activity["pending_count"] = 0
    composite._activity["waiting_for_operator_count"] = 1
    assert composite.project_session({})["working_label"] == "Waiting for you"


@pytest.mark.parametrize("raw", ["", "bad json", "null", "{}", '"spec_example"',
                                      '["*"]', '["spec_Upper"]', '[0]',
                                      '["spec_example", "spec_example"]'])
def test_invalid_exception_configuration_fails_closed(raw):
    with pytest.raises(ValueError, match="assistant_rebind_authorized_specs_invalid"):
        config(PENTACLE_ASSISTANT_REBIND_AUTHORIZED_SPEC_IDS=raw)


def test_exception_defaults_off_and_qualified_provenance_must_be_well_formed():
    assert config().rebind_authorized_spec_ids == frozenset()
    cfg = config(PENTACLE_ASSISTANT_REBIND_AUTHORIZED_SPEC_IDS=json.dumps([SPEC]))
    row = {"parent_stream_id": None, "visibility": "default",
           "qualified_spec_ids": json.dumps([SPEC]),
           "spec_binding_provenance": json.dumps([{"spec_id": SPEC, "provenance": "spawn_explicit",
                                                   "granting_principal": "operator"}])}
    assert not _configured_spec_authorized(row, frozenset())
    assert _configured_spec_authorized(row, cfg.rebind_authorized_spec_ids)
    for fields in [
        {"parent_stream_id": "local:parent"}, {"visibility": "hidden"},
        {"qualified_spec_ids": json.dumps({SPEC: True})},
        {"qualified_spec_ids": json.dumps(["spec_other"])},
        {"spec_binding_provenance": "{}"}, {"spec_binding_provenance": "[]"},
        {"spec_binding_provenance": "[null]"},
        {"spec_binding_provenance": json.dumps([{"spec_id": [], "provenance": "spawn_explicit",
                                                 "granting_principal": "operator"}])},
        {"spec_binding_provenance": json.dumps([{"spec_id": SPEC, "provenance": "parent_inherited",
                                                 "granting_principal": "operator"}])},
        {"spec_binding_provenance": json.dumps([{"spec_id": SPEC, "provenance": "spawn_explicit",
                                                 "granting_principal": ""}])},
    ]:
        assert not _configured_spec_authorized({**row, **fields}, cfg.rebind_authorized_spec_ids)


def test_exception_policy_is_trusted_and_replay_survives_restart_and_revocation(tmp_path):
    async def run():
        database = str(tmp_path / "binding.db")
        store = Store(database)
        store.start()
        try:
            seats = {}
            for name in ("backend", "replacement", "recovery"):
                extra = ({"qualified_spec_ids": [SPEC], "spec_id": SPEC,
                          "spec_binding_provenance": [{"spec_id": SPEC, "provenance": "spawn_explicit",
                                                       "granting_principal": "operator",
                                                       "granted_at": "2026-01-01T00:00:00Z"}]} if name == "recovery" else {})
                seats[name] = await store.open_session("local", name, provider="codex", visibility="default",
                                                       pane_status="pane_alive", effective_model="gpt-6-sol",
                                                       effective_effort="medium", **extra)
            cfg = replace(config(), direct_primary_generation=seats["backend"]["session_generation"])
            composite = AssistantComposite(store, config=cfg)
            await composite.load_binding()
            request = {"request_id": "denied-before-opt-in", "expected_revision": 0,
                       "target_stream_id": "local:replacement",
                       "authorized_spec_ids": [SPEC]}  # Untrusted request policy must be ignored.
            with pytest.raises(ValueError, match="assistant_rebind_unauthorized"):
                await composite.rebind(request, actor_stream_id="local:recovery")
            enabled = AssistantComposite(store, config=replace(cfg, rebind_authorized_spec_ids=frozenset([SPEC])))
            await enabled.load_binding()
            with pytest.raises(ValueError, match="assistant_rebind_unauthorized"):
                await enabled.rebind(request, actor_stream_id="local:recovery")
            accepted = await enabled.rebind({**request, "request_id": "new-authorized-intent"},
                                           actor_stream_id="local:recovery")
            assert accepted["new_binding"]["stream_id"] == "local:replacement"
            assert accepted["new_binding"]["generation"] == seats["replacement"]["session_generation"]
        finally:
            store.stop()
        reopened = Store(database)
        reopened.start()
        try:
            revoked = AssistantComposite(reopened, config=cfg)
            await revoked.load_binding()
            replay = await revoked.rebind({**request, "request_id": "new-authorized-intent"},
                                         actor_stream_id="local:recovery")
            assert replay["duplicate"] and replay["new_binding"] == accepted["new_binding"]
            with pytest.raises(ValueError, match="assistant_rebind_unauthorized"):
                await revoked.rebind({**request, "request_id": "after-revocation", "expected_revision": 1},
                                     actor_stream_id="local:recovery")
        finally:
            reopened.stop()
    asyncio.run(run())
