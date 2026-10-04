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
from contextlib import contextmanager
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from datetime import datetime

# Add mic-server to path
sys.path.insert(0, os.path.dirname(__file__))

from audio_buffer import get_audio_buffer
from speaker_service import get_service

def audio_duration_s(path):
    """Probe container duration before upload inference; never load another model."""
    import math
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
        capture_output=True, text=True, timeout=10, check=True,
    )
    duration = float(json.loads(result.stdout)["format"]["duration"])
    if not math.isfinite(duration) or duration < 0:
        raise ValueError("invalid audio duration")
    return duration


PORT = 7780
TRANSCRIPT_DIR = os.path.join(os.path.dirname(__file__), "transcripts")
REMOTE_CLIPBOARD_MAX_LINES = 200

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
    "caller": None,
    "paused_mode": None,
    "paused_caller": None,
    "last_error": None,
    "clipboard_remote": False,
    "remote_clipboard_lines": [],
    # Always-on state
    "on_listener_state": "LISTENING",  # LISTENING, AWAKE, or CAPTURING
    "on_last_heard": "",
    "on_last_command": "",
    "on_last_copied": "",
}

meeting_recorder = None
clipboard_proc = None
clipboard_proc_lock = threading.Lock()
always_on_listener = None
always_on_models_lock = threading.Lock()
model_preload_status = {"state": "idle", "error": None, "vocabulary_version": None}
transition_lock = threading.Lock()
transition_lock_owner = None
remote_clipboard_lock = threading.Lock()


class AudioHealthState:
    NONZERO_PEAK = 1e-6
    STALE_CALLBACK_SECS = 3.0
    DEGRADED_ERROR_SECS = 10.0

    def __init__(self):
        self._lock = threading.RLock()
        self._data = self._initial_data()
        self._last_callback_at = None
        self._flatline_started_at = None
        self._degraded_since = None

    def _initial_data(self):
        return {
            "selected_device": None,
            "preferred_device_name": os.environ.get("MIC_DEVICE_NAME", "").strip() or None,
            "preferred_present": False,
            "disallowed_device": False,
            "stream_open": False,
            "callback_age_ms": None,
            "last_nonzero_at": None,
            "peak": 0.0,
            "rms": 0.0,
            "flatline_seconds": 0.0,
            "stream_status": None,
            "health_state": "ok",
            "health_message": "No audio stream has reported yet.",
        }

    def update(self, payload, now=None):
        now = time.time() if now is None else now
        with self._lock:
            for key in (
                "selected_device",
                "preferred_device_name",
                "preferred_present",
                "disallowed_device",
                "stream_open",
                "stream_status",
            ):
                if key in payload:
                    self._data[key] = payload[key]

            if "peak" in payload:
                peak = float(payload.get("peak") or 0.0)
                rms = float(payload.get("rms") or 0.0)
                self._data["peak"] = peak
                self._data["rms"] = rms
                self._last_callback_at = now
                if peak > self.NONZERO_PEAK:
                    self._data["last_nonzero_at"] = now
                    self._flatline_started_at = None
                    self._data["flatline_seconds"] = 0.0
                else:
                    if self._flatline_started_at is None:
                        self._flatline_started_at = now
                    self._data["flatline_seconds"] = max(0.0, now - self._flatline_started_at)

            self._classify_locked(now)
            return dict(self._data)

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        with self._lock:
            self._classify_locked(now)
            return dict(self._data)

    def reset(self):
        with self._lock:
            self._data = self._initial_data()
            self._last_callback_at = None
            self._flatline_started_at = None
            self._degraded_since = None

    def _classify_locked(self, now):
        if self._last_callback_at is None:
            self._data["callback_age_ms"] = None
        else:
            self._data["callback_age_ms"] = int(max(0.0, now - self._last_callback_at) * 1000)

        if self._flatline_started_at is not None:
            self._data["flatline_seconds"] = max(0.0, now - self._flatline_started_at)

        state_name, message = self._health_result_locked()
        self._data["health_state"] = state_name
        self._data["health_message"] = message

        if state_name == "ok":
            self._degraded_since = None
        elif self._degraded_since is None:
            self._degraded_since = now

    def _health_result_locked(self):
        selected = self._data["selected_device"]
        preferred = self._data["preferred_device_name"]
        preferred_present = self._data["preferred_present"]
        callback_age_ms = self._data["callback_age_ms"]

        if preferred and preferred_present is False:
            return "preferred_missing", f"Preferred input '{preferred}' is not present."
        if self._data["disallowed_device"]:
            return "wrong_device", f"Selected input '{selected}' is denylisted."
        if preferred and selected and preferred.lower() not in selected.lower():
            return "wrong_device", f"Selected input '{selected}' is not preferred '{preferred}'."
        if self._data["stream_status"]:
            return "stream_error", f"Audio stream status: {self._data['stream_status']}"
        if selected and not self._data["stream_open"]:
            return "stream_error", "Audio stream is not open."
        if self._data["stream_open"] and callback_age_ms is None:
            return "stream_error", "Audio stream is open but no callbacks have arrived."
        if callback_age_ms is not None and callback_age_ms > int(self.STALE_CALLBACK_SECS * 1000):
            return "stream_error", f"Audio callbacks are stale ({callback_age_ms} ms)."
        if self._data["stream_open"] and self._data["flatline_seconds"] >= 1.0:
            return (
                "muted_or_tcc_silence",
                "Selected input is returning silence; check hardware mute and macOS microphone permission.",
            )
        return "ok", "Audio stream is healthy."

    def last_error_payload(self, now=None):
        now = time.time() if now is None else now
        with self._lock:
            self._classify_locked(now)
            if self._data["health_state"] == "ok":
                return None
            if self._degraded_since is None:
                return None
            if now - self._degraded_since < self.DEGRADED_ERROR_SECS:
                return None
            return {
                "type": "audio_health",
                "health_state": self._data["health_state"],
                "health_message": self._data["health_message"],
            }


audio_health = AudioHealthState()


@contextmanager
def locked_transition():
    global transition_lock_owner
    transition_lock.acquire()
    transition_lock_owner = threading.get_ident()
    try:
        yield
    finally:
        transition_lock_owner = None
        transition_lock.release()


def assert_transition_locked():
    if transition_lock_owner != threading.get_ident():
        raise RuntimeError("transition_lock must be held for coordination state writes")


def get_bind_host():
    return os.environ.get("MIC_BIND_HOST", "127.0.0.1")


def always_on_enabled():
    return os.environ.get("MIC_ALWAYS_ON_ENABLED", "false") == "true"


def auto_start_caller():
    """Identity recorded as `caller` for modes started by MIC_SERVER_START_MODE.
    Defaults to None (legacy/anonymous). Operators set MIC_SERVER_HOST_ID so
    other clients see e.g. "bart using mic" instead of "unknown using mic"."""
    value = os.environ.get("MIC_SERVER_HOST_ID", "").strip()
    return value or None


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    state["logs"].append(line)
    if len(state["logs"]) > 50:
        state["logs"] = state["logs"][-50:]
    print(line, flush=True)


def append_remote_clipboard_text(text):
    with remote_clipboard_lock:
        state["remote_clipboard_lines"].append(text)
        if len(state["remote_clipboard_lines"]) > REMOTE_CLIPBOARD_MAX_LINES:
            state["remote_clipboard_lines"] = state["remote_clipboard_lines"][-REMOTE_CLIPBOARD_MAX_LINES:]


def clear_remote_clipboard_lines():
    with remote_clipboard_lock:
        state["remote_clipboard_lines"] = []


def remote_clipboard_since(idx):
    with remote_clipboard_lock:
        lines = state["remote_clipboard_lines"][idx:]
        total = len(state["remote_clipboard_lines"])
    return {"lines": lines, "total": total}


def handle_clipboard_stdout_line(line):
    if line.startswith("{"):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return
        if isinstance(data, dict) and data.get("type") == "audio_health":
            update_audio_health(data)
            return
        if isinstance(data, dict) and data.get("type") == "remote_clipboard":
            append_remote_clipboard_text(data.get("text", ""))
            return
        if isinstance(data, dict) and data.get("type") == "end_session":
            with locked_transition():
                mode = state["mode"]
                if mode == "clipboard":
                    stop_clipboard()
                    set_coordination_state(
                        mode="off",
                        caller=None,
                        paused_mode=None,
                        paused_caller=None,
                        last_error=None,
                        clipboard_remote=False,
                    )
                    log("[end_session] stop-word from listener")
                else:
                    log(f"[end_session] ignored (mode={mode})")
            return
    log(line)


def set_coordination_state(mode=None, caller=None, paused_mode=None, paused_caller=None,
                           last_error=None, clipboard_remote=None):
    assert_transition_locked()
    if mode is not None:
        state["mode"] = mode
    state["caller"] = caller
    state["paused_mode"] = paused_mode
    state["paused_caller"] = paused_caller
    state["last_error"] = last_error
    if clipboard_remote is not None:
        state["clipboard_remote"] = clipboard_remote


def update_audio_health(payload):
    audio_health.update(payload)


def apply_audio_last_error(audio):
    error = audio_health.last_error_payload()
    if error is None:
        if audio["health_state"] == "ok" and isinstance(state["last_error"], dict):
            if state["last_error"].get("type") == "audio_health":
                state["last_error"] = None
        elif audio["health_state"] == "ok" and state["last_error"] == "audio_health":
            state["last_error"] = None
        return
    state["last_error"] = error


def mic_in_use_response():
    assert_transition_locked()
    return {
        "error": "mic_in_use",
        "mode": state["mode"],
        "caller": state["caller"],
    }


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
    if kind == "audio_health":
        update_audio_health(data)
    elif kind == "partial":
        state["partial"] = data
    elif kind == "final":
        state["transcript"].append(data)
        state["partial"] = ""
    elif kind == "log":
        log(data)


def start_meeting(stop_existing=True):
    global meeting_recorder
    if stop_existing:
        stop_all()

    if meeting_recorder is None:
        from meeting_recorder import MeetingRecorder
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
    log("Meeting recording started")


def stop_meeting():
    global meeting_recorder
    if meeting_recorder and meeting_recorder.running:
        session_file = meeting_recorder.stop()
        state["session_file"] = session_file
    state["meeting_active"] = False
    state["meeting_start"] = None
    audio_health.reset()
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


def _wait_for_process(proc, timeout=45):
    try:
        is_running = True
        if hasattr(proc, "poll"):
            is_running = proc.poll() is None
        if is_running:
            proc.terminate()
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=timeout)
    except ProcessLookupError:
        try:
            proc.wait(timeout=0)
        except Exception:
            pass
    finally:
        stdout = getattr(proc, "stdout", None)
        if stdout:
            try:
                stdout.close()
            except Exception:
                pass


def _clear_clipboard_proc(proc):
    global clipboard_proc
    with clipboard_proc_lock:
        if clipboard_proc is proc:
            clipboard_proc = None
            state["clipboard_pid"] = None


def start_clipboard(remote=False, stop_existing=True):
    global clipboard_proc
    if stop_existing:
        stop_all()

    clear_remote_clipboard_lines()

    # Check if already running
    pids = _find_process("mic_listener.py")
    if pids:
        state["clipboard_pid"] = pids[0]
        log("Clipboard mode already running")
        return

    script = os.path.join(os.path.dirname(__file__), "mic_listener.py")
    env = os.environ.copy()
    if remote:
        env["MIC_REMOTE_CLIPBOARD"] = "1"
    else:
        env.pop("MIC_REMOTE_CLIPBOARD", None)
    proc = subprocess.Popen(
        [sys.executable, "-u", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )
    with clipboard_proc_lock:
        clipboard_proc = proc
        state["clipboard_pid"] = proc.pid
    log(f"Clipboard mode started (PID {proc.pid})")

    # Monitor output in background
    def tail_output(child):
        try:
            try:
                for line in child.stdout:
                    handle_clipboard_stdout_line(line.decode().strip())
            except (ValueError, OSError):
                pass
        finally:
            try:
                child.wait(timeout=0)
            except subprocess.TimeoutExpired:
                pass
            except Exception:
                pass
            _clear_clipboard_proc(child)
    threading.Thread(target=tail_output, args=(proc,), daemon=True).start()


def stop_clipboard():
    global clipboard_proc

    with clipboard_proc_lock:
        proc = clipboard_proc

    if proc:
        _wait_for_process(proc)
        _clear_clipboard_proc(proc)

    # Kill any stray mic_listener.py processes not tracked by this parent.
    tracked_pid = getattr(proc, "pid", None)
    for pid in _find_process("mic_listener.py"):
        if pid != tracked_pid:
            _kill_process(pid)

    state["clipboard_pid"] = None
    state["clipboard_remote"] = False
    audio_health.reset()
    log("Clipboard mode stopped")


# --- Always-On Mode Integration ---

def always_on_event(kind, data):
    """Called by AlwaysOnListener for live updates."""
    if kind == "audio_health":
        update_audio_health(data)
    elif kind == "log":
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
            if getattr(always_on_listener, "capture_origin", None) not in ("wake", "local_action", "followup"):
                state["on_last_copied"] = ""


def ensure_always_on_models():
    """Publish a complete resident listener once; loading does not hold transition_lock."""
    global always_on_listener
    with always_on_models_lock:
        if always_on_listener is None:
            from always_on import AlwaysOnListener
            candidate = AlwaysOnListener()
            candidate.on_event = always_on_event
            candidate.load_models()
            if candidate.whisper_model is None:
                raise RuntimeError("Resident ASR model did not load")
            always_on_listener = candidate
        return always_on_listener


def preload_always_on():
    """Managed OFF startup: load the upload model/profile, never start capture."""
    model_preload_status.update(state="loading", error=None, vocabulary_version=None)
    try:
        import fleet_vocabulary
        prompt, _, version = fleet_vocabulary.load_vocabulary()
        if not prompt:
            raise ValueError("Fleet vocabulary is not configured")
        listener = ensure_always_on_models()
        if listener.whisper_model is None:
            raise RuntimeError("Resident ASR model did not load")
        model_preload_status.update(state="ready", error=None, vocabulary_version=version)
        log("OFF resident model ready; capture remains closed")
    except Exception as exc:
        model_preload_status.update(state="failed", error=str(exc))
        with transition_lock:
            if state["mode"] == "off":
                state["last_error"] = f"model_preload_failed: {exc}"
        log(f"OFF resident model preload failed: {exc}")


def start_always_on(stop_existing=True):
    if stop_existing:
        stop_all()

    listener = ensure_always_on_models()
    listener.on_event = always_on_event
    listener.on_capture_end = lambda: get_service().capture_ended('room_mic', listener, state['meeting_active'])
    if getattr(getattr(listener, 'voice_actions', None), 'enabled', False):
        log('WARNING: local actions must remain disabled until capture acknowledgement ordering is fixed; keep MIC_LOCAL_ACTIONS=false.')
    listener.on_meeting_start = start_meeting_voice
    listener.on_meeting_stop = stop_meeting_voice

    # Re-enumerate the audio backend so this long-running server opens the input
    # stream against the current CoreAudio device IDs. PortAudio caches the device
    # table at Pa_Initialize (process start); after a mic replug / default-input
    # change / login relog / coreaudiod restart those IDs go stale and the open
    # fails with AUHAL '!obj' -> -10851 -> PortAudio -9986 (sd.query_devices()
    # still returns the stale list, so device resolution succeeds while the open
    # fails). Skip the refresh only while a meeting recorder has a genuinely live
    # stream: the refresh is a global PortAudio re-init that would tear it down,
    # and a live stream already proves the device IDs are current. We test the
    # actual stream (has_open_stream), not `running`, because a failed meeting
    # open leaves running=True with no stream -- where the refresh is both safe
    # and needed.
    if not (meeting_recorder and meeting_recorder.has_open_stream()):
        from audio_device import refresh_audio_backend
        refresh_audio_backend()

    try:
        listener.start()
    except Exception:
        # A failed audio open may already have started capture workers.
        # Retain the resident model but stop those workers before reporting failure.
        listener.stop()
        raise
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
    audio_health.reset()
    log("Meeting recording stopped (voice)")


def stop_always_on():
    global always_on_listener
    if always_on_listener and always_on_listener.running:
        always_on_listener.stop()
    state["on_listener_state"] = "LISTENING"
    audio_health.reset()
    log("Always-on listener stopped")


# --- HTTP Handler ---

class MicHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/status":
            audio = audio_health.snapshot()
            with transition_lock:
                if state["mode"] != "off":
                    apply_audio_last_error(audio)
                else:
                    audio.update(health_state="idle", health_message="Microphone is off.")
                    if isinstance(state["last_error"], dict) and state["last_error"].get("type") == "audio_health":
                        state["last_error"] = None
                data = {
                    "speaker": get_service().status(),
                    "mode": state["mode"],
                    "meeting_active": state["meeting_active"],
                    "clipboard_pid": state["clipboard_pid"],
                    "transcript_count": len(state["transcript"]),
                    "partial": state["partial"],
                    "session_file": state["session_file"],
                    "duration": (time.time() - state["meeting_start"]) if state["meeting_start"] else 0,
                    "caller": state["caller"],
                    "paused_mode": state["paused_mode"],
                    "paused_caller": state["paused_caller"],
                    "last_error": state["last_error"],
                    # Always-on fields
                    "on_listener_state": state["on_listener_state"],
                    "on_last_heard": state["on_last_heard"],
                    "on_last_command": state["on_last_command"],
                    "on_last_copied": state["on_last_copied"],
                    "on_captured_texts": state.get("on_captured_texts", []),
                    "capture_origin": getattr(always_on_listener, "capture_origin", None),
                    "wake": always_on_listener.wake.snapshot() if getattr(always_on_listener, "wake", None) else {"enabled": False},
                    "audio": audio,
                    "local_actions": always_on_listener.voice_actions.snapshot() if getattr(always_on_listener, "voice_actions", None) else {"enabled": False},
                    "audio_buffer": get_audio_buffer().snapshot() if get_audio_buffer() else {"enabled": False},
                    "asr": {
                        "backend": os.environ.get("MIC_WHISPER_BACKEND", "faster-whisper"),
                        "model": os.environ.get("MIC_WHISPER_MODEL", "small.en"),
                        "device": os.environ.get("MIC_WHISPER_DEVICE", "cpu"),
                        "compute_type": os.environ.get("MIC_WHISPER_COMPUTE_TYPE", "float16" if os.environ.get("MIC_WHISPER_DEVICE") == "cuda" else "int8"),
                        "loaded": bool(always_on_listener and always_on_listener.whisper_model),
                        "load_state": "ready" if always_on_listener and always_on_listener.whisper_model else model_preload_status["state"],
                        "load_error": None if always_on_listener and always_on_listener.whisper_model else model_preload_status["error"],
                        "vocabulary_version": model_preload_status["vocabulary_version"],
                        "cpu_threads": os.environ.get("MIC_WHISPER_CPU_THREADS", "4"),
                    },
                }
            self._json(data)

        elif self.path == "/wake/last-claim":
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                self._json({"error": "wake review is loopback only"}, 403)
                return
            with transition_lock:
                wake = getattr(always_on_listener, "wake", None)
                self._json({"claim": wake.review() if wake else None})

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

        elif self.path.startswith("/clipboard/since/"):
            try:
                idx = int(self.path.split("/")[-1])
                self._json(remote_clipboard_since(idx))
            except Exception:
                self._json({"lines": [], "total": len(state["remote_clipboard_lines"])})

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
        # The transcription adapter takes RAW audio bytes, so it must be handled
        # before _read_body() (which consumes the body as JSON).
        if self.path.split("?", 1)[0] == "/transcribe":
            self._handle_transcribe()
            return

        body = self._read_body()

        if self.path in ("/speak", "/turn-ended", "/speaker/rules/reload", "/conversation/timing"):
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                self._json({"outcome": "refused", "reason": "loopback_only"}, 403)
                return
            service = get_service()
            if self.path == "/speak":
                self._json(service.speak(body, always_on_listener, state["meeting_active"] or state["mode"] == "meeting"))
            elif self.path == '/conversation/timing':
                self._json({'ok': service.mark(body.get('conversation_id'), body.get('stage'), body.get('at'))})
            elif self.path == "/turn-ended":
                self._json(service.turn_ended(body.get("conversation_id") if isinstance(body, dict) else None,
                                            state["meeting_active"] or state["mode"] == "meeting"))
            else:
                self._json({"ok": service.reload(), "rules": service.rules.status()})

        elif self.path == "/audio/keep":
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                self._json({"error": "audio preservation is loopback only"}, 403)
                return
            archive = get_audio_buffer()
            if not archive or not archive.snapshot()["ready"]:
                self._json({"error": "Audio buffer is not enabled or ready"}, 503)
                return
            try:
                job_id = archive.start_keep(body.get("start"), body.get("end"), body.get("feedback", ""))
                self._json({"id": job_id, "state": "running"}, 202)
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
            except RuntimeError as exc:
                self._json({"error": str(exc)}, 409)

        elif self.path == "/mode/clipboard":
            self._handle_mode_start("clipboard", body)

        elif self.path == "/mode/meeting":
            self._handle_mode_start("meeting", body)

        elif self.path == "/mode/on":
            self._handle_mode_start("on", body)

        elif self.path == "/mode/off":
            self._handle_mode_off()

        elif self.path == "/mode/silent":
            # Silent mode is a speaker-service flag, independent of the mic mode and of
            # meeting mode. Web and mobile toggles post here; the voice path sets it directly.
            on = body.get("on", body.get("silent")) if isinstance(body, dict) else None
            source = body.get("source", "web") if isinstance(body, dict) else "web"
            if not isinstance(on, bool):
                self._json({"error": "on must be boolean"}, 400)
            else:
                result = get_service().set_silent(on, source, listener=always_on_listener,
                                                  meeting=state["meeting_active"] or state["mode"] == "meeting")
                if result.get("outcome") == "refused":
                    self._json({"error": result.get("reason")}, 400)
                else:
                    self._json({"ok": True, **result})

        elif self.path == "/calibrate/start":
            if not always_on_listener or not always_on_listener.running:
                self._json({"error": "always-on listener not active -- set mode to 'on' first"}, 400)
            else:
                group = body.get("group", "wake")
                count = body.get("count", 5)
                ok = always_on_listener.start_calibration(group, count)
                self._json({"ok": ok, "group": group, "count": count})

        elif self.path == "/actions/outcome":
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                self._json({"error": "action outcomes are loopback only"}, 403)
                return
            with transition_lock:
                actions = getattr(always_on_listener, 'voice_actions', None)
                if not actions or not actions.enabled:
                    self._json({"error": "Local actions are disabled"}, 409)
                    return
                try:
                    with always_on_listener.wake.lock:
                        accepted = actions.outcome(body.get('id'), body.get('generation'), body.get('outcome'), body.get('receipt'))
                    self._json({"ok": True, "accepted": accepted})
                except ValueError as exc:
                    self._json({"error": str(exc)}, 400)

        elif self.path == "/wake/claim":
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                self._json({"error": "wake claims are loopback only"}, 403)
                return
            with transition_lock:
                wake = getattr(always_on_listener, "wake", None)
                if state["mode"] != "on" or not wake or not wake.enabled or not always_on_listener.running:
                    self._json({"error": "wake capture is not active"}, 409)
                else:
                    claim = wake.claim(actions_version=body.get("actions_version", 0))
                    if claim and not claim.get("action"):
                        if not claim.get('conversation_id'):
                            claim['conversation_id'] = get_service().open('room_mic', always_on_listener, state['meeting_active'], acknowledge=False)
                        claim['voice_reply'] = get_service().contract(claim['conversation_id'])
                        get_service().mark(claim['conversation_id'], 'claimed_at')
                        with wake.lock:
                            wake.last_claim.update(conversation_id=claim["conversation_id"])
                    self._json({"claim": claim, "generation": wake.generation, "mode": state["mode"]})

        elif self.path == "/copy/start":
            if not always_on_listener or not always_on_listener.running:
                self._json({"error": "always-on listener not active"}, 400)
            elif getattr(always_on_listener, "capture_origin", None) in ("wake", "local_action", "followup"):
                self._json({"error": "wake capture is active; finish it with over first"}, 409)
            elif always_on_listener.state == "CAPTURING":
                self._json({"ok": True, "already": True})
            else:
                try:
                    always_on_listener._execute_command("start_copy")
                    self._json({"ok": True})
                except ValueError as exc:
                    self._json({"error": str(exc)}, 409)

        elif self.path == "/copy/stop":
            if not always_on_listener or not always_on_listener.running:
                self._json({"error": "always-on listener not active"}, 400)
            elif getattr(always_on_listener, "capture_origin", None) in ("wake", "local_action", "followup"):
                self._json({"ok": True, "copied": state.get("on_last_copied", ""), "already": True})
            elif always_on_listener.state != "CAPTURING":
                self._json({"ok": True, "copied": state.get("on_last_copied", ""), "already": True})
            else:
                try:
                    always_on_listener.finish_capture()
                    self._json({"ok": True, "copied": state.get("on_last_copied", "")})
                except TimeoutError as exc:
                    self._json({"error": str(exc)}, 503)

        elif self.path == "/calibrate/stop":
            if always_on_listener:
                save = body.get("save", True)
                always_on_listener.stop_calibration(save=save)
                self._json({"ok": True})
            else:
                self._json({"error": "always-on listener not active"}, 400)

        else:
            self._json({"error": "not found"}, 404)

    def _handle_mode_start(self, requested_mode, body):
        if model_preload_status["state"] == "loading":
            self._json({"error": "model_preload_in_progress", "mode": state["mode"]}, 409)
            return
        caller = body.get("caller")
        remote = bool(body.get("remote", False)) if requested_mode == "clipboard" else False

        if requested_mode == "on" and not always_on_enabled():
            self._json({"error": "always_on_disabled"}, 403)
            return

        with locked_transition():
            action = None
            stop_before_start = None
            current_mode = state["mode"]
            current_caller = state["caller"]
            same_caller = caller == current_caller

            if current_mode == "off":
                set_coordination_state(
                    mode=requested_mode,
                    caller=caller,
                    paused_mode=None,
                    paused_caller=None,
                    last_error=None,
                    clipboard_remote=remote if requested_mode == "clipboard" else False,
                )
                action = "start"

            elif current_mode == requested_mode and requested_mode in ("clipboard", "meeting"):
                if same_caller:
                    self._json({"ok": True, "mode": current_mode})
                    return
                self._json(mic_in_use_response(), 409)
                return

            elif current_mode in ("clipboard", "meeting"):
                if (
                    requested_mode == "on"
                    and state["paused_mode"] == "on"
                    and same_caller
                ):
                    stop_before_start = current_mode
                    set_coordination_state(
                        mode="on",
                        caller=caller,
                        paused_mode=None,
                        paused_caller=None,
                        last_error=None,
                        clipboard_remote=False,
                    )
                    action = "start"
                else:
                    self._json(mic_in_use_response(), 409)
                    return

            elif current_mode == "on":
                if requested_mode == "on":
                    if state["paused_mode"] == "on":
                        self._json(mic_in_use_response(), 409)
                        return
                    set_coordination_state(
                        mode="on",
                        caller=caller,
                        paused_mode=None,
                        paused_caller=None,
                        last_error=None,
                        clipboard_remote=False,
                    )
                    self._json({"ok": True, "mode": "on"})
                    return

                stop_before_start = "on"
                set_coordination_state(
                    mode=requested_mode,
                    caller=caller,
                    paused_mode="on",
                    paused_caller=current_caller,
                    last_error=None,
                    clipboard_remote=remote if requested_mode == "clipboard" else False,
                )
                action = "pause_and_start"

            try:
                if stop_before_start == "on":
                    stop_always_on()
                elif stop_before_start == "clipboard":
                    stop_clipboard()
                elif stop_before_start == "meeting":
                    stop_meeting()
                if action:
                    self._start_mode_process(requested_mode, remote)
            except Exception as e:
                if state["mode"] == requested_mode and state["caller"] == caller:
                    set_coordination_state(
                        mode="off",
                        caller=None,
                        paused_mode=None,
                        paused_caller=None,
                        last_error=f"{requested_mode}_start_failed: {e}",
                        clipboard_remote=False,
                    )
                self._json({"error": f"{requested_mode}_start_failed", "message": str(e)}, 500)
                return

            self._json({"ok": True, "mode": requested_mode})

    def _handle_mode_off(self):
        with locked_transition():
            active_mode = state["mode"]
            paused_mode = state["paused_mode"]
            paused_caller = state["paused_caller"]
            set_coordination_state(
                mode="off",
                caller=None,
                paused_mode=None,
                paused_caller=None,
                last_error=None,
                clipboard_remote=False,
            )

            try:
                if active_mode == "meeting":
                    stop_meeting()
                elif active_mode == "clipboard":
                    stop_clipboard()
                elif active_mode == "on":
                    stop_always_on()
            except Exception as e:
                log(f"Stop during /mode/off failed: {e}")

            if paused_mode == "on":
                try:
                    set_coordination_state(
                        mode="on",
                        caller=paused_caller,
                        paused_mode=None,
                        paused_caller=None,
                        last_error=None,
                        clipboard_remote=False,
                    )
                    # First always-on start may load ML models. Holding the transition
                    # lock here keeps the state machine atomic; subsequent starts reuse
                    # the loaded listener.
                    start_always_on(stop_existing=False)
                except Exception as e:
                    error = str(e)
                    set_coordination_state(
                        mode="off",
                        caller=None,
                        paused_mode=None,
                        paused_caller=None,
                        last_error=f"always_on_resume_failed: {error}",
                        clipboard_remote=False,
                    )
                    self._json({
                        "ok": True,
                        "mode": "off",
                        "resumed": False,
                        "resume_error": error,
                    })
                    return

                self._json({"ok": True, "mode": "on", "resumed": True})
                return

            self._json({"ok": True, "mode": "off", "resumed": False})

    def _start_mode_process(self, mode, remote=False):
        if mode == "clipboard":
            start_clipboard(remote=remote, stop_existing=False)
        elif mode == "meeting":
            start_meeting(stop_existing=False)
        elif mode == "on":
            # First always-on start may load ML models. Holding the transition
            # lock here keeps the state machine atomic; subsequent starts reuse
            # the loaded listener.
            start_always_on(stop_existing=False)

    # transcription_2026_09 § Shared contract item 1). Loopback-only; same process
    # and model instance as live capture, serialized by the model's own RLock
    # (AppleWhisperModel.transcribe acquires it). The upload route passes the fleet
    # initial_prompt override and greedy beam=1 (MLX has no beam search); the live
    # wake/content path is untouched.
    TRANSCRIBE_MAX_BYTES = 16 * 1024 * 1024  # spec default cap (10 min / 16 MiB)
    TRANSCRIBE_MIME_EXT = {"audio/mp4": ".m4a", "audio/wav": ".wav"}

    def _handle_transcribe(self):
        from urllib.parse import urlparse, parse_qs

        # Consume the request body FIRST, so every early error response still
        # leaves a clean connection (an unread body would reset the socket). The
        # loopback caller (the daemon) already caps at TRANSCRIBE_MAX_BYTES.
        length = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(length) if length > 0 else b""

        if self.client_address[0] not in ("127.0.0.1", "::1"):
            self._json({"error": "transcribe is loopback only"}, 403)
            return

        # Required profile: a missing/unknown profile is a 400 so a call without
        # the fleet vocabulary cannot silently succeed.
        query = parse_qs(urlparse(self.path).query)
        profile = (query.get("prompt_profile") or [""])[0]
        if profile != "fleet":
            self._json({"error": "prompt_profile=fleet is required"}, 400)
            return

        model = getattr(always_on_listener, "whisper_model", None) if always_on_listener else None
        if model is None:
            self._json({"error": "model not loaded"}, 503)
            return

        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        ext = self.TRANSCRIBE_MIME_EXT.get(content_type)
        if not ext:
            self._json({"error": f"unsupported content-type: {content_type or '<none>'}"}, 400)
            return

        if length <= 0:
            self._json({"error": "empty audio body"}, 400)
            return
        if length > self.TRANSCRIBE_MAX_BYTES:
            self._json({"error": "audio exceeds the transcription cap"}, 413)
            return

        import fleet_vocabulary
        try:
            prompt, corrections, vocab_version = fleet_vocabulary.load_vocabulary()
        except (OSError, ValueError) as exc:
            self._json({"error": f"fleet vocabulary unavailable: {exc}"}, 503)
            return
        if not prompt:
            self._json({"error": "fleet vocabulary is not configured"}, 503)
            return

        import tempfile
        fd, tmp_path = tempfile.mkstemp(suffix=ext)
        os.close(fd)
        try:
            with open(tmp_path, "wb") as handle:
                handle.write(data)
            try:
                duration_s = audio_duration_s(tmp_path)
                if duration_s > 600:
                    self._json({"error": "audio exceeds the ten-minute transcription cap"}, 413)
                    return
                segments, info = model.transcribe(
                    tmp_path, beam_size=1, language="en", vad_filter=False,
                    initial_prompt=prompt,
                )
                seg_list = [
                    {"start": float(getattr(s, "start", 0.0)),
                     "end": float(getattr(s, "end", 0.0)),
                     "text": str(getattr(s, "text", ""))}
                    for s in segments
                ]
            except Exception as exc:  # noqa: BLE001 - undecodable audio is a 400
                self._json({"error": f"could not decode audio: {exc}"}, 400)
                return

            raw_text = "".join(seg["text"] for seg in seg_list).strip()
            text = fleet_vocabulary.apply_corrections(raw_text, corrections)
            self._json({
                "text": text,
                "language": str(getattr(info, "language", "en") or "en"),
                "duration_s": float(duration_s),
                "model": os.environ.get("MIC_WHISPER_MODEL", "small.en"),
                "compute": os.environ.get(
                    "MIC_WHISPER_COMPUTE_TYPE",
                    "float16" if os.environ.get("MIC_WHISPER_DEVICE") == "cuda" else "int8",
                ),
                "segments": seg_list,
                "vocabulary_version": vocab_version,
            })
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length:
            try:
                return json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                return {}
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
        get_service().close()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    get_audio_buffer()  # Startup expiry and cleanup continue even when mic is Off.
    bind_host = get_bind_host()
    server = ThreadingHTTPServer((bind_host, PORT), MicHandler)
    threading.Thread(target=get_service().start, name="resident-speaker-start", daemon=True).start()
    log(f"Mic Server running on http://{bind_host}:{PORT}")
    log(f"Platform: {sys.platform}")
    log("Modes: clipboard, meeting, on, off")

    # Optional auto-start mode via env var
    start_mode = os.environ.get("MIC_SERVER_START_MODE", "").strip().lower()
    if start_mode == "off":
        model_preload_status.update(state="loading", error=None, vocabulary_version=None)
        # HTTP status stays responsive while the existing model warms up.
        threading.Thread(target=preload_always_on, daemon=True).start()
    elif start_mode in ("on", "clipboard", "meeting"):
        log(f"Auto-starting in '{start_mode}' mode (MIC_SERVER_START_MODE)")
        def _autostart():
            try:
                if start_mode == "on":
                    if not always_on_enabled():
                        log("Auto-start skipped: MIC_ALWAYS_ON_ENABLED is not true")
                        return
                    with locked_transition():
                        set_coordination_state(
                            mode="on",
                            caller=auto_start_caller(),
                            paused_mode=None,
                            paused_caller=None,
                            last_error=None,
                            clipboard_remote=False,
                        )
                        start_always_on(stop_existing=False)
                elif start_mode == "clipboard":
                    with locked_transition():
                        set_coordination_state(
                            mode="clipboard",
                            caller=auto_start_caller(),
                            paused_mode=None,
                            paused_caller=None,
                            last_error=None,
                            clipboard_remote=False,
                        )
                        start_clipboard(stop_existing=False)
                elif start_mode == "meeting":
                    with locked_transition():
                        set_coordination_state(
                            mode="meeting",
                            caller=auto_start_caller(),
                            paused_mode=None,
                            paused_caller=None,
                            last_error=None,
                            clipboard_remote=False,
                        )
                        start_meeting(stop_existing=False)
            except Exception as e:
                log(f"Auto-start failed: {e}")
        threading.Thread(target=_autostart, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        handle_signal(None, None)


if __name__ == "__main__":
    main()
