#!/usr/bin/env python3
"""Generate catalog/documents.json, catalog/collections.json, and
catalog/epics.json from memory frontmatter.

Iterates every in-scope memory MD via memory_config.iter_in_scope_paths,
parses frontmatter via memory_frontmatter.load_frontmatter, and writes
deterministic catalog files.

Collections are auto-discovered: any `collections/*.md` with `type: collection`
emits an entry whose `document_ids` is taken from the collection MD's
frontmatter `document_ids` field. Adding a new collection is therefore a
single drop-in file; no manual seed required.

Epics (`epics/<slug>.md`, `type: epic`) are auto-discovered the same way.
Each epic gets an entry in `catalog/epics.json` with its title, summary, the
list of member spec/work ids that reference it via the `epic:` frontmatter
field, and a rolled-up status computed from member states. Epic status is
**not** stored on the epic doc itself — it is a pure function of member
spec states. See the workspace contract § Target
State → Epic status for the rollup rules.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

# Local imports
sys.path.insert(0, str(Path(__file__).parent))
from memory_config import (
    ARCHIVE_PATH,
    CATALOG_PATH,
    COLLECTIONS_PATH,
    EPICS_PATH,
    PROJECT_LOCATION_KEYS,
    ROOT,
    iter_in_scope_paths,
    required_keys_for,
)
from memory_frontmatter import load_frontmatter


# The terminal work/ folders whose entries are partitioned into archive.json.
# Keyed on entry["path"] (the doc's actual relative_to(ROOT) location), NOT on
# frontmatter `source_path` (which goes stale) or frontmatter `status` (which
# can drift from the folder). The validator's folder-consistency lint uses the
# same physical-path notion, so "the work/ folder is the source of truth for
# lifecycle" is enforced consistently.
_ARCHIVE_PATH_PREFIXES = ("work/completed/", "work/deprecated/")

# Open (non-terminal) triage-tagged specs, rolled up for aging visibility.
# Deliberately stable fields only (no age_days): the file changes when the
# triage population changes, not on every cadence run; consumers compute age
# from created_at.
TRIAGE_OPEN_PATH = CATALOG_PATH.parent / "triage_open.json"


def build_triage_open_entries(entries: list, docs_by_id: dict) -> list:
    rollup = []
    for entry in entries:
        path = entry["path"]
        if not path.startswith("work/") or is_archive(entry):
            continue
        if entry.get("type") != "spec" or "triage" not in entry.get("tags", []):
            continue
        parts = path.split("/")
        if len(parts) < 4 or parts[-1] != "spec.md":
            continue
        rollup.append(
            {
                "id": entry["id"],
                "item": parts[2],
                "status": parts[1],
                "created_at": docs_by_id.get(entry["id"], {}).get("created_at"),
            }
        )
    rollup.sort(key=lambda e: (e["created_at"] or "", e["id"]))
    return rollup


def is_archive(entry: dict) -> bool:
    """True iff a catalog entry's doc physically lives in a terminal work/ folder."""
    return entry["path"].startswith(_ARCHIVE_PATH_PREFIXES)


def write_json_atomic(path: Path, data) -> None:
    """Serialize `data` to `path` atomically (temp file in the same dir + os.replace).

    A plain write can be observed half-complete by the memory-cadence sync; the
    catalog/archive split widens that window to two cross-consistent files, so each
    file is written then atomically renamed into place. No temp file is left behind.
    """
    text = json.dumps(data, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        # Best-effort cleanup so a failed run leaves no stray .tmp file.
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


OPTIONAL_KEYS = (
    "aliases",
    "priority",
    "role",
    "machine",
    "owner",
    "supersedes",
    "superseded_by",
    "completed_at",
    "kind",
    "relationship",
    "operates_bots",
    "operated_by",
    "epic",
)
# Note: `channels` is intentionally NOT propagated to the catalog. The
# canonical channel data lives only in the source `.md` file; agents
# must Read the contact doc for actual phone/email/handle values.
# Optional contact consumers can use catalog ids to locate source documents.


def build_catalog_entry(path: Path, metadata: dict) -> dict:
    doc_type = metadata.get("type", "")
    required = set(required_keys_for(doc_type))
    missing = required - metadata.keys()
    if missing:
        raise ValueError(
            f"{path}: missing required frontmatter keys {sorted(missing)}"
        )

    entry = {
        "id": metadata["id"],
        "path": path.relative_to(ROOT).as_posix(),
        "title": metadata["title"],
        "type": metadata["type"],
        "canonical": metadata["canonical"],
        "updated_at": metadata["updated_at"],
        "source_path": metadata["source_path"],
        "tags": metadata["tags"],
        "summary": metadata["summary"],
        "related": metadata["related"],
    }
    # `status` is required for every type except `epic` — epic status is
    # derived per § Epic status rollup, not stored on the doc.
    if "status" in metadata:
        entry["status"] = metadata["status"]

    for key in OPTIONAL_KEYS:
        if key in metadata:
            entry[key] = metadata[key]

    for key in sorted(PROJECT_LOCATION_KEYS):
        if key in metadata:
            entry[key] = metadata[key]

    return entry


def load_doc_safe(path: Path):
    """Load a doc's frontmatter without aborting the whole catalog build.

    Returns ``(metadata, None)`` on success, or ``(None, warning)`` when the
    file's frontmatter is malformed (unparseable YAML, missing/!mapping block,
    StrictLoader policy violation). A single bad doc must not block
    regeneration of the remaining workspace catalog.
    The caller collects warnings and continues.
    """
    try:
        return load_frontmatter(path), None
    except Exception as exc:  # noqa: BLE001 - intentional: any parse failure -> skip+warn
        return None, str(exc)


def build_collection_entry(path: Path, metadata: dict) -> dict:
    return {
        "id": metadata["id"],
        "path": path.relative_to(ROOT).as_posix(),
        "title": metadata["title"],
        "document_ids": list(metadata.get("document_ids", [])),
    }


def _source_path_matches_physical_path(path: Path, metadata: dict) -> bool:
    return metadata.get("source_path") == path.relative_to(ROOT).as_posix()


def _prefer_candidate_duplicate(existing: tuple, candidate: tuple) -> bool:
    """Whether a duplicate candidate should replace the existing kept doc."""
    _existing_entry, existing_metadata, existing_path = existing
    _candidate_entry, candidate_metadata, candidate_path = candidate
    existing_matches = _source_path_matches_physical_path(existing_path, existing_metadata)
    candidate_matches = _source_path_matches_physical_path(candidate_path, candidate_metadata)
    return candidate_matches and not existing_matches


def _duplicate_warning(doc_id: str, kept_path: Path, skipped_path: Path) -> str:
    return (
        f"{skipped_path}: duplicate document id {doc_id}; "
        f"kept {kept_path.relative_to(ROOT).as_posix()}"
    )


# Epic status rollup. The four states are mutually exclusive and exhaustive
# over all member configurations.
_IN_FLIGHT_MEMBER_STATUSES = frozenset({
    "analysis", "ready_for_dev", "in_progress", "needs_qa", "blocked",
})


def roll_up_epic_status(member_statuses: list) -> str:
    """Compute an epic's rolled-up status from its member spec statuses.

    Rules (positive definitions; mutually exclusive):
    - backlog    — no members, OR all members in {backlog, deprecated} with at
                   least one backlog. Nothing has progressed past backlog yet.
    - active     — any member in {analysis, ready_for_dev, in_progress,
                   needs_qa, blocked}, OR at least one completed AND at least
                   one backlog. Work is in flight, or work has shipped with
                   more queued.
    - completed  — all members in {completed, deprecated}, at least one
                   completed. Everything settled, at least one piece shipped.
    - deprecated — at least one member, all members in {deprecated}. Everything
                   settled, nothing shipped.
    """
    if not member_statuses:
        return "backlog"

    has_in_flight = any(s in _IN_FLIGHT_MEMBER_STATUSES for s in member_statuses)
    has_backlog = any(s == "backlog" for s in member_statuses)
    has_completed = any(s == "completed" for s in member_statuses)
    has_deprecated = any(s == "deprecated" for s in member_statuses)

    if has_in_flight or (has_completed and has_backlog):
        return "active"
    if has_backlog and not has_completed:
        # Only backlog (and possibly deprecated) members.
        return "backlog"
    if has_completed:
        # All members terminal, at least one shipped.
        return "completed"
    # All members deprecated.
    return "deprecated" if has_deprecated else "backlog"


def build_epic_entries(docs_by_id: dict, entries: list) -> list:
    """Build catalog/epics.json entries from a list of catalog entries.

    `docs_by_id` maps id→metadata for cross-referencing. Each `type: epic`
    doc gets one entry with its title, summary, members (sorted list of
    spec/work ids that point to it via the `epic:` frontmatter field), and a
    rolled-up status. Entries are sorted by epic id for determinism.
    """
    epics_by_id = {
        e["id"]: e for e in entries if e["type"] == "epic"
    }
    members_by_epic: dict = {epic_id: [] for epic_id in epics_by_id}
    for entry in entries:
        epic_id = entry.get("epic")
        if epic_id and epic_id in members_by_epic:
            members_by_epic[epic_id].append(entry["id"])

    epic_entries = []
    for epic_id in sorted(epics_by_id):
        epic = epics_by_id[epic_id]
        members = sorted(members_by_epic[epic_id])
        member_statuses = [
            docs_by_id[m].get("status") for m in members
            if docs_by_id.get(m, {}).get("status") is not None
        ]
        epic_entries.append({
            "id": epic_id,
            "path": epic["path"],
            "title": epic["title"],
            "summary": epic["summary"],
            "status": roll_up_epic_status(member_statuses),
            "members": members,
        })
    return epic_entries


def main() -> int:
    entries_by_id: dict = {}
    docs_by_id: dict = {}
    warnings: list = []

    for path, in_catalog in iter_in_scope_paths():
        if not in_catalog:
            continue
        metadata, warn = load_doc_safe(path)
        if warn is not None:
            warnings.append(warn)
            continue
        doc_id = metadata.get("id")
        try:
            entry = build_catalog_entry(path, metadata)
        except Exception as exc:  # noqa: BLE001 - skip+warn on a single bad doc
            warnings.append(str(exc))
            continue

        candidate = (entry, metadata, path)
        existing = entries_by_id.get(doc_id)
        if existing is not None:
            if _prefer_candidate_duplicate(existing, candidate):
                warnings.append(_duplicate_warning(doc_id, path, existing[2]))
                entries_by_id[doc_id] = candidate
                docs_by_id[doc_id] = metadata
            else:
                warnings.append(_duplicate_warning(doc_id, existing[2], path))
            continue

        entries_by_id[doc_id] = candidate
        docs_by_id[doc_id] = metadata

    entries = [entry for entry, _metadata, _path in entries_by_id.values()]
    collection_entries = [
        build_collection_entry(path, metadata)
        for _entry, metadata, path in entries_by_id.values()
        if metadata.get("type") == "collection"
    ]

    entries.sort(key=lambda entry: entry["id"])
    collection_entries.sort(key=lambda entry: entry["id"])
    # Epics and collections roll up over the UNION of all entries (active +
    # archive) — they must see terminal members. Compute them before the
    # active/archive partition; never pass a pre-partitioned list here.
    epic_entries = build_epic_entries(docs_by_id, entries)

    # Partition the (already id-sorted) entries into the active index and the
    # terminal archive; each sublist stays id-sorted because partitioning
    # preserves order.
    active_entries = [e for e in entries if not is_archive(e)]
    archive_entries = [e for e in entries if is_archive(e)]

    triage_open_entries = build_triage_open_entries(active_entries, docs_by_id)

    write_json_atomic(CATALOG_PATH, active_entries)
    write_json_atomic(ARCHIVE_PATH, archive_entries)
    write_json_atomic(COLLECTIONS_PATH, collection_entries)
    write_json_atomic(EPICS_PATH, epic_entries)
    write_json_atomic(TRIAGE_OPEN_PATH, triage_open_entries)

    print(
        f"Generated {len(active_entries)} active + {len(archive_entries)} archive "
        f"catalog entries, {len(collection_entries)} collections, "
        f"{len(epic_entries)} epics, {len(triage_open_entries)} open triage specs."
    )
    if warnings:
        print(
            f"WARNING: skipped {len(warnings)} malformed doc(s) "
            f"(excluded from the catalog):",
            file=sys.stderr,
        )
        for w in warnings:
            print(f"  - {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
