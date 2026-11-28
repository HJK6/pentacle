"""QA admission resolves spec identity off the Store thread, from one snapshot.

Regression for a daemon store-thread stall: every QA-flag admission walked the
whole work tree several times inside its write transaction.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

from _shared.specs_service import SpecsSubsystem
from test_qa_dispatch_counter import SPEC, send_msg, state


def _spec(root: Path, status: str, folder: str, declared: str | None) -> None:
    path = root / "work" / status / folder
    path.mkdir(parents=True)
    front = f"id: {declared}\n" if declared else ""
    (path / "spec.md").write_text(f"---\n{front}title: T\ntype: spec\nstatus: {status}\n---\n\nGoal.\n",
                                  encoding="utf-8")


def _service(root: Path) -> SpecsSubsystem:
    return SpecsSubsystem(memory_root=root, session_summaries=lambda: [],
                          changed_callback=lambda ids: None, debounce_s=0)


def test_batch_identities_match_single_resolver(tmp_path) -> None:
    _spec(tmp_path, "in_progress", "pentacle__alpha", "spec_pentacle__alpha")
    _spec(tmp_path, "ready_for_dev", "pentacle-web__beta", "spec_pentacle_web__beta")
    _spec(tmp_path, "in_progress", "pentacle__dup_one", "spec_pentacle__dup")
    _spec(tmp_path, "completed", "pentacle__dup_two", "spec_pentacle__dup")
    _spec(tmp_path, "in_progress", "pentacle__undeclared", None)
    _spec(tmp_path, "unlisted_bucket", "pentacle__hidden", "spec_pentacle__hidden")
    service = _service(tmp_path)
    ids = ["spec_pentacle__alpha", "pentacle__alpha", "pentacle-web__beta", "spec_pentacle_web__beta",
           "pentacle_web__beta", "spec_pentacle__dup", "pentacle__undeclared", "spec_pentacle__hidden",
           "spec_pentacle__missing", "", None, "spec_pentacle__alpha"]
    expected = {value: service.canonical_spec_identity(value) for value in ids}
    assert expected["pentacle__alpha"] == "spec_pentacle__alpha"
    assert expected["spec_pentacle__dup"] is None

    scans = []
    real = service._scan_folders_by_id
    service._scan_folders_by_id = lambda *a, **k: scans.append(1) or real(*a, **k)
    assert service.canonical_spec_identities(ids) == expected
    assert len(scans) <= 2


def test_qa_admission_never_walks_the_tree_on_the_store_thread(tmp_path, monkeypatch) -> None:
    _spec(tmp_path, "in_progress", SPEC.removeprefix("spec_"), SPEC)
    for i in range(4000):
        _spec(tmp_path, ("in_progress", "ready_for_dev", "completed")[i % 3],
              f"pentacle__filler_{i:04d}", f"spec_pentacle__filler_{i:04d}")
    service = _service(tmp_path)

    async def run() -> None:
        store, _, _, comms, _, _ = await state()
        try:
            store.set_spec_identity_resolver(service.canonical_spec_identity,
                                             service.canonical_spec_identities)
            walks = []
            real = service._scan_folders_by_id

            def scan(*a, **k):
                start = time.perf_counter()
                try:
                    return real(*a, **k)
                finally:
                    walks.append((threading.current_thread() is store._thread,
                                  time.perf_counter() - start))
            service._scan_folders_by_id = scan

            assert (await comms.send(send_msg("qa1")))["delivery"] == "landed"
            on_store = [seconds for is_store, seconds in walks if is_store]
            assert walks and not on_store, (len(walks), sum(on_store))
            # One catalog walk plus one fresh tree walk per admission call.
            assert len(walks) <= 4
        finally:
            store.stop()

    asyncio.run(run())


def test_warm_qa_send_reads_no_unbound_spec_bytes(tmp_path, monkeypatch, caplog) -> None:
    _spec(tmp_path, "in_progress", SPEC.removeprefix("spec_"), SPEC)
    _spec(tmp_path, "unknown_bucket", "pentacle__other", "spec_pentacle__other")
    service = _service(tmp_path)
    read = service._read_identity_input
    reads = []

    def capture(path, **kwargs):
        result = read(path, **kwargs)
        # Count the descriptor-bound read, including the inner call for a bound document.
        if path.name == "spec.md" and kwargs.get("directory_fd") is not None and result[1] is not None:
            reads.append(path.parent.name)
        return result

    monkeypatch.setattr(service, "_read_identity_input", capture)
    caplog.set_level("INFO", logger="_shared.specs_service")

    async def run():
        store, _, _, comms, _, _ = await state()
        try:
            store.set_spec_identity_resolver(service.canonical_spec_identity,
                                             service.canonical_spec_identities)
            assert (await comms.send(send_msg("qa1")))["delivery"] == "landed"
            assert "pentacle__other" in reads
            reads.clear()
            assert (await comms.send(send_msg("qa2")))["delivery"] == "landed"
            assert reads == [SPEC.removeprefix("spec_")]
        finally:
            store.stop()

    asyncio.run(run())
    messages = [record.getMessage() for record in caplog.records if record.getMessage().startswith("qa_spec_capture ")]
    assert len(messages) == 2
    for message, outcome in zip(messages, ("strict", "reuse"), strict=True):
        prefix, result, *timings = message.split()
        assert prefix == "qa_spec_capture" and result == f"outcome={outcome}"
        assert [field.split("=")[0] for field in timings] == ["executor_queue_ms", "capture_work_ms", "shared_wait_ms"]
        assert SPEC not in message and str(tmp_path) not in message
