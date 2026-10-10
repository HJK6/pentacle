"""Run canonical merge gate with no inherited fleet/seat identity or live opt-ins."""
import json, os, subprocess, sys
from pathlib import Path
def scrub_environment(environment):
    env = dict(environment)
    removed = sorted(k for k in env if k.startswith(('PENTACLE_', 'AGENT_ORCH_', 'COSMO_', 'TMUX')))
    for key in removed:
        del env[key]
    return env, removed


def main():
    source, output = map(lambda s: Path(s).resolve(), sys.argv[1:3])
    output.mkdir(parents=True, exist_ok=True)
    env, removed = scrub_environment(os.environ)
    env.update(PYTHONDONTWRITEBYTECODE='1', V2_PYTHON_BIN=sys.executable,
               V2_GATE_EVIDENCE_DIR=str(output))
    (output/'environment-scrub.json').write_text(json.dumps({'removed_names':removed,
        'remaining_identity_names':[k for k in env if k.startswith(('PENTACLE_', 'AGENT_ORCH_', 'TMUX'))],
        'values_recorded':False}, indent=2)+'\n')
    raise SystemExit(subprocess.call([sys.executable, str(source/'services/chat-stream-v2/tools/run_gate.py'),
        'merge', '--evidence-dir', str(output/'canonical-gate'), '--evidence-out', str(output/'canonical-gate.json'),
        '--basetemp', str(output/'gate-tmp')], env=env, cwd=source/'services/chat-stream-v2'))

if __name__ == '__main__':
    main()
