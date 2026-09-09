from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_orch import cli, triage


def _write_item(
    root: Path,
    name: str,
    *,
    spec_id: str,
    title: str,
    tags: list[str] | None = None,
    status: str = "backlog",
) -> Path:
    folder = root / "work" / status / name
    folder.mkdir(parents=True, exist_ok=True)
    tag_lines = "\n".join(f"- {tag}" for tag in (tags or []))
    spec = folder / "spec.md"
    summary = folder / "summary.md"
    spec.write_text(
        f"""---
id: {spec_id}
title: {title}
type: spec
status: {status}
canonical: false
created_at: '2026-07-02'
updated_at: '2026-07-02'
source_path: work/{status}/{name}/spec.md
machine: example
owner: agents
tags:
{tag_lines}
summary: Test spec.
related: []
---

## Status

Draft.
""",
        encoding="utf-8",
    )
    summary.write_text(
        f"""---
id: work_{name}_backlog
title: {title}
type: work
status: {status}
canonical: false
created_at: '2026-07-02'
updated_at: '2026-07-02'
source_path: work/{status}/{name}/summary.md
machine: example
owner: agents
tags:
{tag_lines}
summary: Test summary.
related: []
---

**Goal** - test.
""",
        encoding="utf-8",
    )
    return folder


def test_discover_creates_state_and_notification_payload(tmp_path):
    _write_item(
        tmp_path,
        "example__followup",
        spec_id="spec_example__followup",
        title="Fix followup",
        tags=["triage", "pentacle"],
    )

    item = triage.discover_items(tmp_path)[0]
    state = triage.ensure_state(item, now="2026-07-02T00:00:00Z")
    payload = triage.notification_payload(item, state, candidates=[])

    assert state["status"] == "pending"
    assert state["notification_dedup_key"].startswith("triage:spec_example__followup:")
    assert payload["dedup_key"] == state["notification_dedup_key"]
    assert [action["value"]["action"] for action in payload["actions"]] == ["keep", "defer", "deprecate"]
    assert {action["value"]["spec_id"] for action in payload["actions"]} == {"spec_example__followup"}


def test_notification_payload_offers_non_triage_merge_candidates(tmp_path):
    _write_item(
        tmp_path,
        "example__followup",
        spec_id="spec_example__followup",
        title="Fix heartbeat followup",
        tags=["triage", "pentacle"],
    )
    _write_item(
        tmp_path,
        "example__heartbeat",
        spec_id="spec_example__heartbeat",
        title="Fix heartbeat runner",
        tags=["pentacle"],
    )

    item = triage.discover_items(tmp_path)[0]
    state = triage.ensure_state(item, now="2026-07-02T00:00:00Z")
    payload = triage.notification_payload(item, state)

    assert any(
        action.get("value", {}).get("action") == "merge"
        and action.get("value", {}).get("target_spec_id") == "spec_example__heartbeat"
        for action in payload["actions"]
    )


def test_notification_payload_excludes_blocked_and_needs_qa_merge_candidates(tmp_path):
    _write_item(
        tmp_path,
        "example__followup",
        spec_id="spec_example__followup",
        title="Fix heartbeat followup",
        tags=["triage", "pentacle"],
    )
    _write_item(
        tmp_path,
        "example__blocked",
        spec_id="spec_example__blocked",
        title="Fix heartbeat blocked",
        tags=["pentacle"],
        status="blocked",
    )
    _write_item(
        tmp_path,
        "example__needs_qa",
        spec_id="spec_example__needs_qa",
        title="Fix heartbeat needs qa",
        tags=["pentacle"],
        status="needs_qa",
    )

    item = triage.discover_items(tmp_path)[0]
    state = triage.ensure_state(item, now="2026-07-02T00:00:00Z")
    payload = triage.notification_payload(item, state)
    target_ids = {
        action.get("value", {}).get("target_spec_id")
        for action in payload["actions"]
        if action.get("value", {}).get("action") == "merge"
    }

    assert "spec_example__blocked" not in target_ids
    assert "spec_example__needs_qa" not in target_ids


def test_scan_publish_dry_run_does_not_create_sidecar(tmp_path, capsys):
    folder = _write_item(
        tmp_path,
        "example__followup",
        spec_id="spec_example__followup",
        title="Fix followup",
        tags=["triage", "pentacle"],
    )

    args = cli.build_parser().parse_args(
        ["triage", "scan-publish", "--memory-root", str(tmp_path), "--dry-run"]
    )

    assert args.func(args) == 0
    assert not (folder / "triage.json").exists()
    assert json.loads(capsys.readouterr().out)["published"][0]["spec_id"] == "spec_example__followup"


def test_scan_publish_routes_operator_answers(monkeypatch, tmp_path, capsys):
    _write_item(
        tmp_path,
        "example__followup",
        spec_id="spec_example__followup",
        title="Fix followup",
        tags=["triage", "pentacle"],
    )
    created_payloads = []

    async def fake_create(_config, payload, timeout):
        created_payloads.append((payload, timeout))
        return {"type": "notification.create.ok", "notification": {"notification_id": "notif-1"}}

    monkeypatch.setattr(cli, "load_config", lambda: object())
    monkeypatch.setattr(cli, "notification_create_once", fake_create)
    args = cli.build_parser().parse_args(
        [
            "triage",
            "scan-publish",
            "--memory-root",
            str(tmp_path),
            "--answer-to-stream-id",
            "hostb:codex-triage",
        ]
    )

    assert args.func(args) == 0
    payload, timeout = created_payloads[0]
    assert timeout == 30.0
    assert payload["answer_to_stream_id"] == "hostb:codex-triage"
    assert payload["actions"][0]["value"] == {
        "consumer": "triage",
        "spec_id": "spec_example__followup",
        "action": "keep",
    }
    assert json.loads(capsys.readouterr().out)["published"][0]["response"]["type"] == "notification.create.ok"


def test_scan_publish_limit_caps_published_notifications(monkeypatch, tmp_path, capsys):
    for index in range(3):
        _write_item(
            tmp_path,
            f"example__followup_{index}",
            spec_id=f"spec_example__followup_{index}",
            title=f"Fix followup {index}",
            tags=["triage", "pentacle"],
        )
    created_payloads = []

    async def fake_create(_config, payload, timeout):
        created_payloads.append((payload, timeout))
        return {"type": "notification.create.ok", "notification": {"notification_id": f"notif-{len(created_payloads)}"}}

    monkeypatch.setattr(cli, "load_config", lambda: object())
    monkeypatch.setattr(cli, "notification_create_once", fake_create)
    args = cli.build_parser().parse_args(
        [
            "triage",
            "scan-publish",
            "--memory-root",
            str(tmp_path),
            "--limit",
            "2",
            "--answer-to-stream-id",
            "hostb:codex-triage",
        ]
    )

    assert args.func(args) == 0
    published = json.loads(capsys.readouterr().out)["published"]
    assert len(published) == 2
    assert len(created_payloads) == 2


def test_keep_is_idempotent_and_removes_triage_tag(tmp_path):
    folder = _write_item(
        tmp_path,
        "example__followup",
        spec_id="spec_example__followup",
        title="Fix followup",
        tags=["triage", "pentacle"],
    )

    first = triage.apply_action(
        tmp_path,
        spec_id="spec_example__followup",
        action_value="keep",
        decided_by="operator",
        now="2026-07-02T00:00:00Z",
    )
    second = triage.apply_action(
        tmp_path,
        spec_id="spec_example__followup",
        action_value="keep",
        decided_by="operator",
        now="2026-07-02T00:01:00Z",
    )

    assert first == second
    spec_text = (folder / "spec.md").read_text(encoding="utf-8")
    assert "- triage" not in spec_text
    assert "## Triage" in spec_text


def test_apply_answer_json_infers_spec_id_from_routed_notification_value(tmp_path, capsys):
    folder = _write_item(
        tmp_path,
        "example__followup",
        spec_id="spec_example__followup",
        title="Fix followup",
        tags=["triage", "pentacle"],
    )
    answer = {
        "value": {
            "consumer": "triage",
            "spec_id": "spec_example__followup",
            "action": "keep",
        }
    }
    args = cli.build_parser().parse_args(
        ["triage", "apply-answer", "--memory-root", str(tmp_path), "--answer-json", json.dumps(answer)]
    )

    assert args.func(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "kept"
    assert "- triage" not in (folder / "spec.md").read_text(encoding="utf-8")


def test_defer_action_uses_future_timestamp_and_does_not_republish_immediately(tmp_path):
    folder = _write_item(
        tmp_path,
        "example__followup",
        spec_id="spec_example__followup",
        title="Fix followup",
        tags=["triage", "pentacle"],
    )

    state = triage.apply_action(
        tmp_path,
        spec_id="spec_example__followup",
        action_value="defer",
        decided_by="operator",
        reason="later",
        now="2026-07-02T00:00:00Z",
    )

    assert state["status"] == "deferred"
    assert state["reason"] == "later"
    assert state["defer_until"] == "2026-07-03T00:00:00Z"
    item = triage.discover_items(tmp_path)[0]
    assert triage.item_is_publishable(item, state, now="2026-07-02T01:00:00Z") is False
    assert json.loads((folder / "triage.json").read_text(encoding="utf-8"))["defer_until"] == "2026-07-03T00:00:00Z"


def test_defer_action_accepts_explicit_future_timestamp(tmp_path):
    _write_item(
        tmp_path,
        "example__followup",
        spec_id="spec_example__followup",
        title="Fix followup",
        tags=["triage", "pentacle"],
    )

    state = triage.apply_action(
        tmp_path,
        spec_id="spec_example__followup",
        action_value="defer",
        decided_by="operator",
        defer_until="2026-07-04T12:00:00Z",
        now="2026-07-02T00:00:00Z",
    )

    assert state["defer_until"] == "2026-07-04T12:00:00Z"


def test_malformed_defer_until_does_not_crash_publishability(tmp_path):
    _write_item(
        tmp_path,
        "example__followup",
        spec_id="spec_example__followup",
        title="Fix followup",
        tags=["triage", "pentacle"],
    )
    item = triage.discover_items(tmp_path)[0]
    state = triage.ensure_state(item)
    state.update({"status": "deferred", "defer_until": "ask me later"})

    assert triage.item_is_publishable(item, state, now="2026-07-02T01:00:00Z") is False


def test_deprecate_moves_folder_and_updates_status(tmp_path):
    _write_item(
        tmp_path,
        "example__followup",
        spec_id="spec_example__followup",
        title="Fix followup",
        tags=["triage", "pentacle"],
    )

    state = triage.apply_action(
        tmp_path,
        spec_id="spec_example__followup",
        action_value="deprecate",
        decided_by="operator",
        reason="not needed",
        now="2026-07-02T00:00:00Z",
    )

    target = tmp_path / "work" / "deprecated" / "example__followup"
    assert state["status"] == "deprecated"
    assert target.exists()
    assert "status: deprecated" in (target / "spec.md").read_text(encoding="utf-8")
    assert json.loads((target / "triage.json").read_text(encoding="utf-8"))["reason"] == "not needed"


def test_duplicate_deprecate_answer_after_move_returns_terminal_state(tmp_path):
    _write_item(
        tmp_path,
        "example__followup",
        spec_id="spec_example__followup",
        title="Fix followup",
        tags=["triage", "pentacle"],
    )

    first = triage.apply_action(
        tmp_path,
        spec_id="spec_example__followup",
        action_value="deprecate",
        decided_by="operator",
        reason="not needed",
        now="2026-07-02T00:00:00Z",
    )
    second = triage.apply_action(
        tmp_path,
        spec_id="spec_example__followup",
        action_value="deprecate",
        decided_by="operator",
        reason="not needed",
        now="2026-07-02T00:01:00Z",
    )

    assert second == first


def test_merge_updates_target_and_deprecates_source(tmp_path):
    _write_item(
        tmp_path,
        "example__followup",
        spec_id="spec_example__followup",
        title="Fix followup",
        tags=["triage", "pentacle"],
    )
    target = _write_item(
        tmp_path,
        "example__target",
        spec_id="spec_example__target",
        title="Fix target",
        tags=["pentacle"],
    )

    state = triage.apply_action(
        tmp_path,
        spec_id="spec_example__followup",
        action_value="merge:spec_example__target",
        decided_by="operator",
        now="2026-07-02T00:00:00Z",
    )

    assert state["status"] == "merged"
    assert state["target_spec_id"] == "spec_example__target"
    assert "## Triage Merges" in (target / "spec.md").read_text(encoding="utf-8")
    assert (tmp_path / "work" / "deprecated" / "example__followup").exists()


def test_merge_rejects_blocked_target(tmp_path):
    _write_item(
        tmp_path,
        "example__followup",
        spec_id="spec_example__followup",
        title="Fix followup",
        tags=["triage", "pentacle"],
    )
    _write_item(
        tmp_path,
        "example__blocked",
        spec_id="spec_example__blocked",
        title="Fix blocked",
        tags=["pentacle"],
        status="blocked",
    )

    with pytest.raises(triage.TriageError, match="target spec not found or terminal"):
        triage.apply_action(
            tmp_path,
            spec_id="spec_example__followup",
            action_value="merge:spec_example__blocked",
            decided_by="operator",
            now="2026-07-02T00:00:00Z",
        )


def test_duplicate_merge_answer_after_move_returns_terminal_state(tmp_path):
    _write_item(
        tmp_path,
        "example__followup",
        spec_id="spec_example__followup",
        title="Fix followup",
        tags=["triage", "pentacle"],
    )
    target = _write_item(
        tmp_path,
        "example__target",
        spec_id="spec_example__target",
        title="Fix target",
        tags=["pentacle"],
    )

    first = triage.apply_action(
        tmp_path,
        spec_id="spec_example__followup",
        action_value="merge:spec_example__target",
        decided_by="operator",
        now="2026-07-02T00:00:00Z",
    )
    second = triage.apply_action(
        tmp_path,
        spec_id="spec_example__followup",
        action_value="merge:spec_example__target",
        decided_by="operator",
        now="2026-07-02T00:01:00Z",
    )

    assert second == first
    assert (target / "spec.md").read_text(encoding="utf-8").count("## Triage Merges") == 1

