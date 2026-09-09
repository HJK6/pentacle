"""Frozen L6b session binding and test-attestation regression coverage."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

import pytest

SERVICE_DIR = Path(__file__).resolve().parents[1]

from server import Server  # noqa: E402
from sessions import Sessions, VerbError  # noqa: E402
from spawnctl import SpawnCtl  # noqa: E402
from store import Store, normalize_spec_ids  # noqa: E402
from uiverbs import UIVerbs  # noqa: E402
from ledger import Ledger  # noqa: E402
pytest.importorskip("specs_parser", reason="requires the shared specs parser")
from specs_parser import DEFAULT_STATUSES  # noqa: E402
from _shared.specs_service import SpecsSubsystem  # noqa: E402


SPEC_ID = "example-spec-new"
CANONICAL_SPEC_ID = f"spec_{SPEC_ID}"
SYNTHETIC_STATUS = "release_candidate"
CONFIGURED_STATUS_NAMES = tuple(status["name"] for status in DEFAULT_STATUSES) + (
    SYNTHETIC_STATUS,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        ([" example-topic ", "spec-topic"], ["spec-topic"]),
        (["spec-topic", "spec_spec-topic"], ["spec-topic"]),
        (["", "  ", None], []),
    ),
)
def test_spec_binding_normalization_uses_one_canonical_identity(
    raw: list[object], expected: list[str],
) -> None:
    assert normalize_spec_ids(raw) == expected


def test_v2_catalog_import_does_not_expose_v1_service_path() -> None:
    v1_service_dir = str(SERVICE_DIR.parent / "chat-stream")
    assert v1_service_dir not in sys.path
    assert SpecsSubsystem.__module__ == "_shared.specs_service"


class Catalog:
    def resolution_for(self, spec_id: str | None) -> str | None:
        return "resolved" if self.equivalent_spec_ids(spec_id, SPEC_ID) else "zero_matches"

    @staticmethod
    def equivalent_spec_ids(left: str | None, right: str | None) -> bool:
        def identity(value: str | None) -> str:
            return str(value or "").removeprefix("spec_")

        return bool(left and right and identity(left) == identity(right))

    @staticmethod
    def canonical_spec_identity(spec_id: str | None) -> str | None:
        value = str(spec_id or "").removeprefix("spec_")
        return f"spec_{value}" if value else None


class SpawnTmux:
    def __init__(self) -> None:
        self.live: set[str] = set()

    async def new_session(self, name: str, command: str, cwd: str | None = None, env: dict[str, str] | None = None) -> None:
        self.live.add(name)

    async def has_session(self, name: str) -> bool:
        return name in self.live

    async def session_state(self, name: str) -> str:
        return "alive" if name in self.live else "gone"

    async def capture(self, name: str) -> str:
        return "READY\n"

    async def pane_pid(self, name: str) -> str:
        return "4321"

    async def kill_session(self, name: str) -> None:
        self.live.discard(name)

    async def run(self, *args: str, **kwargs: object) -> tuple[int, str]:
        return 0, ""


def _write_spec(folder: Path, spec_id: str, *, declared_id: str | None = None) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "spec.md").write_text(
        "---\n"
        f"id: {declared_id or spec_id}\n"
        f"title: {spec_id}\n"
        f"status: {folder.parent.name}\n"
        "---\n",
        encoding="utf-8",
    )


def _write_statuses(root: Path, names: tuple[str, ...] = CONFIGURED_STATUS_NAMES) -> None:
    work_root = root / "work"
    work_root.mkdir(parents=True, exist_ok=True)
    statuses = [
        {
            "name": name,
            "order": order,
            "display_label": name.replace("_", " ").title(),
            "is_terminal": name in {"completed", "deprecated"},
        }
        for order, name in enumerate(names, start=1)
    ]
    (work_root / "statuses.json").write_text(
        json.dumps({"version": 2, "statuses": statuses}), encoding="utf-8",
    )


def _stale_catalog(root: Path, monkeypatch: pytest.MonkeyPatch) -> SpecsSubsystem:
    if not (root / "work" / "statuses.json").is_file():
        _write_statuses(root)
    monkeypatch.setenv("PENTACLE_MEMORY_ROOT", str(root))
    catalog = SpecsSubsystem(session_summaries=lambda: [], changed_callback=lambda _ids: None)
    catalog.push_enabled = True
    return catalog


@pytest.mark.parametrize("status", CONFIGURED_STATUS_NAMES)
def test_live_spawn_scan_resolves_every_status_from_statuses_json(
    status: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "memory"
    _write_statuses(root)
    spec_id = f"example-status-{status}"
    _write_spec(root / "work" / status / spec_id, spec_id)
    catalog = _stale_catalog(root, monkeypatch)
    catalog._folder_index_cache = {}

    resolution = catalog.resolve_for_spawn(spec_id)

    assert resolution["resolution"] == "resolved"
    assert resolution["tree_resolution"] == "resolved"
    assert resolution["tree_candidates"] == [f"work/{status}/{spec_id}/spec.md"]


def test_peer_needs_qa_folder_and_declared_id_spellings_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "memory"
    _write_statuses(root)
    folder_id = "provider-capabilities"
    declared_id = "spec-provider-capabilities"
    _write_spec(
        root / "work" / "needs_qa" / folder_id,
        folder_id,
        declared_id=declared_id,
    )
    catalog = _stale_catalog(root, monkeypatch)

    resolution = catalog.resolve_for_spawn(folder_id)

    assert resolution["resolution"] == "resolved"
    assert resolution["tree_resolution"] == "resolved"
    assert resolution["tree_candidates"] == [
        f"work/needs_qa/{folder_id}/spec.md",
    ]


def test_resolver_uses_declared_document_identity_without_folded_collisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "memory"
    _write_statuses(root)
    folder_alias = "process-doc"
    canonical_id = "spec-process-doc"
    _write_spec(
        root / "work" / "in_progress" / folder_alias,
        folder_alias,
        declared_id=canonical_id,
    )
    folded_left = "spec-legal-a__identity"
    folded_right = "spec-legal-b__identity"
    _write_spec(
        root / "work" / "analysis" / "legal-alpha__identity",
        "legal-alpha__identity",
        declared_id=folded_left,
    )
    _write_spec(
        root / "work" / "blocked" / "legal_alpha__identity",
        "legal_alpha__identity",
        declared_id=folded_right,
    )
    catalog = _stale_catalog(root, monkeypatch)

    assert catalog.canonical_spec_identity(f"spec_{folder_alias}") == canonical_id
    assert catalog.canonical_spec_identity(canonical_id) == canonical_id
    assert catalog.resolve_for_spawn(folded_left)["resolution"] == "resolved"
    assert catalog.resolve_for_spawn(folded_right)["resolution"] == "resolved"
    assert catalog.canonical_spec_identity(folded_left) != catalog.canonical_spec_identity(folded_right)


@pytest.mark.parametrize(
    "requested_id",
    [
        "provider-capabilities",
        "spec-provider-capabilities",
    ],
)
def test_equivalent_declared_id_ambiguity_cannot_be_bypassed_by_spelling(
    requested_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "memory"
    _write_statuses(root)
    folder_id = "provider-capabilities"
    declared_id = "spec-provider-capabilities"
    _write_spec(
        root / "work" / "needs_qa" / folder_id,
        folder_id,
        declared_id=declared_id,
    )
    _write_spec(
        root / "work" / "blocked" / "duplicate-owner",
        folder_id,
        declared_id=declared_id,
    )
    catalog = _stale_catalog(root, monkeypatch)

    resolution = catalog.resolve_for_spawn(requested_id)

    assert resolution["resolution"] == "multiple_matches"
    assert resolution["tree_resolution"] == "multiple_matches"
    assert resolution["tree_candidates"] == [
        "work/blocked/duplicate-owner/spec.md",
        f"work/needs_qa/{folder_id}/spec.md",
    ]


def test_cross_host_resolution_uses_daemon_local_synced_memory_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def go() -> None:
        daemon_root = tmp_path / "hosta-memory"
        target_root = tmp_path / "hostc-memory"
        _write_statuses(daemon_root)
        _write_statuses(target_root)
        item = daemon_root / "work" / "in_progress" / SPEC_ID
        _write_spec(item, SPEC_ID)
        catalog = _stale_catalog(daemon_root, monkeypatch)
        catalog._folder_index_cache = {}

        # Model a target host with no local copy at the path known to this test.
        # The daemon's already-constructed subsystem must remain authoritative.
        monkeypatch.setenv("PENTACLE_MEMORY_ROOT", str(target_root))
        store = Store(":memory:")
        store.start()
        try:
            ctl = SpawnCtl(
                store, Sessions(store, local_host="hosta"), tmux=object(), specs=catalog,
            )
            binding = await ctl._resolve_spec_binding(
                {"spec_id": SPEC_ID}, "hostc", "cross-host",
            )
            assert binding["spec_resolution"] == "resolved"
            assert catalog.memory_root == daemon_root
        finally:
            store.stop()

    asyncio.run(go())


def test_session_binding_fields_round_trip_from_store() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            binding = {
                "spec_id": SPEC_ID,
                "provenance": "spawn_explicit",
                "granting_principal": "public-client",
                "granted_at": "2026-08-08T00:00:00Z",
            }
            opened = await store.open_session(
                "alpha",
                "bound",
                visibility="hidden",
                spec_id=SPEC_ID,
                spec_ids=[SPEC_ID],
                spec_resolution="resolved",
                qualified_spec_ids=[SPEC_ID],
                spec_binding_provenance=[binding],
            )
            fetched = await store.fetch_session("alpha", "bound")
            listed = (await store.list_sessions())[0]
            expected_binding = {**binding, "spec_id": CANONICAL_SPEC_ID}
            for row in (opened, fetched, listed):
                assert row["spec_id"] == CANONICAL_SPEC_ID
                assert row["spec_ids"] == [CANONICAL_SPEC_ID]
                assert row["qualified_spec_ids"] == [CANONICAL_SPEC_ID]
                assert row["spec_binding_provenance"] == [expected_binding]
                assert row["spec_resolution"] == "resolved"
        finally:
            store.stop()

    asyncio.run(go())


def test_unknown_spawn_spec_fails_before_reservation() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        sessions = Sessions(store, local_host="alpha")
        try:
            ctl = SpawnCtl(store, sessions, tmux=object(), specs=Catalog())
            with pytest.raises(VerbError) as raised:
                await ctl._resolve_spec_binding(
                    {"spec_id": "example-missing"},
                    "alpha",
                    "unknown",
                )
            assert raised.value.code == "spec_unresolved"
            assert await store.fetch_session("alpha", "unknown") is None
            assert await store.reservations() == []
        finally:
            store.stop()

    asyncio.run(go())


def test_fresh_spec_missing_from_cached_catalog_resolves_from_live_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def go() -> None:
        root = tmp_path / "memory"
        catalog = _stale_catalog(root, monkeypatch)
        catalog._folder_index_cache = {}
        item = root / "work" / "ready_for_dev" / SPEC_ID
        _write_spec(item, SPEC_ID)

        store = Store(":memory:")
        store.start()
        try:
            ctl = SpawnCtl(store, Sessions(store, local_host="alpha"), tmux=object(), specs=catalog)
            binding = await ctl._resolve_spec_binding({"spec_id": SPEC_ID}, "alpha", "fresh")
            assert binding["spec_resolution"] == "resolved"
        finally:
            store.stop()

    asyncio.run(go())


def test_peer_moved_spec_repro_ignores_nonexistent_cached_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def go() -> None:
        root = tmp_path / "memory"
        old = root / "work" / "analysis" / SPEC_ID
        new = root / "work" / "in_progress" / SPEC_ID
        # Model a cache refresh during the transient Syncthing overlap, then
        # convergence of the authoritative tree before another refresh.
        _write_spec(old, SPEC_ID)
        _write_spec(new, SPEC_ID)
        catalog = _stale_catalog(root, monkeypatch)
        assert catalog.resolution_for(SPEC_ID) == "multiple_matches"
        (old / "spec.md").unlink()
        old.rmdir()

        store = Store(":memory:")
        store.start()
        try:
            ctl = SpawnCtl(store, Sessions(store, local_host="alpha"), tmux=object(), specs=catalog)
            binding = await ctl._resolve_spec_binding({"spec_id": SPEC_ID}, "alpha", "moved")
            assert binding["spec_resolution"] == "resolved"
            resolution = catalog.resolve_for_spawn(SPEC_ID)
            assert resolution["source"] == "catalog"
            assert resolution["catalog_candidates"] == [f"work/in_progress/{SPEC_ID}/spec.md"]
            assert resolution["tree_candidates"] == [f"work/in_progress/{SPEC_ID}/spec.md"]
        finally:
            store.stop()

    asyncio.run(go())


def test_live_tree_wins_when_existing_cached_path_now_owns_another_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "memory"
    old = root / "work" / "analysis" / SPEC_ID
    new = root / "work" / "in_progress" / SPEC_ID
    _write_spec(old, SPEC_ID)
    catalog = _stale_catalog(root, monkeypatch)
    assert catalog.resolution_for(SPEC_ID) == "resolved"

    new.parent.mkdir(parents=True)
    old.rename(new)
    replacement_id = "example-analysis-replacement"
    _write_spec(old, replacement_id)

    resolution = catalog.resolve_for_spawn(SPEC_ID)

    assert resolution["resolution"] == "resolved"
    assert resolution["source"] == "work_tree"
    assert resolution["catalog_candidates"] == [f"work/analysis/{SPEC_ID}/spec.md"]
    assert resolution["tree_candidates"] == [f"work/in_progress/{SPEC_ID}/spec.md"]


def test_true_live_tree_ambiguity_errors_with_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def go() -> None:
        root = tmp_path / "memory"
        first = root / "work" / "analysis" / "first-folder"
        second = root / "work" / "blocked" / "second-folder"
        _write_spec(first, SPEC_ID)
        _write_spec(second, SPEC_ID)
        catalog = _stale_catalog(root, monkeypatch)
        catalog._folder_index_cache = {}

        store = Store(":memory:")
        store.start()
        try:
            ctl = SpawnCtl(store, Sessions(store, local_host="alpha"), tmux=object(), specs=catalog)
            with pytest.raises(VerbError) as raised:
                await ctl._resolve_spec_binding({"spec_id": SPEC_ID}, "alpha", "ambiguous")
            assert raised.value.code == "spec_unresolved"
            assert raised.value.extra["spec_catalog_resolution"] == "zero_matches"
            assert raised.value.extra["spec_tree_resolution"] == "multiple_matches"
            assert raised.value.extra["spec_candidates"] == [
                "work/analysis/first-folder/spec.md",
                "work/blocked/second-folder/spec.md",
            ]
            assert "catalog may be stale" in str(raised.value)
            assert "live work/ tree=multiple_matches" in str(raised.value)
        finally:
            store.stop()

    asyncio.run(go())


def test_unknown_spec_error_names_catalog_and_live_tree_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def go() -> None:
        root = tmp_path / "memory"
        (root / "work").mkdir(parents=True)
        catalog = _stale_catalog(root, monkeypatch)
        catalog._folder_index_cache = {}
        missing = "example-analysis"

        store = Store(":memory:")
        store.start()
        try:
            ctl = SpawnCtl(store, Sessions(store, local_host="alpha"), tmux=object(), specs=catalog)
            with pytest.raises(VerbError) as raised:
                await ctl._resolve_spec_binding({"spec_id": missing}, "alpha", "missing")
            assert raised.value.extra == {
                "spec_id": f"spec_{missing}",
                "spec_resolution": "zero_matches",
                "spec_catalog_resolution": "zero_matches",
                "spec_tree_resolution": "zero_matches",
                "spec_resolution_source": "work_tree",
                "spec_candidates": [],
            }
            assert "catalog may be stale" in str(raised.value)
            assert "regenerate the catalog" in str(raised.value)
        finally:
            store.stop()

    asyncio.run(go())


def test_cataloged_spawn_persists_binding_and_inspect_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def go() -> None:
        catalog_root = tmp_path / "memory"
        item = catalog_root / "work" / "analysis" / SPEC_ID
        item.mkdir(parents=True)
        (item / "spec.md").write_text(
            "---\n"
            f"id: {SPEC_ID}\n"
            "title: Disposable analysis example\n"
            "status: analysis\n"
            "---\n\n"
            "## Goal\n\nExercise durable spawn provenance.\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("PENTACLE_MEMORY_ROOT", str(catalog_root))
        catalog = SpecsSubsystem(session_summaries=lambda: [], changed_callback=lambda _ids: None)
        assert catalog.resolution_for(SPEC_ID) == "resolved"

        store = Store(":memory:")
        store.start()
        sessions = Sessions(store, local_host="alpha")
        tmux = SpawnTmux()
        ctl = SpawnCtl(store, sessions, tmux=tmux, specs=catalog)
        server = Server(store=store, sessions=sessions, spawnctl=ctl, local_host="alpha")
        try:
            (spawned,) = await server._dispatch(json.dumps({"objective": "Exercise the existing spawn contract",
                "type": "spawn",
                "command": "run",
                "session_name": "cataloged",
                "request_id": "spawn-cataloged",
                "spec_id": SPEC_ID,
            }))
            assert spawned["type"] == "spawn.ok"
            (inspected,) = await server._dispatch(json.dumps({
                "type": "inspect_stream",
                "stream_id": "alpha:cataloged",
                "event_tail": 0,
            }))
            session = inspected["session"]
            assert session["spec_id"] == CANONICAL_SPEC_ID
            assert session["spec_ids"] == [CANONICAL_SPEC_ID]
            assert session["qualified_spec_ids"] == [CANONICAL_SPEC_ID]
            assert session["spec_binding_provenance"][0]["provenance"] == "spawn_explicit"

            (rejected,) = await server._dispatch(json.dumps({"objective": "Exercise the existing spawn contract",
                "type": "spawn",
                "command": "run",
                "session_name": "unknown-catalog-item",
                "request_id": "spawn-unknown",
                "spec_id": "example-unknown",
            }))
            assert rejected["type"] == "spawn.error"
            assert rejected["error_code"] == "spec_unresolved"
            assert rejected["request_id"] == "spawn-unknown"
            assert await store.fetch_session("alpha", "unknown-catalog-item") is None
            assert await store.reservations() == []
        finally:
            store.stop()

    asyncio.run(go())


def test_spawn_materializes_resolver_canonical_binding_after_store_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def go() -> None:
        root = tmp_path / "memory"
        _write_statuses(root)
        folder_alias = "process-doc"
        canonical_id = "spec-process-doc"
        _write_spec(
            root / "work" / "in_progress" / folder_alias,
            folder_alias,
            declared_id=canonical_id,
        )
        catalog = _stale_catalog(root, monkeypatch)
        database = tmp_path / "sessions.db"
        store = Store(str(database))
        store.start()
        try:
            binding = await SpawnCtl(
                store, Sessions(store, local_host="alpha"), tmux=object(), specs=catalog,
            )._resolve_spec_binding({"spec_id": folder_alias}, "alpha", "canonical")
            assert binding["spec_id"] == canonical_id
            assert binding["spec_ids"] == [canonical_id]
            assert binding["qualified_spec_ids"] == [canonical_id]
            assert binding["spec_binding_provenance"][0]["spec_id"] == canonical_id
            await store.open_session("alpha", "canonical", visibility="hidden", **binding)
        finally:
            store.stop()

        reopened = Store(str(database))
        reopened.start()
        try:
            row = await reopened.fetch_session("alpha", "canonical")
            assert row is not None
            assert row["spec_ids"] == [canonical_id]
            assert row["qualified_spec_ids"] == [canonical_id]
            assert row["spec_binding_provenance"][0]["spec_id"] == canonical_id
        finally:
            reopened.stop()

    asyncio.run(go())


def test_attach_updates_inspect_and_ledger_agreement() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        sessions = Sessions(store, local_host="alpha")
        await sessions.open("alpha", "attach", visibility="hidden")
        catalog = Catalog()
        spawnctl = SpawnCtl(store, sessions, tmux=object(), specs=catalog)
        uiverbs = UIVerbs(store, sessions, spawnctl, specs=catalog)
        server = Server(store=store, sessions=sessions)
        server.handlers.update(uiverbs.wire_handlers())
        try:
            (attached,) = await server._dispatch(json.dumps({
                "type": "session.spec_update",
                "request_id": "attach-1",
                "action": "attach",
                "host": "alpha",
                "session_name": "attach",
                "spec_id": SPEC_ID,
            }))
            assert attached["type"] == "session.spec_update.ok"
            inspected = await server._on_inspect_stream({
                "stream_id": "alpha:attach", "event_tail": 0,
            })
            session = inspected["session"]
            stored = await store.fetch_session("alpha", "attach")
            assert session["spec_id"] == CANONICAL_SPEC_ID
            assert session["spec_ids"] == [CANONICAL_SPEC_ID]
            assert session["qualified_spec_ids"] == [CANONICAL_SPEC_ID]
            assert session["spec_binding_provenance"][0]["provenance"] == "operator_v2"
            assert stored is not None
            assert stored["spec_ids"] == session["spec_ids"]
            assert stored["spec_binding_provenance"] == session["spec_binding_provenance"]
        finally:
            store.stop()

    asyncio.run(go())


def test_report_qa_attestation_round_trips() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            await store.open_session("alpha", "worker", visibility="hidden")
            sessions = Sessions(store, local_host="alpha")
            attestation = {"stream_id": "hosta:review", "report_id": "review-123"}
            evidence = {
                "subject_sha": "0" * 40,
                "gate_evidence": {"focused": {"passed": 5, "failed": 0}},
            }
            await Ledger(store, sessions=sessions).ingest({
                "report_id": "subject-1",
                "from_stream_id": "alpha:worker",
                "msg_id": 7,
                "status": "done",
                "summary": "validated",
                "findings": [],
                "next_action": "none",
                "completion_kind": "implementation_ready",
                "qa_attestation": attestation,
                "details": evidence,
            })
            stored = await store.get_report("subject-1")
            assert stored["qa_attestation"] == attestation
            assert stored["details"] == evidence
        finally:
            store.stop()

    asyncio.run(go())
