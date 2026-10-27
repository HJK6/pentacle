"""Bounded Terra trial admission; one concurrent three-arm task batch at a time."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import subprocess
from . import evaluate, runner


def admission(observations, stop_pct=64):
    rows = [r for r in observations if r.get("provider") == "codex"]
    if len(rows) != 1:
        return False, "Codex meter unavailable/ambiguous"
    row = rows[0]
    pct, age = row.get("pct"), row.get("age_seconds")
    if row.get("status") != "ok" or row.get("outcome") != "ok":
        return False, "Codex meter not healthy"
    if not isinstance(pct, (int, float)) or not 0 <= pct <= 100:
        return False, "Codex percentage unavailable"
    if not isinstance(age, (int, float)) or not 0 <= age <= 600:
        return False, "Codex meter older than 10 minutes"
    if pct >= min(stop_pct, 90):
        return False, f"budget stop at {pct} percent"
    return True, f"admit at {pct} percent; age {age}s"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tasks", required=True)
    p.add_argument("--sha256", required=True)
    p.add_argument("--bundle", required=True)
    p.add_argument("--timeout", type=int, default=720)
    args = p.parse_args()
    bundle = Path(args.bundle).resolve()
    bundle.mkdir(parents=True, exist_ok=True)
    doc = evaluate.load_tasks(args.tasks)
    assert len(doc["tasks"]) == 8 and set(doc["arms"]) == set(runner.ARMS)
    done = {(r["task_id"], r["model"]) for r in evaluate.load_rows(str(bundle))}
    for task in doc["tasks"]:
        arms = [m for m in runner.ARMS if (task["id"], m) not in done]
        if not arms:
            continue
        assert hashlib.sha256(Path(args.tasks).read_bytes()).hexdigest() == args.sha256
        probe = subprocess.run(["agent-orch", "usage", "--host", "amaterasu", "--json",
                                "--max-age-seconds", "600"], check=True, capture_output=True, text=True)
        observations = json.loads(probe.stdout)
        (bundle / ("usage-before-" + task["id"] + ".json")).write_text(probe.stdout)
        allowed, reason = admission(observations)
        print(task["id"], reason, flush=True)
        if not allowed:
            raise SystemExit(3)
        with ThreadPoolExecutor(max_workers=3) as pool:
            pending = {pool.submit(evaluate.run_one, task, arm, str(bundle), doc, args.timeout): arm
                       for arm in arms}
            for result in as_completed(pending):
                row = result.result()
                evaluate.append_row(str(bundle), row)
                print(json.dumps({k: row.get(k) for k in
                      ("task_id", "model", "status", "correct", "protocol", "wall_s", "failure_reason")}), flush=True)


if __name__ == "__main__":
    main()
