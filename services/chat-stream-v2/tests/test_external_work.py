"""External-work check: real Store, handler, outbox, Comms and CLI paths; frozen time, synthetic data."""
import asyncio
import hashlib
import json
import logging

import pytest

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from external_work import ASSISTANT_NAME, CONFIG_ENV, ExternalWork, ExternalWorkConfig, install, load_config
from outbound_notices import OutboundNoticeConfig, OutboundNoticeQueue
from server import Server
from store import STREAM_TOKEN_HASH_VERSION, Store
from test_nudges import HOST, Harness

T0 = 1_900_000_000.0
WATCH = "fixture-watch"
CONFIG = ExternalWorkConfig("enabled", WATCH, "Fixture external worker", "fixture://queue")
RAW_CONFIG = {"v": 1, "watch_id": WATCH, "label": CONFIG.label, "queue_ref": CONFIG.queue_ref,
              "assistant_name": ASSISTANT_NAME}
DESK = f"{HOST}:desk"


class Rig:
    def __init__(self, store, config=CONFIG, comms=True, outbox=None):
        self.store, self.h, self.now, self.binding, self.tokens = store, Harness(store), T0, {}, {}
        self.outbound = OutboundNoticeQueue(store, self.h.comms if comms else None, config=outbox)
        self.attach(config)

    def attach(self, config):
        self.runtime = ExternalWork(self.store, self.h.sessions, self.outbound, config=config,
                                    env_binding=lambda: self.binding, clock=lambda: self.now)

    async def seat(self, name="desk", *, bind=True, pane="pane_alive"):
        await self.h.open(name, visibility="default", user_event_count=0, pane_status=pane)
        await self.h.observe()
        stream_id, token = f"{HOST}:{name}", f"token-{name}-{len(self.tokens)}"
        digest = hashlib.sha256(token.encode()).hexdigest()
        if await self.store.grant_stream_token(HOST, name, digest, STREAM_TOKEN_HASH_VERSION) != "ok":
            await self.store.update_session(HOST, name, token_hash=digest)  # a replaced seat rotates its token
        generation = self.h.sessions.get(stream_id)["session_generation"]
        self.tokens[stream_id] = token
        if bind:
            self.binding = {"stream_id": stream_id, "generation": generation}
        return stream_id, generation

    async def call(self, verb, actor=DESK, **fields):
        return await self.runtime.handle({
            "type": f"external_work.{verb}", "stream_token": self.tokens.get(actor),
            "_auth_context": {"stream_id": actor, "token_verified": True}, **fields})

    async def version(self):
        row = await self.store.submit(lambda c: c.execute(
            "SELECT version FROM v2_external_work_state WHERE watch_id=?", (WATCH,)).fetchone())
        return row[0] if row else 0

    async def observation(self, request_id, state="working", **fields):
        known = state != "unknown"
        return {"watch_id": WATCH, "request_id": request_id, "expected_version": await self.version(),
                "observed_at": self.now, "state": state,
                "current_packet_ref": "packet://1" if state == "working" else None,
                "evidence_refs": ["evidence://receipt-1"] if known else [], "queue_sha256": "a" * 64,
                "next_packet_ref": "packet://2", "supply_gap": None,
                "blocker": {"owner": "fleet", "reason": "input owed"} if state == "waiting_on_fleet" else None,
                **fields}

    async def record(self, request_id, state="working", actor=DESK, **fields):
        return await self.call("record", actor, record=await self.observation(request_id, state, **fields))

    async def state(self):
        return (await self.call("show"))["state"]

    async def notices(self):
        return await self.store.submit(lambda c: [dict(r) for r in c.execute(
            "SELECT * FROM v2_outbound_notices WHERE kind='external_work_due' ORDER BY rowid")])

    async def land(self, notice_id):
        row = await self.store.claim_outbound_notice(notice_id, owner="fixture", lease_s=30, force=True)
        assert row and await self.store.complete_outbound_notice(notice_id, owner="fixture")

    async def rows(self, table):
        return await self.store.submit(lambda c: [dict(r) for r in c.execute(f"SELECT * FROM {table}")])


def exercise(check, path=":memory:", **rig):
    async def run():
        store = Store(str(path))
        store.start()
        try:
            await check(Rig(store, **rig))
        finally:
            store.stop()
    asyncio.run(run())


def test_initial_due_then_two_hour_check_and_one_hour_action_thresholds():
    async def check(rig):
        await rig.seat()
        tick = await rig.runtime.tick()
        assert tick["reasons"] == ["check_due", "state_unknown", "supply_gap"] and tick["episode"] == 1
        first = await rig.notices()
        assert len(first) == 1 and first[0]["recipient_stream_id"] == DESK and tick["issued"] == first[0]["notice_id"]
        shown = await rig.call("show")
        assert shown["type"] == "external_work.show.ok" and shown["version"] == 0
        assert shown["health"]["due_at"] == T0 and shown["health"]["last_notice"]["delivered_at"] is None

        ok = await rig.record("working-1")
        assert ok["type"] == "external_work.record.ok" and ok["version"] == 1 and ok["duplicate"] is False
        assert ok["health"] == {"enabled": True, "due_at": T0 + 7200, "reasons": [], "last_notice": None}
        assert (await rig.notices())[0]["terminal_reason"] == "external_work_resolved"
        rig.now = T0 + 7199
        assert (await rig.runtime.tick())["reasons"] == [] and len(await rig.notices()) == 1
        rig.now = T0 + 7200
        # Threshold readback is distinct from delivery: show reports the deadline, only a tick enqueues.
        assert (await rig.call("show"))["health"]["reasons"] == ["check_due"] and len(await rig.notices()) == 1
        assert (await rig.runtime.tick())["episode"] == 2 and len(await rig.notices()) == 2

        idle = await rig.record("idle-1", "idle")
        assert idle["health"]["reasons"] == [] and idle["health"]["due_at"] == T0 + 7200 + 3600
        rig.now = T0 + 7200 + 3599
        assert (await rig.runtime.tick())["reasons"] == []
        rig.now = T0 + 7200 + 3600
        assert (await rig.runtime.tick())["reasons"] == ["action_due"] and len(await rig.notices()) == 3
        body = (await rig.notices())[-1]["body"]
        assert "[external_work_due] Fixture external worker" in body and "queue: fixture://queue" in body
        assert "episode: 3" in body and "reasons: action_due" in body and "blocked_age_s: 3600" in body
        assert "agent-orch external-work show" in body and all(t not in body for t in rig.tokens.values())
    exercise(check)


def test_unknown_never_verifies_and_only_working_clears_the_blocked_clock():
    async def check(rig):
        await rig.seat()
        unknown = await rig.record("unknown-1", "unknown")
        assert unknown["state"]["last_verified_at"] is None
        assert unknown["health"]["reasons"] == ["check_due", "state_unknown"]
        await rig.record("idle-1", "idle")
        rig.now += 600
        await rig.record("waiting-1", "waiting_on_fleet")
        rig.now += 600
        still = await rig.record("unknown-2", "unknown")
        assert still["state"]["blocked_since"] == T0 and still["state"]["last_verified_at"] == T0 + 600
        rig.now = T0 + 3600
        # A fresh check-in cannot restart the one-hour clock.
        late = await rig.record("idle-2", "idle")
        assert late["state"]["blocked_since"] == T0 and late["health"]["reasons"] == ["action_due"]
        gap = await rig.record("working-1", next_packet_ref=None, supply_gap="no ready packet")
        assert gap["state"]["blocked_since"] is None and gap["state"]["last_verified_at"] == T0 + 3600
        # A supply gap stays an explicit unresolved action regardless of check recency.
        assert gap["health"]["reasons"] == ["supply_gap"] and gap["health"]["due_at"] == rig.now
        assert (await rig.record("working-2"))["health"]["reasons"] == []
    exercise(check)


def test_configuration_file_states_are_visible_and_never_fail_startup(tmp_path, caplog):
    path = tmp_path / "private-watch.json"
    path.write_text(json.dumps(RAW_CONFIG))
    assert load_config({CONFIG_ENV: str(path)}) == CONFIG
    assert load_config({}).status == "disabled" and load_config({CONFIG_ENV: " "}).status == "disabled"
    invalid = [{**RAW_CONFIG, "extra": 1}, {**RAW_CONFIG, "v": 2}, {**RAW_CONFIG, "v": True}, {**RAW_CONFIG, "v": 1.0},
               {**RAW_CONFIG, "watch_id": "Bad Id"}, {**RAW_CONFIG, "label": " "}, {**RAW_CONFIG, "label": "x" * 81},
               {**RAW_CONFIG, "queue_ref": 7}, {**RAW_CONFIG, "queue_ref": "q" * 513},
               {**RAW_CONFIG, "assistant_name": "other"}, {k: v for k, v in RAW_CONFIG.items() if k != "label"}, []]
    with caplog.at_level(logging.ERROR, logger="chat_streamd_v2.external_work"):
        for raw in invalid:
            path.write_text(json.dumps(raw))
            assert load_config({CONFIG_ENV: str(path)}).status == "config_invalid", raw
        path.write_text("{not json secret-value")
        assert load_config({CONFIG_ENV: str(path)}).status == "config_invalid"
        assert load_config({CONFIG_ENV: str(tmp_path / "absent.json")}).status == "config_invalid"
    assert len(caplog.records) == len(invalid) + 2
    assert {r.getMessage() for r in caplog.records} == {"subsystem=external_work error=config_invalid action=disabled"}

    async def check(rig):
        await rig.seat()
        for status in ("disabled", "config_invalid"):
            rig.attach(ExternalWorkConfig(status))
            assert await rig.runtime.tick() is None
            assert (await rig.call("show"))["error_code"] == status
            assert (await rig.record("refused-" + status))["error_code"] == status
        assert await rig.rows("v2_external_work_state") == [] and await rig.notices() == []
    exercise(check)


@pytest.mark.parametrize("content,status", [(json.dumps(RAW_CONFIG), "enabled"), ("{broken", "config_invalid")])
def test_startup_wiring_loads_the_configured_file(tmp_path, content, status):
    """The daemon's own wiring function, fed a real file through the environment variable."""
    path = tmp_path / "private-watch.json"
    path.write_text(content)

    async def check(rig):
        _, generation = await rig.seat()
        composite = AssistantComposite(rig.store, config=AssistantCompositeConfig(
            enabled=True, name=ASSISTANT_NAME, stream_id=f"{HOST}:assistant", direct_primary_stream_id=DESK,
            direct_primary_generation=generation, astra_stream_id=DESK))
        server = Server(store=rig.store, sessions=rig.h.sessions, comms=rig.h.comms, local_host=HOST)
        outbound = OutboundNoticeQueue(rig.store, rig.h.comms)
        runtime = install(server, rig.store, rig.h.sessions, outbound, composite, {CONFIG_ENV: str(path)})
        assert server.external_work is outbound.external_work is runtime and runtime.config.status == status
        assert server.handlers["external_work.show"] == runtime.handle
        await outbound.drain_once()
        shown = await runtime.handle({"type": "external_work.show", "stream_token": rig.tokens[DESK],
                                      "_auth_context": {"stream_id": DESK, "token_verified": True}})
        if status == "enabled":
            assert shown["watch_id"] == WATCH and shown["health"]["enabled"] is True
            assert len(rig.h.tmux.pasted) == 1 and "[external_work_due] Fixture external worker" in rig.h.tmux.pasted[0]
        else:
            assert shown["error_code"] == "config_invalid" and rig.h.tmux.pasted == [] and await rig.notices() == []
    exercise(check)


def test_main_startup_uses_the_wiring_function():
    from pathlib import Path
    source = (Path(__file__).parents[1] / "main.py").read_text()
    assert "install_external_work(server, store, sessions, outbound, assistant_composite)" in source


def test_disable_and_reenable_preserve_ages_and_refuse_queued_delivery(tmp_path):
    async def check(rig):
        await rig.seat()
        await rig.record("idle-1", "idle")
        rig.now = T0 + 3600
        await rig.runtime.tick()
        (queued,) = await rig.notices()
        saved = await rig.rows("v2_external_work_state")
        rig.store.stop()
        rig.store.start()
        rig.attach(ExternalWorkConfig("disabled"))
        rig.now = T0 + 9000
        await rig.outbound.drain_once(force=True)
        assert rig.h.tmux.pasted == [] and await rig.rows("v2_external_work_state") == saved
        assert (await rig.notices())[0]["terminal_reason"] == "external_work_disabled"
        rig.attach(CONFIG)
        state = await rig.state()
        assert (state["blocked_since"], state["last_verified_at"], state["episode"]) == (T0, T0, 1)
        assert state["active_reasons"] == ["action_due", "check_due"] and state["active_notice_id"] == queued["notice_id"]
        assert (await rig.runtime.tick())["issued"]  # refused while off, so sent again at once
        # A changed watch id is a new immediately-due check, never a migrated healthy observation.
        rig.attach(ExternalWorkConfig("enabled", "other-watch", "Other", "fixture://other"))
        assert (await rig.runtime.tick())["reasons"] == ["check_due", "state_unknown", "supply_gap"]
        fresh = {r["watch_id"]: json.loads(r["data"]) for r in await rig.rows("v2_external_work_state")}
        assert fresh["other-watch"]["last_verified_at"] is None and fresh[WATCH]["last_verified_at"] == T0
    exercise(check, tmp_path / "store.db")


def test_replay_conflict_cas_and_fault_rollback():
    async def check(rig):
        await rig.seat()
        payload = await rig.observation("request-1")
        first = await rig.call("record", record=payload)
        before = await rig.rows("v2_external_work_state")
        rig.now += 5000  # a replay is answered from the receipt even after the freshness window
        replay = await rig.call("record", record=payload)
        assert replay == {**first, "duplicate": True} and await rig.rows("v2_external_work_state") == before
        changed = await rig.call("record", record={**payload, "state": "idle", "current_packet_ref": None})
        assert changed["error_code"] == "idempotency_conflict" and changed["request_id"] == "request-1"
        stale_version = await rig.record("request-2", expected_version=0)
        assert stale_version["error_code"] == "version_conflict"
        assert await rig.rows("v2_external_work_state") == before
        assert [r["request_id"] for r in await rig.rows("v2_external_work_records")] == ["request-1"]
        receipt = (await rig.rows("v2_external_work_records"))[0]
        assert (receipt["actor_stream"], receipt["actor_generation"]) == (DESK, rig.binding["generation"])

        def fault():
            raise RuntimeError("injected")
        rig.store._external_work_fault = fault
        with pytest.raises(RuntimeError):
            await rig.record("request-3")
        rig.store._external_work_fault = None
        assert await rig.rows("v2_external_work_state") == before
        assert len(await rig.rows("v2_external_work_records")) == 1
        assert (await rig.record("request-3"))["version"] == 2
    exercise(check)


def test_two_concurrent_submissions_one_wins(tmp_path):
    async def check(rig):
        await rig.seat()
        one, two = await rig.observation("race-1"), await rig.observation("race-2")
        replies = await asyncio.gather(rig.call("record", record=one), rig.call("record", record=two))
        assert sorted(r.get("error_code", "ok") for r in replies) == ["ok", "version_conflict"]
        assert await rig.version() == 1 and len(await rig.rows("v2_external_work_records")) == 1
    exercise(check, tmp_path / "store.db")


@pytest.mark.parametrize("fields,code", [
    ({"observed_at": T0 + 1}, "invalid_request"), ({"observed_at": T0 - 901}, "observation_stale"),
    ({"observed_at": float("nan")}, "invalid_request"), ({"observed_at": float("inf")}, "invalid_request"),
    ({"observed_at": True}, "invalid_request"), ({"observed_at": "now"}, "invalid_request"),
    ({"expected_version": False}, "invalid_request"), ({"expected_version": -1}, "invalid_request"),
    ({"expected_version": 0.0}, "invalid_request"), ({"actor_stream": DESK}, "invalid_request"),
    ({"watch_id": "other-watch"}, "invalid_request"), ({"request_id": " "}, "invalid_request"),
    ({"request_id": "r" * 129}, "invalid_request"), ({"state": "done"}, "invalid_request"),
    ({"evidence_refs": []}, "invalid_request"), ({"evidence_refs": "evidence://x"}, "invalid_request"),
    ({"evidence_refs": ["a", "a"]}, "invalid_request"), ({"evidence_refs": [f"e{i}" for i in range(11)]}, "invalid_request"),
    ({"evidence_refs": ["e" * 513]}, "invalid_request"), ({"evidence_refs": [1]}, "invalid_request"),
    ({"current_packet_ref": None}, "invalid_request"), ({"queue_sha256": "A" * 64}, "invalid_request"),
    ({"next_packet_ref": None}, "invalid_request"), ({"supply_gap": "also set"}, "invalid_request"),
    ({"blocker": {"owner": "fleet"}}, "invalid_request"), ({"blocker": {"owner": "", "reason": "r"}}, "invalid_request"),
    ({"state": "waiting_on_fleet"}, "invalid_request"), ({"supply_gap": "g" * 17000, "next_packet_ref": None}, "invalid_request"),
])
def test_malformed_or_untimely_observation_is_refused(fields, code):
    async def check(rig):
        await rig.seat()
        refused = await rig.call("record", record={**await rig.observation("refused"), **fields})
        assert refused["error_code"] == code
        assert await rig.rows("v2_external_work_records") == [] and await rig.version() == 0
        accepted = await rig.record("accepted", observed_at=T0 - 900)
        assert accepted["ok"] and accepted["state"]["last_verified_at"] == T0 - 900
        # Observation time never moves backwards.
        assert (await rig.record("older", observed_at=T0 - 901 + 1 - 1))["error_code"] == "observation_stale"
    exercise(check)


def test_only_the_current_front_desk_generation_is_authorized():
    async def check(rig):
        await rig.seat()
        worker, _ = await rig.seat("worker", bind=False)
        for verb, extra in (("show", {}), ("record", {"record": await rig.observation("worker-1")})):
            assert (await rig.call(verb, worker, **extra))["error_code"] == "fd_not_current"
            # An operator token alone, a bare claim, a wrong token or a chosen actor authorize nothing.
            base = {"type": f"external_work.{verb}", "stream_token": rig.tokens[DESK], **extra}
            for auth, fields in (({"operator_authenticated": True}, {}), ({"stream_id": DESK}, {}),
                                 ({"stream_id": DESK, "token_verified": True}, {"stream_token": "wrong"}),
                                 ({"stream_id": worker, "token_verified": True}, {"from_stream_id": DESK})):
                reply = await rig.runtime.handle({**base, **fields, "_auth_context": auth})
                assert reply["error_code"] == "not_authenticated", (verb, auth)
        assert (await Server()._dispatch(json.dumps({
            "type": "external_work.show", "_auth_context": {"stream_id": DESK, "token_verified": True}})))[0][
                "error_code"] == "unsupported_in_v2"
        server = Server(store=rig.store, sessions=rig.h.sessions)
        server.handlers.update(rig.runtime.wire_handlers())
        forged = await server._dispatch(json.dumps({
            "type": "external_work.show", "request_id": "forged", "stream_token": rig.tokens[DESK],
            "_auth_context": {"stream_id": DESK, "token_verified": True}}))
        assert forged[0] == {"type": "external_work.error", "ok": False, "error_code": "not_authenticated",
                             "request_id": "forged"}

        old_token, old_generation = rig.tokens[DESK], rig.binding["generation"]
        _, new_generation = await rig.seat(bind=False)  # the desk seat is replaced; binding still names the old one
        assert new_generation != old_generation
        rig.tokens[DESK], new_token = old_token, rig.tokens[DESK]
        assert (await rig.call("show"))["error_code"] == "not_authenticated"
        rig.tokens[DESK] = new_token
        assert (await rig.call("show"))["error_code"] == "fd_not_current"
        rig.binding = {"stream_id": DESK, "generation": new_generation}
        assert (await rig.record("successor-1"))["ok"] is True  # a successor records with its own credential
        await rig.store.update_session(HOST, "desk", status="closed")
        assert (await rig.call("show"))["error_code"] == "not_authenticated"
        assert len(await rig.rows("v2_external_work_records")) == 1
    exercise(check)


def test_restart_before_and_after_insertion_and_after_landing_keeps_one_identity(tmp_path):
    async def check(rig):
        async def restart():
            rig.store.stop()
            rig.store.start()
            rig.attach(CONFIG)

        assert (await rig.runtime.tick())["issued"] is None  # no front desk bound: pending, no notice
        await restart()
        rig.now = T0 + 50
        assert (await rig.runtime.tick())["episode"] == 1 and await rig.notices() == []
        await rig.seat()
        issued = (await rig.runtime.tick())["issued"]
        await restart()
        for _ in range(3):
            assert (await rig.runtime.tick())["issued"] is None
        (row,) = await rig.notices()
        assert row["notice_id"] == row["tell_id"] == row["dedupe_key"] == issued
        await rig.land(issued)
        await restart()
        rig.now = T0 + 50 + 7199
        assert (await rig.runtime.tick())["issued"] is None
        rig.now = T0 + 50 + 7200
        reminder = (await rig.runtime.tick())["issued"]
        state = json.loads((await rig.rows("v2_external_work_state"))[0]["data"])
        assert reminder and reminder != issued and state["episode"] == 1 and state["notice_sequence"] == 2
        # Delivery, however often, never records a check.
        assert state["created_at"] == T0 and state["last_verified_at"] is None and state["observation"] is None
    exercise(check, tmp_path / "store.db")


def test_reminder_is_delivered_through_comms_once_and_survives_pane_absence(tmp_path):
    async def check(rig):
        await rig.seat()
        await rig.store.update_session(HOST, "desk", pane_status="pane_unknown")
        await rig.outbound.drain_once()
        (row,) = await rig.notices()
        assert rig.h.tmux.pasted == [] and row["last_error"] == "external_work_recipient_unverified"
        assert row["terminal_at"] is None and row["attempts"] == 0  # deferred, not a spent transport attempt
        await rig.store.update_session(HOST, "desk", pane_status="pane_alive")
        rig.h.sessions.apply_live(DESK, pane_status="pane_alive", online=True)
        rig.store.stop()
        rig.store.start()
        for _ in range(3):
            await rig.outbound.drain_once(force=True)
        assert len(rig.h.tmux.pasted) == 1 and rig.h.tmux.pasted[0] == row["body"]
        assert len(await rig.notices()) == 1 and (await rig.state())["last_verified_at"] is None
    exercise(check, tmp_path / "store.db")


@pytest.mark.parametrize("landed", [False, True])
def test_front_desk_replacement_reissues_once_to_the_current_generation(landed):
    async def check(rig):
        await rig.seat()
        first = (await rig.runtime.tick())["issued"]
        if landed:
            await rig.land(first)
        successor, generation = await rig.seat("successor")
        second = (await rig.runtime.tick())["issued"]
        old, new = await rig.notices()
        assert old["terminal_reason"] == (None if landed else "external_work_recipient_replaced")
        assert new["notice_id"] == second and new["recipient_stream_id"] == successor
        assert json.loads(new["metadata"])["recipient_generation"] == generation
        for _ in range(3):
            assert (await rig.runtime.tick())["issued"] is None  # the new recipient is recorded
        state = json.loads((await rig.rows("v2_external_work_state"))[0]["data"])
        assert state["episode"] == 1 and state["last_verified_at"] is None
        # A stale notice for the replaced desk is refused; the current one may deliver.
        stale = await rig.runtime.delivery_guard(old)
        assert (stale.action, stale.reason) == ("terminal", "external_work_resolved")
        assert await rig.runtime.delivery_guard(new) is None
        rig.binding = {}
        rebound = await rig.runtime.delivery_guard(new)
        assert (rebound.action, rebound.reason) == ("terminal", "external_work_recipient_rebound")
        assert (await rig.runtime.tick())["issued"] is None  # missing front desk: pending, never checked
        rig.binding = {"stream_id": successor, "generation": generation}
        await rig.store.update_session(HOST, "successor", status="closed")
        gone = await rig.runtime.delivery_guard(new)
        assert (gone.action, gone.reason) == ("terminal", "external_work_recipient_unavailable")
    exercise(check)


def test_reminder_refused_for_a_missing_binding_is_sent_when_the_same_binding_returns():
    async def check(rig):
        await rig.seat()
        first = (await rig.runtime.tick())["issued"]
        binding, rig.binding = rig.binding, {}
        await rig.outbound.drain_once(force=True)
        assert (await rig.notices())[0]["terminal_reason"] == "external_work_recipient_rebound"
        assert rig.h.tmux.pasted == [] and (await rig.runtime.tick())["issued"] is None
        rig.binding = binding
        rig.now = T0 + 60
        second = (await rig.runtime.tick())["issued"]
        assert second and second != first and (await rig.runtime.tick())["issued"] is None
        assert len(await rig.notices()) == 2
    exercise(check)


def test_in_flight_reminder_is_neither_retired_nor_duplicated():
    async def check(rig):
        await rig.seat()
        first = (await rig.runtime.tick())["issued"]
        # A delivery holds the row: its guard has passed and the paste is under way.
        assert await rig.store.claim_outbound_notice(first, owner="in-flight", lease_s=30, force=True)
        rig.now = T0 + 7200
        assert (await rig.runtime.tick())["issued"] is None  # no second reminder beside it
        await rig.record("working-1")
        (row,) = await rig.notices()
        assert row["terminal_at"] is None and row["lease_owner"] == "in-flight"
        assert await rig.store.complete_outbound_notice(first, owner="in-flight")  # the send still settles as sent
        (row,) = await rig.notices()
        assert row["delivered_at"] and row["terminal_reason"] is None
    exercise(check)


def test_delivery_guard_refuses_a_resolved_episode():
    async def check(rig):
        await rig.seat()
        await rig.runtime.tick()
        (row,) = await rig.notices()
        assert await rig.runtime.delivery_guard(row) is None
        await rig.record("working-1")
        late = await rig.runtime.delivery_guard(row)
        assert (late.action, late.reason) == ("terminal", "external_work_resolved")
        await rig.outbound.drain_once(force=True)
        assert rig.h.tmux.pasted == []
    exercise(check)


def test_trusted_reminder_bypasses_the_digest_hold_and_a_forged_marker_cannot():
    async def check(rig):
        _, generation = await rig.seat()
        composite = AssistantComposite(rig.store, config=AssistantCompositeConfig(
            enabled=True, name=ASSISTANT_NAME, stream_id=f"{HOST}:assistant", direct_primary_stream_id=DESK,
            direct_primary_generation=generation, astra_stream_id=DESK))
        comms = rig.h.comms
        comms.assistant_ingress_policy = composite.suppress_routine_backend_ingress
        comms.front_desk_digest = rig.outbound.front_desk_digest = composite.front_desk_digest
        await rig.outbound.drain_once()  # empty digest: the reminder still arrives
        (row,) = await rig.notices()
        assert rig.h.tmux.pasted == [row["body"]]
        await comms.tell({"stream_id": DESK, "message": "START: routine", "tell_id": "held-start"})
        server = Server(store=rig.store, sessions=rig.h.sessions, comms=comms, local_host=HOST)
        forged = await server._dispatch(json.dumps({
            "type": "tell", "to_stream_id": DESK, "tell_id": "forged", "from_stream_id": f"{HOST}:peer",
            "message": row["body"].replace(row["notice_id"], "xw:" + "0" * 64),
            "_outbound_notice_kind": "external_work_due"}))
        assert forged[0]["type"] == "tell.ok" and forged[0]["assistant_backend_ingress"] == "persisted_suppressed"
        assert len(rig.h.tmux.pasted) == 1 and len(await composite.front_desk_digest._rows(DESK)) == 2
        # With rows held, the next reminder still goes straight to the desk and the hold is untouched.
        await rig.land(row["notice_id"])
        rig.now = T0 + 7200
        await rig.outbound.drain_once()
        assert len(rig.h.tmux.pasted) == 2 and "[external_work_due]" in rig.h.tmux.pasted[1]
        assert len(await composite.front_desk_digest._rows(DESK)) == 2
    exercise(check)


def test_tick_failure_does_not_stop_other_notices(caplog):
    async def check(rig):
        await rig.seat()

        async def broken():
            raise RuntimeError("injected tick failure")
        rig.runtime.tick = broken
        await rig.outbound.enqueue(kind="reconciler", dedupe_key="other", tell_id="other",
                                   recipient_stream_id=DESK, body="other notice")
        with caplog.at_level(logging.ERROR, logger="chat_streamd_v2.outbound_notices"):
            await rig.outbound.drain_once()
        assert len(rig.h.tmux.pasted) == 1 and "other notice" in rig.h.tmux.pasted[0]
        assert "subsystem=external_work action=tick_failed" in caplog.text
    exercise(check)


def test_exhausted_retries_keep_one_identity_and_wait_two_hours():
    async def check(rig):
        await rig.seat()
        for _ in range(4):
            await rig.outbound.drain_once(force=True)
        (row,) = await rig.notices()
        assert row["attempts"] == 2 and row["terminal_reason"].startswith("retry_budget_exhausted")
        rig.now = T0 + 7199
        await rig.outbound.drain_once(force=True)
        assert len(await rig.notices()) == 1
        rig.now = T0 + 7200
        await rig.outbound.drain_once(force=True)
        assert len(await rig.notices()) == 2 and (await rig.state())["last_verified_at"] is None
    exercise(check, comms=False, outbox=OutboundNoticeConfig(max_attempts=2))


def test_unsettled_reminder_is_superseded_after_two_hours_not_before():
    async def check(rig):
        await rig.seat()
        await rig.outbound.drain_once()
        (stuck,) = await rig.notices()  # pasted, but its delivery proof never arrives
        assert len(rig.h.tmux.pasted) == 1 and stuck["delivered_at"] is None and stuck["terminal_at"] is None
        rig.now = T0 + 7199
        assert (await rig.runtime.tick())["issued"] is None
        rig.now = T0 + 7200
        await rig.outbound.drain_once()
        old, new = await rig.notices()
        assert old["terminal_reason"] == "external_work_superseded" and new["terminal_at"] is None
        assert len(rig.h.tmux.pasted) == 2 and (await rig.state())["episode"] == 1
    exercise(check)


def test_reasons_change_inside_an_episode_without_extra_notices():
    async def check(rig):
        await rig.seat()
        first = (await rig.runtime.tick())["issued"]
        await rig.land(first)
        changed = await rig.record("unknown-1", "unknown", next_packet_ref=None, supply_gap="none ready")
        assert changed["health"]["reasons"] == ["check_due", "state_unknown", "supply_gap"]
        narrowed = await rig.record("idle-1", "idle", next_packet_ref=None, supply_gap="none ready")
        assert narrowed["health"]["reasons"] == ["supply_gap"] and narrowed["state"]["episode"] == 1
        assert (await rig.runtime.tick())["issued"] is None and len(await rig.notices()) == 1
        cleared = await rig.record("idle-2", "idle")
        assert cleared["health"]["reasons"] == [] and cleared["state"]["active_notice_id"] is None
        rig.now = T0 + 3600
        reopened = await rig.runtime.tick()
        assert reopened["episode"] == 2 and reopened["reasons"] == ["action_due"] and len(await rig.notices()) == 2
    exercise(check)


@pytest.mark.parametrize("checked_in", [False, True])
def test_twenty_four_hour_tick_soak_bounds_notices_and_receipts(checked_in):
    async def check(rig):
        await rig.seat()
        for step in range(24 * 3600 // 5 + 1):
            rig.now = T0 + step * 5
            if checked_in and step % 1200 == 0:  # the desk checks in every 100 minutes
                assert (await rig.record(f"check-{step}"))["ok"]
            issued = (await rig.runtime.tick())["issued"]
            if issued:
                await rig.land(issued)
        (state,) = await rig.rows("v2_external_work_state")
        assert len(state["data"]) < 2048
        assert len(await rig.notices()) == (0 if checked_in else 13)  # t0 and one reminder per two hours
        assert len(await rig.rows("v2_external_work_records")) == (15 if checked_in else 0)
    exercise(check)


def test_clock_jumps_never_erase_due_work_or_invent_evidence():
    async def check(rig):
        await rig.seat()
        await rig.record("working-1")
        rig.now = T0 + 7200
        assert (await rig.runtime.tick())["reasons"] == ["check_due"]
        rig.now = T0 - 86400  # backward wall-clock jump
        assert (await rig.runtime.tick())["reasons"] == ["check_due"]
        shown = await rig.call("show")
        assert shown["state"]["clock_high_water"] == T0 + 7200 and shown["health"]["due_at"] == T0 + 7200
        # A caller's observation is checked against the real server clock, not the high-water mark.
        assert (await rig.record("future", observed_at=T0))["error_code"] == "invalid_request"
        rig.now = T0 + 7300
        assert (await rig.record("working-2"))["health"]["reasons"] == []
        rig.now = T0 + 7300 + 10 * 86400  # forward jump: an early advisory, no invented verification
        late = await rig.call("show")
        assert late["health"]["reasons"] == ["check_due"] and late["state"]["last_verified_at"] == T0 + 7300
    exercise(check)


def test_real_cli_round_trip_over_a_loopback_socket(tmp_path, monkeypatch, capsys):
    from agent_orch import cli, wsclient
    from agent_orch.config import Config

    async def check(rig):
        import time
        rig.runtime.clock = time.time
        await rig.seat()
        worker, _ = await rig.seat("worker", bind=False)
        server = Server(host="127.0.0.1", port=0, store=rig.store, sessions=rig.h.sessions, local_host=HOST)
        server.handlers.update(rig.runtime.wire_handlers())
        port = await server.bind()
        monkeypatch.setattr(cli, "load_config", lambda: Config(f"ws://127.0.0.1:{port}", "", HOST, tmp_path))
        monkeypatch.delenv("AGENT_ORCH_INTERNAL_LEADER_STREAM_ID", raising=False)
        actor = [DESK]
        monkeypatch.setattr(wsclient, "_stream_token_from_env", lambda: rig.tokens[actor[0]])
        queue = tmp_path / "queue.md"
        queue.write_text("# synthetic queue\n- packet://2 ready\n")
        record_file = tmp_path / "record.json"

        async def run(*argv):
            monkeypatch.setenv("AGENT_ORCH_STREAM_ID", actor[0])
            args = cli.build_parser().parse_args(["external-work", *argv])
            code = await asyncio.to_thread(args.func, args)
            captured = capsys.readouterr()
            assert all(token not in captured.out + captured.err for token in rig.tokens.values())
            return code, json.loads(captured.out) if captured.out.strip() else captured.err

        try:
            code, shown = await run("show")
            assert code == 0 and shown["type"] == "external_work.show.ok" and shown["version"] == 0
            assert shown["health"]["reasons"] == ["check_due", "state_unknown", "supply_gap"]
            payload = await rig.observation("cli-1", observed_at=time.time() - 1,
                                            queue_sha256=hashlib.sha256(queue.read_bytes()).hexdigest())
            record_file.write_text(json.dumps(payload))
            code, recorded = await run("record", "--file", str(record_file))
            assert code == 0 and recorded["type"] == "external_work.record.ok" and recorded["duplicate"] is False
            assert recorded["request_id"] == "cli-1" and recorded["health"]["reasons"] == []
            assert recorded["state"]["observation"]["queue_sha256"] == payload["queue_sha256"]
            code, replayed = await run("record", "--file", str(record_file))
            assert code == 0 and replayed["duplicate"] is True and replayed["version"] == 1
            record_file.write_text(json.dumps({**payload, "state": "unknown", "evidence_refs": []}))
            code, conflict = await run("record", "--file", str(record_file))
            assert code == 1 and conflict["error_code"] == "idempotency_conflict"
            record_file.write_text(json.dumps({"request_id": "cli-2", "state": "working"}))
            code, refused = await run("record", "--file", str(record_file))
            assert code == 1 and refused == {"type": "external_work.error", "ok": False,
                                             "error_code": "invalid_request", "request_id": "cli-2"}
            record_file.write_text("not json")
            code, unreadable = await run("record", "--file", str(record_file))
            assert code == 2 and "unreadable record file" in unreadable
            actor[0] = worker
            code, denied = await run("show")
            assert code == 1 and denied["error_code"] == "fd_not_current"
            assert len(await rig.rows("v2_external_work_records")) == 1
        finally:
            await server.close()
    exercise(check, tmp_path / "store.db")
