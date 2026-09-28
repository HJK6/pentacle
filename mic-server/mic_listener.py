#!/usr/bin/env python3
"""
Mic Listener - Always-on voice-to-clipboard.

Monitors the mic's audio level. When you unmute and speak, it records.
When you mute again (or silence for a few seconds), it transcribes
and copies the text to your clipboard.

State machine:
  IDLE -> audio above threshold -> RECORDING
  RECORDING -> audio below threshold for SILENCE_DURATION -> TRANSCRIBING
  TRANSCRIBING -> text copied to clipboard -> IDLE
"""

import sys
import os
import json
import re
import time
import signal
import tempfile
import threading
from datetime import datetime, timezone
import numpy as np
import sounddevice as sd
import soundfile as sf
from faster_whisper import WhisperModel
from audio_device import resolve_mic_device
from clipboard import copy_to_clipboard

# --- Config ---
SAMPLE_RATE = 48000
CHANNELS = 1
BLOCK_SIZE = 4800         # 100ms chunks at 48kHz

# Thresholds
NOISE_FLOOR = 0.00015
SPEECH_THRESHOLD = 0.005
UNMUTE_THRESHOLD = 0.001

# Timing
SILENCE_DURATION = 1.5
MIN_RECORDING_SECS = 0.5
MAX_RECORDING_SECS = 120

# Whisper
WHISPER_MODEL = "base.en"
REMOTE_CLIPBOARD = bool(os.environ.get("MIC_REMOTE_CLIPBOARD"))
# Keep in sync with always_on.DEFAULT_COMMANDS["end_copy"] without importing
# always_on here; that module loads calibration/audio dependencies at import time.
CLIPBOARD_END_PHRASES = (
    "stop copying",
    "end copying",
    "stop copy",
    "end copy",
    "that's it",
    "over",
)
MAX_STOP_WORD_STRIP_ITERATIONS = 16
_END_COPY_RE = re.compile(
    r"(?:^|\s+)\b("
    + "|".join(re.escape(phrase) for phrase in CLIPBOARD_END_PHRASES)
    + r")\b[\s.,!?;:]*\Z",
    re.IGNORECASE,
)

# --- State ---
class State:
    IDLE = "IDLE"
    RECORDING = "RECORDING"
    TRANSCRIBING = "TRANSCRIBING"

state = State.IDLE
audio_buffer = []
silence_start = None
recording_start = None
model = None
model_ready = threading.Event()
model_error = None
running = True
transcription_thread = None
device_selection = None
last_health_emit = 0.0
flatline_started_at = None
stream_reopen_requested = None
stream_status_reopened = False
flatline_restarted = False


def emit_audio_health(payload):
    data = {"type": "audio_health"}
    data.update(payload)
    print(json.dumps(data), flush=True)


def emit_device_health(selection, stream_open=False, stream_status=None):
    emit_audio_health({
        "selected_device": selection.get("selected_device"),
        "preferred_device_name": selection.get("preferred_device_name"),
        "preferred_present": selection.get("preferred_present"),
        "disallowed_device": selection.get("disallowed_device"),
        "stream_open": stream_open,
        "stream_status": stream_status,
    })


def request_reopen(reason):
    global stream_reopen_requested
    if stream_reopen_requested is None:
        stream_reopen_requested = reason


def maybe_emit_callback_health(indata, status):
    global last_health_emit, flatline_started_at, stream_status_reopened, flatline_restarted
    now = time.time()
    peak, rms = audio_levels(indata)
    if peak > 1e-6:
        flatline_started_at = None
        flatline_seconds = 0.0
    else:
        if flatline_started_at is None:
            flatline_started_at = now
        flatline_seconds = now - flatline_started_at

    status_text = str(status) if status else None
    if status_text and not stream_status_reopened:
        stream_status_reopened = True
        request_reopen("stream_status")
    if flatline_seconds >= 10.0 and not flatline_restarted:
        flatline_restarted = True
        request_reopen("flatline")

    if status_text or now - last_health_emit >= 0.5:
        last_health_emit = now
        payload = {
            "stream_open": True,
            "peak": peak,
            "rms": rms,
            "stream_status": status_text,
        }
        if device_selection:
            payload.update({
                "selected_device": device_selection.get("selected_device"),
                "preferred_device_name": device_selection.get("preferred_device_name"),
                "preferred_present": device_selection.get("preferred_present"),
                "disallowed_device": device_selection.get("disallowed_device"),
            })
        emit_audio_health(payload)


def audio_levels(indata):
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


def strip_end_copy_phrase(text):
    """Strip a trailing clipboard stop phrase from transcribed text."""
    current = text
    for _ in range(MAX_STOP_WORD_STRIP_ITERATIONS):
        stripped = _END_COPY_RE.sub("", current).rstrip()
        if stripped == current or not stripped:
            return stripped
        current = stripped
    return current


def emit_remote_clipboard(text):
    print(json.dumps({
        "type": "remote_clipboard",
        "text": text,
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }), flush=True)


def emit_end_session(reason):
    print(json.dumps({
        "type": "end_session",
        "reason": reason,
    }), flush=True)


def load_whisper():
    global model, model_error
    print("[init] Loading Whisper model...")
    try:
        model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        print("[init] Whisper ready.")
    except Exception as e:
        model_error = e
        print(f"[error] Whisper load failed: {e}")
    finally:
        model_ready.set()


def ensure_model_ready(timeout=60):
    if model is not None:
        return True
    if not model_ready.wait(timeout=timeout):
        print("[error] Whisper model did not become ready before timeout")
        return False
    if model is None:
        print(f"[error] Whisper model unavailable: {model_error}")
        return False
    return True


def transcribe(audio_data):
    """Transcribe audio numpy array and copy result to clipboard."""
    global state, running

    duration = len(audio_data) / SAMPLE_RATE
    if duration < MIN_RECORDING_SECS:
        print(f"[skip] Recording too short ({duration:.1f}s)")
        state = State.IDLE
        return

    print(f"[transcribe] Processing {duration:.1f}s of audio...")

    if not ensure_model_ready():
        state = State.IDLE
        return

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp_path = f.name
        sf.write(tmp_path, audio_data, SAMPLE_RATE)

    try:
        segments, info = model.transcribe(tmp_path, beam_size=5, language="en")
        text = " ".join(seg.text.strip() for seg in segments).strip()
        stripped_text = strip_end_copy_phrase(text)
        stop_word_detected = stripped_text != text

        if stop_word_detected:
            print("[stop-word] stripped trailing phrase")

        text = stripped_text
        if text:
            if REMOTE_CLIPBOARD:
                emit_remote_clipboard(text)
                print(f"[remote] {text}")
            else:
                copy_to_clipboard(text)
                print(f"[copied] {text}")
        elif stop_word_detected:
            print("[empty] all stop-word, suppressed")
        else:
            print("[empty] No speech detected.")

        if stop_word_detected:
            emit_end_session("stop_word")
            running = False
    except Exception as e:
        print(f"[error] Transcription failed: {e}")
    finally:
        os.unlink(tmp_path)

    state = State.IDLE


def start_transcription(audio_data):
    global transcription_thread
    transcription_thread = threading.Thread(target=transcribe, args=(audio_data,))
    transcription_thread.start()


def wait_for_transcription(timeout=60):
    thread = transcription_thread
    if thread and thread.is_alive():
        thread.join(timeout=timeout)
        if thread.is_alive():
            print("[warn] Transcription still running after shutdown wait")


def finalize_pending_recording(reason):
    global state, audio_buffer
    if state != State.RECORDING or not audio_buffer:
        return
    print(f"[stopped] Finalizing recording on {reason}")
    state = State.TRANSCRIBING
    audio_data = np.concatenate(audio_buffer, axis=0).flatten()
    audio_buffer = []
    transcribe(audio_data)


def audio_callback(indata, frames, time_info, status):
    """Called for each audio block from the stream."""
    global state, audio_buffer, silence_start, recording_start

    maybe_emit_callback_health(indata, status)

    if status:
        print(f"[warn] {status}")

    rms = np.sqrt(np.mean(indata ** 2))

    if state == State.IDLE:
        if rms > UNMUTE_THRESHOLD:
            state = State.RECORDING
            audio_buffer = [indata.copy()]
            silence_start = None
            recording_start = time.time()
            print(f"[recording] Mic unmuted (RMS={rms:.5f})")

    elif state == State.RECORDING:
        audio_buffer.append(indata.copy())
        elapsed = time.time() - recording_start

        if rms < NOISE_FLOOR * 3:
            if silence_start is None:
                silence_start = time.time()
            elif time.time() - silence_start >= SILENCE_DURATION:
                print(f"[stopped] Mic muted after {elapsed:.1f}s")
                state = State.TRANSCRIBING
                audio_data = np.concatenate(audio_buffer, axis=0).flatten()
                audio_buffer = []
                start_transcription(audio_data)
        else:
            silence_start = None

        if elapsed > MAX_RECORDING_SECS:
            print(f"[max] Hit {MAX_RECORDING_SECS}s limit, stopping")
            state = State.TRANSCRIBING
            audio_data = np.concatenate(audio_buffer, axis=0).flatten()
            audio_buffer = []
            start_transcription(audio_data)


def main():
    global running, device_selection, stream_reopen_requested

    def handle_signal(sig, frame):
        global running
        print("\n[exit] Shutting down...")
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print("[init] Mic Listener starting")
    print(f"[init] Unmute to record, mute to stop and transcribe")
    if REMOTE_CLIPBOARD:
        print(f"[init] Text will be emitted for remote clipboard")
    else:
        print(f"[init] Text will be copied to clipboard")
    print()

    try:
        threading.Thread(target=load_whisper, daemon=True).start()
        reopen_attempts = 0
        while running:
            device_selection = resolve_mic_device()
            emit_device_health(device_selection, stream_open=False)
            if device_selection.get("index") is None:
                raise RuntimeError(device_selection.get("selected_device") or "No usable input device")
            dev = sd.query_devices(device_selection["index"])
            print(f"[init] Opening device {device_selection['index']} ({dev['name']})")
            stream_reopen_requested = None
            with sd.InputStream(
                device=device_selection["index"],
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                blocksize=BLOCK_SIZE,
                dtype='float32',
                callback=audio_callback,
            ):
                emit_device_health(device_selection, stream_open=True)
                print("[ready] Listening... (Ctrl+C to quit)\n")
                while running and stream_reopen_requested is None:
                    time.sleep(0.1)
            emit_device_health(device_selection, stream_open=False, stream_status=stream_reopen_requested)
            if not running or stream_reopen_requested is None or reopen_attempts >= 1:
                break
            reopen_attempts += 1
            print(f"[heal] Reopening preferred input after {stream_reopen_requested}")
        finalize_pending_recording("shutdown")
        wait_for_transcription()
    except Exception as e:
        selection = device_selection or resolve_mic_device()
        emit_device_health(selection, stream_open=False, stream_status=str(e))
        print(f"[fatal] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
