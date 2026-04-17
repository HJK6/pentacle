#!/usr/bin/env python3
"""
Pentacle reference API server — stdlib only, no external deps.

Implements the session management endpoints that Pentacle's renderer calls.
State backend: tmux (live sessions) + local JSON files for trash/agent-id mapping.

State files (Linux/macOS):
  ~/.local/state/pentacle/agents.json
  ~/.local/state/pentacle/trash.json

State files (Windows):
  %LOCALAPPDATA%/Pentacle/agents.json
  %LOCALAPPDATA%/Pentacle/trash.json

Usage:
  python3 server.py [--port 7777] [--host 127.0.0.1]
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HIDDEN_SESSIONS = {'cmdcenter', 'quad-view'}
_env_prefixes = os.environ.get('PENTACLE_HIDDEN_PREFIXES', '')
HIDDEN_PREFIXES = [p for p in _env_prefixes.split(',') if p]

COMMON_PREFIXES = ('claude-', 'codex-')
GENERIC_SHELL_NAMES = {'bash', 'zsh', 'sh', 'fish', 'python', 'python3'}

# ---------------------------------------------------------------------------
# State directory
# ---------------------------------------------------------------------------

def _state_dir() -> Path:
    if platform.system() == 'Windows':
        base = os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local')
        d = Path(base) / 'Pentacle'
    else:
        xdg = os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local' / 'state'))
        d = Path(xdg) / 'pentacle'
    d.mkdir(parents=True, exist_ok=True)
    return d

STATE_DIR = _state_dir()
AGENTS_FILE = STATE_DIR / 'agents.json'
TRASH_FILE = STATE_DIR / 'trash.json'

# ---------------------------------------------------------------------------
# tmux binary discovery
# ---------------------------------------------------------------------------

def _find_tmux() -> str | None:
    # 1. TMUX env var contains the socket path, not the binary — skip it.
    # 2. PATH lookup
    found = shutil.which('tmux')
    if found:
        return found
    # 3. Platform fallbacks
    for p in ('/opt/homebrew/bin/tmux', '/usr/local/bin/tmux', '/usr/bin/tmux'):
        if os.path.isfile(p):
            return p
    return None

TMUX = _find_tmux()

# ---------------------------------------------------------------------------
# JSON helpers (atomic-ish write via temp + rename)
# ---------------------------------------------------------------------------

def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default

def _save_json(path: Path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)

# ---------------------------------------------------------------------------
# Agent-id mapping (session name ↔ UUID)
# ---------------------------------------------------------------------------

def _load_agents() -> dict:
    raw = _load_json(AGENTS_FILE, {'agents': []})
    return {e['tmux_session']: e['agent_id'] for e in raw.get('agents', []) if 'tmux_session' in e and 'agent_id' in e}

def _save_agents(mapping: dict):
    _save_json(AGENTS_FILE, {'agents': [{'agent_id': v, 'tmux_session': k} for k, v in mapping.items()]})

def _get_or_mint_id(name: str, mapping: dict) -> str:
    if name not in mapping:
        mapping[name] = str(uuid.uuid4())
        _save_agents(mapping)
    return mapping[name]

# ---------------------------------------------------------------------------
# Trash state
# ---------------------------------------------------------------------------

def _load_trash() -> list:
    return _load_json(TRASH_FILE, {'trashed': []}).get('trashed', [])

def _save_trash(entries: list):
    _save_json(TRASH_FILE, {'trashed': entries})

# ---------------------------------------------------------------------------
# tmux interaction
# ---------------------------------------------------------------------------

def _tmux(*args) -> str:
    result = subprocess.run(
        [TMUX] + list(args),
        capture_output=True, text=True, timeout=5
    )
    return result.stdout

def _tmux_sessions() -> list[dict]:
    """Return list of {name, attached, created, windows, last_activity} from tmux."""
    if not TMUX:
        return []
    fmt = '#{session_name}|#{session_attached}|#{session_created}|#{session_windows}|#{session_activity}'
    try:
        raw = _tmux('list-sessions', '-F', fmt)
    except Exception:
        return []
    sessions = []
    for line in raw.splitlines():
        parts = line.split('|')
        if len(parts) < 4:
            continue
        sessions.append({
            'name': parts[0],
            'attached': parts[1] == '1',
            'created': int(parts[2]) if parts[2].isdigit() else 0,
            'windows': int(parts[3]) if parts[3].isdigit() else 1,
            'last_activity': int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else int(time.time()),
        })
    return sessions

def _tmux_window_name(session: str) -> str:
    """Return the first window name for a session."""
    try:
        raw = _tmux('list-windows', '-t', session, '-F', '#{window_name}')
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        return lines[0] if lines else ''
    except Exception:
        return ''

def _tmux_pane_preview(session: str) -> str:
    """Capture last non-blank line from the first pane."""
    try:
        raw = _tmux('capture-pane', '-t', session, '-p')
        lines = [l.rstrip() for l in raw.splitlines() if l.strip()]
        return lines[-1] if lines else ''
    except Exception:
        return ''

# ---------------------------------------------------------------------------
# Title cleaning
# ---------------------------------------------------------------------------

def _clean_title(name: str, window_name: str) -> str:
    """
    Produce a human-readable title from session name + window name.
    Chain:
      1. Strip common prefixes, convert hyphens to spaces.
      2. Use window name if it's different and not a generic shell name.
      3. Fall back to raw session name.
    """
    cleaned = name
    for prefix in COMMON_PREFIXES:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break
    # Strip trailing numeric suffix like -1234567890
    cleaned = re.sub(r'-\d{6,}$', '', cleaned)
    cleaned = cleaned.replace('-', ' ').strip()

    if window_name and window_name.lower() not in GENERIC_SHELL_NAMES and window_name != name:
        return window_name

    return cleaned if cleaned else name

# ---------------------------------------------------------------------------
# Session building
# ---------------------------------------------------------------------------

def _is_hidden(name: str) -> bool:
    if name in HIDDEN_SESSIONS:
        return True
    for prefix in HIDDEN_PREFIXES:
        if name.startswith(prefix):
            return True
    return False

def _build_active(tmux_sessions: list, mapping: dict, trashed_names: set) -> list:
    result = []
    for s in tmux_sessions:
        name = s['name']
        if _is_hidden(name):
            continue
        if name in trashed_names:
            # Session reappeared in tmux while trashed — surface as active
            continue  # caller drops it from trash
        agent_id = _get_or_mint_id(name, mapping)
        window_name = _tmux_window_name(name)
        title = _clean_title(name, window_name)
        preview = _tmux_pane_preview(name)
        last_activity = s['last_activity']
        result.append({
            'name': name,
            'display_name': title,
            'title': title,
            'type': 'chat',
            'attached': s['attached'],
            'preview': preview,
            'agent_id': agent_id,
            'created': s['created'],
            'last_activity': last_activity,
        })
    return result

# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

def _json_response(handler, code: int, body: dict):
    data = json.dumps(body).encode()
    handler.send_response(code)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(data)))
    handler.send_header('Access-Control-Allow-Origin', '*')
    handler.end_headers()
    handler.wfile.write(data)

def _read_body(handler) -> dict:
    length = int(handler.headers.get('Content-Length', 0))
    raw = handler.rfile.read(length) if length else b''
    return json.loads(raw) if raw else {}


class PentacleHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        # Suppress default access log noise; errors still go to stderr
        pass

    def _no_tmux(self):
        _json_response(self, 503, {'error': 'tmux not found', 'hint': 'install tmux'})

    # ── GET ────────────────────────────────────────────────────────

    def do_GET(self):
        if self.path in ('/', ''):
            _json_response(self, 200, {'ok': True, 'pentacle_api': 1})
            return

        if self.path == '/api/sessions':
            if not TMUX:
                self._no_tmux()
                return
            self._handle_sessions()
            return

        _json_response(self, 404, {'error': 'not found'})

    def _handle_sessions(self):
        mapping = _load_agents()
        trash_entries = _load_trash()
        tmux_sessions = _tmux_sessions()
        live_names = {s['name'] for s in tmux_sessions}

        # Reconcile: if a trashed session name reappears in tmux, drop it from trash
        new_trash = []
        reactivated = set()
        for t in trash_entries:
            if t.get('tmux_session') in live_names:
                reactivated.add(t['tmux_session'])
            else:
                new_trash.append(t)
        if len(new_trash) != len(trash_entries):
            _save_trash(new_trash)
            trash_entries = new_trash

        trashed_names = {t.get('tmux_session') for t in trash_entries}

        active = _build_active(tmux_sessions, mapping, trashed_names)

        # Build trashed list: only include entries whose session is NOT live
        trashed_out = []
        for t in trash_entries:
            trashed_out.append({
                'name': t.get('tmux_session', ''),
                'display_name': t.get('title', t.get('tmux_session', '')),
                'agent_id': t.get('agent_id', ''),
                'trashed_at': t.get('trashed_at', ''),
            })

        _save_agents(mapping)
        _json_response(self, 200, {'active': active, 'trashed': trashed_out})

    # ── POST ───────────────────────────────────────────────────────

    def do_POST(self):
        if not TMUX:
            self._no_tmux()
            return
        try:
            body = _read_body(self)
        except Exception:
            _json_response(self, 400, {'error': 'invalid JSON'})
            return

        routes = {
            '/api/rename': self._handle_rename,
            '/api/trash': self._handle_trash,
            '/api/restore': self._handle_restore,
            '/api/kill': self._handle_kill,
            '/api/new': self._handle_new,
            '/api/cleanup': self._handle_cleanup,
        }
        handler = routes.get(self.path)
        if handler:
            handler(body)
        else:
            _json_response(self, 404, {'error': 'not found'})

    def _handle_rename(self, body: dict):
        session = body.get('session', '')
        new_name = body.get('new_name', '')
        if not session or not new_name:
            _json_response(self, 400, {'error': 'session and new_name required'})
            return
        try:
            subprocess.run([TMUX, 'rename-session', '-t', session, new_name],
                           check=True, capture_output=True, timeout=5)
            # Update agent-id mapping
            mapping = _load_agents()
            if session in mapping:
                mapping[new_name] = mapping.pop(session)
                _save_agents(mapping)
            _json_response(self, 200, {'ok': True})
        except subprocess.CalledProcessError as e:
            _json_response(self, 500, {'error': e.stderr.decode().strip() or 'rename failed'})

    def _handle_trash(self, body: dict):
        session = body.get('session', '')
        if not session:
            _json_response(self, 400, {'error': 'session required'})
            return
        mapping = _load_agents()
        agent_id = mapping.get(session, str(uuid.uuid4()))
        trash = _load_trash()
        # Avoid duplicate entries
        if not any(t.get('agent_id') == agent_id for t in trash):
            trash.append({
                'agent_id': agent_id,
                'tmux_session': session,
                'title': _clean_title(session, ''),
                'trashed_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            })
            _save_trash(trash)
        try:
            subprocess.run([TMUX, 'kill-session', '-t', '=' + session],
                           capture_output=True, timeout=5)
        except Exception:
            pass
        _json_response(self, 200, {'ok': True})

    def _handle_restore(self, body: dict):
        agent_id = body.get('agent_id', '')
        if not agent_id:
            _json_response(self, 400, {'error': 'agent_id required'})
            return
        trash = _load_trash()
        entry = next((t for t in trash if t.get('agent_id') == agent_id), None)
        if not entry:
            _json_response(self, 404, {'error': 'trashed entry not found'})
            return

        session_name = entry.get('tmux_session', '')
        # Check if session already exists in tmux
        live = {s['name'] for s in _tmux_sessions()}
        if session_name and session_name not in live:
            # Recreate the session
            try:
                subprocess.run([TMUX, 'new-session', '-d', '-s', session_name],
                               check=True, capture_output=True, timeout=5)
            except subprocess.CalledProcessError:
                # Session name may collide; mint new name
                session_name = session_name + '-restored'
                try:
                    subprocess.run([TMUX, 'new-session', '-d', '-s', session_name],
                                   check=True, capture_output=True, timeout=5)
                except subprocess.CalledProcessError:
                    pass

        # Remove from trash
        new_trash = [t for t in trash if t.get('agent_id') != agent_id]
        _save_trash(new_trash)

        # Re-mint mapping for restored session
        if session_name:
            mapping = _load_agents()
            mapping[session_name] = agent_id
            _save_agents(mapping)

        _json_response(self, 200, {'ok': True})

    def _handle_kill(self, body: dict):
        session = body.get('session', '')
        agent_id = body.get('agent_id', '')
        # Kill the tmux session if it exists
        if session:
            try:
                subprocess.run([TMUX, 'kill-session', '-t', '=' + session],
                               capture_output=True, timeout=5)
            except Exception:
                pass
        # Remove from trash (permanent delete)
        if agent_id:
            trash = _load_trash()
            new_trash = [t for t in trash if t.get('agent_id') != agent_id]
            _save_trash(new_trash)
        # Remove from agent-id mapping
        if session:
            mapping = _load_agents()
            mapping.pop(session, None)
            _save_agents(mapping)
        _json_response(self, 200, {'ok': True})

    def _handle_new(self, body: dict):
        name = body.get('name', '') or f'session-{int(time.time())}'
        try:
            subprocess.run([TMUX, 'new-session', '-d', '-s', name],
                           check=True, capture_output=True, timeout=5)
            _json_response(self, 200, {'ok': True})
        except subprocess.CalledProcessError as e:
            _json_response(self, 500, {'error': e.stderr.decode().strip() or 'new-session failed'})

    def _handle_cleanup(self, _body: dict):
        """Kill sessions that have no windows (dead sessions)."""
        try:
            raw = _tmux('list-sessions', '-F', '#{session_name}|#{session_windows}')
            killed = []
            for line in raw.splitlines():
                parts = line.split('|')
                if len(parts) == 2 and parts[1].strip() == '0':
                    name = parts[0]
                    subprocess.run([TMUX, 'kill-session', '-t', name],
                                   capture_output=True, timeout=5)
                    killed.append(name)
            _json_response(self, 200, {'ok': True, 'killed': killed})
        except Exception as e:
            _json_response(self, 500, {'error': str(e)})


# ---------------------------------------------------------------------------
# ThreadingHTTPServer
# ---------------------------------------------------------------------------

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Pentacle reference API server')
    parser.add_argument('--port', type=int, default=7777)
    parser.add_argument('--host', default='127.0.0.1')
    args = parser.parse_args()

    print(f'[pentacle-api] tmux: {TMUX or "NOT FOUND"}', flush=True)

    if not TMUX:
        print('[pentacle-api] WARNING: tmux not found — all endpoints will return 503', flush=True)

    try:
        server = ThreadingHTTPServer((args.host, args.port), PentacleHandler)
    except OSError as e:
        if e.errno in (98, 48, 10048):  # EADDRINUSE on Linux/mac/Windows
            print(f'[pentacle-api] ERROR: port {args.port} already in use — exiting', file=sys.stderr)
            sys.exit(1)
        raise

    print(f'[pentacle-api] listening on {args.host}:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('[pentacle-api] shutting down', flush=True)


if __name__ == '__main__':
    main()
