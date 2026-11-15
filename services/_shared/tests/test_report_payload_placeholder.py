"""Placeholder final reports are refused (spec_pentacle__report_placeholder_reject_2026_11).

On 2026-11-15 two hidden leads ended on done reports whose bodies were
``summary "x"``, ``findings []``, ``next_action "y"``; the daemon accepted them
as final and the real result was lost.
"""
from __future__ import annotations

import pytest

import _shared.report_payload_v1 as report_payload_v1
from _shared.report_payload_v1 import SchemaError, validate

# The CLI example summary; read through the module so the RED run fails on behaviour, not import.
EXAMPLE_SUMMARY = getattr(report_payload_v1, "EXAMPLE_SUMMARY", "Completed the assigned work.")

REAL_SUMMARY = "Spec QA reviewed the placeholder rule against v2_reports data."
FINDING = {"severity": "info", "where": "spec.md", "issue": "No defects found.", "suggested_fix": None}
TERMINAL = ("done", "error", "aborted")


def _body(status: str, **overrides):
    body = {"summary": REAL_SUMMARY, "findings": [FINDING], "next_action": "none"}
    if status in {"error", "aborted"}:
        body["reason"] = "The prerequisite host was unavailable."
    body.update(overrides)
    return body


def _codes(exc: SchemaError) -> dict[str, str]:
    return {v["field"]: v["code"] for v in exc.violations}


@pytest.mark.parametrize("next_action", ["y", "lead_merge"])
def test_done_placeholder_bodies_from_incident_are_rejected(next_action):
    with pytest.raises(SchemaError) as exc:
        validate({"summary": "x", "findings": [], "next_action": next_action}, "done")
    codes = _codes(exc.value)
    assert codes["summary"] == "placeholder_summary"
    assert codes["findings"] == "findings_unmarked_empty"
    assert ("next_action" in codes) == (next_action == "y")


@pytest.mark.parametrize("status", TERMINAL)
def test_short_summary_and_next_action_rejected_for_terminal_statuses(status):
    with pytest.raises(SchemaError) as exc:
        validate(_body(status, summary="  " + "s" * 19 + "  "), status)
    assert _codes(exc.value) == {"summary": "placeholder_summary"}
    with pytest.raises(SchemaError) as exc:
        validate(_body(status, next_action=" n/a "), status)
    assert _codes(exc.value) == {"next_action": "placeholder_next_action"}
    validate(_body(status, summary="s" * 20, next_action="none"), status)


def test_example_summary_text_rejected():
    with pytest.raises(SchemaError) as exc:
        validate(_body("done", summary=f" {EXAMPLE_SUMMARY.upper()} "), "done")
    assert _codes(exc.value) == {"summary": "placeholder_summary"}


def test_empty_findings_require_no_findings_marker_on_done_only():
    with pytest.raises(SchemaError) as exc:
        validate(_body("done", findings=[]), "done")
    assert _codes(exc.value) == {"findings": "findings_unmarked_empty"}
    validate(_body("done", findings=[], no_findings=True), "done")
    for status in ("error", "aborted"):
        validate(_body(status, findings=[]), status)


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"no_findings": True}, "invalid_dependency"),
        ({"findings": [], "no_findings": "yes"}, "wrong_type"),
        ({"findings": [], "no_findings": False}, None),
    ],
)
def test_no_findings_contradiction_and_type(overrides, code):
    with pytest.raises(SchemaError) as exc:
        validate(_body("done", **overrides), "done")
    codes = _codes(exc.value)
    if code is None:
        assert codes == {"findings": "findings_unmarked_empty"}
    else:
        assert codes["no_findings"] == code


def test_progress_reports_unchanged():
    validate({"summary": "x"}, "progress")
    validate({"summary": "x", "findings": [], "next_action": "y"}, "progress")


@pytest.mark.parametrize(
    "body",
    [
        {"summary": "Shipped e7aae4f9; live proof recorded in agent-outputs.", "findings": [FINDING], "next_action": "none"},
        {"summary": "Spec QA ACCEPT; no defects in the plan or tests.", "findings": [], "no_findings": True, "next_action": "lead_merge"},
        {"summary": "recurring smoke passed on thoth", "findings": [], "no_findings": True, "next_action": "done"},
    ],
)
def test_real_report_shapes_accepted(body):
    validate(body, "done")


def test_reject_placeholders_false_skips_only_new_rules():
    incident = {"summary": "x", "findings": [], "next_action": "y"}
    validate(incident, "done", reject_placeholders=False)
    with pytest.raises(SchemaError) as exc:
        validate({**incident, "stray": 1}, "done", reject_placeholders=False)
    assert _codes(exc.value) == {"stray": "unknown_field"}
    with pytest.raises(SchemaError) as exc:
        validate({**incident, "no_findings": "yes"}, "done", reject_placeholders=False)
    assert _codes(exc.value) == {"no_findings": "wrong_type"}
