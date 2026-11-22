"""Exercise advisor-rule provenance through authenticated disposable sockets."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import hashlib

import pytest

from _shared import operator_auth
from assistant_lane_rulings import AssistantLaneRulings
from codex_rollout_norm import normalize_codex_rollout_record
from comms import Comms
from ledger import Ledger
from sessions import Sessions, VerbError
from spawnctl import SpawnCtl
from smoke.test_connection_logging_socket import _connection
from store import Store, STREAM_TOKEN_HASH_VERSION
from test_assistant_composite_addressing import _Harness, _NamedTmux, COMPOSITE_CHAT, HOST, SEAT_A, SEAT_B, SENDER


WORDS = "Change the café heading.\r\nUse the draft label.  "
MESSAGE_ID = "fixture-operator-message-complete-id"
TELL_ID = "fixture-dispatch-complete-id"
ORIGIN = "registered_operator_ws_v1"


class RuleHarness(_Harness):
    def __init__(self, tmp_path):
        self.tmux = _NamedTmux()
        self.store = Store(str(tmp_path / "advisor-rules.db"))
        self.store.start()
        self.sessions = Sessions(self.store, tmux=self.tmux, local_host=HOST)
        self.comms = Comms(self.store, self.sessions,
                          SpawnCtl(self.store, self.sessions, tmux=self.tmux),
                          attachment_root=tmp_path / "attachments")
        self.server = self.composite = None
        self.gen = {}

    async def rotate_fd(self):
        await self.store.recover_assistant_binding(
            name=self.composite.env_config.name, target_stream_id=SENDER,
            target_generation=self.gen[SENDER],
        )
        await self.composite.load_binding()


@asynccontextmanager
async def rule_fixture(tmp_path):
    harness = RuleHarness(tmp_path)
    try:
        await harness.start(front_desk=True)
        server = harness.server
        server.binds = ["127.0.0.1"]
        server.port = 0
        server.operator_credential_registry = operator_auth.OperatorCredentialRegistry(tmp_path / "operators.json")
        harness.composite.env_config = replace(harness.composite.env_config, authority_stream_id=SEAT_B)
        server.lane_rulings = AssistantLaneRulings(server)
        server.ledger = Ledger(harness.store, sessions=harness.sessions)
        paste = harness.tmux.paste

        async def record_paste(name, text, **options):
            harness.paste_options.append(options)
            await paste(name, text)
            await record_native(harness, name, text)

        harness.paste_options = []
        harness.tmux.paste = record_paste
        _, envelope = server.operator_credential_registry.issue("pentacle")
        await server.bind()
        yield harness, envelope
    finally:
        if harness.server is not None:
            await harness.server.close()
        harness.store.stop()


async def record_native(harness, name, text):
    host = SEAT_B.split(":")[0]
    event = normalize_codex_rollout_record({
        "type": "response_item", "timestamp": "2026-10-31T22:00:00Z",
        "payload": {"type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": text}]},
    }, host=host, session_name=name, session_id="synthetic-native")[0]
    await harness.store.append_session_event(f"{host}:{name}", event,
        identity=f"fixture-native-{len(harness.tmux.inputs)}-{hashlib.sha256(text.encode()).hexdigest()}",
        limit=100)


@asynccontextmanager
async def operator(harness, envelope):
    async with _connection(harness.server) as socket:
        ready = await socket.hello(envelope=envelope, subscribe={
            "snapshot": False, "mode": "rpc", "assistant_composite_v1": True,
        }, capabilities={"assistant_composite_v1": True})
        assert ready[0]["type"] in {"ready", "hello"}
        yield socket


@asynccontextmanager
async def seat(harness, stream):
    host, name = stream.split(":")
    token = "synthetic-seat-token-" + name
    await harness.store.grant_stream_token(host, name, hashlib.sha256(token.encode()).hexdigest(),
                                          STREAM_TOKEN_HASH_VERSION)
    async with _connection(harness.server) as socket:
        auth = {"from_stream_id": stream, "stream_token": token}
        assert (await socket.hello(client="agent-orch", subscribe={"snapshot": False, "mode": "rpc"},
                                   **auth))[0]["type"] == "ready"

        async def rpc(verb, **fields):
            return await socket.rpc(verb, **auth, **fields)

        yield rpc


async def input_row(harness, identity):
    return await harness.store.get_assistant_composite_route(stream_id=COMPOSITE_CHAT, input_identity=identity)


async def admit(socket, identity=MESSAGE_ID, body=WORDS, **extra):
    return await socket.rpc("send", to_stream_id=COMPOSITE_CHAT, text=body,
                            optimistic_id=identity, request_id="fixture-send-" + identity, **extra)


async def forward(fd, identity=MESSAGE_ID, body=WORDS, tell_id=TELL_ID, target=SEAT_B):
    return await fd("tell", to_stream_id=target, text=body, advisor_rule_message_id=identity, tell_id=tell_id)


def rule_report(report_id="fixture-rule-report", **review_fields):
    return {
        "report_id": report_id, "status": "progress", "msg_id": 0,
        "extras": {"advisor_rule": {
            "version": 1, "verdict": "accept", "operator_message_id": MESSAGE_ID, "fd_event_id": TELL_ID,
            "path": "docs/reference/assistant_bart_context.md", "base_revision": "a" * 64,
            "new_bytes_sha256": "b" * 64, "scope": {"mode": "single", "removed": [], "added": ["c" * 64]},
            **review_fields,
        }},
    }


def test_only_direct_registered_operator_stamps_the_first_admission(tmp_path):
    async def run():
        async with rule_fixture(tmp_path) as (h, envelope), operator(h, envelope) as socket:
            credential_id = operator_auth.decode_envelope(envelope)["credential_id"]
            principal = "operator:" + credential_id
            await h.composite.accept_input({"text": WORDS, "msg_id": "server-only"}, operator_principal=principal)
            await h.server._on_send({"to_stream_id": COMPOSITE_CHAT, "text": WORDS,
                "msg_id": "finisher", "_auth_context": {
                    "operator_authenticated": True, "operator_principal": principal, "outbound_finisher": True,
                }})
            for identity, actor in (("local-admin", "operator:local-admin"), ("null-actor", None)):
                await h.server._on_send({"to_stream_id": COMPOSITE_CHAT, "text": WORDS, "msg_id": identity,
                    "_auth_context": {"operator_authenticated": True, "operator_principal": actor,
                                      "local_admin_verified": identity == "local-admin"}})
                row = await input_row(h, identity)
                assert row["actor_stream_id"] == actor and row["operator_origin"] is None
            async with seat(h, SENDER) as sender:
                h.server._seat_operator_authority = True
                result = await sender("send", to_stream_id=COMPOSITE_CHAT, text=WORDS, msg_id="elevated")
                assert result["type"] == "send.result"
                assert (await input_row(h, "elevated"))["operator_origin"] is None
                h.server._seat_operator_authority = False
            _, scoped = h.server.operator_credential_registry.issue("pentacle-mobile", scope={"stream": COMPOSITE_CHAT})
            async with operator(h, scoped) as client:
                scoped_reply = await admit(client, "scoped")
                assert scoped_reply["type"] == "send.result", scoped_reply.get("error_code")
                assert (await input_row(h, "scoped"))["operator_origin"] is None
            assert (await admit(socket, "server-only"))["assistant_composite"]["duplicate"]
            assert (await admit(socket, _operator_origin="forged"))["type"] == "send.result"
            assert (await admit(socket, "other"))["type"] == "send.result"
            for identity in ("server-only", "finisher"):
                row = await input_row(h, identity)
                assert row["actor_stream_id"] == principal
                assert row.get("operator_origin") is None
            stamped = await input_row(h, MESSAGE_ID)
            assert stamped["actor_stream_id"] == principal
            assert stamped.get("operator_origin") == ORIGIN
            assert (await input_row(h, "other")).get("operator_origin") == ORIGIN

    asyncio.run(run())


def test_authenticated_fd_requires_exact_original_and_records_dispatch(tmp_path):
    async def run():
        async with rule_fixture(tmp_path) as (h, envelope), operator(h, envelope) as socket:
            assert (await admit(socket))["type"] == "send.result"
            async with seat(h, SEAT_A) as fd:
                ordinary = await fd("tell", to_stream_id=SEAT_B, text="Fixture status.", tell_id="ordinary")
                assert ordinary["delivery_status"] == "delivered"
                assert (await h.store.get_tell_delivery("ordinary")).get("advisor_rule_intent") is None
                wrong = await forward(fd, body=WORDS.rstrip())
                assert wrong.get("error_code") == "advisor_rule_words_mismatch"
                assert await h.store.get_tell_delivery(TELL_ID) is None
                delivered = await forward(fd)
                assert delivered["delivery_status"] == "delivered" and delivered["submission_confirmed"] is True
                evidence = await h.store.get_tell_delivery(TELL_ID)
                assert evidence["advisor_rule_intent"]["operator_message_id"] == MESSAGE_ID
                assert evidence["advisor_rule_intent"]["fd_binding_revision"] == 0
                assert evidence["delivery"]["text"].endswith("\n" + WORDS)
                assert h.paste_options == [{}, {"preserve_newlines": True}]
    asyncio.run(run())


def test_full_primary_key_literal_words_and_tell_id_collisions(tmp_path, monkeypatch):
    from test_receipt_route_retention import move, r

    async def run():
        async with rule_fixture(tmp_path) as (h, envelope), operator(h, envelope) as socket:
            assert (await admit(socket))["type"] == "send.result"
            assert (await admit(socket, "fixture-second-input"))["type"] == "send.result"
            await h.composite.accept_input({"text": WORDS, "msg_id": "legacy"}, operator_principal="operator:fixture")

            def settle(conn):
                conn.execute(f"UPDATE {r.ROUTE_TABLE} SET routing_state='resolved',delivery_state='landed',"
                             "created_at='2026-08-01T00:00:00Z',updated_at='2026-08-01T00:00:00Z'")
                # A stamped row in the wrong composite cannot supply provenance.
                wrong = dict(conn.execute(f"SELECT * FROM {r.ROUTE_TABLE} WHERE input_identity=?",
                                          ("fixture-second-input",)).fetchone())
                wrong.update(route_id="fixture-other-composite", stream_id="fixture:other",
                             input_identity="fixture-wrong-primary", event_id=None)
                columns = ",".join(wrong)
                placeholders = ",".join("?" for _ in wrong)
                conn.execute(f"INSERT INTO {r.ROUTE_TABLE} ({columns}) VALUES ({placeholders})", tuple(wrong.values()))
                conn.commit()
            await h.store.submit(settle)
            # Relocate only the fixture rows using the real copy and CAS.
            moved_ids = {MESSAGE_ID, "fixture-second-input", "legacy", "fixture-wrong-primary"}
            monkeypatch.setattr(r, "_settled_route", lambda conn, row, cutoff: row["input_identity"] in moved_ids)
            assert (await move(h.store, r.ROUTE_TABLE))["moved"] == 4
            assert (await input_row(h, MESSAGE_ID))["operator_origin"] == ORIGIN
            assert (await input_row(h, "legacy"))["operator_origin"] is None

            async with seat(h, SEAT_A) as fd:
                for absent in ("legacy", "fixture-wrong-primary", MESSAGE_ID[:12], "bare"):
                    result = await forward(fd, identity=absent)
                    assert result.get("error_code") == "advisor_rule_operator_unverified"
                for changed in ("Paraphrase", WORDS.replace("\r\n", "\n"), WORDS.replace("é", "e\u0301")):
                    assert (await forward(fd, body=changed)).get("error_code") == "advisor_rule_words_mismatch"
                assert (await forward(fd, target=SENDER)).get("error_code") == "advisor_rule_advisor_unverified"
                assert (await fd("tell", to_stream_id=SEAT_B, text=WORDS, tell_id="ordinary"))["type"] == "tell.ok"
                assert (await forward(fd, tell_id="ordinary")).get("error_code") == "tell_id_conflict"
                assert (await forward(fd))["delivery_status"] == "delivered"
                assert (await forward(fd, identity="fixture-second-input")).get("error_code") == "tell_id_conflict"
                assert (await forward(fd, identity="fixture-second-input", tell_id="typed-second"))["delivery_status"] == "delivered"
                ordinary = await fd("tell", to_stream_id=SEAT_B, text=WORDS, tell_id=TELL_ID)
                assert ordinary.get("error_code") == "tell_id_conflict"
            assert len(h.tmux.inputs) == 3

    asyncio.run(run())


@pytest.mark.parametrize("reserve_first", [False, True])
def test_fd_rotation_linearizes_at_dispatch_writer(tmp_path, monkeypatch, reserve_first):
    async def run():
        async with rule_fixture(tmp_path) as (h, envelope), operator(h, envelope) as socket:
            assert (await admit(socket))["type"] == "send.result"
            entered, release = asyncio.Event(), asyncio.Event()
            reserve = h.store.reserve_advisor_rule_tell

            async def at_reservation(*args, **kwargs):
                if reserve_first:
                    result = await reserve(*args, **kwargs)
                entered.set()
                await release.wait()
                return result if reserve_first else await reserve(*args, **kwargs)

            async with seat(h, SEAT_A) as fd, seat(h, SENDER) as successor:
                monkeypatch.setattr(h.store, "reserve_advisor_rule_tell", at_reservation)
                pending = asyncio.create_task(forward(fd))
                try:
                    await asyncio.wait_for(entered.wait(), 3)
                    await h.rotate_fd()
                finally:
                    release.set()
                result = await pending
                if reserve_first:
                    assert result["delivery_status"] == "delivered"
                    intent = (await h.store.get_tell_delivery(TELL_ID))["advisor_rule_intent"]
                    assert (intent["fd_stream_id"], intent["fd_binding_revision"]) == (SEAT_A, 0)
                else:
                    assert result.get("error_code") == "advisor_rule_fd_unverified"
                    assert await h.store.get_tell_delivery(TELL_ID) is None
                assert (await forward(fd, tell_id="after-rebind")).get("error_code") == "advisor_rule_fd_unverified"
                assert (await forward(successor, tell_id="new-desk"))["delivery_status"] == "delivered"
                assert (await h.store.get_tell_delivery("new-desk"))["advisor_rule_intent"]["fd_stream_id"] == SENDER

    asyncio.run(run())


def test_same_id_concurrency_and_late_exact_user_proof(tmp_path, monkeypatch):
    import comms

    monkeypatch.setattr(comms, "PROOF_FAST_WAIT_S", 0.04)

    async def run():
        async with rule_fixture(tmp_path) as (h, envelope), operator(h, envelope) as socket:
            assert (await admit(socket))["type"] == "send.result"
            reached, release = asyncio.Event(), asyncio.Event()

            async def paste_without_evidence(name, text, **_options):
                h.tmux.inputs.append((name, text))
                reached.set()
                await release.wait()
            monkeypatch.setattr(h.tmux, "paste", paste_without_evidence)

            async with seat(h, SEAT_A) as first_fd, seat(h, SEAT_A) as second_fd:
                first = asyncio.create_task(forward(first_fd))
                try:
                    await asyncio.wait_for(reached.wait(), 3)
                    repeated = asyncio.create_task(forward(second_fd))
                finally:
                    release.set()
                result, duplicate = await asyncio.gather(first, repeated)
                assert result["delivery_status"] == duplicate["delivery_status"] == "committed_pending_proof"
                assert duplicate["duplicate"] and len(h.tmux.inputs) == 1
                name, wire = h.tmux.inputs[0]
                await record_native(h, name, wire.rstrip())
                assert (await h.store.get_tell_delivery(TELL_ID))["reply"]["submission_confirmed"] is False
                await record_native(h, name, wire)
                accepted = await forward(first_fd)
                assert accepted["duplicate"] and accepted["delivery_status"] == "delivered"
                assert len(h.tmux.inputs) == 1

    asyncio.run(run())


@pytest.mark.parametrize("prepared", [False, True])
def test_dispatch_custody_survives_failure_before_or_after_paste_preparation(tmp_path, monkeypatch, prepared):
    async def run():
        async with rule_fixture(tmp_path) as (h, envelope), operator(h, envelope) as socket:
            assert (await admit(socket))["type"] == "send.result"
            name, method = ("paste", h.tmux.paste) if prepared else ("_resolve_route", h.comms._resolve_route)

            async def transport_refused(*_args, **_kwargs):
                raise VerbError("host_offline", "synthetic transport interruption")

            owner = h.tmux if prepared else h.comms
            monkeypatch.setattr(owner, name, transport_refused)
            async with seat(h, SEAT_A) as fd:
                assert (await forward(fd)).get("error_code") == "host_offline"
                retained = await h.store.get_tell_delivery(TELL_ID)
                assert ("delivery" in retained) is prepared
                monkeypatch.setattr(owner, name, method)
                result = await forward(fd)
                assert result["delivery_status"] == ("proof_unavailable" if prepared else "delivered")
                assert len(h.tmux.inputs) == (0 if prepared else 1)
                assert (await h.store.get_tell_delivery(TELL_ID))["advisor_rule_intent"] == retained["advisor_rule_intent"]

    asyncio.run(run())


def test_typed_transport_selects_verbatim_tmux_bytes(tmp_path):
    from pathlib import Path
    from tmux_transport import Tmux

    class RecordingTmux(Tmux):
        def __init__(self):
            super().__init__()
            self.commands = []
            self.contents = []

        async def run(self, *args, **kwargs):
            self.commands.append(args)
            if args[0] == "load-buffer":
                self.contents.append(Path(args[-1]).read_bytes())
            return 0, ""

    async def run():
        transport = RecordingTmux()
        await transport.paste("fixture", WORDS, preserve_newlines=True)
        await transport.paste("fixture", WORDS)
        pastes = [args for args in transport.commands if args[0] == "paste-buffer"]
        assert pastes[0][3:] == ("-t", "=fixture:", "-d", "-p", "-r")
        assert pastes[1][3:] == ("-t", "=fixture:", "-d", "-p")
        assert transport.contents == [WORDS.encode(), WORDS.encode()]

    asyncio.run(run())


def test_mapped_desk_preserves_display_dispatch_and_legacy_duplicate(tmp_path, monkeypatch):
    import desk_stamp
    from assistant_composite import direct_dispatch_envelope

    async def run():
        async with rule_fixture(tmp_path) as (h, envelope), operator(h, envelope) as socket:
            credential = operator_auth.decode_envelope(envelope)["credential_id"]
            monkeypatch.setattr(desk_stamp, "load", lambda: {credential: "fixture-desk"})
            label = "[from fixture-desk]\n"
            await h.server._on_send({"to_stream_id": COMPOSITE_CHAT, "text": WORDS, "msg_id": "legacy-desk",
                "_auth_context": {"operator_authenticated": True, "operator_principal": "operator:" + credential}})
            duplicate = await admit(socket, "legacy-desk")
            assert duplicate["assistant_composite"]["duplicate"]
            assert (await input_row(h, "legacy-desk"))["operator_origin"] is None

            fresh = await admit(socket)
            assert fresh["type"] == "send.result"
            row = await input_row(h, MESSAGE_ID)
            assert row["body"] == WORDS and row["operator_origin"] == ORIGIN
            events = await h.store.fetch_session_event_tail(COMPOSITE_CHAT, limit=8)
            new_event = next(event for event in events if event["message_id"] == MESSAGE_ID)
            assert new_event["text"] == label + WORDS
            receipt = await h.store.get_send_receipt(
                COMPOSITE_CHAT, "fixture-send-" + MESSAGE_ID, include_display_text=True)
            assert receipt["display_text"] == label + WORDS
            dispatch = direct_dispatch_envelope(h.composite.config, row, dispatch_id="fixture-direct",
                                               target=SEAT_A, generation=h.gen[SEAT_A])
            assert dispatch["original_input"]["text"] == label + WORDS
            async with seat(h, SEAT_A) as fd:
                assert (await forward(fd))["delivery_status"] == "delivered"
    asyncio.run(run())


@pytest.mark.parametrize("duplicate", [False, True])
def test_operator_connection_rejection_during_admission_leaves_no_route(tmp_path, monkeypatch, duplicate):
    async def run():
        async with rule_fixture(tmp_path) as (h, envelope), operator(h, envelope) as socket:
            if duplicate:
                credential = operator_auth.decode_envelope(envelope)["credential_id"]
                await h.composite.accept_input({"msg_id": MESSAGE_ID, "text": WORDS},
                    operator_principal="operator:" + credential)
            prior = await input_row(h, MESSAGE_ID)
            paused, release = asyncio.Event(), asyncio.Event()
            validate = h.server._validate_assistant_input_attachments

            async def attachment_barrier(*args, **kwargs):
                paused.set()
                await release.wait()
                return await validate(*args, **kwargs)

            monkeypatch.setattr(h.server, "_validate_assistant_input_attachments", attachment_barrier)
            await socket.send({"type": "send", "request_id": "fixture-paused-send",
                               "to_stream_id": COMPOSITE_CHAT, "text": WORDS, "optimistic_id": MESSAGE_ID})
            try:
                await asyncio.wait_for(paused.wait(), 3)
                auth = await socket.rpc("hello", client="pentacle", auth_v2={})
                assert auth.get("error_code") == "operator_auth_invalid"
            finally:
                release.set()
            result = await socket.recv()
            assert result.get("request_id") == "fixture-paused-send"
            assert result.get("error_code") == "authentication_required"
            after = await input_row(h, MESSAGE_ID)
            if prior is None:
                assert after is None
            else:
                for key in ("route_id", "event_id", "body", "actor_stream_id", "operator_origin"):
                    assert after[key] == prior[key]
            assert await h.store.get_send_receipt(COMPOSITE_CHAT, "fixture-paused-send") is None

    asyncio.run(run())
