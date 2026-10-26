from __future__ import annotations

import json
from pathlib import Path

import pytest

from send_boundary_capture import CALLERS, PATHS, cell_key, run_cell

FIXTURE = json.loads((Path(__file__).parent / "fixtures/send_boundary/base_frames_889ca9db.json").read_text())


@pytest.mark.parametrize("key,expected", FIXTURE["cells"].items())
def test_base_wire_parity(key, expected):
    path, identity, state, caller = key.split("/")
    if path == "reader" and identity == "RD":
        actual = run_cell(path, "OP", state)
    elif path == "envelope":
        actual = run_cell(path, identity, state, form=caller)
    else:
        actual = run_cell(path, identity, state, caller)
    # Parity concerns transmitted bytes, not a socket opened then closed
    # before admission. Dashboard's stronger no-connect rule is tested below.
    actual["frames"] = [frames for frames in actual["frames"] if frames]
    assert actual == expected


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("identity", ["CD", "BK"])
@pytest.mark.parametrize("state", [f"F{i}" for i in range(1, 9)])
@pytest.mark.parametrize("caller", CALLERS)
def test_service_wire_matrix(path, identity, state, caller):
    actual = run_cell(path, identity, state, caller)
    assert actual["closed"]
    frames = [json.loads(raw) for socket in actual["frames"] for raw in socket]
    if path == "status" and identity == "CD":
        # This extra builder-policy row rejects a colon-free self stream id
        # before transport on the base too. Its egress class is covered by rpc.
        assert not frames and actual["error"] == "ValueError"
        return
    if state != "F1":
        assert not frames, (path, identity, state, frames)
        if path == "thread":
            assert actual["result"] == 2
            assert len(actual["output"][1].splitlines()) == 1
        else:
            assert actual["error"] == "ServiceCredentialUnavailable"
        return
    assert frames
    for frame in frames:
        assert "OPERATOR-sentinel" not in json.dumps(frame)
        assert "CALLER-" not in json.dumps(frame)
        assert "identity_token" not in frame
        if frame["type"] not in {"pong", "fetch_blob", "upload_blob_chunk", "upload_prompt_blob_chunk"}:
            assert frame["from_stream_id"] == {"CD": "altum-bot-cd", "BK": "amaterasu:wmi-pg-dailybackup"}[identity]
            assert frame["stream_token"] == "FILE-sentinel"
            assert "token" not in frame


@pytest.mark.parametrize("state", ["R1", "PADDED", "R2", "SPACE", "R3"])
@pytest.mark.parametrize("seat_env", [False, True])
def test_reader_matrix(state, seat_env):
    actual = run_cell("reader", "OP", state, seat_env=seat_env)
    frames = [json.loads(raw) for socket in actual["frames"] for raw in socket]
    if state in {"R2", "SPACE"} or (seat_env and state in {"R1", "PADDED"}):
        assert not frames
        assert actual["result"]["error_code"] == "unavailable"
    elif state in {"R1", "PADDED"}:
        hello, request = [frame for frame in frames if frame["type"] != "pong"]
        assert hello["from_stream_id"] == "work-report-reader"
        assert hello["stream_token"] == (" READER-sentinel " if state == "PADDED" else "READER-sentinel")
        assert "token" not in hello
        assert set(request) == {"type", "request_id", "report_id"}


@pytest.mark.parametrize("identity", ["CD", "BK", "RD"])
@pytest.mark.parametrize("state", ["F1", "F7"])
@pytest.mark.parametrize("form", ["bearer", "envelope"])
def test_dashboard_service_never_connects(identity, state, form):
    actual = run_cell("envelope", identity, state, form=form)
    assert actual["frames"] == []
    assert actual["error"] == "ServiceCredentialUnavailable"
