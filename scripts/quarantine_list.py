"""Read the candidate's reviewed, exact quarantine list."""
from datetime import date, datetime, timezone
import json
from pathlib import Path


QUARANTINE_PATH = 'scripts/quarantine-v2.json'


class ExpiredQuarantine(ValueError):
    def __init__(self, data, expired):
        self.data = data
        super().__init__("expired quarantine: " + ", ".join(expired))


def load_quarantine(root):
    return validate_quarantine(json.loads((Path(root) / QUARANTINE_PATH).read_text()))


def validate_quarantine(data):
    if not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 2 or not isinstance(data.get('nonblocking_tests'), list):
        raise ValueError('invalid quarantine list')
    seen = set()
    expired = []
    for row in data['nonblocking_tests']:
        if not isinstance(row, dict) or set(row) != {'id', 'owner', 'reason', 'expires'}:
            raise ValueError('invalid quarantine row')
        if any(not isinstance(row[k], str) or not row[k].strip() or row[k] != row[k].strip() for k in row):
            raise ValueError('invalid quarantine metadata')
        identity = row['id']
        if '::' not in identity or identity in seen:
            raise ValueError(f'invalid or duplicate quarantine ID: {identity}')
        prefix, _, rest = identity.partition('::')
        if prefix in ('daemon', 'cli', 'shared'):
            file, separator, node = rest.partition('::')
            if not separator or not file.endswith('.py') or not node or file.startswith('/') or '..' in Path(file).parts:
                raise ValueError(f'invalid quarantine ID: {identity}')
        else:
            raise ValueError(f'invalid quarantine ID: {identity}')
        try:
            expiry = date.fromisoformat(row['expires'])
            if expiry.isoformat() != row['expires']:
                raise ValueError()
        except ValueError as exc:
            raise ValueError(f'invalid expiry for {identity}: {row["expires"]}') from exc
        if expiry <= datetime.now(timezone.utc).date():
            expired.append(f'{identity} ({expiry})')
        seen.add(identity)
    if expired:
        raise ExpiredQuarantine(data, expired)
    return data


def mark_quarantined(items, suite, identities):
    """Mark only uniquely identified cases; ambiguous duplicates stay blocking."""
    from collections import Counter
    import pytest

    # Pytest permits inherited markers on modules and classes. Membership is
    # derived from the current list at every collection, including restoration.
    for node in {node for item in items for node in item.listchain()}:
        node.own_markers[:] = [marker for marker in node.own_markers if marker.name != 'quarantined']
    counts = Counter(item.nodeid for item in items)
    for item in items:
        identity = suite + '::' + item.nodeid
        if identity in identities:
            if counts[item.nodeid] == 1:
                item.add_marker(pytest.mark.quarantined)
            else:
                print(f'QUARANTINE AMBIGUOUS, BLOCKING: {identity}', flush=True)
