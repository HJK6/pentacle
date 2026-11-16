"""Read-only dependency assertion under the selected runtime interpreter."""
import importlib.metadata, importlib.util, json, sys
from pathlib import Path
assert importlib.util.find_spec('watchdog') is None, 'watchdog importable: refuse activation'
try:
    importlib.metadata.distribution('watchdog')
except importlib.metadata.PackageNotFoundError:
    pass
else:
    raise AssertionError('watchdog installed: refuse activation')
for filename in sys.argv[1:]:
    lines = [s.split('#',1)[0].strip() for s in Path(filename).read_text().splitlines()]
    assert not any(lines), 'runtime overlay must be empty; refuse unknown companion'
print(json.dumps({'watchdog_importable':False,'watchdog_installed':False,'overlay_empty':True,'interpreter':sys.executable}))
