"""Command line: freeze, eligibility, run, grade, report."""
import argparse
import hashlib
import json
import os
import sys

from . import evaluate, grader, runner, scoring


def cmd_freeze(args):
    print(evaluate.file_sha256(args.tasks))


def cmd_eligibility(args):
    """RED at the pre-fix commit and GREEN at the fix commit for a fix task."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="elig-") as parent:
        results = {}
        for name, sha in (("red", args.pre_fix), ("green", args.fix)):
            wt = runner.new_worktree(args.repo, sha, parent)
            try:
                evaluate.setup_worktree(wt, args.setup_cmd)
                evaluate.restore_files(args.repo, args.fix, args.test_files, wt)
                results[name] = evaluate.run_check(args.cmd, wt)
            finally:
                runner.remove_worktree(args.repo, wt)
    ok, reason = scoring.eligibility(results["red"], results["green"], args.red_pattern)
    receipt = {"eligible": ok, "reason": reason, "pre_fix": args.pre_fix, "fix": args.fix, "cmd": args.cmd}
    for name, (code, out) in results.items():
        receipt[name] = {"exit": code, "sha256": hashlib.sha256(out.encode()).hexdigest(),
                         "tail": out[-1500:]}
        if args.out:
            os.makedirs(args.out, exist_ok=True)
            with open(os.path.join(args.out, f"{name}.txt"), "w", encoding="utf-8") as fh:
                fh.write(out)
    print(scoring.dumps(receipt))
    return 0 if ok else 1


def cmd_run(args):
    doc = evaluate.load_tasks(args.tasks)
    done = {(r["task_id"], r["model"]) for r in evaluate.load_rows(args.bundle)}
    tasks = [t for t in doc["tasks"] if not args.task or t["id"] in args.task]
    for task, model in evaluate.run_order(tasks):
        if (task["id"], model) in done or (args.model and model != args.model):
            continue
        row = evaluate.run_one(task, model, args.bundle, doc, args.timeout)
        evaluate.append_row(args.bundle, row)
        print(json.dumps({k: row.get(k) for k in
                          ("task_id", "model", "status", "correct", "protocol", "wall_s",
                           "tokens_in", "tokens_out", "usd_api_equiv", "failure_reason")}),
              flush=True)


def cmd_grade(args):
    doc = evaluate.load_tasks(args.tasks)
    rows = evaluate.load_rows(args.bundle)
    path = os.path.join(args.bundle, "grades.json")
    graded = json.load(open(path)) if os.path.exists(path) else {}
    for task in doc["tasks"]:
        cells = {r["model"]: r for r in rows if r["task_id"] == task["id"]}
        if task["id"] in graded or not cells:
            continue
        order = grader.shuffle(task["id"], list(cells))
        outputs = {m: grader.output_text(r, None) for m, r in cells.items() if r["status"] != "failed"}
        if outputs:
            prompt = grader.build_prompt(task, order, outputs,
                                         doc.get("grader_prompt", grader.DEFAULT_PROMPT))
            text, raw = grader.run_grader(prompt)  # one call per task; an invalid reply raises
            grades = grader.parse_grades(text, order, expected=outputs)
        else:
            text, raw, grades = "", {}, {}
        graded[task["id"]] = {"order": order, "grades": grades, "reply": text}
        with open(path, "w") as fh:
            fh.write(scoring.dumps(graded))
        print(task["id"], {m: g["grade"] for m, g in grades.items()}, flush=True)


def final_rows(bundle):
    rows = evaluate.load_rows(bundle)
    path = os.path.join(bundle, "grades.json")
    graded = json.load(open(path)) if os.path.exists(path) else {}
    for row in rows:
        g = graded.get(row["task_id"], {}).get("grades", {}).get(row["model"])
        if row["status"] == "failed":
            row["grade"] = 0
        elif g:
            row["grade"] = g["grade"]
            row["followed_scope"] = g["followed_scope"]
    return rows


def cmd_report(args):
    rows = final_rows(args.bundle)
    result = scoring.recommend(rows)
    result["totals"] = {f"{c}/{m}": v for (c, m), v in scoring.class_totals(rows).items()}
    print(scoring.dumps(result))


def main(argv=None):
    p = argparse.ArgumentParser(prog="model_eval")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("freeze"); f.add_argument("tasks"); f.set_defaults(fn=cmd_freeze)
    e = sub.add_parser("eligibility")
    for name in ("repo", "pre-fix", "fix", "cmd", "red-pattern"):
        e.add_argument("--" + name, required=True)
    e.add_argument("--setup-cmd"); e.add_argument("--out"); e.add_argument("--test-files", nargs="+", required=True); e.set_defaults(fn=cmd_eligibility)
    r = sub.add_parser("run")
    r.add_argument("--tasks", required=True); r.add_argument("--bundle", required=True)
    r.add_argument("--task", action="append"); r.add_argument("--model", choices=sorted(runner.MODELS))
    r.add_argument("--timeout", type=int, default=runner.DEFAULT_TIMEOUT_S); r.set_defaults(fn=cmd_run)
    g = sub.add_parser("grade"); g.add_argument("--tasks", required=True)
    g.add_argument("--bundle", required=True); g.set_defaults(fn=cmd_grade)
    rp = sub.add_parser("report"); rp.add_argument("--bundle", required=True); rp.set_defaults(fn=cmd_report)
    args = p.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
