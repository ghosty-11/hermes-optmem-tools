"""Speaker-scope authorization for explicit companion OptMem tools.

On a speaker-scoped companion profile the explicit tools bind to the VERIFIED turn
identity (ambient attestation + session context), never to anything the model
wrote: a note about anyone but the verified speaker is refused, recall is limited
to the speaker's own keys, wake/nap are internal-only, and note writes are budgeted
(2/turn, 8/speaker/day, 64/profile/day) atomically and idempotently with typed
refusals that never claim a save. Off-surface callers (CLI/operator) keep the
stock tools.

"""

import contextvars
import importlib.util
import os
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = Path(os.environ.get("OPTMEM_PLUGIN_PATH") or ROOT / "__init__.py")



def _load():
    spec = importlib.util.spec_from_file_location("optmem_note_auth_under_test", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_SESSION_VAR_NAMES = (
    "HERMES_SESSION_PLATFORM", "HERMES_SESSION_MESSAGE_ID",
    "HERMES_SESSION_USER_ID", "HERMES_SESSION_USER_NAME",
)


class _FakeGateway:
    """A dependency-free stand-in for gateway.session_context + the ambient attestation."""

    def __init__(self):
        self.vars = {name: ContextVar(name, default="") for name in _SESSION_VAR_NAMES}
        self.identity = ContextVar("fixture_ambient_turn_identity", default=None)
        gateway = types.ModuleType("gateway")
        session_context = types.ModuleType("gateway.session_context")
        session_context._ambient_turn_identity = self.identity
        session_context.get_session_env = (
            lambda name, default="": self.vars[name].get() or default)
        gateway.session_context = session_context
        self.modules = {"gateway": gateway, "gateway.session_context": session_context}
        self.tokens = []

    def bind(self, message_id="m-1", user_id="111", user_name="riverbend", attested=True):
        values = {
            "HERMES_SESSION_PLATFORM": "discord",
            "HERMES_SESSION_MESSAGE_ID": message_id,
            "HERMES_SESSION_USER_ID": user_id,
            "HERMES_SESSION_USER_NAME": user_name,
        }
        for name, value in values.items():
            self.tokens.append(self.vars[name].set(value))
        self.tokens.append(self.identity.set((message_id, user_id) if attested else None))

    def unbind(self):
        while self.tokens:
            token = self.tokens.pop()
            token.var.reset(token)


class SpeakerScopedSurface(unittest.TestCase):
    """Shared setUp: speaker-scoped companion profile with a synthetic memory dir."""

    def setUp(self):
        self.mod = _load()
        self.gateway = _FakeGateway()
        for name, module in self.gateway.modules.items():
            self.enterContext(mock.patch.dict(sys.modules, {name: module}))
        self.tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="optmem-auth-"))
        self.memory_dir = str(Path(self.tmp))
        self.enterContext(mock.patch.object(
            self.mod, "_cfg",
            return_value={"speaker_scoped": True, "memory_dir": self.memory_dir}))
        self.enterContext(mock.patch.object(
            self.mod, "_profile_restricted", lambda: True))
        self.enterContext(mock.patch.object(
            self.mod, "_now", lambda: datetime(2026, 9, 23, 12, tzinfo=timezone.utc)))

    def _bind_verified(self, user_id="111", user_name="riverbend", message_id="m-1",
                       attested=True):
        self.gateway.bind(message_id=message_id, user_id=user_id,
                          user_name=user_name, attested=attested)
        self.addCleanup(self.gateway.unbind)

    def _note(self, text, task_id="t1", session_id="s1"):
        return self.mod._handle_note({"text": text}, task_id=task_id, session_id=session_id)

    def _old_memo_binary(self):
        old = Path(self.memory_dir) / "old-memo"
        old.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "from pathlib import Path\n"
            "if sys.argv[1] != 'note':\n"
            "    print('No such command', file=sys.stderr)\n"
            "    raise SystemExit(1)\n"
            "with (Path(os.environ['MEMORY_DIR']) / 'LOG.txt').open('a') as log:\n"
            "    log.write(sys.argv[2] + '\\n')\n"
            "print('Saved as #0.')\n",
            encoding="utf-8",
        )
        old.chmod(0o700)
        return str(old)

    def _ledger_rows(self):
        path = Path(self.mod._scope_db_path(self.memory_dir))
        if not path.exists():
            return []
        conn = sqlite3.connect(path)
        try:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(
                "SELECT * FROM note_scope ORDER BY created_utc")]
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()


class TestNoteSubjectBinding(SpeakerScopedSurface):
    def test_note_to_self_succeeds_and_records_scoped_provenance(self):
        self._bind_verified()
        with mock.patch.object(self.mod, "_run", return_value="Saved as #3."):
            result = self._note("@riverbend id:111: prefers tea")
        self.assertEqual(result, "Saved as #3.")
        rows = self._ledger_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subject"], "id:111")
        self.assertEqual(rows[0]["author"], "id:111")
        self.assertEqual(rows[0]["audience"], "id:111")
        self.assertEqual(rows[0]["source_event"], "m-1")

    def test_foreign_id_note_refused_before_backend_write(self):
        self._bind_verified()
        with mock.patch.object(self.mod, "_run") as run:
            result = self._note("@mallory id:222: hates tea")
        self.assertIn("refused", result.lower())
        self.assertIn("nothing was written", result.lower())
        run.assert_not_called()
        self.assertEqual(self._ledger_rows(), [])

    def test_foreign_handle_note_refused(self):
        self._bind_verified()
        with mock.patch.object(self.mod, "_run") as run:
            result = self._note("@mallory: hates tea")
        self.assertIn("refused", result.lower())
        run.assert_not_called()

    def test_own_handle_note_allowed_with_verified_name(self):
        self._bind_verified()
        with mock.patch.object(self.mod, "_run", return_value="Saved as #0.") as run:
            result = self._note("@riverbend: prefers rooibos")
        self.assertEqual(result, "Saved as #0.")
        rows = self._ledger_rows()
        self.assertEqual(rows[0]["subject"], "riverbend")
        self.assertEqual(rows[0]["audience"], "id:111")

    def test_subjectless_note_refused_before_budget_or_backend(self):
        self._bind_verified()
        with mock.patch.object(self.mod, "_run", return_value="Saved as #1.") as run:
            result = self._note("the greenhouse door sticks in august")
        self.assertIn("refused", result.lower())
        self.assertIn("id:", result)
        run.assert_not_called()
        self.assertEqual(self._ledger_rows(), [])

    def test_unattested_turn_refuses_note_fail_closed(self):
        self._bind_verified(attested=False)
        with mock.patch.object(self.mod, "_run") as run:
            result = self._note("@riverbend id:111: prefers tea")
        self.assertIn("verified", result)
        run.assert_not_called()
        self.assertEqual(self._ledger_rows(), [])

    def test_off_surface_turn_keeps_stock_foreign_note(self):
        """CLI/operator path: no companion surface, foreign ids allowed (operator-authored)."""
        fact = "@mallory id:222: hates tea"
        with mock.patch.object(self.mod, "_binary",
                               return_value=self._old_memo_binary()):
            result = self._note(fact)
        self.assertEqual(result, "Saved as #0.")
        self.assertEqual(
            (Path(self.memory_dir) / "LOG.txt").read_text(encoding="utf-8").splitlines(),
            [fact],
        )
        rows = self._ledger_rows()
        self.assertEqual(rows[0]["author"], "operator")
        self.assertEqual(rows[0]["audience"], "operator")

    def test_scoped_operator_missing_ledger_does_not_claim_remembered(self):
        fact = "@mallory id:222: hates tea"
        with mock.patch.object(self.mod, "_binary",
                               return_value=self._old_memo_binary()), \
             mock.patch.object(self.mod, "_ledger_write_conn", return_value=None):
            result = self._note(fact)
        self.assertIn("unconfirmed", result.lower())
        self.assertEqual(
            (Path(self.memory_dir) / "LOG.txt").read_text(encoding="utf-8").splitlines(),
            [fact],
        )
        self.assertEqual(self._ledger_rows(), [])

    def test_scoped_operator_ledger_insert_error_does_not_claim_saved(self):
        fact = "@mallory id:222: hates tea"

        class BrokenLedger:
            def execute(self, *_args):
                raise sqlite3.OperationalError("scope commit unavailable")

            def close(self):
                pass

        with mock.patch.object(self.mod, "_binary",
                               return_value=self._old_memo_binary()), \
             mock.patch.object(self.mod, "_ledger_write_conn",
                               return_value=BrokenLedger()):
            try:
                result = self._note(fact)
            except sqlite3.Error as exc:
                self.fail(f"raw ledger error after store write: {exc}")
        self.assertIn("unconfirmed", result.lower())
        self.assertEqual(
            (Path(self.memory_dir) / "LOG.txt").read_text(encoding="utf-8").splitlines(),
            [fact],
        )
        self.assertEqual(self._ledger_rows(), [])

    def test_private_operator_note_keeps_stock_receipt_without_ledger(self):
        fact = "@mallory id:222: hates tea"
        with mock.patch.object(self.mod, "_profile_restricted", return_value=False), \
             mock.patch.object(self.mod, "_binary",
                               return_value=self._old_memo_binary()), \
             mock.patch.object(self.mod, "_ledger_write_conn", return_value=None):
            result = self._note(fact)
        self.assertEqual(result, "Saved as #0.")
        self.assertEqual(
            (Path(self.memory_dir) / "LOG.txt").read_text(encoding="utf-8").splitlines(),
            [fact],
        )
        self.assertEqual(self._ledger_rows(), [])



class TestNoteBudget(SpeakerScopedSurface):
    def _spend(self, facts, task_ids):
        with mock.patch.object(self.mod, "_run", return_value="Saved."):
            return [self._note(fact, task_id=tid) for fact, tid in zip(facts, task_ids)]

    def test_third_note_same_turn_refused_with_retry_guidance(self):
        self._bind_verified()
        results = self._spend(
            ["id:111: fact one", "id:111: fact two", "id:111: fact three"],
            ["t1", "t1", "t1"])
        self.assertEqual(results[0], "Saved.")
        self.assertEqual(results[1], "Saved.")
        self.assertIn("refused", results[2])
        self.assertIn("turn", results[2])
        self.assertIn("Nothing was written", results[2])
        self.assertEqual(len(self._ledger_rows()), 2)

    def test_ninth_note_for_speaker_that_day_refused_with_retry_time(self):
        self._bind_verified()
        facts = [f"id:111: fact {i}" for i in range(9)]
        results = self._spend(facts, [f"t{i}" for i in range(9)])
        for result in results[:8]:
            self.assertEqual(result, "Saved.")
        self.assertIn("refused", results[8])
        self.assertIn("2026-09-24", results[8])
        self.assertIn("Nothing was written", results[8])
        self.assertEqual(len(self._ledger_rows()), 8)

    def test_sixty_fifth_note_for_profile_that_day_refused(self):
        """64/profile/day needs eight speakers spending their own eight."""
        with mock.patch.object(self.mod, "_run", return_value="Saved."):
            for speaker in range(8):
                self._bind_verified(user_id=f"5{speaker:02d}", message_id=f"m-{speaker}")
                for i in range(8):
                    note = self._note(
                        f"id:5{speaker:02d}: fact {i}", task_id=f"t{speaker}-{i}")
                    self.assertEqual(note, "Saved.")
            self.assertEqual(len(self._ledger_rows()), 64)
            # A brand-new speaker still cannot spend the profile's 65th slot.
            self._bind_verified(user_id="599", message_id="m-new")
            refused = self._note("id:599: one more", task_id="t-new")
        self.assertIn("refused", refused)
        self.assertIn("profile", refused)
        self.assertEqual(len(self._ledger_rows()), 64)

    def test_identical_retry_is_idempotent_without_second_write(self):
        self._bind_verified()
        with mock.patch.object(self.mod, "_run", return_value="Saved as #0.") as run:
            first = self._note("id:111: prefers tea")
            second = self._note("id:111: prefers tea", task_id="t2")
        self.assertEqual(first, "Saved as #0.")
        self.assertIn("already", second.lower())
        self.assertEqual(run.call_count, 1)
        self.assertEqual(len(self._ledger_rows()), 1)

    def test_failed_save_not_claimed_and_not_spent(self):
        self._bind_verified()
        with mock.patch.object(
                self.mod, "_run", return_value="optmem: 'note' failed: disk full"):
            failed = self._note("id:111: prefers tea")
        self.assertIn("failed", failed)
        self.assertNotIn("Saved", failed)
        self.assertEqual(self._ledger_rows(), [])
        with mock.patch.object(self.mod, "_run", return_value="Saved as #1.") as run:
            retried = self._note("id:111: prefers tea")
        self.assertEqual(retried, "Saved as #1.")
        run.assert_called_once()
        self.assertEqual(len(self._ledger_rows()), 1)

    def test_empty_backend_receipt_is_explicitly_unconfirmed(self):
        self._bind_verified()
        with mock.patch.object(self.mod, "_run", return_value="") as backend:
            result = self._note("id:111: prefers tea")
        backend.assert_called_once()
        self.assertIn("unconfirmed", result.lower())
        self.assertEqual(self._ledger_rows(), [])

    def test_nonzero_note_exit_cannot_confirm_a_stdout_save_receipt(self):
        self._bind_verified()
        process = types.SimpleNamespace(
            stdout="Saved as #0.", stderr="", returncode=1)
        with mock.patch.object(self.mod.subprocess, "run", return_value=process):
            result = self._note("id:111: prefers tea")
        self.assertIn("unconfirmed", result.lower())
        self.assertEqual(self._ledger_rows(), [])

    def test_zero_exit_without_note_receipt_does_not_approve_fact(self):
        self._bind_verified()
        process = types.SimpleNamespace(stdout="", stderr="", returncode=0)
        with mock.patch.object(self.mod.subprocess, "run", return_value=process):
            result = self._note("id:111: prefers tea")
        self.assertIn("unconfirmed", result.lower())
        self.assertEqual(self._ledger_rows(), [])

    def test_stderr_only_zero_exit_is_not_a_note_receipt(self):
        self._bind_verified()
        process = types.SimpleNamespace(
            stdout="", stderr="memo warning", returncode=0)
        with mock.patch.object(self.mod.subprocess, "run", return_value=process):
            result = self._note("id:111: prefers tea")
        self.assertIn("unconfirmed", result.lower())
        self.assertEqual(self._ledger_rows(), [])

    def test_concurrent_writes_cannot_double_spend_the_last_slot(self):
        self._bind_verified()
        facts = [f"id:111: filler {i}" for i in range(7)]
        self._spend(facts, [f"f{i}" for i in range(7)])
        outcomes = []
        barrier = threading.Barrier(2)

        def contender(fact, task_id, context):
            barrier.wait()
            with mock.patch.object(self.mod, "_run", return_value="Saved."):
                outcomes.append(context.run(self._note, fact, task_id=task_id))

        # threading does NOT inherit contextvars: hand each contender a copy of
        # the bound turn context explicitly.
        contexts = [contextvars.copy_context(), contextvars.copy_context()]
        threads = [
            threading.Thread(
                target=contender, args=("id:111: winner fact", "w1", contexts[0])),
            threading.Thread(
                target=contender, args=("id:111: loser fact", "w2", contexts[1])),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        saved = [r for r in outcomes if r == "Saved."]
        refused = [r for r in outcomes if "refused" in r]
        self.assertEqual(len(saved), 1, outcomes)
        self.assertEqual(len(refused), 1, outcomes)
        self.assertEqual(len(self._ledger_rows()), 8)


class TestExplicitRecallScope(SpeakerScopedSurface):
    def _seed_rows(self):
        with mock.patch.object(self.mod, "_run", return_value="Saved as #0."):
            self._note("@riverbend id:111: prefers tea")
        # A third-party claim about id:111 asserted by id:222 (review-created):
        # digest-only insert — no fact text in the ledger.
        conn = sqlite3.connect(self.mod._scope_db_path(self.memory_dir))
        try:
            conn.execute(self.mod.NOTE_SCOPE_DDL)
            conn.execute(
                "INSERT OR IGNORE INTO note_scope (row_key, fact_digest, subject,"
                " author, audience, source_event, turn_key, day, created_utc)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex,
                 self.mod._visibility_digest(
                     "id:111", "@someone id:111: asserted by another"),
                 "id:111", "id:222", "id:222", "m-x", "t-x", "2026-09-23",
                 "2026-09-23T12:00:00+00:00"))
            conn.commit()
        finally:
            conn.close()

    def test_own_recall_returns_only_audience_visible_lines(self):
        self._bind_verified()
        self._seed_rows()
        lines = (
            "@riverbend id:111: prefers tea\n"
            "@someone id:111: asserted by another\n"
            "legacy line about id:111 with no provenance\n"
            "2 matches."
        )
        with mock.patch.object(self.mod, "_run", return_value=lines) as run:
            result = self.mod._handle_recall({"pattern": "id:111"})
        run.assert_called_once()
        self.assertIn("@riverbend id:111: prefers tea", result)
        self.assertNotIn("asserted by another", result)
        self.assertNotIn("legacy line", result)
        self.assertNotIn("matches.", result)

    def test_recall_by_verified_handle_allowed(self):
        self._bind_verified()
        self._seed_rows()
        with mock.patch.object(
                self.mod, "_run",
                return_value="@riverbend id:111: prefers tea") as run:
            result = self.mod._handle_recall({"pattern": "riverbend"})
        run.assert_called_once()
        self.assertIn("prefers tea", result)

    def test_foreign_id_recall_refused_before_backend(self):
        self._bind_verified()
        with mock.patch.object(self.mod, "_run") as run:
            result = self.mod._handle_recall({"pattern": "id:222"})
        self.assertIn("refused", result.lower())
        run.assert_not_called()

    def test_broad_recall_refused_before_backend(self):
        self._bind_verified()
        with mock.patch.object(self.mod, "_run") as run:
            result = self.mod._handle_recall({"pattern": "otters"})
        self.assertIn("refused", result.lower())
        run.assert_not_called()

    def test_unattested_recall_refused_fail_closed(self):
        self._bind_verified(attested=False)
        with mock.patch.object(self.mod, "_run") as run:
            result = self.mod._handle_recall({"pattern": "id:111"})
        self.assertIn("verified", result)
        run.assert_not_called()

    def test_absence_message_when_nothing_is_visible(self):
        self._bind_verified()
        with mock.patch.object(self.mod, "_run", return_value="legacy only line"):
            result = self.mod._handle_recall({"pattern": "id:111"})
        self.assertIn("No memories", result)


class TestHousekeepingIsInternal(SpeakerScopedSurface):
    def test_wake_refused_on_verified_companion_turn(self):
        self._bind_verified()
        with mock.patch.object(self.mod, "_run") as run:
            result = self.mod._handle_wake({})
        self.assertIn("internal", result)
        run.assert_not_called()

    def test_nap_refused_on_verified_companion_turn(self):
        self._bind_verified()
        with mock.patch.object(self.mod, "_run") as run:
            result = self.mod._handle_nap({})
        self.assertIn("internal", result)
        run.assert_not_called()

    def test_unattested_companion_turn_still_refuses_wake(self):
        self._bind_verified(attested=False)
        with mock.patch.object(self.mod, "_run") as run:
            self.mod._handle_wake({})
        run.assert_not_called()

    def test_private_profile_keeps_stock_wake(self):
        """Private-profile surface: speaker_scoped unset, attestation present."""
        self._bind_verified()
        self.enterContext(mock.patch.object(
            self.mod, "_cfg", return_value={"memory_dir": self.memory_dir}))
        self.enterContext(mock.patch.object(
            self.mod, "_profile_restricted", lambda: False))
        with mock.patch.object(self.mod, "_run", return_value="You are awake.") as run:
            result = self.mod._handle_wake({})
        self.assertEqual(result, "You are awake.")
        run.assert_called_once_with(["wake"])

    def test_missing_attestation_attribute_still_restricts(self):
        """Ambient attestation absent entirely: posture + platform restrict."""
        gateway = types.ModuleType("gateway")
        session_context = types.ModuleType("gateway.session_context")
        for name in _SESSION_VAR_NAMES:
            var = ContextVar(name, default="")
            session_context.__dict__[name] = var
        session_context.get_session_env = (
            lambda name, default="": session_context.__dict__[name].get() or default)
        gateway.session_context = session_context
        with mock.patch.dict(sys.modules, {
                "gateway": gateway, "gateway.session_context": session_context}):
            session_context.__dict__["HERMES_SESSION_PLATFORM"].set("discord")
            with mock.patch.object(self.mod, "_run") as run:
                wake = self.mod._handle_wake({})
                nap = self.mod._handle_nap({})
                note = self._note("@riverbend id:111: prefers tea")
                recall = self.mod._handle_recall({"pattern": "id:111"})
        self.assertIn("internal", wake)
        self.assertIn("internal", nap)
        self.assertIn("verified", note)
        self.assertIn("verified", recall)
        run.assert_not_called()
        self.assertEqual(self._ledger_rows(), [])

    def test_unreadable_session_context_refuses_conservatively(self):
        with mock.patch.dict(sys.modules, {"gateway": None}):
            with mock.patch.object(self.mod, "_run") as run:
                note = self._note("@riverbend id:111: prefers tea")
        self.assertIn("verified", note)
        run.assert_not_called()
        self.assertEqual(self._ledger_rows(), [])

    def test_off_surface_turn_keeps_stock_nap(self):
        with mock.patch.object(self.mod, "_run", return_value="Nothing left to compress.") as run:
            result = self.mod._handle_nap({})
        self.assertEqual(result, "Nothing left to compress.")
        run.assert_called_once_with(["nap"])


class TestDigestOnlyLedger(SpeakerScopedSurface):
    def test_ledger_bytes_contain_no_fact_text_and_recall_still_works(self):
        self._bind_verified()
        sentinel = "LEDGER_SENTINEL_collects_blue_bottles_4d7a"
        with mock.patch.object(self.mod, "_run", return_value="Saved."):
            self._note(f"@riverbend id:111: {sentinel}")
        with mock.patch.object(
                self.mod, "_run", return_value=f"@riverbend id:111: {sentinel}"):
            result = self.mod._handle_recall({"pattern": "id:111"})
        self.assertIn(sentinel, result)
        path = Path(self.mod._scope_db_path(self.memory_dir))
        blob = path.read_bytes() if path.exists() else b""
        wal = path.with_name(path.name + "-wal")
        if wal.exists():
            blob += wal.read_bytes()
        self.assertNotIn(sentinel.encode(), blob,
                         "the ledger must never duplicate fact text")

    def test_revoked_fact_is_suppressed_even_when_previously_visible(self):
        self._bind_verified()
        fact = "@riverbend id:111: prefers tea"
        with mock.patch.object(self.mod, "_run", return_value="Saved."):
            self._note(fact)
        conn = sqlite3.connect(self.mod._scope_db_path(self.memory_dir))
        try:
            conn.execute(self.mod.NOTE_SCOPE_DDL)
            conn.execute(
                "INSERT OR IGNORE INTO note_scope (row_key, fact_digest, subject,"
                " author, audience, source_event, turn_key, day, created_utc)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, self.mod._visibility_digest("id:111", fact),
                 "id:111", "operator", "revoked", "review:operator", "review",
                 "", ""))
            conn.commit()
        finally:
            conn.close()
        with mock.patch.object(self.mod, "_run", return_value=fact):
            result = self.mod._handle_recall({"pattern": "id:111"})
        self.assertIn("No memories", result)
    def test_global_revoke_blocks_resave_and_scoped_recall(self):
        self._bind_verified()
        fact = "@riverbend id:111: prefers tea"
        with mock.patch.object(self.mod, "_run", return_value="Saved."):
            self.assertEqual(self._note(fact), "Saved.")
        conn = sqlite3.connect(self.mod._scope_db_path(self.memory_dir))
        try:
            conn.execute(self.mod.NOTE_SCOPE_DDL)
            conn.execute(
                "INSERT INTO note_scope (row_key, fact_digest, subject, author,"
                " audience, source_event, turn_key, day, created_utc)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, self.mod._visibility_digest("", fact),
                 "", "operator", "revoked", "review:operator", "review", "", ""),
            )
            conn.commit()
        finally:
            conn.close()
        with mock.patch.object(self.mod, "_run", return_value="Saved.") as backend:
            result = self._note(fact, task_id="later-turn")
        self.assertIn("refused", result.lower())
        backend.assert_not_called()
        with mock.patch.object(self.mod, "_run", return_value=fact):
            recalled = self.mod._handle_recall({"pattern": "id:111"})
        self.assertIn("No memories", recalled)
    def test_revocation_cannot_finish_before_inflight_note_backend(self):
        self._bind_verified()
        fact = "@riverbend id:111: prefers tea"
        log = Path(self.memory_dir) / "LOG.txt"
        log.write_text(fact + "\n", encoding="utf-8")
        review_spec = importlib.util.spec_from_file_location(
            "optmem_review_inflight", ROOT / "scripts" / "optmem_review.py"
        )
        review = importlib.util.module_from_spec(review_spec)
        review_spec.loader.exec_module(review)
        digest = review._fact_digest_of(fact)[:16]
        backend_entered = threading.Event()
        backend_release = threading.Event()
        revoke_entered = threading.Event()
        revoke_done = threading.Event()
        note_results, revoke_results = [], []
        original_conn = review._ledger_conn

        def backend(args):
            if args[0] == "recall":
                return fact
            backend_entered.set()
            if not backend_release.wait(5):
                raise RuntimeError("note backend fixture was not released")
            with log.open("a", encoding="utf-8") as out:
                out.write(fact + "\n")
            return "Saved."

        def mark_revoke_connection(path):
            revoke_entered.set()
            return original_conn(path)

        writer_context = contextvars.copy_context()
        def note_writer():
            note_results.append(writer_context.run(self._note, fact, "race-turn"))

        def revoker():
            try:
                revoke_results.append(review.main(
                    ["--memory-dir", self.memory_dir, "revoke", digest]
                ))
            finally:
                revoke_done.set()

        with mock.patch.object(self.mod, "_run", side_effect=backend), \
             mock.patch.object(review, "_ledger_conn", side_effect=mark_revoke_connection):
            note_thread = threading.Thread(target=note_writer, name="note-writer")
            revoke_thread = threading.Thread(target=revoker, name="revoker")
            note_thread.start()
            try:
                self.assertTrue(backend_entered.wait(2))
                revoke_thread.start()
                self.assertTrue(revoke_entered.wait(2))
                self.assertFalse(revoke_done.wait(0.3),
                                 "revoke committed before the in-flight store write")
            finally:
                backend_release.set()
                note_thread.join(timeout=5)
                if revoke_thread.ident is not None:
                    revoke_thread.join(timeout=5)
            self.assertEqual((note_results, revoke_results), (["Saved."], [0]))
            self.assertIn("No memories",
                          self.mod._handle_recall({"pattern": "id:111"}))

    def test_retry_after_lost_ledger_commit_does_not_append_again(self):
        self._bind_verified()
        fact = "@riverbend id:111: prefers tea"
        log = Path(self.memory_dir) / "LOG.txt"
        opened = 0
        open_conn = self.mod._ledger_write_conn

        class LostCommit:
            def __init__(self, conn):
                self.conn = conn

            def execute(self, sql, params=()):
                if sql == "COMMIT":
                    raise sqlite3.OperationalError("simulated lost ledger commit")
                return self.conn.execute(sql, params)

            def close(self):
                self.conn.close()

        def ledger(path):
            nonlocal opened
            opened += 1
            conn = open_conn(path)
            return LostCommit(conn) if opened == 1 else conn

        def memo(args):
            if args[0] == "recall":
                return log.read_text(encoding="utf-8")
            if args[0] == "note-once" and log.exists():
                return "Already saved as #0."
            index = (len(log.read_text(encoding="utf-8").splitlines())
                     if log.exists() else 0)
            with log.open("a", encoding="utf-8") as out:
                out.write(fact + "\n")
            return f"Saved as #{index}."

        with mock.patch.object(self.mod, "_ledger_write_conn", side_effect=ledger), \
             mock.patch.object(self.mod, "_run", side_effect=memo):
            first = self._note(fact, task_id="first")
            self.assertIn("unconfirmed", first.lower())
            self.assertEqual(self._ledger_rows(), [])
            second = self._note(fact, task_id="retry")
            recalled = self.mod._handle_recall({"pattern": "id:111"})
        self.assertEqual(second, "Already saved as #0.")
        self.assertEqual(log.read_text(encoding="utf-8").splitlines(), [fact])
        self.assertEqual(recalled, fact)
        self.assertEqual(len(self._ledger_rows()), 1)

    def test_old_backend_refuses_without_writing_or_approving(self):
        self._bind_verified()
        old = self._old_memo_binary()
        with mock.patch.object(self.mod, "_binary", return_value=str(old)):
            result = self._note("@riverbend id:111: prefers tea")
        self.assertIn("unconfirmed", result.lower())
        self.assertFalse((Path(self.memory_dir) / "LOG.txt").exists())
        self.assertEqual(self._ledger_rows(), [])


class TestSchemaTruthfulness(SpeakerScopedSurface):
    def test_note_schema_states_current_speaker_limit(self):
        blob = (self.mod._NOTE["description"]
                + self.mod._NOTE["parameters"]["properties"]["text"]["description"])
        self.assertIn("current speaker", blob.lower())

    def test_recall_schema_states_own_scope(self):
        blob = self.mod._RECALL["description"]
        self.assertIn("current speaker", blob.lower())

    def test_wake_schema_marks_internal_housekeeping(self):
        self.assertIn("internal", self.mod._WAKE["description"].lower())
        self.assertIn("internal", self.mod._NAP["description"].lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
