"""Provider event identity, public history and reconnect behavior."""

from tests.contract.harness import event_stamp, unique
from tools.client_contract_probe import assert_client_consumable

FRAME_BUDGET = 900 * 1024
PREVIEW_MARKER = "[Event truncated for backfill; original retained by daemon.]"


def _wait_record(client, seat, start, record):
    return client.event(seat.stream_id, start=start,
                        match=lambda event: event_stamp(event) == (record["uuid"], 0),
                        description=f"provider record {record['uuid']}")


def _event_key(event):
    return event["stream_id"], event["daemon_seq"]


def _rendered(event):
    return _event_key(event), event_stamp(event), event["kind"], event["text"]


def _history_events(client, seat, *, limit, chunk_limit, min_events, **cursor):
    replies = client.history(seat.stream_id, limit=limit, chunk_limit=chunk_limit, **cursor)
    assert all(frame.byte_count <= FRAME_BUDGET for frame in replies), "C7 history frame exceeded 900 KiB"
    pages = [frame.body["events"] for frame in replies if frame.body["events"]]
    for page in pages:
        assert len(page) <= chunk_limit
        seqs = [event["daemon_seq"] for event in page]
        assert seqs == sorted(seqs), "C7 events within a page are not ascending"
    for newer, older in zip(pages, pages[1:]):
        assert older[-1]["daemon_seq"] < newer[0]["daemon_seq"], "C7 older page preceded a newer page"
    events = [event for page in reversed(pages) for event in page]
    problems = assert_client_consumable(events, min_events=min_events)
    assert not problems, ("C7 reconstructed history is not client consumable", problems)
    return replies, events


def test_c6_append_and_live_identity(chat):
    """C6: ingest.py:264-324; claude_jsonl_norm.py:74-82,491-495."""
    chat.start()
    client = chat.client()
    client.subscribe()
    seat = chat.spawn(client, unique("live"))
    user = seat.record("user", unique("turn"))
    text = unique("assistant")
    assistant = seat.record("assistant", text)
    same_text = seat.record("assistant", text)
    sentinel = seat.record("user", unique("after-same-text"))
    start = len(client.frames)
    seat.append(user, assistant, assistant, same_text, sentinel)
    # The sentinel lets the client observe duplicates immediately after the
    # final same-text record as well as those from the repeated provider input.
    _wait_record(client, seat, start, sentinel)
    events = client.events_since(seat.stream_id, start)
    assert [(event_stamp(event), event["kind"], event["text"]) for event in events] == [
        ((user["uuid"], 0), "USER", user["message"]["content"]),
        ((assistant["uuid"], 0), "ASSIST_TEXT", text),
        ((same_text["uuid"], 0), "ASSIST_TEXT", text),
        ((sentinel["uuid"], 0), "USER", sentinel["message"]["content"]),
    ], "C6 live event prefix through the sentinel differs"
    assert len({event["daemon_seq"] for event in events}) == len(events)
    assert [event["daemon_seq"] for event in events] == sorted(event["daemon_seq"] for event in events)


def test_c7_history_and_reconnect(chat):
    """C7: docs/chat_protocol.md history and reconnect; tools/client_contract_probe.py:539-579."""
    chat.start()
    client = chat.client()
    assert client.subscribe()["sessions"] == []
    seat = chat.spawn(client, unique("history"))
    records = [seat.record("user", unique("row")) for _ in range(7)]
    start = len(client.frames)
    seat.append(*records)
    _wait_record(client, seat, start, records[-1])
    live = client.events_since(seat.stream_id, start)
    assert [(event_stamp(event), event["kind"], event["text"]) for event in live] == [
        ((record["uuid"], 0), "USER", record["message"]["content"]) for record in records
    ]

    replies, history = _history_events(client, seat, limit=5, chunk_limit=2, min_events=5)
    assert len(history) == 5, "C7 history total exceeded the requested limit"
    assert [frame.body["type"] for frame in replies] == [
        *["request_stream_events.chunk"] * 3, "request_stream_events.ok",
    ]
    request_id = replies[-1].body["request_id"]
    assert replies[-1].body["complete"] is True and replies[-1].body["events"] == []
    assert client.call("ping")["type"] == "pong"
    assert sum(frame.body["type"] == "request_stream_events.ok" and
               frame.body.get("request_id") == request_id for frame in client.frames) == 1
    assert list(map(_rendered, history)) == list(map(_rendered, live[-5:]))

    _, older = _history_events(client, seat, limit=5, chunk_limit=2, min_events=2,
                               before_daemon_seq=history[0]["daemon_seq"])
    assert list(map(_rendered, older)) == list(map(_rendered, live[:2]))

    large_text = "é" * (FRAME_BUDGET // 2 + 1024)
    oversize = seat.record("assistant", large_text)
    start = len(client.frames)
    seat.append(oversize)
    full = _wait_record(client, seat, start, oversize)
    assert full["text"] == large_text
    _, page = _history_events(client, seat, limit=3, chunk_limit=1, min_events=3)
    preview = page[-1]
    assert _rendered(preview)[:3] == _rendered(full)[:3]
    prefix, marker, trailing = preview["text"].partition(f"\n\n{PREVIEW_MARKER}")
    assert marker and not trailing and prefix and large_text.startswith(prefix), (
        "C7 preview did not retain a nonempty prefix of the original text"
    )
    assert len(preview["text"]) < len(large_text)
    _, resumed = _history_events(client, seat, limit=2, chunk_limit=1, min_events=2,
                                 before_daemon_seq=preview["daemon_seq"])
    assert list(map(_rendered, resumed)) == list(map(_rendered, live[-2:]))
    original = chat.inspect(client, seat.stream_id, event_tail=1)["recent_events"]
    assert list(map(_rendered, original)) == [_rendered(full)]

    witness = chat.client()
    witness.subscribe()
    anchor = _event_key(full)
    client.close()
    offline = seat.record("assistant", unique("offline"))
    start = len(witness.frames)
    seat.append(offline)
    offline_event = _wait_record(witness, seat, start, offline)

    client = chat.client()
    client.subscribe()
    start = len(client.frames)
    witness_start = len(witness.frames)
    overlapping = seat.record("user", unique("overlap"))
    seat.append(overlapping)
    overlap_event = _wait_record(witness, seat, witness_start, overlapping)
    # The reconnecting client is subscribed and buffers the event while history
    # independently reads it and walks back to the anchor.
    _, recovered = _history_events(client, seat, limit=3, chunk_limit=1, min_events=3)
    assert _event_key(recovered[0]) == anchor
    assert list(map(_rendered, recovered[1:])) == list(map(_rendered, (offline_event, overlap_event)))
    buffered = client.events_since(seat.stream_id, start)
    assert list(map(_rendered, buffered)) == [_rendered(overlap_event)]
    merged = {_event_key(event): event for event in [*recovered, *buffered, full]}
    assert [merged[key]["text"] for key in sorted(merged)] == [
        large_text, offline["message"]["content"][0]["text"], overlapping["message"]["content"],
    ]
