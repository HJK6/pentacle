"""Daily enrollment and restart invariants on an isolated source tree."""
from datetime import datetime
from dataclasses import replace
from pathlib import Path
import asyncio
import json
import re

import pytest

from tools import daily_retro as retro


def source(root, name, body="Lesson.", day="2026-09-27", status="completed"):
    path = root / "work" / status / name / "spec.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nid: spec_{name}\ntype: spec\nstatus: {status}\ncompleted_at: '{day}'\n---\n\n## Retro\n{body}\n")
    return path


@pytest.fixture
def config(tmp_path):
    memory = tmp_path / "memory"
    for folder in ("completed", "deprecated"):
        (memory / "work" / folder).mkdir(parents=True)
    (tmp_path / "token").write_text("fixture")
    return retro.Settings(memory, tmp_path / "state", "ws://127.0.0.1:12345",
                          tmp_path / "token", "fixture", isolated=True)


def at(text="2026-09-28T05:00:00-05:00"):
    return datetime.fromisoformat(text)


def test_first_enrollment_retry_late_and_changed(config):
    old = source(config.memory_root, "old", day="2026-09-20")
    source(config.memory_root, "today")
    first = retro.collect(config, at())
    assert [s["id"] for s in first["sources"]] == ["spec_today"]
    assert first["baseline"]["spec_old"]["reviewed"] is False
    # Manifest, rather than the derived index, is the restart authority.
    (config.state_root / "index.json").unlink()
    assert retro.collect(config, at()) == first
    source(config.memory_root, "late", day="2026-09-19")
    old.write_text(old.read_text().replace("Lesson.", "Revised lesson."))
    following = retro.collect(config, at("2026-09-29T05:00:00-05:00"))
    assert {s["id"] for s in following["sources"]} == {"spec_late", "spec_old"}


@pytest.mark.parametrize("status", ["completed", "deprecated"])
def test_first_enrollment_uses_current_terminal_status_date(config, status):
    current_day, older_day = "2026-09-27", "2026-07-08"
    path = source(config.memory_root, "terminal", day=current_day if status == "completed" else older_day,
                  status=status)
    deprecated_day = current_day if status == "deprecated" else older_day
    path.write_text(path.read_text().replace("\n---\n\n## Retro", f"\ndeprecated_at: '{deprecated_day}'\n---\n\n## Retro"))
    manifest = retro.collect(config, at())
    assert [s["id"] for s in manifest["sources"]] == ["spec_terminal"]
    assert manifest["sources"][0]["terminal_date"] == current_day
    assert not manifest["baseline"]


def test_move_unrelated_edit_missing_and_overflow(config):
    path = source(config.memory_root, "one")
    missing = source(config.memory_root, "missing", body="")
    first = retro.collect(config, at(), max_sources=1)
    moved = config.memory_root / "work/deprecated/moved/spec.md"
    moved.parent.mkdir(parents=True)
    moved.write_text(path.read_text().replace("status: completed", "status: deprecated") + "\n## Notes\nUnrelated.\n")
    path.unlink()
    missing.write_text(missing.read_text().replace("## Retro\n", "## Retro\nRecovered lesson."))
    second = retro.collect(config, at("2026-09-29T05:00:00-05:00"), max_sources=1)
    assert [s["id"] for s in second["sources"]] == ["spec_missing"]
    assert first["gaps"] and not second["gaps"]
    for n in range(3):
        source(config.memory_root, f"more{n}")
    third = retro.collect(config, at("2026-09-30T05:00:00-05:00"), max_sources=1)
    assert len(third["sources"]) == 1 and len(third["deferred"]) == 2
    fourth = retro.collect(config, at("2026-10-01T05:00:00-05:00"))
    assert len(fourth["sources"]) == 2


@pytest.mark.parametrize("stamp,hours", [("2026-03-09T05:00:00-05:00", 23),
                                         ("2026-11-02T05:00:00-06:00", 25)])
def test_local_calendar_dst(config, stamp, hours):
    manifest = retro.collect(config, at(stamp))
    start, end = (datetime.fromisoformat(manifest["window"][k]) for k in ("start", "end"))
    assert (end - start).total_seconds() == hours * 3600


def test_timer_guard_and_duplicate_ids(config):
    assert not retro.timer_due(at("2026-09-28T04:59:59-05:00"))
    assert retro.timer_due(at())
    source(config.memory_root, "a")
    b = source(config.memory_root, "b")
    b.write_text(b.read_text().replace("spec_b", "spec_a"))
    manifest = retro.collect(config, at())
    assert manifest["gaps"] and not manifest["sources"]


def history_fixture(config, count=43, body="Historical lesson."):
    for n in range(count):
        source(config.memory_root, f"old{n:03}", body=body, day="2026-08-01")
    source(config.memory_root, "recent")
    daily = retro.collect(config, at())
    assert [s["id"] for s in daily["sources"]] == ["spec_recent"]
    baseline = config.state_root / "runs" / daily["run_id"] / "collection.json"
    return replace(config, state_root=config.state_root.parent / "history-state"), baseline


def test_history_all_originals_bounded_and_stable(config):
    history, baseline = history_fixture(config)
    daily_before = {p: p.read_bytes() for p in config.state_root.rglob("*.json")}
    first = retro.history_collect(history, baseline, 1)
    second = retro.history_collect(history, baseline, 2)
    assert len(first["sources"]) == 40 and len(second["sources"]) == 3
    assert {s["id"] for s in first["sources"] + second["sources"]} == {
        f"spec_old{n:03}" for n in range(43)}
    assert first["phase"] == "history" and first["baseline"] == {} and first["window"] is None
    saved = (history.state_root / "runs" / first["run_id"] / "collection.json").read_bytes()
    assert retro.history_collect(history, baseline, 1) == first
    assert (history.state_root / "runs" / first["run_id"] / "collection.json").read_bytes() == saved
    assert history.namespace != config.namespace
    assert {p: p.read_bytes() for p in config.state_root.rglob("*.json")} == daily_before
    inventory = retro.read(history.state_root / "history.json")
    assert inventory["baseline_sha256"] == retro.hashlib.sha256(baseline.read_bytes()).hexdigest()
    assert len(inventory["sources"]) == 43


def test_history_byte_bound_preserves_full_original(config):
    history, baseline = history_fixture(config, count=3, body="é" * 16000)
    batches = [retro.history_collect(history, baseline, n) for n in (1, 2)]
    assert [len(m["sources"]) for m in batches] == [2, 1]
    for manifest in batches:
        assert sum(len(s["original"].encode()) for s in manifest["sources"]) <= 65536
        assert all(s["original"] == "## Retro\n" + "é" * 16000 for s in manifest["sources"])


@pytest.mark.parametrize("damage", ["missing", "changed", "malformed", "oversized"])
def test_history_refuses_partial_or_changed_intake(config, damage):
    history, baseline = history_fixture(config, count=2, body="x" * (65536 if damage == "oversized" else 10))
    path = config.memory_root / "work/completed/old000/spec.md"
    if damage == "missing":
        path.unlink()
    elif damage == "changed":
        path.write_text(path.read_text() + "changed original")
    elif damage == "malformed":
        path.write_text(path.read_text().replace("## Retro", "## Notes"))
    with pytest.raises(ValueError):
        retro.history_collect(history, baseline, 1)
    assert not (history.state_root / "history.json").exists()
    assert not list((history.state_root / "runs").glob("*/collection.json"))


def test_history_baseline_conflict_and_namespace_refusal(config):
    history, baseline = history_fixture(config, count=1)
    retro.history_collect(history, baseline, 1)
    baseline.write_text(baseline.read_text() + "\n")
    with pytest.raises(ValueError, match="baseline"):
        retro.history_collect(history, baseline, 1)
    for state in (config.state_root, config.state_root / "history", config.state_root.parent):
        with pytest.raises(ValueError, match="separate"):
            retro.history_collect(replace(history, state_root=state), baseline, 1)


@pytest.mark.parametrize("copy_index", [False, True])
def test_history_copied_baseline_cannot_admit_daily_state(config, copy_index):
    history, baseline = history_fixture(config, count=1)
    copied = config.state_root.parent / "not-daily/runs/2026-09-28/collection.json"
    copied.parent.mkdir(parents=True)
    copied.write_bytes(baseline.read_bytes())
    if copy_index:
        (copied.parents[2] / "index.json").write_bytes((config.state_root / "index.json").read_bytes())
    before = {p: p.read_bytes() for p in config.state_root.rglob("*.json")}
    with pytest.raises(ValueError):
        retro.history_collect(replace(history, state_root=config.state_root), copied, 1)
    assert {p: p.read_bytes() for p in config.state_root.rglob("*.json")} == before
    if not copy_index:
        with pytest.raises(ValueError, match="baseline"):
            retro.history_collect(history, copied, 1)


@pytest.mark.parametrize("entry", ["collect", "run"])
def test_history_namespace_refusal_creates_no_paths(config, entry):
    history, baseline = history_fixture(config, count=1)
    def inventory():
        return {str(p.relative_to(config.state_root)): None if p.is_dir() else p.read_bytes()
                for p in config.state_root.rglob("*")}
    before = inventory()
    unsafe = replace(history, state_root=config.state_root / "phase2/state")
    with pytest.raises(ValueError, match="separate"):
        if entry == "collect":
            retro.history_collect(unsafe, baseline, 1)
        else:
            asyncio.run(retro.Pipeline(unsafe, Transport()).history_run(baseline, 1))
    assert inventory() == before


@pytest.mark.parametrize("batch", [0, -1, 2, "../escape", True])
def test_history_batch_range_and_traversal(config, batch):
    history, baseline = history_fixture(config, count=1)
    with pytest.raises(ValueError):
        retro.history_collect(history, baseline, batch)


@pytest.mark.parametrize("damage", ["original", "phase", "digest", "order", "batch"])
def test_history_edited_manifest_refused_before_admission(config, damage):
    history, baseline = history_fixture(config, count=2)
    manifest = retro.history_collect(history, baseline, 1)
    if damage == "original":
        manifest["sources"][0]["original"] = "Invented original"
    elif damage == "phase":
        manifest["phase"] = "daily"
    elif damage == "digest":
        manifest["history"]["inventory_digest"] = "stale"
    elif damage == "order":
        manifest["sources"].reverse()
    else:
        manifest["history"]["batch"] = 2
    retro.atomic(history.state_root / "runs" / manifest["run_id"] / "collection.json", manifest)
    rpc = Transport()
    with pytest.raises(ValueError, match="manifest"):
        asyncio.run(retro.Pipeline(history, rpc).history_run(baseline, 1))
    assert not rpc.spawns and not rpc.sent and not rpc.closed


def test_history_review_replay_uses_only_requested_batch(config, monkeypatch):
    history, baseline = history_fixture(config)
    first = retro.history_collect(history, baseline, 1)
    second = retro.history_collect(history, baseline, 2)
    rpc = Transport()
    pipeline = retro.Pipeline(history, rpc)
    assert asyncio.run(pipeline.history_run(baseline, 1)) == {"delivered": [first["run_id"]]}
    monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:reviewer")
    final = retro.read(history.state_root / "runs" / first["run_id"] / "astra.json")
    review = {"packet_hash": final["packet_hash"], "dispositions": []}
    asyncio.run(pipeline.record_review(first["run_id"], review))
    assert asyncio.run(pipeline.history_run(baseline, 1)) == {"delivered": []}
    assert len(rpc.spawns) == 2 and len(rpc.sent) == 1 and len(rpc.closed) == 2
    assert not (history.state_root / "runs" / second["run_id"] / "sol.json").exists()
    with pytest.raises(ValueError, match="invalid run ID"):
        asyncio.run(pipeline.record_review("history-../escape", review))


class Transport:
    """Faultable RPC counterpart; independent socket tests use the real daemon."""
    def __init__(self):
        self.spawns, self.sent, self.closed, self.questions = {}, {}, [], {}
        self.binding = {"stream_id": "fixture:reviewer", "session_generation": "g1"}
        self.interrupt_send = False
        self.interrupt_ask = False

    async def assistant_once(self, config, payload):
        return {"type": "assistant.binding.ok", **self.binding}

    async def inspect_stream_once(self, config, actor):
        return {"type": "inspect_stream.ok", "session": {**self.binding, "visibility": "visible", "status": "open"}}

    async def spawn_once(self, config, payload):
        key = payload["request_id"]
        self.spawns.setdefault(key, payload)
        return {"type": "spawn.ok", "stream_id": "fixture:" + key,
                "session": {"session_generation": key}}

    async def await_report_once(self, config, stream, msg_id, **kwargs):
        payload = self.spawns[stream.split(":", 1)[1]]
        path = re.search(r"Read immutable input JSON (.*?);", payload["initial_prompt"])[1]
        inp = json.loads(Path(path).read_text())
        manifest = inp["collection"]
        packet = {"run_id": manifest["run_id"], "dispositions": [
            {"id": s["id"], "fingerprint": s["fingerprint"], "reason": "explicit no useful change"}
            for s in manifest["sources"]], "candidates": []}
        return {"type": "await_report.ok", "ok": True, "result_kind": "report",
                "report": {"report_id": inp["report_id"], "effective_model": payload["model"],
                           "effective_effort": payload["effort"], "extras": {"daily_retro": packet}}}

    async def close_once(self, config, stream, **kwargs):
        self.closed.append((stream, kwargs["expected_generation"]))
        return {"type": "close.ok", "session": {"status": "closed", "session_generation": kwargs["expected_generation"]}}

    async def send_receipt_once(self, config, target, key):
        return {"type": "send.receipt.get.ok", "receipts": [self.sent[key]] if key in self.sent else []}

    async def send_once(self, config, payload):
        self.sent[payload["request_id"]] = {"delivery": "landed", "payload": payload}
        if self.interrupt_send:
            self.interrupt_send = False
            raise ConnectionError("lost response after durable send")
        return self.sent[payload["request_id"]]

    async def prompt_status_once(self, config, qid):
        if qid not in self.questions:
            return {"type": "prompt.error", "error_code": "question_not_found"}
        return {"type": "prompt.status.ok", "question": self.questions[qid]}

    async def prompt_ask_once(self, config, payload):
        qid = payload["envelope"]["question_id"]
        self.questions.setdefault(qid, {"state": "open", "question_id": qid})
        if self.interrupt_ask:
            self.interrupt_ask = False
            raise ConnectionError("lost ask reply")
        return {"type": "prompt.ask.ok", "question_id": qid}

    async def prompt_cancel_once(self, config, payload):
        self.questions[payload["question_id"]]["state"] = "cancelled"
        return {"type": "prompt.cancel.ok"}


def proposal():
    return {"id": "fix-one", "scope": "Repair the fixture output", "citations": ["spec_one"],
            "title": "Approve the fixture repair?", "body": "The fixture failed. Repair its output with a bounded change.",
            "options": [{"label": "Approve", "value": "approve"}, {"label": "Reject", "value": "reject"}],
            "owner": "fixture:owner", "checkpoint": "2026-09-29", "success_measure": "output matches fixture"}


def test_rehearsal_and_daily_worker_identities_are_separate(config):
    from dataclasses import replace
    from spawnctl import SpawnCtl
    from store import Store

    async def run():
        store = Store(str(config.state_root.parent / "admission.db"))
        store.start()
        try:
            class Admission(Transport):
                async def spawn_once(self, rpc_config, payload):
                    claim = await store.atomic_claim_or_replay(
                        config.host, f"worker-{len(self.spawns)}", idempotency_key=payload["idempotency_key"],
                        request_payload_hash=SpawnCtl._spawn_payload_hash(payload), ttl_s=60,
                        request_id=payload["request_id"], nonce="fixture", owner_instance_id="fixture")
                    assert claim["status"] == "claimed", claim["status"]
                    return await super().spawn_once(rpc_config, payload)

            rpc = Admission()
            daily = replace(config, state_root=config.state_root.parent / "daily-state")
            first = await retro.Pipeline(config, rpc).worker(retro.collect(config, at()), "sol")
            second = await retro.Pipeline(daily, rpc).worker(retro.collect(daily, at()), "sol")
            assert len(rpc.spawns) == 2 and first["report_id"] != second["report_id"]
            assert await retro.Pipeline(config, rpc).worker(retro.collect(config, at()), "sol") == first
            assert len(rpc.spawns) == 2
        finally:
            store.stop()
    asyncio.run(run())


def test_delivery_identities_are_separate_between_state_roots(config):
    from dataclasses import replace

    async def run():
        rpc = Transport()
        daily = replace(config, state_root=config.state_root.parent / "daily-state")
        for number, settings in enumerate((config, daily)):
            manifest = retro.collect(settings, at())
            final = {"report": {"report_id": f"packet-{number}"}, "packet_hash": f"hash-{number}"}
            await retro.Pipeline(settings, rpc).deliver(manifest, final)
            await retro.Pipeline(settings, rpc).deliver(manifest, final)
        assert len(rpc.sent) == 2
        assert {f"report_id=packet-{n}" in row["payload"]["text"] for n, row in enumerate(rpc.sent.values())} == {True}
    asyncio.run(run())


def test_worker_and_delivery_restart(config, monkeypatch):
    async def run():
        source(config.memory_root, "one", body="No lessons.")
        rpc = Transport()
        rpc.interrupt_send = True
        pipeline = retro.Pipeline(config, rpc)
        with pytest.raises(ConnectionError):
            await pipeline.run(at())
        assert len(rpc.spawns) == 2 and len(rpc.closed) == 2
        await pipeline.run(at())
        # One real packet delivery and one retained failure notice, no replay paste.
        assert len(rpc.spawns) == 2 and len(rpc.sent) == 2
        assert all(generation == stream.split(":", 1)[1] for stream, generation in rpc.closed)
        final = retro.read(config.state_root / "runs/2026-09-28/astra.json")
        monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:reviewer")
        result = {"packet_hash": final["packet_hash"], "dispositions": []}
        review = await pipeline.record_review("2026-09-28", result)
        assert await pipeline.record_review("2026-09-28", result) == review
        await pipeline.run(at())
        assert not rpc.questions and len(rpc.sent) == 2
    asyncio.run(run())


@pytest.mark.parametrize("quiet", [False, True])
def test_worker_survives_rpc_wait_cap_without_another_admission(config, monkeypatch, quiet):
    clock = [0.0]
    monkeypatch.setattr(retro, "monotonic", lambda: clock[0], raising=False)
    class CappedWait(Transport):
        def __init__(self):
            super().__init__()
            self.calls = []
        async def await_report_once(self, config, stream, msg_id, **kwargs):
            self.calls.append((stream, msg_id, kwargs["timeout"]))
            if len(self.calls) <= 2:
                clock[0] += 900
                return {"type": "await_report.timeout", "ok": False, "stream_id": stream,
                        "msg_id": msg_id, "reason": "await_timeout"}
            return await super().await_report_once(config, stream, msg_id, **kwargs)
    async def run():
        rpc = CappedWait()
        if quiet:
            settings, baseline = history_fixture(config, count=1)
            assert (await retro.Pipeline(settings, rpc).history_run(baseline, 1, no_deliver=True))["prepared"]
        else:
            source(config.memory_root, "one")
            assert (await retro.Pipeline(config, rpc).run(at()))["delivered"]
        assert len(rpc.spawns) == len(rpc.closed) == 2
        assert len(rpc.calls) == 4
        assert len({stream for stream, _, _ in rpc.calls[:3]}) == 1
        assert all(msg == 0 and 0 < timeout <= 900 for _, msg, timeout in rpc.calls)
        assert len(rpc.sent) == (0 if quiet else 1)
    asyncio.run(run())


def test_worker_report_deadline_bounds_repeated_timeout_and_cleanup(config, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(retro, "monotonic", lambda: clock[0], raising=False)
    class NeverReports(Transport):
        def __init__(self):
            super().__init__()
            self.calls = []
        async def await_report_once(self, config, stream, msg_id, **kwargs):
            self.calls.append((stream, kwargs["timeout"]))
            clock[0] += min(kwargs["timeout"], 900) + 1
            return {"type": "await_report.timeout", "ok": False, "stream_id": stream,
                    "msg_id": msg_id, "reason": "await_timeout"}
    async def run():
        settings, baseline = history_fixture(config, count=1)
        rpc = NeverReports()
        with pytest.raises(RuntimeError, match="worker report deadline exceeded"):
            await retro.Pipeline(settings, rpc).history_run(baseline, 1, no_deliver=True)
        assert [timeout for _, timeout in rpc.calls] == [900, 900, 900, 897]
        assert len({stream for stream, _ in rpc.calls}) == 1
        assert len(rpc.spawns) == len(rpc.closed) == 1 and not rpc.sent
        assert clock[0] == 3601
    asyncio.run(run())


@pytest.mark.parametrize("reply_type,reply_stream", [("await_report.error", None), ("await_report.timeout", "fixture:foreign")])
def test_worker_retries_only_owned_timeout(config, monkeypatch, reply_type, reply_stream):
    monkeypatch.setattr(retro, "monotonic", lambda: 0, raising=False)
    class InvalidWait(Transport):
        calls = 0
        async def await_report_once(self, config, stream, msg_id, **kwargs):
            self.calls += 1
            assert self.calls == 1
            return {"type": reply_type, "ok": False, "stream_id": reply_stream or stream,
                    "msg_id": msg_id, "reason": "fixture_failure"}
    async def run():
        settings, baseline = history_fixture(config, count=1)
        rpc = InvalidWait()
        with pytest.raises(RuntimeError):
            await retro.Pipeline(settings, rpc).history_run(baseline, 1, no_deliver=True)
        assert rpc.calls == len(rpc.spawns) == len(rpc.closed) == 1 and not rpc.sent
    asyncio.run(run())


def test_worker_total_deadline_includes_stalled_transport(config, monkeypatch):
    ticks = iter([0, 3599.99])
    monkeypatch.setattr(retro, "monotonic", lambda: next(ticks))
    class StalledWait(Transport):
        async def await_report_once(self, config, stream, msg_id, **kwargs):
            await asyncio.sleep(1)
            pytest.fail("transport outlived the total report deadline")
    async def run():
        settings, baseline = history_fixture(config, count=1)
        rpc = StalledWait()
        with pytest.raises(RuntimeError, match="worker report deadline exceeded"):
            await retro.Pipeline(settings, rpc).history_run(baseline, 1, no_deliver=True)
        assert len(rpc.spawns) == len(rpc.closed) == 1 and not rpc.sent
    asyncio.run(run())


def test_pending_generation_and_stale_answer(config, monkeypatch):
    async def run():
        source(config.memory_root, "one")
        rpc = Transport()
        pipeline = retro.Pipeline(config, rpc)
        monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:reviewer")
        rpc.interrupt_ask = True
        with pytest.raises(ConnectionError):
            await pipeline.decision("spec_one", proposal())
        original = await pipeline.decision("spec_one", proposal())
        assert len(rpc.questions) == 1
        first = original["attempts"][0]["question_id"]
        rpc.binding["session_generation"] = "g2"
        # Hot rebind retains the old live question.
        await pipeline.decision("spec_one", proposal())
        assert len(rpc.questions) == 1
        rpc.questions[first]["state"] = "expired"
        replaced = await pipeline.decision("spec_one", proposal())
        assert len(rpc.questions) == 2 and sum(q["state"] == "open" for q in rpc.questions.values()) == 1
        second = replaced["attempts"][-1]["question_id"]
        rpc.questions[second].update(state="answered", answer={"selections": ["approve"]})
        rpc.binding["session_generation"] = "g3"
        answered = await pipeline.decision("spec_one", proposal())
        assert answered["answer"]["selections"] == ["approve"] and len(rpc.questions) == 2
        changed = proposal(); changed["scope"] = "A materially larger repair"
        current = await pipeline.decision("spec_one", changed)
        assert current["state"] == "pending" and "answer" not in current
        assert current["attempts"][1]["stale_answer_refused"]
        assert len(rpc.questions) == 3
    asyncio.run(run())


def test_packet_omission_and_endpoint_safety(config, tmp_path):
    source(config.memory_root, "one")
    manifest = retro.collect(config, at())
    with pytest.raises(ValueError, match="every original"):
        retro.validate_packet({"run_id": manifest["run_id"], "dispositions": [], "candidates": []}, manifest)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"memory_root": str(config.memory_root), "state_root": str(config.state_root),
                                     "token_path": str(config.token_path), "timezone": retro.ZONE.key,
                                     "host": "fixture", "ws_url": "ws://127.0.0.1:7791", "isolated": True}))
    with pytest.raises(ValueError, match="owned local"):
        retro.Settings.load(config_path)


def test_partial_scan_recovers_without_baseline_loss(config, monkeypatch):
    late = source(config.memory_root, "unread", day="2026-09-19")
    parser = retro.parse_frontmatter

    def unread(path):
        if path == late:
            raise PermissionError("fixture unreadable")
        return parser(path)

    monkeypatch.setattr(retro, "parse_frontmatter", unread)
    first = retro.collect(config, at())
    assert first["gaps"] and not first["baseline"]
    monkeypatch.setattr(retro, "parse_frontmatter", parser)
    recovered = retro.collect(config, at("2026-09-30T05:00:00-05:00"))
    assert [s["id"] for s in recovered["sources"]] == ["spec_unread"]
    assert not recovered["gaps"]


def test_worker_crash_report_retention_and_stale_cleanup(config):
    async def run():
        source(config.memory_root, "one")
        rpc = Transport()
        real_await = rpc.await_report_once
        failed = False

        async def crash(conf, stream, msg, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                return {"result_kind": "closed_without_report"}
            return await real_await(conf, stream, msg, **kwargs)

        rpc.await_report_once = crash
        pipeline = retro.Pipeline(config, rpc)
        with pytest.raises(RuntimeError, match="confirmed failed"):
            await pipeline.run(at())
        await pipeline.run(at())
        assert len(rpc.spawns) == 3  # Only confirmed failed Sol gets a recovery seat.
        assert retro.read(config.state_root / "runs/2026-09-28/sol-attempts.json")[0]["failed"]
        path = config.state_root / "runs/2026-09-28/astra.json"
        stage = retro.read(path); stage["closed"] = False; retro.atomic(path, stage)

        async def replaced(*args, **kwargs):
            return {"type": "close.already_closed", "session": {"status": "open", "session_generation": "replacement"}}

        rpc.close_once = replaced
        with pytest.raises(RuntimeError, match="cleanup blocked"):
            await pipeline.cleanup(retro.read(path.parent / "collection.json"))
        assert not retro.read(path)["closed"]
    asyncio.run(run())


def test_authorized_work_and_review_require_durable_version(config, monkeypatch):
    async def run():
        source(config.memory_root, "one")
        monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:reviewer")
        rpc = Transport()
        pipeline = retro.Pipeline(config, rpc)
        authorized = {k: v for k, v in proposal().items() if k not in {"title", "body", "options"}}
        authorized.update(disposition="authorized", authority="Existing fixture work grant, unchanged exact scope.")
        record = await pipeline.decision("spec_one", authorized)
        assert record["state"] == "authorized" and not rpc.questions
        assert await pipeline.decision("spec_one", authorized) == record
        manifest = retro.collect(config, at())
        candidate = {k: "fixture" for k in ("id", "problem", "consequence", "prior_occurrences", "existing", "action", "benefit", "effort", "risk", "uncertainty", "owner", "decision")}
        candidate.update(id="c-one", citations=["spec_one"])
        packet = {"run_id": manifest["run_id"], "dispositions": [{"id": s["id"], "fingerprint": s["fingerprint"], "reason": "Existing authorized work"} for s in manifest["sources"]], "candidates": [candidate]}
        retro.validate_packet(packet, manifest)
        root = config.state_root / "runs" / manifest["run_id"]
        retro.atomic(root / "astra.json", {"packet": packet, "packet_hash": retro.digest(packet)})
        result = {"packet_hash": retro.digest(packet), "dispositions": [{"id": "c-one", "disposition": "authorized", "reason": "Route evidence within standing scope", "work_id": "spec_one", "proposal_id": authorized["id"], "version": "wrong"}]}
        with pytest.raises(ValueError, match="durable proposal"):
            await pipeline.record_review(manifest["run_id"], result)
        result["dispositions"][0]["version"] = record["version"]
        receipt = await pipeline.record_review(manifest["run_id"], result)
        assert receipt["result"] == result and receipt["collection_to_decision_ready_seconds"] >= 0
        assert not rpc.questions
    asyncio.run(run())


def test_decision_lock_stays_in_private_state(config, monkeypatch):
    async def run():
        path = source(config.memory_root, "one")
        monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:reviewer")
        rpc = Transport()
        authorized = {**proposal(), "disposition": "authorized", "authority": "Existing fixture grant"}
        pipeline = retro.Pipeline(config, rpc)
        before = {p for p in config.memory_root.rglob("*")}
        record = await pipeline.decision("spec_one", authorized)
        assert record["state"] == "authorized" and not rpc.questions
        assert {p for p in config.memory_root.rglob("*")} == before
        assert (config.state_root / "locks/spec_one.lock").is_file()
        assert retro.proposals(path.read_text())[authorized["id"]]["version"] == record["version"]
        with retro.locked(config.state_root / "locks/spec_one.lock"):
            with pytest.raises(BlockingIOError):
                await pipeline.decision("spec_one", authorized)
        assert await pipeline.decision("spec_one", authorized) == record
    asyncio.run(run())


def test_decision_spec_compare_and_swap_preserves_concurrent_edit(config, monkeypatch):
    async def run():
        path = source(config.memory_root, "one")
        monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:reviewer")
        original_save = retro.save_proposals
        concurrent = path.read_bytes() + b"\n## Concurrent note\nKeep this edit.\n"
        def moved(work, preimage, records):
            work.write_bytes(concurrent)
            return original_save(work, preimage, records)
        monkeypatch.setattr(retro, "save_proposals", moved)
        authorized = {**proposal(), "disposition": "authorized", "authority": "Existing fixture grant"}
        rpc = Transport()
        with pytest.raises(RuntimeError, match="preimage moved"):
            await retro.Pipeline(config, rpc).decision("spec_one", authorized)
        assert path.read_bytes() == concurrent and not rpc.questions
    asyncio.run(run())


def sol_shaped_packet():
    """Citation/evidence structure from the first real analytical rehearsal."""
    candidate = {k: 'bounded fixture finding' for k in ('id', 'problem', 'consequence', 'prior_occurrences', 'existing', 'action', 'benefit', 'effort', 'risk', 'uncertainty', 'owner', 'decision')}
    candidate.update(id='credential_in_report', citations=['spec_fixture_serious', 'fixture_report_tool', 'verification_synthetic_report', 'spec_fixture_existing_owner'])
    packet = {'run_id': '2026-09-28', 'dispositions': [{'id': 'spec_fixture_serious', 'fingerprint': 'original-fingerprint', 'reason': 'Read and locally reproduced.'}], 'candidates': [candidate],
              'evidence_sources': {'fixture_report_tool': {'path': 'fixture_tools/report.py', 'finding': 'packet returns credential field'},
                                   'verification_synthetic_report': {'method': 'synthetic marker probe', 'result': 'marker appears in packet'},
                                   'spec_fixture_existing_owner': {'path': 'work/in_progress/existing-owner/spec.md', 'finding': 'existing accepting owner'}}}
    manifest = {'run_id': packet['run_id'], 'sources': [{'id': 'spec_fixture_serious', 'fingerprint': 'original-fingerprint'}]}
    return packet, manifest


@pytest.mark.parametrize("stray_label", [None, "unverified_probe_label"])
def test_sol_supplemental_evidence_preserves_original_coverage_and_raw_report(stray_label):
    packet, manifest = sol_shaped_packet()
    if stray_label:
        packet["candidates"][0]["citations"].append(stray_label)
    original = json.loads(json.dumps(packet))
    normalized = retro.validate_packet(packet, manifest)
    candidate = normalized['candidates'][0]
    assert candidate['citations'] == ['spec_fixture_serious']
    assert candidate['evidence_citations'] == original['candidates'][0]['citations'][1:]
    assert normalized['normalization_notes']
    assert normalized['evidence_sources'] == original['evidence_sources']
    assert packet == original
    assert retro.validate_packet(normalized, manifest) == normalized


@pytest.mark.parametrize('citations', [[], ['fixture_report_tool'], ['spec_fabricated'], ['spec_fixture_serious', 'spec_fabricated']])
def test_sol_missing_or_fabricated_originals_are_rejected(citations):
    packet, manifest = sol_shaped_packet()
    packet['candidates'][0]['citations'] = citations
    with pytest.raises(ValueError):
        retro.validate_packet(packet, manifest)


class HistoryTransport(Transport):
    def __init__(self, reasons=None, keep_final=True):
        super().__init__()
        self.reasons = ['no_owning_work'] if reasons is None else reasons
        self.keep_final = keep_final
        self.report_calls = 0
        self.fail = False

    async def await_report_once(self, config, stream, msg_id, **kwargs):
        self.report_calls += 1
        if self.fail:
            raise ConnectionError('fixture worker unavailable')
        payload = self.spawns[stream.split(':', 1)[1]]
        path = re.search(r'Read immutable input JSON (.*?);', payload['initial_prompt'])[1]
        inp = retro.read(Path(path))
        manifest = inp['collection']
        if manifest.get('phase') == 'history-final':
            catalogue = manifest['consolidation']['catalogue']
            selections = []
            for row in catalogue:
                candidate = json.loads(json.dumps(row['candidate']))
                candidate['bart_attention'] = {'reasons': ['uncertain'] if self.keep_final else [],
                                              'rationale': 'current verified relevance' if self.keep_final else 'verified retired subject'}
                selections.append({'alias_id': row['alias_id'], 'disposition': 'keep' if self.keep_final else 'drop',
                                   'reason': 'current evidence inspected', 'candidate': candidate})
            packet = {'run_id': manifest['run_id'], 'input_digest': retro.digest(catalogue), 'selections': selections}
        else:
            refs = [s['id'] for s in manifest['sources']]
            c = {'id': 'same-issue', 'problem': 'Shared failure', 'consequence': 'Work is lost',
                 'citations': refs, 'prior_occurrences': 'known historical issue',
                 'existing': 'spec_existing remains an unassigned triage backlog',
                 'action': 'Assign the existing bounded repair', 'benefit': 'preserved work',
                 'effort': 'small', 'risk': 'small', 'uncertainty': 'explicit', 'owner': 'unassigned',
                 'decision': 'new bounded grant required', 'finding_key': 'shared-failure',
                 'bart_attention': {'reasons': self.reasons, 'rationale': 'current state verified'}}
            packet = {'run_id': manifest['run_id'], 'dispositions': [
                {'id': s['id'], 'fingerprint': s['fingerprint'], 'reason': 'explicit issue disposition'}
                for s in manifest['sources']], 'candidates': [c]}
        return {'type': 'await_report.ok', 'ok': True, 'result_kind': 'report',
                'report': {'report_id': inp['report_id'], 'effective_model': payload['model'],
                           'effective_effort': payload['effort'], 'extras': {'daily_retro': packet}}}


def prepared_history(config, count=43, reasons=None):
    history, baseline = history_fixture(config, count=count)
    rpc = HistoryTransport(reasons)
    pipeline = retro.Pipeline(history, rpc)
    inventory = retro.history_collect(history, baseline, 1)
    for n in range(1, inventory['history']['batches'] + 1):
        assert asyncio.run(pipeline.history_run(baseline, n, no_deliver=True))['delivered'] == []
    return history, baseline, rpc, pipeline


def test_history_quiet_pair_context_and_replay(config):
    history, baseline, rpc, pipeline = prepared_history(config)
    inv, done = retro.completed_history(history, baseline)
    assert len(rpc.spawns) == 4 and len(rpc.closed) == 4 and not rpc.sent
    second = Path(done[2]['root'])
    context = retro.read(second / 'history-context.json')
    assert context['findings'][0]['alias']['run_id'] == done[1]['manifest']['run_id']
    assert context['inputs'][0]['packet_hash'] == done[1]['final']['packet_hash']
    assert 'skip renewed detailed investigation' in next(iter(rpc.spawns.values()))['initial_prompt']
    assert asyncio.run(pipeline.history_run(baseline, 2, no_deliver=True))['prepared']
    assert len(rpc.spawns) == 4
    with pytest.raises(ValueError, match='mode conflict'):
        asyncio.run(pipeline.history_run(baseline, 2))
    assert not rpc.sent


@pytest.mark.parametrize('reason', sorted(retro.ATTENTION))
def test_checkpoint_linked_work_attention_survives_and_counts(config, reason):
    history, baseline, rpc, pipeline = prepared_history(config, reasons=[reason])
    before = {p: p.read_bytes() for p in (history.state_root / 'runs').glob('history-*/astra.json')}
    result = asyncio.run(pipeline.history_consolidate(baseline, [2, 1]))
    assert len(rpc.sent) == 1
    packet = retro.read(history.state_root / 'runs' / result['run_id'] / 'astra.json')['packet']
    assert packet['counts']['originals'] == 43
    assert packet['counts']['candidate_occurrences'] == 2
    assert packet['counts']['surfaced_occurrences'] == 2
    assert packet['counts']['surfaced_candidates'] == (2 if reason in retro.CHANGED else 1)
    assert all(c['bart_attention']['reasons'] == [reason] for c in packet['candidates'])
    assert set(ref for c in packet['candidates'] for ref in c['citations']) == {s['id'] for s in retro.read(history.state_root/'history.json')['sources']}
    assert asyncio.run(pipeline.history_consolidate(baseline, [1, 2])) == result
    assert len(rpc.sent) == 1
    assert {p: p.read_bytes() for p in before} == before
    with pytest.raises(ValueError, match='overlapping'):
        asyncio.run(pipeline.history_consolidate(baseline, [1]))


def test_quiet_checkpoint_final_empty_once_and_final_replay(config):
    history, baseline, rpc, pipeline = prepared_history(config, reasons=[])
    result = asyncio.run(pipeline.history_consolidate(baseline, [1, 2]))
    assert result['quiet'] and not rpc.sent
    assert asyncio.run(pipeline.history_consolidate(baseline, [2, 1])) == result
    rpc.keep_final = False
    final = asyncio.run(pipeline.history_consolidate(baseline, final=True))
    assert not final['quiet'] and final['counts']['surfaced_candidates'] == 0
    assert len(rpc.spawns) == 5 and len(rpc.closed) == 5 and len(rpc.sent) == 1
    assert asyncio.run(pipeline.history_consolidate(baseline, final=True)) == final
    assert len(rpc.spawns) == 5 and len(rpc.sent) == 1


def test_checkpoint_only_new_versions_and_final_comprehensive(config):
    history, baseline, rpc, pipeline = prepared_history(config)
    first = asyncio.run(pipeline.history_consolidate(baseline, [1]))
    second = asyncio.run(pipeline.history_consolidate(baseline, [2]))
    assert first['counts']['surfaced_candidates'] == 1 and second['quiet']
    assert len(rpc.sent) == 1
    final = asyncio.run(pipeline.history_consolidate(baseline, final=True))
    assert final['counts']['candidate_occurrences'] == 2 and final['counts']['surfaced_candidates'] == 1
    assert len(rpc.spawns) == 5 and len(rpc.sent) == 2


@pytest.mark.parametrize('damage', ['hash', 'report', 'model', 'cleanup', 'manifest', 'raw_report'])
def test_consolidation_refuses_edited_inputs_before_mutation(config, damage):
    history, baseline, rpc, pipeline = prepared_history(config, count=1)
    inv = retro.read(history.state_root/'history.json')
    root = history.state_root/'runs'/f"history-{inv['inventory_digest'][:16]}-0001"
    p = root/'astra.json'
    stage = retro.read(p)
    if damage == 'hash': stage['packet_hash'] = 'wrong'
    elif damage == 'report': stage['report']['report_id'] = 'wrong'
    elif damage == 'model': stage['report']['effective_model'] = 'gpt-6-luna'
    elif damage == 'cleanup': stage['cleanup']['session']['session_generation'] = 'wrong'
    elif damage == 'raw_report':
        stage['packet']['candidates'][0]['action'] = 'invented action'
        stage['packet_hash'] = retro.digest(stage['packet'])
    else:
        p = root/'collection.json'; stage = retro.read(p); stage['sources'][0]['fingerprint'] = 'wrong'
    retro.atomic(p, stage)
    before = {p: p.read_bytes() for p in history.state_root.rglob('*') if p.is_file()}
    with pytest.raises(ValueError):
        asyncio.run(pipeline.history_consolidate(baseline, [1]))
    assert before == {p: p.read_bytes() for p in history.state_root.rglob('*') if p.is_file()}
    assert len(rpc.spawns) == 2 and not rpc.sent


@pytest.mark.parametrize('batches', [[], [1, 1], [0], [99], [1, 2, 3, 4, 5, 6], ['1'], [True]])
def test_consolidation_membership_bounds(config, batches):
    history, baseline, rpc, pipeline = prepared_history(config, count=1)
    with pytest.raises(ValueError):
        asyncio.run(pipeline.history_consolidate(baseline, batches))
    assert not rpc.sent and len(rpc.spawns) == 2


def test_serial_skip_and_incomplete_final_refusal(config):
    history, baseline = history_fixture(config)
    retro.history_collect(history, baseline, 1)
    rpc = HistoryTransport()
    pipeline = retro.Pipeline(history, rpc)
    with pytest.raises(ValueError, match='preceding'):
        asyncio.run(pipeline.history_run(baseline, 2, no_deliver=True))
    assert not rpc.spawns
    asyncio.run(pipeline.history_run(baseline, 1, no_deliver=True))
    with pytest.raises(ValueError, match='complete history'):
        asyncio.run(pipeline.history_consolidate(baseline, final=True))
    assert len(rpc.spawns) == 2 and not rpc.sent


def test_no_deliver_failure_has_no_notice(config):
    history, baseline = history_fixture(config, count=1)
    rpc = HistoryTransport(); rpc.fail = True
    with pytest.raises(ConnectionError):
        asyncio.run(retro.Pipeline(history, rpc).history_run(baseline, 1, no_deliver=True))
    assert not rpc.sent and len(rpc.spawns) == 1


def test_checkpoint_crash_replacement_review_replay(config, monkeypatch):
    history, baseline, rpc, pipeline = prepared_history(config, count=1)
    rpc.interrupt_send = True
    with pytest.raises(ConnectionError):
        asyncio.run(pipeline.history_consolidate(baseline, [1]))
    result = asyncio.run(pipeline.history_consolidate(baseline, [1]))
    assert len(rpc.sent) == 1
    rpc.binding = {'stream_id': 'fixture:replacement', 'session_generation': 'g2'}
    assert asyncio.run(pipeline.history_consolidate(baseline, [1])) == result
    assert len(rpc.sent) == 2
    monkeypatch.setenv('PENTACLE_STREAM_ID', 'fixture:reviewer')
    with pytest.raises(RuntimeError, match='CURRENT'):
        asyncio.run(pipeline.record_review(result['run_id'], {}))
    monkeypatch.setenv('PENTACLE_STREAM_ID', 'fixture:replacement')
    packet = retro.read(history.state_root/'runs'/result['run_id']/'astra.json')['packet']
    review = {'packet_hash': result['packet_hash'], 'dispositions': [
        {'id': c['id'], 'disposition': 'no_change', 'reason': 'verified fixture'} for c in packet['candidates']]}
    asyncio.run(pipeline.record_review(result['run_id'], review))
    rpc.binding = {'stream_id': 'fixture:third', 'session_generation': 'g3'}
    asyncio.run(pipeline.history_consolidate(baseline, [1]))
    assert len(rpc.sent) == 2 and len(rpc.spawns) == 2 and not rpc.questions


@pytest.mark.parametrize('damage', ['omit', 'invent', 'digest', 'citation', 'drop_relevant'])
def test_final_selection_validation(config, damage):
    history, baseline, rpc, pipeline = prepared_history(config, count=1)
    original = rpc.await_report_once
    async def damaged(*args, **kwargs):
        response = await original(*args, **kwargs)
        packet = response['report']['extras']['daily_retro']
        if 'selections' in packet:
            if damage == 'omit': packet['selections'] = []
            elif damage == 'invent': packet['selections'][0]['alias_id'] = 'invented'
            elif damage == 'digest': packet['input_digest'] = 'wrong'
            elif damage == 'citation': packet['selections'][0]['candidate']['citations'] = ['spec_fabricated']
            else: packet['selections'][0]['disposition'] = 'drop'
        return response
    rpc.await_report_once = damaged
    with pytest.raises(ValueError):
        asyncio.run(pipeline.history_consolidate(baseline, final=True))
    assert len(rpc.spawns) == 3 and len(rpc.closed) == 3 and not rpc.sent


def test_final_recovers_wrong_quiet_checkpoint(config):
    history, baseline, rpc, pipeline = prepared_history(config, count=1, reasons=[])
    assert asyncio.run(pipeline.history_consolidate(baseline, [1]))['quiet']
    result = asyncio.run(pipeline.history_consolidate(baseline, final=True))
    assert result['counts']['surfaced_candidates'] == 1 and len(rpc.sent) == 1
    assert len(rpc.spawns) == 3


@pytest.mark.parametrize('damage', ['context', 'compiled', 'catalogue'])
def test_consolidation_frozen_state_tampering(config, damage):
    history, baseline, rpc, pipeline = prepared_history(config, count=1)
    result = asyncio.run(pipeline.history_consolidate(baseline, [1]))
    root = history.state_root/'runs'/result['run_id']
    if damage == 'compiled':
        p=root/'astra.json'; value=retro.read(p); value['packet']['counts']['originals']=999
    else:
        p=root/'collection.json'; value=retro.read(p)
        if damage == 'context': value['consolidation']['context']['work_root']='invented'
        else: value['consolidation']['catalogue'][0]['candidate']['action']='invented'
    retro.atomic(p,value)
    with pytest.raises(ValueError):
        asyncio.run(pipeline.history_consolidate(baseline,[1]))
    assert len(rpc.sent)==1 and len(rpc.spawns)==2


def test_history_cumulative_context_tamper_refuses_replay(config):
    history, baseline, rpc, pipeline=prepared_history(config,count=1)
    inv=retro.read(history.state_root/'history.json')
    root=history.state_root/'runs'/f"history-{inv['inventory_digest'][:16]}-0001"
    p=root/'history-context.json'; value=retro.read(p);value['findings']=[{'invented':'issue'}];retro.atomic(p,value)
    with pytest.raises(ValueError,match='context'):
        asyncio.run(pipeline.history_run(baseline,1,no_deliver=True))
    assert len(rpc.spawns)==2 and not rpc.sent


def test_checkpoint_action_survives_final_without_recommission(config,monkeypatch):
    history,baseline,rpc,pipeline=prepared_history(config,count=1)
    checkpoint=asyncio.run(pipeline.history_consolidate(baseline,[1]))
    p=history.state_root/'runs'/checkpoint['run_id']/'astra.json'; packet=retro.read(p)['packet']
    monkeypatch.setenv('PENTACLE_STREAM_ID','fixture:reviewer')
    review={'packet_hash':checkpoint['packet_hash'],'dispositions':[
        {'id':c['id'],'disposition':'no_change','reason':'exact fixture previously reviewed'} for c in packet['candidates']]}
    asyncio.run(pipeline.record_review(checkpoint['run_id'],review))
    final=asyncio.run(pipeline.history_consolidate(baseline,final=True))
    packet=retro.read(history.state_root/'runs'/final['run_id']/'astra.json')['packet']
    assert packet['candidates'][0]['prior_reviews']==review['dispositions']
    assert 'do not recommission' in list(rpc.sent.values())[-1]['payload']['text']


def test_final_compact_alias_selection_preserves_candidate(config):
    history,baseline,rpc,pipeline=prepared_history(config,count=1)
    original=rpc.await_report_once
    async def compact(*args,**kwargs):
        response=await original(*args,**kwargs)
        packet=response['report']['extras']['daily_retro']
        if 'selections' in packet:
            for row in packet['selections']:
                row['bart_attention']=row.pop('candidate')['bart_attention']
        return response
    rpc.await_report_once=compact
    result=asyncio.run(pipeline.history_consolidate(baseline,final=True))
    assert result['counts']['surfaced_candidates']==1 and len(rpc.spawns)==3


def test_history_invalid_annotation_closes_owned_generation_without_retry(config):
    history,baseline=history_fixture(config,count=1)
    rpc=HistoryTransport(['invented-reason']);pipeline=retro.Pipeline(history,rpc)
    with pytest.raises(ValueError,match='annotation'):
        asyncio.run(pipeline.history_run(baseline,1,no_deliver=True))
    assert len(rpc.spawns)==1 and len(rpc.closed)==1 and not rpc.sent
    with pytest.raises(RuntimeError,match='parent ruling'):
        asyncio.run(pipeline.history_run(baseline,1,no_deliver=True))
    assert len(rpc.spawns)==1


@pytest.mark.parametrize('changed_field', ['owner','action','decision','existing','uncertainty','prior_occurrences'])
def test_same_finding_key_does_not_dedup_substantive_change(config,changed_field):
    history,baseline,rpc,pipeline=prepared_history(config)
    catalogue=retro.history_catalogue(retro.completed_history(history,baseline)[1])
    a=catalogue[0];b=json.loads(json.dumps(a))
    b['candidate'][changed_field]='changed current evidence'
    assert retro.finding(b['candidate'],b['alias'])['version'] != a['version']


def test_pending_checkpoint_delivery_blocks_next_group(config):
    history,baseline,rpc,pipeline=prepared_history(config)
    original=rpc.send_once
    async def pending(config,payload):
        return {'state':'queued'}
    rpc.send_once=pending
    with pytest.raises(RuntimeError,match='pending'):
        asyncio.run(pipeline.history_consolidate(baseline,[1]))
    with pytest.raises(RuntimeError,match='unresolved'):
        asyncio.run(pipeline.history_consolidate(baseline,[2]))
    rpc.send_once=original
    asyncio.run(pipeline.history_consolidate(baseline,[1]))
    assert asyncio.run(pipeline.history_consolidate(baseline,[2]))['quiet']
    assert len(rpc.spawns)==4


def test_final_cleanup_failure_blocks_delivery_then_replay_cleans(config):
    history,baseline,rpc,pipeline=prepared_history(config,count=1)
    original=rpc.close_once
    async def fail_final(config,stream,**kwargs):
        if 'final-astra' in stream:
            return {'type':'close.error'}
        return await original(config,stream,**kwargs)
    rpc.close_once=fail_final
    with pytest.raises(RuntimeError,match='cleanup'):
        asyncio.run(pipeline.history_consolidate(baseline,final=True))
    assert len(rpc.spawns)==3 and not rpc.sent
    rpc.close_once=original
    result=asyncio.run(pipeline.history_consolidate(baseline,final=True))
    assert result['counts']['surfaced_candidates']==1 and len(rpc.spawns)==3 and len(rpc.sent)==1


def test_compiled_packet_and_hash_cannot_replace_frozen_inputs(config):
    history,baseline,rpc,pipeline=prepared_history(config,count=1)
    result=asyncio.run(pipeline.history_consolidate(baseline,[1]))
    p=history.state_root/'runs'/result['run_id']/'astra.json';stage=retro.read(p)
    stage['packet']['candidates'][0]['action']='invented';stage['packet_hash']=retro.digest(stage['packet']);retro.atomic(p,stage)
    with pytest.raises(ValueError,match='immutable inputs'):
        asyncio.run(pipeline.history_consolidate(baseline,[1]))
    assert len(rpc.sent)==1
