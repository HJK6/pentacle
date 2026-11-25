"""Prepare, but never push, a one-file quarantine relay bundle."""
from __future__ import annotations

from .reliability_selection import QUARANTINE_PATH, validate_quarantine

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _git(root, *args, env=None):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True,
                                   stderr=subprocess.PIPE, env=env, timeout=120).strip()


def prepare_bundle(remote, checker, checker_sha256, output, selected, *, activations, python=sys.executable):
    """Pin main, make a single list commit, and require the fleet handoff gate.

    The caller verifies the remote against its authorized source and delivers
    the resulting packet to the relay. No public write is performed here.
    """
    if _sha(checker) != checker_sha256:
        raise ValueError("fleet checker hash mismatch")
    if (not isinstance(selected, (list, set, tuple)) or any(
            not isinstance(test, str) or not test.startswith(("daemon::", "cli::", "shared::")) or "::" not in test for test in selected)):
        raise ValueError("invalid effective quarantine list")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="quarantine-relay-", dir=output) as temporary:
        root = Path(temporary) / "checkout"
        subprocess.run(["git", "clone", "--no-local", "--branch", "main", "--single-branch", remote, str(root)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120)
        base = _git(root, "rev-parse", "HEAD")
        actual_remote = _git(root, "remote", "get-url", "origin")
        if actual_remote != remote:
            raise ValueError("unexpected repository remote")
        inventory = root / QUARANTINE_PATH
        data = json.loads(inventory.read_text())
        existing = validate_quarantine(data)
        rows = data["nonblocking_tests"]
        additions = sorted(set(selected) - existing)
        for test in additions:
            row = activations.get(test)
            if not isinstance(row, dict) or row.get('id') != test:
                raise ValueError('new quarantine row unavailable')
            rows.append(row)
        validate_quarantine(data)
        # Retain every row on the freshly cloned main, including concurrent ones.
        data["nonblocking_tests"] = sorted(rows, key=lambda row: row['id'])
        inventory.write_text(json.dumps(data, indent=2) + "\n")
        if not _git(root, "status", "--porcelain", "--", QUARANTINE_PATH):
            return {"state": "already_present", "base_sha": base}
        _git(root, "switch", "-c", "quarantine-relay")
        _git(root, "add", QUARANTINE_PATH)
        base_date = _git(root, "show", "-s", "--format=%cI", base)
        env = {**os.environ, "GIT_AUTHOR_NAME": "Pentacle Agent", "GIT_COMMITTER_NAME": "Pentacle Agent",
               "GIT_AUTHOR_EMAIL": "agent@users.noreply.github.com", "GIT_COMMITTER_EMAIL": "agent@users.noreply.github.com",
               "GIT_AUTHOR_DATE": base_date, "GIT_COMMITTER_DATE": base_date}
        _git(root, "-c", "commit.gpgsign=false", "commit", "-m", "Update effective quarantine list",
             "-m", "\n".join("Coverage-Drop: " + test for test in additions), env=env)
        tip = _git(root, "rev-parse", "HEAD")
        if _git(root, "diff", "--name-only", base, tip) != QUARANTINE_PATH:
            raise ValueError("relay bundle is not a one-file change")
        proof = subprocess.check_output([python, str(checker), "--root", str(root), "--fleet-base", base,
                                         "--fleet-tip", tip], timeout=120)
        receipt = json.loads(proof)
        if (receipt.get("passed") is not True or type(receipt.get("unexcepted_match_count")) is not int
                or receipt.get("unexcepted_match_count") != 0
                or receipt.get("rule_hits") != [] or receipt.get("base") != base or receipt.get("tip") != tip):
            raise ValueError("fleet handoff gate failed")
        bundle = Path(temporary) / "change.bundle"
        ref = "refs/heads/quarantine-relay"
        _git(root, "bundle", "create", str(bundle), ref, "^" + base)
        _git(root, "bundle", "verify", str(bundle))
        os.replace(bundle, output / "change.bundle")
        receipt_path = output / "fleet-receipt.json"
        receipt_path.write_bytes(proof)
        packet = {"state": "ready_for_fd_relay", "repository": remote, "base_sha": base,
                  "candidate_sha": tip, "candidate_ref": ref,
                  "bundle": {"path": str(output / "change.bundle"), "sha256": _sha(output / "change.bundle")},
                  "fleet_checker_sha256": checker_sha256,
                  "fleet_guard": {"path": str(receipt_path), "sha256": _sha(receipt_path), **receipt}}
        (output / "relay-packet.json").write_text(json.dumps(packet, indent=2) + "\n")
        return packet
