"""Synthetic replay harness: callers enter real wire auth, never private fields."""
import asyncio
import hashlib
import json
import os
from unittest.mock import patch
from contextlib import asynccontextmanager

from _shared import operator_auth
from assistant_composite import AssistantComposite, AssistantCompositeConfig
from ledger import Ledger
from notification_answer_fixture import fixture
from server import Server
from store import STREAM_TOKEN_HASH_VERSION
from test_front_desk_digest import _production_dispatcher
from test_assistant_lane_rulings import RecordingSpawn
from provider_wrappers import normalize_provider_user_text

HOST = "fixture-host"
DESK = HOST + ":v2-test"
ASSISTANT = HOST + ":assistant"

class RemoteSocket:
    remote_address = ("192.0.2.40", 42420)
    transport = None

@asynccontextmanager
async def harness(tmp_path):
    with patch.dict(os.environ, {"PENTACLE_FRONT_DESK_DIGEST_ENABLED":"1", "PENTACLE_FRONT_DESK_DIGEST_S":"3600"}):
        async with _harness(tmp_path) as value:
            yield value

@asynccontextmanager
async def _harness(tmp_path):
    async with fixture(tmp_path, host=HOST) as (notify, queue, comms, provider, sessions, store):
        # The reused fixture's provider is single-pane; keep other synthetic
        # targets separate so advisor setup delivery is not a desk publication.
        original_paste = provider.paste
        other_pastes = []
        async def targeted_paste(name, text):
            if name == "v2-test": return await original_paste(name, text)
            other_pastes.append((name, text))
        provider.paste = targeted_paste
        generation = sessions.get(DESK)["session_generation"]
        await store.update_session(HOST, "v2-test", pane_pid="4242")
        tokens = {}
        for alias, name in [("desk", "v2-test"), ("peer", "peer"), ("advisor", "advisor"), ("child", "child")]:
            token = "synthetic-th-h4-" + alias
            tokens[alias] = token
            if alias != "desk":
                await sessions.open(HOST, name, provider="shell" if alias == "child" else "codex", pane_status="pane_alive",
                                    parent_stream_id=DESK if alias == "child" else None)
            await store.grant_stream_token(HOST, name, hashlib.sha256(token.encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION)
        composite = AssistantComposite(store, config=AssistantCompositeConfig(
            enabled=True, name="bart", stream_id=ASSISTANT,
            direct_primary_stream_id=DESK, direct_primary_generation=generation,
            astra_stream_id=DESK, authority_stream_id=HOST + ":advisor"),
            dispatch=_production_dispatcher(comms))
        comms.front_desk_digest = composite.front_desk_digest
        comms.assistant_ingress_policy = composite.suppress_routine_backend_ingress
        queue.front_desk_digest = composite.front_desk_digest
        spawned = RecordingSpawn()
        ledger = Ledger(store, sessions, comms, outbound=queue)
        server = Server(store=store, sessions=sessions, comms=comms, ledger=ledger, spawnctl=spawned, local_host=HOST, seat_operator_authority=False)
        server.assistant_composite = composite
        await composite.ensure_projection()
        directory = tmp_path / "operator-auth"; directory.mkdir(mode=0o700)
        registry = operator_auth.OperatorCredentialRegistry(directory / "credentials.json")
        credential, envelope = registry.issue("pentacle-mobile")
        server.operator_credential_registry = registry
        operator = RemoteSocket()
        nonce, expiry = operator_auth.new_nonce()
        server._operator_challenges[operator] = (nonce, expiry)
        hello = {"type":"hello", "client":"pentacle-mobile", "subscribe":{"mode":"rpc","snapshot":False},
                 "auth_v2":{"scheme":operator_auth.AUTH_SCHEME,"credential_id":credential,
                  "proof":operator_auth.make_proof(operator_auth.decode_envelope(envelope)["proof_key"],nonce,credential,"pentacle-mobile")}}
        accepted = await server._dispatch(json.dumps(hello), websocket=operator)
        assert accepted[0]["type"] == "ready" and accepted[0]["snapshot"] is False, accepted
        assert server._operator_authenticated(operator), accepted

        sockets = {}
        for alias, token in tokens.items():
            socket = RemoteSocket()
            owner = DESK if alias == "desk" else HOST + ":" + alias
            hello_reply = await server._dispatch(json.dumps({"type":"hello","client":"synthetic-seat",
                "subscribe":{"mode":"rpc","snapshot":False},"stream_token":token,"from_stream_id":owner}),websocket=socket)
            assert hello_reply[0]["type"] == "ready" and hello_reply[0]["snapshot"] is False, hello_reply
            assert server._client_authenticated_streams.get(socket) == owner
            sockets[alias] = socket

        async def dispatch(frame, principal):
            socket = operator if principal == "operator" else sockets.get(principal) or RemoteSocket()
            return (await server._dispatch(json.dumps(frame), websocket=socket))[0]

        async def counts():
            return {
                "pane_submissions": len(provider.pastes),
                "held_rows": len(await composite.front_desk_digest._rows(DESK)),
                "canonical_publications": await store.submit(lambda conn: conn.execute(
                    "SELECT count(*) FROM v2_assistant_composite_publications WHERE stream_id=?", (ASSISTANT,)).fetchone()[0]),
                "report_rows": await store.submit(lambda conn: conn.execute("SELECT count(*) FROM v2_reports").fetchone()[0]),
                "ruling_notices": await store.submit(lambda conn: conn.execute(
                    "SELECT count(*) FROM v2_outbound_notices WHERE kind='assistant_lane_ruling_result' AND recipient_stream_id=?", (DESK,)).fetchone()[0]),
            }
        try:
            yield locals()
        finally:
            await composite.stop()
            await server.lane_rulings.stop()

async def replay(case, tmp_path):
    async with harness(tmp_path) as h:
        dispatch, store, composite = h["dispatch"], h["store"], h["composite"]
        principal = case["principal"]
        frame = dict(case["input"])
        kind = case["class"]
        before = await h["counts"]()
        if kind in {"desk_email", "desk_sms", "pasted_wrapper"}:
            body = frame["body"]
            if kind == "pasted_wrapper":
                body, _ = normalize_provider_user_text(body, provider=frame.get("provider","claude"), authenticated=principal=="provider")
            result = await composite.front_desk_digest.ingress(target_stream_id=DESK, body=body,
                msg={"tell_id":case["id"]}, verb="tell")
            after = await h["counts"]()
            decision = "wake" if result is None else "hold" if after["held_rows"] > before["held_rows"] else "drop"
            # Parser/classifier-only seams have no transport and publish nothing.
            again = await composite.front_desk_digest.ingress(target_stream_id=DESK, body=body,
                msg={"tell_id":case["id"]}, verb="tell")
            assert again == result
            assert await h["counts"]() == after
            return {"decision":decision, "landed_state":"classified", "counts":after}
        if kind == "lane_ruling":
            request = await dispatch({"type":"spawn","host":HOST,"session_name":"synthetic-lane",
                "role":"lead","request_id":"setup-"+case["id"],"idempotency_key":"setup-"+case["id"],
                "objective":"Synthetic approved lane"}, "desk")
            assert request["type"] == "spawn.pending_ruling", request
            frame["ruling_request_id"] = request["ruling_request_id"]
        first = await dispatch(frame, principal)
        error = str(first.get("type","")).endswith(".error")
        if kind == "operator_dispatch" and not error:
            identity = frame["msg_id"]
            route = await store.get_assistant_composite_route(stream_id=ASSISTANT,input_identity=identity)
            for _ in range(100):
                if route and route["delivery_state"] == "landed": break
                await asyncio.sleep(.01)
                route = await store.get_assistant_composite_route(stream_id=ASSISTANT,input_identity=identity)
            assert route and route["delivery_state"] == "landed", (first,route)
            publication = {"type":"assistant.publish","request_id":"publish:"+route["dispatch_id"],"composite_stream_id":ASSISTANT,
                "dispatch_id":route["dispatch_id"],"reply_to_message_id":identity,"reply_to_question_id":None,
                "publish_kind":"prose","response_state":"final","message":"Synthetic final answer",
                "attachment_ids":[],"evidence_refs":[]}
            pub = await dispatch(publication,"desk")
            assert pub["type"] == "assistant.publish.ok", pub
            assert (await dispatch(publication,"desk"))["duplicate"] is True
        if kind == "lane_ruling": await h["queue"].drain_once(force=True)
        after = await h["counts"]()
        spawn_count = len(h["spawned"].calls)
        if kind == "lane_ruling": assert spawn_count == (1 if first.get("state") == "done" else 0)
        duplicate = await dispatch(frame,principal)
        assert duplicate["type"] == first["type"], (first, duplicate)
        if error:
            assert first.get("error_code") == case["expected"]["error_code"], first
            assert duplicate.get("error_code") == first.get("error_code"), duplicate
        elif kind == "operator_dispatch":
            assert duplicate["assistant_composite"]["duplicate"] is True
            assert duplicate["state"] == first["state"]
        elif kind == "lane_ruling":
            assert duplicate["duplicate"] is True and duplicate["state"] == first["state"]
        elif kind == "child_report":
            assert duplicate["report_id"] == first["report_id"]
            assert duplicate["ledger_row_id"] == first["ledger_row_id"]
        else:
            assert duplicate["delivery_status"] == first["delivery_status"]
        if kind == "lane_ruling": await h["queue"].drain_once(force=True)
        assert len(h["spawned"].calls) == spawn_count
        assert await h["counts"]() == after, (case["id"],first,duplicate,after,await h["counts"]())
        decision = "drop" if error else "hold" if after["held_rows"] > before["held_rows"] else "wake" if after["pane_submissions"] > before["pane_submissions"] else "drop"
        state = "rejected" if error else first.get("state") or first.get("delivery_status") or ("durable" if kind=="child_report" else "accepted")
        return {"decision":decision,"landed_state":state,"counts":after,"reply":first}
