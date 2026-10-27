import json
from model_eval import runner, evaluate, grader, scoring
import pytest


def test_three_codex_bindings_usage_and_cells():
    for arm in runner.ARMS:
        spec = runner.MODELS[arm]
        argv = runner.build_argv(arm, "/wt", "/out/last", "/out")
        assert argv[3:5] == ["codex", "exec"]
        assert argv[argv.index("-m") + 1] == spec["model"]
        assert "model_reasoning_effort=" + spec["effort"] in argv
        assert argv[argv.index("-C") + 1] == "/wt" and argv[-1] == "-"
        event = json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 3}})
        assert runner.parse_usage(arm, event)[1:3] == (10, 3)
    cells = [(t["id"], m) for t, m in evaluate.run_order([{"id": str(i)} for i in range(8)])]
    assert len(cells) == len(set(cells)) == 24
    assert {m for _, m in cells} == set(runner.ARMS)


def test_three_output_blind_packet_and_complete_grades():
    order = grader.shuffle("task", runner.ARMS)
    assert set(order) == {"A", "B", "C"} and set(order.values()) == set(runner.ARMS)
    task = {"brief": "Terra via Codex or GPT-6.1-Sol", "class": "boundary", "known": {"diagnosis": "terra-high"}}
    prompt = grader.build_prompt(task, order, {m: grader.scrub(m) for m in runner.ARMS})
    for name in ("terra", "sol", "codex", "gpt"):
        assert name not in prompt.lower()
    reply = {label: {"grade": 2, "followed_scope": True} for label in order}
    assert set(grader.parse_grades(json.dumps(reply), order)) == set(runner.ARMS)
    reply.pop("C")
    with pytest.raises(ValueError):
        grader.parse_grades(json.dumps(reply), order)


def test_boundary_contract_positive_wrong_missing_invalid(tmp_path):
    task = {"known": {"diagnosis": {"root_cause": "a", "causal_chain": ["x", "y"],
            "product_or_harness": "product", "discriminating_control": "z"}}}
    assert evaluate.boundary_correct(task, str(tmp_path))[0] == 0
    p = tmp_path / "diagnosis.json"
    p.write_text("{bad")
    assert evaluate.boundary_correct(task, str(tmp_path))[0] == 0
    p.write_text(json.dumps(task["known"]["diagnosis"]))
    assert evaluate.boundary_correct(task, str(tmp_path))[0] == 1
    for field, bad in [("root_cause", "b"), ("causal_chain", ["y", "x"]),
                       ("product_or_harness", "harness"), ("discriminating_control", "wrong")]:
        p.write_text(json.dumps({**task["known"]["diagnosis"], field: bad}))
        assert evaluate.boundary_correct(task, str(tmp_path))[0] == 0


def row(cls, arm, i, correct, protocol, grade, status="finished"):
    return {"class": cls, "model": arm, "task_id": str(i), "status": status,
            "correct": correct, "protocol": protocol, "grade": grade}


def test_frozen_selection_high_then_xhigh_and_failed_rows():
    rows = [row(cls, arm, i, 1, "yes", 2) for cls in ("fix", "qa", "boundary")
            for arm in runner.ARMS for i in range(2)]
    assert all(x["selected"] == "terra-high" for x in scoring.terra_recommend(rows).values())
    rows[0].update(correct=0, protocol="no", grade=0, status="failed")
    result = scoring.terra_recommend(rows)
    assert result["fix"]["selected"] == "terra-xhigh"
    assert result["boundary"]["boundary_only"] and result["boundary"]["promotion"] == "none: bench only"


def test_budget_admission_fails_closed():
    from model_eval.trial import admission
    healthy = {"provider": "codex", "status": "ok", "outcome": "ok", "pct": 59, "age_seconds": 300}
    assert admission([healthy])[0]
    for patch in ({"pct": 64}, {"pct": 90}, {"pct": None}, {"age_seconds": 601}, {"status": "stale"}):
        assert not admission([{**healthy, **patch}])[0]
    assert not admission([])[0]
