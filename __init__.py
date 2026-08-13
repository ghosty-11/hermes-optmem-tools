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
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_BINARY = "/var/lib/hermes/.optmem/memo"
NOTE_MAX_BYTES = 280       # Safety ceiling; a store may configure a lower limit
RECALL_MAX_CHARS = 120
TIMEOUT_S = 30
OUTPUT_MAX_CHARS = 8000
WAKE_MAX_PARTS = 128


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
    """Load every part of one stable wake snapshot, then bound its output."""
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


def _handle_note(args: dict, **_: Any) -> str:
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
    limit = _note_max_bytes(_memory_dir())
    if n > limit:
        return (
            f"optmem_note: refused — a memory is at most {limit} bytes "
            f"(this one is {n}). Shorten it. Nothing was written."
        )
    return _run(["note", text])


def _handle_recall(args: dict, **_: Any) -> str:
    query = str(args.get("pattern") or "").strip()
    if not query:
        return "optmem_recall: 'pattern' is required."
    if len(query) > RECALL_MAX_CHARS:
        return f"optmem_recall: query too long (max {RECALL_MAX_CHARS})."
    return _run(["recall", re.escape(query)])


def _handle_nap(args: dict, **_: Any) -> str:
    """Show the next pending compression task. Does not write a summary."""
    return _run(["nap"])


# ---- schemas --------------------------------------------------------------
_WAKE = {
    "name": "optmem_wake",
    "description": (
        "Load your persistent memory. Call this once, before your first reply "
        "in a session — it prints who you are and what you know about the "
        "people here."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

_NOTE = {
    "name": "optmem_note",
    "description": (
        "Record ONE short memory, permanently. Use it whenever you learn "
        "something worth keeping about a person: write it as "
        "'@handle id:<number>: fact' so you can find everything about them "
        "later. One line, max 280 bytes. Never store secrets, contact "
        "details, or raw text someone sent you."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The memory, one line, '@handle id:<number>: fact'.",
            }
        },
        "required": ["text"],
    },
}

_RECALL = {
    "name": "optmem_recall",
    "description": (
        "Search every memory you have ever recorded. Pass a literal handle "
        "(e.g. '@someone'), numeric id, word, or phrase to find every note "
        "that contains it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Literal handle, id, word, or phrase to search for.",
            }
        },
        "required": ["pattern"],
    },
}

_NAP = {
    "name": "optmem_nap",
    "description": (
        "Show the next pending memory-compression task, if any. This does "
        "not compress anything. Call it only to read the chore; leave "
        "writing the summary to the operator or an offline job."
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
