"""Bounded Kokoro playback on the configured peer; text is data, never shell code."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import uuid

# This fixed remote program owns only its own process group and temporary WAV.
REMOTE = r'''
import json,os,signal,subprocess,sys,time
from pathlib import Path
p=json.load(sys.stdin)
remaining=p['deadline']-time.time()
if not 0 < remaining <= 35: raise SystemExit('expired or skewed speech deadline')
text=p['text']
if not isinstance(text,str) or not 0<len(text)<=800: raise SystemExit('invalid speech text')
identifier=p['id']
if len(identifier)!=32 or any(c not in '0123456789abcdef' for c in identifier): raise SystemExit('invalid speech ID')
out='/tmp/pentacle-voice-'+identifier+'.wav'
child=None
try:
 script=p.get('script')
 if not isinstance(script,str) or not os.path.isabs(script): raise ValueError('invalid speaker script')
 child=subprocess.Popen([script,text,out],stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
 stdout,stderr=child.communicate(timeout=min(25,remaining))
 if child.returncode: raise RuntimeError('Kokoro playback failed')
 print(json.dumps({'stopped':True,'played':True,'id':identifier}))
finally:
 if child is not None:
  try: os.killpg(child.pid,signal.SIGKILL)
  except ProcessLookupError: pass
  child.wait(timeout=2)
 Path(out).unlink(missing_ok=True)
'''


def speech_command():
    mode = os.environ.get('MIC_VOICE_SPEAKER_MODE', 'ssh')
    if mode == 'local':
        local_speaker_script()
        return [sys.executable, '-c', REMOTE]
    if mode != 'ssh':
        raise ValueError('Unsupported voice speaker mode')
    configured_speaker_script(require_local=False)
    peer = os.environ.get('MIC_VOICE_SPEAKER_SSH', '')
    if not peer or peer.startswith('-') or any(c.isspace() for c in peer):
        raise ValueError('Local voice speaker peer is not configured')
    command = ['/usr/bin/ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', peer,
               'python3 -c '+shlex.quote(REMOTE)]
    if os.name == 'nt':
        command = [str(Path(os.environ.get('SystemRoot', 'C:/Windows'))/'System32'/'wsl.exe'), '-d', 'Ubuntu', '--exec']+command
    return command


def configured_speaker_script(*, require_local):
    script = os.environ.get('MIC_VOICE_SPEAKER_SCRIPT', '')
    if not os.path.isabs(script) or (require_local and (not os.path.isfile(script) or not os.access(script, os.X_OK))):
        raise ValueError('Local voice speaker script must be an executable absolute path')
    return script


def local_speaker_script():
    return configured_speaker_script(require_local=True)


def speak(text, deadline, run=subprocess.run):
    if not isinstance(text, str) or not 0 < len(text) <= 800:
        raise ValueError('Spoken response is too long')
    if run is subprocess.run and os.environ.get('MIC_VOICE_SPEAKER_MODE', 'resident') == 'resident':
        from speaker_service import get_service
        service = get_service()
        if service.silent and 'lines' in service.rules.snapshot()['modes']['silent']['suppresses']:
            return dict(stopped=True, played=False, suppressed=True, reason='silent_mode')
        return service.speaker.speak(text, deadline)
    identifier = uuid.uuid4().hex
    payload = dict(id=identifier, text=text, deadline=deadline)
    payload['script'] = configured_speaker_script(
        require_local=os.environ.get('MIC_VOICE_SPEAKER_MODE', 'ssh') == 'local')
    result = run(speech_command(), input=json.dumps(payload),
                 text=True, capture_output=True, timeout=max(1, deadline-time.time()+5), check=False)
    if result.returncode:
        raise RuntimeError('Speaker playback failed; recognition stays fenced through its deadline')
    receipt = json.loads(result.stdout)
    if receipt.get('id') != identifier or not receipt.get('stopped') or not receipt.get('played'):
        raise RuntimeError('Speaker completion unconfirmed')
    return receipt
