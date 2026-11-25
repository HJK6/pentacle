"""Session lifecycle and restart continuity through public readbacks."""

import signal

from tests.contract.harness import event_stamp, unique


def _conversation(client, stream_id):
    return [event for frame in client.history(stream_id, limit=16, chunk_limit=16)
            for event in frame.body["events"] if event["kind"] == "USER"]


def _identity(event):
    return event_stamp(event), event["daemon_seq"], event["text"], event["session_id"]


def test_c8_restart_continuity(chat):
    """C8: README startup contract, main.py:975,1020-1047, docs/restart-continuity.md."""
    chat.start()
    client = chat.client()
    client.subscribe()
    seat = chat.spawn(client, unique("survivor"))
    retired = chat.spawn(client, unique("retired"))
    assert chat.close_seat(client, retired)["type"] == "close.ok"
    assert retired.name not in chat.namespace.session_names()
    panes = chat.panes(seat)
    assert len(panes) == 1

    records = [seat.record("user", unique("before")) for _ in range(2)]
    start = len(client.frames)
    seat.append(*records)
    client.event(seat.stream_id, start=start,
                 match=lambda event: event_stamp(event) == (records[-1]["uuid"], 0),
                 description="initial ingest")
    previous = _conversation(client, seat.stream_id)
    assert [event_stamp(event) for event in previous] == [(r["uuid"], 0) for r in records]
    assert [event["text"] for event in previous] == [r["message"]["content"] for r in records]

    for sig, rc in ((signal.SIGTERM, 0), (signal.SIGKILL, -signal.SIGKILL)):
        pid = chat.daemon.proc.pid
        client.close()
        stopped = chat.daemon.stop(sig)
        assert stopped["rc"] == rc and not stopped["escalated_to_sigkill"], stopped
        chat.start()
        assert chat.daemon.proc.pid != pid
        client = chat.client()
        snapshot = client.subscribe()
        restored = {row["stream_id"]: row for row in snapshot["sessions"]}
        assert set(restored) == {seat.stream_id}, (
            "C8 first snapshot did not restore exactly the surviving fixture seat", list(restored),
        )
        assert restored[seat.stream_id]["session_generation"] == seat.generation
        assert chat.panes(seat) == panes, "C8 restarted daemon replaced the original pane"
        closed = chat.inspect(client, retired.stream_id, event_tail=0)["session"]
        assert (closed["status"], closed["session_generation"]) == ("closed", retired.generation)
        assert retired.name not in chat.namespace.session_names()

        sentinel = seat.record("user", unique("after"))
        start = len(client.frames)
        seat.append(sentinel)
        new = client.event(seat.stream_id, start=start,
                           match=lambda event: event_stamp(event) == (sentinel["uuid"], 0),
                           description=f"full ingest after {sig.name}")
        assert new["daemon_seq"] > previous[-1]["daemon_seq"], new
        history = _conversation(client, seat.stream_id)
        assert [_identity(event) for event in history] == [
            *map(_identity, previous), _identity(new),
        ], "C8 history changed across the daemon restart"
        assert chat.inspect(client, seat.stream_id, event_tail=0)["session"][
            "session_generation"
        ] == seat.generation
        previous = history


def _list_ids(client):
    reply = client.call("list_sessions")
    assert reply["type"] == "list_sessions.ok", reply
    return {row["stream_id"] for row in reply["active"]}


def _wait_inventory(client, stream_id, *, start, present):
    return client.until(
        lambda body: body["type"] == "session.inventory"
        and (stream_id in {row["stream_id"] for row in body["sessions"]}) == present,
        start=start, description=f"inventory with {stream_id} present={present}",
    )


def _pane_census(namespace):
    reply = namespace.run("list-panes", "-a", "-F", "#{session_name} #{pane_id} #{pane_pid}")
    assert reply.returncode == 0, reply.stderr
    return set(reply.stdout.splitlines())


def test_c2_session_lifecycle(chat):
    """C2: docs/chat_protocol.md inventory; spawnctl.py:2062-2089; sessions.py:313-353."""
    chat.start()
    a, b = chat.client(), chat.client()
    for client in (a, b):
        assert client.subscribe()["sessions"] == []
    baseline = _pane_census(chat.namespace)
    assert {pane.split()[0] for pane in baseline} == {chat.namespace.keepalive}
    assert len(baseline) == 1
    opened_starts = [len(client.frames) for client in (a, b)]
    name, key = unique("lifecycle"), unique("spawn-key")
    seat = chat.spawn(a, name, key=key)
    for client, start in zip((a, b), opened_starts):
        _wait_inventory(client, seat.stream_id, start=start, present=True)
        assert seat.stream_id in _list_ids(client)
    spawned = _pane_census(chat.namespace)
    new_panes = spawned - baseline
    assert baseline <= spawned and len(new_panes) == 1 and next(iter(new_panes)).split()[0] == name

    replay = chat.spawn(a, name, key=key)
    assert replay == seat
    assert _pane_census(chat.namespace) == spawned, "C2 spawn replay changed the whole-server pane census"

    closed_starts = [len(client.frames) for client in (a, b)]
    assert chat.close_seat(a, seat)["type"] == "close.ok"
    for client, start in zip((a, b), closed_starts):
        _wait_inventory(client, seat.stream_id, start=start, present=False)
        assert seat.stream_id not in _list_ids(client)
    closed = chat.inspect(a, seat.stream_id, event_tail=0)["session"]
    assert (closed["status"], closed["session_generation"]) == ("closed", seat.generation)
    assert _pane_census(chat.namespace) == baseline, "C2 close did not restore the keepalive pane census"

    parent = chat.spawn(a, unique("parent"))
    child = chat.spawn(a, unique("child"), parent_stream_id=parent.stream_id)
    assert chat.inspect(a, child.stream_id, event_tail=0)["session"]["parent_stream_id"] == parent.stream_id
    before = _pane_census(chat.namespace)
    child_panes = before - baseline
    assert baseline <= before and len(child_panes) == 2
    assert {pane.split()[0] for pane in child_panes} == {parent.name, child.name}
    refusal = chat.close_seat(a, parent)
    assert (refusal["type"], refusal.get("error_code")) == ("close.error", "close_live_children"), (
        "C2 open child did not fence parent closure", refusal,
    )
    for client in (a, b):
        assert {parent.stream_id, child.stream_id} <= _list_ids(client)
    assert _pane_census(chat.namespace) == before
