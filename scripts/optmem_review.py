#!/usr/bin/env python3
"""optmem_review — operator-only review/import for legacy OptMem memories.

WHY: audience-scoped companion recall quarantines every legacy
store line that carries no verified author/audience provenance. Quarantine is
safe but total — on activation the companion would remember nobody. This script
is the bounded, human-gated way out: the operator reviews legacy lines and
records a scoped successor for each one they approve. The ORIGINAL store line is
never edited or deleted; only provenance rows are added to the derived scope
ledger (``recall-scope.db`` beside the store), which stores DIGESTS ONLY — the
OptMem store stays the single copy of every fact.

AUTHORITY MODEL — deliberately narrow:
  * Run by the operator, on the host, with an explicit ``--memory-dir``. There
    is no default directory and no live-path shortcut; the script never touches
    a store unless you name it.
  * Reads ``LOG.txt`` (the canonical append-only memory log) only under this
    explicit operator action; ``list`` previews truncated content, ``show``
    prints one line in full on the operator's own terminal. Nothing here is
    model-callable, Discord-reachable, or exported anywhere.
  * Writes ONLY to the derived ledger — never to LOG.txt, TREE/ or config.

The digest scheme is the shared ledger contract (byte-for-byte the same as
the paired recall plugin and this package's ``__init__``): keep in sync.

Exit codes: 0 = ok, 1 = refusal (message says why), 2 = usage error.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG_NAME = "LOG.txt"
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
_LISTING_PREFIX_RE = re.compile(r"^#\d+\s+\S+\s+")
_ID_TOKEN = re.compile(r"\bid:\s*([^\s,;]+)", re.I)
# Stability is about FORM (numeric id or plain handle), not snowflake length:
# this tool also reviews synthetic/legacy stores with short ids.
_SAFE_SUBJECT_RE = re.compile(r"^(?:id:\d{1,25}|[A-Za-z0-9_.]{2,32})$")
PREVIEW_CHARS = 48


def _fact_key(line: str) -> str:
    return " ".join(_LISTING_PREFIX_RE.sub("", str(line or "").strip()).split())


def _fact_digest_of(line: str) -> str:
    return hashlib.sha256(f"legacy\x00{_fact_key(line)}".encode()).hexdigest()


def _visibility_digest(subject: str, fact_key: str) -> str:
    return hashlib.sha256(f"fact\x00{subject}\x00{fact_key}".encode()).hexdigest()


def _extract_subject(fact_key: str) -> str:
    """Stable subject key: ``id:<digits>`` wins (numeric identity), then a
    leading ``@handle``. The regex token keeps trailing punctuation, so strip
    the delimiter set before the digit check — `id:111:` binds id:111, never
    the mutable handle."""
    m = _ID_TOKEN.search(fact_key)
    if m:
        token = m.group(1).strip("<>[]().,;:")
        if token.isdigit():
            return f"id:{token}"
    m = re.match(r"^@([A-Za-z0-9_.]{2,32})\b", fact_key)
    return m.group(1) if m else ""


def _review_row_key(reviewed_by: str, subject: str, audience: str, fact_key: str) -> str:
    return hashlib.sha256(
        f"review\x00{reviewed_by}\x00{subject}\x00{audience}\x00{fact_key}"
        .encode()).hexdigest()


def _store_lines(memory_dir: Path):
    log = memory_dir / LOG_NAME
    if not log.is_file():
        return None, f"no {LOG_NAME} under {memory_dir} — is this a memo store?"
    try:
        raw = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return None, f"cannot read {log}: {exc}"
    return [(i, _fact_key(line)) for i, line in enumerate(raw) if _fact_key(line)], None


def _ledger_conn(memory_dir: Path):
    """Writer connection -> ``(conn, None)`` or ``(None, error)``."""
    path = memory_dir / SCOPE_DB_NAME
    try:
        conn = sqlite3.connect(path, timeout=10, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(NOTE_SCOPE_DDL)
        return conn, None
    except Exception as exc:  # noqa: BLE001 — operator-facing message
        return None, exc


def _ledger_conn_ro(memory_dir: Path):
    """Read-only connection for ``list``; None when absent — never creates."""
    path = memory_dir / SCOPE_DB_NAME
    if not path.is_file():
        return None
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except Exception:
        return None

def _ledger_states(conn, fact_key: str, subject: str):
    digest = _visibility_digest(subject, fact_key)
    rows = conn.execute(
        "SELECT audience FROM note_scope WHERE fact_digest = ?", (digest,)
    ).fetchall()
    audiences = [row[0] for row in rows if row[0] != "revoked"]
    revoked = any(row[0] == "revoked" for row in rows)
    return audiences, revoked


def _matching_rows(fact_key, rows):
    """Operator-only: ledger (subject, audience) pairs that bind this store fact.

    The ledger stores no fact text, so match by hashing every known ledger
    subject against the store line. Callers pass one preloaded ``rows``
    snapshot — no extra SQLite round-trips.
    """
    subjects = {subject for _digest, subject, _audience in rows}
    if not subjects:
        return []
    wanted = {_visibility_digest(subject, fact_key): subject for subject in subjects}
    return [(subject, audience) for digest, subject, audience in rows
            if wanted.get(digest) == subject]


def _preview(fact_key: str) -> str:
    if len(fact_key) <= PREVIEW_CHARS:
        return fact_key
    return fact_key[:PREVIEW_CHARS - 1] + "…"


def _cmd_list(args) -> int:
    memory_dir = Path(args.memory_dir)
    entries, err = _store_lines(memory_dir)
    if err:
        print(f"refused: {err}")
        return 1
    conn = _ledger_conn_ro(memory_dir)  # never creates the ledger
    try:
        shown = 0
        if conn is None:
            if (memory_dir / SCOPE_DB_NAME).exists():
                print("refused: existing scope ledger unreadable; no review state reported")
                return 1
            rows = []
        else:
            try:
                rows = conn.execute(
                    "SELECT fact_digest, subject, audience FROM note_scope"
                ).fetchall()
            except Exception:
                print("refused: scope ledger read failed; no review state reported")
                return 1
        for _idx, fact_key in entries:
            subject = _extract_subject(fact_key)
            matched = _matching_rows(fact_key, rows)
            revoked_subjects = {s for s, a in matched if a == "revoked"}
            globally_revoked = "" in revoked_subjects
            live = [a for s, a in matched
                    if a != "revoked" and s not in revoked_subjects]
            # Operator-authored rows record provenance, not an approval that
            # any companion recall reader can use. Keep them in the default
            # review queue until a numeric speaker or public audience is set.
            effective = [
                a for a in live
                if a == "public" or (a.startswith("id:") and a[3:].isdigit())
            ]
            state = ("revoked" if globally_revoked or (revoked_subjects and not effective)
                     else "approved:" + ",".join(sorted(effective)) if effective
                     else "unreviewed")
            if state.startswith("approved:") and not args.include_reviewed:
                continue
            print(f"{_fact_digest_of(fact_key)[:16]}  subject={subject or '-':<12} "
                  f"{state:<18} {_preview(fact_key)}")
            shown += 1
            if args.limit and shown >= args.limit:
                break
        if not shown:
            print("(no unreviewed legacy entries)")
    finally:
        if conn is not None:
            conn.close()
    return 0


def _cmd_show(args) -> int:
    memory_dir = Path(args.memory_dir)
    entries, err = _store_lines(memory_dir)
    if err:
        print(f"refused: {err}")
        return 1
    try:
        fact_key = _find_fact(entries, args.digest)
    except ValueError as exc:
        print(f"refused: {exc} — nothing shown")
        return 1
    print(fact_key)
    return 0


def _find_fact(entries, digest: str):
    """Resolve exactly one current store fact from the displayed digest token."""
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{16}", digest) is None:
        raise ValueError("digest must be the exact 16-hex token from list")
    match = None
    for _idx, fact_key in entries:
        if _fact_digest_of(fact_key).startswith(digest.lower()):
            if match is not None and match != fact_key:
                raise ValueError("ambiguous digest token matches multiple store facts")
            match = fact_key
    if match is None:
        raise ValueError("digest matches no current store line (stale or changed)")
    return match


def _cmd_approve(args) -> int:
    memory_dir = Path(args.memory_dir)
    entries, err = _store_lines(memory_dir)
    if err:
        print(f"refused: {err}")
        return 1
    try:
        fact_key = _find_fact(entries, args.digest)
    except ValueError as exc:
        print(f"refused: {exc}; re-run list. Nothing was written")
        return 1
    subject = args.subject or _extract_subject(fact_key)
    if not _SAFE_SUBJECT_RE.match(subject or ""):
        print(f"refused: subject {subject!r} is not a stable key "
              "(id:<digits> or a plain handle); nothing was written")
        return 1
    if args.audience == "subject" and not subject.startswith("id:"):
        print("refused: private approval requires a verified numeric id:<digits> "
              "subject; pass --subject id:<digits> after checking the person. "
              "Nothing was written.")
        return 1
    audience = "public" if args.audience == "public" else subject
    conn, conn_err = _ledger_conn(memory_dir)
    if conn is None:
        print(f"refused: cannot open scope ledger ({conn_err})")
        return 1
    try:
        conn.execute("BEGIN IMMEDIATE")
        if (_ledger_states(conn, fact_key, "")[1]
                or _ledger_states(conn, fact_key, subject)[1]):
            conn.execute("ROLLBACK")
            print("refused: this fact was revoked for that subject; a revoked fact "
                  "cannot be re-approved (un-revoke deliberately in SQLite). "
                  "Nothing was written.")
            return 1
        row_key = _review_row_key(args.reviewed_by, subject, audience, fact_key)
        cur = conn.execute(
            "INSERT OR IGNORE INTO note_scope (row_key, fact_digest, subject,"
            " author, audience, source_event, turn_key, day, created_utc)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (row_key, _visibility_digest(subject, fact_key), subject,
             args.reviewed_by, audience, f"review:{args.reviewed_by}", "review",
             "", datetime.now(timezone.utc).isoformat()))
        if cur.rowcount == 0:
            conn.execute("ROLLBACK")
            print(f"already approved for subject={subject} audience={audience} "
                  "by this reviewer — nothing new written")
            return 1
        conn.execute("COMMIT")
        print(f"approved: subject={subject} audience={audience} "
              f"review={args.reviewed_by} (original store line untouched)")
        return 0
    except Exception as exc:  # noqa: BLE001 — operator-facing refusal
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        print(f"refused: scope ledger transaction failed ({type(exc).__name__}); "
              "approval not confirmed — inspect the ledger before retrying")
        return 1
    finally:
        conn.close()


def _cmd_revoke(args) -> int:
    memory_dir = Path(args.memory_dir)
    entries, err = _store_lines(memory_dir)
    if err:
        print(f"refused: {err}")
        return 1
    try:
        fact_key = _find_fact(entries, args.digest)
    except ValueError as exc:
        print(f"refused: {exc}; re-run list. Nothing was written")
        return 1
    extra_subject = args.subject
    if extra_subject is not None and not _SAFE_SUBJECT_RE.match(extra_subject):
        print(f"refused: subject {extra_subject!r} is not a stable key; nothing was written")
        return 1
    conn, conn_err = _ledger_conn(memory_dir)
    if conn is None:
        print(f"refused: cannot open scope ledger ({conn_err})")
        return 1
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT fact_digest, subject, audience FROM note_scope"
            ).fetchall()
            matched = _matching_rows(fact_key, rows)
            revoke_subjects = [""]
            seen = {""}
            for subject, audience in matched:
                if subject in seen:
                    continue
                if audience == "public" or (
                        audience.startswith("id:") and audience[3:].isdigit()):
                    revoke_subjects.append(subject)
                    seen.add(subject)
            if extra_subject and extra_subject not in seen:
                revoke_subjects.append(extra_subject)
            created = datetime.now(timezone.utc).isoformat()
            for subject in revoke_subjects:
                conn.execute(
                    "INSERT OR IGNORE INTO note_scope (row_key, fact_digest, subject,"
                    " author, audience, source_event, turn_key, day, created_utc)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (_review_row_key(args.reviewed_by, subject, "revoked", fact_key),
                     _visibility_digest(subject, fact_key), subject,
                     args.reviewed_by, "revoked", f"review:{args.reviewed_by}",
                     "review", "", created))
            conn.execute("COMMIT")
        except Exception as exc:  # noqa: BLE001 — operator-facing message
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            print(f"refused: ledger transaction failed ({type(exc).__name__}); "
                  "revocation not confirmed — inspect the ledger before retrying")
            return 1
        print("revoked: fact withdrawn from all recall visibility "
              "(original store line untouched)")
        return 0
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="optmem_review",
        description="Operator-only review/import of legacy OptMem memories into "
                    "the audience-scoped recall ledger. Never touches a store "
                    "without an explicit --memory-dir; writes digests only.")
    parser.add_argument("--memory-dir", required=True,
                        help="the profile's OptMem memory directory (explicit; "
                             "no default, never inferred)")
    sub = parser.add_subparsers(dest="command", required=True)
    p_list = sub.add_parser("list", help="list legacy entries by digest + preview")
    p_list.add_argument("--limit", type=int, default=0)
    p_list.add_argument("--include-reviewed", action="store_true")
    p_list.set_defaults(func=_cmd_list)
    p_show = sub.add_parser("show", help="show one legacy line in full (operator terminal)")
    p_show.add_argument("digest", help="exact 16-hex digest token from list")
    p_show.set_defaults(func=_cmd_show)
    p_approve = sub.add_parser(
        "approve", help="record a scoped successor for one legacy fact")
    p_approve.add_argument("digest", help="exact 16-hex digest token from list")
    p_approve.add_argument("--subject",
                           help="verified id:<digits> for private approval; "
                                "a handle is permitted only with --audience public")
    p_approve.add_argument("--audience", choices=["subject", "public"], default="subject",
                           help="subject-only (default) or reviewed-public")
    p_approve.add_argument("--reviewed-by", default="operator")
    p_approve.set_defaults(func=_cmd_approve)
    p_revoke = sub.add_parser("revoke", help="withdraw an approved fact from all recall")
    p_revoke.add_argument("digest", help="exact 16-hex digest token from list")
    p_revoke.add_argument("--subject")
    p_revoke.add_argument("--reviewed-by", default="operator")
    p_revoke.set_defaults(func=_cmd_revoke)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
