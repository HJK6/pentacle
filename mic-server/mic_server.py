#!/usr/bin/env python3
"""
Mic Server - HTTP API for controlling mic modes.

Manages three modes:
  1. "clipboard" - Voice-to-clipboard (unmute -> speak -> mute -> text on clipboard)
  2. "meeting"   - Live meeting recording with real-time transcription
  3. "on"        - Always-on command listener (say wake word, then commands)

Exposes HTTP API on port 7780 for Pentacle integration.
Cross-platform: works on macOS, Windows, and Linux/WSL.
"""

import os
import sys
import json
import time
import signal
import threading
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

# Add mic-server to path
sys.path.insert(0, os.path.dirname(__file__))

PORT = 7780
TRANSCRIPT_DIR = os.path.join(os.path.dirname(__file__), "transcripts")

# --- State ---
state = {
    "mode": "off",           # "off", "clipboard", "meeting", "on"
    "meeting_active": False,
    "clipboard_pid": None,
    "transcript": [],         # Live transcript lines for meeting mode
    "partial": "",            # Current partial transcription
    "session_file": None,
    "meeting_start": None,
    "logs": [],               # Recent log messages
    # Always-on state
    "on_listener_state": "LISTENING",  # LISTENING, AWAKE, or CAPTURING
    "on_last_heard": "",
    "on_last_command": "",
    "on_last_copied": "",
}

meeting_recorder = None
clipboard_proc = None
always_on_listener = None


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    state["logs"].append(line)
    if len(state["logs"]) > 50:
        state["logs"] = state["logs"][-50:]
    print(line, flush=True)


# --- Stop all modes helper ---

def stop_all():
    """Stop whichever mode is currently active."""
    mode = state["mode"]
    if mode == "meeting":
        stop_meeting()
    elif mode == "clipboard":
        stop_clipboard()
    elif mode == "on":
        stop_always_on()


# --- Meeting Recorder Integration ---

def status_update(kind, data):
    """Called by MeetingRecorder for live updates."""
    if kind == "partial":
        state["partial"] = data
    elif kind == "final":
        state["transcript"].append(data)
        state["partial"] = ""
    elif kind == "log":
        log(data)


def start_meeting():
    global meeting_recorder
    stop_all()

    from meeting_recorder import MeetingRecorder
    if meeting_recorder is None:
        meeting_recorder = MeetingRecorder()
        import meeting_recorder as mr
        mr.status_callback = status_update
        meeting_recorder.load_models()

    state["transcript"] = []
    state["partial"] = ""
    meeting_recorder.start()
    state["mode"] = "meeting"
    state["meeting_active"] = True
    state["session_file"] = meeting_recorder.session_file
    state["meeting_start"] = time.time()
    log("Meeting recording started")


def stop_meeting():
    global meeting_recorder
    if meeting_recorder and meeting_recorder.running:
        session_file = meeting_recorder.stop()
        state["session_file"] = session_file
    state["meeting_active"] = False
    state["meeting_start"] = None
    if state["mode"] == "meeting":
        state["mode"] = "off"
    log("Meeting recording stopped")


# --- Clipboard Mode Integration ---

def _find_process(name):
    """Cross-platform process search by script name."""
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq python*", "/FO", "CSV"],
                capture_output=True, text=True
            )
            # Can't reliably match by script name on Windows, return empty
            return []
        except Exception:
            return []
    else:
        try:
            result = subprocess.run(
                ["pgrep", "-f", name], capture_output=True, text=True
            )
            return [int(p) for p in result.stdout.strip().split() if p]
        except Exception:
            return []


def _kill_process(pid):
    """Cross-platform process kill."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    else:
        subprocess.run(["kill", str(pid)], capture_output=True)


def start_clipboard():
    global clipboard_proc
    stop_all()

    # Check if already running
    pids = _find_process("mic_listener.py")
    if pids:
        state["mode"] = "clipboard"
        state["clipboard_pid"] = pids[0]
        log("Clipboard mode already running")
        return

    script = os.path.join(os.path.dirname(__file__), "mic_listener.py")
    clipboard_proc = subprocess.Popen(
        [sys.executable, "-u", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    state["mode"] = "clipboard"
    state["clipboard_pid"] = clipboard_proc.pid
    log(f"Clipboard mode started (PID {clipboard_proc.pid})")

    # Monitor output in background
    def tail_output():
        for line in clipboard_proc.stdout:
            log(line.decode().strip())
    threading.Thread(target=tail_output, daemon=True).start()


def stop_clipboard():
    global clipboard_proc

    # Kill any running mic_listener.py
    for pid in _find_process("mic_listener.py"):
        _kill_process(pid)

    if clipboard_proc:
        try:
            clipboard_proc.terminate()
            clipboard_proc.wait(timeout=3)
        except Exception:
            try:
                clipboard_proc.kill()
            except Exception:
                pass
        clipboard_proc = None

    state["clipboard_pid"] = None
    if state["mode"] == "clipboard":
        state["mode"] = "off"
    log("Clipboard mode stopped")


# --- Always-On Mode Integration ---

def always_on_event(kind, data):
    """Called by AlwaysOnListener for live updates."""
    if kind == "log":
        log(data)
    elif kind == "command":
        state["on_last_command"] = data
    elif kind == "capturing":
        state["on_last_heard"] = data
        state.setdefault("on_captured_texts", []).append(data)
    elif kind == "copied":
        state["on_last_copied"] = data
        state["on_captured_texts"] = []
    elif kind == "state":
        state["on_listener_state"] = data
        if data == "LISTENING":
            state["on_captured_texts"] = []
        elif data == "CAPTURING":
            state["on_captured_texts"] = []


def start_always_on():
    global always_on_listener
    stop_all()

    from always_on import AlwaysOnListener
    if always_on_listener is None:
        always_on_listener = AlwaysOnListener()
        always_on_listener.on_event = always_on_event
        always_on_listener.load_models()

    always_on_listener.on_event = always_on_event
    always_on_listener.on_meeting_start = start_meeting_voice
    always_on_listener.on_meeting_stop = stop_meeting_voice
    always_on_listener.start()
    state["mode"] = "on"
    state["on_listener_state"] = "LISTENING"
    state["on_last_heard"] = ""
    state["on_last_command"] = ""
    state["on_last_copied"] = ""
    log("Always-on listener started")


def start_meeting_voice():
    """Start meeting recording via voice command (doesn't change mic mode)."""
    global meeting_recorder
    from meeting_recorder import MeetingRecorder
    if meeting_recorder is None:
        meeting_recorder = MeetingRecorder()
        import meeting_recorder as mr
        mr.status_callback = status_update
        meeting_recorder.load_models()

    state["transcript"] = []
    state["partial"] = ""
    meeting_recorder.start()
    state["meeting_active"] = True
    state["session_file"] = meeting_recorder.session_file
    state["meeting_start"] = time.time()
    log("Meeting recording started (voice)")


def stop_meeting_voice():
    """Stop meeting recording via voice command."""
    global meeting_recorder
    if meeting_recorder and meeting_recorder.running:
        session_file = meeting_recorder.stop()
        state["session_file"] = session_file
    state["meeting_active"] = False
    state["meeting_start"] = None
    log("Meeting recording stopped (voice)")


def stop_always_on():
    global always_on_listener
    if always_on_listener and always_on_listener.running:
        always_on_listener.stop()
    if state["mode"] == "on":
        state["mode"] = "off"
    state["on_listener_state"] = "LISTENING"
    log("Always-on listener stopped")


# --- HTTP Handler ---

class MicHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/status":
            data = {
                "mode": state["mode"],
                "meeting_active": state["meeting_active"],
                "clipboard_pid": state["clipboard_pid"],
                "transcript_count": len(state["transcript"]),
                "partial": state["partial"],
                "session_file": state["session_file"],
                "duration": (time.time() - state["meeting_start"]) if state["meeting_start"] else 0,
                # Always-on fields
                "on_listener_state": state["on_listener_state"],
                "on_last_heard": state["on_last_heard"],
                "on_last_command": state["on_last_command"],
                "on_last_copied": state["on_last_copied"],
                "on_captured_texts": state.get("on_captured_texts", []),
            }
            self._json(data)

        elif self.path == "/transcript":
            self._json({
                "lines": state["transcript"],
                "partial": state["partial"],
            })

        elif self.path.startswith("/transcript/since/"):
            try:
                idx = int(self.path.split("/")[-1])
                self._json({
                    "lines": state["transcript"][idx:],
                    "partial": state["partial"],
                    "total": len(state["transcript"]),
                })
            except Exception:
                self._json({"lines": [], "partial": ""})

        elif self.path == "/calibration":
            if always_on_listener:
                cal = always_on_listener.get_calibration_status()
                cal_file = os.path.join(os.path.dirname(__file__), "calibration.json")
                saved = {}
                if os.path.exists(cal_file):
                    with open(cal_file) as f:
                        saved = json.load(f)
                cal["saved"] = saved
                from always_on import PHRASE_GROUPS
                cal["groups"] = PHRASE_GROUPS
                self._json(cal)
            else:
                self._json({"error": "always-on listener not active"}, 400)

        elif self.path == "/logs":
            self._json({"logs": state["logs"]})

        elif self.path == "/transcripts":
            files = []
            if os.path.exists(TRANSCRIPT_DIR):
                for f in sorted(os.listdir(TRANSCRIPT_DIR), reverse=True):
                    if f.endswith(".txt"):
                        fpath = os.path.join(TRANSCRIPT_DIR, f)
                        files.append({
                            "name": f,
                            "path": fpath,
                            "size": os.path.getsize(fpath),
                            "modified": os.path.getmtime(fpath),
                        })
            self._json({"transcripts": files})

        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        body = self._read_body()

        if self.path == "/mode/clipboard":
            threading.Thread(target=start_clipboard, daemon=True).start()
            self._json({"ok": True, "mode": "clipboard"})

        elif self.path == "/mode/meeting":
            threading.Thread(target=start_meeting, daemon=True).start()
            self._json({"ok": True, "mode": "meeting"})

        elif self.path == "/mode/on":
            threading.Thread(target=start_always_on, daemon=True).start()
            self._json({"ok": True, "mode": "on"})

        elif self.path == "/mode/off":
            stop_all()
            state["mode"] = "off"
            self._json({"ok": True, "mode": "off"})

        elif self.path == "/calibrate/start":
            if not always_on_listener or not always_on_listener.running:
                self._json({"error": "always-on listener not active -- set mode to 'on' first"}, 400)
            else:
                group = body.get("group", "wake")
                count = body.get("count", 5)
                ok = always_on_listener.start_calibration(group, count)
                self._json({"ok": ok, "group": group, "count": count})

        elif self.path == "/copy/start":
            if not always_on_listener or not always_on_listener.running:
                self._json({"error": "always-on listener not active"}, 400)
            elif always_on_listener.state == "CAPTURING":
                self._json({"ok": True, "already": True})
            else:
                always_on_listener._execute_command("start_copy")
                self._json({"ok": True})

        elif self.path == "/copy/stop":
            if not always_on_listener or not always_on_listener.running:
                self._json({"error": "always-on listener not active"}, 400)
            elif always_on_listener.state != "CAPTURING":
                self._json({"ok": False, "error": "not capturing"})
            else:
                always_on_listener._execute_command("end_copy")
                self._json({"ok": True, "copied": state.get("on_last_copied", "")})

        elif self.path == "/calibrate/stop":
            if always_on_listener:
                save = body.get("save", True)
                always_on_listener.stop_calibration(save=save)
                self._json({"ok": True})
            else:
                self._json({"error": "always-on listener not active"}, 400)

        else:
            self._json({"error": "not found"}, 404)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length:
            return json.loads(self.rfile.read(length))
        return {}

    def _json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress default logging


def main():
    def handle_signal(sig, frame):
        log("Shutting down...")
        stop_all()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    server = HTTPServer(("127.0.0.1", PORT), MicHandler)
    log(f"Mic Server running on http://127.0.0.1:{PORT}")
    log(f"Platform: {sys.platform}")
    log("Modes: clipboard, meeting, on, off")

    # Optional auto-start mode via env var
    start_mode = os.environ.get("MIC_SERVER_START_MODE", "").strip().lower()
    if start_mode in ("on", "clipboard", "meeting"):
        log(f"Auto-starting in '{start_mode}' mode (MIC_SERVER_START_MODE)")
        def _autostart():
            try:
                if start_mode == "on":
                    start_always_on()
                elif start_mode == "clipboard":
                    start_clipboard()
                elif start_mode == "meeting":
                    start_meeting()
            except Exception as e:
                log(f"Auto-start failed: {e}")
        threading.Thread(target=_autostart, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        handle_signal(None, None)


if __name__ == "__main__":
    main()
