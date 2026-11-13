"""Persisted-position behavior across real process and Store lifetimes."""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import signal
import select
import sys
import tarfile

import pytest

from test_restart_replay import records


def write(path, items):
    path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items))


class IntakeProcess:
    def __init__(self, root, transcript, provider, *, force_full=False, source_root=None):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.err = (root / "child.stderr").open("a")
        self.killed = False
        search = list(sys.path)
        if source_root is not None:
            services = source_root / "services"
            search = [str(p) for p in (services / "chat-stream-v2", services,
                                      services / "agent-orch", services / "_shared", services / "chat-stream-v2/tools")] + search
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(search),
               "PENTACLE_INGEST_FORCE_FULL_REPLAY": "1" if force_full else "0"}
        self.child = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("intake_resume_process.py")),
             str(root), str(transcript), provider], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.err, text=True)

    def command(self, op, **kwargs):
        self.child.stdin.write(json.dumps({"op": op, **kwargs}) + "\n")
        self.child.stdin.flush()
        # Child exceptions close the pipe; include their durable traceback.
        assert select.select([self.child.stdout], [], [], 30)[0], "intake command timed out"
        line = self.child.stdout.readline()
        assert line, (self.root / "child.stderr").read_text()
        return json.loads(line)

    def close(self):
        if self.child.poll() is None:
            self.child.stdin.write('{"op":"close"}\n')
            self.child.stdin.flush()
        try:
            code = self.child.wait(timeout=15)
            assert code == (-signal.SIGKILL if self.killed else 0), (self.root / "child.stderr").read_text()
        finally:
            if self.child.poll() is None:
                self.child.kill()
                self.child.wait(timeout=5)
            self.child.stdin.close()
            self.child.stdout.close()
            self.err.close()

    def pass_until_killed(self, stage):
        self.command("kill_at", stage=stage)
        self.child.stdin.write('{"op":"pass"}\n')
        self.child.stdin.flush()
        assert select.select([self.child.stdout], [], [], 30)[0], "kill boundary was not reached"
        assert json.loads(self.child.stdout.readline()) == {"kill_ready": stage}
        self.child.kill()
        code = self.child.wait(timeout=30)
        assert code == -signal.SIGKILL, (self.root / "child.stderr").read_text()
        self.killed = True
        assert (self.root / "kill-reached").read_text() == stage


def stable(value, ranks=None):
    """Only daemon write times and row numbers are volatile; provider times stay."""
    if ranks is None:
        events = value.get("events", []) if isinstance(value, dict) else value if isinstance(value, list) else []
        ids = sorted({e["daemon_seq"] for e in events if isinstance(e, dict) and "daemon_seq" in e})
        ranks = {seq: index for index, seq in enumerate(ids, 1)}
    if isinstance(value, dict):
        return {key: ranks.get(item, item) if key in {"daemon_seq", "proof_event_id"} and type(item) is int
                else stable(item, ranks) for key, item in value.items()
                if not (key == "recorded_at" and "daemon_seq" in value)}
    if isinstance(value, list):
        return [stable(item, ranks) for item in value]
    return value


def first_difference(actual, expected, path="root"):
    if type(actual) is not type(expected):
        return path, actual, expected
    if isinstance(actual, dict) and actual.keys() == expected.keys():
        for key in actual:
            if actual[key] != expected[key]:
                return first_difference(actual[key], expected[key], path + "." + key)
    if isinstance(actual, list) and len(actual) == len(expected):
        for index, (left, right) in enumerate(zip(actual, expected)):
            if left != right:
                return first_difference(left, right, f"{path}[{index}]")
    if isinstance(actual, list) and isinstance(expected, list):
        def describe(values):
            return [{"type": v.get("type"), "kind": v.get("event", {}).get("kind"),
                     "text": v.get("event", {}).get("text"), "keys": sorted(v)}
                    if isinstance(v, dict) else v for v in values]
        return path, describe(actual), describe(expected)
    return path, actual, expected


@pytest.mark.parametrize("field,reason", [("version", "format_version"), ("generation", "generation"),
    ("lifecycle", "lifecycle"), ("provider", "provider"), ("path", "path"), ("session_id", "session_id"),
    ("file_id", "file_id"), ("offset", "line_boundary"), ("digest", "prefix_hash"),
    ("corrupt", "unreadable"), ("continuation", "provenance_shape"), ("large", "state_too_large")])
def test_invalid_saved_records_fall_back_with_reason(tmp_path, field, reason):
    path = tmp_path / "transcript.jsonl"
    write(path, records("claude", 3))
    root = tmp_path / "db"
    child = IntakeProcess(root, path, "claude")
    try:
        child.command("pass")
        saved = child.command("position")
        pos = json.loads(saved["position_json"])
        column = "position_json"
        if field == "corrupt":
            value = "{"
        elif field in {"continuation", "large"}:
            column = "provenance_json"
            value = json.dumps({"unknown": "x" * (256 * 1024 if field == "large" else 1)})
        else:
            if field == "offset":
                pos["offset"] -= 1
                pos["digest"] = hashlib.sha256(path.read_bytes()[:-1]).hexdigest()
            elif field == "version":
                pos[field] += 1
            elif field == "file_id":
                pos[field][1] += 1
            elif field == "digest":
                pos[field] = "0" * 64
            else:
                pos[field] = "different"
            if field in {"path", "session_id"}:
                context = json.loads(saved["provenance_json"])
                context["_bound"] = [pos["path"], pos["session_id"]]
                child.command("edit_position", column="provenance_json", value=json.dumps(context))
            value = json.dumps(pos)
        child.command("edit_position", column=column, value=value)
    finally:
        child.close()
    child = IntakeProcess(root, path, "claude")
    try:
        result = child.command("pass")
        assert result["resume_reason"] == reason
        assert prefix_bindings(result) == 3
        assert result["appended"] == 0
    finally:
        child.close()


@pytest.mark.parametrize("action", ["close_seat", "reopen_seat"])
def test_closed_or_recreated_lifecycle_never_uses_old_position(tmp_path, action):
    path = tmp_path / "transcript.jsonl"
    write(path, records("claude", 3))
    root = tmp_path / "db"
    child = IntakeProcess(root, path, "claude")
    try:
        child.command("pass")
        child.command(action)
    finally:
        child.close()
    child = IntakeProcess(root, path, "claude")
    try:
        result = child.command("pass")
        if action == "close_seat":
            assert result["offset"] is None
            assert prefix_bindings(result) == 0
        else:
            assert result["resume_reason"] == "generation"
            assert prefix_bindings(result) == 3
    finally:
        child.close()


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_idle_pass_does_not_write_and_unchanged_state_is_not_rewritten(tmp_path, provider):
    path = tmp_path / "transcript.jsonl"
    write(path, records(provider, 3))
    child = IntakeProcess(tmp_path / "db", path, provider)
    try:
        first = child.command("pass")
        assert len(first["writes"]) == 1
        assert child.command("pass")["writes"] == []
        write(path, records(provider, 6))
        later = child.command("pass")
        assert len(later["writes"]) == 1
        assert later["writes"][0]["columns"] == ["position_json"]
        assert later["writes"][0]["bytes"] < 4096
    finally:
        child.close()


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("count", [64, 256])
def test_restart_skips_proven_prefix(tmp_path, provider, count):
    transcript = tmp_path / "transcript.jsonl"
    items = records(provider, count)
    # Non-ASCII text also makes any character-count position error observable.
    for item in items:
        message = item.get("message") or item.get("payload")
        if message and message.get("content"):
            message["content"][0]["text"] += "λ" * 4096
    write(transcript, items)
    root = tmp_path / "db"
    first = IntakeProcess(root, transcript, provider)
    try:
        seeded = first.command("pass")
        assert seeded["offset"] == transcript.stat().st_size
        assert seeded["appended"] == count
    finally:
        first.close()
    restarted = IntakeProcess(root, transcript, provider)
    try:
        result = restarted.command("pass")
        print(json.dumps({"provider": provider, "count": count, "bytes": transcript.stat().st_size,
                          **result}, sort_keys=True))
        assert result["offset"] == transcript.stat().st_size
        assert result["failures"] == 0
        assert result["appended"] == 0
        prefix_work = {name: n for name, n in result["calls"].items()
                       if any(part in name for part in ("record_usage", "prepare_native_input_events",
                                                        "bind_send_occurrence"))}
        assert not prefix_work, f"restart repeated prefix effects: {prefix_work}"
    finally:
        restarted.close()


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("offline_tail", [False, True])
def test_resume_matches_uninterrupted_pass_boundary(tmp_path, provider, offline_tail):
    transcript = tmp_path / "transcript.jsonl"
    prefix = records(provider, 3)
    write(transcript, prefix)
    reference = IntakeProcess(tmp_path / "reference", transcript, provider)
    candidate = IntakeProcess(tmp_path / "candidate", transcript, provider)
    try:
        assert reference.command("pass")["appended"] == 3
        assert candidate.command("pass")["appended"] == 3
        candidate.close()
        candidate = None
        if offline_tail:
            write(transcript, records(provider, 6))
        candidate = IntakeProcess(tmp_path / "candidate", transcript, provider)
        left, right = reference.command("pass"), candidate.command("pass")
        assert left["failures"] == right["failures"] == 0
        assert stable(reference.command("snapshot")) == stable(candidate.command("snapshot"))
    finally:
        reference.close()
        if candidate is not None:
            candidate.close()


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_boundary_bytes_context_provenance_and_startup_projections(tmp_path, provider):
    from test_usage_provenance import _codex_rollout
    from test_usage_accounting import codex as codex_usage
    path = tmp_path / "transcript.jsonl"
    if provider == "codex":
        accounting = _codex_rollout(native="native", responses=1)
        prefix = accounting[:2] + records(provider, 1)[1:]
        suffix = [codex_usage(100, 50)] + accounting[2:] + records(provider, 2)[-1:]
    else:
        prefix = records(provider, 1)
        prefix[0].update(timestamp="2026-10-01T00:02:00Z", effort="high")
        prefix[0]["message"].update(id="prefix-response", model="claude-opus-5-5",
                                      usage={"input_tokens": 100, "output_tokens": 30,
                                             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})
        prefix.append({"type": "user", "uuid": "old-user", "parentUuid": "root", "sessionId": "native",
                       "timestamp": "2026-10-01T00:02:00Z", "message": {"role": "user", "content": "first λ"}})
        suffix = [{"type": "attachment", "uuid": "queued", "sessionId": "native",
                   "timestamp": "2026-10-01T00:01:00Z", "attachment": {
                       "type": "queued_command", "origin": {"kind": "human"}, "prompt": "queued λ"}},
                  {"type": "user", "uuid": "new-user", "parentUuid": "root", "sessionId": "native",
                   "timestamp": "2026-10-01T00:03:00Z", "message": {"role": "user", "content": "second λ"}}]
        answer = records(provider, 2)[-1]
        answer.update(parentUuid="new-user", effort="high", timestamp="2026-10-01T00:04:00Z")
        answer["message"].update(id="suffix-response", model="claude-opus-5-5", usage={"input_tokens": 120, "output_tokens": 35,
                                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})
        answer["message"]["content"].append({"type": "text", "text": "second segment"})
        suffix.append(answer)
    write(path, prefix)
    with path.open("ab") as target:
        target.write(b'bad json\n{"type":"summary","text":"\xce')
    reference = IntakeProcess(tmp_path / "reference", path, provider)
    candidate = IntakeProcess(tmp_path / "candidate", path, provider)
    try:
        for child in (reference, candidate):
            result = child.command("pass")
            assert result["failures"] == 0
            assert result["offset"] < path.stat().st_size
            assert not result["checkpoint_hold"]
            assert child.command("position") is not None
        candidate.close()
        candidate = None
        # Rebuilt fields have their own stopped-DB startup oracle; continuation
        # stays in the uninterrupted process and is never rebuilt by replay.
        reference.command("rebuild_projections")
        with path.open("ab") as target:
            target.write(b'\xbb"}\n')
            target.write("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in suffix).encode())
        candidate = IntakeProcess(tmp_path / "candidate", path, provider)
        for child in (reference, candidate):
            result = child.command("pass")
            assert result["failures"] == 0
            assert result["offset"] == path.stat().st_size
            assert not result["checkpoint_hold"]
        expected, actual = reference.command("snapshot"), candidate.command("snapshot")
        if stable(actual) != stable(expected):
            print(json.dumps(first_difference(stable(actual), stable(expected)), ensure_ascii=False))
        assert stable(actual) == stable(expected), first_difference(stable(actual), stable(expected))
        assert candidate.command("projection") == reference.command("projection")
        assert stable(candidate.command("client_history")) == stable(reference.command("client_history"))
        if provider == "claude":
            queued = next(e for e in actual["events"] if e.get("text") == "queued λ")
            assert queued["timestamp"] == "2026-10-01T00:01:00Z"
            assert queued["raw"]["queued_at"] == "2026-10-01T00:01:00Z"
        else:
            assert actual["accounting"]["v2_usage_codex_responses"][0]["model"] == "gpt-6-luna-0"
    finally:
        reference.close()
        if candidate is not None:
            candidate.close()


def prefix_bindings(result):
    return sum(n for name, n in result["calls"].items() if "bind_send_occurrence" in name)


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("owner,method", [("provenance", "admit"),
                                         ("routing", "observe_claude_event"),
                                         ("routing", "observe_context"),
                                         ("store", "save_ingest_position")])
def test_failed_effect_holds_position_across_later_success(tmp_path, provider, owner, method):
    from test_usage_accounting import codex as codex_usage
    path = tmp_path / "transcript.jsonl"
    items = records(provider, 3)
    write(path, items)
    root = tmp_path / "db"
    child = IntakeProcess(root, path, provider)
    try:
        assert child.command("pass")["appended"] == 3
        child.command("fail_once", owner=owner, method=method)
        # Include native metadata in the suffix so the provenance sink is called.
        tail = records(provider, 6)[-3:]
        if provider == "codex":
            usage = codex_usage()
            usage["timestamp"] = "2026-10-01T00:00:01Z"
            usage["payload"]["info"]["model_context_window"] = 200000
            tail.insert(0, usage)
            tail.insert(0, {"type": "session_meta", "payload": {"id": "native", "creator_account_id": "test-account"}})
            tail.insert(1, {"type": "turn_context", "payload": {"turn_id": "turn", "model": "gpt-6"}})
            tail.insert(2, {"type": "token_usage_record", "timestamp": "2026-10-01T00:00:01Z",
                            "payload": {"turn_id": "turn", "response_id": "response",
                                        "usage": {"input_tokens": 100, "cached_input_tokens": 50,
                                                  "cache_write_input_tokens": 0, "output_tokens": 30,
                                                  "reasoning_output_tokens": 10}}})
        else:
            tail[0]["message"].update(id="response", model="claude-opus-5-5", usage={"input_tokens": 100, "output_tokens": 30,
                                        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})
            tail.insert(0, {"type": "attachment", "sessionId": "native", "timestamp": "2026-10-01T00:00:01Z",
                           "attachment": {"type": "credential_org", "organizationUuid": "00000000-0000-4000-8000-00000000000a"}})
        write(path, items + tail)
        failed = child.command("pass")
        assert failed["failures"] == 0
        assert failed["injected"] == [method]
        for total in range(9, 25, 3):
            write(path, items + tail + records(provider, total)[-(total - 6):])
            result = child.command("pass")
            assert result["failures"] == 0
            assert result["writes"] == []
        assert (root / "child.stderr").read_text().count("ingest_position_held") == 1
    finally:
        child.close()
    child = IntakeProcess(root, path, provider)
    try:
        result = child.command("pass")
        assert prefix_bindings(result) == 21, result
        assert result["appended"] == 0
        assert not result["checkpoint_hold"]
    finally:
        child.close()


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("fallback", ["switch", "rewrite", "replace", "truncate"])
def test_forced_or_invalid_position_replays(tmp_path, provider, fallback):
    path = tmp_path / "transcript.jsonl"
    write(path, records(provider, 6))
    root = tmp_path / "db"
    child = IntakeProcess(root, path, provider)
    try:
        child.command("pass")
    finally:
        child.close()
    if fallback == "rewrite":
        data = path.read_bytes().replace(b"output 3", b"Output 3")
        path.write_bytes(data)
    elif fallback == "replace":
        replacement = path.with_suffix(".new")
        replacement.write_bytes(path.read_bytes())
        replacement.replace(path)
    elif fallback == "truncate":
        write(path, records(provider, 3))
    child = IntakeProcess(root, path, provider, force_full=fallback == "switch")
    try:
        result = child.command("pass")
        assert prefix_bindings(result) == (3 if fallback == "truncate" else 6)
        assert result["failures"] == 0
    finally:
        child.close()


@pytest.mark.parametrize("multiple_segments", [False, True])
def test_capped_codex_state_cannot_publish_ahead_of_effects(tmp_path, multiple_segments):
    path = tmp_path / "transcript.jsonl"
    prefix = records("codex", 2)
    write(path, prefix)
    root = tmp_path / "db"
    child = IntakeProcess(root, path, "codex")
    try:
        child.command("pass")
        tail = records("codex", 6)[-4:]
        if multiple_segments:
            from codex_rollout_norm import normalize_codex_rollout_record
            tail[1]["payload"].update(role="user", content=[
                {"type": "input_text", "text": "operator input"},
                {"type": "input_text", "text": "[from fixture:sender] [tell:multi]\npeer input"}])
            assert len(normalize_codex_rollout_record(tail[1], host="fixture", session_name="replay", session_id="native")) == 2
        tail.insert(2, {"type": "turn_context", "payload": {"turn_id": "future", "model": "future-model"}})
        write(path, prefix + tail)
        result = child.command("pass", limit=2 if multiple_segments else 1)
        assert result["appended"] == 1
        assert result["offset"] < path.stat().st_size
    finally:
        child.close()

    child = IntakeProcess(root, path, "codex")
    try:
        result = child.command("pass")
        assert prefix_bindings(result) == (5 if multiple_segments else 4)
        assert result["appended"] == (4 if multiple_segments else 3)
    finally:
        child.close()


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("stage", ["after_append", "after_binding", "before_position", "during_position"])
def test_killed_process_replays_unfinished_effects_from_previous_position(tmp_path, provider, stage):
    path = tmp_path / "transcript.jsonl"
    prefix = records(provider, 2)
    write(path, prefix)
    root = tmp_path / "db"
    child = IntakeProcess(root, path, provider)
    reference = IntakeProcess(tmp_path / "reference", path, provider)
    try:
        assert child.command("pass")["appended"] == 2
        assert reference.command("pass")["appended"] == 2
        saved = child.command("position")
        tell = child.command("seed_tell")
        assert reference.command("seed_tell")["wire"] == tell["wire"]
        assert tell["reply"]["delivery_status"] in {"committed_pending_proof", "delivered"}
        assert child.command("tell_state")["delivery"].get("proof_event_id") is None
        if provider == "claude":
            message = {"type": "user", "uuid": "input", "sessionId": "native", "timestamp": "2026-10-01T00:00:01Z",
                       "message": {"role": "user", "content": tell["wire"]}}
        else:
            message = {"type": "response_item", "timestamp": "2026-10-01T00:00:01Z", "payload": {
                "type": "message", "id": "input", "role": "user",
                "content": [{"type": "input_text", "text": tell["wire"]}]}}
        write(path, prefix + [message])
        assert reference.command("pass")["appended"] == 1
        expected_history = reference.command("client_history")
        child.pass_until_killed(stage)
    finally:
        child.close()

        reference.close()
    child = IntakeProcess(root, path, provider)
    try:
        assert child.command("position") == saved
        result = child.command("pass")
        assert result["failures"] == 0
        assert prefix_bindings(result) == 1
        state = child.command("tell_state")
        assert state["delivery"]["delivery_status"] == "delivered"
        assert state["delivery"]["proof_event_id"] is not None
        snapshot = child.command("snapshot")
        assert len(snapshot["events"]) == 3
        assert stable(child.command("client_history")) == stable(expected_history)
        assert not [f for f in snapshot["frames"] if f["type"] == "chat.event"]
        assert json.loads(child.command("position")["position_json"])["offset"] == path.stat().st_size
    finally:
        child.close()


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_actual_previous_source_interlude_preserves_newer_durable_rows(tmp_path, provider):
    """Installed predecessor, not FORCE_FULL_REPLAY masquerading as old code."""
    revision = "1e101ce86c90c09c25c362690bc8519e6a82f58d"
    checkout = Path(__file__).resolve().parents[3]
    available = subprocess.run(["git", "cat-file", "-e", revision + "^{commit}"], cwd=checkout, capture_output=True)
    if available.returncode:
        pytest.skip("Retained predecessor source required for the rollback rehearsal")
    old = tmp_path / "previous-source"
    old.mkdir()
    bundle = tmp_path / "previous-source.tar"
    with bundle.open("wb") as output:
        subprocess.run(["git", "archive", revision, "services", "pentacle-chat-core"],
                       cwd=checkout, stdout=output, check=True)
    with tarfile.open(bundle) as archive:
        archive.extractall(old, filter="data")
    path = tmp_path / "transcript.jsonl"
    write(path, records(provider, 2))
    root = tmp_path / "candidate"
    reference = IntakeProcess(tmp_path / "reference", path, provider)
    candidate = IntakeProcess(root, path, provider)
    try:
        assert reference.command("pass")["appended"] == 2
        assert candidate.command("pass")["appended"] == 2
        saved = candidate.command("position")
        candidate.close()
        candidate = None
        items = records(provider, 4)
        if provider == "codex":
            items.append({"type": "turn_context", "payload": {"turn_id": "old-turn", "model": "old-model"}})
        else:
            items.append({"type": "summary", "sessionId": "native", "summary": "old binary metadata"})
        write(path, items)
        assert reference.command("pass")["appended"] == 2
        candidate = IntakeProcess(root, path, provider, source_root=old)
        result = candidate.command("pass")
        assert result["source"].startswith(str(old))
        assert result["appended"] == 2
        candidate.close()
        candidate = None
        write(path, items + records(provider, 6)[-2:])
        reference.command("rebuild_projections")
        reference.command("pass")
        candidate = IntakeProcess(root, path, provider)
        assert candidate.command("position") == saved
        result = candidate.command("pass")
        assert result["resume_reason"] == "resumed"
        assert result["appended"] == 2
        assert prefix_bindings(result) == 4
        assert stable(candidate.command("snapshot")) == stable(reference.command("snapshot"))
        assert candidate.command("projection") == reference.command("projection")
    finally:
        reference.close()
        if candidate is not None:
            candidate.close()



def test_unavailable_provenance_history_holds_even_without_an_exception(tmp_path):
    from test_usage_provenance import _codex_rollout
    from test_usage_accounting import codex as codex_usage
    path = tmp_path / "transcript.jsonl"
    prefix = records("codex", 2)
    write(path, prefix)
    root = tmp_path / "db"
    child = IntakeProcess(root, path, "codex")
    try:
        child.command("pass")
        saved = child.command("position")
        child.command("unavailable_history")
        suffix = [codex_usage()] + _codex_rollout(native="native", responses=1) + records("codex", 3)[-1:]
        write(path, prefix + suffix)
        result = child.command("pass")
        assert result["failures"] == 0
        assert result["checkpoint_hold"] == "provenance_rejected"
        assert child.command("position") == saved
        write(path, prefix + suffix + records("codex", 4)[-1:])
        assert child.command("pass")["writes"] == []
    finally:
        child.close()
    child = IntakeProcess(root, path, "codex")
    try:
        result = child.command("pass")
        assert result["resume_reason"] == "resumed"
        assert not result["checkpoint_hold"]
        assert prefix_bindings(result) == 2
    finally:
        child.close()


def test_swallowed_routing_notification_failure_holds_position(tmp_path):
    path = tmp_path / "transcript.jsonl"
    prefix = records("claude", 2)
    write(path, prefix)
    root = tmp_path / "db"
    child = IntakeProcess(root, path, "claude")
    try:
        child.command("pass")
        saved = child.command("position")
        child.command("notification_failure")
        suffix = records("claude", 3)[-1:]
        suffix[0]["effort"] = "low"
        suffix[0]["message"]["model"] = "claude-opus-5-5"
        write(path, prefix + suffix)
        result = child.command("pass")
        assert result["injected"] == ["notification"]
        assert result["checkpoint_hold"] == "routing_notification"
        assert result["failures"] == 0
        assert child.command("position") == saved
    finally:
        child.close()


def test_oversized_continuation_falls_back_without_trimming(tmp_path):
    path = tmp_path / "transcript.jsonl"
    prefix = records("codex", 2)
    write(path, prefix)
    root = tmp_path / "db"
    child = IntakeProcess(root, path, "codex")
    try:
        child.command("pass")
        assert child.command("position") is not None
        turns = [{"type": "turn_context", "payload": {"turn_id": str(i), "model": "m" * 100}} for i in range(3000)]
        write(path, prefix + turns)
        result = child.command("pass")
        assert result["failures"] == 0
        assert result["checkpoint_hold"] == "state_too_large"
        assert child.command("position") is None
    finally:
        child.close()
    child = IntakeProcess(root, path, "codex")
    try:
        result = child.command("pass")
        assert result["resume_reason"] == "missing"
        assert prefix_bindings(result) == 2
        assert len(child.command("snapshot")["continuation"]["provenance"]["turn_models"]) == 3000
    finally:
        child.close()


def test_capped_provenance_equals_uninterrupted_with_later_model_context(tmp_path):
    from test_usage_accounting import codex as codex_usage
    path = tmp_path / "transcript.jsonl"
    prefix = records("codex", 1)
    prefix.insert(1, {"type": "turn_context", "payload": {"model": "model-before"}})
    prefix.insert(2, codex_usage())
    write(path, prefix)
    root = tmp_path / "candidate"
    reference = IntakeProcess(tmp_path / "reference", path, "codex")
    candidate = IntakeProcess(root, path, "codex")
    try:
        reference.command("pass")
        candidate.command("pass")
        suffix = [records("codex", 2)[-1], records("codex", 3)[-1],
                  {"type": "token_usage_record", "timestamp": "2026-10-01T00:00:01Z",
                   "payload": {"response_id": "boundary-response", "usage": {
                       "input_tokens": 100, "cached_input_tokens": 50, "cache_write_input_tokens": 0,
                       "output_tokens": 30, "reasoning_output_tokens": 10}}},
                  {"type": "turn_context", "payload": {"model": "model-after"}},
                  records("codex", 4)[-1]]
        write(path, prefix + suffix)
        assert reference.command("pass", limit=1)["appended"] == 1
        assert candidate.command("pass", limit=1)["appended"] == 1
        candidate.close()
        candidate = None
        reference.command("rebuild_projections")
        reference.command("pass")
        candidate = IntakeProcess(root, path, "codex")
        candidate.command("pass")
        expected, actual = reference.command("snapshot"), candidate.command("snapshot")
        assert stable(actual) == stable(expected), first_difference(stable(actual), stable(expected))
        for result in (expected, actual):
            assert result["accounting"]["v2_usage_codex_thread_flags"] == []
            response = next(row for row in result["accounting"]["v2_usage_codex_responses"]
                            if row["response_id"] == "boundary-response")
            assert response["model"] == "model-before"
    finally:
        reference.close()
        if candidate is not None:
            candidate.close()


def test_claude_cap_without_offset_progress_restores_initial_context(tmp_path):
    path = tmp_path / "transcript.jsonl"
    write(path, records("claude", 3))
    child = IntakeProcess(tmp_path / "db", path, "claude")
    try:
        result = child.command("pass", limit=1)
        assert result["appended"] == 1
        assert result["offset"] == 0
        assert result["writes"] == []
        assert child.command("snapshot")["continuation"]["provenance"] == {}
        assert sum(n for name, n in result["calls"].items() if ".record_usage_checked." in name) == 1
        child.command("pass")
        assert child.command("snapshot")["continuation"]["provenance"]["_bound"] == [str(path), "native"]
    finally:
        child.close()


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("method,returns_none", [
    ("record_usage", True),
    ("fetch_open_session_lifecycle", True),
    ("append_session_events_lifecycle_cas", False),
])
def test_no_progress_restores_context_on_early_return_or_failure(tmp_path, provider, method, returns_none):
    path = tmp_path / "transcript.jsonl"
    items = records(provider, 3)
    if provider == "codex":
        items.insert(1, {"type": "turn_context", "payload": {"model": "unconsumed-model", "turn_id": "unconsumed-turn"}})
    write(path, items)
    child = IntakeProcess(tmp_path / "db", path, provider)
    try:
        child.command("fail_once", owner="store", method=method, return_none=returns_none)
        result = child.command("pass")
        assert result["injected"] == [method]
        assert result["offset"] == 0
        assert result["writes"] == []
        assert child.command("snapshot")["continuation"]["provenance"] == {}
    finally:
        child.close()

