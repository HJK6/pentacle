"""Bounded, zero-model-request Cursor admission. Called off the event loop."""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import subprocess
import time
import uuid


def _create_chat(agent: str, cwd: str, timeout: float = 20) -> str:
    master, slave = pty.openpty()
    child = None
    try:
        child = subprocess.Popen([agent, 'create-chat'], cwd=cwd, stdin=slave, stdout=slave,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        os.close(slave)
        slave = -1
        output = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if select.select([master], [], [], min(.1, max(0, deadline-time.monotonic())))[0]:
                try:
                    part = os.read(master, 4096)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    part = b''
                if not part:
                    break
                output.extend(part)
                if len(output) > 65536:
                    raise ValueError('cursor_chat_create_failed')
            if child.poll() is not None:
                # Drain any final UUID bytes before inspecting the exit status.
                if not select.select([master], [], [], 0)[0]:
                    break
        remaining = deadline - time.monotonic()
        if remaining <= 0 or child.wait(timeout=max(.01, remaining)) != 0:
            raise ValueError('cursor_chat_create_failed')
        value = output.decode('utf-8', 'replace').strip()
        if str(uuid.UUID(value)) != value:
            raise ValueError('cursor_chat_create_failed')
        return value
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise ValueError('cursor_chat_create_failed') from None
    finally:
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
        os.close(master)
        if slave != -1:
            os.close(slave)


def prepare_cursor(machine, *, cwd: str | None = None) -> dict:
    try:
        target = Path(cwd or machine.cwd).resolve(strict=True)
        if not target.is_dir() or not os.access(target, os.W_OK):
            raise OSError()
        for part in (target / '.cursor', target / '.cursor/sessions'):
            if part.is_symlink() or (part.exists() and not part.is_dir()):
                raise OSError()
    except (OSError, RuntimeError):
        raise ValueError('cursor_cwd_invalid') from None
    agent = shutil.which(machine.cursor_agent_bin)
    if not agent:
        raise ValueError('provider_executable_missing')
    agent = str(Path(agent).resolve())
    try:
        result = subprocess.run([agent, '--version'], capture_output=True, timeout=15, text=True)
        if result.returncode != 0 or result.stdout.strip() != machine.cursor_version:
            raise ValueError('cursor_version_mismatch')
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError('cursor_version_mismatch') from None
    try:
        result = subprocess.run([agent, 'status', '--format', 'json'], capture_output=True,
                                timeout=15, text=True)
        status = json.loads(result.stdout)
        if result.returncode != 0 or not isinstance(status, dict) or type(status.get('isAuthenticated')) is not bool:
            raise ValueError()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise ValueError('cursor_status_unreadable') from None
    if status['isAuthenticated'] is not True:
        raise ValueError('cursor_not_authenticated')
    return {'agent': agent, 'cwd': str(target), 'cli_version': machine.cursor_version,
            'chat_id': _create_chat(agent, str(target))}
