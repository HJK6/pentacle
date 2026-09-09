from __future__ import annotations

import logging
import os
import re
import json
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Protocol

# The parser ships with the application; no host-local checkout is required.
from .specs_parser import (
    DEFAULT_STATUSES,
    SYNTHETIC_SPEC_PROGRESS,
    is_sync_conflict as _is_spec_sync_conflict,
    parse_statuses_json,
    parse_work_folder,
    declared_spec_id,
    has_declared_spec_id,
)


log = logging.getLogger(__name__)

SPEC_ID_RE = re.compile(r"^[A-Za-z0-9_-]+__[A-Za-z0-9_-]+$")
SPEC_DOCUMENT_PREFIXES = ("spec_", "work_")


class _TimerLike(Protocol):
    daemon: bool

    def start(self) -> None: ...

    def cancel(self) -> None: ...


def _resolve_specs_memory_root() -> tuple[Path | None, str]:
    configured = os.environ.get("PENTACLE_MEMORY_ROOT", "").strip()
    if not configured:
        return None, "not_configured"
    return Path(configured).expanduser(), "PENTACLE_MEMORY_ROOT"


class SpecsSubsystem:
    def __init__(
        self,
        *,
        session_summaries: Callable[[], list[dict[str, Any]]],
        changed_callback: Callable[[list[str]], None],
        retry_interval_s: float = 60.0,
        debounce_s: float = 0.5,
        monotonic: Callable[[], float] = time.monotonic,
        timer_factory: Callable[[float, Callable[..., None], tuple[Any, ...]], _TimerLike] = threading.Timer,
    ) -> None:
        self.memory_root, self.memory_root_source = _resolve_specs_memory_root()
        self.session_summaries = session_summaries
        self.changed_callback = changed_callback
        self.retry_interval_s = retry_interval_s
        self.debounce_s = debounce_s
        self._monotonic = monotonic
        self._timer_factory = timer_factory
        self.disabled = self.memory_root is None or not self.memory_root.is_dir()
        # Cache statuses and reload only when the local file changes. `_statuses_mtime` of
        # -1 forces an initial load; subsequent calls reload only when mtime
        # changes. Missing/invalid file → DEFAULT_STATUSES fallback inside
        # parse_statuses_json with a logged warning.
        self._statuses: list[dict[str, Any]] = list(DEFAULT_STATUSES)
        self._status_order: dict[str, int] = {s["name"]: s["order"] for s in DEFAULT_STATUSES}
        self._statuses_mtime: float = -1.0
        self._statuses_lock = threading.Lock()
        self.subsystem_state = "disabled" if self.disabled else "polling"
        self.push_enabled = False
        self._observer: Any = None
        self._stop = threading.Event()
        self._debounce_timer: _TimerLike | None = None
        self._debounce_generation = 0
        self._last_event_at = 0.0
        self._pending_spec_ids: set[str] = set()
        self._lock = threading.Lock()
        self._presentation_cache_lock = threading.Lock()
        self._folder_index_cache: dict[str, list[tuple[str, Path]]] | None = None
        self._status_card_metadata_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._declared_id_collisions: set[str] = set()
        self._declared_id_owners: dict[str, list[tuple[str, Path]]] = {}
        if self.disabled:
            log.warning(
                "specs subsystem disabled: memory root is missing or invalid source=%s path=%s",
                self.memory_root_source,
                self.memory_root,
            )

    def state_payload(self) -> str:
        return "disabled" if self.disabled else self.subsystem_state

    def start(self) -> None:
        if self.disabled:
            return
        self._attach_or_degrade()
        threading.Thread(target=self._retry_loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            if self._debounce_timer:
                self._debounce_timer.cancel()
                self._debounce_timer = None
        self._stop_observer()

    def _load_statuses(self) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Return (statuses_list, name_to_order). Reloads on mtime change.

        Thread-safe; multiple list_specs callers can race the reload but the
        worst case is one extra parse — the result is the same.
        """
        if self.memory_root is None:
            return self._statuses, self._status_order
        statuses_path = self.memory_root / "work" / "statuses.json"
        try:
            mtime = statuses_path.stat().st_mtime
        except FileNotFoundError:
            mtime = -1.0
        with self._statuses_lock:
            if mtime != self._statuses_mtime:
                statuses, order = parse_statuses_json(self.memory_root)
                self._statuses = statuses
                self._status_order = order
                self._statuses_mtime = mtime
            return self._statuses, self._status_order

    def _status_names(self) -> set[str]:
        statuses, _ = self._load_statuses()
        return {s["name"] for s in statuses}

    def _work_dirs(self) -> list[Path]:
        """All folders under `work/` that look like status buckets.

        Includes every entry in `statuses.json` AND any additional directories
        physically present under `work/` (so unknown-named folders still surface
        with `status_unknown: true` for callers that want to report them).
        """
        if self.disabled:
            return []
        work_root = self.memory_root / "work"
        known = [work_root / s["name"] for s in self._load_statuses()[0]]
        seen = {p.name for p in known}
        on_disk = []
        if work_root.is_dir():
            for child in work_root.iterdir():
                if child.is_dir() and child.name not in seen and not child.name.startswith("."):
                    on_disk.append(child)
        return known + on_disk

    def _spawn_work_dirs(self) -> list[Path]:
        """Configured status buckets used by authoritative spawn resolution.

        Spawn resolution is a correctness boundary, so its search space comes
        directly from ``statuses.json`` (or the parser's documented fallback
        when that file is unavailable).  In particular, it must not inherit a
        caller/target host or a partial lifecycle list from another subsystem.
        Unknown on-disk buckets remain visible to the presentation scanner via
        ``_work_dirs`` but cannot silently become spawn-authoritative.
        """
        if self.disabled:
            return []
        work_root = self.memory_root / "work"
        return [work_root / status["name"] for status in self._load_statuses()[0]]

    def _attach_or_degrade(self) -> None:
        if self.disabled or self._observer is not None:
            return
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer

            subsystem = self

            class Handler(FileSystemEventHandler):
                def on_any_event(self, event) -> None:
                    subsystem._on_fs_event(event)

            observer = Observer()
            handler = Handler()
            for directory in self._work_dirs():
                if not directory.is_dir():
                    raise FileNotFoundError(str(directory))
                observer.schedule(handler, str(directory), recursive=True)
            observer.start()
            self._observer = observer
            self._invalidate_presentation_cache()
            self.push_enabled = True
            self.subsystem_state = "watching"
        except Exception as exc:
            self._stop_observer()
            self.push_enabled = False
            self.subsystem_state = "polling"
            log.warning("specs watcher attach failed; falling back to polling: %s", exc)

    def _stop_observer(self) -> None:
        observer = self._observer
        self._observer = None
        if observer is not None:
            observer.stop()
            observer.join(timeout=2)

    def _retry_loop(self) -> None:
        while not self._stop.wait(self.retry_interval_s):
            if not self.push_enabled:
                self._attach_or_degrade()

    def _on_fs_event(self, event) -> None:
        if not self.push_enabled:
            return
        paths = [Path(str(getattr(event, "src_path", "") or ""))]
        if getattr(event, "dest_path", None):
            paths.append(Path(str(event.dest_path)))
        ids = {
            spec_id
            for path in paths
            if not _is_spec_sync_conflict(path)
            for spec_id in [self.spec_id_for_path(path)]
            if spec_id
        }
        if not ids:
            return
        self._invalidate_presentation_cache()
        with self._lock:
            self._pending_spec_ids.update(ids)
            self._debounce_generation += 1
            self._last_event_at = self._monotonic()
            generation = self._debounce_generation
            if self._debounce_timer:
                self._debounce_timer.cancel()
            self._debounce_timer = self._timer_factory(self.debounce_s, self._flush_debounce, (generation,))
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

    def _flush_debounce(self, generation: int) -> None:
        with self._lock:
            if generation != self._debounce_generation:
                return
            quiet_for = self._monotonic() - self._last_event_at
            if quiet_for < self.debounce_s:
                delay = max(0.001, self.debounce_s - quiet_for)
                self._debounce_timer = self._timer_factory(delay, self._flush_debounce, (generation,))
                self._debounce_timer.daemon = True
                self._debounce_timer.start()
                return
            spec_ids = sorted(self._pending_spec_ids)
            self._pending_spec_ids.clear()
            self._debounce_timer = None
        if not spec_ids:
            return
        self.changed_callback(spec_ids)

    def spec_id_for_path(self, path: Path) -> str | None:
        try:
            rel = path.resolve().relative_to((self.memory_root / "work").resolve())
        except Exception:
            return None
        # Accept any folder under work/ (including unknown ones) — the parser
        # surfaces unknowns with `status_unknown: true` rather than dropping
        # them silently.
        return rel.parts[1] if len(rel.parts) >= 2 else None

    def _sync_conflict_count(self) -> int:
        if self.disabled:
            return 0
        root = self.memory_root / "work"
        return sum(1 for path in root.rglob("*") if _is_spec_sync_conflict(path)) if root.exists() else 0

    def _scan_folders_by_id(
        self, status_dirs: list[Path] | None = None,
    ) -> dict[str, list[tuple[str, Path]]]:
        folders: dict[str, list[tuple[str, Path]]] = defaultdict(list)
        declared_paths: dict[str, set[Path]] = defaultdict(set)
        self._declared_id_owners = {}
        for status_dir in self._work_dirs() if status_dirs is None else status_dirs:
            status_name = status_dir.name
            if status_dir.is_dir():
                for child in status_dir.iterdir():
                    if child.is_dir() and not _is_spec_sync_conflict(child):
                        declared_id = declared_spec_id(child)
                        aliases = set(self._spec_id_aliases(declared_id))
                        if self._folder_name_is_declared_alias(child.name, declared_id):
                            aliases.add(child.name)
                        for alias in aliases:
                            folders[alias].append((status_name, child))
                        if has_declared_spec_id(child):
                            self._declared_id_owners.setdefault(declared_id, []).append((status_name, child))
                            declared_paths[declared_id].add(child)
        self._declared_id_collisions = {
            alias
            for declared_id, paths in declared_paths.items()
            if len(paths) > 1
            for alias in self._spec_id_aliases(declared_id)
        }
        return folders

    def _folders_by_id(self) -> dict[str, list[tuple[str, Path]]]:
        if not self.push_enabled:
            return self._scan_folders_by_id()
        with self._presentation_cache_lock:
            if self._folder_index_cache is None:
                self._folder_index_cache = self._scan_folders_by_id()
            return self._folder_index_cache

    def _invalidate_presentation_cache(self) -> None:
        with self._presentation_cache_lock:
            self._folder_index_cache = None
            self._status_card_metadata_cache.clear()

    def _spec_id_aliases(self, spec_id: str | None) -> list[str]:
        if not spec_id:
            return []
        aliases = [spec_id]
        for prefix in SPEC_DOCUMENT_PREFIXES:
            if spec_id.startswith(prefix):
                folder_id = spec_id[len(prefix):]
                if folder_id and folder_id not in aliases:
                    aliases.append(folder_id)
        return aliases

    def _folder_name_is_declared_alias(self, folder_id: str, declared_id: str) -> bool:
        """Accept a folder spelling only when this document proves the alias.

        The folder and declared IDs must retain the exact topic, and their
        repository tokens may differ only by hyphen/underscore spelling. This
        establishes aliases inside one document; it never compares two
        independently declared documents through a folded key.
        """
        aliases = self._spec_id_aliases(declared_id)
        declared_bare = aliases[-1] if aliases else ""
        if folder_id == declared_bare:
            return True
        if "__" not in folder_id or "__" not in declared_bare:
            return False
        folder_repo, folder_topic = folder_id.split("__", 1)
        declared_repo, declared_topic = declared_bare.split("__", 1)
        return (
            folder_topic == declared_topic
            and folder_repo.replace("-", "_") == declared_repo.replace("-", "_")
        )

    def _normalized_spec_id(self, spec_id: str | None) -> str:
        """Normalize legacy presentation/coordination spellings by family.

        This compatibility key is deliberately not a proof of document
        identity.  Resolver-proven canonical identity is used only by
        provenance and attestation paths.
        """
        aliases = self._spec_id_aliases(spec_id)
        canonical = aliases[-1] if aliases else ""
        if "__" not in canonical:
            return canonical
        repo, topic = canonical.split("__", 1)
        return f"{repo.replace('-', '_')}__{topic}"

    def equivalent_spec_ids(self, left: str | None, right: str | None) -> bool:
        """Legacy family equivalence for presentation and coordination only."""
        if not left or not right:
            return False
        return (
            bool(set(self._spec_id_aliases(left)).intersection(self._spec_id_aliases(right)))
            or self._normalized_spec_id(left) == self._normalized_spec_id(right)
        )

    def canonical_spec_identity(self, spec_id: str | None) -> str | None:
        """Return one resolver-proven frontmatter identity, or fail closed.

        Folder spellings and declared ids are aliases only when the live work
        tree resolves both to exactly one document. No character folding may
        create an identity shared by independently declared documents.
        """
        resolution = self.resolve_for_spawn(spec_id)
        identity = resolution.get("canonical_spec_id")
        return str(identity) if resolution.get("resolution") == "resolved" and identity else None

    def equivalent_spec_id_family(self, spec_id: str | None) -> tuple[str, ...]:
        """Return legacy persisted spellings for coordination family operations."""
        aliases = self._spec_id_aliases(spec_id)
        if not aliases:
            return ()
        canonical = aliases[-1]
        return tuple(sorted({canonical, *(f"{prefix}{canonical}" for prefix in SPEC_DOCUMENT_PREFIXES)}))

    def _matches_for_spec_id(self, spec_id: str | None) -> list[tuple[str, Path]]:
        return self._matches_from_index(self._folders_by_id(), spec_id)

    def _matches_from_index(
        self,
        folders: dict[str, list[tuple[str, Path]]],
        spec_id: str | None,
    ) -> list[tuple[str, Path]]:
        matches: list[tuple[str, Path]] = []
        seen: set[Path] = set()
        for alias in self._spec_id_aliases(spec_id):
            for match in folders.get(alias, []):
                if match[1] in seen:
                    continue
                seen.add(match[1])
                matches.append(match)
        return matches

    def _declared_matches_from_index(
        self,
        folders: dict[str, list[tuple[str, Path]]],
        spec_id: str,
    ) -> list[tuple[str, Path]]:
        """Return folders explicitly indexed by a requested spelling.

        The index contains each folder name and each declared frontmatter id.
        Those are resolver-proven aliases for the same document; a distinct
        document with a merely fold-equivalent spelling cannot enter the set.
        """
        direct_matches = [
            match
            for match in self._matches_from_index(folders, spec_id)
            if has_declared_spec_id(match[1])
        ]
        declared_ids = {declared_spec_id(folder) for _status, folder in direct_matches}
        matches = list(direct_matches)
        seen = {folder for _status, folder in matches}
        for declared_id in declared_ids:
            for match in self._matches_from_index(folders, declared_id):
                if match[1] in seen or declared_spec_id(match[1]) != declared_id:
                    continue
                seen.add(match[1])
                matches.append(match)
        return matches

    @staticmethod
    def _resolution_for_matches(matches: list[tuple[str, Path]]) -> str:
        return "zero_matches" if not matches else "multiple_matches" if len(matches) > 1 else "resolved"

    def _candidate_paths(self, matches: list[tuple[str, Path]]) -> list[str]:
        candidates: list[str] = []
        for _status, folder in matches:
            spec_path = folder / "spec.md"
            try:
                candidates.append(spec_path.relative_to(self.memory_root).as_posix())
            except ValueError:
                candidates.append(str(spec_path))
        return sorted(candidates)

    def resolve_for_spawn(self, spec_id: str | None) -> dict[str, Any]:
        """Resolve a spawn binding against the cache, then the live work tree.

        The presentation index may lag Syncthing or miss an invalidation event.
        It is useful as the cheap first lookup, but ``work/`` is the memory
        model's source of truth. Missing cached paths are discarded, and any
        cache/tree disagreement is settled by a fresh tree scan.
        """
        if not spec_id:
            return {
                "resolution": None,
                "source": "work_tree",
                "catalog_resolution": None,
                "tree_resolution": None,
                "catalog_candidates": [],
                "tree_candidates": [],
            }

        # Catalog-first: use the subsystem's current index, but never count a
        # row whose directory has disappeared since the index was built.
        catalog_matches = [
            match for match in self._matches_for_spec_id(spec_id)
            if match[1].is_dir()
        ]
        catalog_resolution = self._resolution_for_matches(catalog_matches)

        # Always take one fresh snapshot before authorizing ownership. This is
        # what detects the resolved↔resolved race where the old cached path
        # still exists but its frontmatter now declares a different id.
        tree_matches = self._declared_matches_from_index(
            self._scan_folders_by_id(self._spawn_work_dirs()), spec_id,
        )
        tree_resolution = self._resolution_for_matches(tree_matches)
        catalog_paths = {path.resolve() for _status, path in catalog_matches}
        tree_paths = {path.resolve() for _status, path in tree_matches}
        agrees = catalog_resolution == tree_resolution and catalog_paths == tree_paths
        source = "catalog" if agrees and tree_resolution == "resolved" else "work_tree"
        canonical_spec_id = (
            declared_spec_id(tree_matches[0][1]) if tree_resolution == "resolved" else None
        )

        return {
            "resolution": catalog_resolution if agrees else tree_resolution,
            "source": source,
            "catalog_resolution": catalog_resolution,
            "tree_resolution": tree_resolution,
            "catalog_candidates": self._candidate_paths(catalog_matches),
            "tree_candidates": self._candidate_paths(tree_matches),
            "canonical_spec_id": canonical_spec_id,
        }

    def _summary_has_spec_id(self, summary: dict[str, Any], spec_id: str) -> bool:
        target_aliases = set(self._spec_id_aliases(spec_id))
        raw_spec_ids = summary.get("spec_ids")
        if isinstance(raw_spec_ids, list):
            summary_ids = [str(item) for item in raw_spec_ids if item]
        elif summary.get("spec_id"):
            summary_ids = [str(summary.get("spec_id"))]
        else:
            summary_ids = []
        return any(target_aliases.intersection(self._spec_id_aliases(item)) for item in summary_ids)

    def _has_folder_match(self, spec_id: str) -> bool:
        return bool(self._matches_for_spec_id(spec_id))

    def resolution_for(self, spec_id: str | None) -> str | None:
        if not spec_id:
            return None
        return self._resolution_for_matches(self._matches_for_spec_id(spec_id))

    def _read_text(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def _read_json(self, path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except Exception as exc:
            log.warning("specs epics catalog read failed path=%s: %s", path, exc)
            return []

    def _live_leaders(self, spec_id: str) -> list[dict[str, Any]]:
        leaders: list[dict[str, Any]] = []
        for summary in self.session_summaries():
            if not self._summary_has_spec_id(summary, spec_id):
                continue
            if not summary.get("online") or summary.get("parent_stream_id") is not None:
                continue
            if str(summary.get("visibility") or "default") == "hidden":
                continue
            leaders.append(
                {
                    "stream_id": str(summary.get("stream_id") or ""),
                    "provider": str(summary.get("provider") or ""),
                    "host": str(summary.get("host") or ""),
                    "display_name": str(summary.get("display_name") or summary.get("session_name") or ""),
                    "last_event_at": str(summary.get("last_event_at") or ""),
                    "handoff_from_stream_id": summary.get("handoff_from_stream_id"),
                }
            )
        leaders.sort(key=lambda item: str(item.get("last_event_at") or ""), reverse=True)
        return leaders

    def _synthetic(self, spec_id: str, reason: str) -> dict[str, Any]:
        return {
            "spec_id": spec_id,
            "synthetic": True,
            "unresolved_reason": reason,
            "lifecycle": "unresolved",
            "status": None,
            "status_unknown": False,
            "repo": None,
            "topic": None,
            "title": None,
            "frontmatter_status": None,
            "machine": None,
            "owner": None,
            "created_at": None,
            "updated_at": None,
            "completed_at": None,
            "goal_excerpt": None,
            "next_action": None,
            "blockers": None,
            "progress": dict(SYNTHETIC_SPEC_PROGRESS),
            "frontmatter_drift": False,
            "terminal_state_drift": False,
            "live_leaders": self._live_leaders(spec_id),
        }

    def _parse_folder(self, lifecycle: str, folder: Path) -> dict[str, Any]:
        # Use the bundled parser to retain the established row shape.
        # The `lifecycle` argument here is the
        # folder name — it's emitted both as `lifecycle` (legacy alias) and
        # `status` in the returned dict.
        statuses, _ = self._load_statuses()
        row = parse_work_folder(folder, lifecycle, statuses)
        row["live_leaders"] = self._live_leaders(row["spec_id"])
        return row

    @staticmethod
    def _member_matches_row(member_id: str, row: dict[str, Any]) -> bool:
        spec_id = str(row.get("spec_id") or "")
        if not member_id or not spec_id:
            return False
        if member_id == spec_id:
            return True
        if member_id == f"spec_{spec_id}":
            return True
        if member_id.startswith("work_") and member_id[len("work_"):].startswith(spec_id):
            return True
        return False

    def _load_epics(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        raw = self._read_json(self.memory_root / "catalog" / "epics.json")
        if not isinstance(raw, list):
            return []

        epics: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            members = [str(m) for m in item.get("members", []) if m]
            member_rows: list[dict[str, Any]] = []
            seen_specs: set[str] = set()
            for member_id in members:
                for row in rows:
                    if not self._member_matches_row(member_id, row):
                        continue
                    spec_id = str(row.get("spec_id") or "")
                    if not spec_id or spec_id in seen_specs:
                        continue
                    seen_specs.add(spec_id)
                    member_rows.append(
                        {
                            "spec_id": spec_id,
                            "title": row.get("title"),
                            "status": row.get("status"),
                            "repo": row.get("repo"),
                            "next_action": row.get("next_action"),
                            "live_leaders": row.get("live_leaders") or [],
                        }
                    )
            status_counts: dict[str, int] = {}
            for row in member_rows:
                status = str(row.get("status") or "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1
            epics.append(
                {
                    "id": str(item.get("id") or ""),
                    "path": str(item.get("path") or ""),
                    "title": str(item.get("title") or item.get("id") or ""),
                    "summary": str(item.get("summary") or ""),
                    "status": str(item.get("status") or "backlog"),
                    "members": members,
                    "member_rows": member_rows,
                    "member_counts": status_counts,
                }
            )
        epics.sort(key=lambda entry: (str(entry.get("status") or ""), str(entry.get("id") or "")))
        return epics

    def _canonical(self, matches: list[tuple[str, Path]]) -> tuple[str, Path]:
        # Precedence comes from statuses.json `order` (lower wins). Unknown
        # status folders sort to the end (+inf) so they only win when nothing
        # else matches.
        _, name_to_order = self._load_statuses()
        return sorted(
            matches,
            key=lambda item: name_to_order.get(item[0], float("inf")),
        )[0]

    def list_specs(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.disabled:
            return {"error": "specs_not_configured"}
        folders = self._folders_by_id()
        rows: list[dict[str, Any]] = []
        seen_folders: set[Path] = set()
        seen_collisions: set[str] = set()
        for spec_id, matches in sorted(folders.items()):
            lifecycle, folder = self._canonical(matches)
            if folder not in seen_folders:
                rows.append(self._parse_folder(lifecycle, folder))
                seen_folders.add(folder)
            if len(matches) > 1 and spec_id not in seen_collisions:
                rows.append(self._synthetic(spec_id, "multiple_matches"))
                seen_collisions.add(spec_id)
        live_spec_ids: set[str] = set()
        for summary in self.session_summaries():
            raw_spec_ids = summary.get("spec_ids")
            if isinstance(raw_spec_ids, list):
                live_spec_ids.update(str(item) for item in raw_spec_ids if item)
            elif summary.get("spec_id"):
                live_spec_ids.add(str(summary.get("spec_id")))
        for spec_id in sorted(spec_id for spec_id in live_spec_ids if not self._has_folder_match(spec_id)):
            rows.append(self._synthetic(spec_id, "zero_matches"))
        return {
            "specs": self._apply_filters(rows, filters or {}),
            "epics": self._load_epics(rows),
            "sync_conflict_count": self._sync_conflict_count(),
            "subsystem_state": self.state_payload(),
        }

    def _apply_filters(self, rows: list[dict[str, Any]], filters: dict[str, Any]) -> list[dict[str, Any]]:
        def ok(row: dict[str, Any]) -> bool:
            # `status` is the canonical key; `lifecycle` accepted as legacy
            # alias from clients that haven't migrated to the new field name.
            for key in ("lifecycle", "status", "repo", "machine"):
                if filters.get(key) and row.get(key) != filters[key]:
                    return False
            query = str(filters.get("search") or "").strip().lower()
            if not query:
                return True
            haystack = " ".join(
                str(row.get(key) or "")
                for key in ("spec_id", "repo", "topic", "title", "status", "machine", "owner", "goal_excerpt", "next_action")
            ).lower()
            return query in haystack

        return [row for row in rows if ok(row)]

    def get_spec(self, spec_id: str) -> dict[str, Any]:
        if self.disabled:
            return {"error": "specs_not_configured"}
        matches = self._matches_for_spec_id(spec_id)
        declared_matches = self._declared_id_owners.get(spec_id, [])
        if declared_matches:
            matches = declared_matches
        if not matches:
            return {"error": "spec_not_found"}
        if len(matches) > 1 and set(self._spec_id_aliases(spec_id)).intersection(self._declared_id_collisions):
            candidates = sorted({f"{status}:{folder.name}" for status, folder in matches})
            return {
                "error": "spec_multiple_matches",
                "error_code": "spec_id_ambiguous",
                "candidates": candidates,
            }
        lifecycle, folder = self._canonical(matches)
        parsed = self._parse_folder(lifecycle, folder)
        return {
            "summary_md": self._read_text(folder / "summary.md"),
            "spec_md": self._read_text(folder / "spec.md"),
            "parsed": parsed,
            "live_leaders": parsed["live_leaders"],
        }

    def status_card_metadata(self, spec_id: str) -> dict[str, Any]:
        """Return parsed metadata without the live-leader/session recursion."""
        if self.disabled:
            return {"error": "specs_not_configured"}
        matches = self._matches_for_spec_id(spec_id)
        if len(matches) != 1:
            return {"error": "spec_not_found" if not matches else "spec_multiple_matches"}
        lifecycle, folder = matches[0]
        cache_key = (lifecycle, str(folder))
        if self.push_enabled:
            with self._presentation_cache_lock:
                cached = self._status_card_metadata_cache.get(cache_key)
                if cached is not None:
                    return {"parsed": dict(cached)}
                statuses, _ = self._load_statuses()
                parsed = parse_work_folder(folder, lifecycle, statuses)
                metadata = {
                    "title": parsed.get("title"),
                    "updated_at": parsed.get("updated_at"),
                    "status": parsed.get("status"),
                }
                self._status_card_metadata_cache[cache_key] = metadata
                return {"parsed": dict(metadata)}
        statuses, _ = self._load_statuses()
        parsed = parse_work_folder(folder, lifecycle, statuses)
        metadata = {
            "title": parsed.get("title"),
            "updated_at": parsed.get("updated_at"),
            "status": parsed.get("status"),
        }
        return {"parsed": dict(metadata)}

    def changed_payload(self, spec_ids: list[str]) -> dict[str, Any]:
        return {
            "spec_ids": list(spec_ids),
            "sync_conflict_count": self._sync_conflict_count(),
            "subsystem_state": self.state_payload(),
        }

    def statuses_payload(self) -> list[dict[str, Any]]:
        """Public statuses array for inclusion in specs.capabilities.

        Returns a copy so callers can't mutate the cached list.
        """
        statuses, _ = self._load_statuses()
        return [dict(s) for s in statuses]
