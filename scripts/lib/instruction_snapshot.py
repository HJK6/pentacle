"""The finite, read-only instruction input used by cli-pytest only."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

ENV_NAME = "AGENT_ORCH_MEMORY_REPO"
MANIFEST = "instruction-manifest.json"


def instruction_snapshot(value):
    def need(condition, message):
        if not condition:
            raise ValueError("instruction snapshot: " + message)

    need(isinstance(value, str) and value, "missing declared input")
    root = Path(value)
    need(root.is_absolute() and root == root.resolve() and root.is_dir(), "invalid root")
    manifest = root / MANIFEST
    try:
        need(not manifest.is_symlink(), "symlink manifest")
        raw = manifest.read_bytes()
        data = json.loads(raw)
        need(isinstance(data, dict) and set(data) == {"provenance", "files"}, "invalid manifest")
        provenance, rows = data["provenance"], data["files"]
        need(isinstance(provenance, dict) and Path(provenance.get("source_root", "")).is_absolute()
             and re.fullmatch(r"[0-9a-f]{40}", provenance.get("source_git_head", ""))
             and provenance.get("observed_at") and provenance.get("scope"), "missing provenance")
        need(isinstance(rows, list) and rows, "missing file membership")
        expected = set()
        for row in rows:
            need(isinstance(row, dict) and set(row) == {"path", "sha256"}, "invalid file binding")
            name = row["path"]
            need(isinstance(name, str) and name and not PurePosixPath(name).is_absolute()
                 and ".." not in PurePosixPath(name).parts and name not in expected
                 and name == PurePosixPath(name).as_posix(), "invalid/duplicate path")
            expected.add(name)
            need(re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) is not None, "invalid digest")
            path = root
            for part in PurePosixPath(name).parts:
                path = path / part
                need(not path.is_symlink(), "symlink input")
            need(hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"], "content drift " + name)
        required = {"AGENTS.md", "docs/config/agent_orchestration.md"}
        need(required <= expected and all(name in required or
             (PurePosixPath(name).parent.as_posix() == "agents" and name.endswith("_baseline.md"))
             for name in expected), "unsupported instruction closure")
        need({"agents/" + name for name in ("qa_baseline.md", "documentation_baseline.md", "doc_qa_baseline.md",
             "nexus_baseline.md", "lead_baseline.md", "evaluator_baseline.md")} <= expected, "missing exercised baseline")
        actual = set()
        for path in [root, *root.rglob("*")]:
            need(not path.is_symlink() and not path.stat().st_mode & 0o222, "writable/symlink input")
            if path.is_file():
                actual.add(path.relative_to(root).as_posix())
        need(actual == expected | {MANIFEST}, "file membership drift")
        return {"root": str(root), "manifest": {"path": str(manifest), "sha256": hashlib.sha256(raw).hexdigest()},
                "files": [{"path": str(root / row["path"]), "sha256": row["sha256"]} for row in rows]}
    except (OSError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError("instruction snapshot: unreadable/malformed input") from exc
