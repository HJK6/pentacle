"""Bounded metadata-only proof, transportable through Hosts.run_command."""
from __future__ import annotations

import inspect


def probe(expected):
    # Keep this function self-contained: the exact same checked code executes
    # on a peer without depending on that peer's installed Python module path.
    import hashlib
    import json
    import os
    import shlex
    import subprocess

    def command(*args):
        result = subprocess.run(args, capture_output=True, text=True, timeout=8, check=False)
        return result.stdout if result.returncode in (0, 1) else ""

    def process(pid):
        text = command("ps", "-ww", "-p", str(pid), "-o", "pid=,uid=,lstart=,command=")
        parts = text.strip().split(None, 7)
        return parts if len(parts) == 8 and parts[0] == str(pid) else None

    try:
        pid = str(expected.get("pane_pid") or "")
        if not pid.isdecimal() or int(pid) <= 0:
            return {"ok": False, "reason": "missing_binding"}
        binding = expected.get("binding") or {}
        record = process(pid)
        if (not record or int(record[1]) != os.getuid()
                or " ".join(record[2:7]).split() != str(binding.get("pane_started_at") or "").split()
                or str(binding.get("pane_pid")) != pid
                or not binding.get("executable")
                or shlex.split(record[7])[0] != binding["executable"]):
            return {"ok": False, "reason": "provider_identity_changed"}
        # Same bounded pane-descendant census used by the existing rollout resolver.
        parents = []
        for line in command("ps", "-eo", "pid=,ppid=").splitlines():
            fields = line.split()
            if len(fields) == 2 and all(f.isdecimal() for f in fields):
                parents.append((fields[0], fields[1]))
        pids = {pid}
        for _ in range(32):
            grown = pids | {child for child, parent in parents if parent in pids}
            if len(grown) > 256:
                return {"ok": False, "reason": "ambiguous_process_tree"}
            if grown == pids:
                break
            pids = grown
        else:
            return {"ok": False, "reason": "ambiguous_process_tree"}
        files = command("lsof", "-n", "-p", ",".join(sorted(pids)), "-Fn")
        paths = {line[1:] for line in files.splitlines() if line.startswith("n")
                 and "/.codex/sessions/" in line[1:] and line.endswith(".jsonl")}
        if len(paths) != 1:
            return {"ok": False, "reason": "ambiguous_rollout"}
        path = paths.pop()
        anchor = binding.get("transcript") or {}
        with open(path, "rb") as source:
            before = os.fstat(source.fileno())
            head = source.readline(65537)
            if len(head) > 65536 or not head.endswith(b"\n"):
                return {"ok": False, "reason": "unbounded_header"}
            header = json.loads(head)
            native = header.get("payload", {}).get("id")
            if header.get("type") != "session_meta" or native != expected.get("native_session_id"):
                return {"ok": False, "reason": "native_session_changed"}
            digest = hashlib.sha256(
                f"{os.path.abspath(path)}\0{before.st_dev}\0{before.st_ino}".encode("utf-8", "surrogateescape")
            ).hexdigest()
            if anchor.get("source_file_identity_digest"):
                valid_anchor = digest == anchor["source_file_identity_digest"]
            else:
                valid_anchor = (anchor.get("path") == path
                                and anchor.get("file_id") == [before.st_dev, before.st_ino]
                                and anchor.get("session_id") == native)
            if not valid_anchor:
                return {"ok": False, "reason": "rollout_identity_changed"}
            source.seek(max(0, before.st_size - 65536))
            tail = source.read(65537)
            after = os.fstat(source.fileno())
            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    or len(tail) > 65536 or not tail.endswith(b"\n")):
                return {"ok": False, "reason": "rollout_not_stable"}
            lines = tail.splitlines()
            final = json.loads(lines[-1])
            payload = final.get("payload") or {}
            error = payload.get("error") or {}
            valid = (final.get("type") == "event_msg" and payload.get("type") == "task_complete"
                     and isinstance(error, dict) and error.get("codex_error_info") == "server_overloaded"
                     and payload.get("turn_id") == expected.get("error_turn_id"))
        if process(pid) != record:
            return {"ok": False, "reason": "provider_identity_changed"}
        return {"ok": bool(valid), "reason": "current_capacity" if valid else "newer_native_record",
                "native_session_id": native, "turn_id": payload.get("turn_id") if valid else None}
    except (OSError, ValueError, TypeError, KeyError, IndexError, subprocess.SubprocessError):
        return {"ok": False, "reason": "native_proof_unavailable"}


def program():
    return "import json,sys\n" + inspect.getsource(probe) + "\nprint(json.dumps(probe(json.loads(sys.argv[1]))))\n"
