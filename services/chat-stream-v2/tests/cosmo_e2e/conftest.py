"""Make the disposable Cosmo server importable for the cosmo rows.

The Foundation half needs nothing extra.  The cosmo rows import cosmo_server
from a sibling checkout discovered relocatably (COSMO_SERVER_DIR env, else a
sibling worktree); if it is unavailable (e.g. the public CI gate, which has no
cosmo checkout), those rows skip with an explicit reason — never a silent pass.
No host-private path literal lives here.
"""
import sys


def pytest_configure(config):
    from tests.cosmo_e2e import harness as H
    cosmo_dir = H.cosmo_server_dir()
    if cosmo_dir and cosmo_dir not in sys.path:
        sys.path.insert(0, cosmo_dir)
