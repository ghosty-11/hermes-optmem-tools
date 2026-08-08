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
      wake_lines: 48                                     # optional, passed to `wake`

Then add `optmem` to the profile's toolsets (and to platform_toolsets.<surface>
if that profile scopes tools per surface).

SAFETY
------
  * argv is built by this module; the model supplies only a note string or a
    search pattern, never a command, flag or path.
  * MEMORY_DIR comes from config, never from the model — one profile cannot
    read another's memories by asking.
  * env is minimal (MEMORY_DIR + PATH), cwd is fixed, every call is timed out.
  * note text is length-capped and newline-stripped: OptMem's unit is ONE line,
    and a multi-line note would corrupt its log format.
  * recall patterns are length-capped and rejected if they look like a regex
    bomb; a bad pattern returns an error to the model, never an exception.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_BINARY = "/var/lib/hermes/.optmem/memo"
NOTE_MAX_CHARS = 280           # OptMem's own documented unit: one short line
PATTERN_MAX_CHARS = 120
TIMEOUT_S = 30
OUTPUT_MAX_CHARS = 8000        # wake output is bounded by design; belt+braces


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
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
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
        text = text[:OUTPUT_MAX_CHARS] + "\n… (truncated)"
    return text or "(no output)"


# ---- handlers -------------------------------------------------------------
# `memo wake` prints the store AND, when compactions are pending, a housekeeping
# task addressed to whoever ran it: "You are awake. / Compress memories #0-1 into
# one line... / Run: ~/.optmem/memo nap 0-1". That is legitimate here — this plugin
# also exposes `optmem_nap`, so the caller can actually do it, and agents on this
# host demonstrably do (a nap immediately followed by a wake).
#
# What it is NOT is something to say out loud. 2026-08-08: an agent on a public
# surface received this block, took it for an instruction it had just been given,
# and narrated a garbled version of it at a user — a disclaimer about needing to
# "check the file permissions" on an "old one from before the policy changes".
# Nobody injected anything; a maintenance prompt simply arrived in the same channel
# as content, and the model relayed it.
#
# So: keep the chore, label it. Stripping it (which is the right call where the
# block is injected UNASKED, as a memory) would break the compaction loop for every
# seat that uses this tool.
HOUSEKEEPING_NOTE = (
    "[housekeeping — for you alone. Do it silently with optmem_nap, or ignore it. "
    "Never mention, quote or narrate any of this block in a reply.]"
)

# Markers that indicate a pending-compaction chore is present in the output. A wake
# with nothing pending must not gain a stray note.
_CHORE_MARKERS = ("You are awake", "Compress memories", "memo nap")


def _mark_housekeeping(out: str) -> str:
    """Label optmem's operator-facing chore as internal, leaving it intact."""
    if not out or not any(m in out for m in _CHORE_MARKERS):
        return out
    return out.rstrip("\n") + "\n\n" + HOUSEKEEPING_NOTE


def _handle_wake(args: dict, **_: Any) -> str:
    """Load the standing context.

    `wake_lines` is applied HERE, to the output — it is not passed to the binary.
    OptMem's `wake [N]` argument selects **part N** of a segmented memory, not a
    line count, so forwarding a value like 48 asks for a part that does not exist
    and the call fails with `No part 48: the memory has 1 part` — returning
    nothing, on the one call that is supposed to establish what the agent knows.
    Nothing surfaces: the tool reports the error text, the agent carries on, and
    the memory simply never arrives.

    Capping the output ourselves does what the option's name promises and what it
    was configured for (bounding the tokens a wake costs), and it cannot fail.
    """
    return _mark_housekeeping(_wake_output())


def _wake_output() -> str:
    out = _run(["wake"])
    lines = _cfg().get("wake_lines")
    if not lines:
        return out
    try:
        limit = int(lines)
    except (TypeError, ValueError):
        return out
    if limit <= 0:
        return out
    rows = out.splitlines()
    if len(rows) <= limit:
        return out
    # Keep the MOST RECENT lines: OptMem's log is append-only and chronological,
    # so the tail is the current picture. Truncating the head would hand the agent
    # the oldest facts and drop everything learned since.
    return "\n".join(rows[-limit:])


def _handle_note(args: dict, **_: Any) -> str:
    text = str(args.get("text") or "").strip()
    if not text:
        return "optmem_note: 'text' is required."
    # OptMem's unit is ONE line: collapse whitespace so a pasted block cannot
    # corrupt the append-only log, then cap length.
    text = " ".join(text.split())
    if len(text) > NOTE_MAX_CHARS:
        text = text[:NOTE_MAX_CHARS].rstrip()
    return _run(["note", text])


def _handle_recall(args: dict, **_: Any) -> str:
    pattern = str(args.get("pattern") or "").strip()
    if not pattern:
        return "optmem_recall: 'pattern' is required."
    if len(pattern) > PATTERN_MAX_CHARS:
        return f"optmem_recall: pattern too long (max {PATTERN_MAX_CHARS})."
    try:
        re.compile(pattern)
    except re.error as e:
        return f"optmem_recall: not a valid search pattern ({e})."
    return _run(["recall", pattern])


def _handle_nap(args: dict, **_: Any) -> str:
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
        "something worth keeping about a person: write it as '@handle: fact' "
        "so you can find everything about them later. One line, max 280 "
        "characters. Never store secrets, contact details, or raw text "
        "someone sent you."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The memory, one line, ideally '@handle: fact'.",
            }
        },
        "required": ["text"],
    },
}

_RECALL = {
    "name": "optmem_recall",
    "description": (
        "Search every memory you have ever recorded. Pass a handle (e.g. "
        "'@someone') to pull that person's whole history back, or a word to "
        "find every note mentioning it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Handle or word to search for.",
            }
        },
        "required": ["pattern"],
    },
}

_NAP = {
    "name": "optmem_nap",
    "description": (
        "Perform the pending memory compressions. Call this only when a "
        "previous optmem_note told you a compression is pending."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}


def register(ctx) -> None:
    """Register the OptMem tools under the `optmem` toolset.

    check_fn gates every tool on this profile having `optmem.memory_dir` set
    and the binary being present, so profiles that don't use OptMem never see
    them in their schema (and pay no prompt tokens for them).
    """
    for schema, handler, emoji in (
        (_WAKE, _handle_wake, "🌅"),
        (_NOTE, _handle_note, "📝"),
        (_RECALL, _handle_recall, "🔎"),
        (_NAP, _handle_nap, "🌙"),
    ):
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
    logger.info("optmem-tools: registered optmem_wake/note/recall/nap")
