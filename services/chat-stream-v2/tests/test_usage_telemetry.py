"""Fail-first Stage 1 journeys for authenticated remote usage accounting."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path

from event_push import EventPush
from store import Store


FIXTURE = Path(__file__).parent / "fixtures" / "usage_telemetry_cases.json"
MANIFEST_SCHEMA = Path(__file__).parent / "fixtures" / "usage_stage1_manifest_schema.json"
SHARED_SECRET = "secret"
HOST_SECRET = "amaterasu-host-secret"
SATELLITE_SHA = "a" * 40
SATELLITE_PID = 1234
STREAM_ID = "amaterasu:v2-codex"
SESSION_NAME = "v2-codex"
GENERATION = "generation-a"
PANE_PID = "8123"
SOURCE_DIGEST = "a" * 64


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _case(provider: str) -> dict:
    return next(item for item in _fixture()["cases"] if item["provider"] == provider)


def _host_proof(host: str, *, secret: str = HOST_SECRET) -> str:
    message = f"event.push.v1\0{host}\0{SATELLITE_SHA}\0{SATELLITE_PID}"
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def _push(
    records: list[dict],
    *,
    host: str = "amaterasu",
    request_id: str = "usage-1",
    generation: str = GENERATION,
    pane_pid: str = PANE_PID,
    source_digest: str = SOURCE_DIGEST,
    provider: str = "codex",
    proof: str | None = None,
) -> dict:
    usage = {
        "stream_id": STREAM_ID,
        "provider": provider,
        "session_generation": generation,
        "source_pane_pid": pane_pid,
        "native_session_id": "native-codex-redacted",
        "source_file_identity_digest": source_digest,
        "records": records,
    }
    return {
        "type": "event.push",
        "request_id": request_id,
        "push_secret": SHARED_SECRET,
        "satellite_sha": SATELLITE_SHA,
        "satellite_pid": SATELLITE_PID,
        "wire_version": 1,
        "host": host,
        "source_host_proof": proof if proof is not None else _host_proof(host),
        "events": [],
        "usage": [usage],
        "high_water": {},
        "inventory": [SESSION_NAME],
        "frozen_streams": [],
    }


async def _secret(value: str) -> str:
    return value


async def _setup():
    store = Store(":memory:")
    store.start()
    row = await store.open_session(
        "amaterasu",
        SESSION_NAME,
        provider="codex",
        pane_pid=PANE_PID,
        session_generation=GENERATION,
        observer_binding={
            "executable": "/usr/bin/codex",
            "pane_pid": PANE_PID,
            "pane_started_at": "start-a",
        },
    )
    ep = EventPush(
        store,
        lambda _frame: asyncio.sleep(0),
        _Alerts(),
        recent_limit=20,
        host_secrets={"amaterasu": HOST_SECRET},
    )
    ep._secret = lambda: _secret(SHARED_SECRET)
    return store, row, ep


def test_frozen_fixture_covers_native_providers_and_redaction() -> None:
    payload = _fixture()
    assert payload["schema_version"] == 1
    assert {case["provider"] for case in payload["cases"]} == {"codex", "claude"}
    assert all(len(case["records"]) >= 2 for case in payload["cases"])
    assert all(len(case["source_file_identity_digest"]) == 64 for case in payload["cases"])
    for case in payload["cases"]:
        unsigned = {key: value for key, value in case.items() if key != "case_sha256"}
        digest = hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        assert case["case_sha256"] == digest
    assert "native-codex-redacted" in FIXTURE.read_text(encoding="utf-8")
    assert "prompt" not in FIXTURE.read_text(encoding="utf-8").lower()
    assert {case["expected_reason"] for case in payload["fence_cases"]} == {
        "unauthorized_source_host",
        "source_identity_mismatch",
        "generation_mismatch",
        "pane_pid_mismatch",
        "provider_mismatch",
    }


def test_manifest_schema_is_exact_and_candidate_bound() -> None:
    schema = json.loads(MANIFEST_SCHEMA.read_text(encoding="utf-8"))
    assert schema["schema"] == "pentacle.usage-accounting.stage1"
    assert schema["schema_version"] == 1
    assert schema["required"] == [
        "schema", "schema_version", "candidate_sha", "base_sha", "selector",
        "overlay", "runtime", "pin", "streams", "receipts",
    ]
    assert schema["properties"]["selector"]["const"] == [
        "services/chat-stream-v2/tests/test_usage_accounting.py",
        "services/chat-stream-v2/tests/test_event_push.py",
        "services/chat-stream-v2/tests/test_usage_telemetry.py",
    ]
    assert schema["properties"]["runtime"]["properties"]["satellites"]["required"] == [
        "amaterasu", "merlin",
    ]
    assert schema["properties"]["streams"]["items"]["$ref"] == "#/$defs/stream"


def test_remote_usage_push_authenticates_source_fences_and_replays_by_record() -> None:
    async def run() -> None:
        store, row, ep = await _setup()
        try:
            records = _case("codex")["records"]
            first = await ep.handle_push(_push(records, request_id="usage-1"))
            same_request = await ep.handle_push(_push(records, request_id="usage-1"))
            new_request = await ep.handle_push(_push(records, request_id="usage-2"))
            usage = (await store.fetch_session("amaterasu", SESSION_NAME))["usage"]
            assert first["request_id"] == "usage-1"
            assert first["usage_recorded"] == 1
            assert first["usage_replayed"] == 0
            assert same_request["request_id"] == "usage-1"
            assert same_request["usage_recorded"] == 0
            assert same_request["usage_replayed"] == 1
            assert new_request["request_id"] == "usage-2"
            assert new_request["usage_recorded"] == 0
            assert new_request["usage_replayed"] == 1
            assert usage["collection_host"] == "amaterasu"
            assert usage["session_generation"] == row["session_generation"] == GENERATION
            assert usage["revision"] == 1
            assert usage["tokens"] == _case("codex")["expected_tokens"]
        finally:
            store.stop()

    asyncio.run(run())


def test_remote_usage_rejects_host_source_generation_pid_and_provider_changes_without_writes() -> None:
    async def run() -> None:
        store, _row, ep = await _setup()
        try:
            records = _case("codex")["records"]
            accepted = await ep.handle_push(_push(records, request_id="usage-bind"))
            assert accepted["usage_recorded"] == 1
            before = (await store.fetch_session("amaterasu", SESSION_NAME))["usage"]

            wrong_host = await ep.handle_push(_push(
                records, host="merlin", request_id="usage-wrong-host",
                proof=_host_proof("amaterasu"),
            ))
            wrong_source = await ep.handle_push(_push(
                records, request_id="usage-wrong-source", source_digest="b" * 64,
            ))
            wrong_generation = await ep.handle_push(_push(
                records, request_id="usage-wrong-generation", generation="generation-b",
            ))
            wrong_pid = await ep.handle_push(_push(
                records, request_id="usage-wrong-pid", pane_pid="9999",
            ))
            wrong_provider = await ep.handle_push(_push(
                records, request_id="usage-wrong-provider", provider="claude",
            ))
            after = (await store.fetch_session("amaterasu", SESSION_NAME))["usage"]

            assert wrong_host["error"] == "unauthorized_source_host"
            assert wrong_source["usage_recorded"] == 0
            assert wrong_source["usage_rejected"] == [{
                "stream_id": STREAM_ID, "reason": "source_identity_mismatch",
            }]
            assert wrong_generation["usage_rejected"] == [{
                "stream_id": STREAM_ID, "reason": "generation_mismatch",
            }]
            assert wrong_pid["usage_rejected"] == [{
                "stream_id": STREAM_ID, "reason": "pane_pid_mismatch",
            }]
            assert wrong_provider["usage_rejected"] == [{
                "stream_id": STREAM_ID, "reason": "provider_mismatch",
            }]
            assert after["revision"] == before["revision"] == 1
            assert after["tokens"] == before["tokens"]
        finally:
            store.stop()

    asyncio.run(run())


def test_codex_and_claude_snapshots_preserve_native_fields_and_label_derived_input() -> None:
    async def run() -> None:
        for case in _fixture()["cases"]:
            store = Store(":memory:")
            store.start()
            try:
                row = await store.open_session(
                    "amaterasu", f"v2-{case['provider']}", provider=case["provider"],
                    session_generation=f"generation-{case['provider']}",
                )
                observed = await store.record_usage(
                    row,
                    case["records"],
                    native_session_id=case["native_session_id"],
                    collection_host="amaterasu",
                )
                assert observed is not None
                assert observed["tokens"] == case["expected_tokens"]
                assert observed.get("derived_tokens", {}) == case["expected_derived_tokens"]
                assert observed.get("derived_fields", {}) == case["expected_derived_fields"]
                if case["provider"] == "codex":
                    assert observed["derived_tokens"]["uncached_input"] == 90
                    assert observed["derived_fields"]["uncached_input"] == "input_total - cached_input"
            finally:
                store.stop()

    asyncio.run(run())


class _Alerts:
    def emit(self, *_args, **_kwargs):
        return None
