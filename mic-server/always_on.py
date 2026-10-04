#!/usr/bin/env python3
"""
Always-On Mic - VAD-gated command listener with wake word.

Continuously listens via Silero-VAD. When speech is detected, transcribes
the utterance with faster-whisper and checks for triggers/commands.

State machine:
  LISTENING -> hears wake word -> AWAKE
  AWAKE     -> hears command ("start copy", "copy this") -> CAPTURING
  AWAKE     -> 10s timeout with no command -> LISTENING
  CAPTURING -> accumulates utterance text -> hears "over" / "end copy" -> clipboard -> LISTENING
"""

import os
import sys
import json
import re
import time
import signal
import threading
import queue
import tempfile
import numpy as np
import sounddevice as sd
import soundfile as sf
from datetime import datetime
from scipy.signal import resample_poly
from audio_device import resolve_mic_device
from clipboard import copy_to_clipboard

# --- Config ---
CAPTURE_SR = 48000
ASR_SR = 16000
CHANNELS = 1
BLOCK_MS = 100            # 100ms chunks for VAD (gives 1600 samples at 16kHz)
BLOCKSIZE = CAPTURE_SR * BLOCK_MS // 1000  # 4800

VAD_THRESHOLD = 0.45
MIN_SILENCE_MS = 600      # Silence to end utterance
MIN_SPEECH_MS = 200       # Min speech to process
COOLDOWN_SECS = 1.0       # Cooldown between commands

WHISPER_MODEL = os.environ.get("MIC_WHISPER_MODEL", "small.en")
WHISPER_BACKEND = os.environ.get("MIC_WHISPER_BACKEND", "faster-whisper")
WHISPER_DEVICE = os.environ.get("MIC_WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.environ.get("MIC_WHISPER_COMPUTE_TYPE", "float16" if WHISPER_DEVICE == "cuda" else "int8")
WHISPER_CPU_THREADS = int(os.environ.get("MIC_WHISPER_CPU_THREADS", "4"))
if WHISPER_CPU_THREADS < 1:
    raise ValueError("MIC_WHISPER_CPU_THREADS must be positive")
_FINISH_CAPTURE = object()

# --- Calibration file ---
CALIBRATION_FILE = os.path.join(os.path.dirname(__file__), "calibration.json")

# Default phrases (before calibration) — generic wake words
DEFAULT_WAKE = {
    "hey pentacle", "hi pentacle", "hello pentacle",
    "hey bart", "hey bartimaeus", "hey bartimeus",
    "hi bart", "hi bartimaeus", "hi bartimeus",
    "hello bart", "hello bartimaeus", "hello bartimeus",
    "yo bart", "yo bartimaeus",
    "hey bar", "a bart", "a bartimaeus",
    "hey board", "hey bard", "hey bort", "hey bert",
    "hi board", "hi bard", "hi bort", "hi bert",
    "hello board", "hello bard", "hello bort", "hello bert",
    "hey part", "hi part", "hey bought", "hey bot",
    "hey boy", "hi boy", "hey barty", "hi barty",
}
DEFAULT_COMMANDS = {
    "start_copy": {"start copy", "start copying", "copy this", "begin copy", "begin copying"},
    "end_copy": {"over", "end copy", "stop copy", "stop copying", "end copying", "that's it"},
    "start_meeting": {"start meeting", "start recording", "begin meeting"},
    "end_meeting": {"end meeting", "stop meeting", "stop recording", "end recording"},
}

AWAKE_TIMEOUT = 10.0  # seconds to wait for a command after wake word

# Calibratable phrase groups -- keys match calibration.json
PHRASE_GROUPS = {
    "wake": "wake words",
    "start_copy": "start copy command",
    "end_copy": "end copy / over command",
    "start_meeting": "start meeting command",
    "end_meeting": "end meeting command",
}


def load_calibration():
    """Load calibrated phrases from disk, merge with defaults."""
    cal = {}
    if os.path.exists(CALIBRATION_FILE):
        with open(CALIBRATION_FILE) as f:
            cal = json.load(f)

    wake = set(DEFAULT_WAKE)
    wake.update(cal.get("wake", []))

    commands = {}
    for cmd, defaults in DEFAULT_COMMANDS.items():
        commands[cmd] = set(defaults)
        commands[cmd].update(cal.get(cmd, []))

    return wake, commands


def split_and_clean(phrases):
    """Split multi-phrase utterances on sentence boundaries and clean up."""
    result = set()
    for phrase in phrases:
        parts = re.split(r'[.,;!?\n]+', phrase)
        for part in parts:
            cleaned = part.strip().lower()
            if len(cleaned) >= 2:
                result.add(cleaned)
    return result


def save_calibration(group, phrases):
    """Save newly calibrated phrases for a group, merging with existing."""
    cal = {}
    if os.path.exists(CALIBRATION_FILE):
        with open(CALIBRATION_FILE) as f:
            cal = json.load(f)
    cleaned = split_and_clean(phrases)
    existing = set(cal.get(group, []))
    existing.update(cleaned)
    cal[group] = sorted(existing)
    with open(CALIBRATION_FILE, "w") as f:
        json.dump(cal, f, indent=2)


# Load on import
WAKE_WORDS, COMMANDS = load_calibration()


def reload_calibration():
    """Reload calibration from disk into globals."""
    global WAKE_WORDS, COMMANDS
    WAKE_WORDS, COMMANDS = load_calibration()


def normalize(text):
    """Normalize transcription for command matching."""
    text = text.lower().strip()
    text = re.sub(r'[.,!?;:"\'\-]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _edit_distance(a, b):
    """Simple Levenshtein distance."""
    if len(a) < len(b):
        return _edit_distance(b, a)
    if len(b) == 0:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (ca != cb)))
        prev = curr
    return prev[-1]


def match_wake(text):
    """Check if text contains a wake word, with fuzzy matching."""
    normed = normalize(text)
    for wake in WAKE_WORDS:
        if wake in normed:
            return True
    words = normed.split()
    for n in (2, 3, 1):
        for i in range(max(1, len(words) - n + 1)):
            chunk = " ".join(words[i:i+n])
            for wake in WAKE_WORDS:
                dist = _edit_distance(chunk, wake)
                threshold = max(2, len(wake) // 3)
                if dist <= threshold:
                    return True
    return False


def match_command(text):
    """Check if text matches any command. Returns command name or None."""
    normed = normalize(text)
    for cmd, aliases in COMMANDS.items():
        for alias in aliases:
            if cmd.startswith("end_"):
                if normed == alias:
                    return cmd
            else:
                if alias in normed:
                    return cmd
    return None


class RingBuffer:
    """Pre-allocated numpy ring buffer. No allocations after __init__."""

    def __init__(self, capacity: int, dtype=np.float32):
        self.buf = np.zeros(capacity, dtype=dtype)
        self.capacity = capacity
        self.write_pos = 0
        self.length = 0

    def append(self, data: np.ndarray):
        n = len(data)
        if n >= self.capacity:
            self.buf[:] = data[-self.capacity:]
            self.write_pos = 0
            self.length = self.capacity
            return
        end = self.write_pos + n
        if end <= self.capacity:
            self.buf[self.write_pos:end] = data
        else:
            first = self.capacity - self.write_pos
            self.buf[self.write_pos:] = data[:first]
            self.buf[:n - first] = data[first:]
        self.write_pos = end % self.capacity
        self.length = min(self.length + n, self.capacity)

    def _read_start(self) -> int:
        return (self.write_pos - self.length) % self.capacity

    def read_all(self) -> np.ndarray:
        if self.length == 0:
            return np.zeros(0, dtype=self.buf.dtype)
        start = self._read_start()
        if start + self.length <= self.capacity:
            return self.buf[start:start + self.length].copy()
        first = self.capacity - start
        result = np.empty(self.length, dtype=self.buf.dtype)
        result[:first] = self.buf[start:]
        result[first:] = self.buf[:self.length - first]
        return result

    def consume(self, n: int) -> np.ndarray:
        if n > self.length:
            n = self.length
        start = self._read_start()
        result = np.empty(n, dtype=self.buf.dtype)
        end = start + n
        if end <= self.capacity:
            result[:] = self.buf[start:end]
        else:
            first = self.capacity - start
            result[:first] = self.buf[start:]
            result[first:] = self.buf[:n - first]
        self.length -= n
        return result

    def clear(self):
        self.write_pos = 0
        self.length = 0


def get_timestamp():
    return datetime.now().strftime("%H:%M:%S")


class AlwaysOnListener:
    def __init__(self):
        from wake_capture import WakeCaptures
        from audio_buffer import get_audio_buffer
        self.audio_buffer = get_audio_buffer()
        self.wake = WakeCaptures(os.environ.get("MIC_WAKE_CAPTURE", "").lower() in ("1", "true", "yes"))
        self.capture_origin = None
        self._recognition_epoch = 0
        self._recognition_until = 0.0
        from voice_actions import VoiceActions
        self.voice_actions = VoiceActions(self)
        self.vad_model = None
        self.whisper_model = None
        self.audio_q = queue.Queue(maxsize=200)
        self.running = False
        self._stream = None
        self._stop_event = threading.Event()
        self._device_selection = None
        self._health_last_emit = 0.0
        self._last_callback_monotonic = time.monotonic()
        self._callback_stall_reopened = False
        self._callback_watchdog_ready = False
        self._health_flatline_started_at = None
        self._stream_status_reopened = False
        self._flatline_restarted = False
        self._reopen_lock = threading.Lock()
        self._capture_lock = threading.Lock()
        self._finishing_capture = False
        self._finish_done = threading.Event()
        self._dll_handles = []

        # VAD state
        self.speech_active = False
        self.speech_buf = RingBuffer(ASR_SR * 17)
        self.silence_count = 0
        self.speech_count = 0
        self.pre_speech_buf = RingBuffer(ASR_SR * 1)
        self._utterance_q = queue.Queue(maxsize=8)

        # Command state
        self.state = "LISTENING"
        self.captured_texts = []
        self.last_command_time = 0
        self.awake_since = 0

        # Meeting state
        self.meeting_active = False
        self.on_meeting_start = None
        self.on_meeting_stop = None

        # Mode voice phrases (silent / meeting), matched in-service before Bart routing.
        # mode_phrases() returns the rules Modes section; on_silent(on) toggles silent mode.
        self.mode_phrases = None
        self.on_silent = None

        # Calibration state
        self.cal_group = None
        self.cal_samples = []
        self.cal_target = 5
        self.cal_prev_state = None

        # Callback for mic_server integration
        self.on_event = None
        self.on_capture_end = None

    def load_models(self):
        from silero_vad import load_silero_vad
        if sys.platform == "win32":
            for directory in os.environ.get("MIC_CUDA_DLL_DIRS", "").split(os.pathsep):
                if directory:
                    self._dll_handles.append(os.add_dll_directory(directory))
        self._log("Loading Silero VAD...")
        self.vad_model = load_silero_vad()
        if self.vad_model is None or not callable(self.vad_model):
            self._log("VAD model failed to load, retrying...")
            self.vad_model = load_silero_vad()
        self._log("Loading Whisper model...")
        if WHISPER_BACKEND == "mlx":
            from apple_asr import AppleWhisperModel
            self.whisper_model = AppleWhisperModel(WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE_TYPE)
        elif WHISPER_BACKEND == "faster-whisper":
            from faster_whisper import WhisperModel
            model_options = dict(device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE)
            if WHISPER_DEVICE == "cpu":
                model_options["cpu_threads"] = WHISPER_CPU_THREADS
            self.whisper_model = WhisperModel(WHISPER_MODEL, **model_options)
        else:
            raise ValueError(f"Unsupported ASR backend: {WHISPER_BACKEND}")
        self._log(f"ASR: {WHISPER_MODEL}, {WHISPER_DEVICE}, {WHISPER_COMPUTE_TYPE}")
        self._log("Models ready.")

    def start(self):
        if self.running:
            return
        with self.wake.lock:
            self.wake.invalidate()
            self.voice_actions.reset_dialogue()
            self.capture_origin = None
        self.running = True
        self._stop_event = threading.Event()
        stop_event = self._stop_event
        self._last_callback_monotonic = time.monotonic()
        self._callback_stall_reopened = False
        self._callback_watchdog_ready = False
        self.state = "LISTENING"
        self.captured_texts = []
        self.awake_since = 0
        self.speech_active = False
        self.speech_buf.clear()
        self.silence_count = 0
        self.speech_count = 0
        self.pre_speech_buf.clear()

        while not self.audio_q.empty():
            try:
                self.audio_q.get_nowait()
            except queue.Empty:
                break
        while not self._utterance_q.empty():
            try:
                self._utterance_q.get_nowait()
            except queue.Empty:
                break

        self._worker = threading.Thread(target=self._process_loop, args=(stop_event,), daemon=True)
        self._worker.start()

        self._transcribe_thread = threading.Thread(target=self._transcribe_worker, args=(stop_event,), daemon=True)
        self._transcribe_thread.start()

        self._open_stream()
        self._callback_watchdog_ready = True
        self.voice_actions.start()
        self._log("Always-on listener started")

    def stop(self):
        with self.wake.lock:
            self.wake.invalidate()
            self.voice_actions.reset_dialogue()
            self.state = "LISTENING"
            self.captured_texts = []
            self.capture_origin = None
        if not self.running:
            return
        self.running = False
        self._stop_event.set()
        with self._reopen_lock:
            if self._stream:
                self._stream.stop()
                self._stream.close()
                self._stream = None
        if hasattr(self, '_worker') and self._worker.is_alive():
            self._worker.join(timeout=3)
        if hasattr(self, '_transcribe_thread') and self._transcribe_thread.is_alive():
            self._transcribe_thread.join(timeout=3)
        if self.audio_buffer:
            try:
                self.audio_buffer.flush()
            except Exception as exc:
                self._log(f"Audio archive flush failed: {exc}")
        self._log("Always-on listener stopped")

    def _audio_callback(self, indata, frames, time_info, status):
        if not self.running:
            return
        self._last_callback_monotonic = time.monotonic()
        self._callback_stall_reopened = False
        self._emit_callback_health(indata, status)
        mono48 = indata[:, 0].astype(np.float32)
        if self.audio_buffer:
            self.audio_buffer.submit(mono48)
        epoch, suppressed = self.recognition_stamp()
        if suppressed:
            return
        mono16 = resample_poly(mono48, up=1, down=3).astype(np.float32)
        with self._capture_lock:
            if self._finishing_capture:
                return
            if self.audio_q.full():
                try:
                    self.audio_q.get_nowait()
                except queue.Empty:
                    pass
            self.audio_q.put_nowait((epoch, mono16))

    def recognition_stamp(self):
        if self._recognition_until and time.monotonic() >= self._recognition_until:
            self._recognition_until = 0.0
            self._recognition_epoch += 1
        return self._recognition_epoch, bool(self._recognition_until)

    def suppress_recognition_until(self, deadline):
        self._recognition_epoch += 1
        self._recognition_until = deadline

    def finish_capture(self, timeout=45):
        """Finalize after audio already accepted by the callback and ASR queues."""
        with self._capture_lock:
            if self.state != "CAPTURING":
                return
            if not self._finishing_capture:
                self._finishing_capture = True
                self._finish_done.clear()
                self.audio_q.put(_FINISH_CAPTURE, timeout=2)
        if not self._finish_done.wait(timeout):
            raise TimeoutError("Microphone is still finishing transcription")


    def _open_stream(self):
        self._device_selection = resolve_mic_device()
        self._emit_device_health(stream_open=False)
        if self._device_selection.get("index") is None:
            raise RuntimeError(self._device_selection.get("selected_device") or "No usable input device")
        self._stream = sd.InputStream(
            device=self._device_selection["index"],
            samplerate=CAPTURE_SR,
            channels=CHANNELS,
            blocksize=BLOCKSIZE,
            dtype='float32',
            callback=self._audio_callback,
        )
        self._stream.start()
        self._emit_device_health(stream_open=True)

    def _emit_device_health(self, stream_open, stream_status=None):
        selection = self._device_selection or {}
        self._emit("audio_health", {
            "selected_device": selection.get("selected_device"),
            "preferred_device_name": selection.get("preferred_device_name"),
            "preferred_present": selection.get("preferred_present"),
            "disallowed_device": selection.get("disallowed_device"),
            "stream_open": stream_open,
            "stream_status": stream_status,
        })

    def _emit_callback_health(self, indata, status):
        now = time.time()
        peak, rms = self._audio_levels(indata)
        if peak > 1e-6:
            self._health_flatline_started_at = None
            flatline_seconds = 0.0
        else:
            if self._health_flatline_started_at is None:
                self._health_flatline_started_at = now
            flatline_seconds = now - self._health_flatline_started_at

        status_text = str(status) if status else None
        if status_text and not self._stream_status_reopened:
            self._stream_status_reopened = True
            self._request_reopen("stream_status")
        if flatline_seconds >= 10.0 and not self._flatline_restarted:
            self._flatline_restarted = True
            self._request_reopen("flatline")

        if status_text or now - self._health_last_emit >= 0.5:
            self._health_last_emit = now
            selection = self._device_selection or {}
            self._emit("audio_health", {
                "selected_device": selection.get("selected_device"),
                "preferred_device_name": selection.get("preferred_device_name"),
                "preferred_present": selection.get("preferred_present"),
                "disallowed_device": selection.get("disallowed_device"),
                "stream_open": True,
                "peak": peak,
                "rms": rms,
                "stream_status": status_text,
            })

    def _audio_levels(self, indata):
        peak = 0.0
        total = 0.0
        count = 0
        for raw in indata.reshape(-1):
            sample = float(raw)
            magnitude = -sample if sample < 0 else sample
            if magnitude > peak:
                peak = magnitude
            total += sample * sample
            count += 1
        if count == 0:
            return 0.0, 0.0
        return peak, float(np.sqrt(total / count))

    def _check_callback_stall(self, stop_event):
        # A dead callback cannot report its own failure. The existing idle loop
        # requests one recovery per stall; any fresh frame (even silence) rearms it.
        if (self.running and self._callback_watchdog_ready
                and stop_event is self._stop_event and not stop_event.is_set()
                and not self._callback_stall_reopened
                and time.monotonic() - self._last_callback_monotonic > 3.0):
            self._callback_stall_reopened = True
            self._request_reopen("callbacks_stale", stop_event)

    def _request_reopen(self, reason, stop_event=None):
        stop_event = self._stop_event if stop_event is None else stop_event
        threading.Thread(target=self._reopen_stream, args=(reason, stop_event), daemon=True).start()

    def _reopen_stream(self, reason, stop_event=None):
        stop_event = self._stop_event if stop_event is None else stop_event
        if not self.running:
            return
        with self._reopen_lock:
            if not self.running or stop_event is not self._stop_event or stop_event.is_set():
                return
            old_stream = self._stream
            self._stream = None
            if old_stream:
                try:
                    old_stream.stop()
                    old_stream.close()
                except Exception as exc:
                    self._log(f"[warn] Failed to close stream during audio self-heal: {exc}")
            self._emit_device_health(stream_open=False, stream_status=reason)
            try:
                self._open_stream()
                self._log(f"[heal] Reopened preferred input after {reason}")
            except Exception as exc:
                self._emit_device_health(stream_open=False, stream_status=str(exc))
                self._log(f"[error] Failed to reopen preferred input after {reason}: {exc}")

    def _process_loop(self, stop_event):
        import torch

        VAD_FRAME = 512
        vad_ring = RingBuffer(4096)
        process_epoch = self.recognition_stamp()[0]
        accepted_samples = vad_samples = 0
        last_speech_frame_end = None

        while not stop_event.is_set():
            try:
                chunk = self.audio_q.get(timeout=0.5)
            except queue.Empty:
                self._check_callback_stall(stop_event)
                if self.state == "AWAKE" and time.time() - self.awake_since > AWAKE_TIMEOUT:
                    self._log("[awake] Timed out (silence) -- back to LISTENING")
                    self.state = "LISTENING"
                    self._emit("state", "LISTENING")
                continue

            if chunk is _FINISH_CAPTURE:
                # FIFO barrier: no later callbacks enter the queue until finalized.
                tail = self.speech_buf.read_all() if self.speech_active else self.pre_speech_buf.read_all()
                # Retain active speech and partial VAD onset, including short or
                # quiet speech that has not reached six consecutive frames.
                # An inactive silence-only tail must never reach ASR.
                has_tail_speech = last_speech_frame_end is not None and (
                    last_speech_frame_end > accepted_samples - len(tail)
                )
                if len(tail) and (self.speech_active or has_tail_speech):
                    self._utterance_q.put((process_epoch, tail))
                self.speech_buf.clear()
                self.pre_speech_buf.clear()
                self.speech_active = False
                self.speech_count = self.silence_count = 0
                vad_ring.clear()
                accepted_samples = vad_samples = 0
                last_speech_frame_end = None
                self._utterance_q.put(_FINISH_CAPTURE)
                continue

            chunk_epoch, chunk = chunk if isinstance(chunk, tuple) else (self.recognition_stamp()[0], chunk)
            current_epoch, suppressed = self.recognition_stamp()
            if suppressed or chunk_epoch != current_epoch:
                continue
            if process_epoch != current_epoch:
                self.speech_buf.clear()
                self.pre_speech_buf.clear()
                self.speech_active = False
                self.speech_count = self.silence_count = 0
                vad_ring.clear()
                accepted_samples = vad_samples = 0
                last_speech_frame_end = None
                process_epoch = current_epoch

            accepted_samples += len(chunk)

            if self.state == "AWAKE" and time.time() - self.awake_since > AWAKE_TIMEOUT:
                self._log("[awake] Timed out -- back to LISTENING")
                self.state = "LISTENING"
                self._emit("state", "LISTENING")

            if self.speech_active:
                self.speech_buf.append(chunk)
            else:
                self.pre_speech_buf.append(chunk)

            vad_ring.append(chunk)
            while vad_ring.length >= VAD_FRAME:
                frame = vad_ring.consume(VAD_FRAME)
                vad_samples += VAD_FRAME
                frame_tensor = torch.from_numpy(frame)
                if self.vad_model is None:
                    self._log("[error] VAD model is None, reloading...")
                    self.load_models()
                    continue
                with torch.inference_mode():
                    confidence = self.vad_model(frame_tensor, ASR_SR).item()
                is_speech = confidence > VAD_THRESHOLD
                if is_speech:
                    last_speech_frame_end = vad_samples

                if not self.speech_active:
                    if is_speech:
                        self.speech_count += 1
                        if self.speech_count >= 6:
                            self.speech_active = True
                            self.silence_count = 0
                            pre = self.pre_speech_buf.read_all()
                            self.speech_buf.clear()
                            if len(pre) > 0:
                                self.speech_buf.append(pre)
                            self.pre_speech_buf.clear()
                            last_speech_frame_end = None
                    else:
                        self.speech_count = 0
                else:
                    if not is_speech:
                        self.silence_count += 1
                        if self.silence_count >= 18:
                            self.speech_active = False
                            self.speech_count = 0
                            self.silence_count = 0
                            audio = self.speech_buf.read_all()
                            self.speech_buf.clear()
                            self.pre_speech_buf.clear()
                            last_speech_frame_end = None
                            try:
                                self._utterance_q.put_nowait((process_epoch, audio))
                            except queue.Full:
                                self._log("[warn] transcription backed up, dropping utterance")
                    else:
                        self.silence_count = 0

                    if self.speech_buf.length / ASR_SR > 15:
                        audio = self.speech_buf.read_all()
                        self.speech_buf.clear()
                        try:
                            self._utterance_q.put_nowait((process_epoch, audio))
                        except queue.Full:
                            self._log("[warn] transcription backed up, dropping utterance")

    def _transcribe_worker(self, stop_event):
        while not stop_event.is_set():
            try:
                audio = self._utterance_q.get(timeout=1.0)
            except queue.Empty:
                continue
            if stop_event.is_set():
                return
            if audio is _FINISH_CAPTURE:
                try:
                    with self.wake.lock:
                        if not stop_event.is_set() and self.state == "CAPTURING":
                            self._execute_command("end_copy")
                finally:
                    with self._capture_lock:
                        self._finishing_capture = False
                    self._finish_done.set()
                continue
            try:
                audio_epoch, audio = audio if isinstance(audio, tuple) else (self.recognition_stamp()[0], audio)
                current_epoch, suppressed = self.recognition_stamp()
                if suppressed or audio_epoch != current_epoch:
                    continue
                self._handle_utterance(audio, stop_event=stop_event, recognition_epoch=audio_epoch)
            except Exception as e:
                self._log(f"[error] utterance handler: {e}")

    # --- Calibration ---

    def start_calibration(self, group, count=5):
        if group not in PHRASE_GROUPS:
            self._log(f"[cal] Unknown group: {group}. Valid: {list(PHRASE_GROUPS.keys())}")
            return False
        self.cal_prev_state = self.state
        self.state = "CALIBRATING"
        self.cal_group = group
        self.cal_samples = []
        self.cal_target = count
        self._log(f"[cal] Calibrating '{PHRASE_GROUPS[group]}' -- say the phrase {count} times")
        self._emit("state", "CALIBRATING")
        self._emit("cal_start", {"group": group, "target": count})
        return True

    def stop_calibration(self, save=True):
        if self.state != "CALIBRATING":
            return
        if save and self.cal_samples:
            normalized = [normalize(s) for s in self.cal_samples]
            unique = list(set(normalized))
            save_calibration(self.cal_group, unique)
            reload_calibration()
            self._log(f"[cal] Saved {len(unique)} phrases for '{PHRASE_GROUPS[self.cal_group]}': {unique}")
            self._emit("cal_done", {"group": self.cal_group, "phrases": unique})
        else:
            self._log("[cal] Calibration cancelled, nothing saved")
            self._emit("cal_done", {"group": self.cal_group, "phrases": []})
        self.state = self.cal_prev_state or "LISTENING"
        self.cal_group = None
        self.cal_samples = []
        self._emit("state", self.state)

    def get_calibration_status(self):
        return {
            "active": self.state == "CALIBRATING",
            "group": self.cal_group,
            "collected": len(self.cal_samples),
            "target": self.cal_target,
            "samples": list(self.cal_samples),
        }

    def _handle_utterance(self, audio, stop_event=None, recognition_epoch=None):
        if len(audio) / ASR_SR < 0.3:
            return
        generation = self.wake.generation
        epoch, suppressed = self.recognition_stamp()
        if suppressed or (recognition_epoch is not None and recognition_epoch != epoch):
            return
        text = self._transcribe(audio)
        with self.wake.lock:
            if self.recognition_stamp() != (epoch, False):
                return
            if not text or generation != self.wake.generation or (stop_event and stop_event.is_set()):
                return
            self._handle_text(text)

    def _handle_text(self, text):
        if self.recognition_stamp()[1]:
            return
        pending = self.voice_actions.waiting()
        if self.voice_actions.pending and normalize(text) == 'cancel':
            self.voice_actions.reset_dialogue()
            return
        if self.wake.enabled and (self.state == "LISTENING" or self.capture_origin == 'followup'):
            # Fresh wake preempts a direct answer, including a partial answer capture.
            match = re.match(r"^\s*hey(?:[^\w]+)bart\b(?!['’]\w)", text, re.IGNORECASE)
            local_match = re.match(r"^\s*hey(?:[^\w]+)pentacle\b(?!['’]\w)", text, re.IGNORECASE) if self.voice_actions.enabled and self.voice_actions.policy == 'separate' else None
            if local_match:
                match = local_match
            if match:
                self.voice_actions.reset_dialogue()
                if not self.wake.can_start():
                    return
                self.capture_origin = "local_action" if local_match or (self.voice_actions.enabled and self.voice_actions.policy == "shared") else "wake"
                self.state = "CAPTURING"
                tail = text[match.end():].lstrip(" \t.,!?:;—–-")
                self.captured_texts = [tail] if tail else []
                self._emit("state", "CAPTURING")
                return
            if self.state == 'LISTENING':
                if pending and match_command(text) != 'end_copy':
                    self.capture_origin = 'followup'
                    self.followup_id = pending['id']
                    self.state = 'CAPTURING'
                    self.captured_texts = [text]
                    self._emit('state', 'CAPTURING')
                return

        self._log(f"[heard] {text}")

        if self.state == "CALIBRATING":
            self.cal_samples.append(text)
            remaining = self.cal_target - len(self.cal_samples)
            self._log(f"[cal] Sample {len(self.cal_samples)}/{self.cal_target}: \"{text}\"" +
                       (f" -- {remaining} more" if remaining > 0 else " -- done!"))
            self._emit("cal_sample", {"text": text, "count": len(self.cal_samples), "target": self.cal_target})
            if len(self.cal_samples) >= self.cal_target:
                self.stop_calibration(save=True)
            return

        if self.state == "LISTENING":
            if match_wake(text):
                self.state = "AWAKE"
                self.awake_since = time.time()
                self._log("[wake] Heard wake word -- listening for command...")
                self._emit("state", "AWAKE")
                cmd = match_command(text)
                if cmd:
                    self._execute_command(cmd)
            return

        if self.state == "AWAKE":
            if time.time() - self.awake_since > AWAKE_TIMEOUT:
                self._log("[awake] Timed out waiting for command -- back to LISTENING")
                self.state = "LISTENING"
                self._emit("state", "LISTENING")
                return
            cmd = match_command(text)
            if cmd:
                self._execute_command(cmd)
            else:
                self._log(f"[awake] Didn't match a command: {text}")
                if self.meeting_active:
                    self.state = "MEETING"
                    self._emit("state", "MEETING")
            return

        if self.state == "CAPTURING":
            MAX_CAPTURE_SEGMENTS = 60
            cmd = match_command(text)
            if cmd == "end_copy":
                self._execute_command(cmd)
            else:
                self.captured_texts.append(text)
                self._log(f"[capturing] {text}")
                self._emit("wake_capturing" if self.capture_origin in ("wake", "local_action", "followup") else "capturing", text)
                if len(self.captured_texts) >= MAX_CAPTURE_SEGMENTS:
                    self._log("[warn] capture limit reached, auto-flushing to clipboard")
                    if self.capture_origin == 'followup':
                        self.voice_actions.reset_dialogue()
                    else:
                        self._execute_command("end_copy")
            return

        if self.state == "MEETING":
            if match_wake(text):
                self.state = "AWAKE"
                self.awake_since = time.time()
                self._log("[wake] Heard wake word during meeting -- listening for command...")
                self._emit("state", "AWAKE")
                cmd = match_command(text)
                if cmd:
                    self._execute_command(cmd)
            return

    def match_mode(self, text):
        """Match a captured wake line against the rules Modes phrases (silent/meeting).

        Returns a command name handled inside the mic service, or None. Matching is
        substring on normalized text so near misses ("silent movie", "meeting notes")
        do not switch a mode. The wake word and "over" are still required to reach here.
        """
        if not self.mode_phrases:
            return None
        try:
            modes = self.mode_phrases()
        except Exception:
            return None
        if not isinstance(modes, dict):
            return None
        norm = normalize(text)
        def hit(group, key):
            return any(normalize(p) and normalize(p) in norm for p in modes.get(group, {}).get(key, []))
        if hit('silent', 'on_phrases'):
            return 'silent_on'
        if hit('silent', 'off_phrases'):
            return 'silent_off'
        if hit('meeting', 'on_phrases'):
            return 'start_meeting'
        if hit('meeting', 'off_phrases'):
            return 'end_meeting'
        return None

    def _execute_command(self, cmd):
        with self.wake.lock:
            return self._execute_command_locked(cmd)

    def _execute_command_locked(self, cmd):
        if cmd == 'start_copy' and self.recognition_stamp()[1]:
            raise ValueError('Local voice reply is playing; wait before recording.')
        if cmd == "start_copy" and self.capture_origin in ("wake", "local_action", "followup"):
            raise ValueError("wake capture is active; finish it with over first")
        self._log(f"[command] {cmd}")
        self._emit("command", cmd)

        if cmd == "start_copy":
            self.voice_actions.reset_dialogue()
            self.capture_origin = "manual"
            self.state = "CAPTURING"
            self.captured_texts = []
            self._log("[mode] Capturing for clipboard...")
            self._emit("state", "CAPTURING")

        elif cmd == "end_copy":
            if self.captured_texts:
                full_text = " ".join(self.captured_texts).strip()
                if self.capture_origin in ("wake", "local_action", "followup"):
                    origin = self.capture_origin
                    # Release CAPTURING before the acknowledgement takes the recognition fence.
                    self.state = "LISTENING"
                    mode_cmd = self.match_mode(full_text) if origin == 'wake' else None
                    if mode_cmd:
                        # A silent/meeting phrase is handled in-service and never sent to Bart;
                        # no conversation is opened and no acknowledgement plays. The listener
                        # stays in LISTENING so "Hey Bart ... over" still reaches Bart (and ends
                        # the meeting) while a meeting records.
                        self._emit('mode_phrase', {'command': mode_cmd, 'text': full_text})
                        if mode_cmd in ('silent_on', 'silent_off') and self.on_silent:
                            self.on_silent(mode_cmd == 'silent_on')
                        elif mode_cmd == 'start_meeting':
                            self.meeting_active = True
                            if self.on_meeting_start:
                                self.on_meeting_start()
                        elif mode_cmd == 'end_meeting':
                            self.meeting_active = False
                            if self.on_meeting_stop:
                                self.on_meeting_stop()
                    else:
                        metadata = self.on_capture_end() if self.on_capture_end else {}
                        if origin == 'followup':
                            self.voice_actions.submit_answer(full_text, self.followup_id)
                        elif origin == 'local_action':
                            self.voice_actions.submit(full_text, self.wake.generation, metadata=metadata)
                        else:
                            self.wake.complete(full_text, metadata=metadata)
                    self._emit("wake_completed", {"generation": self.wake.generation})
                else:
                    copy_to_clipboard(full_text)
                    self._log(f"[copied] {full_text}")
                    self._emit("copied", full_text)
            else:
                self._log("[skip] Nothing captured")
            self.state = "LISTENING"
            self.captured_texts = []
            self.capture_origin = None
            self._emit("state", "LISTENING")

        elif cmd == "start_meeting":
            self.state = "MEETING"
            self.meeting_active = True
            self._log("[mode] Meeting recording started")
            self._emit("state", "MEETING")
            if self.on_meeting_start:
                self.on_meeting_start()

        elif cmd == "end_meeting":
            self.meeting_active = False
            self._log("[mode] Meeting recording stopped")
            if self.on_meeting_stop:
                self.on_meeting_stop()
            self.state = "LISTENING"
            self._emit("state", "LISTENING")

    def _transcribe(self, audio):
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                tmp = f.name
                sf.write(tmp, audio, ASR_SR)

            # A proper-name hint helps short wake utterances without accepting
            # alternate wake phrases or biasing subsequent message content.
            wake_recognition = self.wake.enabled and (
                (self.state == "LISTENING" and not self.voice_actions.pending) or
                (self.state == "CALIBRATING" and self.cal_group == "wake")
            )
            hints = {"hotwords": "Hey Bart, Hey Pentacle" if self.voice_actions.enabled and self.voice_actions.policy == "separate" else "Hey Bart"} if wake_recognition else {}
            segments, _ = self.whisper_model.transcribe(
                tmp, beam_size=1, language="en",
                vad_filter=False, condition_on_previous_text=False, **hints,
            )
            text = " ".join(seg.text.strip() for seg in segments).strip()
            return text
        except Exception as e:
            self._log(f"[error] {e}")
            return ""
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    def _log(self, msg):
        ts = get_timestamp()
        line = f"[{ts}] {msg}"
        if self.on_event:
            self.on_event("log", line)
        else:
            print(line, flush=True)

    def _emit(self, kind, data):
        if self.on_event:
            self.on_event(kind, data)

    def get_state(self):
        return {
            "running": self.running,
            "listener_state": self.state,
            "captured_count": len(self.captured_texts),
            "speech_active": self.speech_active,
        }


# --- Standalone mode ---
if __name__ == "__main__":
    listener = AlwaysOnListener()
    listener.load_models()

    def handle_signal(sig, frame):
        print("\n[stopping]")
        listener.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    listener.start()
    print("[ready] Always-on listener active. Say a wake word then a command.\n")

    while listener.running:
        time.sleep(0.1)
