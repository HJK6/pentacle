from __future__ import annotations
"""Shared configuration for memory validator and catalog generator.

Single source of truth for which directories the validator and generator
scan, which files are excluded, the per-type required-key map, and the
in-scope path iterator.

Both `validate_memory_v2.py` and `generate_catalog.py` import from here.
This module is config-only — parsing lives in `memory_frontmatter.py` and
`strict_memory_loader.py`. The generator no longer imports from the
validator; there is no circular dependency between these modules.
"""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def is_syncthing_transient(name: str) -> bool:
    """Recognize temporary staging names used by optional Syncthing replication.

    Never memory content: excluded from document discovery here and from the
    validator's work-folder scans. Real `*.sync-conflict-*` files are NOT
    transient and stay findings.
    """
    return (
        name.startswith(".syncthing.")
        or name.startswith("~syncthing")
        or name in (".stfolder", ".stversions")
    )

CATALOG_PATH = ROOT / "catalog" / "documents.json"
# Terminal work items (docs physically under work/completed/ or work/deprecated/)
# are partitioned out of documents.json into this archive index so the default
# lookup path doesn't carry dead weight. See the workspace contract
# (catalog partitioning).
ARCHIVE_PATH = ROOT / "catalog" / "archive.json"
COLLECTIONS_PATH = ROOT / "catalog" / "collections.json"
EPICS_PATH = ROOT / "catalog" / "epics.json"
STATUSES_PATH = ROOT / "work" / "statuses.json"
CATALOG_STALE_WARNING_SECONDS = 60 * 60

# Fallback work-folder statuses when statuses.json is missing or invalid.
# The transitional active name supports older workspaces.
_DEFAULT_WORK_STATUSES = (
    "backlog", "ready_for_dev", "active", "in_progress",
    "needs_qa", "blocked", "completed",
)


def _work_status_names() -> tuple[str, ...]:
    """Read status folder names from work/statuses.json.

    Falls back to the day-1 default list if the file is missing or invalid.
    """
    try:
        data = json.loads(STATUSES_PATH.read_text(encoding="utf-8"))
        names = tuple(s["name"] for s in data.get("statuses", []) if "name" in s)
        return names or _DEFAULT_WORK_STATUSES
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError):
        return _DEFAULT_WORK_STATUSES


def _work_globs() -> tuple[str, ...]:
    out: list[str] = []
    for name in _work_status_names():
        out.append(f"{name}/*/summary.md")
        out.append(f"{name}/*/spec.md")
    return tuple(out)

# Per-directory scan rules.
# - patterns: globs to include (relative to the directory)
# - exclude:  filenames (relative to the directory) to skip
# - in_catalog: if False, files are validated but not required to appear in catalog
SOURCE_DIRS = {
    ROOT / "docs": {
        "in_catalog": True,
        "patterns": ("**/*.md",),
        "exclude": (),
    },
    ROOT / "agents": {
        "in_catalog": True,
        "patterns": ("**/*.md",),
        "exclude": (),
    },
    ROOT / "collections": {
        "in_catalog": True,
        "patterns": ("*.md",),
        "exclude": (),
    },
    ROOT / "startup": {
        "in_catalog": True,
        "patterns": ("*.md",),
        "exclude": ("AGENTS.template.md",),  # Scaffold, like templates/
    },
    ROOT / "tests": {
        "in_catalog": False,
        "patterns": ("*.md",),
        "exclude": (),
    },
    ROOT / "work": {
        "in_catalog": True,
        # Glob patterns derive from work/statuses.json (single source of
        # truth). Adding a new status to the JSON also extends this list on
        # next validator run without code changes.
        "patterns": _work_globs(),
        "exclude": ("statuses.json",),
    },
    ROOT / "epics": {
        "in_catalog": True,
        "patterns": ("*.md",),
        "exclude": ("README.md",),
    },
}

# Root-level and key subdirectory README docs (treated like a SOURCE_DIRS
# entry but each is a single file). work/README.md carries id meta_work_layout
# which is referenced by many other docs.
ROOT_DOCS = (
    ROOT / "MEMORY.md",
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    ROOT / "work" / "README.md",
)

# Excluded entirely from validation (scaffolds and project-level pointers).
EXCLUDED_PATHS = (
    ROOT / "templates" / "document.md",
    ROOT / "CLAUDE.md",
)

# Per-document-type required frontmatter keys.
# Common required core for every cataloged type. `status` is added on top
# for every type *except* `epic` — epic status is derived from member spec
# states by the catalog generator and is not stored on the epic doc.
COMMON_REQUIRED_KEYS = (
    "id", "title", "type", "canonical",
    "created_at", "updated_at", "source_path",
    "tags", "summary", "related",
)

# Per-type extras (added on top of COMMON_REQUIRED_KEYS + `status`).
TYPE_REQUIRED_EXTRAS = {
    "spec":         ("machine", "owner"),
    "work":         ("machine", "owner"),
    "agent-rules":  ("role",),
    "project":      (),  # PROJECT_LOCATION_KEYS still enforced as today's custom rule
}


def required_keys_for(doc_type: str) -> tuple[str, ...]:
    """Return the required-key tuple for a given document type."""
    if doc_type == "epic":
        return COMMON_REQUIRED_KEYS
    extras = TYPE_REQUIRED_EXTRAS.get(doc_type, ())
    return COMMON_REQUIRED_KEYS + ("status",) + extras


# Project docs additionally require all four location keys present (empty arrays acceptable).
# Custom Python check; Spec A consolidates into schema as if/then.
PROJECT_LOCATION_KEYS = ("repos", "non_repo_paths", "specs", "logs")


def iter_in_scope_paths():
    """Yield (path, in_catalog) for every memory MD file in scope.

    Single source of truth for which files the validator and generator
    walk. Honors per-directory glob patterns, exclusions, and the
    in_catalog flag.
    """
    seen = set()
    for source_dir, rules in SOURCE_DIRS.items():
        if not source_dir.exists():
            continue
        excludes = set(rules.get("exclude", ()))
        for pattern in rules.get("patterns", ("**/*.md",)):
            for path in sorted(source_dir.glob(pattern)):
                if not path.is_file():
                    continue
                if path in EXCLUDED_PATHS:
                    continue
                # A transient dir mid-sync can hold real-named files (e.g.
                # work/<status>/.stversions/spec.md) that match the globs.
                if any(is_syncthing_transient(part) for part in path.relative_to(source_dir).parts):
                    continue
                rel_to_dir = path.relative_to(source_dir).as_posix()
                if path.name in excludes or rel_to_dir in excludes:
                    continue
                if path in seen:
                    continue
                seen.add(path)
                yield path, rules.get("in_catalog", True)
    for path in ROOT_DOCS:
        if path.exists() and path not in EXCLUDED_PATHS and path not in seen:
            seen.add(path)
            yield path, True


def newest_work_catalog_source_mtime() -> float | None:
    """Newest mtime among catalogable work source docs."""
    newest = None
    for path, in_catalog in iter_in_scope_paths():
        if not in_catalog:
            continue
        try:
            rel = path.relative_to(ROOT).parts
        except ValueError:
            continue
        if not rel or rel[0] != "work":
            continue
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            continue
        newest = mtime if newest is None else max(newest, mtime)
    return newest


def catalog_staleness_warning(
    catalog_path: Path = CATALOG_PATH,
    threshold_seconds: int = CATALOG_STALE_WARNING_SECONDS,
) -> str | None:
    """Return a warning when the catalog is older than work source docs."""
    try:
        catalog_mtime = catalog_path.stat().st_mtime
    except FileNotFoundError:
        return f"WARNING: {catalog_path.relative_to(ROOT).as_posix()} is missing; results may be incomplete."

    source_mtime = newest_work_catalog_source_mtime()
    if source_mtime is None:
        return None
    lag_seconds = source_mtime - catalog_mtime
    if lag_seconds <= threshold_seconds:
        return None
    lag_minutes = int(lag_seconds // 60)
    return (
        f"WARNING: {catalog_path.relative_to(ROOT).as_posix()} is stale by "
        f"about {lag_minutes} minute(s) relative to work/ source docs; "
        "results may be incomplete; run scripts/generate_catalog.py in this workspace."
    )
