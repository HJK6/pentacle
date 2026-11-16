"""Frozen, seeded acceptance matrix for the per-input activity projection."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from store import Store


STREAM_ID = "fixture-host-chat:assistant"
FIXTURE = Path(__file__).parent / "fixtures" / "assistant_activity_cases.json"
ROUTE_COLUMNS = (
    "route_id", "stream_id", "input_identity", "payload_digest", "input_request_id",
    "event_id", "body", "attachments_json", "reply_to_message_id", "reply_to_question_id",
    "actor_stream_id", "routing_state", "delivery_state", "dispatch_id", "route_target",
    "route_target_generation", "route_json", "depends_on_message_id", "error_code",
    "lease_owner", "lease_until", "created_at", "updated_at",
)
PUBLICATION_COLUMNS = (
    "publication_key", "stream_id", "payload_digest", "canonical_payload_json", "dispatch_id",
    "reply_to_message_id", "reply_to_question_id", "publish_kind", "attachment_ids_json",
    "evidence_refs_json", "event_id", "created_at",
)
LANE_COLUMNS = (
    "lane_id", "stream_id", "parent_lane_id", "phase", "bound_stream_id", "bound_generation",
    "bound_backend_kind", "completion_report_id", "summary", "pending_question_id",
    "question_bridge_operation_id", "version", "created_at", "updated_at",
)
OPERATION_COLUMNS = (
    "operation_id", "stream_id", "dispatch_id", "lane_id", "reply_to_message_id", "operation",
    "payload_digest", "payload_json", "evidence_refs_json", "expected_lane_version",
    "actor_stream_id", "prior_phase", "next_phase", "created_at",
)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _without(mapping: dict, key: str) -> dict:
    return {name: value for name, value in mapping.items() if name != key}


def _load_fixture() -> dict:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert fixture["schema_version"] == 1
    assert len(fixture["cases"]) == 7
    for case in fixture["cases"]:
        assert _digest(_without(case, "case_sha256")) == case["case_sha256"]
        assert case["cells"]
        for cell in case["cells"]:
            assert _digest(_without(cell, "cell_sha256")) == cell["cell_sha256"]
    return fixture


def _json_value(value: object) -> str:
    return value if isinstance(value, str) else _canonical(value)


async def _seed(store: Store, cell: dict) -> None:
    await store.ensure_assistant_composite_projection(stream_id=STREAM_ID)

    def insert(conn) -> None:
        for route in cell["routes"]:
            values = {
                **route,
                "attachments_json": _json_value(route["attachments_json"]),
                "route_json": _json_value(route["route_json"]),
            }
            conn.execute(
                f"INSERT INTO v2_assistant_composite_routes ({','.join(ROUTE_COLUMNS)}) "
                f"VALUES ({','.join('?' for _ in ROUTE_COLUMNS)})",
                tuple(values[column] for column in ROUTE_COLUMNS),
            )
        for publication in cell["publications"]:
            values = {
                **publication,
                "canonical_payload_json": _json_value(publication["canonical_payload_json"]),
                "attachment_ids_json": _json_value(publication["attachment_ids_json"]),
                "evidence_refs_json": _json_value(publication["evidence_refs_json"]),
            }
            conn.execute(
                f"INSERT INTO v2_assistant_composite_publications ({','.join(PUBLICATION_COLUMNS)}) "
                f"VALUES ({','.join('?' for _ in PUBLICATION_COLUMNS)})",
                tuple(values[column] for column in PUBLICATION_COLUMNS),
            )
        for lane in cell["lanes"]:
            conn.execute(
                f"INSERT INTO v2_assistant_composite_lanes ({','.join(LANE_COLUMNS)}) "
                f"VALUES ({','.join('?' for _ in LANE_COLUMNS)})",
                tuple(lane[column] for column in LANE_COLUMNS),
            )
        for operation in cell["operations"]:
            values = {
                **operation,
                "payload_json": _json_value(operation["payload_json"]),
                "evidence_refs_json": _json_value(operation["evidence_refs_json"]),
            }
            conn.execute(
                f"INSERT INTO v2_assistant_composite_operations ({','.join(OPERATION_COLUMNS)}) "
                f"VALUES ({','.join('?' for _ in OPERATION_COLUMNS)})",
                tuple(values[column] for column in OPERATION_COLUMNS),
            )
        conn.commit()

    await store.submit(insert)


def test_frozen_activity_cases_match_live_and_history_projection() -> None:
    fixture = _load_fixture()

    async def run() -> None:
        for case in fixture["cases"]:
            for cell in case["cells"]:
                store = Store(":memory:")
                store.start()
                try:
                    await _seed(store, cell)
                    expected = cell["expected_inputs"]
                    input_ids = list(expected)
                    live = await store.assistant_composite_activity(stream_id=STREAM_ID)
                    history = await store.assistant_composite_activity(
                        stream_id=STREAM_ID, input_ids=input_ids,
                    )
                    assert live == expected, (case["case_key"], cell["cell_key"], live)
                    assert history == expected, (case["case_key"], cell["cell_key"], history)
                    assert live == history

                    from assistant_activity import summarize_activity

                    snapshot = summarize_activity(history)
                    for key, value in cell["expected_snapshot"].items():
                        assert snapshot[key] == value, (case["case_key"], cell["cell_key"], snapshot)
                finally:
                    store.stop()

    asyncio.run(run())


def test_route_replay_preserves_identity_dispatch_and_timing_receipts() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            await store.ensure_assistant_composite_projection(stream_id=STREAM_ID)
            first = await store.admit_assistant_composite_input(
                stream_id=STREAM_ID,
                input_identity="replay-input",
                input_request_id="replay-request-1",
                body="fixture replay",
                attachments=[],
                reply_to_message_id=None,
                reply_to_question_id=None,
                actor_stream_id="operator:fixture",
            )
            claimed = await store.claim_assistant_composite_route(
                stream_id=STREAM_ID, owner="fixture-router",
            )
            assert claimed is not None
            resolved = await store.update_assistant_composite_route(
                claimed["route_id"],
                routing_state="resolved",
                delivery_state="landed",
                dispatch_id="dispatch-replay",
                route_payload={"schema_version": "fixture-replay"},
            )
            assert resolved is not None
            before = await store.get_assistant_composite_route(
                stream_id=STREAM_ID, input_identity="replay-input",
            )
            assert before is not None
            await store.update_assistant_composite_route(
                claimed["route_id"],
                routing_state="resolved",
                delivery_state="landed",
                dispatch_id="dispatch-replay",
                route_payload={
                    "schema_version": "fixture-replay-mutated",
                    "timings": {"routing_started_at": "1999-01-01T00:00:00.000Z"},
                },
            )
            after = await store.get_assistant_composite_route(
                stream_id=STREAM_ID, input_identity="replay-input",
            )
            assert after is not None
            assert after["route_id"] == first["route_id"] == before["route_id"]
            assert after["dispatch_id"] == before["dispatch_id"] == "dispatch-replay"
            before_json = json.loads(before["route_json"])
            after_json = json.loads(after["route_json"])
            assert after_json["timings"] == before_json["timings"]
            assert after["created_at"] == before["created_at"]
        finally:
            store.stop()

    asyncio.run(run())
