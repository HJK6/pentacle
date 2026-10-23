"""B16 reference RED (Wave 0 planner): the native Codex initial prompt must reach the
provider as exact data after an option terminator. Runs the real generated launch
command under /bin/sh against a stub codex that records its argv; no provider starts."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import launch

print("BINDING", launch.__file__, file=sys.stderr)

PROMPTS = {
    "yaml-frontmatter": "---\nid: spec_x\ntitle: t\n---\n# Body\nRead the brief.\n",
    "leading-dash-option": "--help\n",
    "single-dash": "-c model=x",
    "subcommand-word": "exec",
    "ordinary-trailing-newlines": "Read /tmp/brief.txt and follow it exactly.\n\n\n",
}


def _stub(tmp_path: Path) -> tuple[Path, Path]:
    out = tmp_path / "argv.bin"
    stub = tmp_path / "codex-stub"
    stub.write_text(
        "#!/bin/sh\n"
        f"for a in \"$@\"; do printf '%s\\0' \"$a\"; done > '{out}'\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub, out


@pytest.mark.parametrize("prompt", list(PROMPTS.values()), ids=list(PROMPTS))
def test_native_initial_prompt_is_exact_data_after_option_terminator(tmp_path: Path, prompt: str) -> None:
    stub, out = _stub(tmp_path)
    workdir = tmp_path / "wt"
    workdir.mkdir()
    machine = launch.local_machine(
        "test-native-host", cwd=str(tmp_path / "machine-default"), codex_bin=str(stub),
        projects_root=str(tmp_path / "projects"),
    )
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_bytes(prompt.encode("utf-8"))
    cmd = launch._codex_command(
        machine, "sess", str(tmp_path / "tok"), launch_model="gpt-x", launch_effort="high",
        initial_prompt_file=str(prompt_file), launch_cwd=str(workdir),
    )
    subprocess.run(["/bin/sh", "-c", cmd], check=True, timeout=20, env={**os.environ, "PATH": "/usr/bin:/bin"})
    argv = out.read_bytes().split(b"\0")[:-1]
    assert argv[-2:] == [b"--", prompt.encode("utf-8")]
    assert argv.count(b"--") == 1
    # Normal launch tuple preserved ahead of the terminator.
    assert argv[argv.index(b"-m") + 1] == b"gpt-x"
    assert b"model_reasoning_effort=high" in argv[: argv.index(b"--")]


def test_codex_command_without_initial_prompt_has_no_terminator(tmp_path: Path) -> None:
    stub, _out = _stub(tmp_path)
    machine = launch.local_machine(
        "test-native-host", cwd=str(tmp_path / "machine-default"), codex_bin=str(stub),
        projects_root=str(tmp_path / "projects"),
    )
    cmd = launch._codex_command(machine, "sess", str(tmp_path / "tok"), launch_model=None, launch_effort=None)
    assert " -- " not in cmd and launch.NATIVE_INITIAL_PROMPT_LAUNCHER not in cmd
