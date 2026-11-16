import importlib.util
from pathlib import Path

path = Path(__file__).resolve().parents[1] / '.github/ci/hermetic_python/specs_parser.py'
spec = importlib.util.spec_from_file_location('public_specs_parser', path)
parser = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parser)


def test_default_statuses_and_configured_statuses(tmp_path):
    defaults, order = parser.parse_statuses_json(tmp_path)
    assert order['analysis'] < order['in_progress']
    assert any(row['is_terminal'] for row in defaults)
    (tmp_path / 'work').mkdir()
    (tmp_path / 'work/statuses.json').write_text('{"statuses":[{"name":"custom","order":7}]}')
    assert parser.parse_statuses_json(tmp_path)[1] == {'custom': 7}


def test_work_folder_declared_identity_headings_and_drift(tmp_path):
    folder = tmp_path / 'example__topic'; folder.mkdir()
    (folder / 'spec.md').write_text('---\nid: spec_example\ntitle: Example\nstatus: completed\nmachine: node1\n---\n## Goal\nSynthetic goal\n')
    (folder / 'summary.md').write_text('---\nstatus: completed\n---\n## Next action\nSynthetic action\n')
    row = parser.parse_work_folder(folder, 'in_progress', parser.DEFAULT_STATUSES)
    assert row['id'] == 'spec_example'
    assert row['repo'] == 'example' and row['topic'] == 'topic'
    assert row['goal_excerpt'] == 'Synthetic goal'
    assert row['next_action'] == 'Synthetic action'
    assert row['frontmatter_drift'] and row['terminal_state_drift']
    assert parser.is_sync_conflict('spec.sync-conflict-20260101.md')
