"""The same installed-command projection covers thresholds, missing inputs and replay."""
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_orch import cli, reliability_artifacts, reliability_hosted
from agent_orch.reliability_receipts import nightly_slots, stamp
from agent_orch.reliability_score import report
from agent_orch.reliability_work import weekly

NOW = datetime(2026, 3, 18, 12, tzinfo=timezone.utc)
SHA = "a" * 40


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return str(path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_work(root, name, status="backlog"):
    path = root / status / name / "summary.md"
    path.parent.mkdir(parents=True)
    path.write_text(f"---\nid: work_{name}\nowner: operator\nstatus: {status}\nupdated_at: '2026-03-01'\n---\n")
    return str(path)


@pytest.fixture
def sources(tmp_path):
    root = tmp_path
    hosts = ["primary", "peer"]
    checks = {k: {"outcome": "passed"} for k in ("front_desk", "slow_consumer", "fleet_smoke", "client_delivery")}
    checks["fleet_smoke"]["cells"] = [
        dict(host=h, provider=p, prompt_mode=m, outcome="passed")
        for h in hosts for p in ("claude", "codex") for m in ("prompted", "promptless")]
    checks["client_delivery"]["assertions"] = dict.fromkeys(("publish", "receive", "reconnect", "recover"), "passed")
    release = {"complete_through": stamp(NOW), "intents": []}
    for i in range(10):
        target, prior = f"{i+2:040x}", f"{i+1:040x}"
        at = stamp(NOW - timedelta(days=10-i))
        transaction = str(i)
        path = write(root / f"stamp-{i}.json", dict(sha=target, prior_sha=prior, serving=True, **checks,
            restart_activation=dict(target_sha=target, activation_id=transaction), disposition="active and checks passed"))
        release["intents"].append(dict(id=transaction, sha=target, prior_sha=prior, kind="ordinary", created=at,
            source=path, attempts=[dict(attempt=1, path=path, type="stamp", transaction=transaction)]))
    nightly = dict(root=str(root / "soak"), timezone="America/Chicago", local_time="01:00", bindings={})
    for due in nightly_slots(nightly, NOW):
        directory = due.strftime("%Y%m%dT%H%M%SZ")
        path = write(Path(nightly["root"]) / directory / "evidence/receipt.json",
                     dict(job="nightly-soak", date=stamp(due)[:10], source_sha=SHA, finished=True,
                          checks=checks, result="PASS"))
        nightly["bindings"][stamp(due)] = dict(directory=directory, sha256=digest(path), release_sha=SHA)
    runs = [dict(id=i, sha=SHA, created=stamp(NOW-timedelta(minutes=110-i)), attempt=1, first="success",
                 latest="success", failures=[], source=f"workflow/{i}") for i in range(100)]
    hosted = dict(complete_through=stamp(NOW), snapshot=write(root/"runs.json", runs), repository="org/repo")
    work = dict(work_root=str(root/"work"), selector=write(root/"effective.json", {"nonblocking_tests": []}),
                register=write(root/"register.json", dict(complete_through=stamp(NOW), records=[], pending=[])))
    Path(work["work_root"]).mkdir()
    return dict(required_hosts=hosts, hosted=hosted, releases=release, nightly=nightly, work=work)


def change_json(path, **changes):
    p = Path(path)
    data = json.loads(p.read_text())
    write(p, {**data, **changes})


def mark_release_failed(cfg, index):
    path = cfg["releases"]["intents"][index]["attempts"][0]["path"]
    change_json(path, disposition="active with failed checks")


def pkg():
    return dict(runner=["ubuntu-24.04"], job="unit", step="System packages",
                assertion="System packages timed out", mirror_timeout=True, source="job/1")


@pytest.mark.parametrize("failures,passed", [(1, True), (2, False)])
def test_first_try_boundary_and_attached_retry(sources, failures, passed):
    for idx in range(failures):
        row = sources["releases"]["intents"][idx]
        later = copy.deepcopy(row["attempts"][0])
        later["path"] = write(Path(later["path"]).with_name(f"retry-{idx}.json"),
                              json.loads(Path(later["path"]).read_text()))
        later["attempt"] = 2
        row["attempts"].append(later)
        mark_release_failed(sources, idx)
    state = report(sources, NOW)
    assert state["conditions"]["first_try_releases"]["numerator"] == 10 - failures
    assert state["conditions"]["first_try_releases"]["passed"] is passed
    assert state["observations"]["releases"][0]["attempts"][1]["result"] == "passed"
    assert state["conditions"]["scheduled_nights"]["passed"]


@pytest.mark.parametrize("count,passed", [(1, True), (2, False)])
def test_false_red_threshold_and_immutable_attempt(sources, count, passed):
    path = Path(sources["hosted"]["snapshot"])
    rows = json.loads(path.read_text())
    for r in rows[:count]:
        r.update(first="failure", latest="success", attempt=2, failures=[pkg()])
    write(path, rows + [rows[0]])
    state = report(sources, NOW)
    cond = state["conditions"]["false_reds"]
    assert (cond["numerator"], cond["denominator"], cond["passed"]) == (count, 100, passed)
    assert state["conditions"]["open_failure_items"]["status"] == "incomplete"


def test_mixed_and_unreviewed_failures(sources):
    p = Path(sources["hosted"]["snapshot"])
    rows = json.loads(p.read_text())
    more = dict(step="CLI checks", job="contracts", source="job/2", runner=["ubuntu-latest"])
    rows[4].update(first="failure", failures=[pkg(), more], attempt=2)
    rows[5].update(first="failure", failures=[{**more, "step": "Installed doctor"}])
    rows[8].update(first="action_required")
    sources["hosted"]["decisions"] = [dict(run=4, sha=SHA, attempt=1, step="CLI checks", job="contracts",
        reviewer="release owner", revision="1", source="attempt 1 review", reason="dispatch parity failed",
        causes=[dict(kind="code", cause="parser-contract", cause_class="contract drift")])]
    write(p, rows)
    state = report(sources, NOW)
    sample = state["observations"]["hosted"]
    assert sample[4]["classification"] == "genuine_red"
    assert [c["kind"] for c in sample[4]["causes"]] == ["non_code", "code"]
    assert sample[5]["classification"] == sample[8]["classification"] == "incomplete"
    assert state["conditions"]["false_reds"]["numerator"] == 0
    assert state["conditions"]["false_reds"]["status"] == "incomplete"


def test_new_observation_evicts_only_its_own_oldest(sources):
    path = Path(sources["hosted"]["snapshot"])
    rows = json.loads(path.read_text())
    rows.append({**rows[-1], "id": 100, "first": "queued"})
    write(path, rows)
    state = report(sources, NOW)
    hosted = state["observations"]["hosted"]
    assert [r["id"] for r in hosted] == list(range(1, 101))
    assert state["conditions"]["false_reds"]["status"] == "incomplete"
    assert state["conditions"]["first_try_releases"]["numerator"] == 10
    assert state["conditions"]["scheduled_nights"]["numerator"] == 7


def test_scheduled_first_missing_replayed_and_failed_teardown(sources):
    settings = sources["nightly"]
    first = next(iter(settings["bindings"]))
    row = settings["bindings"][first]
    path = Path(settings["root"]) / row["directory"] / "evidence/receipt.json"
    original = json.loads(path.read_text())
    write(Path(settings["root"]) / "adhoc" / "evidence/receipt.json", original)
    path.unlink()
    missing = report(sources, NOW)
    assert missing["observations"]["nightly"][0]["result"] == "incomplete"
    assert missing["conditions"]["scheduled_nights"]["numerator"] == 6
    original["checks"]["client_delivery"]["outcome"] = "failed"
    write(path, original)
    row["sha256"] = digest(path)
    result = report(sources, NOW)
    assert result["conditions"]["scheduled_nights"]["historical_passes"] == 7
    assert result["conditions"]["scheduled_nights"]["numerator"] == 6
    assert "client_delivery/outcome" in result["observations"]["nightly"][0]["missing"]
    row["release_sha"] = "b"*40
    assert report(sources, NOW)["observations"]["nightly"][0]["historical"] is None


def test_lifecycle_and_effective_quarantine_reconcile_beyond_windows(sources):
    w = sources["work"]
    root = Path(w["work_root"])
    make_work(root, "repo__case")
    record = dict(cause="fixture", item="repo__case", test="case", first_seen="2025-01-01T00:00:00Z",
                  occurrences=["public-checks/older/1"])
    change_json(w["register"], records=[record])
    change_json(w["selector"], nonblocking_tests=["case"])
    state = report(sources, NOW)
    assert state["conditions"]["open_quarantines"]["status"] == state["conditions"]["open_failure_items"]["status"] == "fail"
    assert state["observations"]["work"]["rows"][0]["owner"] == "operator"
    change_json(w["selector"], nonblocking_tests=[])
    assert report(sources, NOW)["conditions"]["open_quarantines"]["status"] == "incomplete"
    (root/"backlog/repo__case/summary.md").unlink()
    path = make_work(root, "repo__case", "completed")
    change_json(w["register"], records=[{**record, "disposition": "unreproduced, on watch",
                "recurrence_signature": "case", "disposition_source": path}])
    assert report(sources, NOW)["conditions"]["open_failure_items"]["status"] == "incomplete"
    with Path(path).open("a") as handle:
        handle.write("Closed unreproduced, on watch; recurrence signature case.\n")
    assert report(sources, NOW)["conditions"]["open_failure_items"]["passed"]


def test_automatic_selection_requires_matching_open_decision(sources, tmp_path):
    w = sources["work"]
    test_id = "tests/test_example.py::test_example"
    inventory = tmp_path / "checkout/scripts/quarantine.json"
    w["selector_checkout"] = str(inventory.parents[1])
    make_work(Path(w["work_root"]), "repo__case")
    record = dict(cause="fixture", item="repo__case", test=test_id,
                  first_seen="2025-01-01T00:00:00Z")
    change_json(w["register"], records=[record])
    write(inventory, {"version": 1, "nonblocking_tests": [test_id]})
    condition = report(sources, NOW)["conditions"]["open_quarantines"]
    assert condition["status"] == "fail" and condition["numerator"] == 1
    write(inventory, {"version": 1, "nonblocking_tests": []})
    assert report(sources, NOW)["conditions"]["open_quarantines"]["status"] == "incomplete"
    change_json(w["register"], records=[])
    condition = report(sources, NOW)["conditions"]["open_quarantines"]
    assert condition["passed"] and condition["numerator"] == 0


def test_inert_command_three_exit_states(sources, tmp_path, capsys):
    config = write(tmp_path/"sources.json", sources)
    args = ["reliability", "score", "--as-of", stamp(NOW), "--sources", config]
    def hashes():
        return {p: digest(p) for p in tmp_path.rglob("*") if p.is_file()}
    before = hashes()
    assert cli.main(args+["--json"]) == 0
    green = json.loads(capsys.readouterr().out)
    assert all(row["passed"] for row in green["conditions"].values())
    assert cli.main(args) == 0
    rendered = capsys.readouterr().out
    for name in green["conditions"]:
        assert name in rendered
        assert green["conditions"][name]["reason"] in rendered
    assert hashes() == before
    # Link both measured release failures to one already closed, evidenced item.
    for idx in (0, 1):
        mark_release_failed(sources, idx)
    path = make_work(Path(sources["work"]["work_root"]), "release__case", "completed")
    with Path(path).open("a") as handle:
        handle.write("Repaired the release check.\n")
    change_json(sources["work"]["register"], records=[dict(cause="known", item="release__case",
        first_seen=stamp(NOW-timedelta(days=11)), disposition="repaired", disposition_source=path,
        occurrences=["release/0/1","release/1/1"])])
    assert cli.main(args+["--json"]) == 1
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["status"] == "fail"
    Path(sources["hosted"]["snapshot"]).unlink()
    assert cli.main(args+["--json"]) == 1
    assert json.loads(capsys.readouterr().out)["conditions"]["false_reds"]["status"] == "incomplete"



def test_decisive_false_reds_preserve_missing_night(sources, tmp_path, capsys):
    hosted = Path(sources["hosted"]["snapshot"])
    rows = json.loads(hosted.read_text())
    for row in rows[:2]:
        row.update(first="failure", failures=[pkg()])
    write(hosted, rows)
    del sources["nightly"]["bindings"][next(iter(sources["nightly"]["bindings"]))]
    config = write(tmp_path / "mixed.json", sources)
    args = ["reliability", "score", "--sources", config, "--as-of", stamp(NOW), "--json"]
    assert cli.main(args) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["conditions"]["false_reds"]["status"] == "fail"
    assert result["conditions"]["scheduled_nights"]["status"] == "incomplete"

    write(hosted, rows[2:])
    assert cli.main(args) == 2


def test_weekly_reclassification_keeps_one_evidence_identity():
    at = "2026-03-10T08:00:00Z"
    events = [dict(id="same", kind="class", at=at, **{"class": cls})
              for cls in ("timing", "fixture")]
    week = weekly({"events": events}, NOW)["weeks"][-2]
    assert week["evidence_count"] == 1
    assert week["classes"] == ["fixture"]

def test_weekly_dedup_aliases_and_partial_week():
    h = dict(from_inception=True, complete_through=stamp(NOW), class_aliases={"legacy":"fixture"}, events=[])
    for day, cls in ((4, "legacy"), (7, "fixture"), (10, "timing")):
        h["events"].append(dict(id=f"occ-{day}", kind="class", at=f"2026-03-{day:02}T08:00:00Z", **{"class":cls}))
    h["events"] += [h["events"][0], dict(id="decision", kind="quarantined", test="test-1", at="2026-03-03T10:00:00Z")]
    trend = weekly(h, NOW)
    assert [r["new_classes"] for r in trend["weeks"]] == [1, 1, 0]
    assert [r["evidence_count"] for r in trend["weeks"]] == [3, 1, 0]
    assert set(trend["review_signals"]) == {"new_classes", "open_quarantines"}
    assert trend["weeks"][-1]["partial_week"]


def test_first_attempt_junit_checks_the_complete_failure_population():
    xml = b'<testsuites><testsuite failures="2" errors="0"><testcase classname="m" name="t"><failure message="bad"/></testcase></testsuite></testsuites>'
    with pytest.raises(ValueError, match="census"):
        reliability_artifacts.junit(xml)
    facts = reliability_artifacts.junit(xml.replace(b'failures="2"', b'failures="1"'))
    assert facts == [dict(test="m::t", assertion_digest=hashlib.sha256(b"bad").hexdigest())]


def test_refusal_before_activation_and_independent_release_expiry(sources, tmp_path):
    intents = sources["releases"]["intents"]
    previous = intents[-1]["sha"]
    target = "c" * 40
    receipt = write(tmp_path/"forward-attempt.json", dict(attempt=1, target_sha=target, exit_code=2,
        created_at=stamp(NOW-timedelta(seconds=1)), outcome="refused_before_activation",
        evidence={"preimage": {"source_sha": previous}}))
    sources["releases"]["attempt_sources"] = [dict(root=str(tmp_path), pattern="forward-attempt.json", type="refusal")]
    data = report(sources, NOW)
    last = data["observations"]["releases"][-1]
    assert last["attempts"][0]["source"] == receipt and last["result"] == "failed"
    assert data["observations"]["releases"][0]["id"] == "1"
    assert data["conditions"]["false_reds"]["denominator"] == 100
    assert data["conditions"]["scheduled_nights"]["historical_passes"] == 7


def test_incomplete_denominator_and_due_slot_boundary(sources):
    sources["releases"]["intents"].pop()
    path = Path(sources["hosted"]["snapshot"])
    write(path, json.loads(path.read_text())[1:])
    later = report(sources, NOW + timedelta(days=1))
    assert later["conditions"]["first_try_releases"]["denominator"] == 9
    assert later["conditions"]["false_reds"]["denominator"] == 99
    assert later["observations"]["nightly"][-1]["result"] == "missing"
    assert later["conditions"]["scheduled_nights"]["numerator"] == 6
    assert all(later["conditions"][k]["status"] == "incomplete"
               for k in ("first_try_releases", "false_reds", "scheduled_nights"))


def test_unverified_timeout_and_portability_are_distinct():
    original = dict(id=1, first="failure", sha=SHA, source="attempt/1")
    timeout = {**pkg(), "mirror_timeout": False}
    assert reliability_hosted.classifications({**original, "failures": [timeout]}, {})[0] == "incomplete"
    platform = dict(job="unit", step="Daemon unit partition", runner=["ubuntu-24.04"],
                    source="job/2", portability="macOS sandbox required in Linux gate setup")
    verdict, causes = reliability_hosted.classifications({**original, "failures": [platform, pkg()]}, {})
    assert verdict == "genuine_red"
    assert [c["kind"] for c in causes] == ["code", "non_code"]


def test_archived_projection_binds_original_and_current_digest(sources, tmp_path):
    settings = sources["nightly"]
    slot, row = next(iter(settings["bindings"].items()))
    path = Path(settings["root"]) / row["directory"] / "evidence/receipt.json"
    archived = write(tmp_path/"retained.json", dict(source=str(path), original_sha256=row["sha256"],
                                                     fields=json.loads(path.read_text())))
    row["retained"] = {"path": archived, "sha256": digest(archived)}
    path.unlink()
    assert report(sources, NOW)["observations"]["nightly"][0]["result"] == "passed"
    change_json(archived, original_sha256="0"*64)
    assert report(sources, NOW)["observations"]["nightly"][0]["result"] == "incomplete"


def test_ambiguous_intents_and_rehearsals(sources):
    rows = sources["releases"]["intents"]
    rows[0].update(kind="rehearsal", reason="declared rollback exercise")
    rows[1]["kind"] = "unknown"
    state = report(sources, NOW)
    assert state["observations"]["excluded_releases"][0]["id"] == "0"
    assert state["observations"]["releases"][0]["result"] == "incomplete"
    assert state["conditions"]["first_try_releases"]["passed"] is False



def test_release_timestamp_is_normalized_to_z(sources):
    entry = sources["releases"]["intents"][0]
    entry["created"] = entry["created"].replace("Z", "+00:00")
    result = report(sources, NOW)
    assert result["observations"]["releases"][0]["created"].endswith("Z")


def test_lifecycle_timestamp_compares_same_day_instants(sources):
    work = sources["work"]
    name = "pentacle__clock_case"
    path = Path(make_work(Path(work["work_root"]), name))
    change_json(work["register"], records=[dict(cause="clock", item=name, first_seen="2026-03-01T00:00:00Z")])
    before = path.read_text().replace("2026-03-01", "2026-03-18T05:59:00-06:00")
    path.write_text(before)
    assert report(sources, NOW)["conditions"]["open_failure_items"]["numerator"] == 1
    path.write_text(before.replace("05:59", "06:01"))
    state = report(sources, NOW)["conditions"]["open_failure_items"]
    assert state["status"] == "incomplete"
    assert "current lifecycle is newer than as-of: " + name in state["missing"]
