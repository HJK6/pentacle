"""Installed scorecard writer entry point and production publisher wiring."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

from .quarantine_adapter import ProductionAdapter
from .quarantine_decisions import MemoryPublisher
from .reliability_occurrences import SURFACES
from .reliability_receipts import instant, read
from .reliability_score import source_paths
from .reliability_writer import WorkWriter


def run(config, now, surfaces=SURFACES):
    memory = Path(config['memory_root']).resolve()
    path = Path(config['sources']).expanduser().resolve(strict=True)
    sources = source_paths(read(path), path.parent)
    work = sources['work']
    if Path(work['work_root']).resolve() != memory / 'work':
        raise ValueError('work publisher and scorecard root disagree')
    for name in ('owner', 'epic', 'machine'):
        if not isinstance(config.get(name), str) or not config[name]:
            raise ValueError('scorecard publisher configuration incomplete')
    publisher = MemoryPublisher(memory, config['owner'], config['epic'], config['machine'],
                                requesting_stream_id=config.get('requesting_stream_id'))
    adapter = ProductionAdapter(None, checker='/dev/null', packet_root=memory,
                                agent_orch=config.get('agent_orch', 'agent-orch'))
    return WorkWriter(work, config, publisher, adapter.scorecard_alert).run(sources, now, surfaces)


def command(args):
    try:
        now = instant(args.as_of) if args.as_of else datetime.now(timezone.utc)
        result = run(read(Path(args.config).expanduser()), now, (args.surface,) if args.surface else SURFACES)
    except (KeyError, OSError, TypeError, ValueError, subprocess.SubprocessError) as exc:
        result = {'status': 'incomplete', 'issues': [{'kind': 'configuration', 'error': type(exc).__name__}]}
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] == 'complete' else 2


def add_parser(subcommands):
    parser = subcommands.add_parser('reconcile', help='Replay misses through the work publisher')
    parser.add_argument('--config', required=True, help='Private scorecard writer configuration')
    parser.add_argument('--as-of', help='UTC cutoff (ISO 8601 with timezone)')
    parser.add_argument('--surface', choices=SURFACES, help='Reconcile one source')
    parser.set_defaults(func=command)
