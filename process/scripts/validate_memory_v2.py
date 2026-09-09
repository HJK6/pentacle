#!/usr/bin/env python3
"""Memory validator (v2).

Spec A: PyYAML + jsonschema enforcement. Spec 0: per-type required keys via
the schema, with cross-doc integrity (catalog presence, dangling related/
supersedes/superseded_by, collection references) handled in Python here.

Modes:
    default     — full validation: parser + schema + cross-doc + catalog
    --source-only — parser + schema + cross-doc (skips catalog presence/field
                    checks and collections.json reference checks; used on
                    replicated workspaces where the catalog may lag)
"""

import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

import jsonschema
import yaml

# Local imports
sys.path.insert(0, str(Path(__file__).parent))
from memory_config import (
    ARCHIVE_PATH,
    CATALOG_PATH,
    COLLECTIONS_PATH,
    PROJECT_LOCATION_KEYS,
    ROOT,
    is_syncthing_transient,
    iter_in_scope_paths,
    newest_work_catalog_source_mtime,
)
from memory_frontmatter import (
    SchemaInvalidError,
    load_frontmatter,
    load_schema,
    validate_frontmatter,
)
from strict_memory_loader import StrictLoaderError


SCHEMA_PATH = ROOT / "schema" / "document.schema.json"
NEXUS_DOMAIN_SCHEMA_PATH = ROOT / "schema" / "nexus-domain.schema.json"
NEXUS_DOMAINS_PATH = ROOT / "docs" / "config" / "nexus_domains"
STATUSES_PATH = ROOT / "work" / "statuses.json"

import re

_MACHINE_ID_RE = re.compile(r"^reference_machine_[a-z0-9_]+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Per the workspace contract: for type=spec and
# type=work documents, status: must match the folder name and be in the
# statuses.json set. Legacy `pending` on already-completed specs is tolerated
# with a warning so frontmatter from before the redesign still validates.
# (`superseded` was retired from the status enum entirely by
# the workspace contract; its four stragglers were
# migrated to `deprecated` at the same time.)
_LEGACY_STATUS_BACK_COMPAT = {"pending"}
_ALLOWED_WORK_DIRECT_FILES = {"spec.md", "summary.md", "triage.json"}
STALE_IN_PROGRESS_DAYS = 7

# Optional file replication can deliver a work item incrementally. Ignore known
# staging names and allow five minutes for incomplete new folders to settle.
# Actual sync-conflict files remain findings. This workspace does not require
# a replication service; the same checks also cover interrupted local writes.
SYNC_SETTLE_GRACE_SECONDS = 300
# Recently edited source documents may temporarily precede catalog regeneration.
# Report such disagreement as a warning for fifteen minutes, then as an error.
# Regenerate catalogs with scripts/generate_catalog.py after source changes.
CATALOG_LAG_GRACE_SECONDS = 900

# Shared with document discovery (memory_config.iter_in_scope_paths), which
# must also skip transient dirs holding real-named files mid-sync.
_is_syncthing_transient = is_syncthing_transient

# Closure condition 4 (## Retro) enforcement windows. Completions before the
# warn date are grandfathered; see the workspace contract.
RETRO_LINT_WARN_START = date(2026, 7, 18)
RETRO_LINT_ERROR_START = date(2026, 7, 25)
# Markdown allows up to 3 leading spaces before an ATX heading.
_RETRO_HEADER_RE = re.compile(r"^ {0,3}##\s+Retro\b")
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def _has_retro_header(text: str) -> bool:
    """True iff the doc has a real ## Retro heading outside fenced code blocks.

    CommonMark closure rules: a fence closes only on the same character with
    at least the opener's run length and nothing but that run on the line.
    """
    fence_char = None
    fence_len = 0
    for line in text.splitlines():
        if fence_char is None:
            m = _FENCE_OPEN_RE.match(line)
            # CommonMark: a backtick opener's info string may not contain a
            # backtick; such a line is ordinary text, not a fence opener.
            if m and not (m.group(1)[0] == "`" and "`" in line[m.end():]):
                fence_char = m.group(1)[0]
                fence_len = len(m.group(1))
                continue
            if _RETRO_HEADER_RE.match(line):
                return True
        elif re.match(rf"^ {{0,3}}{fence_char}{{{fence_len},}}\s*$", line):
            fence_char = None
            fence_len = 0
    return False


def _check_nexus_domains(docs: dict) -> list[str]:
    """Validate standing Nexus definitions and their cross-record graph."""
    errors: list[str] = []
    try:
        schema = json.loads(NEXUS_DOMAIN_SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
    except (OSError, json.JSONDecodeError, jsonschema.SchemaError) as exc:
        return [f"nexus domain schema load failed: {exc}"]
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    definitions: dict[str, tuple[Path, dict]] = {}
    alias_owners: dict[str, str] = {}
    for path in sorted(NEXUS_DOMAINS_PATH.glob("*.yaml")):
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError) as exc:
            errors.append(f"{path}: nexus domain parse failed: {exc}")
            continue
        if not isinstance(payload, dict):
            errors.append(f"{path}: nexus domain must be a mapping")
            continue
        validation_errors = sorted(validator.iter_errors(payload), key=lambda item: list(item.path))
        if validation_errors:
            errors.extend(f"{path}: {item.message}" for item in validation_errors)
            continue
        expires_at = payload.get("expires_at")
        if expires_at is not None:
            try:
                parsed_expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            except ValueError:
                errors.append(f"{path}: expires_at is not a real calendar timestamp")
                continue
            if parsed_expiry.tzinfo is None:
                errors.append(f"{path}: expires_at must include a timezone")
                continue
        domain_id = payload["id"]
        if path.stem != domain_id:
            errors.append(f"{path}: filename must match domain id {domain_id}")
        if domain_id in definitions:
            errors.append(f"{path}: duplicate nexus domain id {domain_id}")
        definitions[domain_id] = (path, payload)
        for alias in payload["aliases"]:
            if alias == domain_id:
                errors.append(f"{path}: alias duplicates canonical id {alias}")
            owner = alias_owners.get(alias)
            if owner is not None:
                errors.append(f"{path}: duplicate nexus domain alias {alias} (also {owner})")
            alias_owners[alias] = domain_id
    for domain_id, (path, payload) in definitions.items():
        if domain_id in alias_owners:
            errors.append(f"{path}: canonical id is also an alias: {domain_id}")
        parent_id = payload["parent_id"]
        if parent_id is not None and parent_id not in definitions:
            errors.append(f"{path}: unknown nexus parent {parent_id}")
        seen: set[str] = set()
        cursor: str | None = domain_id
        while cursor is not None and cursor in definitions:
            if cursor in seen:
                errors.append(f"{path}: nexus domain parent cycle")
                break
            seen.add(cursor)
            cursor = definitions[cursor][1]["parent_id"]
        for binding in payload["bindings"]:
            if binding["scope_type"] == "project_doc" and binding["scope_key"] not in docs:
                errors.append(f"{path}: project_doc binding not found: {binding['scope_key']}")
    return errors


def _load_statuses() -> tuple:
    """Return (status_names, transitional_statuses, live_statuses).

    `transitional_statuses` is the subset whose entry has `"transitional": true`;
    the lint treats frontmatter/folder mismatches on items in transitional
    folders as warnings rather than errors.

    `live_statuses` is the subset whose entry has `"is_terminal": false`; live
    work folder-shape checks derive from this so statuses.json remains the
    status single source of truth.

    Returns default workspace statuses if the file is missing or unreadable.
    """
    default_names = {"backlog", "ready_for_dev", "in_progress", "needs_qa", "blocked", "completed"}
    default_live = default_names - {"completed"}
    try:
        data = json.loads(STATUSES_PATH.read_text(encoding="utf-8"))
        statuses = data.get("statuses", [])
        names = {s["name"] for s in statuses} or default_names
        transitional = {s["name"] for s in statuses if s.get("transitional") is True}
        live = {s["name"] for s in statuses if s.get("is_terminal") is False} or default_live
        return names, transitional, live
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError):
        return default_names, set(), default_live


def _check_status_folder_consistency(docs: dict, status_names: set, transitional_statuses: set) -> tuple:
    """Soft lint: for type=spec and type=work docs under work/<status>/, the
    frontmatter status: must equal the parent folder name and be in
    status_names. Mismatches are errors; legacy `pending` on already-completed
    specs is tolerated as a warning.

    Returns (errors, warnings).
    """
    errors = []
    warnings = []
    for doc_id, doc in docs.items():
        metadata = doc["metadata"]
        doc_type = metadata.get("type")
        if doc_type not in ("spec", "work"):
            continue
        path = doc["path"]  # e.g. work/in_progress/foo__bar/spec.md
        parts = path.split("/")
        if len(parts) < 3 or parts[0] != "work":
            continue
        folder_status = parts[1]
        fm_status = metadata.get("status")
        # Report unknown folders separately from frontmatter/folder mismatches.
        if folder_status not in status_names:
            warnings.append(
                f"{doc_id}: parent folder '{folder_status}' is not in statuses.json"
            )
            continue
        if fm_status != folder_status:
            if fm_status in _LEGACY_STATUS_BACK_COMPAT and folder_status == "completed":
                warnings.append(
                    f"{doc_id}: legacy frontmatter status:{fm_status} on already-completed spec (folder=completed)"
                )
            elif folder_status in transitional_statuses:
                # Items in transitional folders (e.g. legacy `active`) are
                # tolerated with mismatched frontmatter until the migration
                # runs.
                warnings.append(
                    f"{doc_id}: legacy frontmatter status:{fm_status!r} in transitional folder {folder_status!r}"
                )
            else:
                errors.append(
                    f"{doc_id}: frontmatter status:{fm_status!r} does not match folder {folder_status!r}"
                )
    return errors, warnings


def _check_completed_at_lint(docs: dict) -> list:
    errors = []
    for doc_id, doc in docs.items():
        metadata = doc["metadata"]
        if metadata.get("type") not in ("spec", "work"):
            continue
        path = doc["path"]
        parts = path.split("/")
        if len(parts) < 4 or parts[0] != "work" or parts[1] != "completed":
            continue
        completed_at = metadata.get("completed_at")
        if not isinstance(completed_at, str) or not _DATE_RE.match(completed_at):
            errors.append(f"{doc_id}: completed work doc missing date-shaped completed_at")
        elif _parse_frontmatter_date(completed_at) is None:
            # Date-shaped but not a real calendar date (e.g. 2026-99-99).
            # Hard error: a bogus date would also silently bypass the retro
            # lint's window comparison (QA finding, retro_stage_analysis).
            errors.append(f"{doc_id}: completed work doc has non-calendar completed_at {completed_at!r}")
    return errors


def _check_retro_lint(docs: dict, root: Path = ROOT) -> tuple[list, list]:
    """Closure condition 4: a completed spec carries a compact ## Retro.

    Warn for completed_at in [RETRO_LINT_WARN_START, RETRO_LINT_ERROR_START),
    error from RETRO_LINT_ERROR_START on; earlier completions grandfathered.
    An explicit "## Retro — none" one-liner satisfies the check.
    """
    errors, warnings = [], []
    for doc_id, doc in docs.items():
        metadata = doc["metadata"]
        if metadata.get("type") != "spec":
            continue
        parts = doc["path"].split("/")
        if len(parts) < 4 or parts[0] != "work" or parts[1] != "completed" or parts[-1] != "spec.md":
            continue
        completed = _parse_frontmatter_date(metadata.get("completed_at"))
        if completed is None or completed < RETRO_LINT_WARN_START:
            continue
        try:
            text = (root / doc["path"]).read_text(encoding="utf-8")
        except OSError:
            continue
        if _has_retro_header(text):
            continue
        msg = f"{doc_id}: completed spec missing ## Retro (closure condition 4)"
        if completed >= RETRO_LINT_ERROR_START:
            errors.append(msg)
        else:
            warnings.append(msg)
    return errors, warnings


def _check_live_work_folder_shape(
    root: Path = ROOT,
    live_statuses: set | None = None,
    now: float | None = None,
    grace_seconds: float = SYNC_SETTLE_GRACE_SECONDS,
) -> tuple[list, list]:
    """Source-shape checks for live work-item folders. Returns (errors, warnings).

    Terminal work is grandfathered and fixed on touch. Live items must have
    spec.md + summary.md and must put auxiliary files under _artifacts/.
    Syncthing transients are ignored entirely, and an incomplete item dir
    whose mtime is within the settle grace is a mid-sync warning, not an
    error; a fully empty young dir is not yet an item.
    """
    errors = []
    warnings = []
    if now is None:
        now = time.time()
    work_root = root / "work"
    if live_statuses is None:
        _, _, live_statuses = _load_statuses()
    for status in sorted(live_statuses):
        status_root = work_root / status
        if not status_root.exists():
            continue
        for item_dir in sorted(p for p in status_root.iterdir() if p.is_dir()):
            if _is_syncthing_transient(item_dir.name):
                continue
            rel_item = item_dir.relative_to(root).as_posix()
            children = [
                child for child in sorted(item_dir.iterdir())
                if not _is_syncthing_transient(child.name)
            ]
            missing = [
                name for name in ("spec.md", "summary.md")
                if not (item_dir / name).is_file()
            ]
            try:
                age = now - item_dir.stat().st_mtime
            except OSError:
                age = None
            # Bounded skew: an mtime up to one grace in the future is peer
            # clock skew and still gets the grace; anything further future is
            # treated as settled so a corrupt mtime cannot suppress findings
            # indefinitely.
            within_grace = age is not None and -grace_seconds <= age < grace_seconds
            if missing and within_grace:
                if children:
                    warnings.append(
                        f"{rel_item}: missing {' and '.join(missing)} but dir is "
                        "within the sync settle grace; treating as mid-sync"
                    )
                else:
                    warnings.append(
                        f"{rel_item}: empty item dir within the sync settle grace; "
                        "treating as not yet synced"
                    )
            else:
                for name in missing:
                    errors.append(f"{rel_item}: missing {name}")
            for child in children:
                if child.is_file() and child.name not in _ALLOWED_WORK_DIRECT_FILES:
                    errors.append(
                        f"{child.relative_to(root).as_posix()}: auxiliary work file must live under _artifacts/"
                    )
    return errors, warnings


def _check_live_work_item_name_uniqueness(root: Path = ROOT, live_statuses: set | None = None) -> list:
    """Hard check: a live work item name may appear in only one live status."""
    by_name = {}
    work_root = root / "work"
    if live_statuses is None:
        _, _, live_statuses = _load_statuses()
    for status in sorted(live_statuses):
        status_root = work_root / status
        if not status_root.exists():
            continue
        for item_dir in sorted(p for p in status_root.iterdir() if p.is_dir()):
            if _is_syncthing_transient(item_dir.name):
                continue
            by_name.setdefault(item_dir.name, []).append(item_dir.relative_to(root).as_posix())

    errors = []
    for name, locations in sorted(by_name.items()):
        if len(locations) > 1:
            errors.append(
                f"live work item name duplicated across statuses: {name} at {', '.join(locations)}"
            )
    return errors


def _acceptance_counts(path: Path) -> tuple[int, int]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0, 0
    checked = len(re.findall(r"(?m)^\s*-\s*\[[xX]\]\s+", text))
    unchecked = len(re.findall(r"(?m)^\s*-\s*\[\s\]\s+", text))
    return checked, unchecked


def _parse_frontmatter_date(value) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip("'\"")[:10])
    except ValueError:
        return None


def _check_stale_in_progress_lint(docs: dict, root: Path = ROOT, stale_days: int = STALE_IN_PROGRESS_DAYS) -> list:
    warnings = []
    today = date.today()
    for doc_id, doc in docs.items():
        metadata = doc["metadata"]
        if metadata.get("type") != "spec":
            continue
        rel_path = doc["path"]
        parts = rel_path.split("/")
        if len(parts) < 4 or parts[0] != "work" or parts[1] != "in_progress" or parts[-1] != "spec.md":
            continue
        hints = []
        updated = _parse_frontmatter_date(metadata.get("updated_at"))
        if updated is not None:
            age = (today - updated).days
            if age > stale_days:
                hints.append(f"updated_at stale by {age}d")
        checked, unchecked = _acceptance_counts(root / rel_path)
        if unchecked > 0 and checked == 0:
            hints.append(f"all AC unchecked ({unchecked})")
        if hints:
            warnings.append(f"{doc_id}: stale in_progress lint: {', '.join(hints)}; likely disposition needed")
    return warnings


def _is_contact_graph_participant(doc_id: str, metadata: dict) -> bool:
    """Docs eligible to carry operates_bots/operated_by."""
    return metadata.get("type") == "contact" or bool(_MACHINE_ID_RE.match(doc_id or ""))


def _check_epic_references(docs: dict) -> list:
    """If a doc has `epic: epic_<slug>`, the referenced doc must exist and be
    type:epic. Returns a list of error strings (empty = clean).

    Per the workspace contract: specs and work items
    may opt into a charter via the `epic:` frontmatter field. The schema
    enforces format and that only spec/work docs may carry the field; this
    cross-doc check verifies the reference target.
    """
    errors = []
    for doc_id, doc in docs.items():
        epic_id = doc["metadata"].get("epic")
        if epic_id is None:
            continue
        target = docs.get(epic_id)
        if target is None:
            errors.append(f"{doc_id}: epic id not found: {epic_id}")
            continue
        target_type = target["metadata"].get("type")
        if target_type != "epic":
            errors.append(
                f"{doc_id}: epic id {epic_id} resolves to type "
                f"{target_type!r}, expected 'epic'"
            )
    return errors


def _check_contact_links(docs: dict) -> list:
    """Enforce the eight invariants from the workspace contract.

    1. Every doc-ID in operates_bots/operated_by exists.
    2. Forward edge operates_bots:[B] requires matching reverse B.operated_by:[A].
    3. Reverse edge operated_by:[A] requires matching forward A.operates_bots:[B].
    4. Self-links rejected.
    5. Duplicate IDs within a single array rejected.
    6. Bots (kind:bot OR id ~ ^reference_machine_) that participate must have
       non-empty operated_by. Humans may omit operates_bots.
    7. Multi-operator bots are valid (implicit; no separate rule).
    8. Only contact-graph participants may carry operates_bots / operated_by.
       (Schema also enforces; validator double-checks for clearer errors.)
    """
    errors = []
    for doc_id, doc in docs.items():
        metadata = doc["metadata"]
        forwards = metadata.get("operates_bots") or []
        reverses = metadata.get("operated_by") or []

        # 8: only contact-graph participants
        if (forwards or reverses) and not _is_contact_graph_participant(doc_id, metadata):
            errors.append(
                f"{doc_id}: carries operates_bots/operated_by but is not type:contact "
                f"and id does not match ^reference_machine_"
            )
            continue

        # 5: dedupe within array
        if len(forwards) != len(set(forwards)):
            errors.append(f"{doc_id}: duplicate ids in operates_bots")
        if len(reverses) != len(set(reverses)):
            errors.append(f"{doc_id}: duplicate ids in operated_by")

        # 4: no self-links
        if doc_id in forwards:
            errors.append(f"{doc_id}: operates_bots references self")
        if doc_id in reverses:
            errors.append(f"{doc_id}: operated_by references self")

        # 1: targets must exist
        for target in forwards:
            if target not in docs:
                errors.append(f"{doc_id}: operates_bots target not found: {target}")
        for target in reverses:
            if target not in docs:
                errors.append(f"{doc_id}: operated_by target not found: {target}")

        # 2: forward edge requires matching reverse
        for target in forwards:
            if target not in docs or target == doc_id:
                continue  # already flagged above
            target_reverses = docs[target]["metadata"].get("operated_by") or []
            if doc_id not in target_reverses:
                errors.append(
                    f"{doc_id}: operates_bots:[{target}] has no matching "
                    f"{target}.operated_by:[{doc_id}]"
                )

        # 3: reverse edge requires matching forward
        for target in reverses:
            if target not in docs or target == doc_id:
                continue
            target_forwards = docs[target]["metadata"].get("operates_bots") or []
            if doc_id not in target_forwards:
                errors.append(
                    f"{doc_id}: operated_by:[{target}] has no matching "
                    f"{target}.operates_bots:[{doc_id}]"
                )

        # 6: bots in the graph must have an operator. "Bot" is determined by
        # explicit `kind: bot` declaration — the schema's id-prefix gate only
        # decides *who may carry* contact fields, not who counts as a bot.
        # This avoids tripping on non-machine docs that happen to use the
        # `reference_machine_` prefix (e.g. reference_machine_profile_schema).
        if metadata.get("kind") == "bot" and not reverses:
            errors.append(f"{doc_id}: bot in contact graph has no operated_by")

    return errors


def _missing_from_catalog(docs: dict, catalog_ids: set) -> list:
    """Source docs flagged in_catalog that appear in neither catalog file.

    `catalog_ids` is the UNION of documents.json + archive.json ids, so a terminal
    doc present in the archive is not flagged; a doc absent from both still is.
    See the workspace contract.
    """
    return [
        f"doc missing from catalog: {doc_id}"
        for doc_id, doc in docs.items()
        if doc["in_catalog"] and doc_id not in catalog_ids
    ]


def _is_catalog_lag(
    catalog_mtime: float,
    source_mtime: float | None,
    now: float | None = None,
    grace_seconds: float = CATALOG_LAG_GRACE_SECONDS,
) -> bool:
    """True when a catalog/source disagreement is explainable by regen lag:
    the source changed after the catalog was written and within the grace
    window. `source_mtime=None` (doc absent from source, e.g. moved) uses the
    newest work source mtime passed by the caller."""
    if source_mtime is None:
        return False
    now = time.time() if now is None else now
    return source_mtime > catalog_mtime and (now - source_mtime) < grace_seconds


def _emit_findings(errors: list, warnings: list, stream=None) -> None:
    """Errors before warnings: cadence derives the primary offending path from
    the first path-bearing lines of this stderr, and settle-grace warnings can
    carry bare spec.md/summary.md tokens that must not win attribution."""
    if stream is None:
        stream = sys.stderr
    if errors:
        print("memory_v2 validation failed", file=stream)
        for error in errors:
            print(f"- {error}", file=stream)
    if warnings:
        print("memory_v2 validation warnings:", file=stream)
        for warning in warnings:
            print(f"- {warning}", file=stream)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-only", action="store_true",
        help="Validate source documents without requiring regenerated catalogs",
    )
    args = parser.parse_args(argv)

    # Load and validate the schema itself first
    try:
        schema = load_schema(SCHEMA_PATH)
    except SchemaInvalidError as exc:
        print(f"memory_v2 schema load failed: {exc}", file=sys.stderr)
        return 1

    docs = {}
    docs_in_catalog = set()
    errors = []
    warnings = []
    status_names, transitional_statuses, live_statuses = _load_statuses()

    for path, in_catalog in iter_in_scope_paths():
        try:
            metadata = load_frontmatter(path)
        except (ValueError, StrictLoaderError) as exc:
            errors.append(f"{path}: frontmatter parse failed: {exc}")
            continue

        # Schema validation
        schema_errors = validate_frontmatter(metadata, schema)
        for err in schema_errors:
            errors.append(f"{path}: {err}")
        if schema_errors:
            continue

        if "repo" in metadata:
            errors.append(f"{path}: deprecated frontmatter key 'repo'")

        # Project-location check. Schema's oneOf branch for type=project
        # already requires all four keys; this Python guard is redundant but
        # kept as a defense-in-depth check that produces a clearer error
        # message than the schema's "is not valid under any of the given
        # schemas" diagnostic.
        if metadata.get("type") == "project":
            missing_project_keys = set(PROJECT_LOCATION_KEYS) - metadata.keys()
            if missing_project_keys:
                errors.append(
                    f"{path}: missing project location keys {sorted(missing_project_keys)}"
                )

        doc_id = metadata.get("id")
        if doc_id in docs:
            errors.append(f"{path}: duplicate document id {doc_id}")
            continue

        docs[doc_id] = {
            "path": path.relative_to(ROOT).as_posix(),
            "metadata": metadata,
            "in_catalog": in_catalog,
            "mtime": path.stat().st_mtime,
        }
        if in_catalog:
            docs_in_catalog.add(doc_id)

    # Cross-doc integrity (always runs, even in --source-only)
    for doc_id, doc in docs.items():
        for related_id in doc["metadata"].get("related", []):
            if related_id not in docs:
                errors.append(f"{doc_id}: related id not found: {related_id}")
        for sup_id in doc["metadata"].get("supersedes", []):
            if sup_id not in docs:
                errors.append(f"{doc_id}: supersedes id not found: {sup_id}")
        for sup_id in doc["metadata"].get("superseded_by", []):
            if sup_id not in docs:
                errors.append(f"{doc_id}: superseded_by id not found: {sup_id}")
    errors.extend(_check_epic_references(docs))
    errors.extend(_check_nexus_domains(docs))

    # Contact-link integrity (always runs)
    errors.extend(_check_contact_links(docs))

    # Status-folder consistency lint for type=spec / type=work documents
    # (always runs; produces both errors and warnings).
    status_errors, status_warnings = _check_status_folder_consistency(
        docs, status_names, transitional_statuses
    )
    errors.extend(status_errors)
    warnings.extend(status_warnings)
    errors.extend(_check_completed_at_lint(docs))
    retro_errors, retro_warnings = _check_retro_lint(docs)
    errors.extend(retro_errors)
    warnings.extend(retro_warnings)
    shape_errors, shape_warnings = _check_live_work_folder_shape(ROOT, live_statuses)
    errors.extend(shape_errors)
    warnings.extend(shape_warnings)
    errors.extend(_check_live_work_item_name_uniqueness(ROOT, live_statuses))
    warnings.extend(_check_stale_in_progress_lint(docs))

    # Catalog checks (skipped in --source-only)
    catalog_count = 0
    collections_count = 0
    if not args.source_only:
        # The catalog is split into the active index (documents.json) and the
        # terminal archive (archive.json). Both must be checked against the
        # source tree; the union of their ids is the full catalogued set.
        # See the workspace contract (catalog partitioning).
        active = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        archive = (
            json.loads(ARCHIVE_PATH.read_text(encoding="utf-8"))
            if ARCHIVE_PATH.exists()
            else []
        )
        catalog = active + archive
        collections = json.loads(COLLECTIONS_PATH.read_text(encoding="utf-8"))
        catalog_count = len(catalog)
        collections_count = len(collections)
        catalog_mtime = CATALOG_PATH.stat().st_mtime
        newest_source_mtime = newest_work_catalog_source_mtime()
        now = time.time()

        def catalog_finding(msg: str, source_mtime: float | None) -> None:
            """Route a catalog/source disagreement to warnings when it is
            regen lag (see CATALOG_LAG_GRACE_SECONDS), else to errors."""
            if _is_catalog_lag(catalog_mtime, source_mtime, now):
                warnings.append(f"{msg} (catalog lag; run scripts/generate_catalog.py in this workspace)")
            else:
                errors.append(msg)

        catalog_ids = set()
        for entry in catalog:
            doc_id = entry["id"]
            catalog_ids.add(doc_id)
            if doc_id not in docs:
                # Doc moved/removed after regen: attribute to the newest work edit.
                catalog_finding(f"catalog references missing doc id {doc_id}", newest_source_mtime)
                continue
            doc = docs[doc_id]
            doc_mtime = doc.get("mtime")
            if entry["path"] != doc["path"]:
                catalog_finding(f"{doc_id}: catalog path mismatch {entry['path']} != {doc['path']}", doc_mtime)
            for key in ("title", "type", "status", "canonical", "updated_at", "summary", "source_path"):
                if entry.get(key) != doc["metadata"].get(key):
                    catalog_finding(f"{doc_id}: catalog field mismatch for {key}", doc_mtime)
            for key in ("tags", "related", "aliases", "operates_bots", "operated_by"):
                if entry.get(key, []) != doc["metadata"].get(key, []):
                    catalog_finding(f"{doc_id}: catalog field mismatch for {key}", doc_mtime)
            for key in ("priority", "kind", "relationship"):
                if entry.get(key) != doc["metadata"].get(key):
                    catalog_finding(f"{doc_id}: catalog field mismatch for {key}", doc_mtime)
            for key in PROJECT_LOCATION_KEYS:
                if entry.get(key, []) != doc["metadata"].get(key, []):
                    catalog_finding(f"{doc_id}: catalog field mismatch for {key}", doc_mtime)
            if "repo" in entry:
                errors.append(f"{doc_id}: deprecated catalog key 'repo'")

        for msg in _missing_from_catalog(docs, catalog_ids):
            doc_id = msg.rsplit(" ", 1)[-1]
            catalog_finding(msg, docs[doc_id].get("mtime"))

        for collection in collections:
            path = ROOT / collection["path"]
            if not path.exists():
                errors.append(f"collection path missing: {collection['path']}")
            for doc_id in collection["document_ids"]:
                if doc_id not in docs:
                    errors.append(f"collection {collection['id']} references missing doc id {doc_id}")

    _emit_findings(errors, warnings)
    if errors:
        return 1

    mode = " (--source-only)" if args.source_only else ""
    warn_suffix = f", {len(warnings)} warnings" if warnings else ""
    print(
        f"Validated {len(docs)} docs ({len(docs_in_catalog)} catalogable), "
        f"{catalog_count} catalog entries, {collections_count} collections{mode}{warn_suffix}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
