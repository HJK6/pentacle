import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from quarantine_list import load_quarantine


def test_list_validation_and_expiry(tmp_path):
    path = tmp_path / 'scripts/quarantine-v2.json'
    path.parent.mkdir()
    row = {'id': 'daemon::tests/test_a.py::test_a[one]', 'owner': 'team', 'reason': 'flaky', 'expires': '2099-01-01'}
    def write(rows):
        path.write_text(json.dumps({'version': 2, 'nonblocking_tests': rows}))
    write([row])
    assert load_quarantine(tmp_path)['nonblocking_tests'] == [row]
    for invalid in ([row, row], [{**row, 'expires': '2000-01-01'}], [{**row, 'id': 'test/a.js::case'}], [{**row, 'reason': ''}], [{**row, 'id': 'daemon::tests/test_a.py'}]):
        write(invalid)
        with pytest.raises(ValueError):
            load_quarantine(tmp_path)
