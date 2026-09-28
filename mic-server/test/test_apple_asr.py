import sys
from types import SimpleNamespace

import pytest


def test_apple_backend_rejects_changed_model_or_compute():
    from apple_asr import AppleWhisperModel
    for options in [('small.en', 'metal', 'float16'), ('large-v3', 'cpu', 'int8')]:
        with pytest.raises(ValueError, match='large-v3/metal/float16'):
            AppleWhisperModel(*options)


def test_apple_backend_warms_once_and_keeps_hints_explicit(monkeypatch):
    calls = []
    fake = SimpleNamespace(transcribe=lambda audio, **kw: calls.append((audio, kw)) or
        dict(text='Recognized words.', language='en', segments=[dict(text='Recognized words.')]))
    monkeypatch.setitem(sys.modules, 'mlx_whisper', fake)
    from apple_asr import AppleWhisperModel
    model = AppleWhisperModel('large-v3', 'metal', 'float16')
    assert len(calls) == 1
    assert 'initial_prompt' not in calls[0][1]
    segments, info = model.transcribe('wake.wav', beam_size=1, language='en',
        vad_filter=False, condition_on_previous_text=False, hotwords='Hey Bart')
    assert [segment.text for segment in segments] == ['Recognized words.']
    assert info.language == 'en'
    assert calls[-1][1]['initial_prompt'] == 'Hey Bart'
    model.transcribe('message.wav', beam_size=1, language='en',
        vad_filter=False, condition_on_previous_text=False)
    assert 'initial_prompt' not in calls[-1][1]
    assert all(call[1]['path_or_hf_repo'] == 'mlx-community/whisper-large-v3-mlx' for call in calls)
    with pytest.raises(ValueError, match='beam_size=1'):
        model.transcribe('upload.wav', beam_size=5)


def test_listener_selects_one_apple_model_handle(monkeypatch):
    from .test_voice_capture_finish import listener_module
    monkeypatch.setenv('MIC_WHISPER_BACKEND', 'mlx')
    monkeypatch.setenv('MIC_WHISPER_MODEL', 'large-v3')
    monkeypatch.setenv('MIC_WHISPER_DEVICE', 'metal')
    monkeypatch.setenv('MIC_WHISPER_COMPUTE_TYPE', 'float16')
    module = listener_module(monkeypatch)
    calls = []
    monkeypatch.setitem(sys.modules, 'silero_vad', SimpleNamespace(load_silero_vad=lambda:lambda:None))
    monkeypatch.setitem(sys.modules, 'apple_asr', SimpleNamespace(AppleWhisperModel=lambda *args:calls.append(args) or object()))
    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace(WhisperModel=lambda *a,**k:pytest.fail('second ASR residency')))
    listener = module.AlwaysOnListener()
    listener.load_models()
    assert listener.whisper_model is not None
    assert calls == [('large-v3', 'metal', 'float16')]
