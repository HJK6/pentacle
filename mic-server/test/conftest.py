"""The portable mic suite must never open an audio output device."""
import os
from pathlib import Path
import subprocess
import pytest


@pytest.fixture(autouse=True)
def forbid_real_audio_output(monkeypatch):
    monkeypatch.setenv('MIC_SPEAKER_SINK', 'null')
    original = subprocess.Popen
    def guarded(argv, *args, **kwargs):
        command = argv if isinstance(argv, str) else ' '.join(map(str, argv))
        if any(word in command for word in ('afplay', 'SwitchAudioSource', 'set volume', 'bart_speak.sh')):
            pytest.fail('The test suite attempted real audio playback')
        return original(argv, *args, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', guarded)
