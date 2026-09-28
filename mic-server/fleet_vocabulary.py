"""Fleet vocabulary + name-correction config for the /transcribe adapter route.

The upload transcription route biases the recognizer toward fleet names with an
``initial_prompt`` and applies an optional post-hoc name-correction map. Both are
DATA (config), not code, and are versioned together in ``vocabulary_version`` so a
test/daemon can prove the fleet profile was applied.

Config (env), applied ONLY to the upload route so the live wake/content path keeps
its no-content-bias rule:
- ``MIC_VOCABULARY_FILE``: a text file whose contents become the ``initial_prompt``
  (the fleet names, e.g. "Bartimaeus, Bart, Samplehost, Thirdhost, Otherhost, Pentacle,
  Altum, Daffodil, Triforce, Nexus, Astra, Luna, Sol, Opus, Fable").
- ``MIC_NAME_CORRECTIONS``: a JSON object mapping a known mis-transcription variant
  to its canonical fleet name. Replacement is whole-word and case-insensitive and
  applies ONLY to listed variants, never inside a longer word — so a decoy phrase
  the speaker actually said is not rewritten into a fleet name (C5: zero decoy
  false positives).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Dict, Optional, Tuple


def load_vocabulary(
    *,
    vocabulary_file: Optional[str] = None,
    corrections_file: Optional[str] = None,
) -> Tuple[Optional[str], Dict[str, str], str]:
    """Return ``(initial_prompt, corrections, vocabulary_version)``.

    Paths default to the ``MIC_VOCABULARY_FILE`` / ``MIC_NAME_CORRECTIONS`` env
    vars. A missing/empty vocabulary file yields ``initial_prompt=None`` (the
    caller decides whether that is acceptable for the requested profile)."""
    vf = vocabulary_file if vocabulary_file is not None else os.environ.get("MIC_VOCABULARY_FILE")
    cf = corrections_file if corrections_file is not None else os.environ.get("MIC_NAME_CORRECTIONS")

    prompt: Optional[str] = None
    if vf and os.path.exists(vf):
        with open(vf, encoding="utf-8") as handle:
            prompt = handle.read().strip() or None

    corrections: Dict[str, str] = {}
    if cf and os.path.exists(cf):
        with open(cf, encoding="utf-8") as handle:
            raw = json.load(handle)
        if isinstance(raw, dict):
            corrections = {str(k): str(v) for k, v in raw.items() if str(k)}

    return prompt, corrections, vocabulary_version(prompt, corrections)


def vocabulary_version(prompt: Optional[str], corrections: Dict[str, str]) -> str:
    """Deterministic digest over the applied prompt + correction map."""
    digest = hashlib.sha256()
    digest.update((prompt or "").encode("utf-8"))
    digest.update(b"\x00")
    digest.update(json.dumps(corrections, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return "fleet-" + digest.hexdigest()[:12]


def apply_corrections(text: str, corrections: Dict[str, str]) -> str:
    """Whole-word, case-insensitive replacement of listed variants only.

    Never rewrites inside a longer word (word boundaries), so it corrects a known
    mis-transcription of a real name without turning an unrelated word or a decoy
    phrase into a fleet name."""
    if not text or not corrections:
        return text
    out = text
    # Longest variants first so a multi-word variant wins over a substring rule.
    for variant in sorted(corrections, key=len, reverse=True):
        canonical = corrections[variant]
        pattern = re.compile(r"\b" + re.escape(variant) + r"\b", re.IGNORECASE)
        out = pattern.sub(canonical, out)
    return out
