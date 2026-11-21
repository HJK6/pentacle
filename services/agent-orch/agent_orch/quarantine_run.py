"""One scheduled, restart-safe quarantine reconciliation pass."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from .quarantine_adapter import ProductionAdapter
from .quarantine_decisions import DecisionStore, MemoryPublisher
from .quarantine_sweep import load_sources, sweep
from .quarantine_resolution import _report, verify_restoration
from .quarantine_transition import _key


def run(config):
    required = ('sources', 'memory_root', 'owner', 'epic', 'machine', 'checker',
                'packet_root', 'ledger', 'state')
    if any(not isinstance(config.get(k), str) or not config[k] for k in required):
        raise ValueError('quarantine configuration incomplete')
    if any(not Path(config[k]).is_absolute() for k in
           ('sources', 'memory_root', 'checker', 'packet_root', 'ledger', 'state')):
        raise ValueError('quarantine paths must be absolute')
    sources = load_sources(config['sources'])
    if sources['hosted']['repository'] != 'HJK6/pentacle':
        raise ValueError('unexpected scorecard repository')
    memory = Path(config['memory_root']).resolve()
    work = sources['work']
    if Path(work['work_root']).resolve() != memory / 'work':
        raise ValueError('work publisher and scorecard root disagree')
    publisher = MemoryPublisher(memory, config['owner'], config['epic'], config['machine'],
                                requesting_stream_id=config.get('requesting_stream_id'))
    decisions = DecisionStore(work['work_root'], work['register'], publisher)
    command = config.get('agent_orch', 'agent-orch')
    adapter = ProductionAdapter(decisions, checker=config['checker'], packet_root=config['packet_root'],
                                agent_orch=command,
                                restoration_verifier=lambda test, proof: verify_restoration(
                                    test, proof, work['selector_checkout'],
                                    get_report=lambda report_id: _report(report_id, command)))
    restorations = {}
    if config.get('restorations_file'):
        path = Path(config['restorations_file'])
        if not path.is_absolute():
            raise ValueError('restorations path must be absolute')
        restorations = json.loads(path.read_text())
        if not isinstance(restorations, dict):
            raise ValueError('invalid restorations')
    return sweep(sources, ledger_path=config['ledger'], state_path=config['state'], adapter=adapter,
                 restorations=restorations)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    args = parser.parse_args(argv)
    config = {}
    quarantine_exit = 0
    try:
        config = json.loads(Path(args.config).read_text())
        run(config)
    except Exception as exc:
        # Alerts must not include arbitrary source or subprocess output.
        try:
            adapter = ProductionAdapter(None, checker=config.get('checker', '/dev/null'),
                                        packet_root=config.get('packet_root', '/tmp'),
                                        agent_orch=config.get('agent_orch', 'agent-orch'))
            adapter.alert(_key('sweep-error', type(exc).__name__),
                          'Quarantine sweep incomplete: ' + type(exc).__name__)
        except Exception:
            print('Quarantine sweep and its notification are incomplete', file=sys.stderr)
        else:
            print('Quarantine sweep incomplete', file=sys.stderr)
        quarantine_exit = 2
    writer = config.get('scorecard_writer')
    if writer:
        from .reliability_run import run as reconcile_work
        try:
            result = reconcile_work(load_sources(writer), datetime.now(timezone.utc))
        except Exception as exc:
            result = {'status': 'incomplete', 'issues': [{'alert': 'unconfirmed', 'error': type(exc).__name__}]}
        if any(issue['alert'] == 'unconfirmed' for issue in result['issues']):
            try:
                adapter = ProductionAdapter(None, checker='/dev/null', packet_root='/tmp',
                                            agent_orch=config.get('agent_orch', 'agent-orch'))
                adapter.scorecard_alert(_key('scorecard-worker', writer), 'Scorecard reconciliation is incomplete.')
            except Exception:
                print('Scorecard reconciliation and its notification are incomplete', file=sys.stderr)
                return 2
    if not quarantine_exit:
        print('Quarantine sweep complete')
    return quarantine_exit


if __name__ == '__main__':
    raise SystemExit(main())
