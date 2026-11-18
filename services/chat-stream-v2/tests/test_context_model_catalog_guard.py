"""Context-window table stays consistent with the spawn catalog."""
from __future__ import annotations

import pytest

import context_adapters
from _shared import spawn_profiles
from context_adapters import ContextReading, context_fields


@pytest.mark.parametrize("model", ["claude-fable-5-1", "claude-fable-5-2"])
def test_fable_models_declare_an_explicit_1m_window(model):
    # Explicit rows: the 1,000,000 fallback default would pass without them.
    assert context_adapters._CLAUDE_MODEL_WINDOWS[model] == 1_000_000
    assert context_fields("claude", ContextReading(1, model=model))[1] == 1_000_000


def test_every_declared_claude_window_is_spawnable_or_intentionally_not():
    assert spawn_profiles.unspawnable_gap("claude", context_adapters._CLAUDE_MODEL_WINDOWS) == set()


def test_catalog_gap_is_warned_on_successful_reload(tmp_path, monkeypatch, caplog):
    import json
    import os
    config = json.loads(spawn_profiles.SPAWN_DEFAULTS_PATH.read_text())
    path = tmp_path / 'spawn_defaults.json'
    path.write_text(json.dumps(config))
    monkeypatch.setattr(spawn_profiles, 'SPAWN_DEFAULTS_PATH', path)
    spawn_profiles.register_known_ids('claude', context_adapters._CLAUDE_MODEL_WINDOWS)
    spawn_profiles.load_spawn_config.cache_clear()
    try:
        spawn_profiles.load_spawn_config()
        del config['models']['claude']['claude-fable-5-1']
        before = path.stat().st_mtime_ns
        path.write_text(json.dumps(config))
        os.utime(path, ns=(before + 1000000, before + 1000000))
        assert spawn_profiles.resolve_spawn(provider='codex', model='sol')['model'] == 'gpt-6.2-sol'
        assert 'spawn_catalog_gap' in caplog.text
        assert 'claude-fable-5-1' in caplog.text
        assert spawn_profiles.unspawnable_gap('claude', context_adapters._CLAUDE_MODEL_WINDOWS) == {'claude-fable-5-1'}
    finally:
        spawn_profiles.load_spawn_config.cache_clear()


def test_register_known_ids_after_cache_warm_warns(caplog):
    spawn_profiles.load_spawn_config()
    try:
        spawn_profiles.register_known_ids('claude', [*context_adapters._CLAUDE_MODEL_WINDOWS, 'test-unknown-model'])
        assert 'spawn_catalog_gap' in caplog.text and 'test-unknown-model' in caplog.text
    finally:
        spawn_profiles.register_known_ids('claude', context_adapters._CLAUDE_MODEL_WINDOWS)
        spawn_profiles.load_spawn_config.cache_clear()


def test_context_modules_import_without_shared():
    import subprocess
    import sys
    from pathlib import Path
    daemon = str(Path(__file__).resolve().parents[1])
    for module in ('context_adapters', 'ingest', 'event_push'):
        result = subprocess.run(
            [sys.executable, '-I', '-c',
             f'import sys; sys.path[:0] = [{daemon!r}]; import {module}'],
            capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, f'{module}: {result.stderr}'


def test_routing_integrity_registers_claude_ids():
    import subprocess
    import sys
    from pathlib import Path
    daemon = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, '-I', '-c',
         f'import sys; sys.path[:0] = {[str(daemon.parent), str(daemon)]!r}; '
         'import routing_integrity, context_adapters; '
         'from _shared import spawn_profiles; '
         "assert spawn_profiles._known_ids['claude'] == "
         'frozenset(context_adapters._CLAUDE_MODEL_WINDOWS)'],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
