"""Welcome runtime SHA resolves a fallback so a non-git/tarball install does not
advertise an empty SHA ('Daemon unknown')."""

import json
from pathlib import Path

import main


def test_checkout_sha_wins_when_present(tmp_path: Path) -> None:
    stamp = tmp_path / "stamp.json"
    stamp.write_text(json.dumps({"sha": "b" * 40}))
    version = tmp_path / "VERSION"
    version.write_text("v9\n")
    assert main._select_runtime_sha("a" * 40, str(stamp), version) == "a" * 40
    # whitespace-only git output is treated as empty
    assert main._select_runtime_sha("   ", str(stamp), version) == "b" * 40


def test_falls_back_to_deploy_stamp_then_version(tmp_path: Path) -> None:
    stamp = tmp_path / "stamp.json"
    stamp.write_text(json.dumps({"sha": "c" * 40, "service": "chat-streamd-v2"}))
    version = tmp_path / "VERSION"
    version.write_text("v1.2.3-deadbeef\ntrailing\n")
    assert main._select_runtime_sha("", str(stamp), version) == "c" * 40
    # no stamp -> VERSION first line
    assert main._select_runtime_sha("", None, version) == "v1.2.3-deadbeef"


def test_empty_only_when_no_source(tmp_path: Path) -> None:
    assert main._select_runtime_sha("", None, tmp_path / "absent-VERSION") == ""
    assert main._select_runtime_sha("", str(tmp_path / "absent.json"), tmp_path / "absent-VERSION") == ""


def test_deploy_stamp_reader_tolerates_bad_input(tmp_path: Path) -> None:
    assert main._read_deploy_stamp_sha(None) == ""
    assert main._read_deploy_stamp_sha(str(tmp_path / "missing.json")) == ""
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json")
    assert main._read_deploy_stamp_sha(str(corrupt)) == ""
    no_sha = tmp_path / "nosha.json"
    no_sha.write_text(json.dumps({"service": "x"}))
    assert main._read_deploy_stamp_sha(str(no_sha)) == ""


def test_version_reader_handles_missing_and_empty(tmp_path: Path) -> None:
    assert main._read_version_file_sha(tmp_path / "nope") == ""
    empty = tmp_path / "VERSION"
    empty.write_text("\n\n")
    assert main._read_version_file_sha(empty) == ""
