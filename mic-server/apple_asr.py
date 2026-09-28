"""Optional Apple runtime for the established large-v3 live speech contract."""
import threading
from types import SimpleNamespace

import numpy as np


class AppleWhisperModel:
    repository = 'mlx-community/whisper-large-v3-mlx'

    def __init__(self, model, device, compute_type):
        if (model, device, compute_type) != ('large-v3', 'metal', 'float16'):
            raise ValueError('Apple ASR requires large-v3/metal/float16; no fallback')
        import mlx_whisper
        self.runtime = mlx_whisper
        self.lock = threading.RLock()
        # Compile before publishing readiness. This is internal silence, never
        # a capture event, wake result or retained recording.
        with self.lock:
            self.runtime.transcribe(np.zeros(16000, dtype=np.float32),
                path_or_hf_repo=self.repository, language='en',
                condition_on_previous_text=False, verbose=None)

    def transcribe(self, audio, *, beam_size=1, language='en', vad_filter=False,
                   condition_on_previous_text=False, hotwords=None, initial_prompt=None):
        if beam_size != 1 or vad_filter:
            raise ValueError('Apple live ASR supports beam_size=1 and external VAD only')
        options = dict(path_or_hf_repo=self.repository, language=language,
                       condition_on_previous_text=condition_on_previous_text, verbose=None)
        # MLX greedy decoding is the established beam_size=1 path. MLX has no
        # beam-search implementation; refuse other beam counts explicitly.
        if initial_prompt is not None or hotwords:
            options['initial_prompt'] = initial_prompt if initial_prompt is not None else hotwords
        with self.lock:
            result = self.runtime.transcribe(audio, **options)
        segments = [SimpleNamespace(**segment) for segment in result['segments']]
        return iter(segments), SimpleNamespace(language=result['language'])
