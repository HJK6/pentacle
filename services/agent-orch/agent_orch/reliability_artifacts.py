"""Select only failed-test identities and assertion digests from hosted evidence."""
from __future__ import annotations

import hashlib
from io import BytesIO
import json
import re
import subprocess
from xml.etree import ElementTree as ET
from zipfile import ZipFile


def junit(raw):
    root = ET.fromstring(raw)
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    expected = sum(int(s.attrib[k]) for s in suites for k in ("errors", "failures"))
    facts = []
    for case in root.iter("testcase"):
        for node in case:
            if node.tag not in ("error", "failure"):
                continue
            test = case.get("classname", "") + "::" + case.get("name", "")
            if not re.fullmatch(r"[\w.]+::[\w.\[\]-]+", test):
                test = "unclassified test identity"
            facts.append({"test": test, "assertion_digest": hashlib.sha256(
                node.get("message", "").encode()).hexdigest()})
    if not expected or expected != len(facts):
        raise ValueError("incomplete JUnit failure census")
    return facts


def details(repo, artifact, download):
    if artifact.get("expired") or not 0 < artifact["size_in_bytes"] <= 8_000_000:
        return None
    data = download(repo, f"actions/artifacts/{int(artifact['id'])}/zip")
    with ZipFile(BytesIO(data)) as archive:
        terminal = []
        failures = None
        for member in archive.infolist():
            if member.file_size > 12_000_000:
                continue
            if member.filename == "daemon/gate/unit.junit.xml":
                failures = junit(archive.read(member))
            elif re.fullmatch(r"[^/]+/attempts/[^/]+/\d+-terminal.json", member.filename):
                record = json.loads(archive.read(member))
                first = record.get("first_failure") or {}
                detail = record.get("detail")
                if isinstance(detail, dict):
                    detail = detail.get("sandbox_error")
                if (record.get("state") == "terminal" and first.get("stage") == "setup"
                        and first.get("code") in {"sandbox_incomplete", "sandbox_unavailable"}
                        and isinstance(detail, str) and ("sandbox-exec" in detail or "/System" in detail)):
                    terminal.append(first["code"])
        if terminal:
            return dict(portability="macOS sandbox required in Linux gate setup")
        if failures:
            return dict(tests=failures)
    return None


def job_artifacts(job, rows, rid):
    suffix = f"-{rid}-1"
    shard = re.fullmatch(r"daemon-unit \((\d+)\)", job)
    prefix = f"daemon-unit-{shard[1]}" if shard else "gate-summaries-" + job
    fallback = "supported-gates-" + (f"daemon-unit-{shard[1]}" if shard else job)
    by_name = {r["name"]: r for r in rows}
    return [by_name[name + suffix] for name in (prefix, fallback) if name + suffix in by_name]


def apply_rules(failure, sha, signatures, repo_root):
    if failure.get("portability") and any(r.startswith("ubuntu-") for r in failure["runner"]):
        return [dict(kind="code", cause="gate-requires-macos-on-linux", cause_class="platform assumption",
                     reason=failure["portability"])]
    result = []
    for case in failure.get("tests", []):
        found = None
        for rule in signatures:
            if (rule["step"] != failure["step"] or rule["runner"] not in failure["runner"]
                    or rule["test"] != case["test"] or rule["assertion_digest"] != case["assertion_digest"]):
                continue
            # Non-code attribution applies only with the reviewed fixture AND
            # affected behavior unchanged. Never decide from a retry alone.
            if rule["kind"] == "non_code":
                if not repo_root or not rule.get("paths") or not re.fullmatch("[a-f0-9]{40}", sha):
                    continue
                if not re.fullmatch("[a-f0-9]{40}", rule["revision"]):
                    continue
                same = subprocess.run(["git", "-C", str(repo_root), "diff", "--quiet", "--no-ext-diff",
                                       rule["revision"], sha, "--", *rule["paths"]], check=False,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                if same.returncode:
                    continue
            found = {k: rule[k] for k in ("kind", "cause", "cause_class", "reviewer", "reason", "revision")}
            break
        result.append(dict(**(found or {"kind": "unclassified", "cause": "pending classification"}), **case))
    return result
