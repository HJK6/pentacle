"""Replay a provider's fast terminal failure through the actual smoke reader."""
from contextlib import contextmanager
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from codex_rollout_norm import normalize_codex_rollout_record
import spawn_fleet_smoke as smoke


STREAM = "satellite:v2-fleet-smoke-codex-fixture"
MARKER = "PENTACLE_FLEET_SMOKE_fixture"


def terminal_error(duration_ms):
    # Native shape observed on two separate failed turns. Arbitrary provider
    # text must never be copied into the normalized event or smoke report.
    return {"type": "event_msg", "ordinal": 10, "payload": {
        "type": "task_complete", "turn_id": f"fixture-turn-{duration_ms}",
        "last_agent_message": None, "duration_ms": duration_ms,
        "error": {"message": "private provider detail",
                  "codex_error_info": {"response_too_many_failed_attempts": {
                      "http_status_code": 429}}},
    }}


@pytest.mark.parametrize("duration_ms", [317, 366])
def test_terminal_throttle_stops_reply_wait_without_another_receive(monkeypatch, duration_ms):
    event = normalize_codex_rollout_record(
        terminal_error(duration_ms), host="satellite",
        session_name=STREAM.split(":", 1)[1], session_id="fixture-native",
    )[0]
    frames = [
        {"type": "chat.event", "event": {"stream_id": STREAM, "kind": "USER", "text": MARKER,
                                           "raw": {"source": "structured"}}},
        {"type": "chat.event", "event": event},
    ]

    class Socket:
        def recv(self, timeout=None):
            assert frames, "smoke ignored the terminal provider error and waited again"
            return json.dumps(frames.pop(0))

    @contextmanager
    def connection(*_args):
        yield SimpleNamespace(snapshot=None, socket=Socket())

    monkeypatch.setattr(smoke, "authenticated_operator_connection", connection)
    registry = SimpleNamespace(configure_runtime=lambda _: None)
    with smoke._operator_connection("ws://fixture", None, 180.0, registry) as closures:
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="provider_throttled") as caught:
            closures[2](STREAM, MARKER)
        assert time.monotonic() - started < 15
    assert smoke.classify_cell_outcome(caught.value) == {
        "class": "untested", "reason": "provider_throttled",
    }
    assert "private provider detail" not in str(caught.value)
    assert "input_accepted=true" in str(caught.value)
    assert "transcript_bound=true" in str(caught.value)
    assert not frames


@pytest.mark.parametrize("native_code,expected", [
    ({"response_too_many_failed_attempts": {"http_status_code": 429}}, "provider_throttled"),
    ({"response_too_many_failed_attempts": {"http_status_code": "429"}}, "unknown"),
    ({"response_too_many_failed_attempts": {"http_status_code": 500}}, "unknown"),
    ({"response_too_many_failed_attempts": None}, "unknown"),
    ("server_overloaded", "server_overloaded"),
    ("429 private provider detail", "unknown"),
    (None, "unknown"),
])
def test_only_typed_throttle_is_environmental_and_details_are_redacted(native_code, expected):
    record = terminal_error(317)
    record["payload"]["error"]["codex_error_info"] = native_code
    event = normalize_codex_rollout_record(
        record, host="satellite", session_name="fixture", session_id="fixture-native",
    )[0]
    assert event["raw"]["provider_error_code"] == expected
    assert event["kind"] == "ERROR"
    assert "private provider detail" not in json.dumps(event)


@pytest.mark.parametrize("code", ["unknown", "server_overloaded"])
def test_legacy_or_unknown_terminal_error_fails_fast(monkeypatch, code):
    event = {"stream_id": STREAM, "kind": "ERROR", "text": "private provider detail",
             "raw": {"source": "structured", "subtype": "provider-turn-error",
                     "provider_error_code": code}}
    frames = [{"type": "chat.event", "event": event}]

    class Socket:
        def recv(self, timeout=None):
            assert frames, "waited after a terminal error"
            return json.dumps(frames.pop(0))

    @contextmanager
    def connection(*_args):
        yield SimpleNamespace(snapshot=None, socket=Socket())

    monkeypatch.setattr(smoke, "authenticated_operator_connection", connection)
    with smoke._operator_connection("ws://fixture", None, 180,
                                    SimpleNamespace(configure_runtime=lambda _: None)) as closures:
        with pytest.raises(smoke.ProviderTurnError) as caught:
            closures[2](STREAM, MARKER)
    assert smoke.classify_cell_outcome(caught.value) == {"class": "failure", "reason": "provider_error"}
    assert "input_accepted=unknown" in str(caught.value)
    assert "transcript_bound=true" in str(caught.value)
    assert "private provider detail" not in str(caught.value)


@pytest.mark.parametrize("seen_user", [True, False])
def test_timeout_reports_accepted_send_and_only_observed_progress(monkeypatch, seen_user):
    frames = []
    sent = []

    class Socket:
        def send(self, text):
            request = json.loads(text)
            sent.append(request)
            if seen_user:
                frames.append({"type": "chat.event", "event": {
                    "stream_id": STREAM, "kind": "USER", "text": MARKER,
                    "raw": {"source": "structured"},
                }})
            frames.append({"type": "send.result", "request_id": request["request_id"],
                           "to_stream_id": STREAM, "receipt_id": "fixture-receipt", "delivery": "landed"})

        def recv(self, timeout=None):
            if not frames:
                raise TimeoutError("the final receive remainder is not the cell duration")
            return json.dumps(frames.pop(0))

    @contextmanager
    def connection(*_args):
        yield SimpleNamespace(snapshot=None, socket=Socket())

    monkeypatch.setattr(smoke, "authenticated_operator_connection", connection)
    with smoke._operator_connection("ws://fixture", None, 180,
                                    SimpleNamespace(configure_runtime=lambda _: None)) as closures:
        closures[0]({"type": "send"}, "send")
        with pytest.raises(RuntimeError, match="reply wait timed out") as caught:
            closures[2](STREAM, MARKER)
    detail = str(caught.value)
    assert "send_accepted=true" in detail
    assert f"input_accepted={'true' if seen_user else 'unknown'}" in detail
    assert f"transcript_bound={'true' if seen_user else 'unknown'}" in detail
    assert "reply_seen=unknown" in detail
    assert "remainder" not in detail
    assert len(sent) == 1


def test_foreign_and_nonterminal_errors_do_not_end_a_successful_cell(monkeypatch):
    frames = [{"type": "chat.event", "event": event} for event in [
        {"stream_id": "foreign:fixture", "kind": "ERROR", "raw": {
            "source": "structured", "subtype": "provider-turn-error", "provider_error_code": "provider_throttled"}},
        {"stream_id": STREAM, "kind": "ERROR", "raw": {"source": "structured", "subtype": "tool-error"}},
        {"stream_id": STREAM, "kind": "ASSIST_TEXT", "text": MARKER},
    ]]

    class Socket:
        def recv(self, timeout=None):
            if not frames:
                assert timeout == 0, "waited again after the reply"
                raise TimeoutError
            return json.dumps(frames.pop(0))

    @contextmanager
    def connection(*_args):
        yield SimpleNamespace(snapshot=None, socket=Socket())

    monkeypatch.setattr(smoke, "authenticated_operator_connection", connection)
    with smoke._operator_connection("ws://fixture", None, 180,
                                    SimpleNamespace(configure_runtime=lambda _: None)) as closures:
        closures[2](STREAM, MARKER)
    assert not frames


def cell_callbacks(error, *, rejected_send=False, teardown_failure=False):
    calls = []

    def rpc(payload, prefix):
        calls.append(prefix)
        if prefix == "spawn_catalog_get":
            return {"type": "spawn_catalog_get.ok", "catalog_version": "fixture",
                    "profiles": {"desktop_manual": {"codex": ("gpt-6-luna", "max")}}}
        if prefix == "spawn":
            return {"stream_id": STREAM}
        if prefix == "send":
            return {"type": "send.result", "receipt_id": "fixture-receipt",
                    "delivery": "not_landed" if rejected_send else "landed"}
        raise AssertionError(prefix)

    def wait(*_args):
        calls.append("wait_event")
        raise error

    def close(*_args):
        calls.append("close")
        if teardown_failure:
            raise RuntimeError("fixture teardown failed")

    return calls, dict(rpc=rpc, wait_ready=lambda _: None, wait_event=wait,
                      register_owned=lambda *_: None, close_owned=close, verify_teardown=close)


@pytest.mark.parametrize("teardown_failure", [False, True])
def test_terminal_error_is_not_retried_and_teardown_failure_stays_red(teardown_failure):
    error = smoke.ProviderTurnError("provider_throttled", "fixture progress")
    calls, callbacks = cell_callbacks(error, teardown_failure=teardown_failure)
    with pytest.raises(RuntimeError) as caught:
        smoke.run_cell("satellite", "codex", "promptless", **callbacks,
                       capture_pane=lambda *_: pytest.fail("terminal error needs no extra pane capture"))
    assert calls.count("send") == calls.count("wait_event") == calls.count("close") == 1
    if teardown_failure:
        assert smoke.classify_cell_outcome(caught.value)["class"] == "failure"
        assert "teardown" in str(caught.value)
    else:
        assert caught.value is error


def test_rejected_send_never_burns_the_reply_timeout():
    calls, callbacks = cell_callbacks(AssertionError("must not wait"), rejected_send=True)
    with pytest.raises(RuntimeError, match="send_accepted=false"):
        smoke.run_cell("satellite", "codex", "promptless", **callbacks)
    assert "wait_event" not in calls
    assert calls.count("send") == calls.count("close") == 1


def test_matrix_and_cli_preserve_throttled_cell_as_nonpass(monkeypatch, capsys):
    calls, callbacks = cell_callbacks(smoke.ProviderTurnError("provider_throttled", "fixture progress"))

    @contextmanager
    def connection(*_args):
        yield (callbacks["rpc"], callbacks["wait_ready"], callbacks["wait_event"],
               callbacks["register_owned"], callbacks["close_owned"])

    monkeypatch.setattr(smoke, "_operator_connection", connection)
    monkeypatch.setattr(smoke, "publish_smoke_card", lambda payload: {"cleaned_up": True})
    evidence = []
    rows = smoke.run_matrix("ws://fixture", Path("unused"), 180,
                            (("satellite", "codex", "promptless"),), evidence=evidence)
    assert rows[0]["class"] == "untested"
    assert rows[0]["reason"] == "provider_throttled"
    assert evidence[0]["outcome"] == "UNTESTED"
    assert calls.count("send") == calls.count("close") == 1
    monkeypatch.setattr(smoke, "run_matrix", lambda *a, **kw: rows)
    assert smoke.main(["ws://fixture", "satellite"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "UNTESTED" and payload["ok"] is False
    assert payload["untested"][0]["reason"] == "provider_throttled"
