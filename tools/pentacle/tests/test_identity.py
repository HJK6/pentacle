import contextlib
import datetime as dt
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pentacle.cli import version_text
from pentacle.install import atomic_json, identity, release_manifest


class IdentityTest(unittest.TestCase):
    def test_cache_is_private_bound_to_source_and_contains_no_remote_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            remote = "https://user:CACHE_CREDENTIAL_MARKER@example.invalid/repo.git"
            def fetched(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 0, '{"schema_version":1,"releases":[]}' if "show" in argv else "", "")
            with patch("pentacle.install.run", side_effect=fetched):
                online = release_manifest(root, remote)
            self.assertEqual(online["source"], "fetched")
            path = root / ".pentacle-deploy/releases-cache.json"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("CACHE_CREDENTIAL_MARKER", path.read_text())
            cached = json.loads(path.read_text())
            self.assertEqual(cached["release_remote"], "custom")
            cached["fetched_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)).isoformat()
            atomic_json(path, cached)
            with patch("pentacle.install.run", return_value=subprocess.CompletedProcess([], 1, "", "SECRET")):
                offline = release_manifest(root, remote)
                wrong = release_manifest(root, "https://example.invalid/another.git")
            self.assertEqual(offline["source"], "cache")
            self.assertGreaterEqual(offline["age_days"], 8)
            self.assertEqual(wrong["source"], "none")
            with patch("pentacle.install.commit", return_value="a" * 40):
                value = identity(root, offline, None, None)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                version_text(value)
            self.assertIn("8 days", output.getvalue())
            self.assertIn("online release status unknown", output.getvalue())
            self.assertNotIn("up to date", output.getvalue())

    def test_development_stable_and_ahead_identities_are_truthful(self):
        a, b, c, ahead = (char * 40 for char in "abcd")
        manifest = {"release_remote": "custom", "source": "fetched", "fetched_at": None,
                    "data": {"releases": [{"version": "2.0.0", "commit": b}, {"version": "2.1.0", "commit": c}]}}
        edges = {(a, b), (a, c), (b, c)}
        for head, version, behind in [(a, None, 2), (b, "2.0.0", 1), (ahead, None, None)]:
            with patch("pentacle.install.commit", return_value=head), patch("pentacle.install.ancestor", side_effect=lambda root, older, newer: (older, newer) in edges):
                value = identity(Path("."), manifest, {"runtime_sha": a}, {"build_commit": "BAD_SECRET", "build_id": "BAD_SECRET"})
            self.assertEqual(value["install"]["version"], version)
            self.assertEqual(value["behind_releases"], behind)
            self.assertIsNone(value["web"]["build_commit"])
            self.assertIsNone(value["web"]["build_id"])


if __name__ == "__main__":
    unittest.main()
