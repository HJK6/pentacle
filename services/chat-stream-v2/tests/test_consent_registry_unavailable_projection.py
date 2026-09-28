"""Optional consent projection must not break ordinary host transport."""
import pytest
from _shared import operator_auth
from notify import Notify
from sessions import VerbError
from test_consent_host_offers import setup
from test_lifecycle_authority import scenario


@pytest.mark.parametrize("registry_state", ["absent", "unavailable"])
@pytest.mark.parametrize("surface", ["hello", "notification.list", "publish"])
def test_unavailable_registry_projects_no_consent_without_breaking_transport(tmp_path, registry_state, surface, caplog):
    async def check(env):
        phone, _ = await setup(env, tmp_path)
        offer = await phone.offer()
        # A real credential-targeted offer was visible before registry loss.
        assert await env.store.consent_notifications(phone.auth, phone.registry)
        peer = type("Peer", (), {"remote_address": ("127.0.0.1", 1)})()
        env.server._connection_trust[peer] = operator_auth.ConnectionTrust("v2", phone.cid, "pentacle-mobile")
        env.server._client_identities[peer] = "pentacle-mobile"
        env.server._clients.add(peer)
        if registry_state == "absent":
            phone.registry.path.unlink()
        else:
            phone.registry.path.write_text("not-json: private-marker-must-not-be-logged")
        notify = Notify(str(tmp_path / "notifications.db"), sessions=env.sessions)
        await notify.start()
        env.server.notify = notify
        notify.consent_snapshot = env.server._consent_notifications_for_msg
        ordinary = await notify.notification({"type": "notification.create", "producer": "fixture.v1", "title": "ordinary", "request_id": "create"})
        try:
            if surface == "hello":
                result = await env.server._on_hello({"client": "pentacle-mobile", "_client_websocket": peer})
                snapshot = next(frame for frame in result if frame["type"] == "snapshot")
                assert snapshot["capabilities"]["close_expected_generation"]
                assert "consent_open_v1" not in snapshot["capabilities"]
                assert "consent_enrollment_offer_v1" not in snapshot["capabilities"]
                assert [r["producer"] for r in snapshot["notifications"]] == ["fixture.v1"]
            elif surface == "notification.list":
                result = await notify.notification({"type": "notification.list", "request_id": "list", "_client_websocket": peer})
                assert result["type"] == "notification.list.ok"
                assert [r["producer"] for r in result["notifications"]] == ["fixture.v1"]
                exact = await notify.notification({"type": "notification.list", "request_id": "exact", "notification_ids": [ordinary["notification"]["notification_id"], "consent-offer:" + offer["offer_id"]], "_client_websocket": peer})
                assert [r["producer"] for r in exact["notifications"]] == ["fixture.v1"]
            else:
                frames = []
                env.server._enqueue = lambda *args: frames.append(args)
                await env.server._publish_consent()
                assert frames == []
            # Consent mutation admission remains closed, independent of projection.
            with pytest.raises(VerbError) as raised:
                await phone.call("consent_key.open", offer_id=offer["offer_id"])
            assert raised.value.code == "consent_registry_unavailable"
            for _ in range(20):
                assert await env.store.consent_notifications(phone.auth, phone.registry) == []
            warnings = [r for r in caplog.records if r.name == "chat_streamd_v2.store" and "consent projection" in r.message]
            assert len(warnings) == 1
            assert warnings[0].message == "consent projection unavailable: registry unavailable; returning empty set"
            assert "private-marker" not in caplog.text
            assert str(phone.registry.path) not in caplog.text
            assert phone.cid not in caplog.text
            # No active key or immutable lifecycle effect can appear from fallback.
            assert await env.store.submit(lambda c: c.execute("SELECT COUNT(*) FROM v2_consent_keys WHERE state='active'").fetchone()[0]) == 0
            assert (await env.grant())["revision"] == 0
        finally:
            await notify.stop()
    scenario(check)
