from satellite import _is_codex_rollout_path


def test_named_satellite_root_is_recognized_only_for_exact_account(tmp_path):
    root = tmp_path / "managed" / "sessions"; root.mkdir(parents=True)
    path = root / "native.jsonl"; path.write_text("{}\n")
    account = {"provider": "codex", "transcript_root": str(root)}
    assert _is_codex_rollout_path(str(path), account)
    assert not _is_codex_rollout_path(str(path), {"provider": "codex", "transcript_root": str(tmp_path / "other")})
    assert not _is_codex_rollout_path(str(path))
