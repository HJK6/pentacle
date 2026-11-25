"""Delivery and receipt contracts through ordinary client send requests."""

import signal

import pytest
from websockets.exceptions import ConnectionClosed

from tests.contract.harness import (
    event_stamp, expect_landed, observed, read_records, unique,
)
from tests.soak.restart_matrix import LOCAL_HOST


def test_c3_send_lands_once(chat):
    """C3: docs/chat_protocol.md independent client walkthrough; comms.py:2017-2068."""
    chat.start()
    client = chat.client()
    client.subscribe()
    seat = chat.spawn(client, unique("delivery"))
    request_id, optimistic_id, text = unique("send"), unique("optimistic"), unique("message")
    before = chat.inputs(seat)
    start = len(client.frames)
    reply = client.call("send", request_id=request_id, optimistic_id=optimistic_id,
                        to_stream_id=seat.stream_id, text=text)

    expect_landed(reply, seat.stream_id, request_id)
    assert [item["text"] for item in chat.inputs(seat)[len(before):]] == [text], (
        "C3 send produced a different number of provider inputs"
    )
    records = read_records(seat.transcript)
    assert [record["message"]["content"] for record in records] == [text], records
    event = client.event(seat.stream_id, start=start, match=lambda event: event["kind"] == "USER",
                         description="C3 durable USER event")
    assert (event["text"], event_stamp(event)) == (text, (records[0]["uuid"], 0)), event
    assert (event["request_id"], event["optimistic_id"], event["receipt_id"]) == (
        request_id, optimistic_id, reply["receipt_id"],
    ), "C3 durable USER was not attributed to this send"


def _receipt(client, seat, request_id):
    reply = client.call("send.receipt.get", request_id=request_id, to_stream_id=seat.stream_id)
    assert reply["type"] == "send.receipt.get.ok", reply
    return reply["receipts"][0] if reply["found"] else None


def _wait_user(client, seat, start, text):
    return client.event(seat.stream_id, start=start,
                        match=lambda event: (event["kind"], event["text"]) == ("USER", text),
                        description=f"durable USER on {seat.stream_id}")


def _input_texts(chat, seat):
    return [record["text"] for record in chat.inputs(seat)]


def test_c4_overlapping_send_and_disconnected_initiator(chat):
    """C4: server.py:1851-1856,5609-5643; comms.py:2281-2347; store.py:6468-6541."""
    chat.start()
    initiating, retrying, observer = (chat.client() for _ in range(3))
    for client in (initiating, retrying, observer):
        client.subscribe()
    chat.hold_user()
    seat = chat.spawn(initiating, unique("overlap"))
    first_id, second_id, text = unique("send"), unique("send"), unique("same-body")
    payload = {"type": "send", "to_stream_id": seat.stream_id,
               "optimistic_id": unique("optimistic"), "text": text}
    initiating.send({**payload, "request_id": first_id})
    chat.wait_for_input(seat, text)
    claimed = _receipt(observer, seat, first_id)
    assert claimed["state"] == "accepted" and claimed["attempts"] == 0, claimed

    retry_start = len(retrying.frames)
    retrying.send({**payload, "request_id": second_id})
    rotated = observed(lambda: _receipt(observer, seat, second_id), bool,
                       description="overlapping request receipt", timeout=2)
    assert rotated["request_id"] == second_id
    assert rotated.get("reason") == f"coalesced_replay:{first_id}", (
        "C4 overlapping physical request did not coalesce", rotated,
    )
    duplicate = retrying.wait_reply(second_id, start=retry_start)
    assert duplicate["type"] == "send.result" and duplicate["coalesced"] is True, duplicate
    assert duplicate["to_stream_id"] == seat.stream_id
    assert duplicate["receipt_id"] == claimed["receipt_id"]
    assert _input_texts(chat, seat) == [text]
    initiating.close()

    start = len(observer.frames)
    chat.release_user()
    first = _wait_user(observer, seat, start, text)
    landed = observed(lambda: _receipt(observer, seat, first_id),
                      lambda receipt: receipt is not None and receipt["state"] == "landed",
                      description="landed receipt after initiating client disconnected", timeout=5)
    assert landed["request_id"] == first_id and landed["submission_confirmed"] is True, landed
    reconnected = chat.client()
    reconnected.subscribe()
    fields = {key: value for key, value in payload.items() if key != "type"}
    replay = reconnected.call("send", request_id=first_id, **fields)
    expect_landed(replay, seat.stream_id, first_id)
    assert replay["coalesced"] is True and replay["receipt_id"] == landed["receipt_id"], replay
    assert _input_texts(chat, seat) == [text]

    independent_id = unique("send")
    start = len(observer.frames)
    independent = reconnected.call("send", request_id=independent_id,
                                   **{**fields, "optimistic_id": unique("optimistic")})
    expect_landed(independent, seat.stream_id, independent_id)
    second = _wait_user(observer, seat, start, text)
    assert second["daemon_seq"] > first["daemon_seq"] and event_stamp(second) != event_stamp(first)
    assert _input_texts(chat, seat) == [text, text]
    assert sum(frame.body.get("request_id") == second_id for frame in retrying.frames[retry_start:]) == 1


def test_c5_honest_send_failure(chat):
    """C5: comms.py:1118-1161,2017-2077,2216-2253; docs/chat_protocol.md send receipt guidance."""
    chat.start()
    client = chat.client()
    client.subscribe()
    closed = chat.spawn(client, unique("closed"))
    assert chat.close_seat(client, closed)["type"] == "close.ok"
    chat.hold_user()
    held = chat.spawn(client, unique("withheld"))
    before = [chat.inputs(seat) for seat in (closed, held)]

    for target in (closed.stream_id, f"{LOCAL_HOST}:{unique('unknown')}"):
        error = client.call("send", to_stream_id=target, text=unique("refused"))
        assert (error["type"], error.get("error_code")) == ("send.error", "unknown_session"), (
            "C5 inactive destination did not report unknown_session", error,
        )
        assert [chat.inputs(seat) for seat in (closed, held)] == before

    request_id, text = unique("send"), unique("no-proof")
    start = len(client.frames)
    result = client.call("send", request_id=request_id, to_stream_id=held.stream_id, text=text, timeout=6)
    assert (result["type"], result["request_id"], result.get("to_stream_id")) == (
        "send.result", request_id, held.stream_id,
    ), result
    assert (result.get("delivery"), result.get("submission_confirmed"),
            result.get("do_not_resubmit")) == ("committed_pending_proof", False, True), (
        "C5 send was not honestly marked unconfirmed", result,
    )
    assert _input_texts(chat, held) == [text]
    pane = chat.namespace.run("capture-pane", "-p", "-t", f"={held.name}:")
    assert pane.returncode == 0 and "❯" in pane.stdout and text not in pane.stdout
    assert not any(event["text"] == text for event in client.events_since(held.stream_id, start))
    chat.release_user()
    _wait_user(client, held, start, text)
    assert _input_texts(chat, held) == [text]


def test_c9_send_receipts_across_restart(chat):
    """C9: docs/restart-continuity.md send cancellation; comms.py:2179-2181,2324-2347."""
    chat.start()
    client = chat.client()
    client.subscribe()
    landed = chat.spawn(client, unique("acknowledged"))
    old_id, text = unique("send"), unique("landed")
    payload = {"to_stream_id": landed.stream_id, "text": text, "optimistic_id": unique("optimistic")}
    acknowledged = client.call("send", request_id=old_id, **payload)
    expect_landed(acknowledged, landed.stream_id, old_id)
    assert _input_texts(chat, landed) == [text]
    client.close()
    assert chat.daemon.stop(signal.SIGTERM)["rc"] == 0
    chat.start()
    client = chat.client()
    client.subscribe()
    replay = client.call("send", request_id=old_id, **payload)
    expect_landed(replay, landed.stream_id, old_id)
    assert replay["coalesced"] is True and replay["receipt_id"] == acknowledged["receipt_id"], (
        "C9 acknowledged receipt was not preserved", replay,
    )
    assert _input_texts(chat, landed) == [text]

    chat.hold_user()
    held = chat.spawn(client, unique("interrupted"))
    held_id, text = unique("send"), unique("interrupted")
    payload = {"to_stream_id": held.stream_id, "text": text, "optimistic_id": unique("optimistic")}
    start = len(client.frames)
    client.send({"type": "send", "request_id": held_id, **payload})
    assert [record["text"] for record in chat.wait_for_input(held, text)] == [text]
    assert chat.daemon.stop(signal.SIGKILL)["rc"] == -signal.SIGKILL
    with pytest.raises(ConnectionClosed):
        client.wait_reply(held_id, start=start, timeout=2)
    chat.start()
    client = chat.client()
    client.subscribe()

    start = len(client.frames)
    replay = client.call("send", request_id=held_id, **payload)
    assert replay["type"] == "send.result" and replay["to_stream_id"] == held.stream_id, replay
    assert replay["delivery"] in {"accepted", "committed_pending_proof"}, replay
    assert replay["submission_confirmed"] is False and replay["do_not_resubmit"] is True, replay
    assert _input_texts(chat, held) == [text]
    chat.release_user()
    _wait_user(client, held, start, text)
    assert _input_texts(chat, held) == [text]
