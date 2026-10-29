"""S0 coverage and live-set projection checks using isolated stores."""

import asyncio
import json
import logging
import pytest

from assistant_activity import project_input, summarize_activity
from assistant_composite import AssistantComposite, AssistantCompositeConfig, direct_dispatch_envelope
from store import Store
from test_assistant_activity_cases import STREAM_ID, _load_fixture, _seed

ROOT = "fixture-root:visible"


async def _direct_fixture():
    store = Store(":memory:")
    store.start()
    root = await store.open_session("fixture-root", "visible", provider="codex")
    generation = root["session_generation"]
    config = AssistantCompositeConfig.from_env({
        "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
        "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": STREAM_ID,
        "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": ROOT,
        "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": generation,
    })
    composite = AssistantComposite(store, config=config)
    await composite.ensure_projection()
    return store, composite, generation


async def _direct_route(store, generation, message_id, *, state="landed", target=ROOT):
    row = await store.admit_assistant_composite_input(
        stream_id=STREAM_ID, input_identity=message_id, input_request_id="request:" + message_id,
        body="fixture input", attachments=[], reply_to_message_id=None,
        reply_to_question_id=None, actor_stream_id="operator:fixture", direct_primary=True,
        direct_target_stream_id=ROOT, direct_target_generation=generation,
    )
    dispatch_id = "dispatch:" + message_id
    def resolve(conn):
        conn.execute("UPDATE v2_assistant_composite_routes SET routing_state='resolved',"
            "delivery_state=?,dispatch_id=?,route_target=?,route_target_generation=?,"
            "route_json=? WHERE route_id=?", (state, dispatch_id, target, generation,
             json.dumps({"admission_mode":"direct_primary"}), row["route_id"]))
        conn.commit()
    await store.submit(resolve)
    return await store.get_assistant_composite_route(stream_id=STREAM_ID, input_identity=message_id)


def _final(route, covers=None):
    return {"request_id":"publish:" + route["dispatch_id"], "composite_stream_id":STREAM_ID,
            "dispatch_id":route["dispatch_id"], "reply_to_message_id":route["input_identity"],
            "reply_to_question_id":None,"publish_kind":"prose","response_state":"final",
            "message":"fixture answer", "attachment_ids":[], "evidence_refs":[],
            **({"covers_message_ids":covers} if covers is not None else {})}


@pytest.mark.parametrize("older", [True, False])
def test_publish_covering_final_links_landed_input(older):
    async def run():
        store, composite, generation = await _direct_fixture()
        try:
            if older:
                y = await _direct_route(store, generation, "y")
                x = await _direct_route(store, generation, "x", state="intent")
            else:
                x = await _direct_route(store, generation, "x", state="intent")
                y = await _direct_route(store, generation, "y")
            before_count = await store.submit(lambda c: c.execute("SELECT count(*) FROM v2_assistant_composite_routes").fetchone()[0])
            receipt = await composite.publish(_final(x, ["y"]), actor_stream_id=ROOT)
            assert receipt["covered_message_ids"] == ["y"]
            activity = await store.assistant_composite_activity(stream_id=STREAM_ID)
            assert activity["y"]["response_state"] == "answered"
            assert activity["y"]["answered_via"] == "burst"
            assert summarize_activity(activity)["pending_count"] == 0
            after = await store.get_assistant_composite_route(stream_id=STREAM_ID, input_identity="y")
            assert after["delivery_state"] == "landed"
            assert await store.submit(lambda c: c.execute("SELECT count(*) FROM v2_assistant_composite_routes").fetchone()[0]) == before_count
        finally:
            await composite.stop()
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("prior", ["intent", "committed_pending", "uncertain"])
def test_exact_final_promotes_committed_pending(prior):
    async def run():
        store, composite, generation = await _direct_fixture()
        try:
            x = await _direct_route(store, generation, "x", state="intent" if prior == "uncertain" else prior)
            delivery_recorded_at = "2026-01-01T00:00:00Z"
            def seed_timing(conn):
                conn.execute("UPDATE v2_assistant_composite_routes SET route_json="
                             "json_set(route_json,'$.timings.delivery_recorded_at',?) WHERE route_id=?",
                             (delivery_recorded_at, x["route_id"]))
                conn.commit()
            await store.submit(seed_timing)
            if prior == "uncertain":
                assert await store.recover_assistant_composite_routes(stream_id=STREAM_ID) == {
                    "requeued_classifying": 0, "retained_uncertain": 1}
            await composite.publish(_final(x), actor_stream_id=ROOT)
            after = await store.get_assistant_composite_route(stream_id=STREAM_ID, input_identity="x")
            payload = json.loads(after["route_json"])
            assert after["delivery_state"] == "landed"
            assert payload["landed_by_publication"] == "publish:dispatch:x"
            assert payload["landed_evidence"] == "own_final"
            assert payload["prior_delivery_state"] == prior
            assert payload["prior_error_code"] == ("recovery_delivery_evidence_required" if prior == "uncertain" else None)
            assert after["error_code"] is None
            assert payload["landed_at"]
            assert payload["timings"]["delivery_recorded_at"] == delivery_recorded_at
        finally:
            await composite.stop()
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("prior", ["intent", "committed_pending", "uncertain"])
def test_exact_bound_result_promotes_direct_delivery(prior):
    async def run():
        store, composite, generation = await _direct_fixture()
        try:
            route = await _direct_route(store, generation, "result-input", state=prior)
            def seed_report(conn):
                conn.execute(
                    "INSERT INTO v2_assistant_composite_terminal_reports VALUES (?,?,?,?,?,?,?,?)",
                    ("result-report", STREAM_ID, "lane-fixture", route["dispatch_id"], ROOT,
                     generation, "digest", "2026-01-01T00:00:00Z"))
                conn.commit()
            await store.submit(seed_report)
            await composite.publish({**_final(route), "publish_kind": "result",
                                     "evidence_refs": ["result-report"]}, actor_stream_id=ROOT)
            after = await store.get_assistant_composite_route(
                stream_id=STREAM_ID, input_identity="result-input")
            payload = json.loads(after["route_json"])
            assert after["delivery_state"] == "landed"
            assert payload["landed_by_publication"] == "publish:dispatch:result-input"
            assert payload["landed_evidence"] == "own_final"
            assert payload["prior_delivery_state"] == prior
            assert (await store.assistant_composite_activity(stream_id=STREAM_ID))[
                "result-input"]["response_state"] == "answered"
        finally:
            await composite.stop()
            store.stop()
    asyncio.run(run())


def test_own_final_after_cover_keeps_earliest_final():
    async def run():
        store, composite, generation = await _direct_fixture()
        try:
            y = await _direct_route(store, generation, "y")
            x = await _direct_route(store, generation, "x", state="intent")
            await composite.publish(_final(x, ["y"]), actor_stream_id=ROOT)
            covered = (await store.assistant_composite_activity(stream_id=STREAM_ID))["y"]
            await composite.publish(_final(y), actor_stream_id=ROOT)
            both = (await store.assistant_composite_activity(stream_id=STREAM_ID))["y"]
            assert both["answered_via"] == "own+burst"
            assert both["final_visible_at"] == covered["final_visible_at"]
            assert set(both["publication_ids"]) == {"publish:dispatch:x", "publish:dispatch:y"}
        finally:
            await composite.stop()
            store.stop()
    asyncio.run(run())


def test_unconfirmed_input_answered_by_own_dispatch_final():
    async def run():
        store, composite, generation = await _direct_fixture()
        try:
            y = await _direct_route(store, generation, "y", state="committed_pending")
            x = await _direct_route(store, generation, "x", state="intent")
            with pytest.raises(ValueError, match="assistant_publish_cover_not_landed"):
                await composite.publish(_final(x, ["y"]), actor_stream_id=ROOT)
            await composite.publish(_final(y), actor_stream_id=ROOT)
            route = await store.get_assistant_composite_route(stream_id=STREAM_ID, input_identity="y")
            assert route["delivery_state"] == "landed"
            assert json.loads(route["route_json"])["landed_evidence"] == "own_final"
            activity = await store.assistant_composite_activity(stream_id=STREAM_ID, input_ids=["y"])
            assert activity["y"]["answered_via"] == "own"
        finally:
            await composite.stop()
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("case,state,expected", [
    ("unknown", "landed", "assistant_publish_cover_unknown"),
    ("intent", "intent", "assistant_publish_cover_not_landed"),
    ("body_maybe_pasted", "committed_pending", "assistant_publish_cover_not_landed"),
    ("submit_unconfirmed", "committed_pending", "assistant_publish_cover_not_landed"),
    ("uncertain", "uncertain", "assistant_publish_cover_not_landed"),
    ("failed", "failed", "assistant_publish_cover_not_landed"),
    ("answered", "landed", "assistant_publish_cover_already_answered"),
    ("other_generation", "landed", "assistant_publish_cover_wrong_binding"),
    ("other_target", "landed", "assistant_publish_cover_wrong_binding"),
    ("linked", "landed", "assistant_publish_cover_already_linked"),
    ("same_id", "landed", "assistant_publish_cover_invalid"),
])
def test_cover_eligibility_errors_are_atomic(case, state, expected):
    async def run():
        store, composite, generation = await _direct_fixture()
        try:
            x = await _direct_route(store, generation, "x", state="intent")
            if case not in ("unknown", "same_id"):
                y = await _direct_route(store, generation, "y", state=state)
                if case in ("other_generation", "other_target"):
                    def change(conn):
                        col = "route_target_generation" if case == "other_generation" else "route_target"
                        conn.execute(f"UPDATE v2_assistant_composite_routes SET {col}=? WHERE route_id=?",
                                     ("other", y["route_id"]))
                        conn.commit()
                    await store.submit(change)
                elif case == "answered":
                    await composite.publish(_final(y), actor_stream_id=ROOT)
                elif case == "linked":
                    def link(conn):
                        conn.execute("INSERT INTO v2_assistant_composite_publication_links "
                                     "VALUES (?,?,?,'dispatch:y','burst','2026-01-01T00:00:00Z')",
                                     (STREAM_ID, "y", "prior:publication"))
                        conn.commit()
                    await store.submit(link)
            covers = ["x" if case == "same_id" else "y"]
            def snapshot(conn):
                return (tuple(conn.execute("SELECT count(*) FROM " + table).fetchone()[0] for table in
                              ("v2_assistant_composite_publications", "session_event_tail",
                               "v2_assistant_composite_publication_links")),
                        tuple(conn.execute("SELECT routing_state,delivery_state,error_code,updated_at,route_json "
                                           "FROM v2_assistant_composite_routes WHERE route_id=?", (x["route_id"],)).fetchone()))
            before = await store.submit(snapshot)
            with pytest.raises(ValueError, match=expected) as raised:
                await composite.publish(_final(x, covers), actor_stream_id=ROOT)
            assert getattr(raised.value, "extra", {}).get("ids") == covers
            if expected == "assistant_publish_cover_not_landed":
                assert raised.value.extra["delivery_state"] == state
            assert await store.submit(snapshot) == before
            receipt = await composite.publish(_final(x, []), actor_stream_id=ROOT)
            assert receipt["covered_message_ids"] == []
        finally:
            await composite.stop()
            store.stop()
    asyncio.run(run())


def test_cover_unsupported_publication_kind_refuses_before_write():
    async def run():
        store, composite, generation = await _direct_fixture()
        try:
            x = await _direct_route(store, generation, "x", state="intent")
            await _direct_route(store, generation, "y")
            message = {**_final(x, ["y"]), "response_state":"acknowledged"}
            with pytest.raises(ValueError, match="assistant_publish_cover_unsupported"):
                await composite.publish(message, actor_stream_id=ROOT)
            assert await store.get_assistant_composite_publication(
                stream_id=STREAM_ID, publication_key=message["request_id"]) is None
        finally:
            await composite.stop()
            store.stop()
    asyncio.run(run())


def test_cover_replay_and_digest_compat():
    async def run():
        store, composite, generation = await _direct_fixture()
        try:
            y = await _direct_route(store, generation, "y")
            x = await _direct_route(store, generation, "x", state="intent")
            message = _final(x, ["y"])
            first = await composite.publish(message, actor_stream_id=ROOT)
            replay = await composite.publish(message, actor_stream_id=ROOT)
            assert first["covered_message_ids"] == replay["covered_message_ids"] == ["y"]
            assert first["duplicate"] is False and replay["duplicate"] is True
            own = await _direct_route(store, generation, "own", state="intent")
            plain = await composite.publish(_final(own), actor_stream_id=ROOT)
            pub = await store.get_assistant_composite_publication(stream_id=STREAM_ID,
                                                                     publication_key=plain["publication_key"])
            assert json.loads(pub["canonical_payload_json"]) == {
                "composite_stream_id": STREAM_ID, "dispatch_id": "dispatch:own",
                "reply_to_message_id": "own", "reply_to_question_id": None,
                "publish_kind": "prose", "message": "fixture answer", "attachment_ids": [],
                "evidence_refs": [], "response_state": "final",
            }
            assert pub["payload_digest"] == "d08c5c36228a954ea76f2082d7d4e20209e8ba8950ec2dd53de61a0338a0bc2c"
        finally:
            await composite.stop()
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("schedule,expected", [
    ("landed_before", "linked"),
    ("admitted_unconfirmed", "assistant_publish_cover_not_landed"),
    ("not_admitted", "assistant_publish_cover_unknown"),
])
def test_cover_publish_race_by_committed_state(schedule, expected):
    async def run():
        store, composite, generation = await _direct_fixture()
        try:
            x = await _direct_route(store, generation, "x", state="intent")
            if schedule != "not_admitted":
                y = await _direct_route(store, generation, "y",
                    state="landed" if schedule == "landed_before" else "intent")
            if expected == "linked":
                assert (await composite.publish(_final(x, ["y"]), actor_stream_id=ROOT))["covered_message_ids"] == ["y"]
            else:
                with pytest.raises(ValueError, match=expected) as raised:
                    await composite.publish(_final(x, ["y"]), actor_stream_id=ROOT)
                if schedule == "admitted_unconfirmed":
                    assert raised.value.extra["delivery_state"] == "intent"
                assert await store.get_assistant_composite_publication(
                    stream_id=STREAM_ID, publication_key="publish:dispatch:x") is None
                if schedule == "not_admitted":
                    y = await _direct_route(store, generation, "y", state="landed")
                else:
                    def landed(conn):
                        conn.execute("UPDATE v2_assistant_composite_routes SET delivery_state='landed' WHERE route_id=?",
                                     (y["route_id"],))
                        conn.commit()
                    await store.submit(landed)
                assert (await composite.publish(_final(x, ["y"]), actor_stream_id=ROOT))["covered_message_ids"] == ["y"]
        finally:
            await composite.stop()
            store.stop()
    asyncio.run(run())


def test_envelope_lists_earlier_inputs_without_dispatch_ids():
    async def run():
        store, composite, generation = await _direct_fixture()
        try:
            landed = await _direct_route(store, generation, "landed")
            pending = await _direct_route(store, generation, "pending", state="committed_pending")
            current = await _direct_route(store, generation, "current", state="intent")
            earlier = await store.list_earlier_unanswered_inputs(stream_id=STREAM_ID, target=ROOT,
                generation=generation, relative_to_event_id=current["event_id"])
            assert [row["message_id"] for row in earlier["coverable"]] == ["landed"]
            assert [row["message_id"] for row in earlier["unconfirmed"]] == ["pending"]
            envelope = direct_dispatch_envelope(composite.config, current, dispatch_id=current["dispatch_id"],
                target=ROOT, generation=generation,
                coverable_earlier_inputs=earlier["coverable"], unconfirmed_earlier_inputs=earlier["unconfirmed"])
            assert "coverable_earlier_inputs_json:" in envelope["wire_body"]
            assert "unconfirmed_earlier_inputs_json:" in envelope["wire_body"]
            assert "--covers-message-ids-json" in envelope["publish_command"]
            assert landed["dispatch_id"] not in envelope["wire_body"]
            assert pending["dispatch_id"] not in envelope["wire_body"]
            assert all(set(item) == {"message_id", "accepted_at"} for item in
                       earlier["coverable"] + earlier["unconfirmed"])
        finally:
            await composite.stop()
            store.stop()
    asyncio.run(run())


def test_envelope_omits_empty_lists_and_answered_interval():
    async def run():
        store, composite, generation = await _direct_fixture()
        try:
            first = await _direct_route(store, generation, "first", state="intent")
            empty = await store.list_earlier_unanswered_inputs(
                stream_id=STREAM_ID, target=ROOT, generation=generation,
                relative_to_event_id=first["event_id"])
            assert empty == {"coverable": [], "unconfirmed": []}
            envelope = direct_dispatch_envelope(
                composite.config, first, dispatch_id=first["dispatch_id"], target=ROOT,
                generation=generation, coverable_earlier_inputs=empty["coverable"],
                unconfirmed_earlier_inputs=empty["unconfirmed"])
            assert "coverable_earlier_inputs_json:" not in envelope["wire_body"]
            assert "unconfirmed_earlier_inputs_json:" not in envelope["wire_body"]
            assert "--covers-message-ids-json" not in envelope["publish_command"]
        finally:
            await composite.stop()
            store.stop()
        store, composite, generation = await _direct_fixture()
        try:
            earlier = await _direct_route(store, generation, "earlier")
            blocker = await _direct_route(store, generation, "blocker")
            await composite.publish(_final(blocker), actor_stream_id=ROOT)
            current = await _direct_route(store, generation, "current", state="intent")
            lists = await store.list_earlier_unanswered_inputs(
                stream_id=STREAM_ID, target=ROOT, generation=generation,
                relative_to_event_id=current["event_id"])
            assert earlier["input_identity"] not in [item["message_id"] for item in lists["coverable"]]
            assert lists == {"coverable": [], "unconfirmed": []}
        finally:
            await composite.stop()
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("x_first,inside,answered,allowed", [
    (True, True, True, False), (False, True, True, False),
    (True, False, True, True), (True, True, False, True),
])
def test_contiguity_interval_cases(x_first, inside, answered, allowed):
    async def run():
        store, composite, generation = await _direct_fixture()
        try:
            if inside:
                first, second, third = (("x", "w", "y") if x_first else ("y", "w", "x"))
            else:
                first, second, third = "x", "y", "w"
            routes = {}
            for name in (first, second, third):
                routes[name] = await _direct_route(store, generation, name,
                                                   state="intent" if name == "x" else "landed")
            if answered:
                await composite.publish(_final(routes["w"]), actor_stream_id=ROOT)
            if allowed:
                receipt = await composite.publish(_final(routes["x"], ["y"]), actor_stream_id=ROOT)
                assert receipt["covered_message_ids"] == ["y"]
            else:
                with pytest.raises(ValueError, match="assistant_publish_cover_not_contiguous") as raised:
                    await composite.publish(_final(routes["x"], ["y"]), actor_stream_id=ROOT)
                assert raised.value.extra["blocking_message_id"] == "w"
                assert await store.get_assistant_composite_publication(
                    stream_id=STREAM_ID, publication_key="publish:dispatch:x") is None
        finally:
            await composite.stop()
            store.stop()
    asyncio.run(run())


def test_contiguity_rejects_intervening_link_and_accepts_both_sides():
    async def run():
        store, composite, generation = await _direct_fixture()
        try:
            z = await _direct_route(store, generation, "z")
            x = await _direct_route(store, generation, "x", state="intent")
            w = await _direct_route(store, generation, "w")
            y = await _direct_route(store, generation, "y")
            await composite.publish(_final(z, ["w"]), actor_stream_id=ROOT)
            with pytest.raises(ValueError, match="assistant_publish_cover_not_contiguous") as raised:
                await composite.publish(_final(x, ["y"]), actor_stream_id=ROOT)
            assert raised.value.extra["blocking_message_id"] == "w"
        finally:
            await composite.stop()
            store.stop()
        store, composite, generation = await _direct_fixture()
        try:
            left = await _direct_route(store, generation, "left")
            x = await _direct_route(store, generation, "x", state="intent")
            right = await _direct_route(store, generation, "right")
            receipt = await composite.publish(_final(x, ["right", "left"]), actor_stream_id=ROOT)
            assert receipt["covered_message_ids"] == ["left", "right"]
            activity = await store.assistant_composite_activity(stream_id=STREAM_ID)
            assert activity["left"]["answered_via"] == activity["right"]["answered_via"] == "burst"
        finally:
            await composite.stop()
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("delivery,reason,exception_type", [
    ("committed_pending_proof", "submit_unconfirmed", None),
    ("landed", None, None),
    ("not_landed", "active_draft", None),
    (None, None, "RuntimeError"),
])
def test_final_before_late_receipt_is_monotonic(delivery, reason, exception_type, caplog):
    caplog.set_level(logging.INFO, logger="chat_streamd_v2.assistant_composite")
    async def run():
        store, composite, generation = await _direct_fixture()
        try:
            route = await _direct_route(store, generation, "x", state="intent")
            entered = asyncio.Event()
            release = asyncio.Event()
            async def dispatch(_route):
                entered.set()
                await release.wait()
                if exception_type:
                    raise RuntimeError("transport failed")
                return {"delivery": delivery, **({"reason": reason} if reason else {})}
            composite.dispatch = dispatch
            task = asyncio.create_task(composite._dispatch_one(route))
            await entered.wait()
            await composite.publish(_final(route), actor_stream_id=ROOT)
            release.set()
            await task
            after = await store.get_assistant_composite_route(stream_id=STREAM_ID, input_identity="x")
            assert after["delivery_state"] == "landed"
            note = json.loads(after["route_json"])["late_receipts"][0]
            assert note["observed_delivery"] == delivery
            assert note["reason"] == (f"dispatch_exception:{exception_type}" if exception_type else reason)
            assert note["exception_type"] == exception_type
            assert note["at"]
            assert any(r.levelname == ("WARNING" if delivery == "not_landed" or exception_type else "INFO")
                       and "late dispatch" in r.message for r in caplog.records)
        finally:
            await composite.stop()
            store.stop()
    asyncio.run(run())


def test_authority_result_does_not_promote_uncertain_route():
    async def run():
        store, composite, generation = await _direct_fixture()
        try:
            route = await _direct_route(store, generation, "x", state="uncertain")
            await store.record_assistant_composite_publication(
                stream_id=STREAM_ID, publication_key="authority:result", dispatch_id=route["dispatch_id"],
                reply_to_message_id="x", reply_to_question_id=None, publish_kind="result",
                attachment_ids=[], evidence_refs=[],
                canonical_payload={"response_state":"final", "message":"authority result"},
                event={"kind":"ASSIST_TEXT", "text":"authority result", "timestamp":"2026-01-01T00:00:00Z"},
            )
            after = await store.get_assistant_composite_route(stream_id=STREAM_ID, input_identity="x")
            assert after["delivery_state"] == "uncertain"
            activity = await store.assistant_composite_activity(stream_id=STREAM_ID, input_ids=["x"])
            assert activity["x"]["response_state"] == "uncertain"
        finally:
            await composite.stop()
            store.stop()
    asyncio.run(run())


def test_stale_generation_final_does_not_promote():
    async def run():
        store, composite, generation = await _direct_fixture()
        try:
            route = await _direct_route(store, generation, "x", state="committed_pending")
            with pytest.raises(ValueError, match="assistant_direct_actor_unverified"):
                await store.record_assistant_composite_publication(
                    stream_id=STREAM_ID, publication_key="publish:dispatch:x", dispatch_id=route["dispatch_id"],
                    reply_to_message_id="x", reply_to_question_id=None, publish_kind="prose",
                    attachment_ids=[], evidence_refs=[], canonical_payload={"response_state":"final"},
                    event={"kind":"ASSIST_TEXT", "text":"answer", "timestamp":"2026-01-01T00:00:00Z"},
                    actor_stream_id=ROOT, actor_generation=generation,
                    direct_target_stream_id=ROOT, direct_target_generation="old-generation", direct_single_final=True,
                )
            assert (await store.get_assistant_composite_route(stream_id=STREAM_ID,
                                                                input_identity="x"))["delivery_state"] == "committed_pending"
        finally:
            await composite.stop()
            store.stop()
    asyncio.run(run())


def test_restart_after_new_code_final_stays_landed(tmp_path):
    async def run():
        path = tmp_path / "routes.db"
        store = Store(str(path))
        store.start()
        composite = None
        try:
            root = await store.open_session("fixture-root", "visible", provider="codex")
            generation = root["session_generation"]
            config = AssistantCompositeConfig.from_env({
                "PENTACLE_ASSISTANT_COMPOSITE_ENABLED":"1",
                "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID":STREAM_ID,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID":ROOT,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION":generation,
            })
            composite = AssistantComposite(store, config=config)
            await composite.ensure_projection()
            x = await _direct_route(store, generation, "x", state="intent")
            await _direct_route(store, generation, "sibling", state="intent")
            entered = asyncio.Event()
            release = asyncio.Event()
            async def blocked_dispatch(_route):
                entered.set()
                await release.wait()
                return {"delivery": "landed"}
            composite.dispatch = blocked_dispatch
            composite._start_dispatch(x)
            await entered.wait()
            assert any(not task.done() for task in composite._dispatch_tasks)
            await composite.publish(_final(x), actor_stream_id=ROOT)
            assert (await store.get_assistant_composite_route(
                stream_id=STREAM_ID, input_identity="x"))["delivery_state"] == "landed"
            assert not release.is_set()
        finally:
            if composite:
                await composite.stop()
            store.stop()
        reopened = Store(str(path))
        reopened.start()
        try:
            counts = await reopened.recover_assistant_composite_routes(stream_id=STREAM_ID)
            assert counts == {"requeued_classifying": 0, "retained_uncertain": 1}
            x = await reopened.get_assistant_composite_route(stream_id=STREAM_ID, input_identity="x")
            sibling = await reopened.get_assistant_composite_route(stream_id=STREAM_ID, input_identity="sibling")
            assert x["delivery_state"] == "landed"
            assert sibling["delivery_state"] == "uncertain"
        finally:
            reopened.stop()
    asyncio.run(run())


def test_legacy_intent_with_final_still_becomes_uncertain_without_tool():
    async def run():
        store, composite, generation = await _direct_fixture()
        try:
            route = await _direct_route(store, generation, "x", state="intent")
            def legacy_final(conn):
                conn.execute("INSERT INTO v2_assistant_composite_publications "
                             "(publication_key,stream_id,payload_digest,canonical_payload_json,dispatch_id,"
                             "reply_to_message_id,publish_kind,event_id,created_at) "
                             "VALUES ('legacy',?,?,?,?,'x','prose',100,'2026-01-01T00:00:00Z')",
                             (STREAM_ID, 'a'*64, json.dumps({"response_state":"final"}), route["dispatch_id"]))
                conn.commit()
            await store.submit(legacy_final)
            counts = await store.recover_assistant_composite_routes(stream_id=STREAM_ID)
            assert counts == {"requeued_classifying":0, "retained_uncertain":1}
            after = await store.get_assistant_composite_route(stream_id=STREAM_ID, input_identity="x")
            assert after["delivery_state"] == "uncertain"
        finally:
            await composite.stop()
            store.stop()
    asyncio.run(run())


def test_covering_final_projects_landed_input() -> None:
    async def run():
        cell = _load_fixture()["cases"][1]["cells"][0]
        store = Store(":memory:")
        store.start()
        try:
            await _seed(store, cell)
            ids = [r["input_identity"] for r in cell["routes"]]
            def insert(conn):
                conn.execute("UPDATE v2_assistant_composite_routes SET routing_state='resolved',delivery_state='landed'")
                conn.execute("INSERT INTO v2_assistant_composite_publications "
                    "(publication_key,stream_id,payload_digest,canonical_payload_json,dispatch_id,reply_to_message_id,publish_kind,event_id,created_at) "
                    "VALUES ('publish:x',?,?,?,'dispatch-x',?,'prose',10,'2026-09-20T00:00:02.000Z')",
                    (STREAM_ID, 'a'*64, json.dumps({'response_state':'final'}), ids[0]))
                conn.execute("INSERT INTO v2_assistant_composite_publication_links "
                    "(stream_id,covered_message_id,publication_key,covered_dispatch_id,link_kind,created_at) "
                    "VALUES (?,?,'publish:x','dispatch-y','burst','2026-09-20T00:00:02.000Z')", (STREAM_ID, ids[1]))
                conn.commit()
            await store.submit(insert)
            activity = await store.assistant_composite_activity(stream_id=STREAM_ID)
            assert activity[ids[0]]["answered_via"] == "own"
            assert activity[ids[1]]["response_state"] == "answered"
            assert activity[ids[1]]["answered_via"] == "burst"
            assert activity[ids[1]]["covered_by_publication_key"] == "publish:x"
            assert activity[ids[1]]["publication_ids"] == ["publish:x"]
            assert summarize_activity(activity)["pending_count"] == 0
            route = await store.get_assistant_composite_route(stream_id=STREAM_ID, input_identity=ids[1])
            assert route["delivery_state"] == "landed"
        finally:
            store.stop()
    asyncio.run(run())


def test_covered_input_leaves_live_set_and_survives_target_close() -> None:
    async def run():
        cell = _load_fixture()["cases"][1]["cells"][0]
        store = Store(":memory:")
        store.start()
        try:
            await _seed(store, cell)
            route = cell["routes"][0]
            def insert(conn):
                conn.execute("UPDATE v2_assistant_composite_routes SET routing_state='resolved',delivery_state='landed' WHERE route_id=?", (route["route_id"],))
                conn.execute("INSERT INTO v2_assistant_composite_publications "
                    "(publication_key,stream_id,payload_digest,canonical_payload_json,dispatch_id,reply_to_message_id,publish_kind,event_id,created_at) "
                    "VALUES ('publish:other',?,?,?,'dispatch-other','other','prose',100,'2026-09-20T00:00:02.000Z')",
                    (STREAM_ID, 'b'*64, json.dumps({'response_state':'final'})))
                conn.execute("INSERT INTO v2_assistant_composite_publication_links "
                    "(stream_id,covered_message_id,publication_key,covered_dispatch_id,link_kind,created_at) "
                    "VALUES (?,?,'publish:other',?,'burst','2026-09-20T00:00:02.000Z')",
                    (STREAM_ID, route["input_identity"], route["dispatch_id"]))
                for i in range(2001):
                    conn.execute("INSERT INTO v2_assistant_composite_routes "
                        "(route_id,stream_id,input_identity,payload_digest,input_request_id,event_id,body,routing_state,created_at,updated_at) "
                        "VALUES (?,?,?,'d','r',?,'', 'routing_failed',?,?)",
                        (f'new-{i}', STREAM_ID, f'new-{i}', i+1000, f'2026-09-21T00:00:{i%60:02d}Z', f'2026-09-21T00:00:{i%60:02d}Z'))
                conn.commit()
            await store.submit(insert)
            live = await store.assistant_composite_activity(stream_id=STREAM_ID)
            assert route["input_identity"] not in live
            history = await store.assistant_composite_activity(stream_id=STREAM_ID, input_ids=[route["input_identity"]])
            assert history[route["input_identity"]]["answered_via"] == "burst"
            changed = await store.fail_closed_assistant_routes(stream_id=STREAM_ID,
                target_stream_id=route["route_target"], target_generation=route["route_target_generation"])
            assert changed == 0
        finally:
            store.stop()
    asyncio.run(run())
