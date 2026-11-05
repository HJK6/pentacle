"""Native workers must not displace their pane's primary chat transcript."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import satellite
from codex_rollout_norm import normalize_codex_rollout_records_grouped
from store import Store
from test_satellite_recovery import HOST, NATIVE, TARGET, _Wire, _sink

NAME = "v2-primary"
PID = 7001


def header(identity=NATIVE, *, worker=False, guardian=False, unknown=False):
    payload = {"id": identity, "source": "cli", "thread_source": "user"}
    if worker:
        payload.update(thread_source="subagent", source={"subagent": {"thread_spawn": {
            "parent_thread_id": NATIVE, "depth": 1, "agent_path": "/root/helper"}}})
    if guardian:
        payload.update(thread_source="guardian_review", source={"subagent": {"other": "guardian"}})
    if unknown:
        payload.update(thread_source="subagent", source={"subagent": {"other": "unknown-kind"}})
    return {"type": "session_meta", "payload": payload}


def message(text):
    return {"type": "response_item", "timestamp": "2026-11-01T01:00:00Z",
            "payload": {"type": "message", "id": text, "role": "assistant",
                        "content": [{"type": "output_text", "text": text}]}}


def append(path, *records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        for record in records:
            stream.write(json.dumps(record) + "\n")


def descriptors(monkeypatch, paths):
    """Replace only OS discovery; all header reads and collection remain real."""
    monkeypatch.setattr(satellite, "_descendant_pids", lambda pid: [pid])
    monkeypatch.setattr(satellite, "_pane_cmdlines", lambda pids: {PID: "codex"})
    monkeypatch.setattr(satellite.Satellite, "_capture_pane", lambda *args: "Codex")

    def run(command, **kwargs):
        if command[0] == "lsof":
            output = "".join(f"n{path}\n" for path in paths)
        elif command[:2] == ["tmux", "list-panes"]:
            output = f"{NAME}\t{PID}\n"
        elif command[0] == "git":
            output = TARGET
        else:
            raise AssertionError(f"unexpected subprocess: {command}")
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(satellite.subprocess, "run", run)


@pytest.mark.parametrize("case", ["single", "guardian", "workers", "workers-reversed",
                                 "worker-only", "two-primaries", "unknown", "foreign"])
def test_primary_selection_uses_headers_and_preserves_ambiguity(tmp_path, monkeypatch, case):
    root = tmp_path / ".codex" / "sessions"
    primary, child, grandchild = [root / (name + ".jsonl") for name in ("primary", "child", "grandchild")]
    append(primary, header(), message('body mentions source.subagent.thread_spawn'))
    append(child, header("child", worker=True), message("worker text"))
    nested = header("grandchild", worker=True)
    nested["payload"]["source"]["subagent"]["thread_spawn"].update(parent_thread_id="child", depth=2)
    append(grandchild, nested)
    paths, expected = [primary], str(primary)
    if case == "guardian":
        review = root / "review.jsonl"; append(review, header("review", guardian=True)); paths.append(review)
    elif case.startswith("workers"):
        paths += [child, grandchild]
        if case.endswith("reversed"):
            paths.reverse()
    elif case == "worker-only":
        paths, expected = [child, grandchild], ""
    elif case in {"two-primaries", "unknown"}:
        other = root / "other.jsonl"; append(other, header("other", unknown=case == "unknown"))
        paths.append(other); expected = ""
    elif case == "foreign":
        foreign = tmp_path / "another-account" / ".codex" / "sessions" / "foreign.jsonl"
        append(foreign, header("foreign")); paths.append(foreign)
    descriptors(monkeypatch, paths)
    assert satellite._codex_transcript_for_pid(PID, {"provider": "codex", "transcript_root": str(root)}) == expected


def test_native_worker_messages_remain_normalizable():
    events = [event for group in normalize_codex_rollout_records_grouped(
        [header("child", worker=True), message("worker report")], host=HOST, session_name=NAME)
              for event in group]
    assert [(event["kind"], event["text"]) for event in events] == [("ASSIST_TEXT", "worker report")]


@pytest.mark.parametrize("restart", [False, True], ids=["live-tail", "restart-readoption"])
def test_missing_suffix_resumes_once_then_uses_live_capture(tmp_path, monkeypatch, restart):
    async def run():
        primary = tmp_path / ".codex" / "sessions" / "primary.jsonl"
        append(primary, header(), message("prefix-1"), message("prefix-2"))
        store = Store(str(tmp_path / "sessions.db")); store.start()
        paths = [primary]
        descriptors(monkeypatch, paths)
        # This synthetic seat has no provisioned billing identity. Keep usage
        # admission out of this chat test; discovery, source ACKs and store
        # admission still execute, including their original identity guards.
        monkeypatch.setattr(satellite.Satellite, "_usage_candidate", lambda *args: None)

        def producer():
            sat = satellite.Satellite(satellite.SatelliteConfig(
                host=HOST, checkout=str(tmp_path), no_autoupdate=True,
                push_secret="shared", host_secret="host-secret", history_bytes=-1,
                chat_checkpoint_path=str(tmp_path / "chat.json"),
                held_span_path=str(tmp_path / "held.json"), max_events_per_pass=3))
            sat.sha = TARGET
            return sat

        try:
            await store.put("event_push.target_sha", TARGET)
            original = await store.open_session(HOST, NAME, provider="codex", pane_pid=str(PID),
                                                created_at="2026-11-01T00:00:00Z")
            casts = []; sink = _sink(store, casts); wire = _Wire(sink)
            fences = {f"{HOST}:{NAME}": (await sink._usage_fences(HOST))[0]}
            sat = producer(); sat._usage_fences = fences

            async def collect_push():
                events, hw, _ = sat._collect()
                ack = await sat._push(wire, events, hw)
                assert ack["type"] == "event.push.ok", ack
                sat._apply_ack(ack, hw)
                return events, ack

            _, initial_ack = await collect_push()
            initial_tail = sat._tails[NAME]
            binding = dict(initial_tail.binding)
            assert binding and initial_tail.offset == primary.stat().st_size, json.dumps(initial_ack)
            checkpoint = json.loads((tmp_path / "chat.json").read_text())
            assert checkpoint["sources"][0]["offset"] == initial_tail.offset

            child = primary.with_name("child.jsonl")
            append(child, header("child", worker=True), message("must never enter primary"))
            paths.append(child)
            suffix = [f"missing-{index}" for index in range(11)]
            append(primary, *(message(text) for text in suffix))
            if restart:
                sat = producer(); sat._usage_fences = fences
            # Baseline live discovery stalls. Baseline restart can drain through
            # retained recovery, but cannot readopt the source into live tails.
            for _ in range(12):
                await collect_push()
            history = await store.fetch_session_event_tail(f"{HOST}:{NAME}", limit=100)
            assert [event["text"] for event in history] == ["prefix-1", "prefix-2", *suffix]
            assert NAME in sat._tails, "recovery must readopt the original live source"
            assert sat._tails[NAME].binding == binding
            if not restart:
                assert sat._tails[NAME] is initial_tail
            assert not sat._recovering and not sat._pass_recovery
            assert sat._tails[NAME].offset == primary.stat().st_size

            append(primary, message("new-live-turn"))
            events, ack = await collect_push()
            assert [event["text"] for event in events] == ["new-live-turn"]
            assert sat._pass_sources and not sat._pass_recovery
            duplicate = await sat._push(wire, events, {str(primary): sat._tails[NAME].offset})
            assert duplicate["inserted"] == 0
            assert (await collect_push())[0] == []
            history = await store.fetch_session_event_tail(f"{HOST}:{NAME}", limit=100)
            assert [event["text"] for event in history] == ["prefix-1", "prefix-2", *suffix, "new-live-turn"]
            assert (await store.fetch_session(HOST, NAME))["session_generation"] == original["session_generation"]
        finally:
            store.stop()

    asyncio.run(run())
