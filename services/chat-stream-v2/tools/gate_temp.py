"""Disposable root policy shared by the gate launcher and its test harness."""
import os
from pathlib import Path


def disposable_location(root: Path, system_temp: Path, *, test_base: str | None = None) -> bool:
    bases = [system_temp]
    for value in (os.environ.get("V2_GATE_EVIDENCE_DIR"), test_base):
        if value:
            bases.append(Path(value).expanduser())
    resolved = root.resolve()
    return any(resolved.is_relative_to(base.resolve()) for base in bases)
