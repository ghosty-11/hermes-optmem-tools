# hermes-optmem-tools

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
| `optmem_nap` | Perform pending compressions when a note asks for one |

The plugin runs the binary itself with a **fixed argv** and the profile's own
`MEMORY_DIR`. The model supplies only a note string or a search pattern — never a command,
flag, or path. No shell is involved (`shell=False`, explicit argv).

## Safety properties

- **No shell.** argv is built in code; nothing the model writes reaches a command line.
- **No cross-profile reads.** `MEMORY_DIR` comes from config, never from the model, so one
  profile cannot ask to read another's memories. Under a multiplexed gateway the config is
  resolved per profile automatically.
- **Minimal env, fixed cwd, hard timeout** on every call.
- **Notes are sanitized**: whitespace collapsed to one line (OptMem's unit is one line — a
  pasted block would corrupt the append-only log) and capped at 280 characters.
- **Patterns are validated**: length-capped and compiled before use; a bad pattern returns
  an error to the model instead of raising.
- **Invisible when unconfigured.** A `check_fn` hides all four tools from profiles with no
  `optmem.memory_dir`, so they cost nothing — not even schema tokens.

## Install

```bash
cp -r . "$HERMES_HOME/plugins/optmem-tools"
hermes plugins enable optmem-tools
sudo systemctl restart hermes-gateway     # plugins load at startup
```

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
  wake_lines: 48                                      # optional, passed to `wake`
```

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

`skill/SKILL.md` is the fix: worked examples of when to call, the `@handle: fact`
format, and what must never be stored. Skills are injected as procedural guidance
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
You have permanent memory through your tools: `optmem_wake` at the start of a session,
`optmem_note` when someone tells you something (written `@handle: fact`), `optmem_recall`
before replying to someone you know. Calling the tool is the only thing that remembers.
Your `optmem` skill has the details.
```

Two settings matter alongside it: `agent.max_turns` must leave room for a tool call
plus a reply (4 is too tight — we use 15), and the shorter the surrounding prompt,
the more reliably a small model reaches for a tool at all.

## Notes

- One agent = one `MEMORY_DIR` = one identity. OptMem's own README is explicit that
  subagents must never write memories; keep that rule in your prompts.
- OptMem compresses notes *with the model*, so summary quality follows whichever model the
  profile runs. On a small local model, consider routing compression elsewhere.
- Companion plugin: [hermes-discord-ambient](https://github.com/ghosty-11/hermes-discord-ambient)
  — ambient presence, reactions, bot-loop breaker and slash-command policy for a Discord
  persona bot. The two were built for the same deployment: a public community chatbot that
  should feel present and remember people, while holding no dangerous capability at all.

## License

MIT
