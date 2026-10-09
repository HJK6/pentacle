"""A1 acceptance through the real shared parser, projection and offline CLI."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from _shared.specs_parser import parse_work_facts
from work_lane_progress import aggregate_members

NUMERIC = "remaining_work_h: 2–4 (median 3) as_of 2026-10-09"
EXEMPT = "remaining_work_h: n/a — umbrella"


def facts(*lines, status="in_progress"):
    return parse_work_facts("## Estimate\n" + "\n".join("- " + s for s in lines), "", status)


@pytest.mark.parametrize("dash", ["-", "–", "—"])
@pytest.mark.parametrize("date", ["2026-10-09", "2024-02-29"])
def test_remaining_numeric_grammar_and_as_of(dash, date):
    actual = facts("estimated_at: 2020-01-01", f"remaining_work_h: 1.5{dash}3.5 (median 2) as_of {date}")
    assert actual["estimate"] == {"p25": 1.5, "p75": 3.5, "median": 2.0, "as_of": date, "provisional": False}
    assert actual["estimate_exempt"] is False


@pytest.mark.parametrize("first", [NUMERIC + " provisional", EXEMPT, "remaining_work_h: n/a — coordination"])
@pytest.mark.parametrize("last", ["remaining_work_h: 1–3 (median 2) as_of 2024-02-29", EXEMPT,
                                 "remaining_work_h: n/a — coordination"])
def test_last_physical_declaration_wins(first, last):
    actual = facts(first, last)
    if "n/a" in last:
        assert actual["estimate"] is None and actual["estimate_exempt"] is True
    else:
        assert actual["estimate"] == {"p25": 1.0, "p75": 3.0, "median": 2.0,
                                      "as_of": "2024-02-29", "provisional": False}
        assert actual["estimate_exempt"] is False


INVALID = ["", "<p25>–<p75> (median <m>) as_of 2026-10-09", "2–4 as_of 2026-10-09", "broken",
           "0–4 (median 2)", "-1–4 (median 2)", "1–0 (median 2)", "1–-4 (median 2)",
           "1–4 (median 0)", "1–4 (median -2)", "5–3 (median 4)", "2–4 (median 1)", "2–4 (median 5)",
           "NaN–4 (median 3)", "1–Infinity (median 2)", "1–4 (median NaN)",
           "9" * 400 + "–" + "9" * 401 + " (median " + "9" * 400 + ")"]
INVALID = [v if not v or "as_of" in v or v in ("broken",) else v + " as_of 2026-10-09" for v in INVALID]
INVALID += ["2–4 (median 3)" + suffix for suffix in ["", " as_of", " as_of 2026-1-01", " as_of 2026-01-1",
            " as_of 2026-02-29", " as_of 2026-13-01", " as_of 2026-04-31", " as_of 0000-01-01",
            " as_of 2026-10-09 extra", " as_of 2026-10-09T00:00:00"]]


@pytest.mark.parametrize("predecessor", [NUMERIC, EXEMPT])
@pytest.mark.parametrize("invalid", INVALID)
def test_invalid_latest_never_revives_predecessor(predecessor, invalid):
    actual = facts(predecessor, "remaining_work_h: " + invalid)
    assert actual["estimate"] is None and actual["estimate_exempt"] is False


@pytest.mark.parametrize("selected,expected", [(NUMERIC, False), (NUMERIC + " provisional", True)])
def test_provisional_is_selected_line_local(selected, expected):
    actual = facts(NUMERIC + " provisional", "basis: provisional", selected, "elapsed_delivery_h: 1–3 (median 2) provisional")
    assert actual["estimate"]["provisional"] is expected


def test_only_unfenced_estimate_section_contributes():
    text = f"## Elsewhere\n- {NUMERIC}\n```\n## Estimate\n- {NUMERIC}\n```\n## Estimate\n- elapsed_delivery_h: 2–4 (median 3)\n~~~\n- {NUMERIC}\n~~~\n## Next\n- {NUMERIC}\n"
    actual = parse_work_facts(text, "", "in_progress")
    assert actual["estimate"] is None and actual["estimate_exempt"] is False


def test_elapsed_overlap_ignored_and_remaining_sum():
    legacy = [facts("elapsed_delivery_h: " + r) for r in ("2–4 (median 3)", "1–3 (median 2)")]
    actual = aggregate_members(legacy, None)
    assert actual["open_estimate_h"] is None and actual["open_estimated"] == 0 and not actual["estimate_complete"]
    items = [facts(NUMERIC), facts("remaining_work_h: 1–3 (median 2) as_of 2026-10-09")]
    actual = aggregate_members(items, None)
    assert actual["open_estimate_h"] == {"p25": 3, "p75": 7, "median": 5}
    assert actual["open_estimated"] == 2 and actual["estimate_complete"] is True


@pytest.mark.parametrize("kind", ["umbrella", "coordination"])
def test_exemption_coverage_partition_and_terminal_exclusion(kind):
    items = [facts(NUMERIC), facts("remaining_work_h: n/a — " + kind)]
    items += [facts(NUMERIC, status=s) for s in ("completed", "deprecated", "missing", "ambiguous")]
    actual = aggregate_members(items, None)
    assert actual["items_total"] == 6 and actual["items_open"] == 2
    assert actual["items_completed"] == 1 and actual["items_dropped"] == 1 and actual["items_unresolved"] == 2
    assert actual["open_estimated"] == 1 and actual["estimate_complete"] is True
    assert actual["open_estimate_h"] == {"p25": 2, "p75": 4, "median": 3}
    assert aggregate_members(items + [facts()], None)["estimate_complete"] is False
    all_exempt = aggregate_members([facts(EXEMPT), facts("remaining_work_h: n/a — coordination")], None)
    assert all_exempt["estimate_complete"] and all_exempt["open_estimated"] == 0 and all_exempt["open_estimate_h"] is None
    assert aggregate_members([], None)["estimate_complete"] is True


def write_spec(root, identity, *lines, status="in_progress", slug=None):
    folder = root / "work" / status / (slug or identity)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "spec.md").write_text(f"---\nid: {identity}\ntitle: Synthetic bridge\n---\n## Estimate\n" +
                                  "\n".join("- " + line for line in lines) + "\n")
    (folder / "summary.md").write_text("**Next action** — Inspect the deck.\n")
    return folder


def manifest(*members, lanes=None):
    return {"schema": "work_lane_estimate_manifest_v1", "lanes": lanes if lanes is not None else [
        {"composite_stream_id": "example:assistant", "lane_id": "wl-demo", "state": "paused",
         "members_total": len(members), "members": list(members)}]}


def offline(root, value, *extra):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "agent-orch")
    return subprocess.run([sys.executable, "-B", "-m", "agent_orch.cli", "work-lane", "estimate-preview",
                           "--memory-root", str(root), "--lanes-json", "-", *extra],
                          input=value if isinstance(value, str) else json.dumps(value), text=True,
                          capture_output=True, env=env, timeout=15)


def test_actual_offline_command_exact_stable_projection(tmp_path):
    ids = ["spec_demo__bridge", "spec_demo__deck", "spec_demo__umbrella"]
    for identity, line in zip(ids, [NUMERIC, "remaining_work_h: 1–3 (median 2) as_of 2026-10-09 provisional", EXEMPT]):
        write_spec(tmp_path, identity, line)
    first, second = offline(tmp_path, manifest(*ids)), offline(tmp_path, manifest(*ids))
    assert first.returncode == 0, first.stderr
    assert first.stdout == second.stdout
    value = json.loads(first.stdout)
    lane = value["lanes"][0]
    assert value["schema"] == "work_lane_estimate_projection_v1" and value["errors"] == []
    assert set(lane) == {"composite_stream_id", "lane_id", "state", "members_total", "members", "open_estimate_h", "open_estimated", "estimate_complete"}
    assert lane["open_estimate_h"] == {"p25": 3.0, "p75": 7.0, "median": 5.0}
    assert lane["open_estimated"] == 2 and lane["estimate_complete"] is True
    assert lane["members"][2] == {"spec_id": ids[2], "status": "in_progress", "estimate": None, "estimate_exempt": True}
    assert lane["members"][0]["estimate"] == facts(NUMERIC)["estimate"]
    assert lane["members"][1]["estimate"]["provisional"] is True
    assert first.stdout == json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"


def test_offline_full_membership_and_lane_census_no_caps(tmp_path):
    ids = [f"spec_demo__item{i}" for i in range(32)]
    for identity in ids:
        write_spec(tmp_path, identity, NUMERIC)
    lanes = [dict(manifest(*ids)["lanes"][0], lane_id=f"wl-{i:03}", composite_stream_id=f"example{i}:assistant",
                  state="done" if i % 2 else "active") for i in range(70)]
    result = offline(tmp_path, manifest(lanes=list(reversed(lanes))))
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)["lanes"]
    assert len(actual) == 70 and all(len(l["members"]) == l["members_total"] == 32 for l in actual)
    assert [(l["composite_stream_id"], l["lane_id"]) for l in actual] == sorted((l["composite_stream_id"], l["lane_id"]) for l in lanes)
    assert all(l["open_estimate_h"] == {"p25": 64, "p75": 128, "median": 96} for l in actual)


@pytest.mark.parametrize("value", ["{", "null", "[]", "{}", {"schema": "wrong", "lanes": []},
                                  {"schema": "work_lane_estimate_manifest_v1"}])
def test_offline_invalid_json_schema(tmp_path, value):
    (tmp_path / "work").mkdir()
    result = offline(tmp_path, value)
    assert result.returncode != 0 and (result.stderr or json.loads(result.stdout)["errors"])


@pytest.mark.parametrize("patch", [{"state": "unknown"}, {"members_total": 2}, {"members_total": True},
    {"members_total": -1}, {"members": ["../outside"]}, {"members": ["spec_a", "spec_a"], "members_total": 2},
    {"members": [f"spec_{i}" for i in range(33)], "members_total": 33}, {"lane_id": ""}, {"composite_stream_id": ""},
    {"members": "spec_a"}, {"members": [12]}, {"members_total": 1.0}])
def test_offline_invalid_lane_contract(tmp_path, patch):
    (tmp_path / "work").mkdir()
    value = manifest("spec_a")
    value["lanes"][0].update(patch)
    result = offline(tmp_path, value)
    assert result.returncode != 0


def test_offline_duplicate_lane_and_explicit_empty_cases(tmp_path):
    (tmp_path / "work").mkdir()
    value = manifest()
    value["lanes"] *= 2
    assert offline(tmp_path, value).returncode != 0
    for value in (manifest(lanes=[]), manifest()):
        result = offline(tmp_path, value)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["errors"] == []


@pytest.mark.parametrize("failure", ["missing", "ambiguous", "malformed", "unsettled", "missing_root"])
def test_offline_source_errors_fail_closed_without_paths(tmp_path, failure):
    root = tmp_path / "root"
    identity = "spec_demo__bridge"
    if failure != "missing_root":
        (root / "work").mkdir(parents=True)
    if failure not in ("missing", "missing_root"):
        folder = write_spec(root, identity, NUMERIC)
        if failure == "ambiguous":
            write_spec(root, identity, NUMERIC, slug="duplicate")
        elif failure == "malformed":
            (folder / "summary.md").write_text("---\na: [broken\n---\n")
        elif failure == "unsettled":
            (folder / "spec.sync-conflict-fixture.md").write_text("ignored conflict")
    result = offline(root, manifest(identity))
    assert result.returncode != 0
    value = json.loads(result.stdout)
    assert value["errors"] and all(set(e) == {"spec_id", "code"} for e in value["errors"])
    assert str(tmp_path) not in result.stdout and "broken" not in result.stdout
    member = value["lanes"][0]["members"][0]
    assert member["estimate"] is None and member["estimate_exempt"] is False
    assert member["status"] == ("ambiguous" if failure == "ambiguous" else "missing")


def test_offline_invalid_estimate_is_null_success_and_done_excluded(tmp_path):
    write_spec(tmp_path, "spec_open", NUMERIC, "remaining_work_h: invalid")
    write_spec(tmp_path, "spec_done", NUMERIC, status="completed")
    result = offline(tmp_path, manifest("spec_open", "spec_done"))
    assert result.returncode == 0, result.stderr
    lane = json.loads(result.stdout)["lanes"][0]
    assert lane["open_estimate_h"] is None and lane["open_estimated"] == 0 and not lane["estimate_complete"]
    assert lane["members"][1]["estimate"] == facts(NUMERIC)["estimate"]


@pytest.mark.parametrize("trap", ["root", "work", "status", "config", "folder", "spec", "summary", "traversal"])
def test_offline_symlink_and_status_escape_rejected_without_writes(tmp_path, trap):
    root, outside = tmp_path / "root", tmp_path / "outside"
    folder = write_spec(root, "spec_demo__bridge", NUMERIC)
    other = write_spec(outside, "spec_demo__bridge", NUMERIC)
    if trap == "traversal":
        (root / "work" / "statuses.json").write_text(json.dumps({"statuses": [{"name": "../../outside/work/in_progress", "order": 1}]}))
    else:
        target, source = {"root": (root, outside), "work": (root / "work", outside / "work"),
            "status": (folder.parent, other.parent), "config": (root / "work" / "statuses.json", outside / "trap.json"),
            "folder": (folder, other), "spec": (folder / "spec.md", other / "spec.md"),
            "summary": (folder / "summary.md", other / "summary.md")}[trap]
        if trap == "config":
            source.write_text('{"statuses": []}')
        if target.exists():
            target.rename(target.with_name(target.name + ".retained"))
        target.symlink_to(source, target_is_directory=source.is_dir())
    def snapshot():
        return {str(p.relative_to(tmp_path)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in tmp_path.rglob("*") if p.is_file() and not p.is_symlink()}
    before = snapshot()
    result = offline(root, manifest("spec_demo__bridge"))
    assert result.returncode != 0
    assert snapshot() == before
    assert json.loads(result.stdout)["errors"]


@pytest.mark.parametrize("names", [["../outside"], ["/outside"], [".."], ["x/y"], ["in_progress", "in_progress"]])
def test_offline_status_names_never_escape_or_duplicate(tmp_path, names):
    root = tmp_path / "root"
    (root / "work").mkdir(parents=True)
    (root / "work" / "statuses.json").write_text(json.dumps({"statuses": [{"name": n, "order": i} for i, n in enumerate(names)]}))
    result = offline(root, manifest())
    assert result.returncode == 1 and json.loads(result.stdout)["errors"]


def test_offline_uses_only_configured_statuses_and_excludes_artifacts(tmp_path):
    identity = "spec_demo__bridge"
    write_spec(tmp_path, identity, NUMERIC, status="custom")
    (tmp_path / "work" / "statuses.json").write_text(json.dumps({"statuses": [{"name": "custom", "order": 1}]}))
    for status, slug in [("in_progress", "ignored"), ("custom", "_artifacts"), ("custom", ".hidden"),
                         ("custom", "demo.sync-conflict-copy")]:
        folder = write_spec(tmp_path, identity, EXEMPT, status=status, slug=slug)
        (folder / "spec.md").write_text("---\na: [malformed trap\n---\n")
    nested = tmp_path / "work" / "custom" / identity / "_artifacts"
    write_spec(nested, identity, EXEMPT)
    (tmp_path / "catalog").mkdir()
    (tmp_path / "catalog" / "documents.json").write_text("malformed trap")
    result = offline(tmp_path, manifest(identity))
    assert result.returncode == 0, result.stderr
    member = json.loads(result.stdout)["lanes"][0]["members"][0]
    assert member["status"] == "custom" and member["estimate"] == facts(NUMERIC)["estimate"]


def test_offline_conflicting_manifest_rejects_but_done_history_and_other_composite_pass(tmp_path):
    write_spec(tmp_path, "spec_demo__bridge", NUMERIC)
    one = manifest("spec_demo__bridge")["lanes"][0]
    two = dict(one, lane_id="wl-other")
    result = offline(tmp_path, manifest(lanes=[one, two]))
    assert result.returncode == 1
    assert {"spec_id": "spec_demo__bridge", "code": "work_lane_member_conflict"} in json.loads(result.stdout)["errors"]
    for patch in ({"state": "done"}, {"composite_stream_id": "other:assistant"}):
        assert offline(tmp_path, manifest(lanes=[one, dict(two, **patch)])).returncode == 0


def test_offline_sum_overflow_is_finite_json_failure(tmp_path):
    huge = "9" * 308
    for identity in ("spec_one", "spec_two"):
        write_spec(tmp_path, identity, f"remaining_work_h: {huge}–{huge} (median {huge}) as_of 2026-10-09")
    result = offline(tmp_path, manifest("spec_one", "spec_two"))
    assert result.returncode == 1
    actual = json.loads(result.stdout, parse_constant=lambda value: pytest.fail("nonfinite JSON"))
    assert {"spec_id": None, "code": "work_estimate_sum_nonfinite"} in actual["errors"]


def test_offline_bounded_reader_never_opens_symlink_target(tmp_path, monkeypatch, capsys):
    import io
    from agent_orch import cli
    from _shared import specs_service
    root, outside = tmp_path / "root", tmp_path / "outside"
    folder = write_spec(root, "spec_demo__bridge", NUMERIC)
    other = write_spec(outside, "spec_demo__bridge", NUMERIC)
    (folder / "summary.md").unlink()
    (folder / "summary.md").symlink_to(other / "summary.md")
    opened = []
    real_open = os.open
    def check(path, flags, *args, **kwargs):
        assert str(outside) not in str(path), "outside data opened"
        assert str(path) != "summary.md", "symlink source opened"
        opened.append(str(path))
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", check)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(manifest("spec_demo__bridge"))))
    assert cli.main(["work-lane", "estimate-preview", "--memory-root", str(root), "--lanes-json", "-"]) == 1
    assert opened and json.loads(capsys.readouterr().out)["errors"]


def test_offline_unsettled_coherent_read_is_not_accepted(tmp_path, monkeypatch, capsys):
    import io
    from agent_orch import cli
    from _shared import specs_service
    folder = write_spec(tmp_path, "spec_demo__bridge", NUMERIC)
    source = specs_service._BoundedSource.read_text
    def racing_read(self, **kwargs):
        text = source(self, **kwargs)
        if self.name == "summary.md":
            (folder / "spec.md").write_text((folder / "spec.md").read_text() + "\nchanged during read\n")
        return text
    monkeypatch.setattr(specs_service._BoundedSource, "read_text", racing_read)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(manifest("spec_demo__bridge"))))
    assert cli.main(["work-lane", "estimate-preview", "--memory-root", str(tmp_path), "--lanes-json", "-"]) == 1
    assert {"spec_id": "spec_demo__bridge", "code": "work_files_unsettled"} in json.loads(capsys.readouterr().out)["errors"]


def test_offline_output_is_independent_of_clock_and_source_mtimes(tmp_path):
    folder = write_spec(tmp_path, "spec_demo__bridge", NUMERIC)
    before = offline(tmp_path, manifest("spec_demo__bridge"))
    for source in folder.iterdir():
        os.utime(source, (4102444800, 4102444800))  # future, same bytes
    after = offline(tmp_path, manifest("spec_demo__bridge"))
    assert before.returncode == after.returncode == 0
    assert before.stdout == after.stdout


@pytest.mark.parametrize("swap", ["status", "item"])
def test_offline_directory_swap_never_enumerates_outside(tmp_path, monkeypatch, swap):
    from _shared.specs_service import SpecsSubsystem
    root, outside = tmp_path / "root", tmp_path / "outside"
    item = write_spec(root, "spec_demo__bridge", NUMERIC)
    outside.mkdir()
    (outside / "outside-directory-entry").mkdir()
    target = item.parent if swap == "status" else item
    identity = (target.stat().st_dev, target.stat().st_ino)
    moved = tmp_path / "held-original"
    triggered, observed = [], []
    real_iterdir, real_listdir = Path.iterdir, os.listdir
    def replace():
        if not triggered:
            target.rename(moved)
            target.symlink_to(outside, target_is_directory=True)
            triggered.append(True)
    def path_entries(path):
        if path == target:
            replace()
            entries = list(real_iterdir(path))
            observed.extend(p.name for p in entries)
            return iter(entries)
        return real_iterdir(path)
    def fd_entries(fd):
        if isinstance(fd, int) and (os.fstat(fd).st_dev, os.fstat(fd).st_ino) == identity:
            replace()
            entries = real_listdir(fd)
            observed.extend(entries)
            return entries
        return real_listdir(fd)
    monkeypatch.setattr(Path, "iterdir", path_entries)
    monkeypatch.setattr(os, "listdir", fd_entries)
    scanner = SpecsSubsystem(memory_root=root, session_summaries=lambda: [], changed_callback=lambda ids: None, debounce_s=0)
    result = scanner.scan_work_observations()
    assert triggered, "the directory-swap injection must execute"
    assert "outside-directory-entry" not in observed, "outside directory data was read"
    assert not result["available"] or result["errors"]
