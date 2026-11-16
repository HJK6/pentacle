"""Compatibility shim for legacy v2 top-level imports.

Delete after v2 callers and downstream tooling no longer import the legacy
module name. The canonical implementation is in ``services/_shared``.
"""

from pathlib import Path
import sys


_SERVICES_ROOT = Path(__file__).resolve().parents[1]
if str(_SERVICES_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICES_ROOT))

from _shared import assets_store as _shared_module  # noqa: E402

sys.modules[__name__] = _shared_module
