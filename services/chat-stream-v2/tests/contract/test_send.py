"""Delivery and receipt contracts through ordinary client send requests."""

import json
import os
import signal

import pytest
from websockets.exceptions import ConnectionClosed

from tests.contract.harness import (
    event_stamp, expect_landed, observed, read_records, unique,
)
from tests.soak.restart_matrix import LOCAL_HOST, SERVICE_DIR


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

    assert reply.get("submission_confirmed") is True, ("C3 send reply lost confirmation", reply)
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


def test_send_records_reply_in_daemon_latency_window(chat):
    # Close the diagnostic window on the first send in this disposable process.
    # There is no public knob for the reporting interval.
    (chat.daemon.root / "sitecustomize.py").write_text(
        "from importlib.util import find_spec\n"
        "if find_spec('send_timing') is not None:\n"
        "    import send_timing\n"
        "    send_timing.SUMMARY_INTERVAL_S = 0\n",
        encoding="utf-8",
    )
    chat.daemon.extra_env["PYTHONPATH"] = os.pathsep.join(map(str, (chat.daemon.root, SERVICE_DIR)))
    chat.start()
    client = chat.client()
    client.subscribe()
    seat = chat.spawn(client, unique("timed"))
    request_id = unique("send")
    reply = client.call("send", request_id=request_id, to_stream_id=seat.stream_id,
                        text=unique("message"))
    expect_landed(reply, seat.stream_id, request_id)

    windows = [json.loads(line.split("send_latency ", 1)[1])
               for line in chat.daemon.logpath.read_text().splitlines()
               if " send_latency " in line]
    assert len(windows) == 1, windows
    assert windows[0]["attempts"] == 1
    assert windows[0]["reply_p95_ms"] is not None


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


def test_busy_lane_send_lands_once(chat):
    (chat.control / "mode").write_text("busy_user", encoding="utf-8")
    chat.start()
    client = chat.client()
    client.subscribe()
    seat = chat.spawn(client, unique("busy"))
    token = chat.token(seat)
    request_id, optimistic_id, text = unique("send"), unique("optimistic"), unique("message")
    start = len(client.frames)
    client.send({"type": "send", "request_id": request_id, "optimistic_id": optimistic_id,
                 "to_stream_id": seat.stream_id, "text": text})
    lane_read = client.call("work_lanes.list", from_stream_id=seat.stream_id, stream_token=token)
    assert lane_read["type"] == "work_lanes.list.ok", lane_read
    assert not any(frame.body.get("request_id") == request_id for frame in client.frames[start:])
    reply = client.wait_reply(request_id, start=start)
    expect_landed(reply, seat.stream_id, request_id)
    assert reply.get("provider_queued") is True, reply

    event = _wait_user(client, seat, start, text)
    assert (event["request_id"], event["optimistic_id"], event["receipt_id"]) == (
        request_id, optimistic_id, reply["receipt_id"],
    )

    def user_history():
        return [item["daemon_seq"] for frame in client.history(seat.stream_id, limit=20, chunk_limit=20)
                for item in frame.body["events"]
                if item["kind"] == "USER" and item["text"] == text]

    assert user_history() == [event["daemon_seq"]]
    destination = client.call("send.receipt.get", request_id=request_id, to_stream_id=seat.stream_id)
    assert destination["type"] == "send.receipt.get.ok" and destination["found"], destination
    receipt, = destination["receipts"]
    assert (receipt["to_stream_id"], receipt["request_id"], receipt["receipt_id"]) == (
        seat.stream_id, request_id, reply["receipt_id"],
    )
    assert (receipt["state"], receipt["submission_confirmed"], receipt["attempts"]) == ("landed", True, 1)

    chat.release_user()
    client.event(seat.stream_id, start=start,
                 match=lambda item: (item["kind"], item["text"]) == ("ASSIST_TEXT", "Held turn finished"),
                 description="the busy turn finished")
    assert user_history() == [event["daemon_seq"]]
    assert _receipt(client, seat, request_id) == receipt
    assert _input_texts(chat, seat) == [text]
    assert [frame.body for frame in client.frames[start:] if frame.body["type"] == "send.result"
            and frame.body.get("request_id") == request_id] == [reply]


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
    assert retrying.call("ping")["type"] == "pong"
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
    assert replay.get("delivery") == "landed", ("C9 acknowledged replay lost its landed state", replay)
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
