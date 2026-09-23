# hermes-optmem-tools

[![Support this work](https://img.shields.io/badge/Support-EVM-6f42c1?logo=ethereum&logoColor=white)](#support-development)

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that gives an agent
[OptMem](https://github.com/VictorTaelin/OptMem) — permanent, append-only memory — as
**registered tools**, so it never needs shell access to remember anything.

## The problem

OptMem is a CLI: `memo wake`, `memo note "..."`, `memo recall <pattern>`. The obvious way
to give an agent OptMem is to write those commands into its instructions and let it run
them with the terminal tool.

That works fine for a private assistant. It is a non-starter for an agent facing a
**public room**, where the terminal is precisely the capability a prompt-injected bot must
not have — and "you may run only this one binary" is not a promise a shell tool can keep.

We found the failure mode the boring way: a community chatbot had OptMem instructions in
its `AGENTS.md` and `terminal` in its `disabled_toolsets`. It looked configured. It had
written **zero** memories in its entire life — `LOG.txt` was 0 lines — because an agent
told to use a tool it does not have simply never does the thing, and nothing logs it.

> An agent instructed to use a capability it cannot reach fails **silently**. Config keys
> being present is not evidence that a capability works.

## What it does

Registers four tools under an `optmem` toolset:

| Tool | Purpose |
|---|---|
| `optmem_wake` | Load memory at session start — who you are, who these people are |
| `optmem_note` | Record ONE short line, permanently |
| `optmem_recall` | Search every memory ever recorded (e.g. a handle) |
| `optmem_nap` | Show a pending compression task — it does not write one |

The plugin runs the binary itself with a **fixed argv** and the profile's own
`MEMORY_DIR`. The model supplies only note or literal search text — never a command,
flag, path, or regular expression. No shell is involved (`shell=False`, explicit argv).

## Safety properties

- **No shell.** argv is built in code; nothing the model writes reaches a command line.
- **No cross-profile reads.** `MEMORY_DIR` comes from config, never from the model, so one
  profile cannot ask to read another's memories. Under a multiplexed gateway the config is
  resolved per profile automatically.
- **Minimal env, fixed cwd, hard timeout** on every call.
- **Notes are sanitized**: whitespace collapsed to one line (OptMem's unit is one line — a
  pasted block would corrupt the append-only log) and refused if they exceed either the
  store's configured `ENTRY_CHARS` limit or 280 bytes.
- **Recall is literal**: search text is length-capped and regex-escaped before OptMem sees
  it, so metacharacters cannot create expensive expressions.
- **Wake is complete before it is capped**: every paginated part is fetched against the
  same snapshot, then only the most recent configured lines are returned.
- **Invisible when unconfigured.** A `check_fn` hides all four tools from profiles with no
  `optmem.memory_dir`, so they cost nothing — not even schema tokens.

## Install

```bash
cp -r . "$HERMES_HOME/plugins/optmem-tools"
hermes plugins enable optmem-tools
sudo systemctl restart hermes-gateway     # plugins load at startup
```

The gateway loads root plugins. A named profile that also runs in a standalone
process needs the same plugin under its own home. Use one canonical symlink rather
than a copy that can drift:

```bash
PROFILE_HOME="$HERMES_HOME/profiles/PROFILE_NAME"
mkdir -p "$PROFILE_HOME/plugins"
test ! -e "$PROFILE_HOME/plugins/optmem-tools"
ln -s "$HERMES_HOME/plugins/optmem-tools" "$PROFILE_HOME/plugins/optmem-tools"
hermes -p PROFILE_NAME plugins enable optmem-tools
```

Repeat that block for each standalone profile that uses OptMem. Profiles without an
`optmem.memory_dir` remain unaffected because the tool availability check hides the
schemas.

`plugin.yaml` declares `kind: standalone` — that is the kind for a plugin that
registers tools. There is **no** `kind: tools`; an invalid kind is demoted to
standalone with only a WARNING in the gateway log, and `register()` never runs, so
the tools silently never appear. If the tools don't show up, grep the gateway log
for your plugin name before debugging anything else.

## Config

Per profile, in that profile's `config.yaml`:

```yaml
optmem:
  memory_dir: /var/lib/hermes/<agent>-memory/memory   # required — also enables the tools
  binary: /var/lib/hermes/.optmem/memo                # optional (this is the default)
  wake_lines: 48                                      # optional; caps the wake OUTPUT (see note)
```

### `wake_lines` caps the output, and deliberately does not reach the binary

OptMem's `wake [N]` argument selects **part N** of a segmented memory — it is not a line
count. Passing a value like `48` therefore asks for a part that does not exist and the call
fails with `No part 48: the memory has 1 part`, returning nothing on the one call that is
supposed to establish what the agent knows. Worse, nothing about it looks broken: the tool
reports the error, the agent carries on, and the memory simply never arrives.

The plugin starts with a bare `wake`, follows every continuation part against the
snapshot id returned by OptMem, and removes the transport headers. Only after the
complete document reaches the plugin does it apply `wake_lines`, keeping the most
recent lines. A separate 8,000-character tool-output ceiling also keeps the tail, so
neither pagination nor output bounding can silently discard the newest memories.

### Note size follows the store

OptMem stores may lower `ENTRY_CHARS` in their own `config` file. The plugin reads
that value before a write and enforces the lower of it and the plugin's 280-byte
safety ceiling. This prevents a note from passing plugin validation only to fail in
the backing store.

### Disable the built-in memory first — do not run both

Hermes ships its own `memory` toolset. If you leave it enabled alongside these tools the
agent has **two** memory systems and will quietly pick one per turn — ours picked the
built-in one, wrote to `profiles/<name>/memories/MEMORY.md`, and told the operator it had
saved. We checked OptMem's log, found it empty, and accused the agent of fabricating a
memory it had genuinely written. It had not lied; we had looked in the wrong store.

So: remove `memory` from the profile's `toolsets` / `platform_toolsets` (and set
`memory.memory_enabled: false` if nothing else needs it), leaving `optmem` as the single
memory system. Migrate anything already written:

```bash
grep -v '^§' "$HERMES_HOME/profiles/<name>/memories/MEMORY.md" | while read -r line; do
  MEMORY_DIR=<memory_dir> /path/to/memo note "$line"
done
```

> If an agent claims it saved something and the store looks empty, check **every** store
> before disbelieving it. An agent with two memory tools will use the one you didn't check.

Then add `optmem` to the profile's toolsets. If the profile scopes tools per surface, add
it to `platform_toolsets.<surface>` too:

```yaml
platform_toolsets:
  discord: [optmem, messaging, session_search, vision, web]
```

**This must be a real YAML list.** `hermes config set` writes scalars and strings only, and
a string here is silently ignored — as is `hermes tools enable`, which only knows built-in
toolsets, not plugin-registered ones. Edit the profile's `config.yaml` for this one key.

### Install the skill — do not skip this

Tools alone are not enough. A capable model will use them from a one-line hint; a
small local model (we run gpt-oss:20b) will happily reply *"I'll remember that!"*
and never emit the call — memory that silently never happens, which is worse than
no memory at all, because it looks like it worked.

`skill/SKILL.md` is the fix: worked examples of when to call, the
`@handle id:<number>: fact` format, and what must never be stored. Skills are injected
right where tool selection happens, and example-shaped instructions land far better
on small models than prose rules buried in a persona file.

```bash
mkdir -p "$HERMES_HOME/profiles/<name>/skills/memory/optmem"
cp skill/SKILL.md "$HERMES_HOME/profiles/<name>/skills/memory/optmem/SKILL.md"
hermes -p <name> skills list | grep optmem     # should show: enabled
```

Then **delete any older memory prose** from the profile's `AGENTS.md`/`SOUL.md` —
especially instructions to run `memo` on a command line, which it will keep trying
with a shell it doesn't have. Leave a short pointer instead; duplication between the
skill and the persona file just costs context and dilutes both:

```markdown
You have permanent memory through your tools: `optmem_wake` at the start of a session
if that tool is available, `optmem_note` when someone tells you something (written
`@handle id:<number>: fact`), `optmem_recall` before replying to someone you know.
Calling the tool is the only thing that remembers. Your `optmem` skill has the details.
```

Two settings matter alongside it: `agent.max_turns` must leave room for a tool call
plus a reply (4 is too tight — we use 15), and the shorter the surrounding prompt,
the more reliably a small model reaches for a tool at all.

## Key memories on stable identity, not display names

Chat platforms hand the model a *display name*. People change those, and two
people can share one — so notes keyed on a display name eventually merge two
strangers or lose someone after a rename. Worse, a single-user framing ("the
user likes X") is meaningless in a room with fifty people.

The skill writes `@handle id:<number>: fact`. On Discord, the
[companion plugin](https://github.com/ghosty-11/hermes-discord-ambient)
places the verified account handle and numeric id in a tagged, request-only
envelope. The human message remains unchanged in the transcript. A shared
profile refuses notes without a subject key or notes about anyone other
than the verified speaker; a refused note neither writes nor spends a
budget slot.

Do not copy the sample `optmem_wake` instruction into a speaker-scoped companion
profile. That profile keeps wake and nap as internal housekeeping and restricts
recall to the verified speaker.

The backend store and scope ledger cannot commit as one transaction. If the
backend writes a line but the ledger commit fails, the note tool reports an
unconfirmed save. The line remains quarantined from scoped recall, and a retry
may append the same line again without using a confirmed ledger budget slot.
If a save is unconfirmed, inspect the store and ledger before retrying; do not
claim that the companion remembered the note.

## Reviewing legacy memories (operator-only)

On a `speaker_scoped` (shared/public) profile, recall is audience-scoped: a store
line reaches a speaker only when the per-profile scope ledger
(``recall-scope.db`` beside the store) proves it is about them with an audience
that includes them. Companion-authored notes carry subject and audience
provenance automatically. Operator-authored notes record provenance but
stay in the review queue until approved for a numeric speaker or public
audience. Legacy lines without review stay in the store but cannot reach
scoped recall.

From the repository root, replace `/path/to/memory` with the profile's
OptMem store path. Replace `DIGEST_TOKEN` with the exact 16-hex token from `list`.

List unreviewed lines with truncated previews:

```bash
python3 scripts/optmem_review.py \
  --memory-dir /path/to/memory list
```

Show one line in full on your operator terminal:

```bash
python3 scripts/optmem_review.py \
  --memory-dir /path/to/memory show DIGEST_TOKEN
```

For a private approval, verify the numeric subject before recording it:

```bash
python3 scripts/optmem_review.py \
  --memory-dir /path/to/memory approve DIGEST_TOKEN --subject id:1234567890
```

If every speaker may see the reviewed fact, approve a public audience:

```bash
python3 scripts/optmem_review.py \
  --memory-dir /path/to/memory approve DIGEST_TOKEN --audience public
```

Withdraw a fact from all recall:

```bash
python3 scripts/optmem_review.py \
  --memory-dir /path/to/memory revoke DIGEST_TOKEN
```

Rules the script enforces:

- ``--memory-dir`` is **required** — it never guesses or defaults to a live store.
- If a line has only a handle, private approval requires a verified numeric
  `--subject id:<digits>`. The script refuses an invisible handle-only
  private approval instead of recommending a broader public audience.
- Approvals record **digests only** in the ledger (author, audience, review
  receipt); the OptMem store remains the single copy of every fact.
- The original store line is never edited or deleted; review adds a scoped
  successor, and the memo store stays append-only.
- Stale refusals: approving a digest that matches no *current* store line (the
  text changed underneath you, e.g. after a compression) writes nothing.
- Duplicate and revoked refusals: the same approval cannot be recorded twice,
  and a revoked fact cannot be re-approved by the script.
- `revoke DIGEST_TOKEN` writes a digest-only global tombstone and withdraws
  every currently approved subject for that store line in one transaction.
  A revoked fact stays unavailable after a handle changes or a later
  approval names another id. On a scoped companion turn, the note tool
  refuses to save the exact revoked fact.
- Nothing here is model-callable: the CLI is not registered as a tool and
  shares no code path with the gateway.

Migration gating: run ``list`` before activating a speaker-scoped profile on a
store with history, approve the facts that should survive, and expect recall to
stay empty for everything else — thinner recall is the honest state, not a bug.

## Notes

- One agent = one `MEMORY_DIR` = one identity. OptMem's own README is explicit that
  subagents must never write memories; keep that rule in your prompts.
- OptMem compresses notes *with the model*, so summary quality follows whichever model the
  profile runs. On a small local model, consider routing compression elsewhere.
- Companion plugin: [hermes-discord-ambient](https://github.com/ghosty-11/hermes-discord-ambient)
  — ambient presence, reactions, bot-loop breaker and slash-command policy for a Discord
  persona bot. The two were built for the same deployment: a public community chatbot that
  should feel present and remember people, while holding no dangerous capability at all.

## Support development

If this plugin saves you time, you find it useful, or you want to help me cover the token costs of continued development, you can support the work with an EVM donation:

```text
0x9600c9bc632175941608a1b551cb0f018f0f40b4
```

Networks: Ethereum, Base, Polygon, and other EVM-compatible networks. Verify the address and selected network before sending; unsupported assets or networks may be unrecoverable.

## License

MIT

<sub>Made with love, with help from AI agents.</sub>
