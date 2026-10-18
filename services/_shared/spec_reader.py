"""Read-only spec resolver used by WIP admission."""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .specs_service import SpecsSubsystem, _resolve_specs_memory_root, _bounded_directory

class SpecReader:
    """One fresh resolver index per check; no observer, cached catalog or writes."""
    def __init__(self, root: str | Path | None) -> None:
        self.root = Path(root) if root is not None else _resolve_specs_memory_root()[0]
        if self.root is None or not (self.root / 'work').is_dir():
            raise ValueError('spec_root_unavailable')
        self.service = SpecsSubsystem(memory_root=self.root, session_summaries=lambda: [], changed_callback=lambda _: None)
        status_dirs = self.service._work_dirs()
        self.index = self.service._scan_folders_by_id(status_dirs=status_dirs, strict=True)
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
        before = self.service._identity_fingerprint(path)
        scanned = self.service._identity_cache.get(path)
        if before is None or scanned is None or scanned[0] != before:
            raise ValueError('spec_changed')
        directory_before = _directory_fingerprint(folder)
        # Neither the file nor any ancestor may redirect the consumed read.
        with _bounded_directory(folder) as directory:
            fd = os.open('spec.md', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            with os.fdopen(fd, 'rb') as source:
                value = os.fstat(source.fileno())
                opened = (str(path), value.st_dev, value.st_ino, value.st_size,
                          value.st_mtime_ns, value.st_ctime_ns)
                if not stat.S_ISREG(value.st_mode) or opened != before:
                    raise ValueError('spec_changed')
                content = source.read()
        if (self.service._identity_fingerprint(path) != before
                or _directory_fingerprint(folder) != directory_before):
            raise ValueError('spec_changed')
        text = content.decode('utf-8')
        previous = self.consumed.get(path)
        current = ConsumedSpec(path, before, content)
        if previous is not None and previous != current:
            raise ValueError('spec_changed')
        self.consumed[path] = current
        return status, folder, identity, text


def _directory_fingerprint(path: Path):
    try:
        value = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(value.st_mode):
        raise ValueError('spec_directory_changed')
    return value.st_dev, value.st_ino, value.st_mtime_ns, value.st_ctime_ns


@dataclass(frozen=True)
class ConsumedSpec:
    path: Path
    fingerprint: tuple
    content: bytes

