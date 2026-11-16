#!/usr/bin/env python3
"""Create a schema-valid work item (spec.md + summary.md) under work/<status>/.

Usage:
    python3 scripts/new_work_item.py <repo> <topic> --title "..." --summary "..." \
        [--status backlog] [--machine local] [--owner agents] [--epic epic_<slug>] \
        [--tag t1 --tag t2] [--dry-run]

Writes work/<status>/<repo>__<topic>/{spec.md,summary.md} from
templates/work_spec.md and templates/work_summary.md with frontmatter that
passes schema/document.schema.json, then validates both files. Ids follow the
workspace convention: spec_<repo>__<topic> / work_<repo>__<topic> with hyphens
mapped to underscores; ids are path-independent and survive status moves.

Refuses to create an item whose folder name already exists in any status
folder (live-name uniqueness and duplicate-id rules in validate_memory_v2.py).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import memory_config  # noqa: E402
from memory_frontmatter import load_frontmatter, load_schema, validate_frontmatter  # noqa: E402

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
ID_RE = re.compile(r"^[a-z0-9_]+$")

def detect_machine() -> str:
    """Use an explicit local label; never infer a private fleet profile."""
    return "local"


def load_statuses(root: Path) -> tuple[list[str], set[str]]:
    data = json.loads((root / "work" / "statuses.json").read_text(encoding="utf-8"))
    names = [s["name"] for s in data["statuses"]]
    terminal = {s["name"] for s in data["statuses"] if s.get("is_terminal")}
    return names, terminal


def to_id_part(slug: str) -> str:
    return slug.replace("-", "_")


def render_frontmatter(meta: dict) -> str:
    dumped = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=80)
    return f"---\n{dumped}---\n"


def render_body(template_path: Path, subs: dict) -> str:
    text = template_path.read_text(encoding="utf-8")
    match = re.match(r"^---\n.*?\n---\n", text, re.S)
    body = text[match.end():] if match else text
    for key, value in subs.items():
        body = body.replace("{{" + key + "}}", value)
    return body


def build(args, root: Path) -> list[tuple[Path, str]]:
    statuses, terminal = load_statuses(root)
    for name, value in (("repo", args.repo), ("topic", args.topic)):
        if not SLUG_RE.match(value):
            raise SystemExit(f"{name} {value!r} must match {SLUG_RE.pattern}")
    if args.status not in statuses:
        raise SystemExit(f"status {args.status!r} not in work/statuses.json ({', '.join(statuses)})")
    if args.status in terminal:
        raise SystemExit(f"status {args.status!r} is terminal; new items start non-terminal")

    item = f"{args.repo}__{args.topic}"
    for status in statuses:
        existing = root / "work" / status / item
        if existing.exists():
            raise SystemExit(f"item {item} already exists at {existing.relative_to(root)}")

    id_stem = f"{to_id_part(args.repo)}__{to_id_part(args.topic)}"
    spec_id, work_id = f"spec_{id_stem}", f"work_{id_stem}"
    for doc_id in (spec_id, work_id):
        if not ID_RE.match(doc_id):
            raise SystemExit(f"generated id {doc_id!r} is not a valid document id")

    if args.epic:
        if not args.epic.startswith("epic_"):
            raise SystemExit("--epic must be an epic id like epic_<slug>")
        epic_path = root / "epics" / f"{args.epic[len('epic_'):]}.md"
        if not epic_path.exists():
            raise SystemExit(f"epic doc not found: {epic_path.relative_to(root)}")

    today = args.date or _dt.date.today().isoformat()
    folder = Path("work") / args.status / item
    common = {
        "title": args.title,
        "status": args.status,
        "canonical": False,
        "created_at": today,
        "updated_at": today,
        "machine": args.machine,
        "owner": args.owner,
    }
    if args.epic:
        common["epic"] = args.epic
    tags = list(dict.fromkeys(args.tag or [args.repo]))

    def meta(doc_id: str, doc_type: str, filename: str, related: str) -> dict:
        out = {"id": doc_id, "title": common["title"], "type": doc_type, "status": common["status"],
               "canonical": False, "created_at": today, "updated_at": today,
               "source_path": (folder / filename).as_posix(),
               "machine": common["machine"], "owner": common["owner"]}
        if args.epic:
            out["epic"] = args.epic
        out["tags"] = tags
        out["summary"] = args.summary
        out["related"] = [related]
        return out

    subs = {"title": args.title, "summary": args.summary, "date": today, "status": args.status,
            "machine": args.machine, "owner": args.owner, "item": item}
    templates = root / "templates"
    spec_text = render_frontmatter(meta(spec_id, "spec", "spec.md", work_id)) + render_body(
        templates / "work_spec.md", subs)
    summary_text = render_frontmatter(meta(work_id, "work", "summary.md", spec_id)) + render_body(
        templates / "work_summary.md", subs)
    return [(root / folder / "spec.md", spec_text), (root / folder / "summary.md", summary_text)]


def validate_files(paths: list[Path], root: Path) -> list[str]:
    schema = load_schema(root / "schema" / "document.schema.json")
    problems = []
    for path in paths:
        try:
            metadata = load_frontmatter(path)
        except Exception as exc:  # parse errors surface as problems, not tracebacks
            problems.append(f"{path}: frontmatter parse failed: {exc}")
            continue
        problems.extend(f"{path}: {err}" for err in validate_frontmatter(metadata, schema))
        folder_status = path.parent.parent.name
        if metadata.get("status") != folder_status:
            problems.append(f"{path}: status {metadata.get('status')!r} != folder {folder_status!r}")
        if metadata.get("source_path") != path.relative_to(root).as_posix():
            problems.append(f"{path}: source_path does not match location")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("repo", help="repo slug, e.g. pentacle or example-app")
    parser.add_argument("topic", help="topic slug, e.g. spawn_ack_reliability_2026_09")
    parser.add_argument("--title", required=True)
    parser.add_argument("--summary", required=True, help="one-to-three sentence retrieval summary")
    parser.add_argument("--status", default="backlog")
    parser.add_argument("--machine", default=None, help="machine label (default: local)")
    parser.add_argument("--owner", default=None, help="default: $AGENT_ORCH_STREAM_ID, else 'agents'")
    parser.add_argument("--epic", default=None, help="epic id, e.g. epic_example")
    parser.add_argument("--tag", action="append", help="repeatable; default: the repo slug")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD override for created_at/updated_at")
    parser.add_argument("--root", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else memory_config.ROOT
    args.machine = args.machine or detect_machine()
    if not args.machine:
        raise SystemExit("could not detect machine from hostname; pass --machine")
    args.owner = args.owner or os.environ.get("AGENT_ORCH_STREAM_ID") or "agents"

    files = build(args, root)
    if args.dry_run:
        for path, text in files:
            print(f"--- {path.relative_to(root)} ---\n{text}")
        return 0
    files[0][0].parent.mkdir(parents=True, exist_ok=False)
    for path, text in files:
        path.write_text(text, encoding="utf-8")
    problems = validate_files([p for p, _ in files], root)
    if problems:
        print("created files FAILED validation:", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1
    for path, _ in files:
        print(path.relative_to(root).as_posix())
    print("next: fill Goal/Current State/Target State/Plan/Validation/Custom AC in spec.md, "
          "then commit only the files you own using your version-control workflow.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
