"""POST /transcribe adapter route (spec § Shared contract item 1 / C4).

The route reads raw audio, requires prompt_profile=fleet, calls the SAME model
instance as live capture with the fleet initial_prompt override at beam=1, applies
the correction map, and returns {text, language, duration_s, model, compute,
segments, vocabulary_version}. A stubbed model stands in for MLX so the route is
tested without weights; the C4 test proves the route serializes on the model's own
lock against a concurrent live-path inference (no second model, no bypass).
"""

import json
import os
import tempfile
import threading
import time
import unittest
import unittest.mock as mock
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import mic_server


class FakeSegment:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text


class FakeWhisperModel:
    """Mimics AppleWhisperModel: transcribe acquires a shared RLock during work."""

    def __init__(self, text="hello Thirdhost", hold_s=0.0, raise_exc=None):
        self.lock = threading.RLock()
        self.calls = []
        self.hold_intervals = []
        self.text = text
        self.hold_s = hold_s
        self.raise_exc = raise_exc

    def transcribe(self, audio, *, beam_size=1, language="en", vad_filter=False,
                   condition_on_previous_text=False, hotwords=None, initial_prompt=None):
        with self.lock:
            t0 = time.monotonic()
            if self.hold_s:
                time.sleep(self.hold_s)
            self.calls.append({
                "audio": audio, "beam_size": beam_size, "language": language,
                "vad_filter": vad_filter, "initial_prompt": initial_prompt,
            })
            t1 = time.monotonic()
            self.hold_intervals.append((t0, t1))
            if self.raise_exc:
                raise self.raise_exc
        info = SimpleNamespace(language="en", duration=1.5)
        return iter([FakeSegment(0.0, 1.5, self.text)]), info


class TranscribeRouteTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        vf = os.path.join(self.dir, "vocab.txt")
        cf = os.path.join(self.dir, "corr.json")
        with open(vf, "w", encoding="utf-8") as h:
            h.write("Bartimaeus, Samplehost, Thirdhost")
        with open(cf, "w", encoding="utf-8") as h:
            h.write(json.dumps({"toth": "Thirdhost"}))
        self.env = mock.patch.dict(os.environ, {
            "MIC_VOCABULARY_FILE": vf,
            "MIC_NAME_CORRECTIONS": cf,
            "MIC_WHISPER_MODEL": "large-v3",
            "MIC_WHISPER_COMPUTE_TYPE": "float16",
        }, clear=False)
        self.env.start()
        mock.patch.object(mic_server, "audio_duration_s", return_value=1.5).start()

        self.model = FakeWhisperModel()
        mic_server.always_on_listener = SimpleNamespace(whisper_model=self.model, running=True)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), mic_server.MicHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        mic_server.always_on_listener = None
        mock.patch.stopall()

    def post_audio(self, path, data=b"AUDIODATA", content_type="audio/mp4"):
        req = urllib.request.Request(
            self.base + path, data=data, method="POST",
            headers={"Content-Type": content_type},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            return e.code, (json.loads(body) if body else {})

    def test_missing_fleet_vocabulary_fails_before_model(self):
        os.unlink(os.environ['MIC_VOCABULARY_FILE'])
        status, _ = self.post_audio('/transcribe?prompt_profile=fleet')
        self.assertEqual(status, 503)
        self.assertEqual(self.model.calls, [])

    def test_duration_cap_rejects_before_inference(self):
        with mock.patch.object(mic_server, 'audio_duration_s', return_value=600.01, create=True):
            status, _ = self.post_audio('/transcribe?prompt_profile=fleet')
        self.assertEqual(status, 413)
        self.assertEqual(self.model.calls, [])

    def test_happy_path_applies_fleet_prompt_and_corrections(self):
        self.model.text = "hello toth"  # correction map should fix -> Thirdhost
        status, body = self.post_audio("/transcribe?prompt_profile=fleet")
        self.assertEqual(status, 200)
        self.assertEqual(body["text"], "hello Thirdhost")
        self.assertEqual(body["model"], "large-v3")
        self.assertEqual(body["compute"], "float16")
        self.assertTrue(body["vocabulary_version"].startswith("fleet-"))
        self.assertEqual(len(body["segments"]), 1)
        # The fleet initial_prompt override and greedy beam=1 were passed through.
        call = self.model.calls[-1]
        self.assertEqual(call["beam_size"], 1)
        self.assertFalse(call["vad_filter"])
        self.assertEqual(call["initial_prompt"], "Bartimaeus, Samplehost, Thirdhost")

    def test_missing_profile_is_400(self):
        status, body = self.post_audio("/transcribe")
        self.assertEqual(status, 400)
        self.assertEqual(self.model.calls, [])

    def test_unknown_profile_is_400(self):
        status, _ = self.post_audio("/transcribe?prompt_profile=other")
        self.assertEqual(status, 400)

    def test_unsupported_mime_is_400(self):
        status, _ = self.post_audio("/transcribe?prompt_profile=fleet", content_type="audio/ogg")
        self.assertEqual(status, 400)

    def test_empty_body_is_400(self):
        status, _ = self.post_audio("/transcribe?prompt_profile=fleet", data=b"")
        self.assertEqual(status, 400)

    def test_no_model_is_503(self):
        mic_server.always_on_listener = None
        status, _ = self.post_audio("/transcribe?prompt_profile=fleet")
        self.assertEqual(status, 503)

    def test_too_large_is_413(self):
        big = b"x" * (mic_server.MicHandler.TRANSCRIBE_MAX_BYTES + 1)
        status, _ = self.post_audio("/transcribe?prompt_profile=fleet", data=big)
        self.assertEqual(status, 413)
        self.assertEqual(self.model.calls, [])

    def test_decode_failure_is_400(self):
        self.model.raise_exc = RuntimeError("bad audio")
        status, _ = self.post_audio("/transcribe?prompt_profile=fleet")
        self.assertEqual(status, 400)

    def test_wav_mime_accepted(self):
        status, body = self.post_audio("/transcribe?prompt_profile=fleet", content_type="audio/wav")
        self.assertEqual(status, 200)

    def test_C4_route_serializes_with_live_path_on_the_shared_lock(self):
        """The upload route and a concurrent live-path inference never overlap:
        both acquire the SAME model lock, so their hold intervals are disjoint."""
        self.model.hold_s = 0.15

        route_done = threading.Event()

        def run_route():
            self.post_audio("/transcribe?prompt_profile=fleet")
            route_done.set()

        t = threading.Thread(target=run_route)
        t.start()
        # Give the route a moment to enter the request, then fire a live-path call
        # on this thread directly against the same model.
        time.sleep(0.03)
        segs, _ = self.model.transcribe(b"live", beam_size=1, language="en", vad_filter=False)
        list(segs)
        t.join(timeout=5)
        self.assertTrue(route_done.is_set())

        self.assertEqual(len(self.model.hold_intervals), 2)
        (a0, a1), (b0, b1) = sorted(self.model.hold_intervals)
        # Disjoint: the earlier interval ends before the later one begins.
        self.assertLessEqual(a1, b0 + 1e-6, "route and live-path inference overlapped")


if __name__ == "__main__":
    unittest.main()


class AudioDurationProbeTest(unittest.TestCase):
    def test_real_wav_duration_including_silence(self):
        import wave
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'silent.wav'
            with wave.open(str(path), 'wb') as out:
                out.setnchannels(1)
                out.setsampwidth(1)
                out.setframerate(8000)
                out.writeframes(b'\x80' * 8000 * 601)
            self.assertAlmostEqual(mic_server.audio_duration_s(str(path)), 601, places=2)
