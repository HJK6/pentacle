#!/usr/bin/env python3
"""Verify six retained unit partitions before attaching one real smoke receipt.

This has its own evidence schema: it never fabricates a run_gate merge receipt.
Only pytest (the repository pin) is needed by the unchanged journal summarizer.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/chat-stream-v2"))
from tools.gate_progress import summarize  # noqa: E402

SCHEMA = "pentacle.v2.ci-daemon-union.v1"
ASSIGNMENT = "sha256(nodeid file prefix) mod shard_count"


class InvalidEvidence(ValueError):
    pass


def require(ok: bool, message: str) -> None:
    if not ok:
        raise InvalidEvidence(message)


def read_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(data, dict), "evidence must be an object")
    return data


def file_fact(path: Path) -> dict:
    require(path.is_file() and not path.is_symlink(), "missing or linked evidence file")
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def git(checkout: Path, ref: str) -> str:
    return subprocess.check_output(["git", "rev-parse", ref], cwd=checkout, text=True,
                                   stderr=subprocess.PIPE, timeout=15).strip()


def receipt(path: Path, tier: str, sha: str) -> dict:
    data = read_json(path)
    require(data.get("schema") == "pentacle.v2.gate-evidence.v1" and data.get("gate") == tier,
            "wrong gate receipt schema or tier")
    require(data.get("finished") is True and data.get("passed") is True and data.get("sha") == sha,
            "unfinished, failed or wrong-source receipt")
    source = data.get("source", {})
    require(source.get("sha_before") == source.get("sha_after") == sha
            and source.get("clean_before") is True and source.get("clean_after") is True,
            "gate source integrity differs")
    tiers = data.get("tiers")
    require(isinstance(tiers, list) and len(tiers) == 1 and isinstance(tiers[0], dict), "expected one tier")
    unit = tiers[0]
    require(unit.get("tier") == tier and unit.get("passed") is True
            and unit.get("secondary_outcomes") == [], "tier failed or has secondary outcomes")
    counts = unit.get("counts", {})
    require(all(type(counts.get(k)) is int and counts[k] >= 0 for k in ("tests", "failures", "errors", "skipped")),
            "invalid tier counts")
    require(counts["failures"] == counts["errors"] == 0 and counts["tests"] > 0, "non-green tier counts")
    duration = counts.get("duration_s")
    require(type(duration) in (float, int) and math.isfinite(duration) and duration >= 0, "invalid tier duration")
    return unit


def journal(path: Path, *, assigned: dict | None = None) -> dict:
    facts = summarize(path)
    require(all(facts.get(k) is True for k in ("present", "run_started", "run_finished", "complete")),
            "missing or incomplete journal")
    require(type(facts.get("exitstatus")) is int and facts["exitstatus"] == 0
            and facts.get("failed") == [] and facts.get("failed_overflow") == 0
            and facts.get("malformed_lines") == 0
            and all(facts.get(k) is False for k in ("truncated_tail", "budget_exhausted", "collection_mismatch")),
            "damaged or failed journal")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    require(all(type(r.get("seq")) is int for r in rows)
            and [r.get("seq") for r in rows] == list(range(1, len(rows) + 1)), "journal sequence differs")
    collections = [r for r in rows if r.get("kind") == "collected"]
    require(len(collections) == 1, "journal must have one collection")
    count = facts.get("collected")
    require(type(count) is int and count > 0 and count == facts["started_nodes"] == facts["completed_nodes"],
            "journal execution count differs")
    if assigned is not None:
        require(count == assigned["count"] and collections[0].get("nodeids_sha256") == assigned["nodeids_sha256"],
                "journal collection differs from assignment")
        expected = [n["node"] for n in assigned["nodes"]]
        require([r.get("node") for r in rows if r.get("kind") == "start"] == expected
                and [r.get("node") for r in rows if r.get("kind") == "finish"] == expected,
                "journal executed nodes differ from assignment")
    return facts


def junit(path: Path, counts: dict) -> tuple[set[str], set[tuple[str, int, str]]]:
    root = ET.parse(path).getroot()
    require(root.tag in ("testsuite", "testsuites"), "invalid JUnit root")
    require(not list(root.iter("failure")) and not list(root.iter("error")), "JUnit contains failure or error")
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    for key in ("tests", "failures", "errors", "skipped"):
        require(sum(int(s.attrib[key]) for s in suites) == counts[key], "JUnit/receipt counts differ")
    duration = sum(float(s.attrib["time"]) for s in suites)
    require(math.isfinite(duration) and duration == counts["duration_s"], "JUnit/receipt duration differs")
    nodes, skips = set(), set()
    for case in root.iter("testcase"):
        skipped = case.find("skipped")
        if case.get("classname") == "" and skipped is not None and skipped.get("message") == "collection skipped":
            origin = ast.literal_eval(skipped.text or "")
            require(isinstance(origin, tuple) and len(origin) == 3 and isinstance(origin[0], str)
                    and type(origin[1]) is int and isinstance(origin[2], str), "invalid collection skip origin")
            file = origin[0].replace("\\", "/")
            marker = "services/chat-stream-v2/"
            file = marker + file.split(marker, 1)[1] if marker in file else file
            skips.add((file, origin[1], origin[2]))
        else:
            require(bool(case.get("classname")) and bool(case.get("name")), "unidentified JUnit runtime row")
            node = f"{case.get('classname')}::{case.get('name')}"
            require(node not in nodes, "duplicate JUnit runtime row")
            nodes.add(node)
    return nodes, skips


def digest_list(values: object, count: int) -> None:
    require(isinstance(values, list) and len(values) == count
            and all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v) for v in values)
            and len(set(values)) == count, "invalid node digest list")


def collection(data: dict, *, assigned: bool) -> list[str]:
    count = data.get("count")
    require(type(count) is int and count > 0, "empty or invalid assignment/collection")
    require(isinstance(data.get("nodeids_sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", data["nodeids_sha256"]) is not None, "invalid collection digest")
    files = data.get("files")
    require(isinstance(files, list) and files and all(isinstance(f, str) and f for f in files)
            and files == sorted(set(files)), "invalid collection files")
    if not assigned:
        digests = data.get("node_digests")
        digest_list(digests, count)
        require(digests == sorted(digests), "eligible digests are not ordered")
        return digests
    nodes = data.get("nodes")
    require(isinstance(nodes, list) and len(nodes) == count
            and all(isinstance(n, dict) and isinstance(n.get("node"), str) and n["node"] for n in nodes),
            "invalid assigned nodes")
    digests = [n.get("sha256") for n in nodes]
    digest_list(digests, count)
    require(sorted({n["node"].split("::", 1)[0] for n in nodes}) == files, "assigned files/nodes differ")
    return digests


def verify(args: argparse.Namespace, result: dict) -> None:
    require(args.shard_count >= 2, "invalid shard count")
    sha, tree = git(args.checkout, "HEAD"), git(args.checkout, "HEAD^{tree}")
    require(sha == args.expect_sha, "join HEAD differs from expected source")
    result.update(sha=sha, tree=tree, shard_count=args.shard_count)
    dirs = sorted(args.cells.iterdir())
    require(len(dirs) == args.shard_count and all(d.is_dir() and not d.is_symlink() for d in dirs),
            "missing or extra cell directory")
    eligible = rootpath = common_skips = None
    indices, assigned_union, runtime_union, file_union = set(), set(), set(), set()
    cells = []
    for directory in dirs:
        manifests = list(directory.rglob("unit.shard-manifest.json"))
        require(len(manifests) == 1, "missing or duplicate cell manifest")
        path = manifests[0]
        data = read_json(path)
        index = data.get("shard_index")
        require(data.get("schema") == "pentacle.v2.gate-shard-manifest.v1" and data.get("assignment") == ASSIGNMENT,
                "wrong shard schema or assignment")
        require(type(index) is int and 0 <= index < args.shard_count and index not in indices
                and type(data.get("shard_count")) is int and data["shard_count"] == args.shard_count,
                "invalid or duplicate cell index")
        indices.add(index)
        current_eligible, assigned = data["eligible"], data["assigned"]
        eligible_digests = collection(current_eligible, assigned=False)
        digests = collection(assigned, assigned=True)
        require(isinstance(data.get("rootpath"), str) and bool(data["rootpath"]), "invalid rootpath")
        if eligible is None:
            eligible, rootpath = current_eligible, data["rootpath"]
        require(current_eligible == eligible and data["rootpath"] == rootpath, "eligible collections differ")
        require(set(digests) <= set(eligible_digests) and not (assigned_union & set(digests)),
                "overlapping or ineligible assignment")
        assigned_union.update(digests)
        files = set(assigned["files"])
        require(not (file_union & files) and files <= set(eligible["files"]), "overlapping or ineligible file")
        require(all(int(hashlib.sha256(f.encode("utf-8")).hexdigest(), 16) % args.shard_count == index for f in files),
                "file assigned to wrong cell")
        file_union.update(files)
        gate = path.parent
        unit = receipt(gate / "v2-unit-evidence.json", "unit", sha)
        journal(gate / "unit.progress.jsonl", assigned=assigned)
        nodes, skips = junit(gate / "unit.junit.xml", unit["counts"])
        require(len(nodes) == assigned["count"] and not (runtime_union & nodes), "JUnit assignment count or overlap differs")
        runtime_union.update(nodes)
        if common_skips is None:
            common_skips = skips
        require(skips == common_skips, "collection skip identities differ")
        cells.append({"index": index, "files": [file_fact(gate / name) for name in
                      ("unit.shard-manifest.json", "v2-unit-evidence.json", "unit.junit.xml", "unit.progress.jsonl")],
                      "counts": unit["counts"]})
    require(indices == set(range(args.shard_count)) and sorted(assigned_union) == eligible["node_digests"]
            and file_union == set(eligible["files"]) and len(runtime_union) == eligible["count"],
            "unit union does not exactly cover eligible collection")
    result.update(cells=sorted(cells, key=lambda c: c["index"]), eligible=eligible, rootpath=rootpath,
                  collection_skips=sorted(common_skips),
                  union={"assigned_nodes": len(assigned_union), "runtime_rows": len(runtime_union),
                         "files": len(file_union), "disjoint": True, "covers_eligible": True}, unit_union_passed=True)


def finalize(args: argparse.Namespace, result: dict) -> None:
    previous = read_json(args.union)
    require(previous.get("schema") == SCHEMA and previous.get("unit_union_passed") is True
            and previous.get("passed") is False and not previous.get("failures") and "smoke" not in previous,
            "unit union is missing, failed or already finalized")
    for cell in previous["cells"]:
        for fact in cell["files"]:
            require(file_fact(Path(fact["path"])) == fact, "unit evidence changed after verify")
    smoke = receipt(args.smoke, "smoke", previous["sha"])
    gate = args.smoke.parent
    journal(gate / "smoke.progress.jsonl")
    junit(gate / "smoke.junit.xml", smoke["counts"])
    result.update(previous)
    result.update(passed=True, smoke={"counts": smoke["counts"], "files": [file_fact(p) for p in
                  (args.smoke, gate / "smoke.junit.xml", gate / "smoke.progress.jsonl")]})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("verify")
    check.add_argument("--cells", type=Path, required=True)
    check.add_argument("--shard-count", type=int, required=True)
    check.add_argument("--expect-sha", required=True)
    check.add_argument("--checkout", type=Path, required=True)
    finish = sub.add_parser("finalize")
    finish.add_argument("--union", type=Path, required=True)
    finish.add_argument("--smoke", type=Path, required=True)
    for command in (check, finish):
        command.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = {"schema": SCHEMA, "run_id": os.environ.get("GITHUB_RUN_ID"),
              "attempt": os.environ.get("GITHUB_RUN_ATTEMPT"), "event": os.environ.get("GITHUB_EVENT_NAME"),
              "unit_union_passed": False, "passed": False, "failures": []}
    try:
        (verify if args.command == "verify" else finalize)(args, result)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError, ET.ParseError, subprocess.SubprocessError) as exc:
        result["passed"] = False
        result["failures"] = [str(exc) if isinstance(exc, InvalidEvidence) else type(exc).__name__]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temp = args.out.with_name(args.out.name + ".tmp")
    temp.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, args.out)
    return 0 if result["unit_union_passed"] and not result["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
