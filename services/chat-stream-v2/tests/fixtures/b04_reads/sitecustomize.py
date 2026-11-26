"""Observe tracked inputs in Python children launched by B04 mutation fixtures."""
import importlib.util
import json
import os
import sys
from pathlib import Path


def observe(config):
    roots, excluded_file, report_file = config
    excluded = set(json.loads(Path(excluded_file).read_text()))
    roots = tuple(os.path.join(root, '') for root in roots)

    def record(file):
        if not isinstance(file, (str, bytes, os.PathLike)):
            return
        name = os.path.abspath(os.fsdecode(file))
        for root in roots:
            if name.startswith(root):
                relative = name[len(root):]
                if relative.endswith(".pyc") and "__pycache__/" in relative:
                    try:
                        relative = importlib.util.source_from_cache(relative)
                    except ValueError:
                        pass
                if relative in excluded:
                    with open(report_file, 'a', encoding='utf-8') as out:
                        out.write(relative + '\n')
                break
    return record


# Python audit hooks cannot be removed. Install one per worker and enable the
# observer only while a B04 self-test runs. Children live for one command.
_recorder = None
_installed = False


def _audit(event, args):
    if _recorder is not None and event == 'open' and args[2] & os.O_ACCMODE != os.O_WRONLY:
        _recorder(args[0])


def enable(config):
    global _installed, _recorder
    if not _installed:
        sys.addaudithook(_audit)
        _installed = True
    _recorder = observe(config)


def disable():
    global _recorder
    _recorder = None


if __name__ == 'sitecustomize' and 'PENTACLE_B04_READ_GUARD' in os.environ:
    enable(json.loads(os.environ['PENTACLE_B04_READ_GUARD']))
