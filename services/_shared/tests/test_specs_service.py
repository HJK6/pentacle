"""Public specs-subsystem tests for status, alias, and cache behavior."""
from __future__ import annotations

import json
import os
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import _shared.specs_service as specs_service_module
from _shared.specs_service import SpecsSubsystem


def _write_spec(folder: Path, *, status: str = "in_progress", title: str = "T") -> None:
    folder.mkdir(parents=True)
    (folder / "spec.md").write_text(textwrap.dedent(f"""\
        ---
        id: spec_{folder.name}
        title: {title}
        type: spec
        status: {status}
        ---

        ## Goal

        A goal.
        """), encoding="utf-8")
    (folder / "summary.md").write_text(textwrap.dedent(f"""\
        ---
        id: work_{folder.name}
        type: work
        status: {status}
        ---

        ## Next action

        Do X.
        """), encoding="utf-8")


def _make_subsystem(memory_root: Path) -> SpecsSubsystem:
    os.environ["PENTACLE_MEMORY_ROOT"] = str(memory_root)
    return SpecsSubsystem(session_summaries=lambda: [], changed_callback=lambda ids: None)


def _write_statuses(memory_root: Path, statuses):
    (memory_root / "work").mkdir(exist_ok=True)
    (memory_root / "work" / "statuses.json").write_text(
        json.dumps({"version": 1, "statuses": statuses}), encoding="utf-8"
    )


def test_load_statuses_fallback_when_missing(tmp_path):
    (tmp_path / "work").mkdir()
    s = _make_subsystem(tmp_path)
    statuses, order = s._load_statuses()
    assert len(statuses) >= 6
    # DEFAULT_STATUSES includes the transitional `active` entry.
    names = {x["name"] for x in statuses}
    assert {"backlog", "in_progress", "completed"}.issubset(names)
    assert order["completed"] in (6, 7)


def test_load_statuses_reads_file(tmp_path):
    _write_statuses(tmp_path, [
        {"name": "foo", "order": 1, "display_label": "Foo", "is_terminal": False},
        {"name": "bar", "order": 2, "display_label": "Bar", "is_terminal": True},
    ])
    s = _make_subsystem(tmp_path)
    statuses, order = s._load_statuses()
    assert [x["name"] for x in statuses] == ["foo", "bar"]
    assert order == {"foo": 1, "bar": 2}


def test_load_statuses_hot_reload_on_mtime_change(tmp_path):
    _write_statuses(tmp_path, [
        {"name": "alpha", "order": 1, "display_label": "Alpha", "is_terminal": False},
    ])
    s = _make_subsystem(tmp_path)
    statuses1, _ = s._load_statuses()
    assert [x["name"] for x in statuses1] == ["alpha"]
    # Pause to ensure mtime changes (filesystem timestamp resolution).
    time.sleep(0.05)
    _write_statuses(tmp_path, [
        {"name": "beta", "order": 1, "display_label": "Beta", "is_terminal": False},
    ])
    statuses2, _ = s._load_statuses()
    assert [x["name"] for x in statuses2] == ["beta"]


def test_statuses_payload_returns_independent_copy(tmp_path):
    _write_statuses(tmp_path, [
        {"name": "a", "order": 1, "display_label": "A", "is_terminal": False},
    ])
    s = _make_subsystem(tmp_path)
    payload = s.statuses_payload()
    payload[0]["name"] = "MUTATED"
    payload2 = s.statuses_payload()
    # Cached value must be unchanged by the caller's mutation.
    assert payload2[0]["name"] == "a"


def test_list_specs_includes_epics_catalog_with_member_rows(tmp_path):
    _write_statuses(tmp_path, [
        {"name": "in_progress", "order": 1, "display_label": "In Progress", "is_terminal": False},
        {"name": "completed", "order": 2, "display_label": "Completed", "is_terminal": True},
    ])
    folder = tmp_path / "work" / "in_progress" / "example__demo"
    _write_spec(folder, status="in_progress", title="Demo")
    (tmp_path / "catalog").mkdir()
    (tmp_path / "catalog" / "epics.json").write_text(json.dumps([
        {
            "id": "epic_demo",
            "path": "epics/demo.md",
            "title": "Demo Epic",
            "summary": "Group of dashboard specs.",
            "status": "active",
            "members": ["spec_example__demo", "work_example__demo_in_progress", "spec_missing__x"],
        }
    ]), encoding="utf-8")
    s = _make_subsystem(tmp_path)
    payload = s.list_specs({})
    assert len(payload["epics"]) == 1
    epic = payload["epics"][0]
    assert epic["id"] == "epic_demo"
    assert epic["status"] == "active"
    assert epic["members"] == ["spec_example__demo", "work_example__demo_in_progress", "spec_missing__x"]
    assert [row["spec_id"] for row in epic["member_rows"]] == ["example__demo"]
    assert epic["member_counts"] == {"in_progress": 1}


def test_work_dirs_surface_unknown_status_folders(tmp_path):
    _write_statuses(tmp_path, [
        {"name": "in_progress", "order": 1, "display_label": "In Progress", "is_terminal": False},
    ])
    # Folder NOT in statuses.json should still surface.
    (tmp_path / "work" / "some_typo").mkdir()
    (tmp_path / "work" / "in_progress").mkdir()
    s = _make_subsystem(tmp_path)
    names = [p.name for p in s._work_dirs()]
    assert "in_progress" in names
    assert "some_typo" in names


def test_prefixed_document_id_resolves_to_work_folder(tmp_path):
    _write_statuses(tmp_path, [
        {"name": "in_progress", "order": 1, "display_label": "In Progress", "is_terminal": False},
    ])
    folder_id = "example__prefixed_doc"
    _write_spec(tmp_path / "work" / "in_progress" / folder_id, status="in_progress")
    s = _make_subsystem(tmp_path)

    assert s.resolution_for(f"spec_{folder_id}") == "resolved"
    assert s.resolution_for(f"work_{folder_id}") == "resolved"
    assert s.get_spec(f"spec_{folder_id}")["parsed"]["spec_id"] == folder_id


def test_declared_frontmatter_id_resolves_to_hyphenated_folder(tmp_path):
    _write_statuses(tmp_path, [
        {"name": "in_progress", "order": 1, "display_label": "In Progress", "is_terminal": False},
    ])
    folder = tmp_path / "work" / "in_progress" / "example__hyphen-folder"
    _write_spec(folder, status="in_progress")
    (folder / "spec.md").write_text(
        (folder / "spec.md").read_text(encoding="utf-8").replace(f"id: spec_{folder.name}", "id: spec_declared__underscore_folder"),
        encoding="utf-8",
    )
    s = _make_subsystem(tmp_path)

    assert s.resolution_for("spec_declared__underscore_folder") == "resolved"
    assert s.get_spec("spec_declared__underscore_folder")["parsed"]["spec_id"] == "example__hyphen-folder"


def test_duplicate_declared_ids_return_typed_ambiguity(tmp_path):
    _write_statuses(tmp_path, [
        {"name": "in_progress", "order": 1, "display_label": "In Progress", "is_terminal": False},
    ])
    folders = [
        tmp_path / "work" / "in_progress" / "example__first",
        tmp_path / "work" / "in_progress" / "example__second",
    ]
    for folder in folders:
        _write_spec(folder, status="in_progress")
        (folder / "spec.md").write_text(
            (folder / "spec.md").read_text(encoding="utf-8").replace(f"id: spec_{folder.name}", "id: spec_duplicate"),
            encoding="utf-8",
        )
    s = _make_subsystem(tmp_path)

    assert s.resolution_for("spec_duplicate") == "multiple_matches"
    result = s.get_spec("spec_duplicate")
    assert result["error"] == "spec_multiple_matches"
    assert result["error_code"] == "spec_id_ambiguous"
    assert len(result["candidates"]) == 2


def test_declared_id_wins_over_slug_alias_collision(tmp_path):
    _write_statuses(tmp_path, [
        {"name": "in_progress", "order": 1, "display_label": "In Progress", "is_terminal": False},
    ])
    declared = tmp_path / "work" / "in_progress" / "example__declared-owner"
    slug_alias = tmp_path / "work" / "in_progress" / "spec_declared__owner"
    _write_spec(declared, status="in_progress")
    _write_spec(slug_alias, status="in_progress")
    (declared / "spec.md").write_text(
        (declared / "spec.md").read_text(encoding="utf-8").replace(f"id: spec_{declared.name}", "id: spec_declared__owner"),
        encoding="utf-8",
    )
    (slug_alias / "spec.md").write_text(
        (slug_alias / "spec.md").read_text(encoding="utf-8").replace(f"id: spec_{slug_alias.name}", "id: spec_other"),
        encoding="utf-8",
    )
    s = _make_subsystem(tmp_path)

    assert s.get_spec("spec_declared__owner")["parsed"]["spec_id"] == "example__declared-owner"


def test_status_card_presentation_cache_reuses_scan_and_metadata_then_invalidates(tmp_path, monkeypatch):
    _write_statuses(tmp_path, [
        {"name": "in_progress", "order": 1, "display_label": "In Progress", "is_terminal": False},
    ])
    folder_id = "example__cached_status_card"
    folder = tmp_path / "work" / "in_progress" / folder_id
    _write_spec(folder, status="in_progress", title="Cached")
    s = _make_subsystem(tmp_path)
    s.push_enabled = True

    scan_calls = 0
    real_scan = s._scan_folders_by_id

    def counted_scan():
        nonlocal scan_calls
        scan_calls += 1
        return real_scan()

    parse_calls = 0
    real_parse = specs_service_module.parse_work_folder

    def counted_parse(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return real_parse(*args, **kwargs)

    monkeypatch.setattr(s, "_scan_folders_by_id", counted_scan)
    monkeypatch.setattr(specs_service_module, "parse_work_folder", counted_parse)

    assert s.resolution_for(folder_id) == "resolved"
    assert s.resolution_for(f"spec_{folder_id}") == "resolved"
    first = s.status_card_metadata(folder_id)
    assert first["parsed"]["status"] == "in_progress"
    first["parsed"]["title"] = "mutated"
    assert s.status_card_metadata(f"spec_{folder_id}")["parsed"]["title"] == "Cached"
    assert (scan_calls, parse_calls) == (1, 1)

    s._invalidate_presentation_cache()
    assert s.resolution_for(folder_id) == "resolved"
    assert s.status_card_metadata(folder_id)["parsed"]["title"] == "Cached"
    assert (scan_calls, parse_calls) == (2, 2)


def test_specs_watcher_event_invalidates_status_card_presentation_cache(tmp_path):
    _write_statuses(tmp_path, [
        {"name": "in_progress", "order": 1, "display_label": "In Progress", "is_terminal": False},
    ])
    folder_id = "example__watcher_cache"
    folder = tmp_path / "work" / "in_progress" / folder_id
    _write_spec(folder, status="in_progress", title="Before")
    s = _make_subsystem(tmp_path)
    s.push_enabled = True
    assert s.resolution_for(folder_id) == "resolved"
    assert s.status_card_metadata(folder_id)["parsed"]["title"] == "Before"

    class Timer:
        daemon = False

        def cancel(self):
            return None

        def start(self):
            return None

    s._timer_factory = lambda *_args: Timer()
    s._on_fs_event(SimpleNamespace(src_path=str(folder / "spec.md"), dest_path=None))
    assert s._folder_index_cache is None
    assert s._status_card_metadata_cache == {}


def test_prefixed_live_session_id_matches_existing_spec_row(tmp_path):
    _write_statuses(tmp_path, [
        {"name": "in_progress", "order": 1, "display_label": "In Progress", "is_terminal": False},
    ])
    folder_id = "example__live_prefixed"
    _write_spec(tmp_path / "work" / "in_progress" / folder_id, status="in_progress")
    os.environ["PENTACLE_MEMORY_ROOT"] = str(tmp_path)
    s = SpecsSubsystem(
        session_summaries=lambda: [
            {
                "stream_id": "hosta:codex-prefixed",
                "spec_id": f"spec_{folder_id}",
                "spec_ids": [f"spec_{folder_id}"],
                "online": True,
                "parent_stream_id": None,
                "visibility": "default",
                "provider": "codex",
                "host": "hosta",
                "last_event_at": "2026-07-06T00:00:00Z",
            }
        ],
        changed_callback=lambda ids: None,
    )

    payload = s.list_specs({})
    rows = [row for row in payload["specs"] if row["spec_id"] == folder_id]
    assert len(rows) == 1
    assert rows[0]["live_leaders"][0]["stream_id"] == "hosta:codex-prefixed"


def test_parse_folder_emits_status(tmp_path):
    _write_statuses(tmp_path, [
        {"name": "in_progress", "order": 1, "display_label": "In Progress", "is_terminal": False},
        {"name": "completed", "order": 2, "display_label": "Completed", "is_terminal": True},
    ])
    folder = tmp_path / "work" / "in_progress" / "foo__bar"
    _write_spec(folder, status="in_progress")
    s = _make_subsystem(tmp_path)
    row = s._parse_folder("in_progress", folder)
    assert row["status"] == "in_progress"
    # Consumers read `status`; the shared parser may still include the legacy
    # `lifecycle` alias for compatibility.
    if "lifecycle" in row:
        assert row["lifecycle"] == "in_progress"
    assert row["status_unknown"] is False
    assert row["frontmatter_drift"] is False
    assert row["terminal_state_drift"] is False


def test_parse_folder_terminal_state_drift(tmp_path):
    _write_statuses(tmp_path, [
        {"name": "in_progress", "order": 1, "display_label": "In Progress", "is_terminal": False},
        {"name": "completed", "order": 2, "display_label": "Completed", "is_terminal": True},
    ])
    # Folder is non-terminal but spec frontmatter says completed —
    # preserves the original drift rule (b).
    folder = tmp_path / "work" / "in_progress" / "x__y"
    _write_spec(folder, status="completed")
    s = _make_subsystem(tmp_path)
    row = s._parse_folder("in_progress", folder)
    assert row["frontmatter_drift"] is True
    assert row["terminal_state_drift"] is True


def test_parse_folder_status_unknown_surfaced(tmp_path):
    _write_statuses(tmp_path, [
        {"name": "in_progress", "order": 1, "display_label": "In Progress", "is_terminal": False},
    ])
    folder = tmp_path / "work" / "some_typo" / "x__y"
    _write_spec(folder, status="some_typo")
    s = _make_subsystem(tmp_path)
    row = s._parse_folder("some_typo", folder)
    assert row["status"] == "some_typo"
    assert row["status_unknown"] is True


def test_canonical_uses_statuses_order(tmp_path):
    _write_statuses(tmp_path, [
        {"name": "active", "order": 3, "display_label": "Active", "is_terminal": False},
        {"name": "in_progress", "order": 4, "display_label": "In Progress", "is_terminal": False},
        {"name": "completed", "order": 7, "display_label": "Completed", "is_terminal": True},
    ])
    s = _make_subsystem(tmp_path)
    # Same spec_id in two buckets; lower order wins.
    matches = [
        ("in_progress", tmp_path / "work" / "in_progress" / "x"),
        ("active", tmp_path / "work" / "active" / "x"),
    ]
    chosen = s._canonical(matches)
    assert chosen[0] == "active"  # order 3 beats order 4


def test_canonical_unknown_status_sorts_last(tmp_path):
    _write_statuses(tmp_path, [
        {"name": "completed", "order": 7, "display_label": "Completed", "is_terminal": True},
    ])
    s = _make_subsystem(tmp_path)
    matches = [
        ("some_typo", tmp_path / "work" / "some_typo" / "x"),
        ("completed", tmp_path / "work" / "completed" / "x"),
    ]
    chosen = s._canonical(matches)
    assert chosen[0] == "completed"  # known order 7 beats unknown +inf


def test_apply_filters_accepts_status_and_lifecycle_keys(tmp_path):
    (tmp_path / "work").mkdir()
    s = _make_subsystem(tmp_path)
    rows = [
        {"spec_id": "a", "status": "in_progress", "lifecycle": "in_progress", "repo": "x", "machine": None},
        {"spec_id": "b", "status": "completed", "lifecycle": "completed", "repo": "x", "machine": None},
    ]
    by_status = s._apply_filters(rows, {"status": "in_progress"})
    assert [r["spec_id"] for r in by_status] == ["a"]
    by_lifecycle = s._apply_filters(rows, {"lifecycle": "completed"})
    assert [r["spec_id"] for r in by_lifecycle] == ["b"]
