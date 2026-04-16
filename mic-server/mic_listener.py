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
import time
import signal
import tempfile
import threading
import numpy as np
import sounddevice as sd
import soundfile as sf
from faster_whisper import WhisperModel
from audio_device import find_mic_index
from clipboard import copy_to_clipboard

# --- Config ---
DEVICE_INDEX = find_mic_index()
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
running = True


def load_whisper():
    global model
    print("[init] Loading Whisper model...")
    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    print("[init] Whisper ready.")


def transcribe(audio_data):
    """Transcribe audio numpy array and copy result to clipboard."""
    global state

    duration = len(audio_data) / SAMPLE_RATE
    if duration < MIN_RECORDING_SECS:
        print(f"[skip] Recording too short ({duration:.1f}s)")
        state = State.IDLE
        return

    print(f"[transcribe] Processing {duration:.1f}s of audio...")

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp_path = f.name
        sf.write(tmp_path, audio_data, SAMPLE_RATE)

    try:
        segments, info = model.transcribe(tmp_path, beam_size=5, language="en")
        text = " ".join(seg.text.strip() for seg in segments).strip()

        if text:
            copy_to_clipboard(text)
            print(f"[copied] {text}")
        else:
            print("[empty] No speech detected.")
    except Exception as e:
        print(f"[error] Transcription failed: {e}")
    finally:
        os.unlink(tmp_path)

    state = State.IDLE


def audio_callback(indata, frames, time_info, status):
    """Called for each audio block from the stream."""
    global state, audio_buffer, silence_start, recording_start

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
                threading.Thread(target=transcribe, args=(audio_data,), daemon=True).start()
        else:
            silence_start = None

        if elapsed > MAX_RECORDING_SECS:
            print(f"[max] Hit {MAX_RECORDING_SECS}s limit, stopping")
            state = State.TRANSCRIBING
            audio_data = np.concatenate(audio_buffer, axis=0).flatten()
            audio_buffer = []
            threading.Thread(target=transcribe, args=(audio_data,), daemon=True).start()


def main():
    global running

    def handle_signal(sig, frame):
        global running
        print("\n[exit] Shutting down...")
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    whisper_thread = threading.Thread(target=load_whisper, daemon=True)
    whisper_thread.start()

    dev = sd.query_devices(DEVICE_INDEX)
    print(f"[init] Mic Listener starting on device {DEVICE_INDEX} ({dev['name']})")
    print(f"[init] Unmute to record, mute to stop and transcribe")
    print(f"[init] Text will be copied to clipboard")
    print()

    try:
        with sd.InputStream(
            device=DEVICE_INDEX,
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            blocksize=BLOCK_SIZE,
            dtype='float32',
            callback=audio_callback,
        ):
            whisper_thread.join()
            print("[ready] Listening... (Ctrl+C to quit)\n")
            while running:
                time.sleep(0.1)
    except Exception as e:
        print(f"[fatal] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
