"""Disposable root policy for the gate launcher."""
import os
from pathlib import Path


def disposable_location(root: Path, system_temp: Path) -> bool:
    bases = [system_temp]
    evidence = os.environ.get("V2_GATE_EVIDENCE_DIR")
    if evidence:
        bases.append(Path(evidence).expanduser())
    resolved = root.resolve()
    return any(resolved.is_relative_to(base.resolve()) for base in bases)
