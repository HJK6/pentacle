#!/usr/bin/env python3
"""
Meeting Recorder - VAD-driven real-time transcription.

Uses Silero-VAD to detect speech boundaries, then transcribes each
utterance with faster-whisper. Shows live partial text as you speak,
commits final text when you pause.

Saves full transcript to mic-server/transcripts/<timestamp>.txt
"""

import os
import sys
import time
import threading
import queue
import tempfile
import numpy as np
import sounddevice as sd
import soundfile as sf
from datetime import datetime
from scipy.signal import resample_poly
from audio_device import find_mic_index

# --- Config ---
DEVICE_INDEX = find_mic_index()
CAPTURE_SR = 48000
ASR_SR = 16000
CHANNELS = 1
BLOCK_MS = 100
BLOCKSIZE = CAPTURE_SR * BLOCK_MS // 1000  # 4800

# VAD settings
VAD_THRESHOLD = 0.5
SPEECH_PAD_MS = 300
MIN_SILENCE_MS = 500
MIN_SPEECH_MS = 250

# Transcription
WHISPER_MODEL = "base.en"
PARTIAL_INTERVAL = 0.8
MAX_UTTERANCE_SECS = 30

TRANSCRIPT_DIR = os.path.join(os.path.dirname(__file__), "transcripts")

# --- Globals ---
running = True
transcript_lines = []
current_partial = ""
status_callback = None    # Set by mic_server for live updates


def get_timestamp():
    return datetime.now().strftime("%H:%M:%S")


class MeetingRecorder:
    def __init__(self):
        self.vad_model = None
        self.whisper_model = None
        self.audio_q = queue.Queue()
        self.running = False
        self.transcript = []
        self.current_partial = ""
        self.session_file = None
        self.start_time = None
        self._stream = None

        # VAD state
        self.speech_active = False
        self.speech_buf = np.zeros(0, dtype=np.float32)
        self.silence_count = 0
        self.speech_count = 0
        self.last_partial_time = 0

        # Pre-speech buffer
        self.pre_speech_chunks = []
        self.pre_speech_max = int(SPEECH_PAD_MS / BLOCK_MS)

    def load_models(self):
        from silero_vad import load_silero_vad
        from faster_whisper import WhisperModel

        self._log("Loading Silero VAD...")
        self.vad_model = load_silero_vad()
        self._log("Loading Whisper model...")
        self.whisper_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        self._log("Models ready.")

    def start(self):
        if self.running:
            return
        self.running = True
        self.transcript = []
        self.current_partial = ""
        self.start_time = datetime.now()
        self.speech_active = False
        self.speech_buf = np.zeros(0, dtype=np.float32)
        self.silence_count = 0
        self.speech_count = 0
        self.pre_speech_chunks = []

        os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
        ts = self.start_time.strftime("%Y%m%d_%H%M%S")
        self.session_file = os.path.join(TRANSCRIPT_DIR, f"{ts}.txt")

        self._log(f"Recording started -> {self.session_file}")

        self._worker = threading.Thread(target=self._process_loop, daemon=True)
        self._worker.start()

        self._stream = sd.InputStream(
            device=DEVICE_INDEX,
            samplerate=CAPTURE_SR,
            channels=CHANNELS,
            blocksize=BLOCKSIZE,
            dtype='float32',
            callback=self._audio_callback,
        )
        self._stream.start()

    def stop(self):
        if not self.running:
            return None
        self.running = False

        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        if len(self.speech_buf) > 0:
            self._final_transcribe(self.speech_buf.copy())
            self.speech_buf = np.zeros(0, dtype=np.float32)

        self._save_transcript()
        self._log("Recording stopped.")
        return self.session_file

    def _audio_callback(self, indata, frames, time_info, status):
        if not self.running:
            return
        mono48 = indata[:, 0].astype(np.float32)
        mono16 = resample_poly(mono48, up=1, down=3).astype(np.float32)
        self.audio_q.put(mono16)

    def _process_loop(self):
        import torch

        VAD_FRAME = 512
        vad_buf = np.zeros(0, dtype=np.float32)

        while self.running:
            try:
                chunk = self.audio_q.get(timeout=0.5)
            except queue.Empty:
                continue

            if self.speech_active:
                self.speech_buf = np.concatenate([self.speech_buf, chunk])
            else:
                self.pre_speech_chunks.append(chunk.copy())
                if len(self.pre_speech_chunks) > self.pre_speech_max:
                    self.pre_speech_chunks.pop(0)

            vad_buf = np.concatenate([vad_buf, chunk])
            while len(vad_buf) >= VAD_FRAME:
                frame = vad_buf[:VAD_FRAME]
                vad_buf = vad_buf[VAD_FRAME:]

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
                        if self.speech_count >= 8:
                            self.speech_active = True
                            self.silence_count = 0
                            self.last_partial_time = time.time()
                            if self.pre_speech_chunks:
                                self.speech_buf = np.concatenate(self.pre_speech_chunks)
                            else:
                                self.speech_buf = np.zeros(0, dtype=np.float32)
                            self.pre_speech_chunks = []
                            self._log("[speech start]")
                    else:
                        self.speech_count = 0
                else:
                    if not is_speech:
                        self.silence_count += 1
                        if self.silence_count >= 15:
                            self.speech_active = False
                            self.speech_count = 0
                            self.silence_count = 0
                            audio = self.speech_buf.copy()
                            self.speech_buf = np.zeros(0, dtype=np.float32)
                            self.current_partial = ""
                            self.pre_speech_chunks = []

                            threading.Thread(
                                target=self._final_transcribe,
                                args=(audio,),
                                daemon=True
                            ).start()
                    else:
                        self.silence_count = 0

            if self.speech_active:
                now = time.time()
                utterance_secs = len(self.speech_buf) / ASR_SR
                if now - self.last_partial_time >= PARTIAL_INTERVAL and utterance_secs > 0.5:
                    self.last_partial_time = now
                    max_samples = int(3.2 * ASR_SR)
                    partial_audio = self.speech_buf[-max_samples:]
                    threading.Thread(
                        target=self._partial_transcribe,
                        args=(partial_audio.copy(),),
                        daemon=True
                    ).start()

                if utterance_secs > MAX_UTTERANCE_SECS:
                    audio = self.speech_buf.copy()
                    self.speech_buf = np.zeros(0, dtype=np.float32)
                    self.current_partial = ""
                    threading.Thread(
                        target=self._final_transcribe,
                        args=(audio,),
                        daemon=True
                    ).start()

    def _partial_transcribe(self, audio):
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                tmp = f.name
                sf.write(tmp, audio, ASR_SR)

            segments, _ = self.whisper_model.transcribe(
                tmp, beam_size=1, language="en",
                vad_filter=False, condition_on_previous_text=False,
            )
            text = " ".join(seg.text.strip() for seg in segments).strip()
            if text:
                self.current_partial = text
                self._emit_partial(text)
        except Exception:
            pass
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    def _final_transcribe(self, audio):
        duration = len(audio) / ASR_SR
        if duration < 0.3:
            return

        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                tmp = f.name
                sf.write(tmp, audio, ASR_SR)

            segments, _ = self.whisper_model.transcribe(
                tmp, beam_size=3, language="en",
                word_timestamps=True, vad_filter=False,
                condition_on_previous_text=False,
            )
            text = " ".join(seg.text.strip() for seg in segments).strip()

            if text:
                ts = get_timestamp()
                line = f"[{ts}] {text}"
                self.transcript.append(line)
                self.current_partial = ""
                self._emit_final(line)
                self._append_to_file(line)
        except Exception as e:
            self._log(f"[error] {e}")
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    def _append_to_file(self, line):
        if self.session_file:
            with open(self.session_file, "a") as f:
                f.write(line + "\n")

    def _save_transcript(self):
        if self.session_file and self.transcript:
            with open(self.session_file, "w") as f:
                f.write(f"Meeting Transcript - {self.start_time.strftime('%Y-%m-%d %H:%M')}\n")
                f.write("=" * 60 + "\n\n")
                for line in self.transcript:
                    f.write(line + "\n")
            self._log(f"Transcript saved: {self.session_file}")

    def _log(self, msg):
        ts = get_timestamp()
        line = f"[{ts}] {msg}"
        if status_callback:
            status_callback("log", line)
        else:
            print(line, flush=True)

    def _emit_partial(self, text):
        print(f"\r  > {text}", end="", flush=True)
        if status_callback:
            status_callback("partial", text)

    def _emit_final(self, line):
        print(f"\r{line}          ", flush=True)
        if status_callback:
            status_callback("final", line)

    def get_state(self):
        return {
            "running": self.running,
            "speech_active": self.speech_active,
            "transcript_lines": len(self.transcript),
            "current_partial": self.current_partial,
            "session_file": self.session_file,
            "duration": (time.time() - self.start_time.timestamp()) if self.start_time and self.running else 0,
        }


# --- Standalone mode ---
if __name__ == "__main__":
    import signal as sig_mod
    recorder = MeetingRecorder()
    recorder.load_models()

    def handle_signal(sig, frame):
        print("\n[stopping]")
        recorder.stop()
        sys.exit(0)

    sig_mod.signal(sig_mod.SIGINT, handle_signal)
    sig_mod.signal(sig_mod.SIGTERM, handle_signal)

    recorder.start()
    print("[ready] Recording... (Ctrl+C to stop)\n")

    while recorder.running:
        time.sleep(0.1)
