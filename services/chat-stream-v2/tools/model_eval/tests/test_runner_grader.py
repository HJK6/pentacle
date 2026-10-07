import json
import os
import subprocess
import sys

import pytest

from model_eval import evaluate, grader, protocol, runner, scoring


def test_argv_binds_model_effort_and_cwd_explicitly():
    luna = runner.build_argv("luna", "/wt", "/out/last.txt", "/run")
    assert luna[:3] == ["nice", "-n", "10"] and luna[3:5] == ["codex", "exec"]
    assert ["-m", "gpt-6-luna"] == luna[5:7]
    assert "model_reasoning_effort=max" in luna and ["-C", "/wt"] == luna[luna.index("-C"):luna.index("-C") + 2]
    assert "allow_login_shell=false" in luna and "--json" in luna
    haiku = runner.build_argv("haiku", "/wt", "/out/last.txt", "/run")
    assert haiku[3:5] == ["claude", "-p"]
    assert haiku[haiku.index("--model") + 1] == "claude-haiku-5-5"
    assert haiku[haiku.index("--effort") + 1] == "high"
    assert haiku[haiku.index("--permission-mode") + 1] == "acceptEdits"
    assert haiku.count("--add-dir") == 2 and "/wt" in haiku


def test_run_env_puts_shim_first_and_strips_orchestration_vars(tmp_path):
    env = runner.run_env("/shim", "/log", {"PATH": "/usr/bin", "AGENT_ORCH_TOKEN": "x", "TMUX": "1", "HOME": "/h"})
    assert env["PATH"].startswith("/shim:/usr/bin") and "TMUX" not in env
    assert env["EVAL_AGENT_ORCH_LOG"] == "/log" and "AGENT_ORCH_TOKEN" not in env
    assert env["AGENT_ORCH_STREAM_ID"] == "eval:run" and env["HOME"] == "/h"


def test_parse_usage_for_both_providers():
    haiku = "\n".join([json.dumps({"type": "assistant"}), "not json", json.dumps({
        "type": "result", "total_cost_usd": 0.5,
        "usage": {"input_tokens": 10, "cache_creation_input_tokens": 20, "cache_read_input_tokens": 30, "output_tokens": 7},
        "modelUsage": {"claude-haiku-5-5": {}}})])
    assert runner.parse_usage("haiku", haiku)[:4] == ("claude-haiku-5-5", 60, 7, 0.5)
    luna = json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 5}})
    assert runner.parse_usage("luna", luna)[1:4] == (100, 5, scoring.UNAVAILABLE)
    assert runner.parse_usage("haiku", "")[1:4] == (None, None, scoring.UNAVAILABLE)


def test_grader_scrubs_identity_and_shuffles_deterministically():
    text = "Haiku (claude-haiku-5-5) and gpt-6-luna via Codex, Anthropic, OpenAI"
    clean = grader.scrub(text)
    for word in ("haiku", "claude", "luna", "gpt", "codex", "anthropic", "openai"):
        assert word not in clean.lower()
    orders = {tuple(grader.shuffle(f"task-{i}", ["luna", "haiku"]).values()) for i in range(40)}
    assert orders == {("luna", "haiku"), ("haiku", "luna")}
    assert grader.shuffle("t1", ["luna", "haiku"]) == grader.shuffle("t1", ["haiku", "luna"])


def test_grader_prompt_hides_model_names_and_marks_failed_output():
    task = {"id": "t", "class": "qa", "brief": "Review. Reviewer is Luna.", "known": {"verdict": "reject"}}
    order = {"A": "haiku", "B": "luna"}
    prompt = grader.build_prompt(task, order, {"haiku": grader.scrub("claude says reject")})
    assert "haiku" not in prompt.lower() and "luna" not in prompt.lower()
    assert "OUTPUT B:\n[no output: run failed]" in prompt


def test_parse_grades_maps_labels_back_to_models():
    order = {"A": "haiku", "B": "luna"}
    reply = 'ok {"A": {"grade": 2, "followed_scope": true, "note": "x"}, "B": {"grade": 1, "followed_scope": false}}'
    grades = grader.parse_grades(reply, order)
    assert grades["haiku"]["grade"] == 2 and grades["luna"]["grade"] == 1 and not grades["luna"]["followed_scope"]
    with pytest.raises(ValueError):
        grader.parse_grades('{"A": {"grade": 3}}', order)
    with pytest.raises(ValueError):
        grader.parse_grades("no json", order)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def fix_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.test")
    git(repo, "config", "user.name", "t")
    (repo / "mod.py").write_text("def f():\n    return 1\n")
    (repo / "test_mod.py").write_text("from mod import f\n\ndef test_f():\n    assert f() == 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    (repo / "mod.py").write_text("def f():\n    return 2\n")
    (repo / "test_mod.py").write_text("from mod import f\n\ndef test_f():\n    assert f() == 2\n")
    git(repo, "commit", "-qam", "fix")
    return repo, git(repo, "rev-parse", "HEAD^"), git(repo, "rev-parse", "HEAD")


def fix_task(repo, pre, fix):
    return {"id": "fix-1", "class": "fix", "repo": str(repo), "sha": pre, "fix_sha": fix,
            "test_files": ["test_mod.py"], "score_cmd": f"{sys.executable} -m pytest -q -p no:cacheprovider test_mod.py",
            "brief": "fix it"}


def test_test_files_restored_before_scoring_and_worktree_cleanup(fix_repo, tmp_path):
    repo, pre, fix = fix_repo
    task = fix_task(repo, pre, fix)
    wt = evaluate.prepare_worktree(task, str(tmp_path))
    try:
        assert "== 2" in (open(os.path.join(wt, "test_mod.py")).read())  # tests from F installed at F^
        (open(os.path.join(wt, "test_mod.py"), "w")).write("def test_f():\n    assert True\n")  # model tampers
        (open(os.path.join(wt, "mod.py"), "w")).write("def f():\n    return 2\n")  # model fixes source
        log = tmp_path / "log.jsonl"
        log.write_text("\n".join(json.dumps(e) for e in [
            {"cmd": "tell", "argv": ["tell", "p", "START"]},
            {"cmd": "report", "valid": True, "status": "done", "qa_verdict": None, "payload": {}},
            {"cmd": "tell", "argv": ["tell", "p", "END"]}]))
        record = {"model": "haiku", "log": str(log), "failure_reason": None, "wall_s": 1.0,
                  "tokens_in": 1, "tokens_out": 1, "usd_api_equiv": 0.01}
        scored = evaluate.score_run(task, record, wt)
        assert scored["correct"] == 1 and scored["protocol"] == "yes" and scored["status"] == "finished"
        # source left unfixed -> the restored test fails
        open(os.path.join(wt, "mod.py"), "w").write("def f():\n    return 1\n")
        assert evaluate.score_run(task, record, wt)["correct"] == 0
    finally:
        runner.remove_worktree(str(repo), wt)
    assert not os.path.exists(wt)


def test_failed_runs_score_zero_and_keep_observed_usage(fix_repo, tmp_path):
    repo, pre, fix = fix_repo
    task = fix_task(repo, pre, fix)
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    record = {"model": "luna", "log": str(empty), "failure_reason": None, "wall_s": 12.0,
              "tokens_in": 500, "tokens_out": 50, "usd_api_equiv": scoring.UNAVAILABLE}
    no_report = evaluate.score_run(task, record, str(tmp_path))
    assert no_report["status"] == "failed" and "no schema-valid report" in no_report["failure_reason"]
    timeout = evaluate.score_run(task, dict(record, failure_reason="timeout after 1800s"), str(tmp_path))
    for row in (no_report, timeout):
        assert (row["correct"], row["protocol"], row["grade"]) == (0, "no", 0)
        assert row["tokens_in"] == 500 and row["usd_api_equiv"] == scoring.UNAVAILABLE
        assert row["class"] == "fix" and row["task_id"] == "fix-1"


def test_qa_correct_compares_reported_verdict(tmp_path):
    task = {"id": "qa-1", "class": "qa", "known": {"verdict": "reject"}}
    log = tmp_path / "log.jsonl"
    log.write_text("\n".join(json.dumps(e) for e in [
        {"cmd": "tell", "argv": ["tell", "p", "START"]},
        {"cmd": "report", "valid": True, "status": "done", "qa_verdict": "reject", "payload": {}},
        {"cmd": "tell", "argv": ["tell", "p", "END"]}]))
    record = {"model": "luna", "log": str(log), "failure_reason": None}
    assert evaluate.score_run(task, record, str(tmp_path))["correct"] == 1
    task["known"]["verdict"] = "accept"
    assert evaluate.score_run(task, record, str(tmp_path))["correct"] == 0


def test_run_order_alternates_models_per_task():
    tasks = [{"id": "a"}, {"id": "b"}]
    assert [(t["id"], m) for t, m in evaluate.run_order(tasks)] == [
        ("a", "luna"), ("a", "haiku"), ("b", "haiku"), ("b", "luna")]
