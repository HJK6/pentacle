#!/usr/bin/env python3
"""Generate web icons from the byte-exact public mobile launcher/adaptive sources.

Requires Pillow 13.0.0. Run from any directory; no network or artwork transforms.
"""
import hashlib
import io
import json
import re
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'renderer/assets'
PROVENANCE = ASSETS / 'mobile-icon-sources.json'


def main():
    sources = json.loads(PROVENANCE.read_text())['sources']
    for source in sources.values():
        data = (ASSETS / source['file']).read_bytes()
        if hashlib.sha256(data).hexdigest() != source['sha256']:
            raise ValueError(f"Mobile source changed: {source['file']}; update its provenance first")
    manifest_path = ROOT / 'renderer/manifest.webmanifest'
    manifest = json.loads(manifest_path.read_text())
    changes = {}
    for entry in manifest['icons']:
        size = int(entry['sizes'].split('x')[0])
        kind = 'adaptive' if entry.get('purpose') == 'maskable' else 'launcher'
        stem = f"icon-{'maskable-' if kind == 'adaptive' else ''}{size}"
        old = Path(entry['src']).name
        new = render(sources[kind]['file'], size, stem)
        entry['src'] = 'assets/' + new
        changes[old] = new
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
    for size, stem in [(64, 'favicon'), (180, 'favicon-180')]:
        new = render(sources['launcher']['file'], size, stem)
        pattern = re.compile(r'(?<![\w-])' + re.escape(stem) + r'(?:\.[a-f0-9]{12})?\.png')
        for html in (ROOT / 'renderer').glob('*.html'):
            text = html.read_text()
            for old in pattern.findall(text):
                changes[old] = new
            updated = pattern.sub(new, text)
            if updated != text:
                html.write_text(updated)
    for old, new in changes.items():
        if old != new:
            (ASSETS / old).unlink(missing_ok=True)
    print(json.dumps(changes, indent=2))


def render(source, size, stem):
    with Image.open(ASSETS / source) as image:
        # Use the entire mobile canvas: no recentering, insets, masks or color changes.
        resized = image.resize((size, size), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        resized.save(output, format='PNG')
    data = output.getvalue()
    name = f'{stem}.{hashlib.sha256(data).hexdigest()[:12]}.png'
    (ASSETS / name).write_bytes(data)
    return name


if __name__ == '__main__':
    main()
