from __future__ import annotations

import subprocess
from pathlib import Path


def test_removed_send_delivery_shapes_do_not_reappear():
    repo = Path(__file__).resolve().parents[3]
    # Keep guarding genuinely retired send-delivery wording. `send.indeterminate`
    # is now a deliberate idempotency settlement for owner-loss and bounded
    # in-flight waits, so it is no longer part of the retired-shape ban.
    old_terms = [
        "send_" + "unconfirmed",
        "chat_streamd_" + "submit_" + "unconfirmed",
        "likely " + "landed",
    ]
    # The alert contract uses the first token as a typed incident code. It is
    # not a transport delivery shape: the actual send/receipt code remains
    # covered by this ban. Keep the exception local to that accepted enum's
    # definition, family registry, projection and regression; never exempt the
    # other wording.
    alert_enum_files = {
        "docs/contracts/error-alerts-v1.d.ts",
        "docs/contracts/error-alerts-v1.schema.json",
        "services/chat-stream-v2/error_adapters.py",
        "services/chat-stream-v2/error_alerts.py",
        "services/chat-stream-v2/tests/test_error_alerts.py",
    }
    result = subprocess.run(
        [
            "git",
            "grep",
            "-nE",
            "|".join(old_terms),
            "--",
            "services/",
            "main/",
            "docs/",
            "README.md",
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode in (0, 1), result.stderr
    violations = []
    for match in result.stdout.splitlines():
        filename, _, line = match.split(":", 2)
        if filename in alert_enum_files and not any(term in line for term in old_terms[1:]):
            continue
        violations.append(match)
    assert not violations, "\n".join(violations)
