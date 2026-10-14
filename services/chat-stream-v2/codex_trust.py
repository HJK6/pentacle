"""Seed Codex's persisted cwd trust without rewriting its other configuration."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import tomllib


def ensure_codex_trust(cwd: str, *, code_home: str | None = None) -> None:
    key = os.path.realpath(cwd)
    home = Path(code_home or os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    home.mkdir(parents=True, exist_ok=True)
    config = home / 'config.toml'
    # A stable sidecar also protects absent configs. Hold the config inode lock
    # as well, so cooperating writers cannot race the read/append sequence.
    with (home / 'config.toml.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        fd = os.open(config, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'r+b') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            raw = handle.read()
            text = raw.decode('utf-8')
            projects = tomllib.loads(text).get('projects', {})
            existing = projects.get(key)
            if isinstance(existing, dict) and existing.get('trust_level') == 'trusted':
                return
            headers = list(re.finditer(r'^[ \t]*\[(?!\[)[^\n]+\][ \t]*(?:#[^\n]*)?$', text, re.M))
            section = None
            for index, header in enumerate(headers):
                try:
                    table = tomllib.loads(header.group() + '\n__probe = true\n')
                except tomllib.TOMLDecodeError:
                    continue
                if table.get('projects', {}).get(key, {}).get('__probe') is True:
                    end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
                    section = (header.end(), end)
                    break
            if section is None:
                if existing is not None:
                    raise ValueError('Codex project trust table has an unsupported inline or dotted form')
                updated = text + ('\n' if text and not text.endswith('\n') else '')
                updated += '\n[projects.' + json.dumps(key, ensure_ascii=False) + ']\ntrust_level = "trusted"\n'
            else:
                start, end = section
                body = text[start:end]
                assignment = re.search(r'(?m)^([ \t]*trust_level[ \t]*=[ \t]*)("[^"\n]*"|\x27[^\x27\n]*\x27)', body)
                if assignment:
                    body = body[:assignment.start(2)] + '"trusted"' + body[assignment.end(2):]
                else:
                    body = '\ntrust_level = "trusted"\n' + body
                updated = text[:start] + body + text[end:]
            # Validate before any write. Existing unrelated table bytes survive.
            assert tomllib.loads(updated)['projects'][key]['trust_level'] == 'trusted'
            if updated.startswith(text):
                handle.seek(0, os.SEEK_END)
                handle.write(updated[len(text):].encode('utf-8'))
            else:
                handle.seek(0)
                handle.write(updated.encode('utf-8'))
                handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())


if __name__ == '__main__':
    import sys
    ensure_codex_trust(sys.argv[1])
