from model_eval import scoring

PAT = r"AssertionError|failed"


def test_fix_eligibility_accepts_product_red_and_green():
    ok, why = scoring.eligibility((1, "E   AssertionError: assert 1 == 2\n1 failed"), (0, "1 passed"), PAT)
    assert ok, why


def test_fix_eligibility_rejects_already_green_baseline():
    ok, why = scoring.eligibility((0, "1 passed"), (0, "1 passed"), PAT)
    assert not ok and "already green" in why


def test_fix_eligibility_rejects_setup_errors():
    for out in ("ModuleNotFoundError: No module named x\n1 failed",
                "ERROR collecting tests - errors during collection\n1 failed",
                "fixture 'db' not found\n1 failed"):
        ok, why = scoring.eligibility((2, out), (0, "ok"), PAT)
        assert not ok and why.startswith("RED"), out


def test_fix_eligibility_rejects_red_that_never_goes_green():
    ok, why = scoring.eligibility((1, "AssertionError"), (1, "AssertionError"), PAT)
    assert not ok and why.startswith("GREEN")


def row(task, cls, model, correct, protocol="yes", grade=2, status="finished", usd=0.1):
    return {"task_id": task, "class": cls, "model": model, "status": status,
            "correct": correct, "protocol": protocol, "grade": grade, "usd_api_equiv": usd,
            "wall_s": 10, "tokens_in": 100, "tokens_out": 10}


def table(spec):
    """spec: {class: (n_tasks, haiku_correct_each, luna_correct_each)}."""
    rows = []
    for cls, (n, h, l) in spec.items():
        for i in range(n):
            rows += [row(f"{cls}{i}", cls, "haiku", h), row(f"{cls}{i}", cls, "luna", l)]
    return rows


def test_recommend_swap_partial_keep_and_under_sampled():
    assert scoring.recommend(table({"qa": (2, 1, 1), "fix": (2, 1, 0), "typed": (2, 1, 1)}))["verdict"] == "swap"
    partial = scoring.recommend(table({"qa": (2, 1, 1), "fix": (2, 0, 1), "typed": (2, 1, 1)}))
    assert partial["verdict"] == "partial swap: qa, typed"
    assert scoring.recommend(table({"qa": (2, 0, 1), "fix": (2, 0, 1), "typed": (2, 0, 1)}))["verdict"] == "keep Luna"
    small = scoring.recommend(table({"qa": (2, 1, 1), "fix": (1, 1, 1), "typed": (2, 1, 1)}))
    assert small["under_sampled"] == ["fix"] and small["verdict"] == "partial swap: qa, typed"


def test_grade_slack_is_one_point_total():
    rows = table({"qa": (2, 1, 1), "fix": (2, 1, 1), "typed": (2, 1, 1)})
    for r in rows:
        if r["model"] == "haiku" and r["task_id"] == "qa0":
            r["grade"] = 1  # luna total 4, haiku total 3: within slack
    assert scoring.recommend(rows)["classes"]["qa"]["ready"]
    for r in rows:
        if r["model"] == "haiku" and r["task_id"] == "qa1":
            r["grade"] = 1  # haiku total 2 < 4 - 1
    assert not scoring.recommend(rows)["classes"]["qa"]["ready"]


def test_failed_row_changes_totals_and_outcome():
    rows = table({"qa": (2, 1, 1), "fix": (2, 1, 1), "typed": (2, 1, 1)})
    assert scoring.recommend(rows)["verdict"] == "swap"
    for cls in ("qa", "fix", "typed"):
        victim = next(r for r in rows if r["class"] == cls and r["model"] == "haiku")
        victim.update(scoring.failed_row(victim["task_id"], "haiku", "timeout", wall_s=1800.0))
        victim["class"] = cls
        victim["usd_api_equiv"] = scoring.UNAVAILABLE
    result = scoring.recommend(rows)
    qa = result["classes"]["qa"]
    assert qa["challenger"]["runs"] == 2 and qa["challenger"]["finished"] == 1
    assert qa["under_sampled"] and not qa["ready"] and result["verdict"] == "keep Luna"
    assert qa["challenger"]["correct"] == 1 and qa["challenger"]["usd_unavailable"] == 1
    assert qa["challenger"]["wall_s"] >= 1810


def test_unavailable_cost_stays_unavailable():
    assert scoring.luna_cost(None, {}) == scoring.UNAVAILABLE
    rates = {"uncached_input": 1.25, "cache_read": 0.125, "output": 10.0}
    usage = {"input_tokens": 1_000_000, "cached_input_tokens": 800_000, "output_tokens": 100_000}
    assert scoring.luna_cost(usage, rates) == round(0.2 * 1.25 + 0.8 * 0.125 + 1.0, 6)
    assert scoring.failed_row("t", "luna", "crash")["usd_api_equiv"] == scoring.UNAVAILABLE
