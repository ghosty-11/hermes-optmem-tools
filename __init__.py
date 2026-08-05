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
def _handle_wake(args: dict, **_: Any) -> str:
    cmd = ["wake"]
    lines = _cfg().get("wake_lines")
    if lines:
        try:
            cmd.append(str(int(lines)))
        except (TypeError, ValueError):
            pass
    return _run(cmd)


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
