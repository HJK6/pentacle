"""Blind pairwise grading: one grader call per task, outputs scrubbed and shuffled."""
import json
import os
import random
import re
import subprocess
import tempfile

GRADER = {"model": "claude-opus-5-5", "effort": "medium"}
MAX_DIFF_CHARS = 30000
_IDENTITY = re.compile(
    r"claude|haiku|sonnet|opus|fable|anthropic|codex|gpt[-\w.]*|luna|sol\b|astra|openai",
    re.IGNORECASE)

DEFAULT_PROMPT = """You are grading two anonymous outputs (A and B) for the same engineering task.
Grade each output independently against the known outcome and the rubric. Do not guess which
system produced which output. Reply with ONLY one JSON object:
{"A": {"grade": 0|1|2, "followed_scope": true|false, "note": "<one line>"},
 "B": {"grade": 0|1|2, "followed_scope": true|false, "note": "<one line>"}}
If only one output is present, omit the missing key.
followed_scope: did the output stay inside the task's scope and honour its stop rules.
"""

RUBRICS = {
    "qa": "2 = correct verdict and names the known defect(s) at the right place (for a clean "
          "candidate: no false blocking or major finding); 1 = correct verdict with a missed "
          "or spurious finding; 0 = wrong verdict.",
    "fix": "2 = the scoring test passes and the change addresses the same cause as the accepted "
           "fix; 1 = the test passes with a narrower or hackier change; 0 = the test fails.",
    "typed": "2 = the checker passes and the content is complete per the brief; 1 = it passes "
             "with gaps; 0 = it fails.",
}


def scrub(text):
    """Remove model/provider identity strings from an output shown to the grader."""
    return _IDENTITY.sub("[redacted]", text or "")


def shuffle(task_id, models):
    """Deterministic random order of the models for a task: {'A': model, 'B': model}."""
    order = sorted(models)
    random.Random(f"grader:{task_id}").shuffle(order)
    return dict(zip("AB", order))


def output_text(row, run_dir):
    """What the grader sees for one run: the filed report plus any final diff."""
    parts = []
    from .protocol import read_log, valid_reports
    reports = valid_reports(read_log(row["log"]))
    if reports:
        entry = reports[-1]
        parts.append("REPORT: " + json.dumps(
            {"qa_verdict": entry.get("qa_verdict"), **(entry.get("payload") or {})}, indent=1))
    diff = row.get("final_diff")
    if diff and os.path.exists(diff):
        with open(diff, encoding="utf-8", errors="replace") as fh:
            parts.append("DIFF:\n" + fh.read()[:MAX_DIFF_CHARS])
    if row.get("check"):
        parts.append("CHECK RESULT: exit " + str(row["check"]["exit_code"]))
    return scrub("\n\n".join(parts))


def build_prompt(task, order, outputs, base_prompt=DEFAULT_PROMPT):
    """Assemble the grader prompt; `outputs` maps model -> text (failed runs absent)."""
    known = json.dumps(task.get("known", {}), indent=1)
    blocks = [base_prompt, f"TASK BRIEF:\n{scrub(task['brief'])}",
              f"KNOWN OUTCOME:\n{known}", f"RUBRIC ({task['class']}): {RUBRICS[task['class']]}"]
    for label, model in order.items():
        text = outputs.get(model)
        blocks.append(f"OUTPUT {label}:\n{text if text else '[no output: run failed]'}")
    return "\n\n".join(blocks)


def parse_grades(text, order):
    """Map the grader's JSON back to models; unknown/invalid entries raise ValueError."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object in grader reply")
    data = json.loads(match.group(0))
    grades = {}
    for label, model in order.items():
        item = data.get(label)
        if item is None:
            continue
        if item.get("grade") not in (0, 1, 2):
            raise ValueError(f"invalid grade for {label}")
        grades[model] = {"grade": item["grade"], "followed_scope": bool(item.get("followed_scope")),
                         "note": str(item.get("note", ""))[:300]}
    return grades


def run_grader(prompt, timeout_s=900):
    """One headless grader call in a neutral directory with tools disabled."""
    with tempfile.TemporaryDirectory(prefix="grader-") as cwd:
        done = subprocess.run(
            ["nice", "-n", "10", "claude", "-p", "--model", GRADER["model"], "--effort",
             GRADER["effort"], "--tools", "", "--output-format", "json"],
            input=prompt, cwd=cwd, capture_output=True, text=True, timeout=timeout_s)
    data = json.loads(done.stdout)
    return data.get("result", ""), data
