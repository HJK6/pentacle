import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from queued_send_now_probe import probe, program


def fixture(tmp_path, monkeypatch, provider="claude"):
    binary = tmp_path / "native-provider"
    binary.write_bytes(b"qualified-native-fixture")
    root = tmp_path / "projects"
    directory = root / "owned"
    directory.mkdir(parents=True)
    path = directory / "native-id.jsonl"
    now = "2026-10-29T07:00:00Z"
    if provider == "claude":
        records = [{"type": "user", "uuid": "record-1", "sessionId": "native-id",
                    "timestamp": now, "message": {"role": "user", "content": "private selected text"}}]
    else:
        records = [
            {"type": "session_meta", "ordinal": 0, "payload": {"id": "native-id"}},
            {"type": "event_msg", "ordinal": 1, "timestamp": now,
             "payload": {"type": "task_started", "turn_id": "owned-turn"}},
            {"type": "response_item", "ordinal": 2, "timestamp": now,
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": "private selected text"}]}},
        ]
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    info = path.stat()
    file_digest = hashlib.sha256(f"{path}\0{info.st_dev}\0{info.st_ino}".encode()).hexdigest()
    row = {"host": "peer", "session_name": "owned", "status": "open", "provider": provider,
           "session_generation": "generation", "pane_pid": "8123", "claude_session_id": "native-id",
           "account_binding": {"host": "peer", "provider": provider, "executable": str(binary),
               "executable_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(), "transcript_root": str(root)},
           "observer_binding": {"pane_pid": "8123", "generation": "generation",
               "pane_started_at": "Thu Oct 29 07:00:00 2026", "executable": str(binary),
               "transcript": {"native_session_id": "native-id", "source_file_identity_digest": file_digest}}}

    def run(args, **_kwargs):
        if args[0] == "ps" and "pid=,uid=,lstart=,command=" in args:
            return SimpleNamespace(returncode=0, stdout=f"8123 {os.getuid()} Thu Oct 29 07:00:00 2026 {binary}\n")
        if args == ("ps", "-eo", "pid=,ppid="):
            return SimpleNamespace(returncode=0, stdout="8123 1\n")
        if args[0] == "lsof":
            return SimpleNamespace(returncode=0, stdout=f"p8123\nf4\naw\nn{path}\n")
        raise AssertionError(args)

    import subprocess
    monkeypatch.setattr(subprocess, "run", run)
    original_exists = os.path.exists
    monkeypatch.setattr(os.path, "exists", lambda p: False if p == "/proc/8123/exe" else original_exists(p))
    return row, path, records


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_exact_source_yields_digests_only(tmp_path, monkeypatch, provider):
    row, _path, _records = fixture(tmp_path, monkeypatch, provider)
    result = probe(row)
    assert result["ok"]
    assert "private selected text" not in json.dumps(result)
    assert result["source_session_id"] == "native-id"
    assert len(result["input_candidates"]) == 1
    if provider == "codex":
        assert result["active_turn_id"] == "owned-turn" and result["watermark"] == 2


@pytest.mark.parametrize("change,reason", [
    ("generation", "binding_changed"), ("hash", "provider_executable_changed"),
    ("file", "native_file_changed"), ("session", "native_session_changed"),
    ("partial", "native_prefix_incomplete"),
])
def test_changed_or_incomplete_native_proof_refuses(tmp_path, monkeypatch, change, reason):
    row, path, records = fixture(tmp_path, monkeypatch)
    if change == "generation": row["session_generation"] = "replacement"
    elif change == "hash": row["account_binding"]["executable_sha256"] = "different"
    elif change == "file": row["observer_binding"]["transcript"]["source_file_identity_digest"] = "different"
    elif change == "session":
        records[0]["sessionId"] = "foreign"
        path.write_text(json.dumps(records[0]) + "\n")
    elif change == "partial": path.write_text(path.read_text() + "{")
    assert probe(row) == {"ok": False, "reason": reason}


def test_missing_native_ordinal_cannot_authorize_codex_key(tmp_path, monkeypatch):
    row, path, records = fixture(tmp_path, monkeypatch, "codex")
    records[-1].pop("ordinal")
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    assert probe(row) == {"ok": False, "reason": "native_ordinal_unproven"}


def test_transported_program_is_bounded_and_contains_exact_normalizers():
    import subprocess
    import sys
    for provider in ("claude", "codex"):
        source = program(provider)
        compile(source, "transported-native-proof", "exec")
        assert len(source.encode()) < 100000
        assert "normalize_claude_jsonl_records" in source
        assert ("types.ModuleType('codex_rollout_norm')" in source) == (provider == "codex")
        result = subprocess.run([sys.executable, "-I", "-c", source, "{}"],
                                capture_output=True, text=True, timeout=5, check=True)
        assert json.loads(result.stdout) == {"ok": False, "reason": "native_proof_unavailable"}
