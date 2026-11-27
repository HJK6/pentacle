from pathlib import Path

from tools.gate_selection import resolve
from tools.run_gate import MERGE_TIERS


def test_resolves_import_chain_changed_test_table_and_unmapped(tmp_path):
    service = tmp_path / 'services/chat-stream-v2'
    (service / 'tests').mkdir(parents=True)
    (service / 'leaf.py').write_text('VALUE = 1\n')
    (service / 'middle.py').write_text('import leaf\n')
    (service / 'top.py').write_text('from middle import VALUE\n')
    (service / 'tests/test_chain.py').write_text('import top\n')
    (service / 'tests/test_changed.py').write_text('pass\n')
    (service / 'tests/test_link.py').write_text('pass\n')
    prefix = 'services/chat-stream-v2/'
    selected, unmapped = resolve(tmp_path, [prefix + 'leaf.py', prefix + 'tests/test_changed.py',
                                          prefix + 'data.json', 'other.txt'],
                                 {prefix + 'data.json': ['tests/test_link.py']})
    assert selected == ['tests/test_chain.py', 'tests/test_changed.py', 'tests/test_link.py']
    assert unmapped == ['other.txt']
    assert MERGE_TIERS == ('contract', 'unit', 'smoke')
