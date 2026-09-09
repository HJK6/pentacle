import importlib
import sys
from types import SimpleNamespace


class NoopInputStream:
    devices = []

    def __init__(self, *args, **kwargs):
        self.started = False
        self.closed = False
        self.kwargs = kwargs
        NoopInputStream.devices.append(kwargs.get("device"))

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True


def install_listener_import_fakes(monkeypatch, selections=None):
    selections = list(selections or [{
        "index": 0,
        "selected_device": "Example USB Microphone",
        "preferred_device_name": "Example USB Microphone",
        "preferred_present": True,
        "disallowed_device": False,
    }])
    calls = []

    def resolve_mic_device():
        idx = min(len(calls), len(selections) - 1)
        calls.append(idx)
        return selections[idx]

    NoopInputStream.devices = []
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(InputStream=NoopInputStream))
    monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace(write=lambda *args, **kwargs: None))
    monkeypatch.setitem(
        sys.modules,
        "audio_device",
        SimpleNamespace(resolve_mic_device=resolve_mic_device),
    )
    monkeypatch.setitem(sys.modules, "clipboard", SimpleNamespace(copy_to_clipboard=lambda text: None))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(from_numpy=lambda frame: frame))
    monkeypatch.setitem(sys.modules, "scipy", SimpleNamespace(signal=SimpleNamespace()))
    monkeypatch.setitem(
        sys.modules,
        "scipy.signal",
        SimpleNamespace(resample_poly=lambda data, up, down: data),
    )
    return calls


def import_fresh_module(name):
    sys.modules.pop(name, None)
    return importlib.import_module(name)


def test_always_on_stop_sets_event_and_threads_exit(monkeypatch):
    install_listener_import_fakes(monkeypatch)
    always_on = import_fresh_module("always_on")
    monkeypatch.setattr(always_on.sd, "InputStream", NoopInputStream)
    listener = always_on.AlwaysOnListener()

    listener.start()
    worker = listener._worker

    assert worker.is_alive()

    listener.stop()
    worker.join(timeout=2)

    assert listener._stop_event.is_set()
    assert not worker.is_alive()


def test_meeting_recorder_stop_joins_worker(monkeypatch, tmp_path):
    install_listener_import_fakes(monkeypatch)
    meeting_recorder = import_fresh_module("meeting_recorder")
    monkeypatch.setattr(meeting_recorder.sd, "InputStream", NoopInputStream)
    monkeypatch.setattr(meeting_recorder, "TRANSCRIPT_DIR", str(tmp_path))
    recorder = meeting_recorder.MeetingRecorder()

    recorder.start()
    worker = recorder._worker

    assert worker.is_alive()

    recorder.stop()

    assert not worker.is_alive()


def test_always_on_re_resolves_device_on_each_start(monkeypatch):
    calls = install_listener_import_fakes(monkeypatch, selections=[
        {
            "index": 0,
            "selected_device": "Example USB Microphone",
            "preferred_device_name": "Example USB Microphone",
            "preferred_present": True,
            "disallowed_device": False,
        },
        {
            "index": 1,
            "selected_device": "Example USB Microphone",
            "preferred_device_name": "Example USB Microphone",
            "preferred_present": True,
            "disallowed_device": False,
        },
    ])
    always_on = import_fresh_module("always_on")
    monkeypatch.setattr(always_on.sd, "InputStream", NoopInputStream)
    listener = always_on.AlwaysOnListener()

    listener.start()
    listener.stop()
    listener.start()
    listener.stop()

    assert len(calls) == 2
    assert NoopInputStream.devices == [0, 1]


def test_meeting_recorder_re_resolves_device_on_each_start(monkeypatch, tmp_path):
    calls = install_listener_import_fakes(monkeypatch, selections=[
        {
            "index": 0,
            "selected_device": "Example USB Microphone",
            "preferred_device_name": "Example USB Microphone",
            "preferred_present": True,
            "disallowed_device": False,
        },
        {
            "index": 1,
            "selected_device": "Example USB Microphone",
            "preferred_device_name": "Example USB Microphone",
            "preferred_present": True,
            "disallowed_device": False,
        },
    ])
    meeting_recorder = import_fresh_module("meeting_recorder")
    monkeypatch.setattr(meeting_recorder.sd, "InputStream", NoopInputStream)
    monkeypatch.setattr(meeting_recorder, "TRANSCRIPT_DIR", str(tmp_path))
    recorder = meeting_recorder.MeetingRecorder()

    recorder.start()
    recorder.stop()
    recorder.start()
    recorder.stop()

    assert len(calls) == 2
    assert NoopInputStream.devices == [0, 1]
