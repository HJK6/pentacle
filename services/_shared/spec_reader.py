"""Read-only spec resolver used by WIP admission."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .specs_service import SpecsSubsystem, _resolve_specs_memory_root, _content_digest, _input_token

class SpecReader:
    """One fresh resolver index per check; no observer, cached catalog or writes."""
    def __init__(self, root: str | Path | None, *, identity_only: bool = False) -> None:
        self.root = Path(root) if root is not None else _resolve_specs_memory_root()[0]
        if self.root is None or not (self.root / 'work').is_dir():
            raise ValueError('spec_root_unavailable')
        self.service = SpecsSubsystem(memory_root=self.root, session_summaries=lambda: [], changed_callback=lambda _: None)
        statuses_input = self.service._capture_statuses_input()
        statuses, _order = self.service._load_statuses(captured=statuses_input)
        status_dirs = self.service._work_dirs(statuses=statuses)
        # Only checkpoints opt in; WIP admission still seals the whole board's
        # bytes. read() always binds consumed target bytes to this capture.
        self.index = self.service._scan_folders_by_id(
            status_dirs=status_dirs, strict=True, identity_only=identity_only)
        if _input_token(statuses_input) != _input_token(self.service._capture_statuses_input()):
            raise ValueError('spec_changed')
        self.consumed: dict[Path, ConsumedSpec] = {}

    def resolve(self, spec_id: str) -> tuple[str, Path, str]:
        matches = self.service._matches_from_index(self.index, spec_id)
        if len(matches) != 1:
            raise ValueError('spec_unresolved')
        status, folder = matches[0]
        identity = str(self.index.identities[folder][0])
        return status, folder, identity

    def read(self, spec_id: str) -> tuple[str, Path, str, str]:
        status, folder, identity = self.resolve(spec_id)
        path = folder / 'spec.md'
        before, content, _read_error = self.service._read_identity_input(path)
        scanned = self.service._identity_cache.get(path)
        if (before is None or scanned is None or scanned[0] != before
                or scanned[3] != _content_digest(content)):
            raise ValueError('spec_changed')
        text = content.decode('utf-8')
        previous = self.consumed.get(path)
        current = ConsumedSpec(path, before, content)
        if previous is not None and previous != current:
            raise ValueError('spec_changed')
        self.consumed[path] = current
        return status, folder, identity, text

@dataclass(frozen=True)
class ConsumedSpec:
    path: Path
    fingerprint: tuple
    content: bytes
