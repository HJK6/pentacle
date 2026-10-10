"""Daily enrollment and restart invariants on an isolated source tree."""
from datetime import datetime
from dataclasses import replace
from pathlib import Path
import asyncio
import json
import re
import sqlite3

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


def continuous_source(config, name="bart", body="Observed delivery refusal.", window=True):
    path = source(config.memory_root, name, body=body, status="in_progress")
    text = path.read_text().replace("completed_at:", "tags: [continuous-retro]\ncompleted_at:")
    if window:
        text = text.replace("## Retro\n", '## Retro\n<!-- continuous-retro-window {"start":"2026-09-27T00:00:00-05:00","end":"2026-09-28T04:00:00-05:00"} -->\n')
    path.write_text(text)
    return path


def test_continuous_intake_changed_unchanged_status_move_and_unrelated_edit(config):
    path = continuous_source(config)
    source(config.memory_root, "unselected", status="in_progress")
    first = retro.collect(config, at())
    assert [s["id"] for s in first["sources"]] == ["spec_bart"]
    path.write_text(path.read_text().replace("Observed delivery refusal.", "Observed refusal corrected."))
    second = retro.collect(config, at("2026-09-29T05:00:00-05:00"))
    assert [s["id"] for s in second["sources"]] == ["spec_bart"]
    path.write_text(path.read_text().replace("status: in_progress", "status: completed") + "\n## Outside\nUnrelated note.")
    moved = config.memory_root / "work/completed/bart/spec.md"
    moved.parent.mkdir(parents=True)
    path.rename(moved)
    third = retro.collect(config, at("2026-09-30T05:00:00-05:00"))
    assert third["sources"] == []
    assert not third["baseline"]
    assert retro.collect(config, at("2026-09-30T08:00:00-05:00")) == third


@pytest.mark.parametrize("damage", ["missing", "stale", "future", "empty", "tag_substring", "wrong_status", "duplicate"])
def test_continuous_intake_limits_are_explicit(config, damage):
    path = continuous_source(config, window=damage != "missing", body="" if damage == "empty" else "Lesson")
    text = path.read_text()
    if damage == "stale":
        text = text.replace("2026-09-28T04", "2026-09-26T04").replace("2026-09-27T00", "2026-09-25T00")
    elif damage == "future":
        text = text.replace("2026-09-28T04", "2026-09-28T06")
    elif damage == "tag_substring":
        text = text.replace("[continuous-retro]", "[not-continuous-retro]")
    elif damage == "wrong_status":
        text = text.replace("status: in_progress", "status: blocked")
    elif damage == "duplicate":
        other = source(config.memory_root, "duplicate")
        other.write_text(other.read_text().replace("spec_duplicate", "spec_bart"))
    path.write_text(text)
    manifest = retro.collect(config, at())
    assert not manifest["sources"]
    if damage == "tag_substring":
        assert not manifest["gaps"]
    else:
        assert manifest["gaps"]
    assert not manifest["baseline"]


def test_continuous_overflow_never_enrolls_and_multiline_tag(config):
    path = continuous_source(config)
    path.write_text(path.read_text().replace("tags: [continuous-retro]", "tags:\n- continuous-retro\n- bart"))
    first = retro.collect(config, at(), max_bytes=1)
    assert first["coverage"]["deferred"] == 1 and not first["sources"]
    assert "spec_bart" not in retro.rebuild_index(config)["entries"]
    second = retro.collect(config, at("2026-09-29T05:00:00-05:00"))
    assert [s["id"] for s in second["sources"]] == ["spec_bart"]


def primary_fixture(config):
    from store import SESSIONS_DDL, REPORTS_DDL, SEND_RECEIPT_DDL
    from store_routing import OUTBOUND_NOTICE_DDL, ASSISTANT_COMPOSITE_PUBLICATIONS_DDL, ASSISTANT_COMPOSITE_LANES_DDL
    from store_assistant_binding import ASSISTANT_BINDING_DDL, ASSISTANT_REBIND_AUDIT_DDL
    from message_envelopes import build_message_envelope
    path, archive = config.state_root.parent / "primary.db", config.state_root.parent / "archive.db"
    with sqlite3.connect(path) as conn:
        for ddl in (SESSIONS_DDL, REPORTS_DDL, SEND_RECEIPT_DDL, OUTBOUND_NOTICE_DDL,
                    ASSISTANT_COMPOSITE_PUBLICATIONS_DDL, ASSISTANT_COMPOSITE_LANES_DDL, ASSISTANT_BINDING_DDL, ASSISTANT_REBIND_AUDIT_DDL):
            conn.execute(ddl)
        conn.execute("INSERT INTO v2_assistant_direct_binding VALUES ('bart','fixture:fd','g1',1,'2026-09-27T00:00:00Z')")
        for name, parent in (("fd", None), ("lead", "fixture:fd")):
            card = json.dumps({"updated_at": "2026-09-27T00:00:00Z", "plan": [{"status": "active", "text": "PRIVATE_SENTINEL"}]})
            conn.execute("INSERT INTO sessions(host,session_name,parent_stream_id,visibility,created_at,status,status_card) VALUES ('fixture',?,?, 'hidden','2026-09-27T00:00:00Z','open',?)", (name, parent, card))
        for identity, stamp in (("visible", "2026-09-27T12:00:00Z"), ("unknown", "2026-09-27T13:00:00Z"), ("future", "2026-09-29T12:00:00Z")):
            conn.execute("INSERT INTO v2_reports(report_id,from_stream_id,to_stream_id,status,summary,ingested_at,created_at) VALUES (?,'fixture:lead','fixture:fd','done','PRIVATE_SENTINEL',?,?)", (identity, stamp, retro.aware(stamp).timestamp()))
        conn.execute("INSERT INTO v2_assistant_composite_publications(publication_key,stream_id,payload_digest,canonical_payload_json,dispatch_id,publish_kind,evidence_refs_json,event_id,created_at) VALUES ('pub-visible','bart:assistant','h','{\"text\":\"PRIVATE_SENTINEL\"}','','result','[\"visible\"]',1,'2026-09-27T12:00:30Z')")
        conn.execute("INSERT INTO v2_send_receipts(to_stream_id,request_id,receipt_id,state,display_text,content_kind,delivery,created_at,from_stream_id) VALUES ('fixture:reviewer','request','receipt','not_landed','PRIVATE_SENTINEL','send','not_landed','2026-09-27T07:00:00Z','fixture:lead')")
        for n, stamp, age in ((1, "2026-09-27T07:40:00Z", 7000), (2, "2026-09-27T08:01:00Z", 7300), (3, "2026-09-27T08:31:00Z", 9100)):
            key = "d2:" + str(n) * 64
            body = build_message_envelope("lane_digest", notice_id=key, evaluated_at=stamp,
                lanes=[{"stream_id": "fixture:lead", "generation": "g2", "role": "lead", "working": False,
                        "idle_age_s": age, "open_terminal_reports": 0, "eta_at": None, "title": "PRIVATE_SENTINEL"}])
            conn.execute("INSERT INTO v2_outbound_notices(notice_id,kind,dedupe_key,recipient_stream_id,tell_id,body,payload_digest,created_at) VALUES (?,'lane_digest',?,'fixture:fd',?,?,'h',?)", (key, key, key, body, stamp))
    with sqlite3.connect(archive) as conn:
        conn.execute(SESSIONS_DDL)
        conn.execute("INSERT INTO sessions(host,session_name,visibility,created_at,status,closed_at) VALUES ('fixture','reviewer','hidden','2026-09-26T00:00:00Z','closed','2026-09-27T06:00:00Z')")
    return replace(config, primary_store=path, primary_archive=archive)


def test_primary_receipts_cutoff_safe_projection_readonly_and_first_digest(config):
    settings = primary_fixture(config)
    before = settings.primary_store.read_bytes(), settings.primary_archive.read_bytes()
    packet = retro.primary_evidence(settings, at("2026-09-27T00:00:00-05:00"), at())
    text = json.dumps(packet)
    assert "PRIVATE_SENTINEL" not in text and "future" not in text
    assert before == (settings.primary_store.read_bytes(), settings.primary_archive.read_bytes())
    completions = {o["report_id"]: o for o in packet["observations"] if o["kind"] == "completion"}
    assert completions["visible"]["delivery"] == "correlated" and completions["visible"]["completion_to_publication_seconds"] == 30
    assert completions["unknown"]["delivery"] == "unknown"
    failures = [o for o in packet["observations"] if o["kind"] == "delivery_failure"]
    assert failures[0]["recipient_closed_at"] == "2026-09-27T06:00:00Z"
    stalls = [o for o in packet["observations"] if o.get("classification") == "unexplained_idle"]
    assert len(stalls) == 1 and stalls[0]["created_at"] == "2026-09-27T08:01:00Z" and stalls[0]["next_action"]
    assert packet["coverage"]["reports"]["observed"] == 2


def test_primary_missing_archive_stores_overflow_and_wait_provenance(config):
    settings = primary_fixture(config)
    missing = retro.primary_evidence(replace(settings, primary_archive=None), at("2026-09-27T00:00:00-05:00"), at())
    assert missing["coverage"]["recipient_state"]["unknown"] == 1
    assert any(g["source"] == "primary_archive" for g in missing["gaps"])
    limited = retro.primary_evidence(settings, at("2026-09-27T00:00:00-05:00"), at(), max_rows=1)
    assert limited["coverage"]["reports"]["overflow"] == 1
    assert any(o["kind"] == "delivery_failure" for o in limited["observations"])
    with sqlite3.connect(settings.primary_store) as conn:
        conn.execute("INSERT INTO v2_assistant_composite_lanes(lane_id,stream_id,phase,bound_stream_id,bound_generation,version,created_at,updated_at) VALUES ('hold','bart:assistant','waiting','fixture:lead','g2',1,'2026-09-27T06:00:00Z','2026-09-27T06:00:00Z')")
    held = retro.primary_evidence(settings, at("2026-09-27T00:00:00-05:00"), at())
    assert any(o.get("classification") == "intentional_hold" and o["next_action"] is None for o in held["observations"])
    with sqlite3.connect(settings.primary_store) as conn:
        conn.execute("UPDATE v2_assistant_composite_lanes SET bound_generation='wrong'")
        conn.execute("DROP TABLE v2_assistant_composite_publications")
    unknown = retro.primary_evidence(settings, at("2026-09-27T00:00:00-05:00"), at())
    assert any(o.get("classification") == "unexplained_idle" for o in unknown["observations"])
    assert any(g["source"] == "publications" for g in unknown["gaps"])


def test_primary_root_provenance_excludes_spec_guesses_failed_and_future_rebinds(config):
    settings = primary_fixture(config)
    with sqlite3.connect(settings.primary_store) as conn:
        conn.execute("INSERT INTO sessions(host,session_name,visibility,created_at,spec_id) VALUES ('fixture','unrelated-qa','hidden','2026-09-27T00:00:00Z','portfolio-spec')")
        conn.execute("UPDATE sessions SET spec_id='portfolio-spec' WHERE session_name='fd'")
        for key, old, outcome, stamp in (("good", "former-root", "ok", "2026-09-27T06:00:00Z"),
                ("failed", "failed-root", "denied", "2026-09-27T06:00:00Z"), ("future", "future-root", "ok", "2026-09-29T06:00:00Z")):
            conn.execute("INSERT INTO v2_assistant_rebind_audit(request_id,payload_digest,actor_stream_id,actor_generation,old_binding_json,new_binding_json,outcome,created_at) VALUES (?,'h','fixture:fd','g1',?,?,?,?)",
                (key, json.dumps({"stream_id": "fixture:" + old, "generation": "g0"}), json.dumps({"stream_id": "fixture:fd", "generation": "g1"}), outcome, stamp))
        for key in ("former-root", "failed-root", "future-root", "unrelated-qa"):
            conn.execute("INSERT INTO v2_reports(report_id,from_stream_id,to_stream_id,status,ingested_at,created_at) VALUES (?,'fixture:worker',?,'done','2026-09-27T12:00:00Z',?)",
                (key, "fixture:" + key, retro.aware("2026-09-27T12:00:00Z").timestamp()))
    packet = retro.primary_evidence(settings, at("2026-09-27T00:00:00-05:00"), at())
    assert set(packet["roots"]) == {"fixture:fd", "fixture:former-root"}
    assert {o["report_id"] for o in packet["observations"] if o["kind"] == "completion"} == {"visible", "unknown", "former-root"}
    with sqlite3.connect(settings.primary_store) as conn:
        conn.execute("DROP TABLE v2_assistant_rebind_audit")
    limited = retro.primary_evidence(settings, at("2026-09-27T00:00:00-05:00"), at(), max_rows=1)
    assert limited["roots"] == ["fixture:fd"]
    assert any(g["source"] == "rebind_audit" for g in limited["gaps"])
    assert any(o["kind"] == "delivery_failure" for o in limited["observations"])


def test_primary_packet_bound_prioritizes_failure_and_discloses_urgent_overflow(config):
    settings = primary_fixture(config)
    with sqlite3.connect(settings.primary_store) as conn:
        for n in range(160):
            conn.execute("INSERT INTO v2_send_receipts(to_stream_id,request_id,receipt_id,state,content_kind,delivery,created_at,from_stream_id) VALUES ('fixture:reviewer',?,?,'not_landed','send','not_landed','2026-09-27T07:00:00Z','fixture:lead')", ("r" + str(n), "receipt" + str(n)))
    packet = retro.primary_evidence(settings, at("2026-09-27T00:00:00-05:00"), at())
    assert len(retro.encoded(packet)) <= 24576
    assert packet["observations"][0]["kind"] == "delivery_failure"
    assert packet["deferred"]["count"] > 0 and packet["coverage"]["observations"]["deferred_urgent"] > 0


def test_primary_suppression_is_not_failure_and_predating_publication_is_unknown(config):
    settings = primary_fixture(config)
    with sqlite3.connect(settings.primary_store) as conn:
        conn.execute("UPDATE v2_assistant_composite_publications SET created_at='2026-09-27T11:00:00Z'")
        for n, reason in enumerate(("persisted_suppressed", "folded_into_digest", "configured assistant authority changed")):
            conn.execute("INSERT INTO v2_outbound_notices(notice_id,kind,dedupe_key,recipient_stream_id,tell_id,body,payload_digest,created_at,terminal_at,terminal_reason,last_error) VALUES (?,'watch',?,'fixture:fd',?,'PRIVATE_SENTINEL','h','2026-09-27T09:00:00Z','2026-09-27T09:01:00Z',?,'PRIVATE_SENTINEL')",
                (f"suppression-{n}", f"suppression-{n}", f"suppression-{n}", reason))
    packet = retro.primary_evidence(settings, at("2026-09-27T00:00:00-05:00"), at())
    failures = {o["source"] for o in packet["observations"] if o["kind"] == "delivery_failure"}
    assert "v2_outbound_notices:suppression-0" not in failures
    assert "v2_outbound_notices:suppression-1" not in failures
    assert "v2_outbound_notices:suppression-2" in failures
    assert packet["coverage"]["expected_notice_suppression"]["scanned"] == 2
    assert packet["coverage"]["completion_correlation"] == {"observed_done": 2, "explicitly_published": 0, "unknown": 2}
    assert "PRIVATE_SENTINEL" not in json.dumps(packet)


def test_primary_latest_receipt_selection_precedes_projection_cap(config):
    settings = primary_fixture(config)
    with sqlite3.connect(settings.primary_store) as conn:
        conn.execute("INSERT INTO v2_send_receipts(to_stream_id,request_id,receipt_id,state,content_kind,delivery,created_at,from_stream_id) VALUES ('fixture:reviewer','request','landed','landed','send','delivered','2026-09-27T05:30:00Z','fixture:lead')")
        conn.execute("UPDATE v2_send_receipts SET created_at='2026-09-27T05:00:00Z' WHERE receipt_id='receipt'")
    packet = retro.primary_evidence(settings, at("2026-09-27T00:00:00Z"), at(), max_rows=1)
    assert packet["coverage"]["send_receipts"]["observed"] == 1
    assert not any(o["source"] == "v2_send_receipts:receipt" for o in packet["observations"])


@pytest.mark.parametrize("lane,kwargs,expected", [
    ({"working": True, "idle_age_s": 9999}, {}, "live_tool"),
    ({"working": False, "open_terminal_reports": 1}, {}, "terminal_report"),
    ({"working": True, "open_terminal_reports": 1}, {}, "terminal_report"),
    ({"working": False, "idle_age_s": 9999}, {"waiting": True}, "intentional_hold"),
    ({"working": False}, {"plan_statuses": ["done"]}, "terminal_plan"),
    ({"role": "planner", "working": False, "idle_age_s": 9999}, {}, "retained_planner"),
    ({"working": False, "eta_at": "2026-09-28T04:00:00-05:00"}, {}, "expired_checkpoint"),
    ({"working": None}, {}, "unknown"),
])
def test_primary_stall_classes(lane, kwargs, expected):
    assert retro.digest_action(lane, at().isoformat(), **kwargs)[0] == expected


def proposal2(disposition="defer"):
    return {**proposal(), "schema_version": 2, "disposition": disposition,
        "owner_acceptance": {"owner": "fixture:owner", "receipt": "accepted-commission:fixture", "accepted_at": "2026-09-28T10:00:00Z"},
        "checkpoint": {"owner": "fixture:owner", "at": "2026-09-29T10:00:00Z"},
        "authority": "fixture standing repair grant"}


def test_future_work_requires_honest_defer_helper_and_observed_outcome(config, monkeypatch):
    continuous_source(config, "one")
    monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:reviewer")
    pipeline = retro.Pipeline(config, Transport())
    manifest = retro.collect(config, at())
    root = config.state_root / "runs" / manifest["run_id"]
    retro.atomic(root / "astra.json", {"packet_hash": "frozen", "packet": {"schema_version": 2,
        "candidates": [{"id": "wanted-later", "recommendation_kind": "future_work"}]}})
    wrong = {"packet_hash": "frozen", "dispositions": [{"id": "wanted-later", "disposition": "no_change", "reason": "Unassigned backlog exists."}]}
    with pytest.raises(ValueError, match="suppressed"):
        asyncio.run(pipeline.record_review(manifest["run_id"], wrong))
    record = asyncio.run(pipeline.decision("spec_one", proposal2()))
    row = {"id": "wanted-later", "disposition": "defer", "reason": "Accepted owner defers until the dated checkpoint.",
        "work_id": "spec_one", "proposal_id": record["id"], "version": record["version"]}
    reviewed = asyncio.run(pipeline.record_review(manifest["run_id"], {"packet_hash": "frozen", "dispositions": [row]}))
    assert reviewed["weekly_summary"]["due"]
    approved = asyncio.run(pipeline.decision("spec_one", proposal2("authorized")))
    assert approved["state"] == "authorized" and not pipeline.rpc.questions
    resolved = {**proposal2("resolved"), "outcome_evidence": {"receipt": "observed-fixture-output", "observed_at": "2026-09-28T11:00:00Z", "measure": "Output matches fixture.", "shipped_at": "2026-09-28T11:00:00Z"}}
    done = asyncio.run(pipeline.decision("spec_one", resolved))
    assert done["state"] == "resolved"
    summary = retro.weekly_summary(config, "2026-09-28")
    assert summary["sources_reviewed"] == 1 and summary["candidates_reviewed"] == 1 and summary["dispositions"]["defer"] == 1
    assert summary["verified_outcomes"] == 1 and summary["shipped_observed"] == 1
    assert len(summary["missing_runs"]) == 6 and summary["legacy_review_runs"] == []


@pytest.mark.parametrize("damage", ["missing_acceptance", "wrong_owner", "vague_checkpoint", "event_no_ref", "naive_date"])
def test_defer_contract_refuses_unaccepted_or_uncheckable_work(config, monkeypatch, damage):
    source(config.memory_root, "one")
    monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:reviewer")
    payload = proposal2()
    if damage == "missing_acceptance": payload.pop("owner_acceptance")
    elif damage == "wrong_owner": payload["owner_acceptance"]["owner"] = "unassigned"
    elif damage == "vague_checkpoint": payload["checkpoint"] = "next owned touch"
    elif damage == "event_no_ref": payload["checkpoint"] = {"owner": "fixture:owner", "event": "next-release"}
    else: payload["checkpoint"]["at"] = "2026-09-29"
    with pytest.raises(ValueError):
        asyncio.run(retro.Pipeline(config, Transport()).decision("spec_one", payload))
    assert not retro.proposals((config.memory_root / "work/completed/one/spec.md").read_text())


def test_schema2_owner_checkpoint_change_refuses_old_approval(config, monkeypatch):
    source(config.memory_root, "one")
    monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:reviewer")
    rpc = Transport(); pipeline = retro.Pipeline(config, rpc)
    pending = asyncio.run(pipeline.decision("spec_one", proposal2("propose")))
    qid = pending["attempts"][0]["question_id"]
    rpc.questions[qid].update(state="answered", answer={"selections": ["approve"]})
    changed = proposal2("propose"); changed["checkpoint"]["at"] = "2026-09-30T10:00:00Z"
    current = asyncio.run(pipeline.decision("spec_one", changed))
    assert current["state"] == "pending" and "answer" not in current
    assert current["attempts"][0]["stale_answer_refused"]


def test_weekly_retention_is_once_per_week_and_quiet_daily_stays_quiet(config, monkeypatch):
    monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:reviewer")
    pipeline = retro.Pipeline(config, Transport())
    for day in ("2026-09-28", "2026-09-29"):
        manifest = retro.collect(config, at(day + "T05:00:00-05:00"))
        root = config.state_root / "runs" / day
        retro.atomic(root / "astra.json", {"packet_hash": day, "packet": {"schema_version": 2, "candidates": []}})
        receipt = asyncio.run(pipeline.record_review(day, {"packet_hash": day, "dispositions": []}))
        assert receipt["weekly_summary"]["due"] == (day == "2026-09-28")
    assert len(list((config.state_root / "weekly").glob("*.json"))) == 1
    assert not pipeline.rpc.sent and not pipeline.rpc.questions
    summary = retro.weekly_summary(config, "2026-09-29")
    assert summary["runs_collected"] == summary["runs_reviewed"] == 2
    assert summary["primary_coverage"]["gaps"] == 2


def test_weekly_oldest_unresolved_and_explicit_dot_measures(config):
    path = source(config.memory_root, "one")
    old = {**proposal2(), "version": "v1", "state": "defer",
        "checkpoint": {"owner": "fixture:owner", "event": "release-candidate", "trigger_ref": "accepted-release-commission"}}
    done = {**proposal2("resolved"), "id": "dot-result", "version": "v2", "state": "resolved",
        "outcome_evidence": {"receipt": "observed-milestone", "observed_at": "2026-09-28T12:00:00Z", "measure": "Milestone accepted.",
            "measurement_scope": "dot_milestone", "blocked_seconds": 0, "correction_rounds": 2, "shipped_at": "2026-09-28T12:00:00Z"}}
    retro.save_proposals(path, path.read_bytes(), {old["id"]: old, done["id"]: done})
    for identity, stamp in ((old["id"], "2026-09-01T12:00:00Z"), (done["id"], "2026-09-28T12:00:00Z")):
        retro.atomic(config.state_root / "decision-receipts" / (identity + ".json"), {
            "work_id": "spec_one", "proposal_id": identity, "recorded_at": stamp})
    summary = retro.weekly_summary(config, "2026-09-28")
    assert summary["oldest_unresolved"]["first_observed_at"] == "2026-09-01T12:00:00Z"
    assert summary["current_work_counts"]["defer"] == 1
    assert summary["dot_measurements"]["blocked_seconds"] == {"observed": 1, "total": 0}
    assert summary["dot_measurements"]["correction_rounds"] == {"observed": 1, "total": 2}
    assert summary["dot_measurements"]["avoidable_stops"] == {"observed": 0, "total": None}
    assert summary["verified_outcomes"] == summary["shipped_observed"] == 1
    assert next(w for w in summary["current_work"] if w["proposal_id"] == old["id"])["event_occurrence"] == "unknown; inspect trigger_ref"
    following = retro.weekly_summary(config, "2026-10-05")
    assert following["verified_outcomes"] == following["shipped_observed"] == 0


@pytest.mark.parametrize("mode", ["working", "waiting", "planner"])
def test_primary_expiry_crossing_emits_once_with_unchanged_lane_identity(config, mode):
    from message_envelopes import build_message_envelope
    settings = primary_fixture(config)
    with sqlite3.connect(settings.primary_store) as conn:
        conn.execute("DELETE FROM v2_outbound_notices")
        if mode == "waiting":
            conn.execute("INSERT INTO v2_assistant_composite_lanes(lane_id,stream_id,phase,bound_stream_id,bound_generation,version,created_at,updated_at) VALUES ('hold','bart:assistant','waiting','fixture:lead','g2',1,'2026-09-27T06:00:00Z','2026-09-27T06:00:00Z')")
        for n, stamp in enumerate(("2026-09-27T08:00:00Z", "2026-09-27T09:01:00Z", "2026-09-27T09:31:00Z")):
            key = "d2:" + str(n) * 64
            body = build_message_envelope("lane_digest", notice_id=key, evaluated_at=stamp,
                lanes=[{"stream_id": "fixture:lead", "generation": "g2", "role": "planner" if mode == "planner" else "lead",
                    "working": mode == "working", "idle_age_s": 0, "eta_at": "2026-09-27T09:00:00Z", "open_terminal_reports": 0}])
            conn.execute("INSERT INTO v2_outbound_notices(notice_id,kind,dedupe_key,recipient_stream_id,tell_id,body,payload_digest,created_at) VALUES (?,'lane_digest',?,'fixture:fd',?,?,'h',?)", (key, key, key, body, stamp))
    packet = retro.primary_evidence(settings, at("2026-09-27T00:00:00Z"), at())
    observations = [o for o in packet["observations"] if o["kind"] == "digest"]
    assert len(observations) == 2
    due = [o for o in observations if o["next_action"]]
    assert len(due) == 1 and due[0]["created_at"] == "2026-09-27T09:01:00Z"
    assert due[0]["next_action"] == "inspect_expired_checkpoint_and_record_reason_and_next_checkpoint"


def test_continuous_duplicate_retro_heading_is_gap(config):
    path = continuous_source(config)
    path.write_text(path.read_text() + "\n## Retro\nSecond lesson.\n")
    manifest = retro.collect(config, at())
    assert not manifest["sources"] and not manifest["baseline"]
    assert any("exactly one Retro" in gap["reason"] for gap in manifest["gaps"])


@pytest.mark.parametrize("fence", ["```markdown", "~~~markdown"])
def test_continuous_fenced_retro_example_is_not_a_section(config, fence):
    path = continuous_source(config)
    text = path.read_text()
    example = "\n## Example\n" + fence + "\n## Retro\nExample only.\n" + fence[:3] + "\n"
    path.write_text(text.replace("\n## Retro\n", example + "\n## Retro\n", 1))
    manifest = retro.collect(config, at())
    assert [s["id"] for s in manifest["sources"]] == ["spec_bart"]
    assert not manifest["gaps"] and "Example only" not in manifest["sources"][0]["original"]


@pytest.mark.parametrize("weekly_exists", [True, False])
def test_schema2_review_retry_persists_missing_weekly_pointer(config, monkeypatch, weekly_exists):
    monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:reviewer")
    pipeline = retro.Pipeline(config, Transport())
    manifest = retro.collect(config, at())
    root = config.state_root / "runs" / manifest["run_id"]
    retro.atomic(root / "astra.json", {"packet_hash": "frozen", "packet": {"schema_version": 2, "candidates": []}})
    result = {"packet_hash": "frozen", "dispositions": []}
    first = asyncio.run(pipeline.record_review(manifest["run_id"], result))
    first.pop("weekly_summary")
    retro.atomic(root / "review.json", first)
    if not weekly_exists:
        for path in (config.state_root / "weekly").glob("*.json"):
            path.unlink()
    recovered = asyncio.run(pipeline.record_review(manifest["run_id"], result))
    assert retro.read(root / "review.json") == recovered
    assert recovered["weekly_summary"]["sha256"] and recovered["weekly_summary"]["due"]
    assert {k: v for k, v in recovered.items() if k != "weekly_summary"} == first
    preimage = (root / "review.json").read_bytes()
    assert asyncio.run(pipeline.record_review(manifest["run_id"], result)) == recovered
    assert (root / "review.json").read_bytes() == preimage



def test_primary_wait_requires_configured_composite_and_retains_exact_provenance(config):
    settings = primary_fixture(config)
    with sqlite3.connect(settings.primary_store) as conn:
        conn.execute("INSERT INTO v2_assistant_composite_lanes(lane_id,stream_id,phase,bound_stream_id,bound_generation,version,created_at,updated_at) VALUES ('hold','other:assistant','waiting','fixture:lead','g2',7,'2026-09-27T06:00:00Z','2026-09-27T06:00:00Z')")
    wrong = retro.primary_evidence(settings, at("2026-09-27T00:00:00Z"), at())
    assert any(o.get("classification") == "unexplained_idle" for o in wrong["observations"])
    with sqlite3.connect(settings.primary_store) as conn:
        conn.execute("UPDATE v2_assistant_composite_lanes SET stream_id='bart:assistant'")
    held = retro.primary_evidence(settings, at("2026-09-27T00:00:00Z"), at())
    observation = next(o for o in held["observations"] if o.get("classification") == "intentional_hold")
    assert observation["waiting_provenance"] == [{"lane_id": "hold", "stream_id": "bart:assistant", "bound_stream_id": "fixture:lead", "bound_generation": "g2", "phase": "waiting", "version": 7, "updated_at": "2026-09-27T06:00:00Z"}]


@pytest.mark.parametrize("side", ["recipient", "source"])
def test_primary_descendant_notice_failure_survives_session_projection_cap(config, side):
    settings = primary_fixture(config)
    with sqlite3.connect(settings.primary_store) as conn:
        recipient, sender = ("fixture:lead", "fixture:unrelated") if side == "recipient" else ("fixture:unrelated", "fixture:lead")
        conn.execute("INSERT INTO v2_outbound_notices(notice_id,kind,dedupe_key,recipient_stream_id,source_stream_id,tell_id,body,payload_digest,created_at,last_error) VALUES ('descendant-failure','watch','descendant-failure',?,?,'descendant-failure','PRIVATE_SENTINEL','h','2026-09-27T09:00:00Z','PRIVATE_SENTINEL')", (recipient, sender))
    packet = retro.primary_evidence(settings, at("2026-09-27T00:00:00Z"), at(), max_rows=1)
    assert any(o["source"] == "v2_outbound_notices:descendant-failure" and o["kind"] == "delivery_failure" and o["next_action"] for o in packet["observations"])
    assert packet["coverage"]["notices"] == {"observed": 4, "scanned": 1, "overflow": 3}
    assert "PRIVATE_SENTINEL" not in json.dumps(packet)


def test_primary_archived_terminal_plan_metadata_is_safe_and_readonly(config):
    settings = primary_fixture(config)
    with sqlite3.connect(settings.primary_store) as conn:
        conn.execute("DELETE FROM sessions WHERE session_name='lead'")
    with sqlite3.connect(settings.primary_archive) as conn:
        card = json.dumps({"updated_at": "2026-09-27T06:00:00Z", "plan": [{"status": "done", "text": "PRIVATE_SENTINEL"}]})
        conn.execute("INSERT INTO sessions(host,session_name,parent_stream_id,visibility,created_at,status,closed_at,status_card) VALUES ('fixture','lead','fixture:fd','hidden','2026-09-27T00:00:00Z','closed','2026-09-27T07:00:00Z',?)", (card,))
    before = settings.primary_store.read_bytes(), settings.primary_archive.read_bytes()
    packet = retro.primary_evidence(settings, at("2026-09-27T00:00:00Z"), at())
    assert any(o.get("classification") == "terminal_plan" and o["plan_provenance"]["source"] == "primary_archive.sessions" for o in packet["observations"])
    assert not any(o.get("classification") == "unexplained_idle" for o in packet["observations"])
    assert before == (settings.primary_store.read_bytes(), settings.primary_archive.read_bytes())
    assert "PRIVATE_SENTINEL" not in json.dumps(packet)


def test_weekly_primary_retains_scan_sample_and_correlation_denominators(config, monkeypatch):
    monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:reviewer")
    pipeline = retro.Pipeline(config, Transport())
    for n, day in enumerate(("2026-09-28", "2026-09-29"), 1):
        manifest = retro.collect(config, at(day + "T05:00:00-05:00"))
        manifest["primary"] = {"store": "fixture", "gaps": [], "coverage": {
            "reports": {"observed": 2 * n + 3, "scanned": n + 2, "overflow": n + 1},
            "observations": {"observed": 10 * n, "selected": 4 * n, "urgent": 3 * n, "deferred_urgent": n},
            "completion_correlation": {"observed_done": 4 * n, "explicitly_published": n, "unknown": 3 * n}},
            "deferred": {"count": 6 * n, "by_kind": {"digest": 6 * n}}}
        root = config.state_root / "runs" / day
        retro.atomic(root / "collection.json", manifest)
        retro.atomic(root / "astra.json", {"packet_hash": day, "packet": {"schema_version": 2, "candidates": []}})
        asyncio.run(pipeline.record_review(day, {"packet_hash": day, "dispositions": []}))
    coverage = retro.weekly_summary(config, "2026-09-29")["primary_coverage"]
    assert coverage["tables"]["reports"] == {"observed": 12, "scanned": 7, "overflow": 5}
    assert coverage["observations"] == {"observed": 30, "selected": 12, "urgent": 9, "deferred_urgent": 3, "deferred": 18}
    assert coverage["completion_correlation"] == {"observed_done": 12, "explicitly_published": 3, "unknown": 9}
    assert coverage["deferred_by_kind"] == {"digest": 18}


@pytest.mark.parametrize("linked", [False, True])
def test_weekly_direct_resolved_outcome_is_counted_once_per_receipt(config, monkeypatch, linked):
    source(config.memory_root, "one")
    monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:reviewer")
    pipeline = retro.Pipeline(config, Transport())
    outcome = {"receipt": "observed-owning-milestone", "observed_at": "2026-09-28T11:00:00Z", "shipped_at": "2026-09-28T11:00:00Z", "measure": "Output confirmed.", "measurement_scope": "dot_milestone", "blocked_seconds": 0, "correction_rounds": 1}
    if linked:
        asyncio.run(pipeline.decision("spec_one", {**proposal2("resolved"), "outcome_evidence": outcome}))
    manifest = retro.collect(config, at())
    root = config.state_root / "runs" / manifest["run_id"]
    retro.atomic(root / "astra.json", {"packet_hash": "frozen", "packet": {"schema_version": 2, "candidates": [{"id": "observed", "recommendation_kind": "resolved"}]}})
    row = {"id": "observed", "disposition": "resolved", "reason": "Observed result.", "outcome_evidence": outcome}
    asyncio.run(pipeline.record_review(manifest["run_id"], {"packet_hash": "frozen", "dispositions": [row]}))
    summary = retro.weekly_summary(config, manifest["run_id"])
    assert summary["verified_outcomes"] == summary["shipped_observed"] == 1
    assert summary["dot_measurements"]["sample_receipts"] == [outcome["receipt"]]
    assert summary["dot_measurements"]["blocked_seconds"] == {"observed": 1, "total": 0}
    assert summary["dot_measurements"]["correction_rounds"] == {"observed": 1, "total": 1}


def test_primary_provenance_packet_bound_does_not_limit_root_membership(config):
    settings = primary_fixture(config)
    with sqlite3.connect(settings.primary_store) as conn:
        for n in range(300):
            conn.execute("INSERT INTO v2_assistant_rebind_audit(request_id,payload_digest,actor_stream_id,actor_generation,old_binding_json,new_binding_json,outcome,created_at) VALUES (?,'h','fixture:fd','g1',?,?,'ok','2026-09-27T06:00:00Z')", (str(n), json.dumps({"stream_id": "fixture:former-root-" + str(n), "generation": "g" * 64}), json.dumps({"stream_id": "fixture:next-root-" + str(n), "generation": "h" * 64})))
    packet = retro.primary_evidence(settings, at("2026-09-27T00:00:00Z"), at())
    assert len(retro.encoded(packet)) <= 24576
    assert packet["reference_coverage"]["roots"]["observed"] == 601
    assert packet["reference_coverage"]["roots"]["deferred"] > 0
    assert any(o["source"] == "v2_send_receipts:receipt" for o in packet["observations"])
    assert packet["coverage"]["rebind_audit"]["scanned"] == 300


@pytest.mark.parametrize("damage", ["future_observation", "future_shipment", "invalid_shipment"])
def test_outcome_requires_observed_times_before_counting_success(damage):
    from datetime import timedelta, timezone
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    outcome = {"receipt": "observed", "observed_at": "2026-09-28T11:00:00Z", "measure": "Output confirmed."}
    if damage == "future_observation":
        outcome["observed_at"] = future
    else:
        outcome["shipped_at"] = future if damage == "future_shipment" else "unknown"
    with pytest.raises(ValueError):
        retro.validate_outcome(outcome)


def test_changed_schema2_scope_does_not_inherit_old_observed_outcome(config, monkeypatch):
    monkeypatch.setattr(retro, "now_iso", lambda: "2026-10-05T12:00:00+00:00")
    source(config.memory_root, "one", status="in_progress")
    monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:reviewer")
    pipeline = retro.Pipeline(config, Transport())
    old = {**proposal2("resolved"), "outcome_evidence": {"receipt": "old-scope-result", "observed_at": "2026-09-28T11:00:00Z", "measure": "Old output confirmed."}}
    asyncio.run(pipeline.decision("spec_one", old))
    changed = {**proposal2("authorized"), "scope": "Repair a different current defect."}
    current = asyncio.run(pipeline.decision("spec_one", changed))
    assert "outcome_evidence" not in current
    summary = retro.weekly_summary(config, "2026-10-05")
    assert summary["oldest_unresolved"]["proposal_id"] == current["id"]
    assert summary["current_work"][0]["checkpoint_state"] == "overdue"


def test_existing_schema2_proposal_cannot_downgrade_to_legacy_on_replay(config, monkeypatch):
    path = source(config.memory_root, "one")
    monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:reviewer")
    pipeline = retro.Pipeline(config, Transport())
    asyncio.run(pipeline.decision("spec_one", proposal2()))
    preimage = path.read_bytes()
    legacy = {**proposal(), "disposition": "defer"}
    with pytest.raises(ValueError, match="schema_version 2"):
        asyncio.run(pipeline.decision("spec_one", legacy))
    assert path.read_bytes() == preimage


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
        continuous_source(config, "one")
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
        candidate.update(id="c-one", citations=["spec_one"], recommendation_kind="immediate_work")
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
        path = source(config.memory_root, "one", status="in_progress")
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
        path = source(config.memory_root, "one", status="in_progress")
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
    def __init__(self, reasons=None, keep_final=True, evidence_sources=None):
        super().__init__()
        self.reasons = ['no_owning_work'] if reasons is None else reasons
        self.keep_final = keep_final
        self.evidence_sources = evidence_sources
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
            if self.evidence_sources is not None:
                packet['evidence_sources'] = json.loads(json.dumps(self.evidence_sources))
        return {'type': 'await_report.ok', 'ok': True, 'result_kind': 'report',
                'report': {'report_id': inp['report_id'], 'effective_model': payload['model'],
                           'effective_effort': payload['effort'], 'extras': {'daily_retro': packet}}}


def prepared_history(config, count=43, reasons=None, evidence_sources=None):
    history, baseline = history_fixture(config, count=count)
    rpc = HistoryTransport(reasons, evidence_sources=evidence_sources)
    pipeline = retro.Pipeline(history, rpc)
    inventory = retro.history_collect(history, baseline, 1)
    for n in range(1, inventory['history']['batches'] + 1):
        assert asyncio.run(pipeline.history_run(baseline, n, no_deliver=True))['delivered'] == []
    return history, baseline, rpc, pipeline


@pytest.mark.parametrize('sources', [
    [{'id': 'E1', 'path': 'current/spec.md', 'verification': 'retained proof'}],
    {'E1': {'path': 'current/spec.md', 'verification': 'retained proof'}},
    [{'path': 'current/spec.md'}, 'retained verification label'],
])
def test_history_evidence_shapes_checkpoint_final_and_replay(config, sources):
    history, baseline, rpc, pipeline = prepared_history(config, evidence_sources=sources)
    before = {p: p.read_bytes() for p in (history.state_root / 'runs').glob('history-*/*.json')}
    checkpoint = asyncio.run(pipeline.history_consolidate(baseline, [1, 2]))
    final = asyncio.run(pipeline.history_consolidate(baseline, final=True))
    for result in (checkpoint, final):
        packet = retro.read(history.state_root / 'runs' / result['run_id'] / 'astra.json')['packet']
        expected = list(sources.values()) if isinstance(sources, dict) else sources
        assert all(value in packet['evidence_sources'].values() for value in expected)
        assert set(ref for c in packet['candidates'] for ref in c['citations']) == {
            s['id'] for s in retro.read(history.state_root / 'history.json')['sources']}
    assert asyncio.run(pipeline.history_consolidate(baseline, [2, 1])) == checkpoint
    assert asyncio.run(pipeline.history_consolidate(baseline, final=True)) == final
    assert len(rpc.spawns) == 5 and len(rpc.closed) == 5 and len(rpc.sent) == 2
    assert {p: p.read_bytes() for p in before} == before


def test_history_evidence_conflicting_labels_remain_available(config):
    sources = [{'id': 'E1', 'proof': 'first'}, {'id': 'E1', 'proof': 'second'}]
    history, baseline, rpc, pipeline = prepared_history(config, evidence_sources=sources)
    result = asyncio.run(pipeline.history_consolidate(baseline, [1, 2]))
    packet = retro.read(history.state_root / 'runs' / result['run_id'] / 'astra.json')['packet']
    assert all(value in packet['evidence_sources'].values() for value in sources)


@pytest.mark.parametrize('shape', ['list', 'mapping'])
def test_history_evidence_qualified_key_collision_preserves_each_proof(config, shape):
    history, baseline, rpc, pipeline = prepared_history(config)
    result = asyncio.run(pipeline.history_consolidate(baseline, [1, 2]))
    manifest = retro.read(history.state_root / 'runs' / result['run_id'] / 'collection.json')
    _, completed = retro.completed_history(history, baseline)
    first = {'id': 'E1', 'proof': 'first'}
    last = {'id': 'E1', 'proof': 'second'}
    qualified = f"{completed[2]['manifest']['run_id']}:E1:{retro.digest(last)}"
    occupied = {'id': qualified, 'proof': 'preexisting qualified key'}
    completed[1]['final']['packet']['evidence_sources'] = (
        [first, occupied] if shape == 'list' else {'E1': first, qualified: occupied})
    completed[2]['final']['packet']['evidence_sources'] = (
        [last] if shape == 'list' else {'E1': last})
    original = json.loads(json.dumps(completed))
    packet = retro.compile_history_packet(manifest, completed)
    assert all(value in packet['evidence_sources'].values() for value in [first, occupied, last])
    assert json.loads(json.dumps(completed)) == original


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


# --- daemon restart continuity (spec_pentacle__daemon_restart_continuity_2026_10) ---
# Matrix cells C4/C4b run the real `run --on-demand` path against a disposable
# daemon (tests/soak/test_restart_continuity.py); these pin the driver contract.


class DaemonDown(Transport):
    """The daemon restarted under the Astra await and stays down."""
    def __init__(self):
        super().__init__()
        self.down = False

    def _gate(self):
        if self.down:
            raise ConnectionRefusedError(111, "Connect call failed ('127.0.0.1', 7791)")

    async def await_report_once(self, config, stream, msg_id, **kwargs):
        if "astra" in stream:
            self.down = True
        self._gate()
        return await super().await_report_once(config, stream, msg_id, **kwargs)

    async def assistant_once(self, config, payload):
        self._gate()
        return await super().assistant_once(config, payload)

    async def close_once(self, config, stream, **kwargs):
        self._gate()
        return await super().close_once(config, stream, **kwargs)

    async def send_receipt_once(self, config, target, key):
        self._gate()
        return await super().send_receipt_once(config, target, key)


def test_unrecoverable_outage_persists_failure_then_flushes_notice_once(config):
    """C4b: primary error survives cleanup, notice queued before any RPC, flushed once."""
    source(config.memory_root, "one", body="No lessons.")
    rpc = DaemonDown()
    pipeline = retro.Pipeline(config, rpc)
    with pytest.raises(ConnectionRefusedError):
        asyncio.run(pipeline.run(at()))
    root = config.state_root / "runs/2026-09-28"
    failure = retro.read(root / "failure.json")
    assert failure["run_id"] == "2026-09-28" and failure["stage"] == "astra" and failure["seq"] == 1
    assert failure["error"]["class"] == "ConnectionRefusedError" and failure["error"]["reason"] == "transport_loss"
    assert failure["notice"] == "pending"
    assert "cleanup_error" in failure and "notice_error" in failure
    assert retro.read(root / "failure-delivery.json")["pending"]["seq"] == 1
    rpc.down = False
    rpc.await_report_once = Transport.await_report_once.__get__(rpc)
    assert asyncio.run(pipeline.run(at()))["delivered"] == ["2026-09-28"]
    bodies = [r["payload"]["text"] for r in rpc.sent.values()]
    assert sum(b.startswith("REPORT daily-retro failure") for b in bodies) == 1
    assert sum(b.startswith("REPORT daily-retro ready") for b in bodies) == 1
    assert len(rpc.spawns) == 2  # no respawn: Sol and Astra once each
    assert retro.read(root / "failure.json")["notice"] == "delivered"
    asyncio.run(pipeline.run(at()))
    assert len(rpc.sent) == 2  # the delivered notice is never resent


def test_cleanup_error_never_replaces_primary_error(config):
    source(config.memory_root, "one")
    class CleanupDown(Transport):
        async def await_report_once(self, config, stream, msg_id, **kwargs):
            if "astra" in stream:
                raise RuntimeError("primary astra failure")
            return await super().await_report_once(config, stream, msg_id, **kwargs)
        async def close_once(self, config, stream, **kwargs):
            raise ConnectionRefusedError(111, "refused")
    with pytest.raises(RuntimeError, match="primary astra failure"):
        asyncio.run(retro.Pipeline(config, CleanupDown()).run(at()))
    failure = retro.read(config.state_root / "runs/2026-09-28/failure.json")
    assert failure["error"] == retro.structured_error(RuntimeError("primary astra failure"))
    assert failure["cleanup_error"]["class"] == "ConnectionRefusedError"


def test_interrupt_queues_notice_truthfully_without_rpc(config):
    """Finding (c): a BaseException exit queues the notice; no delivery RPC is made."""
    source(config.memory_root, "one")
    class Interrupted(Transport):
        async def await_report_once(self, config, stream, msg_id, **kwargs):
            if "astra" in stream:
                raise KeyboardInterrupt
            return await super().await_report_once(config, stream, msg_id, **kwargs)
        async def assistant_once(self, config, payload):
            raise AssertionError("no delivery RPC on an interrupt")
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(retro.Pipeline(config, Interrupted()).run(at()))
    root = config.state_root / "runs/2026-09-28"
    assert retro.read(root / "failure.json")["notice"] == "pending"
    assert retro.read(root / "failure-delivery.json")["pending"]["stage"] == "astra"


def test_newer_failure_supersedes_unlanded_notice_and_keeps_history(config):
    """Finding (a): one notice per failure; the latest is marked, earlier kept."""
    source(config.memory_root, "one")
    rpc = DaemonDown()
    pipeline = retro.Pipeline(config, rpc)
    for down_from_start in (False, True):  # the second pass cannot flush notice 1
        rpc.down = down_from_start
        with pytest.raises(ConnectionRefusedError):
            asyncio.run(pipeline.run(at()))
    failure = retro.read(config.state_root / "runs/2026-09-28/failure.json")
    assert failure["seq"] == 2 and failure["latest"] is True and failure["notice"] == "pending"
    assert [h["seq"] for h in failure["history"]] == [1]
    assert failure["history"][0]["notice"] == "superseded by failure 2"
    pending = retro.read(config.state_root / "runs/2026-09-28/failure-delivery.json")["pending"]
    assert pending["seq"] == 2


def test_recorded_review_supersedes_pending_notice(config, monkeypatch):
    """Finding (d): a later-reviewed run never leaves a stale pending notice."""
    source(config.memory_root, "one")
    rpc = DaemonDown()
    pipeline = retro.Pipeline(config, rpc)
    with pytest.raises(ConnectionRefusedError):
        asyncio.run(pipeline.run(at()))
    root = config.state_root / "runs/2026-09-28"
    retro.atomic(root / "review.json", {"recorded": True})
    rpc.down = False
    asyncio.run(pipeline.run(at()))
    assert retro.read(root / "failure-delivery.json")["pending"] is None
    assert retro.read(root / "failure.json")["notice"] == "superseded: review recorded"
    assert not rpc.sent


# --------------------------------------------------------------------------- #
# Error records never carry message text (cycle 2, B1 pivot). Property tests:
# fuzzed messages with secret stand-ins in quoted/escaped/newline/backslash and
# cut forms; no persisted or delivered field holds any part of them.
# --------------------------------------------------------------------------- #

# Stand-in alphabet without hex digits, so a 6-character window can never be
# a coincidental match inside a sha256 or a timestamp.
_STANDIN = "GHJKLMNPQRSTUVWXYZghjkmnpqrstuvwxyz"
_FORMS = (
    'password="{a} {b}" tail', "password='{a} {b}", "password='{a} {b}\\", 'api_key="{a} {b}\\',
    'password="{a}\\\n{b}"', "token=\"{a} \\\" {b}\" tail", "secret={a}}}{b} rest", 'secret={a}"{b} rest',
    "secret={a};{b}", "GET /x?access_token={a}&y={b}", "Authorization: Bearer {a}", "Authorization: 'Bearer {a} {b}'",
    '{{"stream_token": "{a}", "next": "{b}"}}', "api_key='{a}\\'{b}'", "Bearer\t{a}\n{b}", "{a}{b}",
    "token=\x00{a}\x7f{b}", "pässwörd={a} {b}", "access_token=[redacted] {a}", "credential:{a}\r\n{b}",
)


def _standin(rng):
    return "".join(rng.choice(_STANDIN) for _ in range(rng.randint(10, 24)))


def _fuzzed_messages(seed, count):
    import random
    rng = random.Random(seed)
    for _ in range(count):
        a, b = _standin(rng), _standin(rng)
        raw = rng.choice(_FORMS).format(a=a, b=b)
        if rng.random() < 0.3:  # a cut string
            raw = raw[: rng.randint(len(raw) // 2, len(raw))]
        if rng.random() < 0.3:
            raw = rng.choice(("prefix: ", "x" * rng.randint(0, 400) + " ", "\\")) + raw
        parts = [p for p in (a, b) if len(p) >= 6 and any(p[i:i + 6] in raw for i in range(len(p) - 5))]
        yield raw, parts


def _leaks(text, parts):
    return [p[i:i + 6] for p in parts for i in range(len(p) - 5) if p[i:i + 6] in text]


def test_structured_error_holds_no_message_text_and_is_idempotent(tmp_path):
    for n, (raw, parts) in enumerate(_fuzzed_messages(1, 2000)):
        for exc in (RuntimeError(raw), ConnectionRefusedError(111, raw), TimeoutError(raw), raw):
            record = retro.structured_error(exc, tmp_path if n % 50 == 0 else None)
            assert set(record) == {"class", "reason", "bytes", "sha256"}
            text = json.dumps(record) + retro.render_error(record)
            assert not _leaks(text, parts), (raw, record)
            assert retro.structured_error(record) == record  # idempotent re-sanitize
            assert retro.structured_error(retro.structured_error(exc)) == retro.structured_error(exc)
    exc = ConnectionRefusedError(111, "refused")
    assert retro.structured_error(exc)["reason"] == "transport_loss"
    assert retro.structured_error(TimeoutError())["reason"] == "timeout"
    assert retro.structured_error(FileNotFoundError(2, "x"))["reason"] == "os_error:ENOENT"
    assert retro.structured_error(KeyboardInterrupt())["reason"] == "interrupted"


ROUTES = ("raised", "dynamic-class", "failed-report", "invalid-packet")


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("seed", range(12))
def test_failure_records_and_notice_never_persist_or_deliver_message_text(tmp_path, seed, route):
    """Every file the run writes (except the local 0600 raw copies) and every
    delivered body is free of the failing text, whether it arrives as a raised
    exception, in a dynamically named exception class, in a failed worker
    report, or in an invalid terminal packet; across primary, cleanup and
    notice-delivery errors and a second pass."""
    raw, parts = next(_fuzzed_messages(1000 + seed, 1))
    memory = tmp_path / "memory"
    for folder in ("completed", "deprecated"):
        (memory / "work" / folder).mkdir(parents=True)
    (tmp_path / "token").write_text("fixture")
    config = retro.Settings(memory, tmp_path / "state", "ws://127.0.0.1:12345", tmp_path / "token", "fixture",
                            isolated=True)
    source(config.memory_root, "one")
    dynamic = type(_standin(__import__("random").Random(seed)), (RuntimeError,), {})
    parts = [*parts, dynamic.__name__]

    class Failing(Transport):
        notice_down = True

        async def await_report_once(self, config, stream, msg_id, **kwargs):
            if "astra" not in stream:
                return await super().await_report_once(config, stream, msg_id, **kwargs)
            if route == "raised":
                raise RuntimeError(raw)
            if route == "dynamic-class":
                raise dynamic(raw)
            if route == "failed-report":
                return {"type": "await_report.ok", "ok": False, "result_kind": "report", "status": "error",
                        "reason": raw, "report": {"status": "error", "reason": raw, "summary": raw}}
            response = await super().await_report_once(config, stream, msg_id, **kwargs)
            response["report"]["extras"]["daily_retro"]["run_id"] = raw
            response["report"]["summary"] = raw
            return response

        async def close_once(self, config, stream, **kwargs):
            raise ConnectionRefusedError(111, raw)

        async def send_receipt_once(self, config, target, key):
            if self.notice_down:
                raise OSError(5, raw)
            return await super().send_receipt_once(config, target, key)

    rpc = Failing()
    with pytest.raises(Exception):
        asyncio.run(retro.Pipeline(config, rpc).run(at()))
    rpc.notice_down = False
    with pytest.raises(Exception):  # Astra still fails; the queued notice flushes first
        asyncio.run(retro.Pipeline(config, rpc).run(at()))
    root = config.state_root / "runs/2026-09-28"
    bodies = [r["payload"]["text"] for r in rpc.sent.values()]
    assert bodies and all(b.startswith("REPORT daily-retro failure") for b in bodies)
    persisted = {p: p.read_text(errors="replace") for p in config.state_root.rglob("*")
                 if p.is_file() and p.parent.name != "errors"}
    for path, text in [*persisted.items(), *(("delivered", b) for b in bodies)]:
        assert not _leaks(text, parts), (path, raw)
    failure = retro.read(root / "failure.json")
    for entry in [failure, *failure["history"]]:
        for field in ("error", "cleanup_error", "notice_error"):
            if field in entry:
                assert retro.structured_error(entry[field]) == entry[field], entry[field]  # genuine record
    assert failure["error"]["class"] in {"RuntimeError", "ValueError", "KeyError", "TypeError"}
    assert failure["cleanup_error"]["reason"] == "transport_loss"
    assert any(h.get("notice_error", {}).get("reason") == "os_error:EIO" for h in [failure, *failure["history"]])
    kept = list((root / "errors").iterdir())
    assert kept and all(p.stat().st_mode & 0o777 == 0o600 for p in kept)
    assert (root / "errors").stat().st_mode & 0o777 == 0o700
    if route in ("raised", "dynamic-class"):
        assert raw.encode("utf-8", "backslashreplace") in {p.read_bytes() for p in kept}


def test_record_vocabulary_is_fixed_and_forged_records_are_not_accepted():
    """Cycle 2 re-read B1-CLASS: neither field can carry caller-chosen text."""
    secret = "QzKwHrTyMnPvXsJg"
    for cls in (type(secret, (RuntimeError,), {}), type(secret, (Exception,), {}), type(secret, (OSError,), {})):
        record = retro.structured_error(cls("x"))
        assert secret not in json.dumps(record) and record["class"] in {"RuntimeError", "Exception", "OSError"}
    spoof = type("ValueError", (Exception,), {"__module__": "builtins"})  # not the genuine builtins class
    assert retro.structured_error(spoof("x"))["class"] == "Exception"
    genuine = retro.structured_error(ConnectionResetError(104, "x"))
    assert retro.structured_error(genuine) == genuine
    for forged in ({**genuine, "reason": secret}, {**genuine, "class": secret}, {**genuine, "reason": "os_error:" + secret},
                   {**genuine, "extra": secret}, {**genuine, "bytes": True}):
        record = retro.structured_error(forged)
        assert record["class"] == "LegacyText" and secret not in json.dumps(record) + retro.render_error(forged)


def test_json_shaped_forged_records_become_text_records_without_error():
    """Cycle 3 B1 check: non-string class or reason (e.g. a list from JSON) is
    a forged record, converted like any raw text, never a TypeError."""
    secret = "QzKwHrTyMnPvXsJg"
    genuine = retro.structured_error(TimeoutError("x"))
    for forged in ({**genuine, "class": [secret]}, {**genuine, "reason": [secret]},
                   {**genuine, "class": {"k": secret}}, {**genuine, "reason": None}):
        record = retro.structured_error(forged)
        assert record["class"] == "LegacyText" and secret not in json.dumps(record) + retro.render_error(forged)


def test_crafted_worker_receipt_with_raw_siblings_is_projected_again(config):
    """Cycle 3 B1 check: a receipt is recognised only by its exact projected
    schema; a valid structured `error` does not bless raw sibling fields."""
    secret = "QzKwHrTyMnPvXsJg"
    manifest = retro.collect(config, at())
    root = config.state_root / "runs" / manifest["run_id"]
    error = retro.structured_error(RuntimeError("x"))
    crafted = {"result_kind": "report", "status": "error", "report_id": "r", "ledger_row_id": 1, "error": error,
               "reason": secret, "report": {"summary": secret}}
    wrong_types = {"result_kind": ["report"], "status": "error", "report_id": "r", "ledger_row_id": 1, "error": error}
    retro.atomic(root / "astra.json", {"attempt": 1, "report_id": "r", "failed": True, "failure": crafted,
                                       "report": {**crafted, "result_kind": secret}})
    retro.atomic(root / "sol-attempts.json", [{"attempt": 1, "report_id": "r", "failed": True,
                                               "failure": wrong_types}])
    retro.Pipeline(config, Transport()).normalize_retained_failure_state(manifest)
    astra = retro.read(root / "astra.json")
    assert set(astra["failure"]) == {"result_kind", "status", "report_id", "ledger_row_id", "error"}
    assert astra["failure"]["error"]["class"] == "WorkerReport"
    assert set(retro.read(root / "sol-attempts.json")[0]["failure"]) == set(astra["failure"])
    for path in root.rglob("*"):
        if path.is_file() and path.parent.name != "errors":
            assert secret not in path.read_text(errors="replace"), path
    projected = retro._worker_failure_receipt({"report": {"status": "done"}}, root, "r")
    assert retro._projected_stage({"failed": True, "report_id": "r", "failure": projected}, root)["failure"] == projected


def test_retained_legacy_failure_state_is_converted_and_never_resent(config):
    """Cycle 2 re-read B1-LEGACY: text records and an unlanded notice attempt
    written before structured records are converted on the next pass. The
    attempt keeps its key (exactly-once) and only the structured body is
    sent; a landed legacy attempt is never resent."""
    secret = "QzKwHrTyMnPvXsJg"
    old = 'RuntimeError: token="' + secret + '\\'
    manifest = retro.collect(config, at())
    root = config.state_root / "runs" / manifest["run_id"]
    retro.atomic(root / "failure.json", {"run_id": manifest["run_id"], "seq": 2, "error": old, "stage": "astra",
                                         "notice": "pending", "cleanup_error": old,
                                         "history": [{"seq": 1, "error": old, "notice": "delivered"}]})
    payload = {"host": "fixture", "session_name": "reviewer", "text": old, "request_id": "legacy-2",
               "optimistic_id": "legacy-2"}
    landed = {**payload, "request_id": "legacy-1", "optimistic_id": "legacy-1"}
    retro.atomic(root / "failure-delivery.json", {
        "pending": {"seq": 2, "stage": "astra", "failure": old},
        "attempts": [{"target": "fixture:reviewer", "generation": "g1", "seq": 1, "request_id": "legacy-1",
                      "payload": landed, "confirmed": True, "receipt": {"delivery": "landed", "payload": landed}},
                     {"target": "fixture:reviewer", "generation": "g1", "seq": 2, "request_id": "legacy-2",
                      "payload": payload}]})
    retro.atomic(root / "astra.json", {"attempt": 1, "report_id": "r", "failed": True,
                                       "failure": {"type": "await_report.ok", "ok": False, "reason": old}})
    rpc = Transport()
    asyncio.run(retro.Pipeline(config, rpc).deliver(manifest, failure=True))
    assert list(rpc.sent) == ["legacy-2"]  # same key; the landed one is not resent
    assert secret not in rpc.sent["legacy-2"]["payload"]["text"]
    assert rpc.sent["legacy-2"]["payload"]["text"].startswith("REPORT daily-retro failure")
    retro.record_failure(root, manifest["run_id"], stage="astra", error=retro.structured_error(RuntimeError("new")),
                         notice="pending")
    for path in root.rglob("*"):
        if path.is_file() and path.parent.name != "errors":
            assert secret not in path.read_text(errors="replace"), path
    assert secret in "".join(p.read_text() for p in (root / "errors").iterdir())


def test_cli_failure_writes_only_the_structured_record_to_stderr(tmp_path):
    """Cycle 2 B1: the scheduled job's stderr is a log file. An uncaught error
    leaves a structured record there; its text and traceback stay in the
    0600 errors folder."""
    import os
    import subprocess
    import sys
    secret = "QzKwHrTyMnPvXsJg"
    memory, state = tmp_path / "memory", tmp_path / "state"
    for folder in ("completed", "deprecated"):
        (memory / "work" / folder).mkdir(parents=True)
    (tmp_path / "token").write_text("fixture")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"timezone": "America/Chicago", "memory_root": str(memory), "state_root": str(state),
                               "token_path": str(tmp_path / "token"), "ws_url": "ws://127.0.0.1:12345",
                               "host": "fixture", "isolated": True}))
    service = Path(retro.__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(service.parent / "agent-orch"), str(service.parent), str(service)])}
    out = subprocess.run([sys.executable, str(service / "tools/daily_retro.py"), "summary", "--config", str(cfg),
                          "--end-day", f"password='{secret}\\"], cwd=str(service), env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 1, out.stderr
    assert secret[:6] not in out.stderr and secret[:6] not in out.stdout and "Traceback" not in out.stderr
    record = json.loads(out.stderr.strip().splitlines()[-1])["error"]
    assert record["class"] == "ValueError" and set(record) == {"class", "reason", "bytes", "sha256"}
    kept = {p.name: p for p in (state / "errors").iterdir()}
    assert f"{record['sha256']}.txt" in kept and f"{record['sha256']}.traceback.txt" in kept
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in kept.values())
    assert secret in kept[f"{record['sha256']}.traceback.txt"].read_text()


def fixture_settings_file(config, **overrides):
    path = config.state_root.parent / "fixture-config.json"
    retro.atomic(path, {"timezone": "America/Chicago", "memory_root": str(config.memory_root),
        "state_root": str(config.state_root), "ws_url": config.ws_url,
        "token_path": str(config.token_path), "host": config.host, "isolated": True, **overrides})
    return path


def move_fixture(path, status):
    target = path.parents[2] / status / path.parent.name / "spec.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    path.rename(target)
    return target


def decision_bytes(config):
    return {str(p.relative_to(config.state_root)): p.read_bytes()
            for p in (config.state_root / "decision-receipts").glob("*.json")}


def test_authorized_guard_config(config, monkeypatch):
    omitted = retro.Settings.load(fixture_settings_file(config))
    assert omitted.self_assignment_exclusions == []
    for invalid in (None, True, "spec_fixture_umbrella", {}, [""], [None], [True],
                    [" spec_fixture_umbrella"], ["../escape"], ["spec/one"]):
        with pytest.raises(ValueError, match="self_assignment_exclusions"):
            retro.Settings.load(fixture_settings_file(config, self_assignment_exclusions=invalid))
    configured = retro.Settings.load(fixture_settings_file(config,
        self_assignment_exclusions=["spec_fixture_umbrella", "spec_fixture_umbrella"]))
    path = source(config.memory_root, "fixture_umbrella", status="in_progress")
    monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:reviewer")
    rpc = Transport()
    before = path.read_bytes()
    with pytest.raises(ValueError, match="^refused: use resolved or duplicate$"):
        asyncio.run(retro.Pipeline(configured, rpc).decision("spec_fixture_umbrella", proposal2("authorized")))
    assert path.read_bytes() == before and not decision_bytes(config)
    assert not rpc.questions and not rpc.sent and not rpc.spawns
    case = source(config.memory_root, "FIXTURE_UMBRELLA", status="in_progress")
    result = asyncio.run(retro.Pipeline(configured, rpc).decision("spec_FIXTURE_UMBRELLA", proposal2("authorized")))
    assert result["baseline_status"] == "in_progress" and case.exists()


@pytest.mark.parametrize("status", ["completed", "deprecated", "missing"])
def test_authorized_guard_refusal(config, monkeypatch, status):
    path = source(config.memory_root, "one", status=status) if status != "missing" else None
    before = path.read_bytes() if path else None
    monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:reviewer")
    rpc = Transport()
    expected = "unknown_work_id" if status == "missing" else "refused: use resolved or duplicate"
    with pytest.raises(ValueError, match="^" + expected + "$"):
        asyncio.run(retro.Pipeline(config, rpc).decision("spec_one", proposal2("authorized")))
    assert (path.read_bytes() if path else None) == before and not decision_bytes(config)
    assert not rpc.questions and not rpc.sent and not rpc.spawns


@pytest.mark.parametrize("replay", ["terminal", "excluded", "missing"])
def test_authorized_guard_replay_baseline(config, monkeypatch, replay):
    path = source(config.memory_root, "one", status="in_progress")
    path.write_text(path.read_text().replace("status: in_progress", "status: completed\nbaseline_status: forged"))
    monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:reviewer")
    rpc = Transport(); pipeline = retro.Pipeline(config, rpc)
    payload = {**proposal2("authorized"), "baseline_status": "forged"}
    first = asyncio.run(pipeline.decision("spec_one", payload))
    assert first["baseline_status"] == "in_progress"
    receipts = decision_bytes(config)
    assert len(receipts) == 1 and json.loads(next(iter(receipts.values())))["baseline_status"] == "in_progress"
    path = move_fixture(path, "needs_qa")
    assert asyncio.run(pipeline.decision("spec_one", payload)) == first
    assert decision_bytes(config) == receipts
    if replay == "terminal":
        path = move_fixture(path, "completed")
    elif replay == "excluded":
        pipeline = retro.Pipeline(replace(config, self_assignment_exclusions=["spec_one"]), rpc)
    else:
        path.unlink()
    before = path.read_bytes() if path.exists() else None
    expected = "unknown_work_id" if replay == "missing" else "refused: use resolved or duplicate"
    with pytest.raises(ValueError, match="^" + expected + "$"):
        asyncio.run(pipeline.decision("spec_one", payload))
    assert (path.read_bytes() if path.exists() else None) == before and decision_bytes(config) == receipts
    assert not rpc.questions and not rpc.sent and not rpc.spawns


def test_authorized_guard_new_version(config, monkeypatch):
    path = source(config.memory_root, "one", status="analysis")
    monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:reviewer")
    pipeline = retro.Pipeline(config, Transport())
    first = asyncio.run(pipeline.decision("spec_one", proposal2("authorized")))
    old_receipts = decision_bytes(config)
    path = move_fixture(path, "in_progress")
    changed = {**proposal2("authorized"), "scope": "New fixture scope"}
    second = asyncio.run(pipeline.decision("spec_one", changed))
    assert second["version"] != first["version"] and second["baseline_status"] == "in_progress"
    assert all(decision_bytes(config)[k] == v for k, v in old_receipts.items())
    cleared = asyncio.run(pipeline.decision("spec_one", {**changed, "disposition": "no_change", "baseline_status": "forged"}))
    assert "baseline_status" not in cleared
    path = move_fixture(path, "needs_qa")
    third = asyncio.run(pipeline.decision("spec_one", {**changed, "scope": "Third fixture scope"}))
    assert third["baseline_status"] == "needs_qa"
    assert all(decision_bytes(config)[k] == v for k, v in old_receipts.items())


def test_authorized_guard_hash_and_legacy(config, monkeypatch):
    path = source(config.memory_root, "one", status="in_progress")
    monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:reviewer")
    payload = proposal2("authorized")
    version = retro.proposal_version(payload)
    assert retro.proposal_version({**payload, "baseline_status": "forged"}) == version
    legacy = {**payload, "version": version, "state": "authorized", "attempts": []}
    retro.save_proposals(path, path.read_bytes(), {legacy["id"]: legacy})
    receipt_path = config.state_root / "decision-receipts" / (retro.digest(["spec_one", legacy["id"], version, "authorized"]) + ".json")
    retro.atomic(receipt_path, {"work_id": "spec_one", "proposal_id": legacy["id"], "version": version, "state": "authorized"})
    old_receipts = decision_bytes(config)
    pipeline = retro.Pipeline(config, Transport())
    result = asyncio.run(pipeline.decision("spec_one", {**payload, "baseline_status": "forged"}))
    assert result.get("baseline_status", "legacy_unknown") == "legacy_unknown"
    assert decision_bytes(config) == old_receipts
    for invalid in ("../escape", "", None):
        with pytest.raises(ValueError, match="normal work ID required"):
            asyncio.run(pipeline.decision(invalid, payload))
    duplicate = source(config.memory_root, "duplicate", status="analysis")
    duplicate.write_text(duplicate.read_text().replace("spec_duplicate", "spec_one"))
    with pytest.raises(ValueError, match="resolve exactly once"):
        asyncio.run(pipeline.decision("spec_one", payload))
    assert decision_bytes(config) == old_receipts and not pipeline.rpc.questions
    duplicate.unlink()
    external = config.state_root.parent / "outside-spec.md"
    external.write_text(path.read_text().replace("spec_one", "spec_escape"))
    escaped = config.memory_root / "work/in_progress/escape/spec.md"
    escaped.parent.mkdir(); escaped.symlink_to(external)
    with pytest.raises(ValueError, match="escapes memory root"):
        asyncio.run(pipeline.decision("spec_escape", payload))
    assert decision_bytes(config) == old_receipts
    authorized_guard_stale_cas_control(config, monkeypatch)


def test_authorized_guard_cli_and_cas(config, monkeypatch, capsys):
    path = source(config.memory_root, "one", status="in_progress")
    monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:reviewer")
    cfg = fixture_settings_file(config)
    payload = config.state_root.parent / "proposal.json"
    retro.atomic(payload, proposal2("authorized"))
    rpc = Transport(); original = retro.Pipeline
    monkeypatch.setattr(retro, "Pipeline", lambda settings: original(settings, rpc))
    monkeypatch.setattr(retro.sys, "argv", ["daily_retro.py", "decision", "--config", str(cfg),
        "--work-id", "spec_one", "--proposal", str(payload)])
    retro.main()
    assert json.loads(capsys.readouterr().out)["baseline_status"] == "in_progress"
    before = decision_bytes(config)
    save = retro.save_proposals
    def concurrent(p, preimage, records):
        p.write_bytes(preimage + b"\nConcurrent fixture note.\n")
        return save(p, preimage, records)
    monkeypatch.setattr(retro, "save_proposals", concurrent)
    with pytest.raises(RuntimeError, match="preimage moved"):
        asyncio.run(original(config, rpc).decision("spec_one", {**proposal2("authorized"), "scope": "Changed scope"}))
    assert path.read_bytes().endswith(b"Concurrent fixture note.\n") and decision_bytes(config) == before
    assert not rpc.questions and not rpc.sent and not rpc.spawns


def test_authorized_guard_lock_move(config, monkeypatch):
    path = source(config.memory_root, "one", status="in_progress")
    monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:reviewer")
    original = retro.locked
    @retro.contextmanager
    def moved(lock):
        with original(lock):
            move_fixture(path, "completed")
            yield
    monkeypatch.setattr(retro, "locked", moved)
    rpc = Transport()
    with pytest.raises(ValueError, match="refused: use resolved or duplicate"):
        asyncio.run(retro.Pipeline(config, rpc).decision("spec_one", proposal2("authorized")))
    assert not decision_bytes(config) and not rpc.questions and not rpc.sent and not rpc.spawns


def test_authorized_guard_schema1_roundtrip(config, monkeypatch):
    path = source(config.memory_root, "one", status="analysis")
    monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:reviewer")
    pipeline = retro.Pipeline(config, Transport())
    payload = {**proposal(), "disposition": "authorized", "authority": "fixture grant"}
    first = asyncio.run(pipeline.decision("spec_one", payload))
    before = decision_bytes(config)
    cleared = asyncio.run(pipeline.decision("spec_one", {**payload, "disposition": "no_change"}))
    assert cleared["state"] == "no_change" and "baseline_status" not in cleared
    path = move_fixture(path, "in_progress")
    replay = asyncio.run(pipeline.decision("spec_one", payload))
    assert replay["state"] == "authorized" and replay["baseline_status"] == first["baseline_status"] == "analysis"
    assert all(decision_bytes(config)[k] == v for k, v in before.items())
    assert not pipeline.rpc.questions


def test_authorized_guard_preserves_default_disposition_replay(config, monkeypatch):
    path = source(config.memory_root, "one", status="analysis")
    monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:reviewer")
    payload = proposal()
    record = {**payload, "version": retro.proposal_version(payload), "state": "rejected", "attempts": []}
    retro.save_proposals(path, path.read_bytes(), {record["id"]: record})
    rpc = Transport()
    assert asyncio.run(retro.Pipeline(config, rpc).decision("spec_one", payload))["state"] == "rejected"
    assert not rpc.questions and not rpc.sent


def test_authorized_guard_pending_baseline(config, monkeypatch):
    path = source(config.memory_root, "one", status="analysis")
    monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:reviewer")
    rpc = Transport(); pipeline = retro.Pipeline(config, rpc)
    asyncio.run(pipeline.decision("spec_one", proposal()))
    payload = {**proposal(), "disposition": "authorized", "authority": "fixture grant"}
    first = asyncio.run(pipeline.decision("spec_one", payload))
    assert first["state"] == "pending" and first["baseline_status"] == "analysis"
    receipts = decision_bytes(config)
    move_fixture(path, "in_progress")
    replay = asyncio.run(pipeline.decision("spec_one", payload))
    assert replay["baseline_status"] == "analysis" and decision_bytes(config) == receipts
    assert len(rpc.questions) == 1


def test_authorized_guard_legacy_shipped_baseline(config, monkeypatch):
    path = source(config.memory_root, "one", status="analysis")
    monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:reviewer")
    payload = {**proposal(), "disposition": "authorized", "authority": "fixture grant"}
    legacy = {**payload, "version": retro.proposal_version(payload), "state": "shipped", "attempts": []}
    retro.save_proposals(path, path.read_bytes(), {legacy["id"]: legacy})
    rpc = Transport()
    current = asyncio.run(retro.Pipeline(config, rpc).decision("spec_one", payload))
    assert current["state"] == "shipped" and "baseline_status" not in current
    assert not rpc.questions


@pytest.mark.parametrize("explicit", [False, True])
def test_authorized_guard_preserves_historical_authorized_replay(config, monkeypatch, explicit):
    path = source(config.memory_root, "one", status="analysis")
    monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:reviewer")
    payload = {**proposal(), **({"disposition": "propose"} if explicit else {})}
    record = {**payload, "version": retro.proposal_version(payload), "state": "authorized", "attempts": []}
    retro.save_proposals(path, path.read_bytes(), {record["id"]: record})
    rpc = Transport()
    assert asyncio.run(retro.Pipeline(config, rpc).decision("spec_one", payload))["state"] == "authorized"
    assert not rpc.questions and not rpc.sent


def authorized_guard_stale_cas_control(config, monkeypatch):
    class ObservedTransport(Transport):
        def __init__(self):
            super().__init__()
            self.prompt_calls = []
        async def prompt_status_once(self, config, qid):
            self.prompt_calls.append("status")
            return await super().prompt_status_once(config, qid)
        async def prompt_ask_once(self, config, payload):
            self.prompt_calls.append("ask")
            return await super().prompt_ask_once(config, payload)
        async def prompt_cancel_once(self, config, payload):
            self.prompt_calls.append("cancel")
            return await super().prompt_cancel_once(config, payload)
    path = source(config.memory_root, "cas_fixture", status="in_progress")
    rpc = ObservedTransport(); pipeline = retro.Pipeline(config, rpc)
    pending = asyncio.run(pipeline.decision("spec_cas_fixture", proposal2("propose")))
    qid = pending["attempts"][0]["question_id"]
    receipts = decision_bytes(config)
    concurrent = path.read_bytes() + b"\nSettled concurrent fixture edit.\n"
    save = retro.save_proposals
    def moved(p, preimage, records):
        p.write_bytes(concurrent)
        return save(p, preimage, records)
    rpc.prompt_calls.clear()
    with monkeypatch.context() as patch:
        patch.setattr(retro, "save_proposals", moved)
        with pytest.raises(RuntimeError, match="preimage moved"):
            asyncio.run(pipeline.decision("spec_cas_fixture", proposal2("authorized")))
    assert path.read_bytes() == concurrent and decision_bytes(config) == receipts
    assert [c for c in rpc.prompt_calls if c != "status"] == ["cancel"]
    assert rpc.questions[qid]["state"] == "cancelled" and not rpc.sent and not rpc.spawns
    rpc.prompt_calls.clear()
    result = asyncio.run(pipeline.decision("spec_cas_fixture", proposal2("authorized")))
    assert result["state"] == "authorized" and result["baseline_status"] == "in_progress"
    assert rpc.prompt_calls == ["status"]  # Reconciliation reads; no prompt mutation.
    assert len(decision_bytes(config)) == len(receipts) + 1
    for case in ("excluded", "completed", "missing"):
        path = source(config.memory_root, "guard_" + case, status="in_progress")
        work_id = "spec_guard_" + case
        rpc = ObservedTransport(); pipeline = retro.Pipeline(config, rpc)
        pending = asyncio.run(pipeline.decision(work_id, proposal2("propose")))
        qid = pending["attempts"][0]["question_id"]
        receipts = decision_bytes(config)
        if case == "excluded":
            pipeline = retro.Pipeline(replace(config, self_assignment_exclusions=[work_id]), rpc)
        elif case == "completed":
            path = move_fixture(path, "completed")
        else:
            path.unlink()
        before = path.read_bytes() if path.exists() else None
        rpc.prompt_calls.clear()
        with pytest.raises(ValueError, match="unknown_work_id" if case == "missing" else "refused: use resolved or duplicate"):
            asyncio.run(pipeline.decision(work_id, proposal2("authorized")))
        assert not rpc.prompt_calls and rpc.questions[qid]["state"] == "open"
        assert not rpc.sent and not rpc.spawns and decision_bytes(config) == receipts
        assert (path.read_bytes() if path.exists() else None) == before
