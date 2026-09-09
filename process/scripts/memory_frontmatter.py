"""Memory frontmatter helpers (Spec A).

Replaces the custom hand-rolled parser in `validate_memory_v2.py:36-98`.
Both `validate_memory_v2.py` and `generate_catalog.py` use these functions.

API:
    load_frontmatter(path) -> dict
        Read file, extract frontmatter block, parse with StrictMemoryLoader,
        coerce datetime.date back to ISO string.

    load_schema(path) -> dict
        Load schema/document.schema.json. Validates the schema itself with
        Draft202012Validator.check_schema. Raises SchemaInvalidError on
        any failure (missing file, JSON parse error, invalid schema syntax).

    validate_frontmatter(metadata, schema) -> list[str]
        Validate a parsed frontmatter dict against a schema. Returns a list
        of human-readable error strings; empty list = valid.
"""

import datetime
import json
import sys
from pathlib import Path

import jsonschema
import yaml

# Sibling import
sys.path.insert(0, str(Path(__file__).parent))
from strict_memory_loader import StrictLoaderError, StrictMemoryLoader


class SchemaInvalidError(Exception):
    """Raised when document.schema.json itself is missing/malformed/invalid."""


def _coerce_dates(obj):
    """Recursively replace datetime.date values with ISO strings."""
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    if isinstance(obj, datetime.date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _coerce_dates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_coerce_dates(item) for item in obj]
    return obj


def load_frontmatter_and_body(path: Path) -> tuple[dict, str]:
    """Parse YAML frontmatter and return ``(metadata, body)``.

    ``body`` is the document content after the closing ``---`` line (may be the
    empty string). Splitting/validation is identical to ``load_frontmatter``; this
    variant additionally returns the body for consumers that need to index or read
    it for optional search or display integrations.

    Raises:
        ValueError: missing or malformed frontmatter block.
        StrictLoaderError: YAML policy violation (anchor/alias/dup-key).
        yaml.YAMLError: YAML syntax error.
    """
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"{path}: missing frontmatter start")
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        raise ValueError(f"{path}: invalid frontmatter block (no closing ---)")
    fm_text = parts[1]
    body = parts[2]
    try:
        data = yaml.load(fm_text, Loader=StrictMemoryLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: YAML parse error: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: frontmatter is not a mapping")
    return _coerce_dates(data), body


def load_frontmatter(path: Path) -> dict:
    """Parse YAML frontmatter from a memory MD file (body discarded).

    Use ``load_frontmatter_and_body`` when the body is needed.

    Raises:
        ValueError: missing or malformed frontmatter block.
        StrictLoaderError: YAML policy violation (anchor/alias/dup-key).
        yaml.YAMLError: YAML syntax error.
    """
    metadata, _ = load_frontmatter_and_body(path)
    return metadata


def load_schema(path: Path) -> dict:
    """Load and validate a JSON Schema file.

    Returns the parsed schema dict. Raises SchemaInvalidError on any
    failure path: missing file, JSON parse error, or invalid schema syntax.
    """
    if not path.exists():
        raise SchemaInvalidError(f"schema file missing: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SchemaInvalidError(f"schema file unreadable: {path}: {exc}") from exc
    try:
        schema = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SchemaInvalidError(
            f"schema file malformed: {path}:{exc.lineno}:{exc.colno}: {exc.msg}"
        ) from exc
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:
        raise SchemaInvalidError(f"schema invalid: {path}: {exc.message}") from exc
    return schema


def validate_frontmatter(metadata: dict, schema: dict) -> list[str]:
    """Validate parsed frontmatter against a JSON Schema.

    Returns a list of error strings. Empty list = valid. Each error includes
    the JSON pointer to the offending location.
    """
    validator = jsonschema.Draft202012Validator(schema)
    errors = []
    for err in sorted(validator.iter_errors(metadata), key=lambda e: list(e.absolute_path)):
        pointer = "/" + "/".join(str(p) for p in err.absolute_path) if err.absolute_path else "/"
        errors.append(f"schema {pointer}: {err.message}")
    return errors
