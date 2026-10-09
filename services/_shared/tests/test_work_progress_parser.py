from pathlib import Path

from _shared.specs_parser import DEFAULT_STATUSES, parse_work_folder

GOLDEN = Path(__file__).parent / "fixtures" / "work_progress" / "golden"


def test_golden_work_facts():
    item = parse_work_folder(GOLDEN, "in_progress", DEFAULT_STATUSES)
    assert item["id"] == "spec_demo__paper_bridge"
    assert item["title"] == "Build a paper bridge for the model river"
    assert (item["ac_checked"], item["ac_total"]) == (2, 3)
    assert item["estimate"] == {"p25": 2, "p75": 6, "median": 4, "provisional": True, "as_of": "2026-10-09"}
    assert item["status_text"] == "Supports cut; deck ready for assembly."
    assert item["next_action_text"] == "Attach the deck to both supports."


def test_missing_sections_are_null_and_status_comes_from_folder(tmp_path):
    from _shared.specs_parser import parse_work_facts
    item = parse_work_facts("---\nid: spec_demo__empty\nstatus: completed\n---\n", "No labels.", "backlog")
    assert item["status"] == "backlog" and item["terminal"] is None
    assert all(item[k] is None for k in ("ac_checked", "ac_total", "estimate", "status_text", "next_action_text"))


def test_fenced_headings_and_waived_boxes():
    from _shared.specs_parser import parse_work_facts
    text = """~~~markdown
## Acceptance Criteria
- [x] Not real.
~~~
## Acceptance Criteria
- [ ] Waived item. (waived: replaced)
- [ ] Still open.
````markdown
```
- [x] Example.
````
## Estimate
- elapsed_delivery_h: 1.5—3.5 (median 2)
- remaining_work_h: 1.5—3.5 (median 2) as_of 2026-10-09
- basis: comparable
"""
    item = parse_work_facts(text, "**Next action** — " + "a" * 300, "deprecated")
    assert (item["ac_checked"], item["ac_total"]) == (1, 2)
    assert item["terminal"] == "deprecated"
    assert item["estimate"] == {"p25": 1.5, "p75": 3.5, "median": 2, "provisional": False, "as_of": "2026-10-09"}
    assert len(item["next_action_text"]) == 280


def test_invalid_and_skeleton_estimates_are_null():
    from _shared.specs_parser import parse_work_facts
    for value in ("0–4 (median 2)", "5–3 (median 4)", "1–4 (median 9)", "<p25>–<p75> (median <m>)"):
        assert parse_work_facts("## Estimate\n- remaining_work_h: " + value + " as_of 2026-10-09", "", "analysis")["estimate"] is None


def test_invalid_yaml_is_not_silently_empty():
    import pytest
    import yaml
    from _shared.specs_parser import load_frontmatter
    with pytest.raises(yaml.YAMLError):
        load_frontmatter("---\nid: [broken\n---\n")
    with pytest.raises(ValueError):
        load_frontmatter("---\nid: spec_demo__broken\n")
