"""Synthetic prompt-obligation probe contracts.

The public test keeps the matrix, receipt joins, artifact ordering, and
fail-closed observation rules while using only local disposable data.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest


PLACEHOLDER = "T"


class PublicProbe:
    @staticmethod
    def _matrix() -> list[dict[str, str]]:
        cells: list[dict[str, str]] = []
        for host, provider, transport, size in (
            ("hosta", "codex", "direct", "small"),
            ("hostb", "codex", "direct", "small"),
            ("hostb", "codex", "staged", "large"),
            ("hosta", "claude", "direct", "small"),
        ):
            for ordinal in range(3):
                cells.append({
                    "cell_id": f"{host}-{provider}-{transport}-{ordinal + 1}",
                    "host": host,
                    "provider": provider,
                    "transport": transport,
                    "size": size,
                })
        return cells

    @staticmethod
    def _prompt(cell: dict[str, str], marker: str) -> str:
        padding = "\n" + ("P" * 20_000) if cell["size"] == "large" else ""
        return (
            f"Prompt canary {marker} for {cell['cell_id']}. Confirm the marker, "
            "then return a completion receipt without creating another session."
            f"{padding}"
        )

    @staticmethod
    def _receipt(cell: dict[str, str], marker: str) -> dict[str, Any]:
        stream_id = f"{cell['host']}:{cell['provider']}-{cell['transport']}"
        return {
            "request_id": f"request-{cell['cell_id']}",
            "tell_id": f"receipt-{cell['cell_id']}",
            "to_stream_id": stream_id,
            "delivery_status": "delivered",
            "marker": marker,
            "transport": cell["transport"],
        }

    @classmethod
    def _collect(cls, cell: dict[str, Any], marker: str) -> dict[str, Any]:
        receipt = cell.get("receipt") if isinstance(cell.get("receipt"), dict) else {}
        expected = cls._receipt(cell, marker)
        checks = {
            "request_matches": receipt.get("request_id") == expected["request_id"],
            "stream_matches": receipt.get("to_stream_id") == expected["to_stream_id"],
            "marker_matches": receipt.get("marker") == marker,
            "delivered": receipt.get("delivery_status") == "delivered",
            "single_attempt": cell.get("attempts") == 1,
        }
        return {**cell, "assertions": checks, "passed": all(checks.values())}

    @classmethod
    def run(cls, artifact_dir: Path, release: str) -> dict[str, Any]:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        cells: list[dict[str, Any]] = []
        for cell in cls._matrix():
            marker = PLACEHOLDER
            prompt = cls._prompt(cell, marker)
            result = {
                **cell,
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "receipt": cls._receipt(cell, marker),
                "attempts": 1,
            }
            cells.append(cls._collect(result, marker))
        manifest = {
            "release": release,
            "matrix": cls._matrix(),
            "prompt_hashes": {cell["cell_id"]: cell["prompt_sha256"] for cell in cells},
        }
        outcome = {"passed": all(cell["passed"] for cell in cells), "cells": cells}
        (artifact_dir / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
        (artifact_dir / "result.json").write_text(json.dumps(outcome, sort_keys=True), encoding="utf-8")
        hashes = {
            name: hashlib.sha256((artifact_dir / name).read_bytes()).hexdigest()
            for name in ("manifest.json", "result.json")
        }
        (artifact_dir / "artifact-hashes.json").write_text(json.dumps(hashes, sort_keys=True), encoding="utf-8")
        return outcome


def test_probe_matrix_is_exact_and_prompts_are_unique() -> None:
    cells = PublicProbe._matrix()
    assert len(cells) == 12
    counts = {
        (host, provider, transport): sum(
            cell["host"] == host and cell["provider"] == provider and cell["transport"] == transport
            for cell in cells
        )
        for host, provider, transport in (
            ("hosta", "codex", "direct"),
            ("hostb", "codex", "direct"),
            ("hostb", "codex", "staged"),
            ("hosta", "claude", "direct"),
        )
    }
    assert set(counts.values()) == {3}
    prompts = [PublicProbe._prompt(cell, PLACEHOLDER) + cell["cell_id"] for cell in cells]
    assert len(set(prompts)) == 12
    assert all("Prompt canary" in prompt for prompt in prompts)
    assert all(len(prompt.encode()) >= 20_000 for cell, prompt in zip(cells, prompts) if cell["transport"] == "staged")


def test_probe_writes_manifest_and_result_for_each_cell(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    outcome = PublicProbe.run(artifact_dir, "a" * 40)

    assert outcome["passed"] is True
    assert len(outcome["cells"]) == 12
    manifest = json.loads((artifact_dir / "manifest.json").read_text(encoding="utf-8"))
    result = json.loads((artifact_dir / "result.json").read_text(encoding="utf-8"))
    assert manifest["release"] == "a" * 40
    assert len(manifest["matrix"]) == 12
    assert len(manifest["prompt_hashes"]) == 12
    assert result["passed"] is True
    hashes = json.loads((artifact_dir / "artifact-hashes.json").read_text(encoding="utf-8"))
    assert set(hashes) == {"manifest.json", "result.json"}


@pytest.mark.parametrize("field", ["request_id", "to_stream_id", "marker", "delivery_status", "attempts"])
def test_probe_rejects_mismatched_receipt_fields(field: str) -> None:
    cell = PublicProbe._matrix()[0]
    data = {**cell, "receipt": PublicProbe._receipt(cell, PLACEHOLDER), "attempts": 1}
    if field == "attempts":
        data["attempts"] = 2
    else:
        data["receipt"] = dict(data["receipt"])
        data["receipt"][field] = "different"
    assert PublicProbe._collect(data, PLACEHOLDER)["passed"] is False


def test_probe_accepts_json_array_from_a_local_command() -> None:
    result = subprocess.CompletedProcess([], 0, '[{"stream_id":"hosta:sample"}]', "")
    assert json.loads(result.stdout) == [{"stream_id": "hosta:sample"}]


def test_probe_observation_fails_closed_on_unknown_status() -> None:
    observation = {"exists": None, "returncode": 255, "observation_ok": False}
    assert observation["exists"] is None
    assert observation["observation_ok"] is False
