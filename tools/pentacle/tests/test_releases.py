"""Exercise release policy against actual Git histories and byte mutations."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pentacle.releases import ReleaseError, add, entry_spans, parse, verify, version_key


class ReleasesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="release-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Example Agent")
        self.git("config", "user.email", "agent@example.invalid")
        (self.root / "CHANGELOG.md").write_text("# Changelog\n\n## 2026-01-01\n\n- Example.\n")
        (self.root / "releases").mkdir()
        self.path = self.root / "releases/manifest.json"
        self.path.write_text('{"schema_version":1,"releases":[]}\n')
        self.baseline = self.commit()
        self.evidence = self.root / "journey.json"
        self.evidence.write_text('{"passed":true}\n')

    def git(self, *args):
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        return subprocess.check_output(["git", "-C", str(self.root), *args], env=env,
                                       text=True, stderr=subprocess.DEVNULL).strip()

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "Fixture commit")
        return self.git("rev-parse", "HEAD")

    def append(self, **kwargs):
        values = dict(version="2.0.0", commit=self.baseline, date="2026-01-02",
                      sections=["2026-01-01"], starts=[self.baseline], actions=[], evidence=self.evidence)
        values.update(kwargs)
        return add(self.root, **values)

    def test_empty_manifest_and_first_entry(self):
        self.assertEqual(verify(self.root, "HEAD")["releases"], [])
        entry = self.append()
        self.assertEqual(entry["evidence_sha256"], hashlib.sha256(self.evidence.read_bytes()).hexdigest())
        candidate = self.commit()
        self.assertEqual(verify(self.root, candidate, self.baseline)["releases"], [entry])

    def test_add_preserves_existing_entry_bytes(self):
        self.append()
        first = self.commit()
        before, _ = entry_spans(self.path.read_text())
        self.append(version="2.0.1", commit=first, starts=["2.0.0"])
        after, _ = entry_spans(self.path.read_text())
        self.assertEqual(before, after[:1])
        self.commit()
        self.assertEqual(len(verify(self.root, "HEAD", first)["releases"]), 2)

    def test_changed_entry_rejected_even_if_json_value_is_identical(self):
        self.append()
        first = self.commit()
        self.path.write_text(self.path.read_text().replace('"version": "2.0.0"', '"version" : "2.0.0"'))
        self.commit()
        with self.assertRaisesRegex(ReleaseError, "earlier release entry"):
            verify(self.root, "HEAD", first)

    def test_deleted_entry_rejected(self):
        self.append()
        first = self.commit()
        self.path.write_text('{"schema_version":1,"releases":[]}')
        self.commit()
        with self.assertRaisesRegex(ReleaseError, "earlier release entry"):
            verify(self.root, "HEAD", first)

    def test_nonancestor_release_rejected(self):
        self.git("checkout", "--orphan", "other")
        (self.root / "other.txt").write_text("other")
        unrelated = self.commit()
        self.git("checkout", "--detach", self.baseline)
        self.evidence.write_text('{"passed":true}\n')
        with self.assertRaises(ReleaseError):
            self.append(commit=unrelated)
        self.append()
        data = json.loads(self.path.read_text())
        data["releases"][0]["commit"] = unrelated
        self.path.write_text(json.dumps(data))
        self.commit()
        with self.assertRaisesRegex(ReleaseError, "not an ancestor"):
            verify(self.root, "HEAD")

    def test_add_refusals_leave_manifest_bytes_unchanged(self):
        for kwargs in [dict(starts=[]), dict(sections=["2099-01-01"]),
                       dict(evidence=self.root / "missing"), dict(date="2026-02-30"),
                       dict(version="02.0.0"), dict(commit="0" * 40)]:
            with self.subTest(kwargs=kwargs):
                before = self.path.read_bytes()
                with self.assertRaises(ReleaseError):
                    self.append(**kwargs)
                self.assertEqual(self.path.read_bytes(), before)

    def test_increasing_versions_unique_commits_and_direct_previous_start(self):
        self.append()
        first = self.commit()
        for kwargs in [dict(version="2.0.0", commit=first), dict(version="1.9.9", commit=first),
                       dict(version="2.0.1"), dict(version="2.0.1", commit=first, starts=[first])]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ReleaseError):
                self.append(**kwargs)

    def test_semantic_version_order_and_manifest_shape(self):
        self.assertLess(version_key("2.9.0"), version_key("2.10.0"))
        for text in ['{"schema_version":true,"releases":[]}',
                     '{"schema_version":1,"schema_version":1,"releases":[]}',
                     '{"schema_version":1,"releases":[],"extra":null}', 'null']:
            with self.subTest(text=text), self.assertRaises(ReleaseError):
                parse(text)

    def test_manifest_introduction_against_base_without_manifest(self):
        self.path.unlink()
        old = self.commit()
        self.path.write_text('{"schema_version":1,"releases":[]}')
        self.commit()
        self.assertEqual(verify(self.root, "HEAD", old)["releases"], [])


if __name__ == "__main__":
    unittest.main()
