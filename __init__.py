"""OptMem as tools — persistent agent memory without shell access.

WHY
---
OptMem (github.com/VictorTaelin/OptMem) is a CLI: `memo wake`, `memo note`,
`memo recall`. The obvious way to give an agent OptMem is to tell it to run
those commands — which requires the terminal toolset. For a profile that faces
a PUBLIC room that is not an option: a terminal is the one capability a
prompt-injected community bot must never have, and "run only this one binary"
is not something a shell tool can promise.

So the binary is wrapped in registered tools instead. The agent calls
`optmem_note(text=...)`; the plugin runs the binary itself with a FIXED argv
and the profile's own MEMORY_DIR. There is no shell, no user-controlled
command, no path the model can steer. A profile with no `optmem.memory_dir`
configured never sees these tools at all.

Discovered the hard way (2026-08-05): a profile was instructed in its AGENTS.md
to run `memo note` while `terminal` sat in its disabled_toolsets. It failed
silently for its entire life — LOG.txt was 0 lines — because an agent told to
use a tool it does not have simply never does the thing, and nothing logs it.

CONFIG (per profile, in config.yaml)
------------------------------------
    optmem:
      memory_dir: /var/lib/hermes/companion-memory/memory   # required; enables the tools
      binary: /var/lib/hermes/.optmem/memo               # optional, this is the default
      wake_lines: 48                                     # optional; caps rendered wake output

Then add `optmem` to the profile's toolsets (and to platform_toolsets.<surface>
if that profile scopes tools per surface).

SAFETY
------
  * argv is built by this module; the model supplies only a note string or
    literal search text, never a command, flag, path or regular expression.
  * MEMORY_DIR comes from config, never from the model — one profile cannot
    read another's memories by asking.
  * env is minimal (MEMORY_DIR + PATH), cwd is fixed, every call is timed out.
  * note text is collapsed to one line and refused if it exceeds the store's
    ENTRY_CHARS limit or 280 bytes, whichever is lower.
  * MEMORY_DIR must already exist (`memo init`). This plugin never creates it.
  * multi-part wake output is fetched to one stable snapshot before output
    limits are applied, so recent memories and the awake banner cannot be lost.
  * optmem_nap only shows a pending compression task; it does not write one.
  * recall text is length-capped and escaped as a literal regex before it
    reaches OptMem; user-controlled regex execution is impossible.
  * On a speaker-scoped companion profile (optmem.speaker_scoped), every tool
    binds to the VERIFIED turn identity (ambient attestation + session
    context), never to model-supplied ids/handles: notes are limited to what
    the current speaker discloses about themself, recall to the speaker's own
    keys, and wake/nap are refused as internal housekeeping.
  * Note writes are budgeted BEFORE the backend runs — 2 per turn, 8 per
    verified speaker per UTC day, 64 per profile per UTC day — counted from
    one profile-owned atomic ledger (recall-scope.db beside the store) with
    fact-identity digests, so concurrent writes cannot double-spend and
    identical retries are idempotent no-ops. Failed saves are not claimed and
    not spent. Legacy unscoped lines stay quarantined from scoped recall until
    a reviewed successor is recorded; the memo store itself is never rewritten.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_BINARY = "/var/lib/hermes/.optmem/memo"
NOTE_MAX_BYTES = 280       # Safety ceiling; a store may configure a lower limit
RECALL_MAX_CHARS = 120
TIMEOUT_S = 30
OUTPUT_MAX_CHARS = 8000
WAKE_MAX_PARTS = 128

# ---- speaker-scope authorization (public companion surface) -----------------
# On a speaker-scoped companion profile every explicit tool binds to the VERIFIED
# turn identity (the ambient adapter's attestation + session context), never to
# anything the model wrote. Note writes are additionally budgeted BEFORE the
# backend runs; successful unique writes only, identical retries idempotent.
NOTE_LIMIT_PER_TURN = 2
NOTE_LIMIT_PER_SPEAKER_DAY = 8
NOTE_LIMIT_PER_PROFILE_DAY = 64
# One profile-owned ledger beside the memo store (never inside it): provenance for
# audience-scoped recall + the atomic note budget. DIGEST-ONLY by contract: OptMem
# stays the single canonical store of fact text; this ledger never duplicates it.
# row_key = idempotency identity (author+subject+fact); fact_digest = the
# visibility key (subject+fact) the recall filter computes from memo output.
# The paired recall plugin reads the same schema — keep DDL byte-identical.
SCOPE_DB_NAME = "recall-scope.db"
NOTE_SCOPE_DDL = (
    "CREATE TABLE IF NOT EXISTS note_scope ("
    " row_key TEXT PRIMARY KEY,"
    " fact_digest TEXT NOT NULL,"
    " subject TEXT NOT NULL,"
    " author TEXT NOT NULL,"
    " audience TEXT NOT NULL,"
    " source_event TEXT NOT NULL DEFAULT '',"
    " turn_key TEXT NOT NULL DEFAULT '',"
    " day TEXT NOT NULL,"
    " created_utc TEXT NOT NULL)"
)
SCOPE_LEDGER_COLUMNS = frozenset(
    {"row_key", "fact_digest", "subject", "author", "audience",
     "source_event", "turn_key", "day", "created_utc"})
_RECALL_NOISE_RE = re.compile(
    r"^(?:no\s+match(?:es)?|\d+\s+match(?:es)?)\.?$", re.IGNORECASE)
_LISTING_PREFIX_RE = re.compile(r"^#\d+\s+\S+\s+")

_INTERNAL_REFUSAL = (
    "optmem: refused — whole-store memory housekeeping is internal to the "
    "operator's maintenance surface and is not available in shared chat. Use "
    "person-scoped recall instead."
)

_OPERATOR_SCOPE_UNCONFIRMED = (
    "optmem_note: unconfirmed — the memo store reported a save, but its "
    "scope ledger receipt is missing. The raw line may be quarantined; "
    "do not claim it was remembered. Inspect the store and ledger before retrying."
)


def _tail_limit(text: str) -> str:
    if len(text) <= OUTPUT_MAX_CHARS:
        return text
    prefix = "… (older wake output truncated)\n"
    return prefix + text[-(OUTPUT_MAX_CHARS - len(prefix)):]


def _head_limit(text: str) -> str:
    if len(text) <= OUTPUT_MAX_CHARS:
        return text
    suffix = "\n… (truncated)"
    return text[:OUTPUT_MAX_CHARS - len(suffix)] + suffix


def _cfg() -> dict:
    """This profile's `optmem` config block (per-profile under multiplex).

    load_config() resolves against the ACTIVE HERMES_HOME, and gateway turns
    run inside a per-profile scope, so this returns the right profile's block
    without the plugin knowing anything about profiles.
    """
    try:
        from hermes_cli.config import load_config

        cfg = load_config().get("optmem")
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        logger.debug("optmem: could not read config", exc_info=True)
        return {}

def _memory_dir() -> str:
    return str(_cfg().get("memory_dir") or "").strip()


def _binary() -> str:
    return str(_cfg().get("binary") or DEFAULT_BINARY).strip()


def _available() -> bool:
    """check_fn: the tools exist only for a profile that configured a memory dir
    AND on a host where the binary is actually present and executable."""
    d = _memory_dir()
    if not d:
        return False
    b = _binary()
    return os.path.isfile(b) and os.access(b, os.X_OK)


def _now():
    """UTC now (patchable clock for budget tests)."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _scope_db_path(memory_dir: str) -> str:
    return os.path.join(memory_dir or "", SCOPE_DB_NAME)


def _profile_restricted() -> bool:
    """Whether the ACTIVE profile carries the shared/public companion posture.

    Own config read (not ``_cfg``): a read failure counts as RESTRICTED —
    scope determination fails closed, never open. Never consults model args.
    """
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
    except Exception:
        return True
    section = cfg.get("optmem") if isinstance(cfg, dict) else None
    if not isinstance(section, dict):
        return False
    return str(section.get("speaker_scoped")).strip().lower() in {"1", "true", "yes", "on"}


def _companion_turn() -> dict | None:
    """The verified companion turn, or ``None`` off the restricted surface.

    RESTRICTED is decided by server-owned facts only, in this order: the
    profile's shared/public posture, then the server-bound platform. The
    ambient adapter's turn attestation only VERIFIES the speaker — a missing
    attestation (plugin failed to load, binding failed) or an unreadable
    session context on a restricted profile yields ``{"verified": False}``
    (handlers refuse), NEVER ``None`` (stock). A caller-supplied id, handle or
    mention is never authority.
    """
    def _unverified() -> dict:
        return {"verified": False, "speaker_id": "", "speaker_name": "",
                "message_id": ""}

    restricted = _profile_restricted()
    try:
        from gateway import session_context
    except Exception:
        return _unverified() if restricted else None
    try:
        platform = session_context.get_session_env("HERMES_SESSION_PLATFORM", "")
    except Exception:
        return _unverified() if restricted else None
    if platform != "discord":
        return None  # CLI, cron, nonambient platforms: stock tools, unchanged
    if not restricted:
        return None  # private work profile on Discord: stock tools, unchanged
    try:
        message_id = session_context.get_session_env("HERMES_SESSION_MESSAGE_ID", "")
        user_id = session_context.get_session_env("HERMES_SESSION_USER_ID", "")
        user_name = session_context.get_session_env("HERMES_SESSION_USER_NAME", "")
        attestation = getattr(session_context, "_ambient_turn_identity", None)
        identity = attestation.get(None) if attestation is not None else None
    except Exception:
        return _unverified()
    verified = bool(attestation is not None and user_id and message_id
                    and identity == (message_id, user_id))
    return {"verified": verified,
            "speaker_id": user_id if verified else "",
            "speaker_name": user_name if verified else "",
            "message_id": message_id}



def _ledger_write_conn(memory_dir: str):
    """Writer connection to the scope ledger, or None when it cannot open.

    A pre-digest legacy table (with fact_text) is refused rather than written:
    the ledger must never become a second copy of the fact corpus. No such
    database is deployed; refuse-and-tell beats silent duplication.
    """
    if not memory_dir or not os.path.isdir(memory_dir):
        return None
    try:
        conn = sqlite3.connect(_scope_db_path(memory_dir), timeout=10,
                               isolation_level=None)
        existing = conn.execute(
            "SELECT name FROM pragma_table_info('note_scope')").fetchall()
        if existing and SCOPE_LEDGER_COLUMNS != {row[0] for row in existing}:
            conn.close()
            logger.warning(
                "optmem: legacy scope ledger schema at %s; delete the derived "
                "ledger file to recreate it (it duplicates fact text and was "
                "never deployed)", _scope_db_path(memory_dir))
            return None
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(NOTE_SCOPE_DDL)
        return conn
    except Exception:
        logger.warning("optmem: could not open scope ledger", exc_info=True)
        return None

def _next_utc_day_iso() -> str:
    tomorrow = (_now() + timedelta(days=1)).date()
    return f"{tomorrow.isoformat()}T00:00:00Z"


def _ledger_read_conn(memory_dir: str):
    """Read-only ledger connection, or None when absent/unreadable (quarantine)."""
    path = _scope_db_path(memory_dir)
    if not memory_dir or not os.path.isfile(path):
        return None
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except Exception:
        return None


def _fact_key(text: str) -> str:
    """Normalised fact text as written/read by memo (prefix stripped, collapsed)."""
    return " ".join(_LISTING_PREFIX_RE.sub("", str(text or "").strip()).split())


def _visibility_digest(subject: str, text: str) -> str:
    """The ledger's fact key: sha256 over subject + normalised fact — no raw text."""
    return hashlib.sha256(
        f"fact\x00{subject or ''}\x00{_fact_key(text)}".encode()).hexdigest()


def _note_row_key(author: str, subject: str, text: str) -> str:
    """Idempotency identity for a note write (author+subject+fact)."""
    return hashlib.sha256(
        f"note\x00{author}\x00{subject or ''}\x00{_fact_key(text)}".encode()).hexdigest()


def _strip_listing_prefix(line: str) -> str:
    return _LISTING_PREFIX_RE.sub("", line.strip())


def _visible_lines(lines: str, subject_keys, audiences, memory_dir: str) -> list:
    """Recall lines visible to this subject/audience, per the digest-only ledger.

    A line is visible only when the ledger holds a record whose fact_digest
    matches (subject, normalised line) with an allowed audience. The ledger
    stores no fact text — OptMem stays the single canonical store. Lines with
    no matching record (legacy unscoped notes, compressed rewrites) are
    quarantined — fail closed — and a ``revoked`` record for the same digest
    suppresses the line outright. A global tombstone (empty subject, revoked)
    is checked before per-subject visibility. The memo store is never modified.
    """
    conn = _ledger_read_conn(memory_dir)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT fact_digest, subject, audience FROM note_scope").fetchall()
    except Exception:
        return []
    finally:
        conn.close()
    allowed = {(digest, subject) for digest, subject, audience in rows
               if audience in audiences}
    revoked = {digest for digest, _subject, audience in rows
               if audience == "revoked"}
    out = []
    for raw in (lines or "").splitlines():
        text = _strip_listing_prefix(raw)
        if not text or _RECALL_NOISE_RE.match(text):
            continue
        if _visibility_digest("", text) in revoked:
            continue
        revoked_hit = False
        visible_hit = False
        for subject in subject_keys:
            digest = _visibility_digest(subject, text)
            if digest in revoked:
                revoked_hit = True
                break
            if (digest, subject) in allowed:
                visible_hit = True
        if revoked_hit or not visible_hit:
            continue
        out.append(text)
    return out

def _run(args: list[str]) -> str:
    """Run the memo binary with a fixed argv. Never uses a shell."""
    d = _memory_dir()
    if not d:
        return "optmem is not configured for this profile."
    b = _binary()
    if not os.path.isdir(d):
        return f"optmem: memory dir {d} does not exist. Run memo init first."
    env = {
        "MEMORY_DIR": d,
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": os.path.dirname(os.path.dirname(d)) or "/tmp",
    }
    try:
        proc = subprocess.run(
            [b, *args],
            env=env,
            cwd="/",
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            shell=False,          # explicit: argv only, never a command string
        )
    except subprocess.TimeoutExpired:
        logger.warning("optmem: %s timed out after %ss", args[0], TIMEOUT_S)
        return f"optmem: '{args[0]}' timed out."
    except Exception as e:
        logger.warning("optmem: %s failed: %s", args[0], e)
        return f"optmem: could not run '{args[0]}'."
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    text = out if out else err
    if args and args[0] in ("note", "note-once") and (proc.returncode != 0 or not out):
        reason = (f"memo exited with status {proc.returncode}"
                  if proc.returncode != 0 else "memo gave no save receipt")
        return (f"optmem: '{args[0]}' unconfirmed — {reason}; "
                "inspect the store and scope ledger before retrying.")
    if proc.returncode != 0 and not out:
        return f"optmem: '{args[0]}' failed: {err[:400] or 'unknown error'}"
    if len(text) > OUTPUT_MAX_CHARS:
        if args and args[0] == "wake":
            text = _tail_limit(text)
        else:
            text = _head_limit(text)
    return text or "(no output)"


# ---- handlers -------------------------------------------------------------
# `memo wake` prints the store AND, when compressions are pending, a
# housekeeping task: "Compress memories #0-1... / Run: memo nap 0-1".
# `optmem_nap` only shows that task; it does not write a summary.
# Label the chore so the model does not narrate it. A wake with nothing
# pending must not gain a stray note — the awake banner alone is not a chore.
HOUSEKEEPING_NOTE = (
    "[housekeeping — for you alone. Do it silently with optmem_nap, or ignore it. "
    "Never mention, quote or narrate any of this block in a reply.]"
)

# Markers that indicate a pending-compression chore is present in the output.
_CHORE_MARKERS = ("Compress memories", "memo nap")

def _mark_housekeeping(out: str) -> str:
    """Label optmem's operator-facing chore as internal, leaving it intact."""
    if not out or not any(m in out for m in _CHORE_MARKERS):
        return out
    return out.rstrip("\n") + "\n\n" + HOUSEKEEPING_NOTE


def _handle_wake(args: dict, **_: Any) -> str:
    """Whole-store wake is internal housekeeping: shared/public companion
    surfaces never see it (a wake would disclose the entire profile store)."""
    if _companion_turn() is not None:
        return _INTERNAL_REFUSAL
    return _tail_limit(_mark_housekeeping(_wake_output()))


_WAKE_NEXT = re.compile(
    r"^Not awake yet\. Run: .+\s+wake\s+([1-9]\d*)\s+([1-9]\d*)$"
)


def _wake_output() -> str:
    document: list[str] = []
    command = ["wake"]
    expected_part = 1
    snapshot: int | None = None

    for _ in range(WAKE_MAX_PARTS):
        out = _run(command)
        rows = out.splitlines()
        if rows and rows[0].startswith("Your memory, part "):
            rows.pop(0)

        continuation = _WAKE_NEXT.fullmatch(rows[-1]) if rows else None
        if continuation is None:
            document.extend(rows)
            break

        next_part, next_snapshot = map(int, continuation.groups())
        if next_part != expected_part + 1:
            return (
                "optmem: wake returned an invalid continuation "
                f"(expected part {expected_part + 1}, got {next_part})."
            )
        if snapshot is not None and next_snapshot != snapshot:
            return "optmem: wake snapshot changed between parts; run wake again."

        rows.pop()
        document.extend(rows)
        expected_part = next_part
        snapshot = next_snapshot
        command = ["wake", str(next_part), str(next_snapshot)]
    else:
        return (
            f"optmem: wake exceeded {WAKE_MAX_PARTS} parts; "
            "reduce the store's wake output fragmentation."
        )

    out = "\n".join(document)
    lines = _cfg().get("wake_lines")
    if lines:
        try:
            limit = int(lines)
        except (TypeError, ValueError):
            limit = 0
        if limit > 0:
            rows = out.splitlines()
            if len(rows) > limit:
                out = "\n".join(rows[-limit:])
    return _tail_limit(out) or "(no output)"


# A Discord id is digits. People-notes here are written `@handle id:<digits>: fact`,
# and the model has repeatedly written the PLACEHOLDER instead of the number — notes
# containing the literal string `id:<number>` were found in her store. That is silent
# corruption: the note looks right, recall matches nothing, and nobody notices, because a
# memory that never matches is indistinguishable from a memory nobody needed.
# Decision O1 (operator, 2026-08-09): reject it at write time.
#
# A regex, not an ontology — rung 3, not rung 5. It converts a class of silent corruption
# into an impossible state, costs no inference, and fixes a defect we observed rather than
# one we imagined.
#
# Deliberately narrow: a note with NO id is fine and always was. Only a PRESENT-and-
# malformed id is refused, so this cannot block ordinary notes.
_ID_TOKEN = re.compile(r"\bid:\s*([^\s,;]+)", re.I)


def _malformed_ids(text: str) -> list[str]:
    return [m.group(1) for m in _ID_TOKEN.finditer(text)
            if not m.group(1).strip("<>[]().,;:").isdigit()]


def _note_max_bytes(memory_dir: str) -> int:
    """Return the store's ENTRY_CHARS, capped by this plugin's safety ceiling."""
    if not memory_dir:
        return NOTE_MAX_BYTES
    configured = NOTE_MAX_BYTES
    try:
        with open(os.path.join(memory_dir, "config"), encoding="utf-8") as fh:
            for raw in fh:
                line = raw.partition("#")[0]
                key, separator, value = line.partition("=")
                if separator and key.strip().upper() == "ENTRY_CHARS":
                    value = value.strip()
                    if value.isdigit() and int(value) > 0:
                        configured = int(value)
    except (OSError, UnicodeError):
        pass
    return min(NOTE_MAX_BYTES, configured)


def _extract_subject(text: str) -> str:
    """The subject key a note text claims: ``id:<digits>`` wins (the stable
    numeric identity), then a leading ``@handle``; ``""`` when the note is not
    about a person. The id token keeps its trailing punctuation in the regex,
    so strip the same delimiter set ``_malformed_ids`` uses before checking."""
    m = _ID_TOKEN.search(text)
    if m:
        token = m.group(1).strip("<>[]().,;:")
        if token.isdigit():
            return f"id:{token}"
    m = re.match(r"^@([A-Za-z0-9_.]{2,32})\b", text)
    return m.group(1) if m else ""


def _note_save_failed(out: str) -> bool:
    return not out or out.startswith("optmem")


def _budgeted_note(memory_dir: str, turn: dict, text: str, subject: str,
                   turn_key: str, run_note) -> str:
    """Spend one atomic budget slot, run the note, commit after the store write.

    Returns the tool result string. Revoked digests (global tombstone and
    this subject), the ledger insert, and the three budget counts are
    checked inside one ``BEGIN IMMEDIATE`` transaction held across the
    memo backend call, so concurrent writers cannot double-spend, a
    revoke for the same store waits until this note finishes its store
    write, a revoked fact cannot claim success, and an identical digest
    (same author+subject+fact) is an idempotent no-op that never re-runs
    memo. ``Saved.`` is returned only when both the backend write and the
    ledger commit succeed.
    """
    author = f"id:{turn['speaker_id']}"
    audience = f"id:{turn['speaker_id']}"
    row_key = _note_row_key(author, subject, text)
    fact_digest = _visibility_digest(subject, text)
    day = _now().strftime("%Y-%m-%d")
    conn = _ledger_write_conn(memory_dir)
    if conn is None:
        return ("optmem_note: refused — the memory budget record could not open, "
                "so no write is authorized this turn. Nothing was written.")
    backend_ran = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
                "SELECT 1 FROM note_scope WHERE audience = 'revoked' "
                "AND fact_digest IN (?, ?)",
                (_visibility_digest("", text), fact_digest)).fetchone() is not None:
            conn.execute("ROLLBACK")
            return ("optmem_note: refused — this fact was revoked; "
                    "nothing was written, and this turn must not claim the "
                    "note was remembered.")
        existing = conn.execute(
            "SELECT 1 FROM note_scope WHERE row_key = ?", (row_key,)
        ).fetchone()
        if existing is not None:
            conn.execute("COMMIT")
            return ("optmem_note: already remembered — identical retry, "
                    "nothing new written.")
        if conn.execute(
                "SELECT COUNT(*) FROM note_scope WHERE turn_key = ? AND day = ?",
                (turn_key, day)).fetchone()[0] >= NOTE_LIMIT_PER_TURN:
            conn.execute("ROLLBACK")
            return (f"optmem_note: refused — this turn's note budget "
                    f"({NOTE_LIMIT_PER_TURN}) is exhausted; try again in a later "
                    "turn. Nothing was written, and this turn must not claim the "
                    "note was remembered.")
        if conn.execute(
                "SELECT COUNT(*) FROM note_scope WHERE author = ? AND day = ?",
                (author, day)).fetchone()[0] >= NOTE_LIMIT_PER_SPEAKER_DAY:
            conn.execute("ROLLBACK")
            return (f"optmem_note: refused — this speaker's daily note budget "
                    f"({NOTE_LIMIT_PER_SPEAKER_DAY}) is exhausted; retry after "
                    f"{_next_utc_day_iso()}. Nothing was written, and this turn "
                    "must not claim the note was remembered.")
        if conn.execute(
                "SELECT COUNT(*) FROM note_scope WHERE day = ?",
                (day,)).fetchone()[0] >= NOTE_LIMIT_PER_PROFILE_DAY:
            conn.execute("ROLLBACK")
            return (f"optmem_note: refused — this profile's daily note budget "
                    f"({NOTE_LIMIT_PER_PROFILE_DAY}) is exhausted; retry after "
                    f"{_next_utc_day_iso()}. Nothing was written, and this turn "
                    "must not claim the note was remembered.")
        conn.execute(
            "INSERT INTO note_scope (row_key, fact_digest, subject, author,"
            " audience, source_event, turn_key, day, created_utc)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (row_key, fact_digest, subject, author, audience,
             turn.get("message_id", ""), turn_key, day, _now().isoformat()))
        backend_ran = True
        out = run_note()
        if _note_save_failed(out):
            conn.execute("ROLLBACK")
            return out or (
                "optmem_note: unconfirmed — the backend gave no save receipt; "
                "a quarantined store line may exist. Do not claim it was saved."
            )
        conn.execute("COMMIT")
        return out
    except Exception:
        logger.warning("optmem: note budget transaction failed", exc_info=True)
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            logger.warning("optmem: note budget rollback failed", exc_info=True)
        if backend_ran:
            return ("optmem_note: unconfirmed — the store may contain a "
                    "quarantined fact; not confirmed saved.")
        return ("optmem_note: refused — the memory budget could not be recorded; "
                "no write happened. Nothing was written.")
    finally:
        conn.close()


def _handle_note(args: dict, task_id: str = "", session_id: str = "", **_: Any) -> str:
    text = str(args.get("text") or "").strip()
    if not text:
        return "optmem_note: 'text' is required."
    bad = _malformed_ids(text)
    if bad:
        # Say what to do, not only what went wrong: a refusal the model cannot act on
        # becomes a retry loop with the same value.
        return ("optmem_note: refused — `id:` must be the person's numeric Discord id, "
                f"not {bad[0]!r}. Use the real numeric id (e.g. `id:1234567890`), or "
                "leave `id:` out entirely if you do not have it. Nothing was written.")
    # OptMem's unit is ONE line: collapse whitespace so a pasted block cannot
    # corrupt the append-only log, then enforce the store's configured limit.
    text = " ".join(text.split())
    n = len(text.encode())
    memory_dir = _memory_dir()
    limit = _note_max_bytes(memory_dir)
    if n > limit:
        return (
            f"optmem_note: refused — a memory is at most {limit} bytes "
            f"(this one is {n}). Shorten it. Nothing was written."
        )

    turn = _companion_turn()
    if turn is None:
        # Off-surface (CLI/operator): stock write, recorded as operator-authored.
        out = _run(["note", text])
        if not _note_save_failed(out):
            conn = _ledger_write_conn(memory_dir)
            if conn is None and _profile_restricted():
                return _OPERATOR_SCOPE_UNCONFIRMED
            if conn is not None:
                try:
                    subject = _extract_subject(text)
                    conn.execute(
                        "INSERT OR IGNORE INTO note_scope (row_key, fact_digest,"
                        " subject, author, audience, source_event, turn_key, day,"
                        " created_utc) VALUES (?,?,?,?,?,?,?,?,?)",
                        (_note_row_key("operator", subject, text),
                         _visibility_digest(subject, text),
                         subject, "operator", "operator",
                         "cli", "cli", _now().strftime("%Y-%m-%d"),
                         _now().isoformat()))
                except sqlite3.Error:
                    if not _profile_restricted():
                        raise
                    logger.warning("optmem: operator note scope ledger write failed",
                                   exc_info=True)
                    return _OPERATOR_SCOPE_UNCONFIRMED
                finally:
                    conn.close()
        return out

    if not turn["verified"]:
        return ("optmem_note: refused — no verified speaker identity for this "
                "turn, so no memory write is authorized. Nothing was written.")
    subject = _extract_subject(text)
    if not subject:
        return ("optmem_note: refused — a shared-profile note needs the CURRENT "
                f"speaker's numeric id:{turn['speaker_id']} in its text, so it "
                "can be found and authorized later. Nothing was written.")
    allowed = {f"id:{turn['speaker_id']}"}
    if turn["speaker_name"]:
        allowed.add(turn["speaker_name"])
    if subject not in allowed:
        return (f"optmem_note: refused — notes on a shared profile may only record "
                f"what the CURRENT speaker (id:{turn['speaker_id']}) tells you "
                "about themself. A note about someone else is an attributed claim "
                "the reviewer must scope; do not write it here. Nothing was "
                "written.")
    turn_key = str(task_id or "").strip() or (
        f"session:{session_id}" if session_id else "unkeyed")
    return _budgeted_note(memory_dir, turn, text, subject, turn_key,
                          lambda: _run(["note-once", text]))


def _handle_recall(args: dict, **_: Any) -> str:
    query = str(args.get("pattern") or "").strip()
    if not query:
        return "optmem_recall: 'pattern' is required."
    if len(query) > RECALL_MAX_CHARS:
        return f"optmem_recall: query too long (max {RECALL_MAX_CHARS})."
    turn = _companion_turn()
    if turn is None:
        return _run(["recall", re.escape(query)])
    if not turn["verified"]:
        return ("optmem_recall: refused — no verified speaker identity for this "
                "turn, so no memory search is authorized.")
    own_keys = {f"id:{turn['speaker_id']}"}
    if turn["speaker_name"]:
        own_keys.add(turn["speaker_name"])
    if query not in own_keys:
        return ("optmem_recall: refused — on a shared profile you may search only "
                "the CURRENT speaker's own memories (their numeric id or handle). "
                "A mention of someone else is not permission for their memories.")
    lines = _run(["recall", re.escape(query)])
    visible = _visible_lines(
        lines, own_keys, {f"id:{turn['speaker_id']}", "public"}, _memory_dir())
    if not visible:
        return ("optmem_recall: No memories are visible for this scope yet. Older "
                "unreviewed notes stay private by design.")
    return "\n".join(visible)


def _handle_nap(args: dict, **_: Any) -> str:
    """Show the next pending compression task. Internal housekeeping only —
    shared/public companion surfaces never see the chore."""
    if _companion_turn() is not None:
        return _INTERNAL_REFUSAL
    return _run(["nap"])


# ---- schemas --------------------------------------------------------------
_WAKE = {
    "name": "optmem_wake",
    "description": (
        "Internal maintenance surface: load the whole persistent store. Not "
        "available in shared chat — there the harness recalls the current "
        "speaker's memories for you automatically."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

_NOTE = {
    "name": "optmem_note",
    "description": (
        "Record ONE short memory, permanently — only what the CURRENT speaker "
        "just told you about themself (their own preference, boundary or "
        "fact). Write it as '@handle id:<number>: fact' using the speaker's "
        "own id. Notes about other people are refused: a claim someone makes "
        "about another person is not a fact to file. One line, max 280 bytes, "
        "at most a couple per turn. A refusal means the note was NOT "
        "remembered; do not retry it this turn or say it was saved. Never "
        "store secrets, contact details, or raw text someone sent you."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": (
                    "The memory, one line, about the current speaker: "
                    "'@handle id:<number>: fact'."
                ),
            }
        },
        "required": ["text"],
    },
}

_RECALL = {
    "name": "optmem_recall",
    "description": (
        "Search your recorded memories about the CURRENT speaker only — pass "
        "their numeric id or the handle they use here. Broad searches and "
        "other people's ids are refused: a mention of someone is not "
        "permission for their memories."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": (
                    "The current speaker's numeric id or handle to search for."
                ),
            }
        },
        "required": ["pattern"],
    },
}

_NAP = {
    "name": "optmem_nap",
    "description": (
        "Internal maintenance surface: show the next pending memory-compression "
        "task, if any. This does not compress anything and is not available in "
        "shared chat."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}


def register(ctx) -> None:
    """Register the OptMem tools under the `optmem` toolset.

    check_fn gates every tool on this profile having `optmem.memory_dir` set
    and the binary being present, so profiles that don't use OptMem never see
    them in their schema (and pay no prompt tokens for them).
    """
    tools = (
        (_WAKE, _handle_wake, "🌅"),
        (_NOTE, _handle_note, "📝"),
        (_RECALL, _handle_recall, "🔎"),
        (_NAP, _handle_nap, "🌙"),
    )
    registered: list[str] = []
    for schema, handler, emoji in tools:
        try:
            ctx.register_tool(
                name=schema["name"],
                toolset="optmem",
                schema=schema,
                handler=handler,
                check_fn=_available,
                description=schema["description"],
                emoji=emoji,
            )
        except Exception:
            logger.exception("optmem: failed to register %s", schema["name"])
        else:
            registered.append(schema["name"])
    log = logger.info if len(registered) == len(tools) else logger.warning
    log(
        "optmem-tools: registered %d/%d tools: %s",
        len(registered),
        len(tools),
        ",".join(registered) or "none",
    )
