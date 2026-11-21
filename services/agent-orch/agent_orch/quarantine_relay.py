"""Prepare, but never push, a one-file quarantine relay bundle."""
from __future__ import annotations

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
            not isinstance(test, str) or not test or "::" not in test for test in selected)):
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
        inventory = root / "scripts/quarantine.json"
        data = json.loads(inventory.read_text())
        if (not isinstance(data, dict) or data.get("version") != 1
                or not isinstance(data.get("nonblocking_tests"), list)
                or any(not isinstance(t, str) or not t or "::" not in t for t in data["nonblocking_tests"])):
            raise ValueError("invalid published quarantine inventory")
        history = data.get('history')
        if not isinstance(history, dict) or any(not isinstance(k, str) or not isinstance(v, dict)
                                                or not isinstance(v.get('activation'), str) or not v['activation']
                                                or not isinstance(v.get('file'), str)
                                                or not isinstance(v.get('parents'), list)
                                                or any(not isinstance(parent, str) or not parent for parent in v['parents'])
                                                for k, v in history.items()):
            raise ValueError('quarantine history unavailable')
        if any(hashlib.sha256(test.encode()).hexdigest() not in history for test in data['nonblocking_tests']):
            raise ValueError('active quarantine is missing its history')
        for test in set(selected) - set(data['nonblocking_tests']):
            activation = activations.get(test)
            if (not isinstance(activation, dict) or not isinstance(activation.get('activation'), str)
                    or not activation['activation'] or not isinstance(activation.get('parents'), list)
                    or any(not isinstance(parent, str) or not parent for parent in activation['parents'])):
                raise ValueError('new quarantine activation identity unavailable')
            raw = test.split('::', 1)[1] if test.startswith(('daemon::', 'cli::', 'shared::')) else test
            history[hashlib.sha256(test.encode()).hexdigest()] = {
                'activation': activation['activation'], 'parents': activation['parents'],
                'file': hashlib.sha256(raw.split('::', 1)[0].encode()).hexdigest(),
            }
        # Concurrent accepted additions must never be silently removed.
        data["nonblocking_tests"] = sorted(set(data["nonblocking_tests"]) | set(selected))
        inventory.write_text(json.dumps(data, indent=2) + "\n")
        if not _git(root, "status", "--porcelain", "--", "scripts/quarantine.json"):
            return {"state": "already_present", "base_sha": base}
        _git(root, "switch", "-c", "quarantine-relay")
        _git(root, "add", "scripts/quarantine.json")
        base_date = _git(root, "show", "-s", "--format=%cI", base)
        env = {**os.environ, "GIT_AUTHOR_NAME": "Pentacle Agent", "GIT_COMMITTER_NAME": "Pentacle Agent",
               "GIT_AUTHOR_EMAIL": "agent@users.noreply.github.com", "GIT_COMMITTER_EMAIL": "agent@users.noreply.github.com",
               "GIT_AUTHOR_DATE": base_date, "GIT_COMMITTER_DATE": base_date}
        _git(root, "-c", "commit.gpgsign=false", "commit", "-m", "Update effective quarantine list", env=env)
        tip = _git(root, "rev-parse", "HEAD")
        if _git(root, "diff", "--name-only", base, tip) != "scripts/quarantine.json":
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
