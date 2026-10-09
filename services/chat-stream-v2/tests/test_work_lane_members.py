import pytest

from work_lane_members import validate_members, validate_title


def test_order_bounds_unique_and_empty_reason():
    assert validate_members(["spec_demo__b", "spec_demo__a"], None) == (["spec_demo__b", "spec_demo__a"], None)
    assert validate_members([], "  A planning conversation.  ") == ([], "A planning conversation.")
    for members in (["spec_demo__a"] * 2, ["work_demo__a"], ["spec_"], [4], "spec_demo__a",
                    [f"spec_demo__item_{i}" for i in range(33)]):
        with pytest.raises(ValueError, match="work_lane_members_invalid"):
            validate_members(members, None)
    with pytest.raises(ValueError, match="work_lane_no_spec_reason_required"):
        validate_members([], None)
    with pytest.raises(ValueError, match="work_lane_no_spec_reason_invalid"):
        validate_members([], "x" * 281)
    assert validate_members(["spec_demo-kit__bridge"], None)[0] == ["spec_demo-kit__bridge"]
    assert len(validate_members([f"spec_demo__item_{i}" for i in range(32)], None)[0]) == 32


@pytest.mark.parametrize("title", ["Build v2-ab12cd34", "Tracking spec_demo__bridge", "wl-" + "a" * 24,
                                  "Track assistant-lane-ab12", "(spec_demo__bridge)"])
def test_id_like_titles_refused_on_title_writes(title):
    with pytest.raises(ValueError, match="work_lane_title_invalid"):
        validate_title(title)


@pytest.mark.parametrize("title", ["Version 2 release", "Paper bridge", "specification review", "Plan v2-GHIJKLMN"])
def test_narrow_title_matcher_accepts_normal_titles(title):
    assert validate_title(title) == title
