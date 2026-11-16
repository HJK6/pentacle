import importlib
import sys
from types import SimpleNamespace

import pytest


class FakeSoundDevice:
    def __init__(self, devices, default_input=0):
        self._devices = devices
        self.default = SimpleNamespace(device=(default_input, None))

    def query_devices(self, index=None):
        if index is None:
            return self._devices
        return self._devices[index]


def device(name, inputs=1):
    return {"name": name, "max_input_channels": inputs}


def load_audio_device(monkeypatch, devices, default_input=0):
    fake_sd = FakeSoundDevice(devices, default_input)
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    sys.modules.pop("audio_device", None)
    return importlib.import_module("audio_device")


def test_resolver_selects_preferred_device(monkeypatch):
    audio_device = load_audio_device(monkeypatch, [device("Built-in"), device("Example USB Microphone")])
    monkeypatch.setenv("MIC_DEVICE_NAME", "Example USB")

    result = audio_device.resolve_mic_device()

    assert result["index"] == 1
    assert result["selected_device"] == "Example USB Microphone"
    assert result["preferred_present"] is True
    assert result["disallowed_device"] is False


def test_resolver_reports_missing_preferred_without_fallback(monkeypatch):
    audio_device = load_audio_device(monkeypatch, [device("Built-in")])
    monkeypatch.setenv("MIC_DEVICE_NAME", "Example USB Microphone")

    result = audio_device.resolve_mic_device()

    assert result["index"] is None
    assert result["preferred_present"] is False
    assert result["selected_device"] is None
    with pytest.raises(RuntimeError, match="Example USB Microphone"):
        audio_device.find_mic_index()


def test_resolver_allows_safe_default_when_unpinned(monkeypatch):
    audio_device = load_audio_device(monkeypatch, [device("Built-in Microphone")])
    monkeypatch.delenv("MIC_DEVICE_NAME", raising=False)

    result = audio_device.resolve_mic_device()

    assert result["index"] == 0
    assert result["selected_device"] == "Built-in Microphone"
    assert result["preferred_device_name"] is None


@pytest.mark.parametrize("name", ["Synthetic Loopback Microphone", "Synthetic Loopback Audio"])
def test_resolver_refuses_denylisted_default(monkeypatch, name):
    audio_device = load_audio_device(monkeypatch, [device(name)])
    monkeypatch.delenv("MIC_DEVICE_NAME", raising=False)

    result = audio_device.resolve_mic_device()

    assert result["index"] is None
    assert result["selected_device"] == name
    assert result["disallowed_device"] is True
    with pytest.raises(RuntimeError, match="disallowed"):
        audio_device.find_mic_index()


def test_resolver_refuses_first_input_when_denylisted(monkeypatch):
    audio_device = load_audio_device(monkeypatch, [device("Synthetic Loopback Microphone")], default_input=-1)
    monkeypatch.delenv("MIC_DEVICE_NAME", raising=False)

    result = audio_device.resolve_mic_device()

    assert result["index"] is None
    assert result["disallowed_device"] is True


def test_resolver_reports_no_input_devices(monkeypatch):
    audio_device = load_audio_device(monkeypatch, [device("Speaker", inputs=0)], default_input=-1)
    monkeypatch.delenv("MIC_DEVICE_NAME", raising=False)

    result = audio_device.resolve_mic_device()

    assert result["index"] is None
    assert result["selected_device"] is None
    with pytest.raises(RuntimeError, match="No input"):
        audio_device.find_mic_index()
