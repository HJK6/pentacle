"""Top-level daemon import shim for the shared CLI/daemon format contract."""
from pathlib import Path
import sys
_SERVICES_ROOT = Path(__file__).resolve().parents[1]
if str(_SERVICES_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICES_ROOT))
from _shared import chat_attachment_types as _shared_module
sys.modules[__name__] = _shared_module
