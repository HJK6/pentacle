"""Delivery and receipt contracts through ordinary client send requests."""

from tests.contract.harness import event_stamp, expect_landed, read_records, unique


def test_c3_send_lands_once(chat):
    """C3: docs/chat_protocol.md independent client walkthrough; comms.py:2017-2068."""
    chat.start()
    client = chat.client()
    client.subscribe()
    seat = chat.spawn(client, unique("delivery"))
    request_id, text = unique("send"), unique("message")
    before = chat.inputs(seat)
    start = len(client.frames)
    reply = client.call("send", request_id=request_id, optimistic_id=unique("optimistic"),
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
