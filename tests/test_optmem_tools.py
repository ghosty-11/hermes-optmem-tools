"""Unit contracts for hermes-optmem-tools.

These assert plugin behaviour, not the memo binary. Run from the repo root:

    python3 -m unittest tests.test_optmem_tools -v
    OPTMEM_PLUGIN_PATH=/deployed/__init__.py python3 -m unittest tests.test_optmem_tools -v
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = Path(os.environ.get("OPTMEM_PLUGIN_PATH") or ROOT / "__init__.py")


def _load():
    spec = importlib.util.spec_from_file_location("optmem_tools_under_test", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestHousekeepingMarkers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def test_awake_banner_alone_is_not_a_chore(self):
        out = "#0 2026-08-01 likes tea\nYou are awake."
        self.assertEqual(self.mod._mark_housekeeping(out), out)

    def test_pending_compression_is_labelled(self):
        out = (
            "#0 2026-08-01 likes tea\n"
            "You are awake.\n\n"
            "Compress memories #0-1 into one line of at most 280 bytes.\n"
            "Run: ~/.optmem/memo nap 0-1 \"<your line>\""
        )
        labelled = self.mod._mark_housekeeping(out)
        self.assertIn(self.mod.HOUSEKEEPING_NOTE, labelled)
        self.assertTrue(labelled.startswith(out.rstrip("\n")))


class TestWakePagination(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def test_multipart_wake_follows_snapshot_and_keeps_newest_lines(self):
        first = (
            "Your memory, part 1 of 2, oldest first (2 memories).\n"
            "#0 2026-08-01 oldest\n"
            "Not awake yet. Run: /opt/memo wake 2 2"
        )
        second = (
            "Your memory, part 2 of 2, oldest first (2 memories).\n"
            "#1 2026-08-02 newest\n"
            "You are awake."
        )
        with unittest.mock.patch.object(
            self.mod, "_run", side_effect=[first, second]
        ) as run, unittest.mock.patch.object(
            self.mod, "_cfg", return_value={"wake_lines": 2}
        ):
            result = self.mod._wake_output()
        self.assertEqual("#1 2026-08-02 newest\nYou are awake.", result)
        self.assertEqual(
            [
                unittest.mock.call(["wake"]),
                unittest.mock.call(["wake", "2", "2"]),
            ],
            run.call_args_list,
        )

    def test_final_wake_result_stays_within_output_ceiling(self):
        raw = (
            ("x" * self.mod.OUTPUT_MAX_CHARS)
            + "\nCompress memories #0-1.\nRun: /opt/memo nap"
        )
        with unittest.mock.patch.object(
            self.mod, "_run", return_value=raw
        ), unittest.mock.patch.object(self.mod, "_cfg", return_value={}):
            result = self.mod._handle_wake({})
        self.assertLessEqual(len(result), self.mod.OUTPUT_MAX_CHARS)
        self.assertIn(self.mod.HOUSEKEEPING_NOTE, result)


class TestNoteValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def test_malformed_placeholder_id_is_refused(self):
        result = self.mod._handle_note({"text": "@riverbend id:<number>: likes tea"})
        self.assertIn("refused", result)
        self.assertIn("Nothing was written", result)

    def test_numeric_id_is_not_refused(self):
        with unittest.mock.patch.object(self.mod, "_run", return_value="Saved as #0.") as run:
            result = self.mod._handle_note(
                {"text": "@riverbend id:1000000001: likes tea"}
            )
        self.assertEqual(result, "Saved as #0.")
        run.assert_called_once()

    def test_ascii_over_byte_cap_is_refused(self):
        text = "x" * 281
        result = self.mod._handle_note({"text": text})
        self.assertIn("refused", result)
        self.assertIn("Nothing was written", result)

    def test_multibyte_over_byte_cap_is_refused(self):
        # 200 × U+00E9 is 200 chars and 400 UTF-8 bytes.
        text = "é" * 200
        self.assertGreater(len(text.encode()), 280)
        result = self.mod._handle_note({"text": text})
        self.assertIn("refused", result)
        self.assertNotIn("Saved", result)

    def test_store_entry_chars_override_is_enforced_before_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = Path(tmp)
            (memory / "config").write_text("ENTRY_CHARS = 20\n", encoding="utf-8")
            with unittest.mock.patch.object(
                self.mod, "_cfg", return_value={"memory_dir": str(memory)}
            ), unittest.mock.patch.object(self.mod, "_run") as run:
                result = self.mod._handle_note({"text": "x" * 21})
        self.assertIn("at most 20 bytes", result)
        run.assert_not_called()




class TestRecallSafety(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def test_recall_treats_regex_metacharacters_as_literal_text(self):
        with unittest.mock.patch.object(
            self.mod, "_run", return_value="No match."
        ) as run:
            result = self.mod._handle_recall({"pattern": "(a+)+$"})
        self.assertEqual("No match.", result)
        run.assert_called_once_with(["recall", r"\(a\+\)\+\$"])
class TestRunDoesNotCreateStore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def test_missing_memory_dir_does_not_mkdir(self):
        missing = Path(tempfile.mkdtemp(prefix="optmem-missing-")) / "memory"
        self.assertFalse(missing.exists())
        with unittest.mock.patch.object(self.mod, "_cfg", return_value={
            "memory_dir": str(missing),
            "binary": "/bin/true",
        }), unittest.mock.patch.object(self.mod.os, "makedirs", side_effect=AssertionError(
            "must not create MEMORY_DIR"
        )), unittest.mock.patch.object(self.mod.subprocess, "run") as run:
            result = self.mod._run(["wake"])
        run.assert_not_called()
        self.assertFalse(missing.exists())
        self.assertIn("does not exist", result)


class TestNapIsDisplayOnly(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def test_nap_handler_runs_bare_nap(self):
        with unittest.mock.patch.object(self.mod, "_run", return_value="Nothing left to compress.") as run:
            result = self.mod._handle_nap({})
        self.assertEqual(result, "Nothing left to compress.")
        run.assert_called_once_with(["nap"])

    def test_nap_schema_does_not_claim_to_compress(self):
        desc = self.mod._NAP["description"].lower()
        self.assertNotIn("perform", desc)
        self.assertIn("show", desc)

    def test_note_schema_requires_stable_id_format(self):
        blob = self.mod._NOTE["description"] + self.mod._NOTE["parameters"]["properties"]["text"]["description"]
        self.assertIn("id:<number>", blob)
        self.assertNotIn("'@handle: fact'", blob)




class TestRegistrationReporting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def test_partial_registration_reports_exact_successes(self):
        ctx = unittest.mock.Mock()

        def register_tool(**kwargs):
            if kwargs["name"] == "optmem_note":
                raise RuntimeError("fixture refusal")

        ctx.register_tool.side_effect = register_tool
        with self.assertLogs(self.mod.logger, level="WARNING") as captured:
            self.mod.register(ctx)
        summary = captured.output[-1]
        self.assertIn("registered 3/4 tools", summary)
        self.assertIn("optmem_wake,optmem_recall,optmem_nap", summary)
class TestSkillContracts(unittest.TestCase):
    def test_skill_does_not_mandate_wake_when_unavailable(self):
        text = (ROOT / "skill" / "SKILL.md").read_text()
        self.assertIn("if the tool is available", text.lower())
        self.assertNotIn(
            "If `optmem_note` says a compression is pending, call `optmem_nap` before your",
            text,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
