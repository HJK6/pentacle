"""Per-run scoring helpers, cost estimates and the fixed recommendation rule."""
import json
import re

CLASSES = ("qa", "fix", "typed")
UNAVAILABLE = "unavailable"

# The fix-task RED control rejects dependency/import/collection/setup failures.
SETUP_ERROR = re.compile(
    r"ModuleNotFoundError|ImportError|errors? during collection|SyntaxError|"
    r"fixture '.*' not found|command not found|No such file or directory|"
    r"no tests ran|collected 0 items",
    re.IGNORECASE)


def classify_red(exit_code, output, red_pattern):
    """Return (eligible, reason) for the scoring command run at the pre-fix commit."""
    if exit_code == 0:
        return False, "already green at the pre-fix commit"
    if SETUP_ERROR.search(output):
        return False, "setup/collection/import error, not a product or assertion failure"
    if not re.search(red_pattern, output):
        return False, "output does not match the expected assertion failure"
    return True, "product/assertion failure"


def eligibility(red, green, red_pattern):
    """`red`/`green` are (exit_code, output) pairs from the pre-fix and fix commits."""
    ok, reason = classify_red(red[0], red[1], red_pattern)
    if not ok:
        return False, f"RED: {reason}"
    if green[0] != 0:
        return False, "GREEN: scoring command fails at the fix commit"
    return True, "RED at pre-fix, GREEN at fix"


def luna_cost(usage, rates):
    """API-equivalent USD from codex usage and a pricing class (USD per MTok)."""
    if not usage:
        return UNAVAILABLE
    cached = usage.get("cached_input_tokens", 0)
    fresh = max(usage.get("input_tokens", 0) - cached, 0)
    total = (fresh * rates["uncached_input"] + cached * rates["cache_read"]
             + usage.get("output_tokens", 0) * rates["output"])
    return round(total / 1e6, 6)


def failed_row(task_id, model, reason, **observed):
    """A failed run scores zero everywhere and stays in every denominator."""
    row = {"task_id": task_id, "model": model, "status": "failed",
           "failure_reason": reason, "correct": 0, "protocol": "no", "grade": 0}
    row.update(observed)
    row.setdefault("usd_api_equiv", UNAVAILABLE)
    return row


def class_totals(rows):
    """Per (class, model) totals over the frozen run table, failed rows included."""
    out = {}
    for row in rows:
        cell = out.setdefault((row["class"], row["model"]),
                              {"runs": 0, "finished": 0, "correct": 0, "protocol_yes": 0,
                               "grade": 0, "wall_s": 0.0, "tokens_in": 0, "tokens_out": 0,
                               "usd": 0.0, "usd_unavailable": 0})
        cell["runs"] += 1
        cell["finished"] += row["status"] != "failed"
        cell["correct"] += row["correct"]
        cell["protocol_yes"] += row["protocol"] == "yes"
        cell["grade"] += row["grade"]
        cell["wall_s"] += row.get("wall_s") or 0
        cell["tokens_in"] += row.get("tokens_in") or 0
        cell["tokens_out"] += row.get("tokens_out") or 0
        usd = row.get("usd_api_equiv", UNAVAILABLE)
        if usd == UNAVAILABLE:
            cell["usd_unavailable"] += 1
        else:
            cell["usd"] += usd
    return out


def recommend(rows, challenger="haiku", incumbent="luna"):
    """Apply the fixed rule: per-class Haiku-ready test, then swap / partial / keep."""
    totals = class_totals(rows)
    classes = {}
    for cls in CLASSES:
        h, l = totals.get((cls, challenger)), totals.get((cls, incumbent))
        tasks = {r["task_id"] for r in rows if r["class"] == cls}
        if not h or not l:
            classes[cls] = {"ready": False, "under_sampled": True, "reason": "no runs"}
            continue
        under = len(tasks) < 2 or h["finished"] < 2 or l["finished"] < 2
        ready = (not under and h["correct"] >= l["correct"]
                 and h["protocol_yes"] >= l["protocol_yes"]
                 and h["grade"] >= l["grade"] - 1)
        classes[cls] = {"ready": ready, "under_sampled": under,
                        "challenger": h, "incumbent": l}
    ready = [c for c in CLASSES if classes[c]["ready"]]
    if len(ready) == len(CLASSES):
        verdict = "swap"
    elif ready:
        verdict = "partial swap: " + ", ".join(ready)
    else:
        verdict = "keep Luna"
    return {"verdict": verdict, "classes": classes,
            "under_sampled": [c for c in CLASSES if classes[c]["under_sampled"]]}


def dumps(obj):
    return json.dumps(obj, indent=2, sort_keys=True, default=str)
