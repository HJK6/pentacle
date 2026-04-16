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
from audio_device import find_mic_index
from clipboard import copy_to_clipboard

# --- Config ---
DEVICE_INDEX = find_mic_index()
CAPTURE_SR = 48000
ASR_SR = 16000
CHANNELS = 1
BLOCK_MS = 100            # 100ms chunks for VAD (gives 1600 samples at 16kHz)
BLOCKSIZE = CAPTURE_SR * BLOCK_MS // 1000  # 4800

VAD_THRESHOLD = 0.45
MIN_SILENCE_MS = 600      # Silence to end utterance
MIN_SPEECH_MS = 200       # Min speech to process
COOLDOWN_SECS = 1.0       # Cooldown between commands

WHISPER_MODEL = "small.en"

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
        self.vad_model = None
        self.whisper_model = None
        self.audio_q = queue.Queue(maxsize=200)
        self.running = False
        self._stream = None

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

        # Calibration state
        self.cal_group = None
        self.cal_samples = []
        self.cal_target = 5
        self.cal_prev_state = None

        # Callback for mic_server integration
        self.on_event = None

    def load_models(self):
        from silero_vad import load_silero_vad
        from faster_whisper import WhisperModel

        self._log("Loading Silero VAD...")
        self.vad_model = load_silero_vad()
        if self.vad_model is None or not callable(self.vad_model):
            self._log("VAD model failed to load, retrying...")
            self.vad_model = load_silero_vad()
        self._log("Loading Whisper model...")
        self.whisper_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        self._log("Models ready.")

    def start(self):
        if self.running:
            return
        self.running = True
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

        self._worker = threading.Thread(target=self._process_loop, daemon=True)
        self._worker.start()

        self._transcribe_thread = threading.Thread(target=self._transcribe_worker, daemon=True)
        self._transcribe_thread.start()

        self._stream = sd.InputStream(
            device=DEVICE_INDEX,
            samplerate=CAPTURE_SR,
            channels=CHANNELS,
            blocksize=BLOCKSIZE,
            dtype='float32',
            callback=self._audio_callback,
        )
        self._stream.start()
        self._log("Always-on listener started")

    def stop(self):
        if not self.running:
            return
        self.running = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if hasattr(self, '_worker') and self._worker.is_alive():
            self._worker.join(timeout=3)
        if hasattr(self, '_transcribe_thread') and self._transcribe_thread.is_alive():
            self._transcribe_thread.join(timeout=3)
        self._log("Always-on listener stopped")

    def _audio_callback(self, indata, frames, time_info, status):
        if not self.running:
            return
        mono48 = indata[:, 0].astype(np.float32)
        mono16 = resample_poly(mono48, up=1, down=3).astype(np.float32)
        if self.audio_q.full():
            try:
                self.audio_q.get_nowait()
            except queue.Empty:
                pass
        self.audio_q.put_nowait(mono16)

    def _process_loop(self):
        import torch

        VAD_FRAME = 512
        vad_ring = RingBuffer(4096)

        while self.running:
            try:
                chunk = self.audio_q.get(timeout=0.5)
            except queue.Empty:
                if self.state == "AWAKE" and time.time() - self.awake_since > AWAKE_TIMEOUT:
                    self._log("[awake] Timed out (silence) -- back to LISTENING")
                    self.state = "LISTENING"
                    self._emit("state", "LISTENING")
                continue

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
                frame_tensor = torch.from_numpy(frame)
                if self.vad_model is None:
                    self._log("[error] VAD model is None, reloading...")
                    self.load_models()
                    continue
                confidence = self.vad_model(frame_tensor, ASR_SR).item()
                is_speech = confidence > VAD_THRESHOLD

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
                            try:
                                self._utterance_q.put_nowait(audio)
                            except queue.Full:
                                self._log("[warn] transcription backed up, dropping utterance")
                    else:
                        self.silence_count = 0

                    if self.speech_buf.length / ASR_SR > 15:
                        audio = self.speech_buf.read_all()
                        self.speech_buf.clear()
                        try:
                            self._utterance_q.put_nowait(audio)
                        except queue.Full:
                            self._log("[warn] transcription backed up, dropping utterance")

    def _transcribe_worker(self):
        while self.running:
            try:
                audio = self._utterance_q.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._handle_utterance(audio)
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

    def _handle_utterance(self, audio):
        duration = len(audio) / ASR_SR
        if duration < 0.3:
            return

        text = self._transcribe(audio)
        if not text:
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
                self._emit("capturing", text)
                if len(self.captured_texts) >= MAX_CAPTURE_SEGMENTS:
                    self._log("[warn] capture limit reached, auto-flushing to clipboard")
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

    def _execute_command(self, cmd):
        self._log(f"[command] {cmd}")
        self._emit("command", cmd)

        if cmd == "start_copy":
            self.state = "CAPTURING"
            self.captured_texts = []
            self._log("[mode] Capturing for clipboard...")
            self._emit("state", "CAPTURING")

        elif cmd == "end_copy":
            if self.captured_texts:
                full_text = " ".join(self.captured_texts).strip()
                copy_to_clipboard(full_text)
                self._log(f"[copied] {full_text}")
                self._emit("copied", full_text)
            else:
                self._log("[skip] Nothing captured")
            self.state = "LISTENING"
            self.captured_texts = []
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

            segments, _ = self.whisper_model.transcribe(
                tmp, beam_size=1, language="en",
                vad_filter=False, condition_on_previous_text=False,
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
