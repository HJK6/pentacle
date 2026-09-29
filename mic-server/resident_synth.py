"""Owned resident Kokoro worker. JSON in, file receipts out; never opens a device."""
import json
import os
from pathlib import Path
import resource
import sys
import time
import uuid


def main():
    import soundfile as sf
    from kokoro_onnx import Kokoro
    root = Path(os.environ['MIC_SPEAKER_OUTPUT_DIR']).resolve()
    root.mkdir(parents=True, exist_ok=True)
    import onnxruntime as ort
    options = ort.SessionOptions()
    options.intra_op_num_threads = int(os.environ.get('MIC_KOKORO_THREADS', '2'))
    options.inter_op_num_threads = 1
    options.add_session_config_entry('session.intra_op.allow_spinning', '0')
    session = ort.InferenceSession(os.environ['MIC_KOKORO_MODEL'], sess_options=options, providers=['CPUExecutionProvider'])
    model = Kokoro.from_session(session, os.environ['MIC_KOKORO_VOICES'])
    voice = os.environ.get('MIC_KOKORO_VOICE', 'bm_george')
    if voice not in model.get_voices():
        raise ValueError('Configured voice is unavailable')
    print(json.dumps(dict(ready=True, model_loads=1, voice=voice,
                         rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                         threads=options.intra_op_num_threads, execution_provider='CPUExecutionProvider',
                         priority=os.getpriority(os.PRIO_PROCESS, 0) if hasattr(os, 'getpriority') else None)), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        started = time.monotonic()
        try:
            text = request['text']
            if not isinstance(text, str) or not 0 < len(text) <= 800:
                raise ValueError('Invalid synthesis text')
            samples, rate = model.create(text, voice=voice, speed=1.0, lang='en-gb')
            path = root / (uuid.uuid4().hex + '.wav')
            sf.write(str(path), samples, rate)
            receipt = dict(path=str(path), duration=len(samples)/rate,
                           synthesis_seconds=time.monotonic()-started,
                           rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        except Exception as exc:
            receipt = dict(error=str(exc))
        print(json.dumps(receipt), flush=True)


if __name__ == '__main__':
    main()
