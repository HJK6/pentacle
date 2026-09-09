"""Pytest path bootstrap for tests that run outside a service checkout."""

from pathlib import Path
import sys


SERVICES_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICES_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICES_ROOT))
