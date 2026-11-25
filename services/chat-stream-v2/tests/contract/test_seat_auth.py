"""Seat tokens and authenticated peer delivery on the public socket."""

from tests.contract.harness import unique


def test_c10_retired_seat_is_fenced(chat):
    """C10: docs/chat_protocol.md seat tokens; server.py:4702-4730; store.py:3020-3034."""
    chat.start()
    client = chat.client()
    client.subscribe()
    seat = chat.spawn(client, unique("reopen"))
    original = chat.token(seat)

    def compact(token, request_id):
        return client.call("compact", request_id=request_id, stream_id=seat.stream_id,
                           from_stream_id=seat.stream_id, stream_token=token)

    original_id = unique("compact")
    first = compact(original, original_id)
    assert first["type"] == "compact.ok" and first["submission_confirmed"] is True, first
    initial = chat.inputs(seat)
    assert len(initial) == 1 and initial[0]["text"].startswith("/compact ")
    assert chat.close_seat(client, seat)["type"] == "close.ok"
    closed = chat.inspect(client, seat.stream_id, event_tail=0)["session"]
    refusal = compact(original, original_id)
    assert (refusal["type"], refusal.get("error_code")) == ("compact.error", "compact_self_only"), (
        "C10 a retired token could still compact", refusal,
    )
    assert chat.inputs(seat) == initial
    assert chat.inspect(client, seat.stream_id, event_tail=0)["session"]["status"] == closed["status"] == "closed"

    replacement = chat.spawn(client, seat.name)
    assert replacement.generation != seat.generation
    new_token = chat.token(replacement)
    assert new_token != original, "C10 replacement reused a retired credential"
    panes = chat.panes(replacement)
    refusal = compact(original, original_id)
    assert (refusal["type"], refusal.get("error_code")) == ("compact.error", "compact_self_only"), refusal
    row = chat.inspect(client, replacement.stream_id, event_tail=0)["session"]
    assert (row["status"], row["session_generation"]) == ("open", replacement.generation)
    assert chat.panes(replacement) == panes and chat.inputs(replacement) == initial
    current = compact(new_token, unique("compact"))
    assert current["type"] == "compact.ok" and current["submission_confirmed"] is True, current
    assert [item["text"] for item in chat.inputs(replacement)] == [initial[0]["text"]] * 2


def test_c11_authenticated_tell_is_deduplicated(chat):
    """C11: services/agent-orch/README.md tell contract; comms.py:556-619; claude_jsonl_norm.py:369-384."""
    chat.start()
    client = chat.client()
    client.subscribe()
    sender = chat.spawn(client, unique("sender"))
    recipient = chat.spawn(client, unique("recipient"))
    tell_id, text = unique("tell"), unique("peer-turn")
    fields = {"tell_id": tell_id, "text": text, "from_stream_id": sender.stream_id,
              "stream_token": chat.token(sender), "to_stream_id": recipient.stream_id}
    start = len(client.frames)
    sent = client.call("tell", **fields)
    assert (sent["type"], sent["tell_id"], sent["to_stream_id"]) == (
        "tell.ok", tell_id, recipient.stream_id,
    ), sent
    event = client.event(recipient.stream_id, start=start, match=lambda event: event["kind"] == "TELL",
                         description="authenticated TELL")
    assert (event["raw"]["sender"], event["raw"]["tell_id"], event["raw"]["peer_payload"],
            event["text"]) == (sender.stream_id, tell_id, text, text), event
    inputs = chat.inputs(recipient)
    assert len(inputs) == 1 and tell_id in inputs[0]["text"] and inputs[0]["text"].endswith("\n" + text)
    duplicate = client.call("tell", **fields)
    assert duplicate["type"] == "tell.ok" and duplicate.get("duplicate") is True, (
        "C11 identical tell was not recognized as a duplicate", duplicate,
    )
    assert duplicate["tell_id"] == tell_id and duplicate["to_stream_id"] == recipient.stream_id
    conflict = client.call("tell", **{**fields, "text": unique("changed")})
    assert (conflict["type"], conflict.get("error_code")) == ("tell.error", "tell_id_conflict"), conflict
    assert chat.inputs(recipient) == inputs
    history = [event for frame in client.history(recipient.stream_id, limit=8, chunk_limit=8)
               for event in frame.body["events"] if event["kind"] == "TELL"]
    assert [(event["daemon_seq"], event["raw"]["tell_id"]) for event in history] == [
        (event["daemon_seq"], tell_id),
    ]
    assert [event["daemon_seq"] for event in client.events_since(recipient.stream_id, start)
            if event["kind"] == "TELL"] == [event["daemon_seq"]]
