"""Bounded native-source observation over the existing Hosts.run_command seam.

Only identity, control watermarks and input digests leave the executing host.
The transported normalizers are the coordinator's unchanged pure source; the
peer need not import modules from an unrelated installed checkout.
"""
from __future__ import annotations

import inspect
from pathlib import Path

from claude_jsonl_norm import normalize_claude_jsonl_records
from codex_rollout_norm import normalize_codex_rollout_records
from store import _send_wire_digest


def probe(expected):
    import hashlib
    import json
    import os
    import shlex
    import subprocess

    def command(*args):
        result = subprocess.run(args, capture_output=True, text=True, timeout=8)
        if result.returncode not in (0, 1):
            raise ValueError("process_observation_failed")
        return result.stdout

    def process(pid):
        parts = command("ps", "-ww", "-p", str(pid), "-o",
                        "pid=,uid=,lstart=,command=").strip().split(None, 7)
        return parts if len(parts) == 8 and parts[0] == str(pid) else None

    try:
        row = expected
        account, binding = row["account_binding"], row["observer_binding"]
        pid, provider = str(row["pane_pid"]), row["provider"]
        if (not pid.isdecimal() or int(pid) <= 0 or row["status"] != "open"
                or binding["generation"] != row["session_generation"]
                or str(binding["pane_pid"]) != pid
                or account["host"] != row["host"] or account["provider"] != provider
                or binding["executable"] != account["executable"]):
            return {"ok": False, "reason": "binding_changed"}
        record = process(pid)
        if (not record or int(record[1]) != os.getuid()
                or " ".join(record[2:7]).split() != binding["pane_started_at"].split()
                or shlex.split(record[7])[0] != binding["executable"]):
            return {"ok": False, "reason": "provider_identity_changed"}
        # Qualified remote cells are native executables. The unqualified npm
        # launcher cell is deliberately absent from the eligibility table.
        executable = account["executable"]
        live_executable = f"/proc/{pid}/exe" if os.path.exists(f"/proc/{pid}/exe") else executable
        with open(live_executable, "rb") as source:
            executable_info = os.fstat(source.fileno())
            hasher = hashlib.sha256()
            for chunk in iter(lambda: source.read(1 << 20), b""):
                hasher.update(chunk)
            digest = hasher.hexdigest()
        if digest != account["executable_sha256"]:
            return {"ok": False, "reason": "provider_executable_changed"}
        result = {"ok": True, "pane_identity": [pid, binding["pane_started_at"]],
                  "native_executable_sha256": digest,
                  "native_executable_file_id": [executable_info.st_dev, executable_info.st_ino],
                  "uid": os.getuid()}
        anchor = binding.get("transcript") or {}
        if provider == "claude":
            from pathlib import Path
            native = row["claude_session_id"]
            if not isinstance(native, str) or not native or "/" in native:
                return {"ok": False, "reason": "native_session_unproven"}
            root = Path(account["transcript_root"]).resolve()
            paths = [p for p in root.glob(f"*/{native}.jsonl")
                     if not p.is_symlink() and p.resolve().is_relative_to(root)]
        elif provider == "codex":
            native = anchor.get("native_session_id") or anchor.get("session_id")
            parents = [line.split() for line in command("ps", "-eo", "pid=,ppid=").splitlines()]
            pids = {pid}
            for _ in range(32):
                grown = pids | {v[0] for v in parents if len(v) == 2 and v[1] in pids}
                if len(grown) > 256:
                    return {"ok": False, "reason": "ambiguous_process_tree"}
                if grown == pids:
                    break
                pids = grown
            else:
                return {"ok": False, "reason": "ambiguous_process_tree"}
            files = command("lsof", "-n", "-p", ",".join(sorted(pids)), "-Fpfan")
            paths, access = set(), None
            from pathlib import Path
            root = Path(account["transcript_root"]).resolve()
            for line in files.splitlines():
                if line.startswith("f"):
                    access = None
                elif line.startswith("a"):
                    access = line[1:]
                elif (line.startswith("n") and access in {"w", "u"}
                      and line.endswith(".jsonl")):
                    path = Path(line[1:])
                    if not path.is_symlink() and path.resolve().is_relative_to(root):
                        paths.add(path)
            paths = list(paths)
        else:
            return {"ok": False, "reason": "provider_unknown"}
        if len(paths) != 1 or not native:
            return {"ok": False, "reason": "native_source_ambiguous"}
        path = os.path.abspath(paths[0])
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            identity = [info.st_dev, info.st_ino]
            file_digest = hashlib.sha256(
                f"{path}\0{info.st_dev}\0{info.st_ino}".encode("utf-8", "surrogateescape")).hexdigest()
            if anchor.get("source_file_identity_digest"):
                matches = file_digest == anchor["source_file_identity_digest"]
            elif anchor:
                matches = anchor.get("path") == path and anchor.get("file_id") == identity
            else:
                matches = provider == "claude"
            if not matches:
                return {"ok": False, "reason": "native_file_changed"}
            head = source.readline(1 << 20)
            if not head.endswith(b"\n"):
                return {"ok": False, "reason": "native_header_unbounded"}
            header = json.loads(head)
            if provider == "codex" and (header.get("type") != "session_meta"
                    or (header.get("payload") or {}).get("id") != native):
                return {"ok": False, "reason": "native_session_changed"}
            end = info.st_size
            start = max(0, end - (1 << 20))
            source.seek(start)
            if start:
                source.readline()
            data = source.read(end - source.tell())
            if not data.endswith(b"\n"):
                return {"ok": False, "reason": "native_prefix_incomplete"}
            records = [json.loads(line) for line in data.splitlines() if line.strip()]
            after = os.fstat(source.fileno())
            if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)):
                return {"ok": False, "reason": "native_prefix_changed"}
        if provider == "claude":
            if any(r.get("sessionId") not in (None, native) for r in records):
                return {"ok": False, "reason": "native_session_changed"}
            events = normalize_claude_jsonl_records(records, host=row["host"], session_name=row["session_name"])
        else:
            ordinals = [r.get("ordinal") for r in records]
            if (not ordinals or any(type(n) is not int or n < 0 for n in ordinals)
                    or any(a >= b for a, b in zip(ordinals, ordinals[1:]))):
                return {"ok": False, "reason": "native_ordinal_unproven"}
            controls = [r["payload"] for r in records if r.get("type") == "event_msg"
                        and (r.get("payload") or {}).get("type") in {"task_started", "task_complete", "turn_aborted"}]
            if not controls:
                return {"ok": False, "reason": "native_turn_unproven"}
            last = controls[-1]
            result.update(watermark=ordinals[-1],
                          active_turn_id=last.get("turn_id") if last["type"] == "task_started" else None)
            events = normalize_codex_rollout_records(records, host=row["host"],
                        session_name=row["session_name"], session_id=native)

        def incomplete(value):
            if isinstance(value, dict):
                retained = value.get("retained_source")
                return ((isinstance(retained, dict) and retained.get("complete") is False)
                        or any(incomplete(v) for v in value.values()))
            return isinstance(value, list) and any(incomplete(v) for v in value)

        # An incomplete record is always a possible input, never an exclusion.
        candidates = [{"wire_digest": _send_wire_digest(e["text"]), "timestamp": e.get("timestamp")}
                      for e in events if e["kind"] in {"USER", "TELL"}]
        candidates.extend({"wire_digest": None, "timestamp": r.get("timestamp")}
                          for r in records if incomplete(r))
        result.update(input_candidates=candidates, source_session_id=native, file_id=identity,
                      source_file_identity_digest=file_digest)
        current_file, current_executable = os.stat(path), os.stat(live_executable)
        if ((current_file.st_dev, current_file.st_ino, current_file.st_size, current_file.st_mtime_ns)
                != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)):
            return {"ok": False, "reason": "native_prefix_changed"}
        if (process(pid) != record
                or (current_executable.st_dev, current_executable.st_ino,
                    current_executable.st_size, current_executable.st_mtime_ns)
                != (executable_info.st_dev, executable_info.st_ino,
                    executable_info.st_size, executable_info.st_mtime_ns)):
            return {"ok": False, "reason": "provider_identity_changed"}
        if len(json.dumps(result)) > 262144:
            return {"ok": False, "reason": "native_observation_unbounded"}
        return result
    except (OSError, ValueError, TypeError, KeyError, IndexError, subprocess.SubprocessError):
        return {"ok": False, "reason": "native_proof_unavailable"}


def program(provider):
    """Same self-contained Python-command transport as codex_capacity_probe."""
    from store import _send_wire_digest, _RECEIPT_ANSI_RE
    modules = ["provider_wrappers", "claude_jsonl_norm"]
    if provider == "codex":
        modules.append("codex_rollout_norm")
    source = "import json,sys,types,hashlib,re\n"
    for name in modules:
        code = Path(__file__).with_name(name + ".py").read_text()
        source += f"m=types.ModuleType({name!r});sys.modules[{name!r}]=m;exec({code!r},m.__dict__)\n"
    source += "from claude_jsonl_norm import normalize_claude_jsonl_records\n"
    if provider == "codex":
        source += "from codex_rollout_norm import normalize_codex_rollout_records\n"
    source += f"_RECEIPT_ANSI_RE=re.compile({_RECEIPT_ANSI_RE.pattern!r}, {_RECEIPT_ANSI_RE.flags})\n"
    source += inspect.getsource(_send_wire_digest) + "\n" + inspect.getsource(probe)
    return source + "\nprint(json.dumps(probe(json.loads(sys.argv[1]))))\n"
