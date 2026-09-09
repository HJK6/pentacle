import contextlib
import importlib
import io
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock

import mic_server


class FakeProc:
    _next_pid = 4100

    def __init__(self, lines=None):
        FakeProc._next_pid += 1
        self.pid = FakeProc._next_pid
        self.stdout = [
            line if isinstance(line, bytes) else line.encode()
            for line in (lines or [])
        ]
        self.terminated = False
        self.wait_count = 0

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.wait_count += 1
        return 0

    def kill(self):
        self.terminated = True

    def poll(self):
        return None


class FakeMeetingRecorder:
    def __init__(self):
        self.running = False
        self.session_file = "/tmp/example-meeting.txt"
        self.start = mock.Mock(side_effect=self._start)
        self.stop = mock.Mock(side_effect=self._stop)

    def _start(self):
        self.running = True

    def _stop(self):
        self.running = False
        return self.session_file


class FakeAlwaysOnListener:
    def __init__(self):
        self.running = False
        self.state = "LISTENING"
        self.start = mock.Mock(side_effect=self._start)
        self.stop = mock.Mock(side_effect=self._stop)
        self.get_calibration_status = mock.Mock(return_value={})
        self._execute_command = mock.Mock()

    def _start(self):
        self.running = True

    def _stop(self):
        self.running = False


class MicServerTestCase(unittest.TestCase):
    def setUp(self):
        self._reset_state()
        self.popen_lines = []
        self.popen_calls = []
        self.last_proc = None
        self.popen_delay = 0
        self.popen_entered = None
        self.env = mock.patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.find_process = mock.patch("mic_server._find_process", return_value=[])
        self.kill_process = mock.patch("mic_server._kill_process")
        self.popen = mock.patch("subprocess.Popen", side_effect=self._fake_popen)
        self.find_process.start()
        self.kill_process.start()
        self.popen.start()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), mic_server.MicHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        mock.patch.stopall()
        self._reset_state()

    def _fake_popen(self, *args, **kwargs):
        if self.popen_entered:
            self.popen_entered.set()
        if self.popen_delay:
            time.sleep(self.popen_delay)
        self.popen_calls.append({"args": args, "kwargs": kwargs})
        self.last_proc = FakeProc(self.popen_lines)
        return self.last_proc

    def _reset_state(self):
        mic_server.state.update({
            "mode": "off",
            "meeting_active": False,
            "clipboard_pid": None,
            "transcript": [],
            "partial": "",
            "session_file": None,
            "meeting_start": None,
            "logs": [],
            "caller": None,
            "paused_mode": None,
            "paused_caller": None,
            "last_error": None,
            "clipboard_remote": False,
            "remote_clipboard_lines": [],
            "on_listener_state": "LISTENING",
            "on_last_heard": "",
            "on_last_command": "",
            "on_last_copied": "",
            "on_captured_texts": [],
        })
        mic_server.clipboard_proc = None
        mic_server.meeting_recorder = None
        mic_server.always_on_listener = None
        mic_server.audio_health.reset()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=3) as resp:
            return resp.status, json.loads(resp.read().decode())

    def post(self, path, body=None):
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def wait_until(self, predicate, timeout=1):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    def install_fake_meeting(self):
        recorder = FakeMeetingRecorder()
        mic_server.meeting_recorder = recorder
        return recorder

    def install_fake_always_on(self):
        listener = FakeAlwaysOnListener()
        mic_server.always_on_listener = listener
        return listener

    def start_on(self, caller="hosta"):
        listener = self.install_fake_always_on()
        code, data = self.post("/mode/on", {"caller": caller})
        self.assertEqual(200, code)
        self.assertEqual("on", data["mode"])
        return listener

    def test_status_exposes_caller_and_paused_fields(self):
        _, data = self.get("/status")
        self.assertIn("caller", data)
        self.assertIn("paused_mode", data)
        self.assertIn("paused_caller", data)
        self.assertIn("last_error", data)
        self.assertIsNone(data["caller"])
        self.assertIsNone(data["paused_mode"])
        self.assertIsNone(data["paused_caller"])
        self.assertIsNone(data["last_error"])
        self.assertIn("audio", data)
        self.assertIn("health_state", data["audio"])

    def test_status_serializes_audio_health(self):
        mic_server.update_audio_health({
            "selected_device": "Example USB Microphone",
            "preferred_device_name": "Example USB Microphone",
            "preferred_present": True,
            "disallowed_device": False,
            "stream_open": True,
            "peak": 0.2,
            "rms": 0.05,
            "stream_status": None,
        })

        _, data = self.get("/status")

        self.assertEqual("Example USB Microphone", data["audio"]["selected_device"])
        self.assertEqual("ok", data["audio"]["health_state"])

    def test_clipboard_audio_health_line_updates_state(self):
        mic_server.handle_clipboard_stdout_line(json.dumps({
            "type": "audio_health",
            "selected_device": "Example USB Microphone",
            "preferred_device_name": "Example USB Microphone",
            "preferred_present": True,
            "disallowed_device": False,
            "stream_open": True,
            "peak": 0.3,
            "rms": 0.1,
        }))
        mic_server.handle_clipboard_stdout_line("{not-json")
        mic_server.handle_clipboard_stdout_line(json.dumps({"type": "ignored"}))

        _, data = self.get("/status")

        self.assertEqual("Example USB Microphone", data["audio"]["selected_device"])
        self.assertEqual(0.3, data["audio"]["peak"])

    def test_normal_clipboard_stop_clears_stale_audio_health(self):
        mic_server.update_audio_health({
            "selected_device": "Example USB Microphone",
            "preferred_device_name": "Example USB Microphone",
            "preferred_present": True,
            "disallowed_device": False,
            "stream_open": True,
            "peak": 0.2,
            "rms": 0.05,
        })
        mic_server.update_audio_health({
            "stream_open": False,
            "stream_status": None,
        })

        mic_server.stop_clipboard()

        with mock.patch("time.time", return_value=time.time() + 11):
            _, data = self.get("/status")
        self.assertIsNone(data["last_error"])
        self.assertEqual("ok", data["audio"]["health_state"])
        self.assertIsNone(data["audio"]["selected_device"])

    def test_in_process_audio_health_callbacks_update_state(self):
        payload = {
            "selected_device": "Example USB Microphone",
            "preferred_device_name": "Example USB Microphone",
            "preferred_present": True,
            "disallowed_device": False,
            "stream_open": True,
            "peak": 0.2,
            "rms": 0.05,
        }

        mic_server.status_update("audio_health", payload)
        _, data = self.get("/status")
        self.assertEqual("Example USB Microphone", data["audio"]["selected_device"])

        mic_server.always_on_event("audio_health", dict(payload, selected_device="Example USB Microphone Extended"))
        _, data = self.get("/status")
        self.assertEqual("Example USB Microphone Extended", data["audio"]["selected_device"])

    def test_mode_clipboard_records_caller(self):
        code, _ = self.post("/mode/clipboard", {"caller": "hostb"})
        self.assertEqual(200, code)
        self.assertEqual("hostb", mic_server.state["caller"])

    def test_clipboard_meeting_conflict_returns_409(self):
        self.post("/mode/clipboard", {"caller": "A"})
        code, data = self.post("/mode/meeting", {"caller": "B"})
        self.assertEqual(409, code)
        self.assertEqual({"error": "mic_in_use", "mode": "clipboard", "caller": "A"}, data)

    def test_clipboard_clipboard_different_caller_returns_409(self):
        self.post("/mode/clipboard", {"caller": "A"})
        code, data = self.post("/mode/clipboard", {"caller": "B"})
        self.assertEqual(409, code)
        self.assertEqual("mic_in_use", data["error"])

    def test_same_caller_idempotent(self):
        first, _ = self.post("/mode/clipboard", {"caller": "A"})
        second, _ = self.post("/mode/clipboard", {"caller": "A"})
        self.assertEqual(200, first)
        self.assertEqual(200, second)
        self.assertEqual(1, len(self.popen_calls))

    def test_null_caller_treated_as_distinct(self):
        self.post("/mode/clipboard", {})
        code, data = self.post("/mode/clipboard", {"caller": "hostb"})
        self.assertEqual(409, code)
        self.assertEqual("mic_in_use", data["error"])

    def test_mode_on_403_when_disabled(self):
        code, data = self.post("/mode/on", {"caller": "hosta"})
        self.assertEqual(403, code)
        self.assertEqual({"error": "always_on_disabled"}, data)

    def test_mode_on_403_overrides_matrix(self):
        self.assertEqual("off", mic_server.state["mode"])
        code, data = self.post("/mode/on", {"caller": "hosta"})
        self.assertEqual(403, code)
        self.assertEqual({"error": "always_on_disabled"}, data)
        self.assertEqual("off", mic_server.state["mode"])

    def test_off_meeting_starts_and_records_caller(self):
        recorder = self.install_fake_meeting()
        code, data = self.post("/mode/meeting", {"caller": "hostb"})
        self.assertEqual(200, code)
        self.assertEqual("meeting", data["mode"])
        recorder.start.assert_called_once()
        _, status = self.get("/status")
        self.assertEqual("meeting", status["mode"])
        self.assertEqual("hostb", status["caller"])
        self.assertTrue(status["meeting_active"])

    def test_off_on_success_when_enabled(self):
        with mock.patch.dict(os.environ, {"MIC_ALWAYS_ON_ENABLED": "true"}, clear=True):
            listener = self.start_on("hosta")
        listener.start.assert_called_once()
        _, status = self.get("/status")
        self.assertEqual("on", status["mode"])
        self.assertEqual("hosta", status["caller"])

    def test_clipboard_on_conflict_returns_409(self):
        self.post("/mode/clipboard", {"caller": "A"})
        with mock.patch.dict(os.environ, {"MIC_ALWAYS_ON_ENABLED": "true"}, clear=True):
            code, data = self.post("/mode/on", {"caller": "A"})
        self.assertEqual(409, code)
        self.assertEqual({"error": "mic_in_use", "mode": "clipboard", "caller": "A"}, data)

    def test_meeting_clipboard_conflict_returns_409(self):
        self.install_fake_meeting()
        self.post("/mode/meeting", {"caller": "A"})
        code, data = self.post("/mode/clipboard", {"caller": "B"})
        self.assertEqual(409, code)
        self.assertEqual({"error": "mic_in_use", "mode": "meeting", "caller": "A"}, data)

    def test_meeting_on_conflict_returns_409(self):
        self.install_fake_meeting()
        self.post("/mode/meeting", {"caller": "A"})
        with mock.patch.dict(os.environ, {"MIC_ALWAYS_ON_ENABLED": "true"}, clear=True):
            code, data = self.post("/mode/on", {"caller": "A"})
        self.assertEqual(409, code)
        self.assertEqual({"error": "mic_in_use", "mode": "meeting", "caller": "A"}, data)

    def test_meeting_meeting_same_caller_idempotent(self):
        recorder = self.install_fake_meeting()
        first, _ = self.post("/mode/meeting", {"caller": "A"})
        second, data = self.post("/mode/meeting", {"caller": "A"})
        self.assertEqual(200, first)
        self.assertEqual(200, second)
        self.assertEqual("meeting", data["mode"])
        recorder.start.assert_called_once()

    def test_meeting_meeting_different_caller_returns_409(self):
        self.install_fake_meeting()
        self.post("/mode/meeting", {"caller": "A"})
        code, data = self.post("/mode/meeting", {"caller": "B"})
        self.assertEqual(409, code)
        self.assertEqual({"error": "mic_in_use", "mode": "meeting", "caller": "A"}, data)

    def test_on_meeting_pause_and_start_different_caller(self):
        with mock.patch.dict(os.environ, {"MIC_ALWAYS_ON_ENABLED": "true"}, clear=True):
            listener = self.start_on("hosta")
            recorder = self.install_fake_meeting()
            code, _ = self.post("/mode/meeting", {"caller": "hostb"})
        self.assertEqual(200, code)
        listener.stop.assert_called_once()
        recorder.start.assert_called_once()
        _, status = self.get("/status")
        self.assertEqual("meeting", status["mode"])
        self.assertEqual("hostb", status["caller"])
        self.assertEqual("on", status["paused_mode"])
        self.assertEqual("hosta", status["paused_caller"])

    def test_on_meeting_pause_and_start_same_caller(self):
        with mock.patch.dict(os.environ, {"MIC_ALWAYS_ON_ENABLED": "true"}, clear=True):
            listener = self.start_on("hosta")
            recorder = self.install_fake_meeting()
            code, _ = self.post("/mode/meeting", {"caller": "hosta"})
        self.assertEqual(200, code)
        listener.stop.assert_called_once()
        recorder.start.assert_called_once()
        _, status = self.get("/status")
        self.assertEqual("meeting", status["mode"])
        self.assertEqual("hosta", status["caller"])
        self.assertEqual("on", status["paused_mode"])
        self.assertEqual("hosta", status["paused_caller"])

    def test_on_on_idempotent_updates_caller_when_unpaused(self):
        with mock.patch.dict(os.environ, {"MIC_ALWAYS_ON_ENABLED": "true"}, clear=True):
            listener = self.start_on("hosta")
            code, data = self.post("/mode/on", {"caller": "hostb"})
        self.assertEqual(200, code)
        self.assertEqual("on", data["mode"])
        self.assertEqual(1, listener.start.call_count)
        _, status = self.get("/status")
        self.assertEqual("on", status["mode"])
        self.assertEqual("hostb", status["caller"])
        self.assertIsNone(status["paused_mode"])

    def test_always_on_pause_on_clipboard_start(self):
        with mock.patch.dict(os.environ, {"MIC_ALWAYS_ON_ENABLED": "true"}, clear=True):
            listener = self.start_on("hosta")
            code, _ = self.post("/mode/clipboard", {"caller": "hostb"})
        self.assertEqual(200, code)
        listener.stop.assert_called_once()
        _, status = self.get("/status")
        self.assertEqual("clipboard", status["mode"])
        self.assertEqual("hostb", status["caller"])
        self.assertEqual("on", status["paused_mode"])
        self.assertEqual("hosta", status["paused_caller"])

    def test_always_on_resume_on_off(self):
        with mock.patch.dict(os.environ, {"MIC_ALWAYS_ON_ENABLED": "true"}, clear=True):
            listener = self.start_on("hosta")
            self.post("/mode/clipboard", {"caller": "hostb"})
            code, data = self.post("/mode/off", {"caller": "hostb"})
        self.assertEqual(200, code)
        self.assertTrue(data["resumed"])
        self.assertEqual(2, listener.start.call_count)
        _, status = self.get("/status")
        self.assertEqual("on", status["mode"])
        self.assertEqual("hosta", status["caller"])
        self.assertIsNone(status["paused_mode"])
        self.assertIsNone(status["last_error"])

    def test_always_on_resume_failure_reports_error_and_clears_paused_state(self):
        with mock.patch.dict(os.environ, {"MIC_ALWAYS_ON_ENABLED": "true"}, clear=True):
            listener = self.start_on("hosta")
            self.post("/mode/clipboard", {"caller": "hostb"})
            listener.start.side_effect = RuntimeError("audio gone")
            code, data = self.post("/mode/off")
        self.assertEqual(200, code)
        self.assertTrue(data["ok"])
        self.assertEqual("off", data["mode"])
        self.assertFalse(data["resumed"])
        self.assertEqual("audio gone", data["resume_error"])
        _, status = self.get("/status")
        self.assertEqual("off", status["mode"])
        self.assertIsNone(status["paused_mode"])
        self.assertIsNone(status["paused_caller"])
        self.assertEqual("always_on_resume_failed: audio gone", status["last_error"])

    def test_concurrent_clipboard_requests_one_wins_one_409(self):
        barrier = threading.Barrier(2)
        results = []
        lock = threading.Lock()

        def run(caller):
            barrier.wait()
            result = self.post("/mode/clipboard", {"caller": caller})
            with lock:
                results.append(result)

        threads = [threading.Thread(target=run, args=(caller,)) for caller in ("A", "B")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)

        codes = sorted(code for code, _ in results)
        self.assertEqual([200, 409], codes)
        self.assertEqual(1, len(self.popen_calls))

    def test_no_interleave_clipboard_during_paused_resume(self):
        with mock.patch.dict(os.environ, {"MIC_ALWAYS_ON_ENABLED": "true"}, clear=True):
            listener = self.start_on("hosta")
            self.popen_delay = 0.15
            self.popen_entered = threading.Event()
            results = {}

            def start_clipboard():
                results["clipboard"] = self.post("/mode/clipboard", {"caller": "hostb"})

            def stop_mode():
                results["off"] = self.post("/mode/off", {"caller": "hosta"})

            clipboard_thread = threading.Thread(target=start_clipboard)
            clipboard_thread.start()
            self.assertTrue(self.popen_entered.wait(timeout=1))

            off_thread = threading.Thread(target=stop_mode)
            off_thread.start()
            clipboard_thread.join(timeout=3)
            off_thread.join(timeout=3)

        self.assertEqual(200, results["clipboard"][0])
        self.assertEqual(200, results["off"][0])
        self.assertTrue(results["off"][1]["resumed"])
        self.assertEqual(2, listener.start.call_count)
        self.assertEqual(1, listener.stop.call_count)
        self.assertEqual(1, len(self.popen_calls))
        _, status = self.get("/status")
        self.assertEqual("on", status["mode"])
        self.assertEqual("hosta", status["caller"])
        self.assertIsNone(status["paused_mode"])
        self.assertIsNone(status["paused_caller"])
        self.assertIsNone(status["last_error"])
        self.assertIsNone(status["clipboard_pid"])

    def test_same_caller_clipboard_reissue_preserves_paused_mode(self):
        with mock.patch.dict(os.environ, {"MIC_ALWAYS_ON_ENABLED": "true"}, clear=True):
            self.start_on("hosta")
            self.post("/mode/clipboard", {"caller": "hostb"})
        code, _ = self.post("/mode/clipboard", {"caller": "hostb"})
        self.assertEqual(200, code)
        self.assertEqual("on", mic_server.state["paused_mode"])
        self.assertEqual("hosta", mic_server.state["paused_caller"])
        self.assertEqual(1, len(self.popen_calls))

    def test_different_caller_reissue_while_always_on_paused_returns_409_and_preserves_paused_caller(self):
        with mock.patch.dict(os.environ, {"MIC_ALWAYS_ON_ENABLED": "true"}, clear=True):
            self.start_on("hosta")
            self.post("/mode/clipboard", {"caller": "hostb"})
            code, data = self.post("/mode/on", {"caller": "hosta"})
        self.assertEqual(409, code)
        self.assertEqual("mic_in_use", data["error"])
        self.assertEqual("hosta", mic_server.state["paused_caller"])

    def test_same_caller_mode_on_while_paused_fresh_starts_always_on(self):
        with mock.patch.dict(os.environ, {"MIC_ALWAYS_ON_ENABLED": "true"}, clear=True):
            listener = self.start_on("hosta")
            self.post("/mode/clipboard", {"caller": "hostb"})
            code, data = self.post("/mode/on", {"caller": "hostb"})
        self.assertEqual(200, code)
        self.assertEqual("on", data["mode"])
        self.assertEqual(2, listener.start.call_count)
        self.assertEqual("on", mic_server.state["mode"])
        self.assertEqual("hostb", mic_server.state["caller"])
        self.assertIsNone(mic_server.state["paused_mode"])
        self.assertIsNone(mic_server.state["paused_caller"])

    def test_remote_clipboard_does_not_call_local_copy(self):
        self.popen_lines = ['{"type":"remote_clipboard","text":"hi","ts":"2026-01-01T00:00:00Z"}\n']
        code, _ = self.post("/mode/clipboard", {"remote": True, "caller": "A"})
        self.assertEqual(200, code)
        self.assertTrue(self.wait_until(lambda: mic_server.state["remote_clipboard_lines"] == ["hi"]))
        env = self.popen_calls[0]["kwargs"]["env"]
        self.assertEqual("1", env.get("MIC_REMOTE_CLIPBOARD"))
        self.assertEqual(["hi"], mic_server.state["remote_clipboard_lines"])

    def test_remote_clipboard_subprocess_parser_ignores_non_json_log_lines(self):
        self.popen_lines = [
            "[init] ready\n",
            '{"type":"remote_clipboard","text":"hi","ts":"2026-01-01T00:00:00Z"}\n',
        ]
        self.post("/mode/clipboard", {"remote": True, "caller": "A"})
        self.assertTrue(self.wait_until(lambda: mic_server.state["remote_clipboard_lines"] == ["hi"]))
        self.assertTrue(any("[init] ready" in line for line in mic_server.state["logs"]))

    def test_remote_clipboard_subprocess_parser_ignores_malformed_json(self):
        self.popen_lines = ['{"type":"remote_clipboard"\n']
        self.post("/mode/clipboard", {"remote": True, "caller": "A"})
        time.sleep(0.05)
        self.assertEqual([], mic_server.state["remote_clipboard_lines"])

    def test_remote_clipboard_from_server_host_caller_works(self):
        self.popen_lines = ['{"type":"remote_clipboard","text":"hosta text","ts":"2026-01-01T00:00:00Z"}\n']
        code, _ = self.post("/mode/clipboard", {"remote": True, "caller": "hosta"})
        self.assertEqual(200, code)
        self.assertTrue(self.wait_until(lambda: mic_server.state["remote_clipboard_lines"] == ["hosta text"]))

    def test_mic_server_handles_end_session_line_from_listener(self):
        self.popen_lines = [
            '{"type":"remote_clipboard","text":"hi","ts":"2026-01-01T00:00:00Z"}\n',
        ]
        code, _ = self.post("/mode/clipboard", {"remote": True, "caller": "hostb"})
        self.assertEqual(200, code)
        self.assertTrue(self.wait_until(lambda: mic_server.state["remote_clipboard_lines"] == ["hi"]))

        mic_server.handle_clipboard_stdout_line('{"type":"end_session","reason":"stop_word"}')

        self.assertEqual("off", mic_server.state["mode"])
        self.assertIsNone(mic_server.state["caller"])
        self.assertIsNone(mic_server.state["paused_mode"])
        self.assertIsNone(mic_server.state["paused_caller"])
        self.assertIsNone(mic_server.state["last_error"])
        self.assertFalse(mic_server.state["clipboard_remote"])
        self.assertIsNone(mic_server.state["clipboard_pid"])
        self.assertEqual(["hi"], mic_server.state["remote_clipboard_lines"])
        _, data = self.get("/clipboard/since/0")
        self.assertEqual(["hi"], data["lines"])
        self.assertTrue(any("[end_session] stop-word from listener" in line for line in mic_server.state["logs"]))

    def test_end_session_preserves_final_lines_for_drain(self):
        self.popen_lines = [
            '{"type":"remote_clipboard","text":"final","ts":"2026-01-01T00:00:00Z"}\n',
            '{"type":"end_session","reason":"stop_word"}\n',
        ]
        code, _ = self.post("/mode/clipboard", {"remote": True, "caller": "hostb"})
        self.assertEqual(200, code)
        self.assertTrue(self.wait_until(lambda: mic_server.state["mode"] == "off"))

        _, data = self.get("/clipboard/since/0")
        self.assertEqual(["final"], data["lines"])
        self.assertEqual(1, data["total"])

        self.popen_lines = []
        code, _ = self.post("/mode/clipboard", {"remote": True, "caller": "hostb"})
        self.assertEqual(200, code)
        _, data = self.get("/clipboard/since/0")
        self.assertEqual([], data["lines"])
        self.assertEqual(0, data["total"])

    def test_mic_server_ignores_end_session_when_not_in_clipboard(self):
        mic_server.state.update({
            "mode": "meeting",
            "caller": "hosta",
            "paused_mode": "on",
            "paused_caller": "hosta",
            "last_error": "keep",
            "clipboard_remote": True,
            "remote_clipboard_lines": ["keep"],
            "clipboard_pid": 123,
        })

        mic_server.handle_clipboard_stdout_line('{"type":"end_session","reason":"stop_word"}')

        self.assertEqual("meeting", mic_server.state["mode"])
        self.assertEqual("hosta", mic_server.state["caller"])
        self.assertEqual("on", mic_server.state["paused_mode"])
        self.assertEqual("hosta", mic_server.state["paused_caller"])
        self.assertEqual("keep", mic_server.state["last_error"])
        self.assertTrue(mic_server.state["clipboard_remote"])
        self.assertEqual(["keep"], mic_server.state["remote_clipboard_lines"])
        self.assertEqual(123, mic_server.state["clipboard_pid"])
        self.assertTrue(any("[end_session] ignored (mode=meeting)" in line for line in mic_server.state["logs"]))

    def test_clipboard_since_returns_lines(self):
        mic_server.append_remote_clipboard_text("one")
        mic_server.append_remote_clipboard_text("two")
        _, data = self.get("/clipboard/since/0")
        self.assertEqual(["one", "two"], data["lines"])
        self.assertEqual(2, data["total"])
        _, data = self.get("/clipboard/since/1")
        self.assertEqual(["two"], data["lines"])
        _, data = self.get("/clipboard/since/5")
        self.assertEqual([], data["lines"])

    def test_clipboard_ring_buffer_capped(self):
        for idx in range(250):
            mic_server.append_remote_clipboard_text(f"line-{idx}")
        _, data = self.get("/clipboard/since/0")
        self.assertEqual(200, data["total"])
        self.assertEqual(200, len(data["lines"]))
        self.assertEqual("line-50", data["lines"][0])

    def test_clipboard_off_preserves_ring_buffer_until_next_start(self):
        self.popen_lines = [
            '{"type":"remote_clipboard","text":"one","ts":"2026-01-01T00:00:00Z"}\n',
            '{"type":"remote_clipboard","text":"two","ts":"2026-01-01T00:00:01Z"}\n',
        ]
        self.post("/mode/clipboard", {"remote": True, "caller": "A"})
        self.assertTrue(self.wait_until(lambda: len(mic_server.state["remote_clipboard_lines"]) == 2))
        code, _ = self.post("/mode/off")
        self.assertEqual(200, code)
        _, data = self.get("/clipboard/since/0")
        self.assertEqual(["one", "two"], data["lines"])

        self.popen_lines = []
        code, _ = self.post("/mode/clipboard", {"remote": True, "caller": "A"})
        self.assertEqual(200, code)
        _, data = self.get("/clipboard/since/0")
        self.assertEqual([], data["lines"])

    def test_bind_host_default_127(self):
        self.assertEqual("127.0.0.1", mic_server.get_bind_host())

    def test_bind_host_env_override(self):
        with mock.patch.dict(os.environ, {"MIC_BIND_HOST": "10.0.0.0"}, clear=True):
            self.assertEqual("10.0.0.0", mic_server.get_bind_host())

    def test_legacy_clipboard_still_copies_locally(self):
        code, _ = self.post("/mode/clipboard")
        self.assertEqual(200, code)
        env = self.popen_calls[0]["kwargs"]["env"]
        self.assertNotIn("MIC_REMOTE_CLIPBOARD", env)
        self.assertEqual([], mic_server.state["remote_clipboard_lines"])

        listener, clipboard = import_mic_listener(remote=False)
        listener.model = FakeWhisperModel("legacy text")
        with contextlib.redirect_stdout(io.StringIO()):
            listener.transcribe([0] * listener.SAMPLE_RATE)
        clipboard.copy_to_clipboard.assert_called_once_with("legacy text")

    def test_takeover_via_mode_off_any_caller(self):
        self.post("/mode/clipboard", {"caller": "hostb"})
        code, data = self.post("/mode/off", {"caller": "hosta"})
        self.assertEqual(200, code)
        self.assertEqual("off", data["mode"])
        self.assertFalse(data["resumed"])
        self.assertEqual("off", mic_server.state["mode"])
        self.assertIsNone(mic_server.state["caller"])

    def test_clipboard_child_reaped_when_stdout_exits(self):
        code, _ = self.post("/mode/clipboard", {"caller": "A"})
        self.assertEqual(200, code)
        proc = self.last_proc
        self.assertIsNotNone(proc)

        self.assertTrue(self.wait_until(lambda: proc.wait_count >= 1))
        self.assertIsNone(mic_server.clipboard_proc)
        self.assertIsNone(mic_server.state["clipboard_pid"])

    def test_stop_clipboard_invokes_wait_for_process_once(self):
        proc = FakeProc()
        mic_server.clipboard_proc = proc
        mic_server.state["clipboard_pid"] = proc.pid

        with mock.patch.object(mic_server, "_wait_for_process") as wait_for_process:
            mic_server.stop_clipboard()

        wait_for_process.assert_called_once_with(proc)

    def test_start_clipboard_with_stop_existing_calls_stop_first(self):
        proc = FakeProc()
        mic_server.clipboard_proc = proc
        mic_server.state["mode"] = "clipboard"
        order = []

        def stop_clipboard():
            order.append("stop")

        def popen(*args, **kwargs):
            order.append("popen")
            return self._fake_popen(*args, **kwargs)

        with mock.patch.object(mic_server, "stop_clipboard", side_effect=stop_clipboard):
            with mock.patch("subprocess.Popen", side_effect=popen):
                mic_server.start_clipboard(stop_existing=True)

        self.assertEqual(["stop", "popen"], order)

    def test_clear_clipboard_proc_noop_when_stale(self):
        tracked = FakeProc()
        stale = FakeProc()
        mic_server.clipboard_proc = tracked
        mic_server.state["clipboard_pid"] = tracked.pid

        mic_server._clear_clipboard_proc(stale)

        self.assertIs(tracked, mic_server.clipboard_proc)
        self.assertEqual(tracked.pid, mic_server.state["clipboard_pid"])


class FakeWhisperModel:
    def __init__(self, text):
        self.text = text

    def transcribe(self, path, beam_size=5, language="en"):
        return [SimpleNamespace(text=self.text)], SimpleNamespace()


class FakeAudio:
    def __len__(self):
        return 48000

    def flatten(self):
        return self


def import_mic_listener(remote):
    sys.modules.pop("mic_listener", None)
    clipboard = SimpleNamespace(copy_to_clipboard=mock.Mock())
    modules = {
        "numpy": SimpleNamespace(),
        "sounddevice": SimpleNamespace(query_devices=lambda idx: {"name": "Fake Mic"}, InputStream=object),
        "soundfile": SimpleNamespace(write=lambda path, audio_data, sample_rate: None),
        "faster_whisper": SimpleNamespace(WhisperModel=object),
        "audio_device": SimpleNamespace(resolve_mic_device=lambda: {
            "index": 0,
            "selected_device": "Fake Mic",
            "preferred_device_name": "Fake Mic",
            "preferred_present": True,
            "disallowed_device": False,
        }),
        "clipboard": clipboard,
    }
    env_value = {"MIC_REMOTE_CLIPBOARD": "1"} if remote else {}
    with mock.patch.dict(sys.modules, modules), mock.patch.dict(os.environ, env_value, clear=True):
        listener = importlib.import_module("mic_listener")
    return listener, clipboard


class MicListenerRemoteClipboardTest(unittest.TestCase):
    def test_strip_end_copy_phrase_trailing_over(self):
        listener, _ = import_mic_listener(remote=False)
        self.assertEqual("How's it going?", listener.strip_end_copy_phrase("How's it going? Over."))

    def test_strip_end_copy_phrase_only_stop_word(self):
        listener, _ = import_mic_listener(remote=False)
        self.assertEqual("", listener.strip_end_copy_phrase("Over."))
        self.assertEqual("", listener.strip_end_copy_phrase("over"))

    def test_strip_end_copy_phrase_case_insensitive(self):
        listener, _ = import_mic_listener(remote=False)
        self.assertEqual("hello", listener.strip_end_copy_phrase("hello OVER"))
        self.assertEqual("hello", listener.strip_end_copy_phrase("hello End Copy"))

    def test_strip_end_copy_phrase_word_boundary_required(self):
        listener, _ = import_mic_listener(remote=False)
        self.assertEqual("moreover please", listener.strip_end_copy_phrase("moreover please"))
        self.assertEqual("moreover", listener.strip_end_copy_phrase("moreover"))

    def test_strip_end_copy_phrase_no_match(self):
        listener, _ = import_mic_listener(remote=False)
        self.assertEqual("just a normal sentence", listener.strip_end_copy_phrase("just a normal sentence"))

    def test_strip_end_copy_phrase_multi_word(self):
        listener, _ = import_mic_listener(remote=False)
        self.assertEqual("write hello world", listener.strip_end_copy_phrase("write hello world stop copying"))

    def test_strip_end_copy_phrase_repeated_over(self):
        listener, _ = import_mic_listener(remote=False)
        self.assertEqual("", listener.strip_end_copy_phrase("over over"))

    def test_strip_end_copy_phrase_repeated_mixed(self):
        listener, _ = import_mic_listener(remote=False)
        self.assertEqual("", listener.strip_end_copy_phrase("stop copy stop copying"))

    def test_strip_end_copy_phrase_partial_repeat(self):
        listener, _ = import_mic_listener(remote=False)
        self.assertEqual("hello", listener.strip_end_copy_phrase("hello over over"))

    def test_remote_clipboard_listener_emits_json_and_suppresses_copy(self):
        listener, clipboard = import_mic_listener(remote=True)
        listener.model = FakeWhisperModel("remote text")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            listener.transcribe([0] * listener.SAMPLE_RATE)
        lines = out.getvalue().splitlines()
        payload = json.loads(lines[1])
        self.assertEqual("remote_clipboard", payload["type"])
        self.assertEqual("remote text", payload["text"])
        clipboard.copy_to_clipboard.assert_not_called()
        self.assertFalse(any(line.startswith('{"type":"remote_clipboard"') for line in lines if line != lines[1]))

    def test_remote_clipboard_emits_stripped_text(self):
        listener, clipboard = import_mic_listener(remote=True)
        listener.model = FakeWhisperModel("hello over")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            listener.transcribe([0] * listener.SAMPLE_RATE)
        payloads = [
            json.loads(line) for line in out.getvalue().splitlines()
            if line.startswith("{")
        ]
        self.assertEqual(2, len(payloads))
        payload = payloads[0]
        self.assertEqual("remote_clipboard", payload["type"])
        self.assertEqual("hello", payload["text"])
        self.assertEqual("end_session", payloads[1]["type"])
        self.assertEqual("stop_word", payloads[1]["reason"])
        self.assertIn("[stop-word] stripped trailing phrase", out.getvalue())
        clipboard.copy_to_clipboard.assert_not_called()

    def test_remote_clipboard_repeated_stopword_suppressed(self):
        listener, clipboard = import_mic_listener(remote=True)
        listener.model = FakeWhisperModel("over over")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            listener.transcribe([0] * listener.SAMPLE_RATE)
        payloads = [
            json.loads(line) for line in out.getvalue().splitlines()
            if line.startswith("{")
        ]
        self.assertEqual(1, len(payloads))
        self.assertEqual("end_session", payloads[0]["type"])
        self.assertEqual("stop_word", payloads[0]["reason"])
        self.assertIn("[empty] all stop-word, suppressed", out.getvalue())
        clipboard.copy_to_clipboard.assert_not_called()

    def test_legacy_clipboard_strips_end_copy_phrase(self):
        listener, clipboard = import_mic_listener(remote=False)
        listener.model = FakeWhisperModel("hello over")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            listener.transcribe([0] * listener.SAMPLE_RATE)
        clipboard.copy_to_clipboard.assert_called_once_with("hello")
        self.assertIn("[stop-word] stripped trailing phrase", out.getvalue())

    def test_stop_word_emits_end_session_and_exits(self):
        listener, clipboard = import_mic_listener(remote=True)
        listener.model = FakeWhisperModel("hello over")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            listener.transcribe([0] * listener.SAMPLE_RATE)
        payloads = [
            json.loads(line) for line in out.getvalue().splitlines()
            if line.startswith("{")
        ]
        self.assertEqual("remote_clipboard", payloads[0]["type"])
        self.assertEqual("hello", payloads[0]["text"])
        self.assertEqual("end_session", payloads[1]["type"])
        self.assertEqual("stop_word", payloads[1]["reason"])
        self.assertFalse(listener.running)
        clipboard.copy_to_clipboard.assert_not_called()

    def test_stop_word_only_utterance_emits_end_session(self):
        listener, clipboard = import_mic_listener(remote=True)
        listener.model = FakeWhisperModel("over")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            listener.transcribe([0] * listener.SAMPLE_RATE)
        payloads = [
            json.loads(line) for line in out.getvalue().splitlines()
            if line.startswith("{")
        ]
        self.assertEqual(1, len(payloads))
        self.assertEqual("end_session", payloads[0]["type"])
        self.assertEqual("stop_word", payloads[0]["reason"])
        self.assertIn("[empty] all stop-word, suppressed", out.getvalue())
        self.assertFalse(listener.running)
        clipboard.copy_to_clipboard.assert_not_called()

    def test_no_stop_word_does_not_emit_end_session(self):
        listener, clipboard = import_mic_listener(remote=True)
        listener.model = FakeWhisperModel("normal sentence")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            listener.transcribe([0] * listener.SAMPLE_RATE)
        payloads = [
            json.loads(line) for line in out.getvalue().splitlines()
            if line.startswith("{")
        ]
        self.assertEqual(1, len(payloads))
        self.assertEqual("remote_clipboard", payloads[0]["type"])
        self.assertTrue(listener.running)
        clipboard.copy_to_clipboard.assert_not_called()

    def test_shutdown_finalizes_pending_recording(self):
        listener, clipboard = import_mic_listener(remote=True)
        listener.model = FakeWhisperModel("manual stop text")
        listener.state = listener.State.RECORDING
        listener.audio_buffer = [object()]
        listener.np = SimpleNamespace(concatenate=lambda chunks, axis=0: FakeAudio())
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            listener.finalize_pending_recording("shutdown")

        payloads = [
            json.loads(line) for line in out.getvalue().splitlines()
            if line.startswith("{")
        ]
        self.assertEqual("remote_clipboard", payloads[0]["type"])
        self.assertEqual("manual stop text", payloads[0]["text"])
        self.assertEqual(listener.State.IDLE, listener.state)
        self.assertEqual([], listener.audio_buffer)
        clipboard.copy_to_clipboard.assert_not_called()


if __name__ == "__main__":
    unittest.main()
