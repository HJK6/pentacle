"""Synthetic gh evidence only: no git, GitHub, tag or main mutation occurs."""
import json
import subprocess
import pytest
from test_merge_gate import merge_gate, CANDIDATE, OLD, TAG_OBJECT, _fake_evidence, _existing_tag_case, _result, CHECKOUT_LOG

pytestmark = pytest.mark.timeout(10)
REPO = "HJK6/pentacle"
TAG = "v2-gate/" + CANDIDATE

class Clock:
    def __init__(self): self.now = 0
    def __call__(self): return self.now
    def sleep(self, seconds): self.now += seconds

def run_row(id=200, number=2, attempt=1, status="completed", conclusion="success", **extra):
    return {"id":id,"run_number":number,"run_attempt":attempt,"workflow_id":7,
        "head_sha":CANDIDATE,"head_branch":TAG,"event":"push",
        "repository":{"full_name":REPO},"head_repository":{"full_name":REPO},
        "path":".github/workflows/predeploy-tests.yml",
        "html_url":f"https://github.com/{REPO}/actions/runs/{id}",
        "status":status,"conclusion":conclusion,**extra}

def jobs(run_id=200, attempt=1):
    return {"total_count":1,"jobs":[{"name":"checks","head_sha":CANDIDATE,"id":run_id*10,"run_id":run_id,"run_attempt":attempt,
        "status":"completed","conclusion":"success",
        "steps":[{"name":"Set up job","number":1,"status":"completed","conclusion":"success"},{"name":"Run actions/checkout@v4","number":2,"status":"completed","conclusion":"success"}]+[{"name":name,"status":"completed","conclusion":"success"} for name in merge_gate.PUBLIC_REQUIRED_STEPS]}]}

def scripted(monkeypatch, snapshots, *, job_payload=None, late=False, timeout=False, log=CHECKOUT_LOG):
    clock=Clock();calls=[];index=[0]
    def command(args, *, input_text=None, timeout=None):
        assert args[:2]==["gh","api"], args
        assert timeout is not None and 0 < timeout <= 5
        calls.append((args,timeout))
        if late: clock.now += 6
        if scripted_timeout: raise subprocess.TimeoutExpired(args,timeout)
        if args[2].endswith("/logs"): return _result(args,log)
        if "/workflows/" in args[2]:
            value=snapshots[min(index[0],len(snapshots)-1)];index[0]+=1
            payload=value if isinstance(value,dict) else {"total_count":len(value),"workflow_runs":value}
        else:
            parts=args[2].split("/")
            payload=job_payload if job_payload is not None else jobs(int(parts[5]),int(parts[7]))
        return _result(args,json.dumps(payload))
    scripted_timeout=timeout
    monkeypatch.setattr(merge_gate,"_command",command)
    return clock,calls

def wait(clock):
    return merge_gate.wait_for_promotion_checks(CANDIDATE,TAG,REPO,7,timeout_s=5,poll_s=1,clock=clock,sleep=clock.sleep)

def test_superseded_cancelled_then_new_green_ignores_unrelated_green(monkeypatch):
    old=run_row(199,1,conclusion="cancelled")
    unrelated=run_row(999,99,head_sha=OLD,head_branch="other")
    current=run_row(status="in_progress",conclusion=None)
    clock,calls=scripted(monkeypatch,[[old,current,unrelated],[old,run_row(),unrelated],[old,run_row(),unrelated]])
    result=wait(clock)
    assert result["run_id"]==200 and result["status"]=="green"
    assert clock.now==1 and len(calls)==5

@pytest.mark.parametrize("rows,reason",[
    ([run_row(conclusion="failure")],"final_red"),
    ([run_row(199,1,conclusion="cancelled"),run_row(conclusion="failure")],"final_red"),
    ([],"required_checks_missing"),
    ([run_row(status="queued",conclusion=None)],"still_running"),
    ([run_row(999,99,head_sha=OLD,head_branch="other")],"required_checks_missing"),
    ([run_row(199,1),run_row(status="in_progress",conclusion=None)],"still_running"),
])
def test_non_green_never_succeeds(monkeypatch,rows,reason):
    clock,calls=scripted(monkeypatch,[rows])
    with pytest.raises(merge_gate.GateError,match=reason):wait(clock)
    assert clock.now <= 5
    assert all(args[:2]==["gh","api"] for args,_ in calls)

def test_new_attempt_displaces_old_green_before_acceptance(monkeypatch):
    old=run_row(attempt=1);new=run_row(attempt=2,status="in_progress",conclusion=None)
    clock,calls=scripted(monkeypatch,[[old],[new],[new],[run_row(attempt=2)],[run_row(attempt=2)]])
    result=wait(clock)
    assert result["run_attempt"]==2
    assert any("/attempts/2/jobs" in args[2] for args,_ in calls)

@pytest.mark.parametrize("mode",["late","timeout"])
def test_subprocess_and_late_green_are_bounded(monkeypatch,mode):
    clock,_=scripted(monkeypatch,[[run_row()]],late=mode=="late",timeout=mode=="timeout")
    with pytest.raises(merge_gate.GateError,match="timeout"):wait(clock)

@pytest.mark.parametrize("payload",[
    {"total_count":2,"workflow_runs":[run_row()]},
    {"total_count":2,"workflow_runs":[run_row(),run_row()]},
    {"total_count":1,"workflow_runs":[run_row(repository={"full_name":"other/repo"})]},
    {"total_count":1,"workflow_runs":[run_row(path=".github/workflows/other.yml")]},
])
def test_incomplete_or_wrong_identity_fails_closed(monkeypatch,payload):
    clock,_=scripted(monkeypatch,[payload])
    with pytest.raises(merge_gate.GateError):wait(clock)

@pytest.mark.parametrize("mutation,reason",[("missing","required_checks_missing"),("failed","final_red"),("wrong_sha","candidate mismatch")])
def test_required_job_evidence_cannot_be_omitted_or_substituted(monkeypatch,mutation,reason):
    payload=jobs()
    if mutation=="missing":payload["jobs"][0]["steps"].pop()
    elif mutation=="failed":payload["jobs"][0]["steps"][-1]["conclusion"]="failure"
    else:payload["jobs"][0]["head_sha"]=OLD
    clock,_=scripted(monkeypatch,[[run_row()]],job_payload=payload)
    with pytest.raises(merge_gate.GateError,match=reason):wait(clock)

def test_remote_tag_mutation_after_green_prevents_main_push(monkeypatch):
    ref="refs/tags/"+TAG
    calls=_existing_tag_case(monkeypatch,f"{TAG_OBJECT}\t{ref}\n{CANDIDATE}\t{ref}^{{}}\n")
    reads=iter([(TAG_OBJECT,CANDIDATE),(OLD,CANDIDATE)])
    monkeypatch.setattr(merge_gate,"_remote_tag_identity",lambda *args,**kwargs:next(reads))
    with pytest.raises(merge_gate.GateError,match="tag changed"):merge_gate.promote(CANDIDATE,123)
    assert not any(args[:2]==["git","push"] for args in calls)

def test_command_passes_remaining_timeout_to_subprocess(monkeypatch):
    seen={}
    def run(args,**kwargs):
        seen.update(kwargs);return _result(args)
    monkeypatch.setattr(merge_gate.subprocess,"run",run)
    merge_gate._command(["synthetic-command"],timeout=0.25)
    assert seen["timeout"]==0.25

@pytest.mark.parametrize("budget",[0,-1,float("inf"),float("nan")])
def test_invalid_budget_rejected_without_command(monkeypatch,budget):
    monkeypatch.setattr(merge_gate,"_command",lambda *a,**k:pytest.fail("command on invalid budget"))
    with pytest.raises(merge_gate.GateError,match="finite"):
        merge_gate.wait_for_promotion_checks(CANDIDATE,TAG,REPO,7,timeout_s=budget)


@pytest.mark.parametrize("field,value",[("run_id",999),("run_attempt",9)])
def test_job_from_another_run_or_attempt_never_passes(monkeypatch,field,value):
    payload=jobs();payload["jobs"][0][field]=value
    clock,_=scripted(monkeypatch,[[run_row()]],job_payload=payload)
    with pytest.raises(merge_gate.GateError,match="run/attempt mismatch"):wait(clock)


def test_invalid_promote_budget_refuses_before_tag_or_any_command(monkeypatch):
    monkeypatch.setattr(merge_gate,"_command",lambda *a,**k:pytest.fail("command on invalid promote budget"))
    with pytest.raises(merge_gate.GateError,match="finite"):
        merge_gate.promote(CANDIDATE,123,checks_timeout_s=0)


@pytest.mark.parametrize("newer,older",[
    (run_row(201,3,status="queued",conclusion=None),run_row(200,2)),
    (run_row(attempt=2,status="queued",conclusion=None),run_row(attempt=1)),
])
def test_api_regression_cannot_resurrect_superseded_green(monkeypatch,newer,older):
    clock,_=scripted(monkeypatch,[[newer],[older],[older]])
    with pytest.raises(merge_gate.GateError,match="required_checks_missing"):wait(clock)
    assert clock.now==5


@pytest.mark.parametrize("bad_log",[
    CHECKOUT_LOG.replace("refs/tags/", "refs/remotes/origin/"),
    CHECKOUT_LOG.replace("checkout --progress --force refs/tags/", "checkout --progress --force -B duplicate refs/remotes/origin/") + CHECKOUT_LOG,
    CHECKOUT_LOG.replace(CANDIDATE, OLD),
    CHECKOUT_LOG.replace("##[group]Checking out the ref", "##[group]Other phase"),
    CHECKOUT_LOG.replace("[command]/usr/bin/git checkout", "  [command]/usr/bin/git checkout"),
    CHECKOUT_LOG.replace("refs/tags/" + TAG, "refs/tags/other"),
    "commit prose: " + CHECKOUT_LOG,
    "",
])
def test_exact_tag_proof_rejects_branch_spoof_malformed_and_missing(monkeypatch,bad_log):
    clock,_=scripted(monkeypatch,[[run_row()]],log=bad_log)
    with pytest.raises(merge_gate.GateError,match="checkout proof absent"):wait(clock)


@pytest.mark.parametrize("mutation",["missing_checkout","duplicate_checkout","wrong_number","missing_id","malformed_job"])
def test_checkout_metadata_is_bound_before_log_read(monkeypatch,mutation):
    payload=jobs()
    if mutation=="missing_checkout":payload["jobs"][0]["steps"]=[s for s in payload["jobs"][0]["steps"] if s["name"]!="Run actions/checkout@v4"]
    elif mutation=="duplicate_checkout":payload["jobs"][0]["steps"].append(dict(payload["jobs"][0]["steps"][1]))
    elif mutation=="wrong_number":payload["jobs"][0]["steps"][1]["number"]=9
    elif mutation=="missing_id":payload["jobs"][0].pop("id")
    else:payload["jobs"]=[None]
    clock,calls=scripted(monkeypatch,[[run_row()]],job_payload=payload)
    with pytest.raises(merge_gate.GateError):wait(clock)
    assert not any(args[2].endswith("/logs") for args,_ in calls)


def test_documented_job_without_attempt_field_uses_attempt_endpoint(monkeypatch):
    payload=jobs();payload["jobs"][0].pop("run_attempt")
    clock,calls=scripted(monkeypatch,[[run_row()]],job_payload=payload)
    assert wait(clock)["run_attempt"]==1
    assert any("/attempts/1/jobs" in args[2] for args,_ in calls)


def test_log_timeout_cannot_extend_absolute_budget(monkeypatch):
    clock,calls=scripted(monkeypatch,[[run_row()]])
    original=merge_gate._command
    def command(args,**kwargs):
        if args[2].endswith("/logs"): raise subprocess.TimeoutExpired(args,kwargs["timeout"])
        return original(args,**kwargs)
    monkeypatch.setattr(merge_gate,"_command",command)
    with pytest.raises(merge_gate.GateError,match="timeout"):wait(clock)


def test_log_unavailable_is_missing_evidence_not_green(monkeypatch):
    clock,_=scripted(monkeypatch,[[run_row()]])
    original=merge_gate._command
    def command(args,**kwargs):
        if args[2].endswith("/logs"): return _result(args,returncode=1)
        return original(args,**kwargs)
    monkeypatch.setattr(merge_gate,"_command",command)
    with pytest.raises(merge_gate.GateError,match="required_checks_missing"):wait(clock)

@pytest.mark.parametrize("history",[
    {"total_count":1,"workflow_runs":[run_row()]},
    {"total_count":2,"workflow_runs":[run_row()]},
    {"total_count":1,"workflow_runs":[None]},
    {"total_count":1,"workflow_runs":[{}]},
    {"total_count":1,"workflow_runs":[{"head_sha":CANDIDATE}]},
    {"total_count":1,"workflow_runs":[{"head_branch":TAG}]},
])
def test_fresh_tag_refuses_stale_or_incomplete_history_before_push(monkeypatch,history):
    _,_,_,calls=_fake_evidence(monkeypatch)
    original=merge_gate._command
    def command(args,**kwargs):
        if args[:2]==["gh","api"] and args[2].endswith("&page=1"):
            calls.append(args);return _result(args,json.dumps(history))
        return original(args,**kwargs)
    monkeypatch.setattr(merge_gate,"_command",command)
    with pytest.raises(merge_gate.GateError,match="historical|incomplete"):
        merge_gate.promote(CANDIDATE,123)
    assert not any(args[:2]==["git","push"] for args in calls)


def test_history_and_wait_share_one_deadline(monkeypatch):
    _,_,_,calls=_fake_evidence(monkeypatch)
    original=merge_gate._command;clock=Clock()
    def command(args,**kwargs):
        if args[:2]==["gh","api"] and args[2].endswith("&page=1"):
            calls.append(args);clock.now+=6
            return _result(args,json.dumps({"total_count":0,"workflow_runs":[]}))
        return original(args,**kwargs)
    monkeypatch.setattr(merge_gate,"_command",command)
    with pytest.raises(merge_gate.GateError,match="timeout during tag history"):
        merge_gate.promote(CANDIDATE,123,checks_timeout_s=5,clock=clock,sleep=clock.sleep)
    assert not any(args[:2]==["git","push"] for args in calls)


LATER_OUTPUT = "".join(f"2026-01-01T00:00:00.0000000Z gate output line {n}\n" for n in range(40000))

def test_whole_job_log_reads_jobs_endpoint_with_escape_flag_and_bounded_prefix(monkeypatch):
    # A real job log is the checkout groups followed by far more gate output than the parsed prefix.
    log=CHECKOUT_LOG+LATER_OUTPUT
    assert len(log) > 4*merge_gate.CHECKOUT_PROOF_PREFIX_CHARS
    clock,calls=scripted(monkeypatch,[[run_row()]],log=log)
    assert wait(clock)["status"]=="green"
    reads=[args for args,_ in calls if args[2].endswith("/logs")]
    assert reads==[["gh","api",f"repos/{REPO}/actions/jobs/2000/logs","--allow-escape-sequences"]]
    assert all(args[-1]!="--allow-escape-sequences" for args,_ in calls if not args[2].endswith("/logs"))


@pytest.mark.parametrize("log",[
    CHECKOUT_LOG.replace(CANDIDATE, OLD)+LATER_OUTPUT+CHECKOUT_LOG,
    CHECKOUT_LOG.replace("refs/tags/" + TAG, "refs/heads/" + TAG)+LATER_OUTPUT+CHECKOUT_LOG,
    LATER_OUTPUT+CHECKOUT_LOG,
])
def test_later_favorable_text_in_job_log_cannot_establish_green(monkeypatch,log):
    clock,_=scripted(monkeypatch,[[run_row()]],log=log)
    with pytest.raises(merge_gate.GateError,match="checkout proof absent"):wait(clock)


def test_prefix_keeps_whole_lines_and_parser_refuses_oversized_input():
    log=CHECKOUT_LOG+LATER_OUTPUT
    prefix=merge_gate._checkout_log_prefix(log)
    assert prefix.endswith("\n") and log.startswith(prefix)
    assert len(prefix) <= merge_gate.CHECKOUT_PROOF_PREFIX_CHARS
    assert merge_gate._checkout_log_prefix(CHECKOUT_LOG)==CHECKOUT_LOG
    assert not merge_gate.checkout_tag_proof(log,CANDIDATE,TAG)


def test_default_budget_exceeds_observed_tag_run_duration():
    import inspect
    assert merge_gate.DEFAULT_CHECKS_TIMEOUT_S >= 900
    for function,name in ((merge_gate.promote,"checks_timeout_s"),(merge_gate.wait_for_promotion_checks,"timeout_s")):
        assert inspect.signature(function).parameters[name].default == merge_gate.DEFAULT_CHECKS_TIMEOUT_S


def test_h5_synthetic_fixture_manifest_is_complete_and_hash_pinned():
    import hashlib
    from pathlib import Path
    root=Path(__file__).parents[1]
    manifest=json.loads((root/"tests/fixtures/th_h5/MANIFEST.json").read_text())
    assert {row["path"] for row in manifest["files"]} == {
        "tests/fixtures/th_h5/tag_checkout.txt","tests/test_merge_gate_polling.py","tests/test_deploy_fleet_verdict.py"}
    for row in manifest["files"]:
        assert row["synthetic"] is True and row["contains_real_traffic"] is False
        assert hashlib.sha256((root/row["path"]).read_bytes()).hexdigest()==row["sha256"]
