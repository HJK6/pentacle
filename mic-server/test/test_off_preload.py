"""Managed OFF startup loads the existing model without capture."""
import os
import sys
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import mic_server

class OffPreloadTest(unittest.TestCase):
    def test_explicit_off_startup_preloads_without_capture(self):
        model = object()
        listener = SimpleNamespace(whisper_model=None, on_event=None,
            load_models=mock.Mock(), start=mock.Mock(), running=False)
        listener.load_models.side_effect = lambda: setattr(listener, "whisper_model", model)
        class InlineThread:
            def __init__(self, target, **kwargs): self.target = target
            def start(self): self.target()
        vocab_dir = tempfile.TemporaryDirectory()
        vocab_file = Path(vocab_dir.name) / "vocabulary.txt"
        vocab_file.write_text("Example")
        old_preload = dict(mic_server.model_preload_status)
        old_state = dict(mic_server.state)
        mic_server.state["mode"] = "off"
        old = mic_server.always_on_listener
        mic_server.always_on_listener = None
        try:
            with mock.patch.dict(os.environ, {"MIC_SERVER_START_MODE":"off", "MIC_VOCABULARY_FILE":str(vocab_file), "MIC_NAME_CORRECTIONS":""}), \
                 mock.patch.dict(sys.modules, {"always_on":SimpleNamespace(AlwaysOnListener=lambda:listener)}), \
                 mock.patch.object(mic_server, "HTTPServer") as server, \
                 mock.patch.object(mic_server, "get_audio_buffer"), \
                 mock.patch.object(mic_server.signal, "signal"), \
                 mock.patch.object(mic_server.threading, "Thread", InlineThread):
                mic_server.main()
            self.assertIsNotNone(mic_server.always_on_listener, "explicit OFF must preload existing model")
            self.assertIs(mic_server.always_on_listener.whisper_model, model)
            listener.load_models.assert_called_once()
            listener.start.assert_not_called()
            self.assertEqual(mic_server.state["mode"], "off")
        finally:
            mic_server.always_on_listener = old
            mic_server.model_preload_status.update(old_preload)
            mic_server.state.clear()
            mic_server.state.update(old_state)
            vocab_dir.cleanup()

class OffPreloadLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.old_listener = mic_server.always_on_listener
        self.old_preload = dict(mic_server.model_preload_status)
        self.old_state = dict(mic_server.state)
        mic_server.always_on_listener = None
        mic_server.model_preload_status.update(state="idle", error=None, vocabulary_version=None)
        mic_server.state.update(mode="off", last_error=None, caller=None, paused_mode=None, paused_caller=None)
        self.vocabulary = mock.patch("fleet_vocabulary.load_vocabulary", return_value=("Example", {}, "fleet-example"))
        self.vocabulary.start()

    def tearDown(self):
        self.vocabulary.stop()
        mic_server.always_on_listener = self.old_listener
        mic_server.model_preload_status.update(self.old_preload)
        mic_server.state.clear()
        mic_server.state.update(self.old_state)

    def fake_listener(self, load=None):
        listener = SimpleNamespace(whisper_model=None, running=False, wake=None, voice_actions=None,
                                  capture_origin=None, on_event=None)
        def default_load(): listener.whisper_model = object()
        listener.load_models = mock.Mock(side_effect=load or default_load)
        listener.start = mock.Mock(side_effect=lambda: setattr(listener, "running", True))
        listener.stop = mock.Mock(side_effect=lambda: setattr(listener, "running", False))
        return listener

    def test_load_failure_never_publishes_partial_model(self):
        listener = self.fake_listener()
        def fail():
            listener.whisper_model = object()
            raise RuntimeError("weight warmup failed")
        listener.load_models.side_effect = fail
        with mock.patch.dict(sys.modules, {"always_on":SimpleNamespace(AlwaysOnListener=lambda:listener)}):
            mic_server.preload_always_on()
        self.assertIsNone(mic_server.always_on_listener)
        self.assertEqual(mic_server.model_preload_status["state"], "failed")
        self.assertIn("weight warmup failed", mic_server.state["last_error"])
        self.assertEqual(mic_server.state["mode"], "off")
        listener.start.assert_not_called()

    def test_missing_profile_fails_before_model_load(self):
        self.vocabulary.stop()
        listener = self.fake_listener()
        factory = mock.Mock(return_value=listener)
        with mock.patch("fleet_vocabulary.load_vocabulary", return_value=(None, {}, "fleet-empty")), \
             mock.patch.dict(sys.modules, {"always_on":SimpleNamespace(AlwaysOnListener=factory)}):
            mic_server.preload_always_on()
        factory.assert_not_called()
        self.assertIsNone(mic_server.always_on_listener)
        self.assertEqual(mic_server.model_preload_status["state"], "failed")

    def test_status_responsive_and_on_deferred_while_model_loads(self):
        import threading
        import urllib.request
        import urllib.error
        import json
        from http.server import HTTPServer
        entered, release = threading.Event(), threading.Event()
        listener = self.fake_listener()
        def load():
            entered.set()
            if not release.wait(3): raise RuntimeError("test release missing")
            listener.whisper_model = object()
        listener.load_models.side_effect = load
        with mock.patch.dict(sys.modules, {"always_on":SimpleNamespace(AlwaysOnListener=lambda:listener)}), \
             mock.patch.object(mic_server, "get_audio_buffer", return_value=None):
            preload = threading.Thread(target=mic_server.preload_always_on)
            server = HTTPServer(("127.0.0.1",0),mic_server.MicHandler)
            worker = threading.Thread(target=server.serve_forever)
            preload.start(); worker.start()
            try:
                self.assertTrue(entered.wait(1))
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib.request.urlopen(base+"/status",timeout=1) as response:
                    status = json.load(response)
                self.assertEqual(status["mode"], "off")
                self.assertFalse(status["asr"]["loaded"])
                self.assertEqual(status["asr"]["load_state"], "loading")
                request = urllib.request.Request(base+"/mode/on",data=b"{}",headers={"Content-Type":"application/json"})
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request,timeout=1)
                self.assertEqual(error.exception.code,409)
                listener.start.assert_not_called()
                release.set(); preload.join(2)
                self.assertFalse(preload.is_alive())
                with urllib.request.urlopen(base+"/status",timeout=1) as response:
                    status = json.load(response)
                self.assertTrue(status["asr"]["loaded"])
                self.assertEqual(status["asr"]["vocabulary_version"],"fleet-example")
                self.assertEqual(status["mode"], "off")
                self.assertFalse(status["audio"]["stream_open"])
                self.assertEqual(status["audio"]["health_state"],"idle")
            finally:
                release.set(); preload.join(4); server.shutdown(); server.server_close(); worker.join(2)

    def test_on_off_reuses_resident_model_and_failed_open_stops_workers(self):
        listener = self.fake_listener()
        with mock.patch.dict(sys.modules, {"always_on":SimpleNamespace(AlwaysOnListener=lambda:listener)}):
            mic_server.preload_always_on()
            model = listener.whisper_model
            mic_server.start_always_on(stop_existing=False)
            mic_server.stop_always_on()
            self.assertIs(listener.whisper_model,model)
            listener.load_models.assert_called_once()
            def fail_start():
                listener.running = True
                raise RuntimeError("capture counterpart failed")
            listener.start.side_effect = fail_start
            with self.assertRaisesRegex(RuntimeError,"capture counterpart failed"):
                mic_server.start_always_on(stop_existing=False)
            self.assertFalse(listener.running)
            self.assertIs(listener.whisper_model,model)
            listener.load_models.assert_called_once()

    def test_concurrent_initializers_share_one_resident_object(self):
        import threading
        entered, release = threading.Event(), threading.Event()
        listener = self.fake_listener()
        def load():
            entered.set()
            if not release.wait(2): raise RuntimeError("test release missing")
            listener.whisper_model = object()
        listener.load_models.side_effect = load
        factory = mock.Mock(return_value=listener)
        results = []
        with mock.patch.dict(sys.modules, {"always_on":SimpleNamespace(AlwaysOnListener=factory)}):
            a = threading.Thread(target=lambda: results.append(mic_server.ensure_always_on_models()))
            b = threading.Thread(target=lambda: results.append(mic_server.ensure_always_on_models()))
            a.start(); self.assertTrue(entered.wait(1)); b.start(); release.set(); a.join(2); b.join(2)
        factory.assert_called_once()
        listener.load_models.assert_called_once()
        self.assertEqual(results,[listener,listener])
        listener.start.assert_not_called()

if __name__ == "__main__": unittest.main()
