"""Pure contract checks for the transcript-backed replay harness."""

from __future__ import annotations

import asyncio
import json

import pytest

from router_replay import (  # noqa: E402
    HARNESS_SCHEMA,
    HarnessError,
    PARSER_VERSION,
    REDACTOR,
    REDACTOR_VERSION,
    build_manifest,
    generate_cases,
    load_manifest,
    merge_misses,
    materialize_cases,
    run_cases,
    sha256_value,
    write_json,
    write_jsonl,
)


_ENV_SECRET_CASES = (
    ("AWS_SECRET_ACCESS_KEY", "aws-env-router-replay-secret"),
    ("OPENAI_API_KEY", "openai-env-router-replay-secret"),
    ("ANTHROPIC_API_KEY", "anthropic-env-router-replay-secret"),
    ("GITHUB_TOKEN", "github-env-router-replay-secret"),
)

_STRUCTURED_SECRET_CASES = (
    ("AWS_SECRET_ACCESS_KEY", "aws-quoted-structured-sentinel-123456789"),
    ("OPENAI_API_KEY", "openai-quoted-structured-sentinel-123456789"),
    ("ANTHROPIC_API_KEY", "anthropic-quoted-structured-sentinel-123456789"),
    ("GITHUB_TOKEN", "github-quoted-structured-sentinel-123456789"),
)


@pytest.mark.parametrize("key,secret", _ENV_SECRET_CASES)
@pytest.mark.parametrize(
    "assignment",
    ("{key}={secret}", "{key}: {secret}", "export {key}={secret}"),
)
def test_redaction_covers_env_style_token_family_assignments(
    key: str, secret: str, assignment: str,
) -> None:
    cleaned = REDACTOR.redact_text(assignment.format(key=key, secret=secret))
    assert secret not in cleaned


@pytest.mark.parametrize("key,secret", _STRUCTURED_SECRET_CASES)
@pytest.mark.parametrize("prefix", ("{key}=", "export {key}=", '"{key}": ', "{key}: "))
@pytest.mark.parametrize("quote", ("", "'", '"', "`"))
def test_redaction_covers_quoted_shell_json_yaml_assignments(
    key: str, secret: str, prefix: str, quote: str,
) -> None:
    assignment = f"{prefix.format(key=key)}{quote}{secret}{quote}"
    cleaned = REDACTOR.redact_text(assignment)
    assert secret not in cleaned


def test_redaction_canary_assertion_is_fail_closed_for_known_seed() -> None:
    planted = "aws-env-router-replay-secret"
    with pytest.raises(HarnessError, match="redaction_canary_leaked"):
        REDACTOR.assert_canary_absent({"sink_output": planted}, (planted,))


def _assert_secrets_absent(value, secrets: tuple[str, ...]) -> None:
    rendered = json.dumps(value, sort_keys=True, ensure_ascii=False)
    for secret in secrets:
        assert secret not in rendered


def test_redaction_is_sink_wide_for_transcript_router_and_artifacts(tmp_path) -> None:
    secrets = tuple(secret for _key, secret in (*_ENV_SECRET_CASES, *_STRUCTURED_SECRET_CASES))
    assignments = "\n".join(
        style.format(key=key, secret=secret)
        for key, secret in _ENV_SECRET_CASES
        for style in ("{key}={secret}", "{key}: {secret}", "export {key}={secret}")
    )
    assignments += "\n" + "\n".join(
        f"{prefix.format(key=key)}{quote}{secret}{quote}"
        for key, secret in _STRUCTURED_SECRET_CASES
        for prefix in ("{key}=", "export {key}=", '"{key}": ', "{key}: ")
        for quote in ("'", '"', "`")
    )
    root = tmp_path / "projects"
    root.mkdir()
    for index in range(2):
        content = assignments if index == 0 else f"Ordinary transcript turn {index}."
        records = [{
            "type": "user", "uuid": f"user-{index}",
            "timestamp": "2026-09-21T00:00:00Z",
            "message": {"role": "user", "content": content},
        }]
        (root / f"session-{index}.jsonl").write_text(
            "\n".join(json.dumps(record) for record in records), encoding="utf-8",
        )

    manifest = build_manifest(root, seed=17, threads=2)
    _assert_secrets_absent(manifest, secrets)
    normalized_turns = [
        turn
        for session in manifest["sessions"]
        for turn in session["turns"]
    ]
    _assert_secrets_absent(normalized_turns, secrets)

    source_fixture = tmp_path / "source-cases.json"
    write_json(source_fixture, [{
        "name": "seeded-fixture",
        "input": assignments,
        "expected": {
            "schema_version": "assistant-router/v1", "disposition": "conversation",
            "lane_id": None, "depends_on_message_id": None, "reason": "fixture",
        },
    }])
    cases = asyncio.run(materialize_cases(generate_cases(manifest, source_fixture)))
    _assert_secrets_absent(cases, secrets)
    _assert_secrets_absent([case["router_input"] for case in cases], secrets)

    async def classifier(_payload):
        return {
            "schema_version": "assistant-router/v1", "disposition": "new_topic",
            "lane_id": None, "depends_on_message_id": None, "reason": "forced miss",
        }

    result = asyncio.run(run_cases(
        cases,
        classifier,
        harness_config={"harness": "sink-canary", "diagnostics": assignments},
    ))
    _assert_secrets_absent(result.report, secrets)
    _assert_secrets_absent(result.misses, secrets)

    report_path = tmp_path / "report.json"
    misses_path = tmp_path / "misses.jsonl"
    write_json(report_path, result.report)
    write_jsonl(misses_path, result.misses)
    _assert_secrets_absent(json.loads(report_path.read_text(encoding="utf-8")), secrets)
    _assert_secrets_absent(misses_path.read_text(encoding="utf-8"), secrets)

    raw_misses_path = tmp_path / "raw-misses.jsonl"
    raw_misses_path.write_text(json.dumps({
        "case_key": "seeded-fixture-merge",
        "router_input": {"body_excerpt": f"AWS_SECRET_ACCESS_KEY={secrets[0]}"},
        "expected": {"reason": secrets[1]},
    }) + "\n", encoding="utf-8")
    merged_fixture = tmp_path / "merged-fixture.json"
    merged_fixture.write_text("[]", encoding="utf-8")
    merge_misses(raw_misses_path, merged_fixture)
    _assert_secrets_absent(json.loads(merged_fixture.read_text(encoding="utf-8")), secrets)


def test_ordered_redaction_strips_canaries_after_pasted_wrapper() -> None:
    text = (
        '<pasted_content id="secret">'
        "AKIAIOSFODNN7EXAMPLE password=router-replay-secret "
        "Bearer router-replay-secret /Users/example/private/router-replay-secret"
        "</pasted_content id="
        '"secret">'
    )
    cleaned = REDACTOR.redact_text(text)
    for canary in (
        "AKIAIOSFODNN7EXAMPLE", "router-replay-secret",
        "/Users/example/private/router-replay-secret",
    ):
        assert canary not in cleaned


def test_manifest_selects_sessions_and_freezes_corpus_digest(tmp_path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    for index in range(3):
        records = [
            {
                "type": "user", "uuid": f"user-{index}",
                "timestamp": "2026-09-21T00:00:00Z",
                "message": {"role": "user", "content": f"Review project {index}."},
            },
        ]
        (root / f"session-{index}.jsonl").write_text(
            "\n".join(json.dumps(record) for record in records), encoding="utf-8",
        )
    manifest = build_manifest(root, seed=17, threads=3)
    assert manifest["schema_version"] == HARNESS_SCHEMA
    assert len(manifest["sessions"]) == 3
    assert manifest["parser_version"] == PARSER_VERSION
    assert manifest["redactor_version"] == REDACTOR_VERSION
    assert manifest["manifest_sha256"] == sha256_value(
        {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    )
    path = tmp_path / "transcript-manifest.json"
    write_json(path, manifest)
    assert load_manifest(path)["corpus_digest"] == manifest["corpus_digest"]


def test_case_oracle_drift_is_harness_error(tmp_path) -> None:
    fixture = tmp_path / "cases.json"
    fixture.write_text(json.dumps([{
        "name": "conversation", "input": "hello",
        "expected": {
            "schema_version": "assistant-router/v1", "disposition": "conversation",
            "lane_id": None, "depends_on_message_id": None, "reason": "chat",
        },
    }]), encoding="utf-8")
    manifest = {
        "schema_version": HARNESS_SCHEMA, "seed": 1, "thread_count": 2,
        "parser_version": PARSER_VERSION, "redactor_version": REDACTOR_VERSION,
        "generator": {}, "sessions": [], "corpus_digest": sha256_value([]),
    }
    cases = asyncio.run(materialize_cases(generate_cases(manifest, fixture)))
    baseline = {"harness_config_sha256": "same", "cases": []}

    async def classifier(_payload):
        return cases[0]["expected"]

    result = asyncio.run(run_cases(
        cases[:1], classifier, harness_config={"harness": "same"}, baseline_report=baseline,
    ))
    assert result.report["status"] == "HARNESS_ERROR"
    assert "harness_config_mismatch" in result.report["error_classes"]
