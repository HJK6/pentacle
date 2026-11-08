"""Read the shared prose contract; regexes and examples live in the fixture."""
from __future__ import annotations

import json
import re
from pathlib import Path

CONTRACT_PATH = Path(__file__).resolve().parents[2] / "pentacle-chat-core/tests/fixtures/summary_prose_cases.json"
CONTRACT = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
PATTERNS = {key: re.compile(value) for key, value in CONTRACT["patterns"].items()}


def summary_line(value: str | None) -> str | None:
    """Accept a passing first sentence, even when the rest of the label is noisy."""
    if not value:
        return None
    value = value.strip()
    label = PATTERNS["label"].match(value)
    if label:
        value = label[2].strip()
    for terminal in PATTERNS["terminal"].finditer(value):
        sentence = value[:terminal.end()].strip()
        if any(sentence.lower().endswith(abbr) for abbr in CONTRACT["abbreviations"]):
            continue
        if len(sentence) > CONTRACT["limit"] or any(
                PATTERNS[key].search(sentence)
                for key in ("semicolon", "hex", "seat", "work_id", "path", "joined_token")):
            return None
        return sentence
    return None
