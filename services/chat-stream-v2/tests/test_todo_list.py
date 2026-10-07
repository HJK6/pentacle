"""Personal to-do list: store, verbs, actor matrix and inventory broadcast."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from typing import Any

import pytest

from _shared import operator_auth
from server import Server
from sessions import Sessions, VerbError
from store import Store
from todo_list import TodoList


CREDENTIAL_ID = "00000000-0000-4000-8000-000000000002"
SEAT = "hosta:seat"
NEXUS = "hosta:nexus"
SERVICE = "hosta:service"
ITEM_KEYS = {"item_id", "text", "priority", "state", "position", "created_at", "updated_at"}


class FakeSessions:
    def __init__(self) -> None:
        self.rows = {
            SEAT: {"stream_id": SEAT, "status": "open", "role": "worker"},
            NEXUS: {"stream_id": NEXUS, "status": "open", "role": "nexus"},
            "hosta:closed": {"stream_id": "hosta:closed", "status": "closed", "role": "worker"},
        }

    def get(self, stream_id: str):
        return self.rows.get(stream_id)

    split = staticmethod(Sessions.split)


def operator_msg(verb: str, **values: Any) -> dict[str, Any]:
    return {
        "type": verb, "request_id": str(uuid.uuid4()),
        "_auth_context": {"operator_authenticated": True, "operator_principal": "operator:test"},
        **values,
    }


def seat_msg(verb: str, actor: str = SEAT, **values: Any) -> dict[str, Any]:
    return {
        "type": verb, "request_id": str(uuid.uuid4()), "from_stream_id": actor,
        "_auth_context": {"token_verified": True, "stream_id": actor}, **values,
    }


def service_msg(verb: str, **values: Any) -> dict[str, Any]:
    return {
        "type": verb, "request_id": str(uuid.uuid4()), "from_stream_id": SERVICE,
        "_auth_context": {"service_authenticated": True, "service_actor": SERVICE}, **values,
    }


def anonymous_msg(verb: str, **values: Any) -> dict[str, Any]:
    return {"type": verb, "request_id": str(uuid.uuid4()), "_auth_context": {}, **values}


@pytest.fixture
def todo(tmp_path):
    store = Store(str(tmp_path / "todo.db"))
    store.start()
    frames: list[dict[str, Any]] = []

    async def broadcast(frame: dict[str, Any]) -> None:
        frames.append(frame)

    surface = TodoList(store, FakeSessions(), broadcast=broadcast)
    surface.frames = frames  # type: ignore[attr-defined]
    surface.raw_store = store  # type: ignore[attr-defined]
    yield surface
    store.stop()


def call(surface: TodoList, verb: str, msg: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(surface.wire_handlers()[verb](msg))


def error_code(surface: TodoList, verb: str, msg: dict[str, Any]) -> str:
    with pytest.raises(VerbError) as caught:
        call(surface, verb, msg)
    return caught.value.code


def add(surface: TodoList, text: str, **values: Any) -> dict[str, Any]:
    reply = call(surface, "todo.add", operator_msg("todo.add", text=text, **values))
    assert reply["type"] == "todo.add.ok"
    return reply["item"]


def listing(surface: TodoList, **values: Any) -> list[dict[str, Any]]:
    reply = call(surface, "todo.list", operator_msg("todo.list", **values))
    assert reply["type"] == "todo.list.ok"
    return reply["items"]


def test_wire_handlers_expose_exactly_the_five_todo_verbs(todo) -> None:
    assert set(todo.wire_handlers()) == {
        "todo.list", "todo.add", "todo.set", "todo.check", "todo.remove",
    }


def test_store_table_matches_the_locked_ddl(todo) -> None:
    def op(conn: sqlite3.Connection):
        columns = {r[1]: (r[2], r[3], r[4]) for r in conn.execute("PRAGMA table_info(v2_todo_items)")}
        pk = [r[1] for r in conn.execute("PRAGMA table_info(v2_todo_items)") if r[5]]
        return columns, pk

    columns, pk = asyncio.run(todo.raw_store.submit(op))
    assert pk == ["item_id"]
    assert set(columns) == ITEM_KEYS  # no created_by or attribution column
    assert columns["priority"][1:] == (1, "'normal'")
    assert columns["state"][1:] == (1, "'open'")

    def bad(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO v2_todo_items VALUES ('x','t','urgent','open',1,'a','a')"
        )

    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(todo.raw_store.submit(bad))


def test_add_returns_item_with_deterministic_defaults(todo) -> None:
    item = add(todo, "  Buy milk  ")
    assert set(item) == ITEM_KEYS
    assert str(uuid.UUID(item["item_id"])) == item["item_id"]
    assert item["text"] == "Buy milk"
    assert item["priority"] == "normal"
    assert item["state"] == "open"
    assert item["position"] == 1
    assert item["created_at"] == item["updated_at"]
    assert item["created_at"].endswith("Z")


def test_positions_are_assigned_one_to_n_and_never_tie(todo) -> None:
    items = [add(todo, f"item {n}") for n in range(4)]
    assert [i["position"] for i in items] == [1, 2, 3, 4]
    call(todo, "todo.remove", operator_msg("todo.remove", item_id=items[1]["item_id"]))
    assert add(todo, "after remove")["position"] == 5


def test_list_orders_high_normal_low_then_position(todo) -> None:
    add(todo, "n1")
    add(todo, "l1", priority="low")
    add(todo, "h1", priority="high")
    add(todo, "n2")
    add(todo, "h2", priority="high")
    assert [i["text"] for i in listing(todo)] == ["h1", "h2", "n1", "n2", "l1"]
    assert all(set(i) == ITEM_KEYS for i in listing(todo))


def test_default_listing_is_open_only_and_include_done_splits_it(todo) -> None:
    keep = add(todo, "keep", priority="high")
    done = add(todo, "finished")
    call(todo, "todo.check", operator_msg("todo.check", item_id=done["item_id"]))

    default = listing(todo)
    assert [i["item_id"] for i in default] == [keep["item_id"]]
    assert all(i["state"] == "open" for i in default)

    with_done = listing(todo, include_done=True)
    assert [(i["text"], i["state"]) for i in with_done] == [("keep", "open"), ("finished", "done")]
    assert listing(todo, include_done=False) == default


def test_set_changes_priority_only_and_reorders(todo) -> None:
    first = add(todo, "first")
    second = add(todo, "second")
    reply = call(todo, "todo.set", operator_msg("todo.set", item_id=second["item_id"], priority="high"))
    assert reply["type"] == "todo.set.ok"
    assert reply["item"]["priority"] == "high"
    assert reply["item"]["text"] == "second"
    assert reply["item"]["position"] == second["position"]
    assert reply["item"]["updated_at"] >= second["updated_at"]
    assert [i["item_id"] for i in listing(todo)] == [second["item_id"], first["item_id"]]


def test_set_ignores_text_editing(todo) -> None:
    item = add(todo, "original")
    reply = call(todo, "todo.set", operator_msg(
        "todo.set", item_id=item["item_id"], priority="low", text="rewritten"))
    assert reply["item"]["text"] == "original"
    assert listing(todo)[0]["text"] == "original"


def test_check_marks_done_bumps_updated_at_and_is_idempotent(todo) -> None:
    item = add(todo, "ship it")
    first = call(todo, "todo.check", operator_msg("todo.check", item_id=item["item_id"]))
    assert first["type"] == "todo.check.ok"
    assert first["item"]["state"] == "done"
    assert first["item"]["updated_at"] > item["updated_at"]
    again = call(todo, "todo.check", operator_msg("todo.check", item_id=item["item_id"]))
    assert again["item"] == first["item"]


def test_remove_deletes_open_and_done_rows(todo) -> None:
    open_item = add(todo, "open one")
    done_item = add(todo, "done one")
    call(todo, "todo.check", operator_msg("todo.check", item_id=done_item["item_id"]))
    for item in (open_item, done_item):
        reply = call(todo, "todo.remove", operator_msg("todo.remove", item_id=item["item_id"]))
        assert reply == {"type": "todo.remove.ok", "item_id": item["item_id"]}
    assert listing(todo, include_done=True) == []


@pytest.mark.parametrize("text", ["", "   ", None, 5, "x" * 201, " " + "x" * 201 + " "])
def test_add_rejects_invalid_text(todo, text) -> None:
    assert error_code(todo, "todo.add", operator_msg("todo.add", text=text)) == "invalid_text"


def test_add_text_length_bounds_are_one_to_two_hundred(todo) -> None:
    assert add(todo, "x")["text"] == "x"
    assert len(add(todo, " " + "y" * 200 + " ")["text"]) == 200


def test_add_missing_text_is_invalid_text(todo) -> None:
    assert error_code(todo, "todo.add", operator_msg("todo.add")) == "invalid_text"


@pytest.mark.parametrize("priority", ["urgent", "HIGH", "", 1, ["high"]])
def test_add_and_set_reject_invalid_priority(todo, priority) -> None:
    assert error_code(todo, "todo.add", operator_msg("todo.add", text="a", priority=priority)) == "invalid_priority"
    item = add(todo, "target")
    assert error_code(todo, "todo.set", operator_msg(
        "todo.set", item_id=item["item_id"], priority=priority)) == "invalid_priority"


def test_set_without_priority_is_invalid_update(todo) -> None:
    item = add(todo, "target")
    assert error_code(todo, "todo.set", operator_msg("todo.set", item_id=item["item_id"])) == "invalid_update"
    assert error_code(todo, "todo.set", operator_msg(
        "todo.set", item_id=item["item_id"], text="only text")) == "invalid_update"


@pytest.mark.parametrize("verb", ["todo.set", "todo.check", "todo.remove"])
@pytest.mark.parametrize("item_id", ["no-such-item", "", None])
def test_unknown_item_id_is_not_found(todo, verb, item_id) -> None:
    values = {"priority": "low"} if verb == "todo.set" else {}
    assert error_code(todo, verb, operator_msg(verb, item_id=item_id, **values)) == "not_found"


def test_duplicate_open_text_is_rejected_trimmed_and_casefolded(todo) -> None:
    add(todo, "Buy Milk")
    for text in ("Buy Milk", "  buy milk ", "BUY MILK", "buy MILK"):
        assert error_code(todo, "todo.add", operator_msg("todo.add", text=text)) == "duplicate"
    assert len(listing(todo)) == 1


def test_done_item_does_not_block_re_adding_the_same_text(todo) -> None:
    item = add(todo, "recurring chore")
    call(todo, "todo.check", operator_msg("todo.check", item_id=item["item_id"]))
    again = add(todo, "Recurring Chore")
    assert again["item_id"] != item["item_id"]
    assert again["state"] == "open"


@pytest.mark.parametrize("make", [
    lambda verb, **v: operator_msg(verb, **v),
    lambda verb, **v: seat_msg(verb, SEAT, **v),
    lambda verb, **v: seat_msg(verb, NEXUS, **v),
])
def test_operator_seat_and_nexus_may_read_and_mutate(todo, make) -> None:
    created = call(todo, "todo.add", make("todo.add", text=f"by {uuid.uuid4().hex[:6]}"))["item"]
    item_id = created["item_id"]
    assert call(todo, "todo.list", make("todo.list"))["type"] == "todo.list.ok"
    assert call(todo, "todo.set", make("todo.set", item_id=item_id, priority="high"))["item"]["priority"] == "high"
    assert call(todo, "todo.check", make("todo.check", item_id=item_id))["item"]["state"] == "done"
    assert call(todo, "todo.remove", make("todo.remove", item_id=item_id))["type"] == "todo.remove.ok"


@pytest.mark.parametrize("verb,values", [
    ("todo.list", {}),
    ("todo.add", {"text": "nope"}),
    ("todo.set", {"item_id": "x", "priority": "high"}),
    ("todo.check", {"item_id": "x"}),
    ("todo.remove", {"item_id": "x"}),
])
@pytest.mark.parametrize("make", [
    service_msg,
    anonymous_msg,
    lambda verb, **v: {**anonymous_msg(verb, **v), "from_stream_id": SEAT},
    lambda verb, **v: {**seat_msg(verb, **v), "_auth_context": {"stream_id": SEAT}},
    lambda verb, **v: {**seat_msg(verb, SEAT, **v), "from_stream_id": NEXUS},
    lambda verb, **v: seat_msg(verb, "hosta:closed", **v),
    lambda verb, **v: seat_msg(verb, "hosta:ghost", **v),
], ids=["service", "anonymous", "claim-only", "unverified-token", "mismatched-owner", "closed-seat", "unknown-seat"])
def test_other_actors_get_unauthorized_and_cause_no_mutation_or_broadcast(todo, make, verb, values) -> None:
    assert error_code(todo, verb, make(verb, **values)) == "unauthorized"
    assert todo.frames == []
    assert listing(todo, include_done=True) == []


@pytest.mark.parametrize("make", [service_msg, anonymous_msg])
def test_unauthorized_actor_cannot_alter_an_existing_item(todo, make) -> None:
    item = add(todo, "protected")
    todo.frames.clear()
    for verb, values in (
        ("todo.set", {"item_id": item["item_id"], "priority": "low"}),
        ("todo.check", {"item_id": item["item_id"]}),
        ("todo.remove", {"item_id": item["item_id"]}),
    ):
        assert error_code(todo, verb, make(verb, **values)) == "unauthorized"
    assert listing(todo) == [item]
    assert todo.frames == []


def test_actor_is_checked_before_input_validation(todo) -> None:
    assert error_code(todo, "todo.add", anonymous_msg("todo.add", text="")) == "unauthorized"


def test_every_mutation_broadcasts_open_inventory_in_sort_order(todo) -> None:
    low = add(todo, "low one", priority="low")
    assert [f["type"] for f in todo.frames] == ["todo.inventory"]
    high = add(todo, "high one", priority="high")
    call(todo, "todo.set", operator_msg("todo.set", item_id=low["item_id"], priority="normal"))
    call(todo, "todo.check", operator_msg("todo.check", item_id=high["item_id"]))
    call(todo, "todo.remove", operator_msg("todo.remove", item_id=low["item_id"]))
    assert [f["type"] for f in todo.frames] == ["todo.inventory"] * 5
    assert [[i["text"] for i in f["items"]] for f in todo.frames] == [
        ["low one"],
        ["high one", "low one"],
        ["high one", "low one"],
        ["low one"],
        [],
    ]
    assert all(set(i) == ITEM_KEYS for f in todo.frames for i in f["items"])


def test_reads_and_failed_mutations_do_not_broadcast(todo) -> None:
    item = add(todo, "stable")
    todo.frames.clear()
    listing(todo)
    error_code(todo, "todo.add", operator_msg("todo.add", text="stable"))
    error_code(todo, "todo.set", operator_msg("todo.set", item_id=item["item_id"], priority="bad"))
    error_code(todo, "todo.remove", operator_msg("todo.remove", item_id="missing"))
    assert todo.frames == []


def test_broadcast_failure_does_not_fail_the_committed_mutation(tmp_path) -> None:
    store = Store(str(tmp_path / "todo-broadcast.db"))
    store.start()

    async def broken(_frame: dict[str, Any]) -> None:
        raise RuntimeError("socket gone")

    surface = TodoList(store, FakeSessions(), broadcast=broken)
    try:
        assert add(surface, "committed")["text"] == "committed"
        assert [i["text"] for i in listing(surface)] == ["committed"]
    finally:
        store.stop()


def test_unhealthy_store_refuses_with_store_unavailable(tmp_path) -> None:
    store = Store(str(tmp_path / "never-started.db"))
    surface = TodoList(store, FakeSessions())
    assert error_code(surface, "todo.list", operator_msg("todo.list")) == "store_unavailable"


def test_rows_survive_a_store_restart(tmp_path) -> None:
    path = str(tmp_path / "durable.db")
    first = Store(path)
    first.start()
    surface = TodoList(first, FakeSessions())
    item = add(surface, "durable", priority="high")
    first.stop()
    second = Store(path)
    second.start()
    try:
        assert listing(TodoList(second, FakeSessions())) == [item]
    finally:
        second.stop()


# -- server wiring and broadcast gate -------------------------------------


def test_server_registers_the_todo_handlers() -> None:
    server = Server()
    for verb in ("todo.list", "todo.add", "todo.set", "todo.check", "todo.remove"):
        assert verb in server.handlers


def _trust(client_kind: str = "pentacle") -> operator_auth.ConnectionTrust:
    return operator_auth.ConnectionTrust(
        transport="v2", credential_id=CREDENTIAL_ID, client_kind=client_kind, operator_trusted=True,
    )


def test_todo_inventory_reaches_only_operator_authenticated_clients() -> None:
    async def go() -> None:
        server = Server()
        pre_hello, token_cli, peer, trusted, loopback = (object() for _ in range(5))
        server._clients = [pre_hello, token_cli, peer, trusted, loopback]  # type: ignore[assignment]
        # A loopback connection passes the transport check without operator
        # auth, so only the frame-type gate keeps it from the to-do list.
        server._is_loopback_client = lambda websocket: websocket is loopback  # type: ignore[method-assign]
        server._client_authenticated_streams[token_cli] = "hosta:seat"
        server._connection_trust[peer] = _trust("public-client")
        server._connection_trust[trusted] = _trust()
        sent: list[tuple[object, str]] = []

        def enqueue(websocket: object, emitted_type: str, encoded: str) -> bool:
            assert json.loads(encoded)["items"] == [{"item_id": "i1"}]
            sent.append((websocket, emitted_type))
            return True

        server._enqueue = enqueue  # type: ignore[method-assign]
        await server.broadcast({"type": "todo.inventory", "items": [{"item_id": "i1"}]})
        assert sent == [(trusted, "todo.inventory")]

    asyncio.run(go())


def test_todo_inventory_is_coalescible_like_schedule_inventory() -> None:
    from server import COALESCIBLE_BROADCAST_FRAME_TYPES

    assert "todo.inventory" in COALESCIBLE_BROADCAST_FRAME_TYPES
