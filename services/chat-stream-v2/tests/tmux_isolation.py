"""Test-only tmux namespace shared by daemon fixtures and their callers."""
from pathlib import Path
import os
import shlex
import shutil
import uuid

from tools.gate_owner_manifest import resolve_tmux_socket


def isolate_tmux(tmp_path: Path, monkeypatch) -> tuple[str, Path]:
    real = shutil.which('tmux')
    if real is None:
        raise RuntimeError('tmux is required for daemon fixtures')
    monkeypatch.delenv('TMUX', raising=False)
    monkeypatch.delenv('TMUX_TMPDIR', raising=False)
    bindir = tmp_path / 'isolated-tmux-bin'
    bindir.mkdir(exist_ok=True)
    wrapper = bindir / 'tmux'
    socket = 'pentacle-test-' + uuid.uuid4().hex
    wrapper.write_text('#!/bin/sh\nunset TMUX TMUX_TMPDIR\n'
                       f'exec {shlex.quote(real)} -L {socket} "$@"\n')
    wrapper.chmod(0o700)
    monkeypatch.setenv('PATH', str(bindir) + os.pathsep + os.environ['PATH'])
    return str(wrapper), resolve_tmux_socket(socket)
