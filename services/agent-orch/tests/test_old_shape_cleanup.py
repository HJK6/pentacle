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
    result = subprocess.run(
        [
            "git",
            "grep",
            "-lE",
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
    assert result.returncode == 1, result.stdout
