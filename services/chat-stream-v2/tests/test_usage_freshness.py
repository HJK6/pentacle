from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from usage_collector import UsageStateCollector
from usage_state import UsageStateError, UsageStateStore, validate_state


NOW = "2026-09-02T00:00:00Z"


def _claude() -> dict:
    # The shared ``check_usage.py --json`` wire shape: flat
    # ``week_all_*`` / ``week_fable_*`` fields, not a ``claude``/``fable`` object.
    return {
        "session_pct": 5,
        "session_resets": "",
        "week_all_pct": 40,
        "week_all_resets": "Mar 16 at 7pm (America/Chicago)",
        "week_sonnet_pct": None,
        "week_sonnet_resets": "",
        "week_fable_pct": 10,
        "week_fable_resets": "Mar 16 at 7pm (America/Chicago)",
        "last_refreshed": NOW,
    }


def _codex() -> dict:
    return {
        "pct": 42, "resets_text": "Aug 22 at 7pm (America/Chicago)",
        "resets_at_iso": "2026-08-23T00:00:00Z", "upstream_reported_at": NOW,
    }


def _run(command, **_kwargs):
    return subprocess.CompletedProcess(command, 0, json.dumps(_claude() if command[0] == "claude" else _codex()), "")


def _collector(state_path: Path, run=_run, now=NOW) -> UsageStateCollector:
    return UsageStateCollector(
        state_path=state_path, claude_command=("claude",), codex_command=("codex",), run=run, now_fn=lambda: now,
    )


def test_external_collector_writes_validated_v2_lkg_for_both_clis(tmp_path: Path) -> None:
    state_path = tmp_path / "usage_state.json"
    _collector(state_path).run_once()
    state = UsageStateStore(state_path).load()
    assert state.lkg == [
        {
            "id": "claude", "label": "Claude", "pct": 40,
            "resets_text": "Mar 16 at 7pm (America/Chicago)", "resets_at_iso": None,
            "probed_at": None, "upstream_reported_at": None,
        },
        {
            "id": "fable", "label": "Fable", "pct": 10,
            "resets_text": "Mar 16 at 7pm (America/Chicago)", "resets_at_iso": None,
            "probed_at": None, "upstream_reported_at": None,
        },
    ]
    assert state.codex_lkg == {
        "id": "codex", "label": "Codex", "pct": 42, "resets_text": "Aug 22 at 7pm (America/Chicago)",
        "resets_at_iso": "2026-08-23T00:00:00Z", "probed_at": NOW, "upstream_reported_at": NOW,
    }
    assert state.codex_health["outcome"] == "ok"
    assert json.loads(state_path.read_text())["schema_version"] == 2


def test_failed_provider_retains_lkg_and_records_health(tmp_path: Path) -> None:
    state_path = tmp_path / "usage_state.json"
    _collector(state_path).run_once()

    def codex_failure(command, **kwargs):
        if command[0] == "codex":
            return subprocess.CompletedProcess(command, 1, "", "unavailable")
        return _run(command, **kwargs)

    _collector(state_path, codex_failure, "2026-09-02T00:01:00Z").run_once()
    state = UsageStateStore(state_path).load()
    assert state.codex_lkg["pct"] == 42
    assert state.codex_health["outcome"] == "provider_error"


def test_claude_no_update_on_fresh_state_still_persists_codex(tmp_path: Path) -> None:
    # Fresh host: check_usage.py emits {"status": "no_update"} until it has pcts.
    # That is a benign no-update, not a provider error, and must not abort the
    # write — the successful Codex observation still persists.
    state_path = tmp_path / "usage_state.json"

    def run(command, **_kwargs):
        if command[0] == "claude":
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"status": "no_update", "reason": "no pcts"}), ""
            )
        return subprocess.CompletedProcess(command, 0, json.dumps(_codex()), "")

    _collector(state_path, run).run_once()
    state = UsageStateStore(state_path).load()
    assert state_path.exists()
    assert state.lkg is None  # no Claude observation yet
    assert state.health["outcome"] == "never"  # no-update is not an error
    assert state.codex_lkg["pct"] == 42
    assert state.codex_health["outcome"] == "ok"


def test_claude_failure_on_fresh_state_persists_codex_without_aborting(tmp_path: Path) -> None:
    state_path = tmp_path / "usage_state.json"

    def run(command, **_kwargs):
        if command[0] == "claude":
            return subprocess.CompletedProcess(command, 1, "", "boom")
        return subprocess.CompletedProcess(command, 0, json.dumps(_codex()), "")

    _collector(state_path, run).run_once()
    state = UsageStateStore(state_path).load()
    assert state_path.exists()  # a provider failure with no prior LKG still writes
    assert state.lkg is None
    assert state.health["outcome"] == "provider_error"
    assert state.codex_lkg["pct"] == 42


def test_malformed_codex_retains_prior_lkg_and_records_health(tmp_path: Path) -> None:
    # Contract: malformed CLI JSON retains that provider's prior LKG and records
    # validated health, without discarding the other provider's fresh write.
    state_path = tmp_path / "usage_state.json"
    _collector(state_path).run_once()  # seed both providers OK (codex pct=42)

    def run(command, **kwargs):
        if command[0] == "codex":
            bad = {**_codex(), "pct": 42.5}  # non-int pct is invalid for an LKG row
            return subprocess.CompletedProcess(command, 0, json.dumps(bad), "")
        return _run(command, **kwargs)

    _collector(state_path, run, "2026-09-02T00:02:00Z").run_once()
    state = UsageStateStore(state_path).load()
    assert state.codex_lkg["pct"] == 42  # prior Codex LKG retained
    assert state.codex_health["outcome"] == "provider_error"
    assert state.lkg[0]["pct"] == 40  # the fresh Claude observation still persisted


@pytest.mark.parametrize("malformed_provider", ["claude", "codex"])
@pytest.mark.parametrize("malformed_payload", [{"unexpected": True}, {"status": "unexpected"}])
def test_malformed_provider_object_retains_only_its_prior_lkg(
    tmp_path: Path, malformed_provider: str, malformed_payload: dict,
) -> None:
    """Missing required CLI fields are a provider failure, never an ``ok`` row.

    The row constructors intentionally accept nullable values for a legitimate
    upstream observation. The collector must therefore validate the CLI object
    before mapping it, rather than using ``dict.get()`` to fabricate a
    nullable row from an unrelated object.
    """
    state_path = tmp_path / "usage_state.json"
    _collector(state_path).run_once()  # seed Claude/Fable=40/10 and Codex=42

    def run(command, **_kwargs):
        if command[0] == malformed_provider:
            return subprocess.CompletedProcess(command, 0, json.dumps(malformed_payload), "")
        if command[0] == "claude":
            return subprocess.CompletedProcess(
                command, 0, json.dumps({**_claude(), "week_all_pct": 7}), ""
            )
        return subprocess.CompletedProcess(command, 0, json.dumps({**_codex(), "pct": 7}), "")

    _collector(state_path, run, "2026-09-02T00:03:00Z").run_once()
    state = UsageStateStore(state_path).load()
    if malformed_provider == "claude":
        assert state.lkg[0]["pct"] == 40
        assert state.health["outcome"] == "provider_error"
        assert state.codex_lkg["pct"] == 7
        assert state.codex_health["outcome"] == "ok"
    else:
        assert state.lkg[0]["pct"] == 7
        assert state.health["outcome"] == "ok"
        assert state.codex_lkg["pct"] == 42
        assert state.codex_health["outcome"] == "provider_error"


@pytest.mark.parametrize("updates", [
    {"upstream_reported_at": None},
    {"probed_at": None},
    {"upstream_reported_at": "2026-09-03T00:00:00Z"},
])
def test_v2_rejects_unpaired_or_out_of_order_codex_timestamps(tmp_path: Path, updates: dict) -> None:
    path = tmp_path / "usage_state.json"
    _collector(path).run_once()
    state = json.loads(path.read_text())
    state["codex_lkg"].update(updates)
    with pytest.raises(ValueError, match="Codex"):
        validate_state(state)


def test_codex_upstream_microseconds_in_collection_second_are_accepted(tmp_path: Path) -> None:
    path = tmp_path / "usage_state.json"

    def run(command, **_kwargs):
        if command[0] == "codex":
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({**_codex(), "upstream_reported_at": "2026-09-02T00:00:00.410082Z"}),
                "",
            )
        return subprocess.CompletedProcess(command, 0, json.dumps(_claude()), "")

    _collector(path, run).run_once()
    state = UsageStateStore(path).load()
    assert state.codex_lkg["pct"] == 42
    assert state.codex_health["outcome"] == "ok"


def test_codex_validator_failure_records_provider_specific_error_and_message(tmp_path: Path) -> None:
    path = tmp_path / "usage_state.json"

    def run(command, **_kwargs):
        if command[0] == "codex":
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({**_codex(), "upstream_reported_at": "2026-09-02T00:00:01.410082Z"}),
                "",
            )
        return subprocess.CompletedProcess(command, 0, json.dumps(_claude()), "")

    _collector(path, run).run_once()
    state = UsageStateStore(path).load()
    assert state.codex_health["outcome"] == "provider_error"
    assert state.codex_health["error"]["code"] == "codex_usage_provider_error"
    assert "Codex upstream timestamp cannot follow collection" in state.codex_health["error"]["message"]


def test_v2_rejects_unvalidated_extension(tmp_path: Path) -> None:
    path = tmp_path / "usage_state.json"
    _collector(path).run_once()
    state = json.loads(path.read_text())
    state["unexpected"] = True
    with pytest.raises(UsageStateError, match="unexpected v2 keys"):
        validate_state(state)
