"""Fleet vocabulary + name-correction config (adapter route data layer).

spec_pentacle_mobile__voice_input_thirdhost_transcription_2026_09 § Fixtures:
corrections are whole-word, case-insensitive, applied only to listed variants,
never inside a longer word — so a decoy the speaker actually said is not rewritten
into a fleet name (C5: zero decoy false positives). The prompt+map are versioned
together in vocabulary_version.
"""

import json
import os
import tempfile
import shutil
import unittest
import unittest.mock

import fleet_vocabulary as fv


class ApplyCorrectionsTest(unittest.TestCase):
    def test_whole_word_case_insensitive(self):
        m = {"lumah": "Thirdhost"}
        self.assertEqual(fv.apply_corrections("call lumah now", m), "call Thirdhost now")
        self.assertEqual(fv.apply_corrections("Call LUMAH", m), "Call Thirdhost")

    def test_never_inside_a_longer_word(self):
        m = {"lumah": "Thirdhost"}
        # "lumaholder" contains "lumah" but must not be rewritten.
        self.assertEqual(fv.apply_corrections("a lumaholder task", m), "a lumaholder task")

    def test_multi_word_variant(self):
        m = {"cloud kit": "Samplehost"}
        self.assertEqual(fv.apply_corrections("ask cloud kit", m), "ask Samplehost")

    def test_decoy_not_in_map_is_untouched(self):
        # The decoy phrase the speaker really said stays as-is (not a fleet name).
        m = {"lumah": "Thirdhost"}
        self.assertEqual(fv.apply_corrections("a cloud kite", m), "a cloud kite")
        self.assertEqual(fv.apply_corrections("that is a tot", m), "that is a tot")

    def test_longest_variant_wins(self):
        m = {"bart": "Bart", "bart's memo": "Bartimaeus's memo"}
        self.assertEqual(fv.apply_corrections("read bart's memo", m), "read Bartimaeus's memo")

    def test_empty_inputs(self):
        self.assertEqual(fv.apply_corrections("", {"a": "b"}), "")
        self.assertEqual(fv.apply_corrections("text", {}), "text")


class LoadVocabularyTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)  # owned synthetic fixture directory only

    def _write(self, name, content):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return path

    def test_loads_prompt_and_corrections(self):
        vf = self._write("vocab.txt", "Bartimaeus, Samplehost, Thirdhost\n")
        cf = self._write("corr.json", json.dumps({"lumah": "Thirdhost"}))
        prompt, corrections, version = fv.load_vocabulary(vocabulary_file=vf, corrections_file=cf)
        self.assertEqual(prompt, "Bartimaeus, Samplehost, Thirdhost")
        self.assertEqual(corrections, {"lumah": "Thirdhost"})
        self.assertTrue(version.startswith("fleet-"))

    def test_missing_files_yield_none_and_empty(self):
        prompt, corrections, version = fv.load_vocabulary(
            vocabulary_file="/no/such/vocab", corrections_file="/no/such/corr",
        )
        self.assertIsNone(prompt)
        self.assertEqual(corrections, {})
        self.assertTrue(version.startswith("fleet-"))

    def test_version_is_deterministic_and_content_sensitive(self):
        v1 = fv.vocabulary_version("Thirdhost", {"lumah": "Thirdhost"})
        v2 = fv.vocabulary_version("Thirdhost", {"lumah": "Thirdhost"})
        v3 = fv.vocabulary_version("Thirdhost", {"lumah": "Thirdhost", "tot": "Thirdhost"})
        v4 = fv.vocabulary_version("Samplehost", {"lumah": "Thirdhost"})
        self.assertEqual(v1, v2)
        self.assertNotEqual(v1, v3)
        self.assertNotEqual(v1, v4)

    def test_reads_from_environment(self):
        vf = self._write("vocab.txt", "Thirdhost")
        with unittest.mock.patch.dict(os.environ, {"MIC_VOCABULARY_FILE": vf}, clear=False):
            os.environ.pop("MIC_NAME_CORRECTIONS", None)
            prompt, corrections, _ = fv.load_vocabulary()
        self.assertEqual(prompt, "Thirdhost")
        self.assertEqual(corrections, {})


if __name__ == "__main__":
    unittest.main()
