"""Operator review/import workflow for legacy companion OptMem memories.

The CLI is the bounded, human-gated escape hatch from legacy quarantine: the
operator lists quarantined store lines by digest, approves one for a subject or
public audience, and the digest-only scope ledger makes it visible to the
matching recall audience. Original store text is never edited; the ledger never
duplicates fact text. Nothing here is model-callable.

"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import threading
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "optmem_review.py"
PLUGIN = Path(os.environ.get("OPTMEM_PLUGIN_PATH") or ROOT / "__init__.py")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


REVIEW_LINE = ("@riverbend id:111: likes oolong and long walks by the flood "
               "channel at dusk")
FOREIGN_LINE = "@kestrel id:222: keeps bees"
PLAIN_LINE = "the greenhouse door sticks in august"
LOG = f"#0 2026-08-01 {REVIEW_LINE}\n{FOREIGN_LINE}\n{PLAIN_LINE}\n"


class ReviewToolTestCase(unittest.TestCase):
    def setUp(self):
        self.review = _load(SCRIPT, "optmem_review_under_test")
        self.plugin = _load(PLUGIN, "optmem_review_plugin_under_test")
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="optmem-review-"))
        self.memory_dir = Path(tmp)
        (self.memory_dir / "LOG.txt").write_text(LOG, encoding="utf-8")
        self.entries = self._store_entries()

    def _store_entries(self):
        entries, err = self.review._store_lines(self.memory_dir)
        self.assertIsNone(err)
        return {self.review._fact_digest_of(f)[:16]: f for _i, f in entries}

    def _digest(self, needle: str) -> str:
        for digest, fact in self.entries.items():
            if needle in fact:
                return digest
        raise AssertionError(f"no store line contains {needle!r}")

    def _run(self, *argv):
        return self.review.main(["--memory-dir", str(self.memory_dir), *argv])

    def _ledger_rows(self):
        path = self.memory_dir / self.review.SCOPE_DB_NAME
        if not path.exists():
            return []
        conn = sqlite3.connect(path)
        try:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute("SELECT * FROM note_scope")]
        finally:
            conn.close()

    def _visible(self, fact, subject_keys, audiences):
        return self.plugin._visible_lines(
            fact, set(subject_keys), set(audiences), str(self.memory_dir))


class TestListing(ReviewToolTestCase):
    def test_list_shows_digests_and_truncated_preview_without_creating_ledger(self):
        with mock.patch("builtins.print") as printed:
            code = self._run("list")
        self.assertEqual(code, 0)
        lines = [call.args[0] for call in printed.call_args_list]
        self.assertTrue(any(self._digest("riverbend") in line for line in lines))
        self.assertTrue(any("unreviewed" in line for line in lines))
        # Long line truncated to the preview bound; the tail never prints.
        self.assertFalse(any("channel at dusk" in line for line in lines))
        self.assertTrue(any("…" in line for line in lines))
        self.assertFalse((self.memory_dir / self.review.SCOPE_DB_NAME).exists(),
                         "list must not create the ledger")

    def test_show_prints_one_full_line_to_the_operator(self):
        with mock.patch("builtins.print") as printed:
            code = self._run("show", self._digest("kestrel"))
        self.assertEqual(code, 0)
        self.assertIn(FOREIGN_LINE, [call.args[0] for call in printed.call_args_list])

    def test_missing_log_txt_refused(self):
        (self.memory_dir / "LOG.txt").unlink()
        with mock.patch("builtins.print") as printed:
            code = self._run("list")
        self.assertEqual(code, 1)
        self.assertIn("refused", printed.call_args_list[0].args[0])

    def test_operator_provenance_only_fact_remains_in_review_queue(self):
        with mock.patch.object(self.plugin, "_memory_dir",
                               return_value=str(self.memory_dir)), \
             mock.patch.object(self.plugin, "_run",
                               return_value="Saved as #0."):
            self.assertEqual(
                self.plugin._handle_note({"text": FOREIGN_LINE}), "Saved as #0."
            )
        self.assertEqual(self._ledger_rows()[0]["audience"], "operator")
        with mock.patch("builtins.print") as printed:
            self.assertEqual(self._run("list"), 0)
        lines = [str(call.args[0]) for call in printed.call_args_list]
        self.assertTrue(any("kestrel" in line and "unreviewed" in line for line in lines), lines)
        self.assertFalse(any("approved:operator" in line for line in lines), lines)
    def test_existing_ledger_read_failure_does_not_claim_unreviewed(self):
        self.assertEqual(self._run("approve", self._digest("riverbend")), 0)
        open_ro = self.review._ledger_conn_ro
        def deny_read(path):
            conn = open_ro(path)
            conn.set_authorizer(
                lambda action, *_: sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_SELECT else sqlite3.SQLITE_OK
            )
            return conn
        with mock.patch.object(self.review, "_ledger_conn_ro", side_effect=deny_read), \
             mock.patch("builtins.print") as printed:
            code = self._run("list")
        self.assertEqual(code, 1)
        self.assertTrue(any("ledger" in str(call.args[0]).lower()
                            for call in printed.call_args_list))



class TestApproval(ReviewToolTestCase):
    def test_approve_subject_only_makes_known_person_recalled(self):
        code = self._run("approve", self._digest("riverbend"))
        self.assertEqual(code, 0)
        rows = self._ledger_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subject"], "id:111")
        self.assertEqual(rows[0]["audience"], "id:111")
        self.assertEqual(rows[0]["author"], "operator")
        self.assertEqual(rows[0]["source_event"], "review:operator")
        # Visibility proof through the plugin's own recall filter.
        self.assertEqual(self._visible(REVIEW_LINE, {"id:111"}, {"id:111", "public"}),
                         [REVIEW_LINE])

    def test_unknown_and_foreign_stay_quarantined_after_one_approval(self):
        self._run("approve", self._digest("riverbend"))
        # The foreign person's line is not visible to the approved subject...
        self.assertEqual(self._visible(FOREIGN_LINE, {"id:111"}, {"id:111", "public"}), [])
        # ...nor to their own recall until separately reviewed...
        self.assertEqual(self._visible(FOREIGN_LINE, {"id:222"}, {"id:222", "public"}), [])
        # ...and a second unreviewed line about the SAME subject stays hidden.
        self.assertEqual(self._visible(PLAIN_LINE, {"id:111"}, {"id:111", "public"}), [])

    def test_approve_public_visible_to_mention_recall(self):
        code = self._run("approve", self._digest("kestrel"), "--audience", "public")
        self.assertEqual(code, 0)
        self.assertEqual(self._visible(FOREIGN_LINE, {"id:222"}, {"public"}),
                         [FOREIGN_LINE])

    def test_review_receipt_records_reviewer(self):
        code = self._run("approve", self._digest("riverbend"), "--reviewed-by", "ghosty")
        self.assertEqual(code, 0)
        self.assertEqual(self._ledger_rows()[0]["source_event"], "review:ghosty")

    def test_stale_digest_refused_with_no_row(self):
        with mock.patch("builtins.print") as printed:
            code = self._run("approve", "deadbeefdeadbeef")
        self.assertEqual(code, 1)
        self.assertIn("stale", printed.call_args_list[0].args[0])
        self.assertEqual(self._ledger_rows(), [])

    def test_changed_store_line_is_stale(self):
        digest = self._digest("greenhouse")
        (self.memory_dir / "LOG.txt").write_text(
            LOG.replace(PLAIN_LINE, "the greenhouse door was fixed in september"),
            encoding="utf-8")
        with mock.patch("builtins.print") as printed:
            code = self._run("approve", digest)
        self.assertEqual(code, 1)
        self.assertIn("stale", printed.call_args_list[0].args[0])
        self.assertEqual(self._ledger_rows(), [])

    def test_duplicate_approval_refused(self):
        self.assertEqual(self._run("approve", self._digest("riverbend")), 0)
        with mock.patch("builtins.print") as printed:
            code = self._run("approve", self._digest("riverbend"))
        self.assertEqual(code, 1)
        self.assertIn("already", printed.call_args_list[0].args[0])
        self.assertEqual(len(self._ledger_rows()), 1)

    def test_bad_subject_refused(self):
        with mock.patch("builtins.print"):
            code = self._run("approve", self._digest("greenhouse"),
                             "--subject", "evil; rm -rf")
        self.assertEqual(code, 1)
        self.assertEqual(self._ledger_rows(), [])
    def test_ledger_select_failure_refuses_approval_before_insert(self):
        open_rw = self.review._ledger_conn
        def deny_read(path):
            conn, error = open_rw(path)
            conn.set_authorizer(
                lambda action, *_: sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_SELECT else sqlite3.SQLITE_OK
            )
            return conn, error
        with mock.patch.object(self.review, "_ledger_conn", side_effect=deny_read), \
             mock.patch("builtins.print") as printed:
            code = self._run("approve", self._digest("riverbend"))
        self.assertEqual(code, 1)
        self.assertTrue(any("ledger" in str(call.args[0]).lower()
                            for call in printed.call_args_list))
        self.assertEqual(self._ledger_rows(), [])


    def test_handle_only_fact_requires_numeric_audience_for_private_approval(self):
        legacy = "@riverbend wears a blue scarf"
        (self.memory_dir / "LOG.txt").write_text(LOG + legacy + "\n", encoding="utf-8")
        self.entries = self._store_entries()
        digest = self._digest("blue scarf")
        with mock.patch("builtins.print") as printed:
            code = self._run("approve", digest)
        self.assertEqual(code, 1)
        self.assertEqual(self._ledger_rows(), [])
        self.assertIn("id:", printed.call_args_list[0].args[0])
        self.assertEqual(self._run("approve", digest, "--subject", "id:111"), 0)
        self.assertEqual(self._ledger_rows()[0]["audience"], "id:111")
        self.assertEqual(
            self._visible(legacy, {"id:111", "riverbend"}, {"id:111", "public"}),
            [legacy],
        )

    def test_handle_only_fact_lists_its_numeric_approval(self):
        legacy = "@riverbend wears a blue scarf"
        (self.memory_dir / "LOG.txt").write_text(LOG + legacy + "\n", encoding="utf-8")
        self.entries = self._store_entries()
        digest = self._digest("blue scarf")
        self.assertEqual(self._run("approve", digest, "--subject", "id:111"), 0)
        with mock.patch("builtins.print") as printed:
            self.assertEqual(self._run("list"), 0)
        self.assertFalse(any(digest in str(call.args[0]) for call in printed.call_args_list))
        with mock.patch("builtins.print") as printed:
            self.assertEqual(self._run("list", "--include-reviewed"), 0)
        self.assertTrue(any(
            digest in str(call.args[0]) and "approved:id:111" in str(call.args[0])
            for call in printed.call_args_list
        ))


class TestRevocation(ReviewToolTestCase):
    def test_revoke_withdraws_and_blocks_reapproval(self):
        self.assertEqual(self._run("approve", self._digest("riverbend")), 0)
        self.assertEqual(self._visible(REVIEW_LINE, {"id:111"}, {"id:111", "public"}),
                         [REVIEW_LINE])
        with mock.patch("builtins.print"):
            code = self._run("revoke", self._digest("riverbend"))
        self.assertEqual(code, 0)
        self.assertEqual(self._visible(REVIEW_LINE, {"id:111"}, {"id:111", "public"}), [])
        with mock.patch("builtins.print") as printed:
            code = self._run("approve", self._digest("riverbend"))
        self.assertEqual(code, 1)
        self.assertIn("revoked", printed.call_args_list[0].args[0])
    def test_handle_only_default_revoke_withdraws_numeric_subject(self):
        legacy = "@riverbend wears a blue scarf"
        (self.memory_dir / "LOG.txt").write_text(LOG + legacy + "\n", encoding="utf-8")
        self.entries = self._store_entries()
        digest = self._digest("blue scarf")
        self.assertEqual(self._run("approve", digest, "--subject", "id:111"), 0)
        self.assertEqual(self._visible(legacy, {"id:111"}, {"id:111", "public"}), [legacy])
        self.assertEqual(self._run("revoke", digest), 0)
        self.assertEqual(self._visible(legacy, {"id:111"}, {"id:111", "public"}), [])
        self.assertTrue(any(
            row["subject"] == "id:111" and row["audience"] == "revoked"
            for row in self._ledger_rows()
        ))
    def test_global_revoke_cannot_be_reapproved_under_another_id(self):
        legacy = "@riverbend wears a blue scarf"
        (self.memory_dir / "LOG.txt").write_text(LOG + legacy + "\n", encoding="utf-8")
        self.entries = self._store_entries()
        digest = self._digest("blue scarf")
        self.assertEqual(self._run("approve", digest, "--subject", "id:111"), 0)
        self.assertEqual(self._run("revoke", digest), 0)
        with mock.patch("builtins.print") as printed:
            code = self._run("approve", digest, "--subject", "id:222")
        self.assertEqual(code, 1)
        self.assertIn("revoked", printed.call_args_list[0].args[0])
        self.assertFalse(any(
            row["subject"] == "id:222" and row["audience"] == "id:222"
            for row in self._ledger_rows()
        ))
    def test_revoke_cannot_finish_between_approval_read_and_write(self):
        digest = self._digest("riverbend")
        checked = threading.Event()
        resume = threading.Event()
        revoke_entered = threading.Event()
        revoke_done = threading.Event()
        approvals, revokes = [], []
        original_states = self.review._ledger_states
        original_conn = self.review._ledger_conn

        def pause_after_read(conn, fact, subject):
            result = original_states(conn, fact, subject)
            if subject == "id:111":
                checked.set()
                if not resume.wait(5):
                    raise RuntimeError("approval fixture was not released")
            return result

        def mark_revoke_connection(path):
            if threading.current_thread().name == "revoker":
                revoke_entered.set()
            return original_conn(path)

        def approve():
            approvals.append(self._run("approve", digest))

        def revoke():
            try:
                revokes.append(self._run("revoke", digest))
            finally:
                revoke_done.set()

        with mock.patch.object(self.review, "_ledger_states", side_effect=pause_after_read), \
             mock.patch.object(self.review, "_ledger_conn", side_effect=mark_revoke_connection):
            approver = threading.Thread(target=approve, name="approver")
            revoker = threading.Thread(target=revoke, name="revoker")
            approver.start()
            try:
                self.assertTrue(checked.wait(2))
                revoker.start()
                self.assertTrue(revoke_entered.wait(2))
                self.assertFalse(revoke_done.wait(0.3),
                                 "revoke committed between approval's read and write")
            finally:
                resume.set()
                approver.join(timeout=5)
                if revoker.ident is not None:
                    revoker.join(timeout=5)
        self.assertEqual((approvals, revokes), ([0], [0]))
        self.assertEqual(self._visible(REVIEW_LINE, {"id:111"}, {"id:111", "public"}), [])

    def test_revoke_lost_commit_ack_does_not_claim_nothing_written(self):
        digest = self._digest("riverbend")
        self.assertEqual(self._run("approve", digest), 0)
        original_conn = self.review._ledger_conn

        def lost_ack(path):
            conn, err = original_conn(path)

            class Connection:
                def execute(self, sql, params=()):
                    cursor = conn.execute(sql, params)
                    if sql == "COMMIT":
                        raise sqlite3.OperationalError("commit acknowledgement lost")
                    return cursor

                def close(self):
                    conn.close()

            return Connection(), err

        with mock.patch.object(self.review, "_ledger_conn", side_effect=lost_ack), \
             mock.patch("builtins.print") as printed:
            code = self._run("revoke", digest)
        result = printed.call_args_list[-1].args[0].lower()
        self.assertEqual(code, 1)
        self.assertIn("not confirmed", result)
        self.assertNotIn("nothing was written", result)
        self.assertEqual(self._visible(REVIEW_LINE, {"id:111"}, {"id:111"}), [])





class TestDigestSelection(ReviewToolTestCase):
    def test_empty_revoke_token_never_withdraws_the_first_fact(self):
        before = (self.memory_dir / "LOG.txt").read_bytes()
        with mock.patch("builtins.print") as printed:
            code = self._run("revoke", "")
        self.assertEqual(code, 1)
        self.assertIn("digest", printed.call_args_list[0].args[0])
        self.assertEqual(self._ledger_rows(), [])
        self.assertEqual((self.memory_dir / "LOG.txt").read_bytes(), before)

    def test_short_unique_approval_token_is_not_authority(self):
        with mock.patch("builtins.print") as printed:
            code = self._run("approve", self._digest("riverbend")[:4])
        self.assertEqual(code, 1)
        self.assertIn("digest", printed.call_args_list[0].args[0])
        self.assertEqual(self._ledger_rows(), [])

    def test_ambiguous_full_token_refuses_show_and_irreversible_revoke(self):
        token = "d" * 16
        with mock.patch.object(self.review, "_fact_digest_of",
                               return_value=token + "0" * 48), \
             mock.patch("builtins.print") as printed:
            show = self._run("show", token)
            revoke = self._run("revoke", token)
        self.assertEqual((show, revoke), (1, 1))
        self.assertTrue(any("ambiguous" in str(call.args[0]).lower()
                            for call in printed.call_args_list))
        self.assertEqual(self._ledger_rows(), [])


class TestAuthorityModel(ReviewToolTestCase):
    def test_memory_dir_is_required(self):
        with mock.patch.object(sys, "argv", ["optmem_review", "list"]):
            try:
                self.review.main(["list"])
            except SystemExit as exc:
                self.assertEqual(exc.code, 2)
            else:
                self.fail("argparse must require --memory-dir")

    def test_store_file_never_modified(self):
        before = (self.memory_dir / "LOG.txt").read_bytes()
        self._run("approve", self._digest("riverbend"))
        self._run("revoke", self._digest("riverbend"))
        self._run("list", "--include-reviewed")
        self.assertEqual((self.memory_dir / "LOG.txt").read_bytes(), before)

    def test_ledger_contains_no_fact_text(self):
        self._run("approve", self._digest("riverbend"))
        blob = (self.memory_dir / self.review.SCOPE_DB_NAME).read_bytes()
        wal = (self.memory_dir / (self.review.SCOPE_DB_NAME + "-wal"))
        if wal.exists():
            blob += wal.read_bytes()
        for needle in (b"oolong", b"flood channel", b"riverbend"):
            self.assertNotIn(needle, blob, "ledger must store digests only")


if __name__ == "__main__":
    unittest.main(verbosity=2)
