"""Creation-to-comparable check (spec_triforce_memory__work_item_estimate_actual_loop_2026_10, AC1b(b)).

`fixtures/work_items_kind/` is the verbatim output of triforce-memory
`scripts/new_work_item.py kind-fixture estimate_loop_demo_2026_10 --kind feature`
(SHA256 pinned below). Marked completed in a temporary work root, it must be
selectable by `usage rollup --comparables --kind feature` and not by `--kind defect`.
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from test_usage_rollup import Fx

ITEM = 'kind-fixture__estimate_loop_demo_2026_10'
SPEC_ID = 'spec_kind_fixture__estimate_loop_demo_2026_10'
FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'work_items_kind' / ITEM
SHA256 = {
    'spec.md': '03ea92c97f690e4bd9b7b1a9568ea979a993448c5452905a4eac3523ac580312',
    'summary.md': 'efed81550d413d14a4e311d4b8f425e1090b8ea6766bbbc309ba7f9772a4ffe3',
}


@pytest.fixture
def fx(tmp_path: Path) -> Fx:
    return Fx(tmp_path)


def _complete(work: Path) -> None:
    """Copy the script output and move it to completed the way a lead does (status, source_path, completed_at)."""
    target = work / 'completed' / ITEM
    shutil.copytree(FIXTURE, target)
    for name in ('spec.md', 'summary.md'):
        path = target / name
        text = path.read_text()
        text = text.replace('status: backlog\n', "status: completed\ncompleted_at: '2026-10-07'\n", 1)
        text = text.replace(f'work/backlog/{ITEM}/', f'work/completed/{ITEM}/', 1)
        path.write_text(text)


def test_usage_rollup_kind_fixture_is_verbatim_script_output() -> None:
    for name, digest in SHA256.items():
        assert hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest() == digest, name
    spec = (FIXTURE / 'spec.md').read_text()
    assert 'tags:\n- kind-fixture\n- feature\n' in spec
    assert '## Estimate' in spec and '- kind: feature' in spec and '- basis: none' in spec


def test_usage_rollup_kind_fixture_selected_by_kind(fx: Fx) -> None:
    _complete(fx.work)
    fx.seat(f'thoth:{ITEM}', created='2026-10-07T00:00:00Z', closed='2026-10-07T02:00:00Z', specs=(SPEC_ID,))
    fx.rec(f'thoth:{ITEM}', {'uncached_input': 0, 'cache_read': 1_000_000, 'cache_write': 0, 'output': 0})

    feature = fx.run('--comparables', '--repo', 'kind-fixture', '--kind', 'feature')['comparables']
    defect = fx.run('--comparables', '--repo', 'kind-fixture', '--kind', 'defect')['comparables']

    assert [r['spec_id'] for r in feature['rows']] == [SPEC_ID]
    assert feature['rows'][0]['kind'] == 'feature' and feature['rows'][0]['elapsed_delivery_h'] == 2.0
    assert defect['rows'] == [] and defect['excluded'] == []
