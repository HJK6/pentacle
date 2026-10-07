"""Task setup, scoring and the frozen run table for one evaluation."""
import hashlib
import json
import os
import subprocess

from . import protocol, runner, scoring

CHECK_TIMEOUT_S = 900


def file_sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def load_tasks(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def restore_files(repo_or_worktree, sha, files, dest):
    """Write each file's bytes from commit `sha` into `dest` (overwriting)."""
    for rel in files:
        blob = subprocess.run(["git", "-C", repo_or_worktree, "show", f"{sha}:{rel}"],
                              check=True, capture_output=True).stdout
        target = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(blob)


def run_check(cmd, cwd, timeout_s=CHECK_TIMEOUT_S):
    """Run a scoring/checker command; return (exit_code, combined output)."""
    try:
        done = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout_s, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        return done.returncode, done.stdout + done.stderr
    except subprocess.TimeoutExpired:
        return 124, f"scoring command timed out after {timeout_s}s"


def prepare_worktree(task, parent):
    """Fresh worktree at the task SHA; fix tasks start with the scoring tests installed."""
    path = runner.new_worktree(task["repo"], task["sha"], parent)
    setup_worktree(path, task.get("setup_cmd"))
    if task["class"] == "fix":
        restore_files(task["repo"], task["fix_sha"], task["test_files"], path)
    return path


def setup_worktree(path, setup_cmd):
    """Optional per-task setup (e.g. link an installed dependency tree) run in the worktree."""
    if setup_cmd:
        subprocess.run(setup_cmd, shell=True, cwd=path, check=True, capture_output=True)


def snapshot_tree(worktree):
    """Tree id of the prepared worktree (after setup and installed tests): the diff baseline."""
    subprocess.run(["git", "-C", worktree, "add", "-A"], check=True, capture_output=True)
    return subprocess.run(["git", "-C", worktree, "write-tree"], check=True, capture_output=True,
                          text=True).stdout.strip()


def capture_diff(worktree, run_dir, base_tree):
    """Everything the model changed since `base_tree`, whether left unstaged, staged or committed."""
    subprocess.run(["git", "-C", worktree, "add", "-A"], capture_output=True)
    diff = subprocess.run(["git", "-C", worktree, "diff", "--cached", base_tree],
                          capture_output=True, text=True).stdout
    path = os.path.join(run_dir, "final.diff")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(diff)
    return path


def score_run(task, record, worktree):
    """Turn a raw run record into a scored row (failed rows keep observed usage)."""
    entries = protocol.read_log(record["log"])
    observed = {k: record.get(k) for k in
                ("wall_s", "tokens_in", "tokens_out", "usd_api_equiv", "requested", "effective",
                 "cwd", "argv", "log", "stdout")}
    base = {"task_id": task["id"], "class": task["class"], "model": record["model"], **observed}
    reason = record.get("failure_reason")
    if reason is None and not protocol.valid_reports(entries):
        reason = "no schema-valid report filed"
    if reason is not None:
        return {**scoring.failed_row(task["id"], record["model"], reason), **base,
                "status": "failed", "failure_reason": reason, "correct": 0,
                "protocol": "no", "grade": 0}
    qa = task["class"] == "qa"
    if qa:
        correct = int(protocol.reported_verdict(entries) == task["known"]["verdict"])
        check = None
    elif task["class"] == "fix":
        restore_files(task["repo"], task["fix_sha"], task["test_files"], worktree)
        code, out = run_check(task["score_cmd"], worktree)
        correct, check = int(code == 0), {"exit_code": code, "output_tail": out[-4000:]}
    else:
        code, out = run_check(task["check_cmd"], worktree)
        correct, check = int(code == 0), {"exit_code": code, "output_tail": out[-4000:]}
    return {**base, "status": "finished", "failure_reason": None, "correct": correct,
            "protocol": protocol.check_protocol(entries, qa), "grade": None, "check": check,
            "verdict": protocol.reported_verdict(entries) if qa else None}


def run_one(task, model, bundle, tasks_doc, timeout_s=runner.DEFAULT_TIMEOUT_S):
    """Run one (task, model) cell end to end and return its scored row."""
    run_dir = os.path.join(bundle, "runs", task["id"], model)
    os.makedirs(run_dir, exist_ok=True)
    wt_parent = os.path.join(bundle, "worktrees")
    os.makedirs(wt_parent, exist_ok=True)
    worktree = prepare_worktree(task, wt_parent)
    base_tree = snapshot_tree(worktree)
    try:
        record = runner.run_session(model, task["brief"], worktree, run_dir, timeout_s,
                                    rates=tasks_doc.get("pricing"))
        final_diff = capture_diff(worktree, run_dir, base_tree)  # before scoring touches the tree
        row = score_run(task, record, worktree)
        row["final_diff"] = final_diff
        with open(os.path.join(run_dir, "record.json"), "w", encoding="utf-8") as fh:
            fh.write(scoring.dumps({**record, "row": row}))
    finally:
        runner.remove_worktree(task["repo"], worktree)
    return row


def load_rows(bundle):
    path = os.path.join(bundle, "runs.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def append_row(bundle, row):
    with open(os.path.join(bundle, "runs.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def run_order(tasks, models=("luna", "haiku")):
    """Sequential cells; the model order alternates per task to spread time-of-day bias."""
    cells = []
    for i, task in enumerate(tasks):
        order = models if i % 2 == 0 else tuple(reversed(models))
        cells.extend((task, m) for m in order)
    return cells
