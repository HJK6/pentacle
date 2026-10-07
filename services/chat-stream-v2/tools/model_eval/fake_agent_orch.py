"""Stand-in for the orchestration CLI: logs every call and returns OK JSON.

Installed first on PATH for a run so protocol use is observable without
touching a daemon. Set EVAL_AGENT_ORCH_LOG to the JSONL log path.
"""
import json
import os
import sys
import time

try:
    from .protocol import validate_report_payload
except ImportError:  # executed as a script
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from protocol import validate_report_payload


def _flag(argv, name):
    for i, arg in enumerate(argv):
        if arg == name and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


def handle(argv):
    """Return (entry, stdout_json, exit_code) for one call."""
    cmd = argv[0] if argv else ""
    entry = {"t": time.time(), "cmd": cmd, "argv": argv}
    if cmd != "report":
        return entry, {"type": f"{cmd}.ok", "ok": True}, 0
    entry["status"] = _flag(argv, "--status")
    entry["qa_verdict"] = _flag(argv, "--qa-verdict")
    entry["target_sha"] = _flag(argv, "--target-sha")
    flags = {"qa_verdict": entry["qa_verdict"], "target_sha": entry["target_sha"],
             "completion_kind": _flag(argv, "--completion-kind"),
             "qa_reviewed_scope": _flag(argv, "--qa-reviewed-scope"),
             "qa_gate_evidence_digest": _flag(argv, "--qa-gate-evidence-digest")}
    if "--result-file" in argv:
        entry.update(valid=False, errors=["unsupported_in_v2: use --result"])
    else:
        raw = _flag(argv, "--result")
        try:
            payload = json.loads(raw) if raw is not None else None
        except ValueError as exc:
            entry.update(valid=False, errors=[f"invalid JSON: {exc}"])
        else:
            errors = (validate_report_payload(payload, entry["status"] or "", flags)
                      if raw is not None else ["--result is required"])
            entry.update(valid=not errors, errors=errors, payload=payload if not errors else None)
    if entry["valid"]:
        return entry, {"type": "report.ok", "ok": True}, 0
    return entry, {"type": "report.error", "ok": False, "errors": entry["errors"]}, 2


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    entry, out, code = handle(argv)
    path = os.environ.get("EVAL_AGENT_ORCH_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    print(json.dumps(out))
    return code


if __name__ == "__main__":
    sys.exit(main())
