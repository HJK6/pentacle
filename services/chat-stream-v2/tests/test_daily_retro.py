"""Daily enrollment and restart invariants on an isolated source tree."""
from datetime import datetime
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
