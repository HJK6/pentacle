import json

from model_eval import fake_agent_orch, protocol

GOOD = {"summary": "done", "findings": [], "next_action": "leader_proceed"}


def run_calls(tmp_path, monkeypatch, calls):
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("EVAL_AGENT_ORCH_LOG", str(log))
    codes = [fake_agent_orch.main(list(c)) for c in calls]
    return protocol.read_log(str(log)), codes


SHA = "a" * 40
QA_FLAGS = ["--target-sha", SHA, "--qa-reviewed-scope", "diff", "--qa-gate-evidence-digest", "b" * 64]


def report_call(payload, verdict=None, extra=()):
    args = ["report", "--msg-id", "1", "--status", "done", "--result", json.dumps(payload)]
    if verdict:
        args += ["--qa-verdict", verdict] + QA_FLAGS
    return args + list(extra)


def test_log_round_trip_and_protocol_yes(tmp_path, monkeypatch, capsys):
    entries, codes = run_calls(tmp_path, monkeypatch, [
        ["title", "eval"], ["tell", "p", "START go"],
        report_call(GOOD, "accept"), ["tell", "p", "END done"]])
    assert codes == [0, 0, 0, 0]
    assert [e["cmd"] for e in entries] == ["title", "tell", "report", "tell"]
    assert entries[2]["valid"] and entries[2]["qa_verdict"] == "accept"
    assert protocol.check_protocol(entries, qa=True) == "yes"
    assert protocol.reported_verdict(entries) == "accept"


def test_protocol_no_without_start_end_or_report(tmp_path, monkeypatch):
    base = [["tell", "p", "START go"], report_call(GOOD), ["tell", "p", "END done"]]
    for drop in range(3):
        calls = [c for i, c in enumerate(base) if i != drop]
        entries, _ = run_calls(tmp_path, monkeypatch, calls)
        (tmp_path / "log.jsonl").unlink()
        assert protocol.check_protocol(entries, qa=False) == "no", drop


def test_qa_protocol_needs_verdict_and_non_qa_does_not(tmp_path, monkeypatch):
    calls = [["tell", "p", "START"], report_call(GOOD), ["tell", "p", "END"]]
    entries, _ = run_calls(tmp_path, monkeypatch, calls)
    assert protocol.check_protocol(entries, qa=True) == "no"
    assert protocol.check_protocol(entries, qa=False) == "yes"


def test_invalid_report_payload_is_rejected_and_not_counted(tmp_path, monkeypatch, capsys):
    bad = dict(GOOD, extra_key=1)
    entries, codes = run_calls(tmp_path, monkeypatch, [
        ["tell", "p", "START"], report_call(bad, "reject"), ["tell", "p", "END"]])
    assert codes[1] == 2 and entries[1]["valid"] is False
    assert protocol.check_protocol(entries, qa=True) == "no"
    assert protocol.reported_verdict(entries) is None


def test_result_file_is_unsupported(tmp_path, monkeypatch):
    entries, codes = run_calls(tmp_path, monkeypatch, [
        ["report", "--msg-id", "1", "--status", "done", "--result-file", "x.json"]])
    assert codes == [2] and not entries[0]["valid"]


def test_finding_without_suggested_fix_is_invalid_and_not_counted(tmp_path, monkeypatch):
    bad = dict(GOOD, findings=[{"severity": "major", "where": "a", "issue": "b"}])
    entries, codes = run_calls(tmp_path, monkeypatch, [
        ["tell", "p", "START"], report_call(bad), ["tell", "p", "END"]])
    assert codes[1] == 2 and protocol.check_protocol(entries, qa=False) == "no"


def test_qa_review_flags_are_required_together_and_checked(tmp_path, monkeypatch):
    partial = ["report", "--status", "done", "--qa-verdict", "accept", "--result", json.dumps(GOOD)]
    bad_digest = report_call(GOOD, "reject")[:-1] + ["NOTHEX"]
    entries, codes = run_calls(tmp_path, monkeypatch, [partial, bad_digest, report_call(GOOD, "reject")])
    assert codes == [2, 2, 0]
    assert protocol.reported_verdict(entries) == "reject"


def test_schema_check_cases():
    assert protocol.validate_report_payload(GOOD) == []
    finding = {"severity": "major", "where": "a.py:1", "issue": "x", "suggested_fix": None}
    assert protocol.validate_report_payload(dict(GOOD, findings=[finding])) == []
    assert protocol.validate_report_payload(dict(GOOD, findings=[dict(finding, severity="huge")]))
    assert protocol.validate_report_payload(dict(GOOD, summary=" "))
    assert protocol.validate_report_payload(dict(GOOD, findings="none"))
    assert protocol.validate_report_payload([])
    assert protocol.validate_report_payload(dict(GOOD, details={"a": 1}, extras={})) == []
    # the payload itself may carry the governance fields the real schema allows
    assert protocol.validate_report_payload(dict(GOOD, qa_verdict="accept", target_sha=SHA)) == []
