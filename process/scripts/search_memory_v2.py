#!/usr/bin/env python3

import argparse
import json
import math
import sys
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from memory_config import catalog_staleness_warning


ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ROOT / "catalog" / "documents.json"
# Terminal specs live in the archive index, searched only with --archive/--all.
# See the workspace contract (catalog partitioning).
ARCHIVE_PATH = ROOT / "catalog" / "archive.json"


def tokenize(text: str) -> list[str]:
    cleaned = []
    for ch in text.lower():
        cleaned.append(ch if ch.isalnum() else " ")
    return [part for part in "".join(cleaned).split() if part]


def is_fuzzy_match(term: str, candidate: str) -> bool:
    if term == candidate:
        return True
    if len(term) < 4 or len(candidate) < 4:
        return False

    ratio = SequenceMatcher(None, term, candidate).ratio()
    if ratio >= 0.84:
        return True

    if abs(len(term) - len(candidate)) > 2:
        return False

    previous = list(range(len(candidate) + 1))
    for i, left in enumerate(term, start=1):
        current = [i]
        row_min = current[0]
        for j, right in enumerate(candidate, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (left != right),
                )
            )
            row_min = min(row_min, current[j])
        previous = current
        if row_min > 2:
            return False

    distance = previous[-1]
    return distance <= (1 if max(len(term), len(candidate)) <= 5 else 2)


def term_score(term: str, candidates: set[str], exact_weight: int, fuzzy_weight: int) -> int:
    if term in candidates:
        return exact_weight
    if any(is_fuzzy_match(term, candidate) for candidate in candidates):
        return fuzzy_weight
    return 0


def flatten_metadata(value) -> str:
    if isinstance(value, dict):
        return " ".join(flatten_metadata(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(flatten_metadata(item) for item in value)
    if value is None:
        return ""
    return str(value)


def score_entry(query_terms: list[str], entry: dict) -> int:
    title_terms = set(tokenize(entry["title"]))
    summary_terms = set(tokenize(entry["summary"]))
    tag_terms = set(tokenize(" ".join(entry.get("tags", []))))
    alias_terms = set(tokenize(" ".join(entry.get("aliases", []))))
    type_terms = set(tokenize(entry["type"]))
    location_terms = set(
        tokenize(
            " ".join(
                flatten_metadata(entry.get(key, []))
                for key in ("repos", "non_repo_paths", "specs", "logs")
            )
        )
    )

    score = 0
    for term in query_terms:
        score += term_score(term, title_terms, 6, 4)
        score += term_score(term, alias_terms, 5, 3)
        score += term_score(term, tag_terms, 4, 2)
        score += term_score(term, location_terms, 4, 2)
        score += term_score(term, summary_terms, 2, 1)
        score += term_score(term, type_terms, 1, 1)
    return score


def _load_slice(path: Path, slice_name: str) -> list:
    """Load catalog entries from `path`, tagging each with its origin slice."""
    if not path.exists():
        return []
    return [(entry, slice_name) for entry in json.loads(path.read_text(encoding="utf-8"))]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Rank memory catalog entries against a query. By default only the "
        "active index (documents.json) is searched; use --archive to also search "
        "terminal specs in archive.json.",
        epilog="To search for a term that starts with a dash, put it after `--`, "
        "e.g. `search_memory_v2.py -- --some-flag-name`.",
    )
    parser.add_argument("query", nargs="+", help="search terms")
    parser.add_argument(
        "--archive", "--all", dest="archive", action="store_true",
        help="also search the terminal archive (completed/deprecated specs)",
    )
    args = parser.parse_args(argv)

    query_terms = tokenize(" ".join(args.query))

    warning = catalog_staleness_warning(CATALOG_PATH)
    if warning:
        print(warning, file=sys.stderr)

    tagged = _load_slice(CATALOG_PATH, "active")
    if args.archive:
        tagged += _load_slice(ARCHIVE_PATH, "archive")

    ranked = [
        (score_entry(query_terms, entry), entry["id"], entry, slice_name)
        for entry, slice_name in tagged
    ]
    ranked.sort(key=lambda item: (-item[0], item[1]))

    if not ranked or ranked[0][0] <= 0:
        return 0

    threshold = max(4, math.ceil(ranked[0][0] * 0.35))

    for score, _, entry, slice_name in ranked[:5]:
        if score < threshold:
            continue
        marker = "" if slice_name == "active" else " [archive]"
        print(f"{score:>3}  {entry['id']:30}{marker}  {entry['path']}")
        print(f"     {entry['summary']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
