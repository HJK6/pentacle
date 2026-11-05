#!/usr/bin/env python3
"""Pentacle's plain TTY REPL over Cursor print mode; stdlib only.

No provider bytes are interpreted as terminal controls or admission receipts.
The only source of a receipt is admission from the pane input descriptor.
"""
from __future__ import annotations

import argparse
from collections import deque
import codecs
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import termios
import time
import tty

from cursor_receipts import receipt_key


def _safe(text: str) -> str:
    return ''.join(c for c in text if c in '\n\t' or c.isprintable()).replace('\x1b', '')


class Repl:
    def __init__(self, args):
        self.args = args
        self.queue = deque()
        self.n = 0
        self.child = None
        self.current = 0
        self.started = 0.0
        self.stdout = b''
        self.stderr_class = 'other'
        self.result_seen = False
        self.failed_identity = False
        self.stopping = False
        self.draft = ''
        self.paste = False
        self.escape = ''
        self.fd = None

    def record(self, record: dict) -> None:
        data = (json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
        if os.write(self.fd, data) != len(data):
            raise OSError('short transcript write')
        os.fsync(self.fd)

    def line(self, text: str) -> None:
        print('\r\x1b[2K' + text, flush=True)

    def idle(self):
        print('› ' + _safe(self.draft).replace('\n', '\n  '), end='', flush=True)

    def admit(self):
        message, self.draft = self.draft.strip(), ''
        if not message:
            if not self.child:
                self.idle()
            return
        if message == '/exit':
            self.stopping = True
            return
        if self.failed_identity:
            self.line('! identity mismatch')
            return
        self.n += 1
        self.line(f'» #{self.n} {receipt_key(message)}')
        self.queue.append((self.n, message))

    def start(self):
        if self.child or not self.queue or self.stopping or self.failed_identity:
            return
        self.current, message = self.queue.popleft()
        self.line(f'▶ #{self.current}')
        args = self.args
        command = [args.agent, '-p', '--trust', '--force', '--disable-auto-update',
                   '--workspace', args.cwd, '--resume=' + args.chat_id,
                   '--output-format', 'stream-json', '--stream-partial-output',
                   '--model', args.model, message]
        self.stdout = b''
        self.stderr_class = 'other'
        self.result_seen = False
        self.started = time.monotonic()
        try:
            self.child = subprocess.Popen(command, cwd=args.cwd, stdin=subprocess.DEVNULL,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          start_new_session=True)
            os.set_blocking(self.child.stdout.fileno(), False)
            os.set_blocking(self.child.stderr.fileno(), False)
        except OSError:
            self.record({'type':'pentacle_repl', 'event':'turn_failed', 'n':self.current,
                         'exit':127, 'stderr_class':'other'})
            self.line(f'! #{self.current} other')
            self.idle()

    def stop_child(self):
        child = self.child
        if child is None:
            return
        # Reap the leader before the final group signal. macOS may report
        # EPERM for a just-exited zombie leader until waitpid consumes it.
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            child.wait(timeout=.1)
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()
        child.stdout.close()
        child.stderr.close()
        self.child = None

    def interrupt(self):
        if not self.child:
            self.line('use /exit to close')
            self.idle()
            return
        self.stop_child()
        self.record({'type':'pentacle_repl', 'event':'interrupted', 'n':self.current})
        self.line(f'! #{self.current} interrupted')
        if not self.queue:
            self.idle()

    def output(self, data: bytes):
        self.stdout += data or b''
        while b'\n' in self.stdout:
            raw, self.stdout = self.stdout.split(b'\n', 1)
            try:
                record = json.loads(raw)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(record, dict):
                continue
            if record.get('type') == 'system' and record.get('subtype') == 'init' and record.get('session_id') != self.args.chat_id:
                self.record({'type':'pentacle_repl', 'event':'identity_mismatch'})
                self.failed_identity = True
                self.queue.clear()
                self.stop_child()
                self.line('! identity mismatch')
                return
            self.record(record)
            kind = record.get('type')
            if kind == 'result':
                self.result_seen = True
            if kind == 'assistant':
                content = record.get('message', {}).get('content', [])
                text = ''.join(part.get('text', '') for part in content if isinstance(part, dict) and part.get('type') == 'text') if isinstance(content, list) else ''
                if text:
                    # Deltas stay live on the pane; every display line is framed.
                    for line in _safe(text).splitlines():
                        self.line('│ ' + line)
            elif kind == 'tool_call':
                self.line('│ tool ' + str(record.get('subtype', '')))

    def input(self, text: str):
        render_draft = False
        for char in text:
            if self.escape or char == '\x1b':
                self.escape += char
                if self.escape in ('\x1b[200~', '\x1b[201~'):
                    self.paste = self.escape == '\x1b[200~'
                    self.escape = ''
                elif not any(seq.startswith(self.escape) for seq in ('\x1b[200~', '\x1b[201~')):
                    self.escape = ''
                continue
            if char == '\x03' and not self.paste:
                self.draft = ''
                self.interrupt()
                render_draft = False
            elif char == '\x04' and not self.paste:
                self.stopping = True
            elif char in '\r\n' and not self.paste:
                self.admit()
                render_draft = False
            elif char in ('\x7f', '\b') and not self.paste:
                self.draft = self.draft[:-1]
                render_draft = True
            elif char.isprintable() or (self.paste and char in '\r\n\t'):
                self.draft += '\n' if char == '\r' else char
                # Prefix draft continuation lines so input cannot forge receipts.
                render_draft = True

        if render_draft:
            print('\r\x1b[2K› ' + _safe(self.draft).replace('\n', '\n  '), end='', flush=True)

    def run(self):
        path = Path(self.args.transcript)
        expected = Path(self.args.cwd) / '.cursor/sessions' / (self.args.session_id + '.jsonl')
        if path != expected or path != path.resolve():
            raise ValueError('cursor_transcript_invalid')
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.fchmod(self.fd, 0o600)
        self.record({'type':'pentacle_repl', 'version':1, 'session_id':self.args.session_id,
                     'chat_id':self.args.chat_id, 'cwd':self.args.cwd, 'model':self.args.model,
                     'cli_version':self.args.cli_version})
        old = termios.tcgetattr(0) if os.isatty(0) else None
        prior_signals = {}
        for sig in (signal.SIGTERM, signal.SIGHUP):
            prior_signals[sig] = signal.signal(sig, lambda *_: setattr(self, 'stopping', True))
        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        try:
            if old:
                tty.setraw(0)
                # Keep output newline translation so captures have ordinary lines.
                state = termios.tcgetattr(0)
                state[1] = old[1]
                termios.tcsetattr(0, termios.TCSANOW, state)
            print('\x1b[?2004h', end='', flush=True)
            self.idle()
            while not self.stopping:
                self.start()
                child = self.child
                readers = [0] + ([child.stdout, child.stderr] if child else [])
                ready, _, _ = select.select(readers, [], [], .05)
                if 0 in ready:
                    data = os.read(0, 65536)
                    if not data:
                        self.stopping = True
                    else:
                        self.input(decoder.decode(data))
                if child and self.child is child:
                    for pipe in (child.stdout, child.stderr):
                        if self.child is not child:
                            break
                        if pipe in ready:
                            data = os.read(pipe.fileno(), 65536)
                            if pipe is child.stdout:
                                self.output(data)
                            elif b'ActionRequiredError' in data:
                                self.stderr_class = 'ActionRequiredError'
                    if self.child and child.poll() is not None:
                        # Drain both pipes after exit, before closing the turn.
                        self.output(child.stdout.read())
                        if self.child is not child:
                            continue
                        error = child.stderr.read() or b''
                        if b'ActionRequiredError' in error:
                            self.stderr_class = 'ActionRequiredError'
                        if self.child is None:
                            continue
                        if child.returncode and not self.result_seen:
                            self.record({'type':'pentacle_repl', 'event':'turn_failed', 'n':self.current,
                                         'exit':child.returncode, 'stderr_class':self.stderr_class})
                            self.line(f'! #{self.current} {self.stderr_class}')
                        else:
                            self.line(f'─ #{self.current} done {int((time.monotonic()-self.started)*1000)} ms')
                        # Kill any surviving per-turn descendants even on success.
                        self.stop_child()
                        if not self.queue:
                            self.idle()
                    elif self.child and time.monotonic() - self.started > 1200:
                        self.interrupt()
        finally:
            self.stop_child()
            self.record({'type':'pentacle_repl', 'event':'exit'})
            os.close(self.fd)
            print('\x1b[?2004l', end='', flush=True)
            if old:
                termios.tcsetattr(0, termios.TCSANOW, old)
            for sig, handler in prior_signals.items():
                signal.signal(sig, handler)
        return 0


def main():
    parser = argparse.ArgumentParser()
    for name in ('agent', 'session-id', 'chat-id', 'transcript', 'cwd', 'model', 'cli-version'):
        parser.add_argument('--' + name, required=True)
    return Repl(parser.parse_args()).run()


if __name__ == '__main__':
    raise SystemExit(main())
