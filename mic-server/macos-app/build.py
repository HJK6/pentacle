#!/usr/bin/env python3
"""Build an owned launcher candidate; never install or alter the template app."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def build(template, output):
    template = template.resolve()
    output = output.resolve()
    for protected in ('/Applications', '/System', '/Library', '/usr'):
        if output.is_relative_to(protected):
            raise ValueError('output must be an owned candidate directory, not an installed app')
    if output.exists():
        raise ValueError('candidate output already exists; refusing overwrite')
    source = Path(__file__).with_name('MicServerLauncher.swift')
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(template)], check=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(template, output, symlinks=True)
    binary = output / 'Contents/MacOS/mic-server-launcher'
    commands = [
        ['/usr/bin/swiftc', '-O', '-target', 'arm64-apple-macos11', '-o', str(binary), str(source)],
        ['/usr/bin/codesign', '--force', '--deep', '--sign', '-', str(output)],
        ['/usr/bin/codesign', '--verify', '--deep', '--strict', str(output)],
    ]
    for command in commands:
        subprocess.run(command, check=True)
    receipt = {'source': str(source), 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
               'binary': str(binary), 'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
               'template': str(template), 'commands': commands, 'installation_performed': False}
    output.with_suffix('.build.json').write_text(json.dumps(receipt, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--template', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    build(args.template, args.output)
