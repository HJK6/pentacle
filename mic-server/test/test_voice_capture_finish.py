import contextlib
import importlib
import os
import queue
import sys
import threading
from types import SimpleNamespace

import numpy as np
from .test_listener_lifecycle import install_listener_import_fakes, import_fresh_module


def listener_module(monkeypatch):
    install_listener_import_fakes(monkeypatch)
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(from_numpy=lambda a:a, inference_mode=contextlib.nullcontext))
    return import_fresh_module('always_on')


def test_manual_stop_preserves_queued_and_buffered_speech(monkeypatch):
    module=listener_module(monkeypatch)
    copied=[]
    monkeypatch.setattr(module, 'copy_to_clipboard', copied.append)
    listener=module.AlwaysOnListener()
    listener.vad_model=lambda a,sr: SimpleNamespace(item=lambda:0)
    listener._transcribe=lambda a:'first words' if a[0]==1 else 'final words'
    listener.start()
    try:
        listener._execute_command('start_copy')
        listener._utterance_q.put(np.ones(8000,dtype=np.float32))
        listener.speech_active=True
        listener.speech_buf.append(np.full(8000,2,dtype=np.float32))
        listener.finish_capture(timeout=3)
        assert copied==['first words final words']
        assert listener.state=='LISTENING'
    finally:
        listener.stop()


def test_manual_stop_skips_vad_silent_owned_tail(monkeypatch):
    module = listener_module(monkeypatch)
    copied, transcribed = [], []
    monkeypatch.setattr(module, 'copy_to_clipboard', copied.append)
    listener = module.AlwaysOnListener()
    listener.vad_model = lambda audio, sr: SimpleNamespace(item=lambda: 0.0)
    fixture = os.environ.get('VOICE_STOP_TAIL_FIXTURE')
    tail = np.load(fixture) if fixture else np.full(16000, .0001, dtype=np.float32)

    def transcribe(audio):
        transcribed.append(audio.copy())
        return 'spoken request' if audio[0] == 1 else 'Thank you.'

    listener._transcribe = transcribe
    listener.start()
    try:
        listener._execute_command('start_copy')
        listener._utterance_q.put(np.ones(8000, dtype=np.float32))
        for offset in range(0, len(tail), 1600):
            listener.audio_q.put((listener.recognition_stamp()[0], tail[offset:offset+1600]))
        listener.finish_capture(timeout=3)
        assert copied == ['spoken request']
        assert len(transcribed) == 1
        assert listener.state == 'LISTENING'
    finally:
        listener.stop()


def test_manual_stop_retains_short_quiet_partial_vad_onset(monkeypatch):
    module = listener_module(monkeypatch)
    copied, transcribed = [], []
    monkeypatch.setattr(module, 'copy_to_clipboard', copied.append)
    listener = module.AlwaysOnListener()
    confidences = iter([.46] + [0.0] * 100)
    listener.vad_model = lambda audio, sr: SimpleNamespace(item=lambda: next(confidences))
    listener._transcribe = lambda audio: transcribed.append(audio.copy()) or 'Yes.'
    listener.start()
    try:
        listener._execute_command('start_copy')
        # One positive frame never reaches the normal six-frame onset. Manual
        # stop must retain this quiet partial utterance without changing VAD.
        for _ in range(4):
            listener.audio_q.put((listener.recognition_stamp()[0], np.full(1600, .0002, dtype=np.float32)))
        listener.finish_capture(timeout=3)
        assert copied == ['Yes.']
        assert len(transcribed) == 1
        assert np.max(np.abs(transcribed[0])) <= .0002
        assert listener.state == 'LISTENING'
    finally:
        listener.stop()


def test_manual_stop_does_not_reuse_expired_vad_onset(monkeypatch):
    module = listener_module(monkeypatch)
    copied, transcribed = [], []
    monkeypatch.setattr(module, 'copy_to_clipboard', copied.append)
    listener = module.AlwaysOnListener()
    confidences = iter([.46] + [0.0] * 100)
    listener.vad_model = lambda audio, sr: SimpleNamespace(item=lambda: next(confidences))
    listener._transcribe = lambda audio: transcribed.append(audio.copy()) or 'Thank you.'
    listener.start()
    try:
        listener._execute_command('start_copy')
        # The positive frame has left the one-second pre-speech buffer.
        for _ in range(14):
            listener.audio_q.put((listener.recognition_stamp()[0], np.full(1600, .0001, dtype=np.float32)))
        listener.finish_capture(timeout=3)
        assert copied == []
        assert transcribed == []
        assert listener.state == 'LISTENING'
    finally:
        listener.stop()


def test_over_is_control_but_go_over_this_is_content(monkeypatch):
    module=listener_module(monkeypatch)
    copied=[]
    monkeypatch.setattr(module,'copy_to_clipboard',copied.append)
    listener=module.AlwaysOnListener()
    listener._execute_command('start_copy')
    listener._transcribe=lambda audio:'Please go over this carefully.'
    listener._handle_utterance(np.ones(8000))
    assert listener.state=='CAPTURING'
    listener._transcribe=lambda audio:'Over.'
    listener._handle_utterance(np.ones(8000))
    assert copied==['Please go over this carefully.']
    assert listener.state=='LISTENING'


def test_capture_start_clears_previous_completion(monkeypatch):
    module=importlib.import_module('mic_server')
    monkeypatch.setitem(module.state, 'on_last_copied', 'Previous message')
    monkeypatch.setitem(module.state, 'on_captured_texts', [])
    monkeypatch.setitem(module.state, 'on_listener_state', 'LISTENING')
    module.always_on_event('state','CAPTURING')
    assert module.state['on_last_copied']==''


def test_gpu_configuration_is_passed_to_model(monkeypatch):
    monkeypatch.setenv('MIC_WHISPER_MODEL','large-v3')
    monkeypatch.setenv('MIC_WHISPER_DEVICE','cuda')
    monkeypatch.setenv('MIC_WHISPER_COMPUTE_TYPE','float16')
    module=listener_module(monkeypatch)
    calls=[]
    monkeypatch.setitem(sys.modules,'silero_vad',SimpleNamespace(load_silero_vad=lambda:lambda:None))
    monkeypatch.setitem(sys.modules,'faster_whisper',SimpleNamespace(WhisperModel=lambda *a,**kw:calls.append((a,kw)) or object()))
    module.AlwaysOnListener().load_models()
    assert calls==[(('large-v3',),{'device':'cuda','compute_type':'float16'})]
