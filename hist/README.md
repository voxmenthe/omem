# hist

`hist` indexes every Codex, Claude Code, Command Code, OpenCode, Devin, and
Antigravity CLI session on this machine into one SQLite store and lets agents navigate it with short, bounded commands. Sessions
in one repository share a timeline across hosts and accounts; workers and
subagents get their own linked timelines. It is read-only history: agents never
write it, and it never changes OMem.

## Run it

```bash
uv tool install .                           # from this checkout; installs `hist` beside `memory`
mkdir -p ~/.hist && cp hist/config.example.toml ~/.hist/config.toml   # default locations
hist sync --all && hist timelines
```

Normal read commands sync for up to a second before reading; `hist sync`
explicitly refreshes the index. When a read's second runs out, its first line
says the index may be behind, and it starts one `hist sync` in the background
to finish. That sync stops after a minute, does nothing if another sync is
running, and writes its result to `~/.hist/catch-up.log`. Startup hooks use the
existing index without syncing. `hist` with no arguments lists the commands.

## Ways to use

[USAGE.md](USAGE.md) covers each of these with examples:

- Let the view orient sessions in the repositories listed in `view.inject`.
- Continue in one tool or account what another did in the same repository.
- Ask for an earlier decision or exact detail instead of re-deriving it.
- Search everything: `hist find "<error text>" --all`.
- Pass a worker references such as `myrepo:12345` instead of pasted context.
- Read a worker's transcript, not just its report: `hist sessions --workers`,
  then `hist read <alias> --tail 20` for its last steps.
- Replay any session in order with `hist read`, or read its end with
  `--tail N`; `--kind user,agent` keeps only the conversation.
- Trace a memory or a commit to the conversation behind it with `hist find`.
- Widen `view.inject` as the view proves useful, and summarize only small
  timelines.

## Configure

`~/.hist/config.toml` (or `$HIST_HOME/config.toml`) is the only source of
transcript locations; `hist` reads no host environment variables to find them.
The parser rejects unknown keys and overlapping roots. Keys:

- `[[sources]]`: one per host installation or account, with `adapter`
  (`codex`, `claude`, `commandcode`, `opencode`, `devin`, or `antigravity`), a
  `label` shown in message headers, and `roots`.
- `[discovery]`: `watch` patterns are scanned only to report unclaimed session
  stores in `hist status`; `exclude` lists paths or repositories never indexed.
- `[view]`: `bytes` caps the injected view; `worker` is what a worker sees at
  start (`header`, `view`, or `none`); `inject` lists the repositories whose
  sessions are shown the view by the hook, or `"all"`.
- `[summaries]`: `command` is a summarizer speaking `hist-summarize/1` on
  stdin/stdout; `timelines` opts timelines in; `max_attempts_per_day` caps calls.
  Without a command, views use outlines, which cost nothing.

Sources written before the store was created wait for `hist sync --all`, which
`hist status` reports. Once a backfill finishes, nothing waits: older
transcripts found later are indexed by ordinary syncs. Measured here, every
transcript on the machine (32,483 sources, 35 GB, 2.75 million messages) took
30 minutes and an 11.2 GB store.

## Other hosts

Command Code, OpenCode, Devin, and the Antigravity CLI have no start hook, so
their sessions are indexed and searchable but are never shown the view or the
worker header. A subagent links to the session that started it on the same
host; a worker started from another host's shell is linked only when its
launcher writes a [marker line](#link-workers-by-a-marker-line). Each
adapter's docstring in `hist/adapters/` describes its store.

- Command Code (`~/.commandcode/projects`): a session is interactive unless its
  `<session>.meta.json` marks a headless `print` run, and unclassified without
  that file. Skill bodies and reminders the host adds to a prompt are not
  stored.
- OpenCode (`~/.local/share/opencode`, `opencode.db`): a turn is indexed once
  OpenCode ends it, so the turn in progress does not appear yet. A session the
  TUI has shown is interactive. Sessions migrated from OpenCode 1.x are read
  from the migrated rows and count as interactive with a second prompt.
- Devin (`~/.local/share/devin/cli`, `sessions.db`): Devin records nothing that
  tells a typed session from a headless one, so every session is a worker. Only
  the main conversation is indexed; subagent, sidekick, and summarizer chains
  are not, nor are `hist summarize devin`'s own runs. Devin occasionally
  rewrites a stored message (3 of 1,493 sessions here), which stops that
  session like any rewritten transcript.
- Antigravity CLI (`~/.gemini/antigravity-cli`): while the CLI holds a
  conversation open, reading stops at the first step still running; once it
  closes, every step is read. A conversation is interactive when its id is in
  the CLI's prompt history or it has a second prompt, and a worker when started
  with `-p`. Results of the older, specific tool steps (view file, run command,
  ...) are counted in `hist status`, not indexed; the calls are. The
  Antigravity IDE's store, `~/.gemini/antigravity`, is not read.

### Link workers by a marker line

A launcher that starts a worker from an agent session can name that session at
the end of the worker's task, on a line of its own:

```text
[hist-parent claude:5e2a9f0c-1b7d-4c3e-9a8f-0d6e2b4c7a91]
```

The value names the parent by its adapter, such as `claude` or `codex`, and
the session id its host gave it. When sync indexes the first prompt of a worker
or subagent, on any host, it records the last marker there as the parent,
unless that marker names the worker itself or an adapter no source uses. The
link resolves once the parent is indexed. A link the host or a hook recorded
first stands, and a marker anywhere later in a session does nothing. Because
the link rests on the launcher's word, the worker's start note in the parent's
timeline says `started (marker)`. Workers started before their launcher wrote
markers stay unlinked.

In a launcher that takes the task as `$task`, from a Codex or Claude Code
shell:

```bash
if [[ -n "${CLAUDE_CODE_SESSION_ID:-}" && -z "${CODEX_THREAD_ID:-}" ]]; then
  task+=$'\n\n'"[hist-parent claude:${CLAUDE_CODE_SESSION_ID}]"
elif [[ -n "${CODEX_THREAD_ID:-}" && -z "${CLAUDE_CODE_SESSION_ID:-}" ]]; then
  task+=$'\n\n'"[hist-parent codex:${CODEX_THREAD_ID}]"
fi
```

With both variables set, or neither, it writes nothing, as the hook records
nothing for conflicting variables. Hosts other than Codex and Claude Code set
neither variable, so a worker that one of their workers starts links to the
nearest Codex or Claude Code session above it.

## Summaries (optional)

`hist build` writes summaries for the timelines in `summaries.timelines`
through `summaries.command`. `hist summarize BACKEND` is a ready-made command
over the agent CLI you already use, or a model API. Each backend has a default
model; `--model` picks another.

| Backend | Calls | Default model | Needs |
|---|---|---|---|
| `claude` | Claude Code, `claude -p` | `claude-haiku-5-5` | Claude Code, logged in |
| `codex` | Codex, `codex exec` | `gpt-6-luna`, medium effort | Codex, logged in |
| `devin` | Devin, `devin -p` | `swe-2-max` | Devin, logged in |
| `opencode` | OpenCode, `opencode run` | `opencode/muse-spark-1.3-contributor-free` | OpenCode |
| `anthropic` | the Anthropic Messages API | `claude-haiku-5-5` | `ANTHROPIC_API_KEY` |
| `openai` | the OpenAI Chat Completions API, or any compatible one | `gpt-6-luna`, medium effort | `OPENAI_API_KEY` |

`codex` and `openai` also take `--effort`, the reasoning effort the model
accepts, such as `low`, `medium` or `high`. Without it, the default model runs
at medium and a model named with `--model` at its own default, so no effort
setting reaches a model that may not accept one.

```toml
[summaries]
command = ["hist", "summarize", "claude"]
# command = ["hist", "summarize", "codex", "--effort", "low"]
# command = ["hist", "summarize", "anthropic"]
# command = ["hist", "summarize", "openai", "--model", "gpt-6-luna", "--effort", "medium"]
# command = ["hist", "summarize", "openai", "--base-url", "https://openrouter.ai/api/v1",
#            "--key-env", "OPENROUTER_API_KEY", "--model", "anthropic/claude-haiku-5.5"]
# command = ["hist", "summarize", "openai", "--base-url", "http://localhost:11434/v1",
#            "--model", "<local model>"]
timelines = ["omem"]
```

An API backend sends one request that offers no tools. `--base-url` points it
at another service: the root for `anthropic`, the URL ending in `/v1` for
`openai`, so OpenRouter or a local server works through `openai`. Keys stay in
the environment, never in the configuration. A default key goes only to its
own service: with `--base-url`, `--key-env` names the variable holding that
service's key, and without it no key is sent, as a local server needs.

Each CLI call runs headless in an empty scratch directory with no tools and
none of your settings, hooks, plugins or MCP servers, leaves no session behind,
and fails unless the agent made exactly one model request and called no tool.
Whatever the CLI always sends still costs tokens. For one message, Claude Code
sent about 1,300 input tokens and Codex about 8,800 with `gpt-6-luna`. About
3,000 of the Codex tokens were an 11 KB global `~/.codex/AGENTS.md`: Codex
sends that file whole and has no setting to leave it out. Devin adds about
6,500 tokens of its own prompt. The same message over an API cost about 500 to
750.

- Claude Code loads no setting source, and `hist`'s system prompt replaces
  its own, so neither `CLAUDE.md` nor hooks are loaded. The run is not saved.
- Codex ignores your `config.toml` and rules and runs with every tool feature
  off, a read-only sandbox, and `--ephemeral`. A renamed setting or feature
  stops the call rather than being skipped.
- Devin's tools are disabled or denied, and the run's Devin session is deleted
  afterwards.
- OpenCode runs a private server (`--standalone`) on a scratch config. Zen's
  free tier refuses requests that list no OpenCode tools, so the tools stay
  listed with every call needing an approval that a headless run cannot give.

`claude-haiku-5-5` is the full model id on purpose: Claude Code lets
`ANTHROPIC_DEFAULT_HAIKU_MODEL` remap the `haiku` alias to another model.

### Your own summarizer

Any command works as `summaries.command` if it reads one JSON request on
stdin and writes one JSON answer on stdout:

```json
{"protocol": "hist-summarize/1", "deadline": "2026-10-10T18:15:00Z",
 "system": "<instructions>", "context": "<nearby lines, or empty>",
 "turns": [{"role": "user", "text": "<the task>"}]}
```

```json
{"text": "<one line>", "usage": {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}}
```

`turns` alternate between `user` and `assistant`, starting and ending with
`user`. A retry adds the line that was too long and a request to shorten it.
`usage` may be `null`. On failure, exit nonzero with the reason on stderr. A
call has until `deadline`, 900 seconds; after that, the builder ends the
command's whole process group.

Measured on one 25-message timeline (38 summaries per model):

- SWE-2 Max wrote the most faithful lines and kept the user's words verbatim.
  A call averaged about 9,800 input tokens (about 6,500 of them Devin's own
  prompt) and 2,500 output tokens, mostly reasoning. Hard merges reasoned much
  longer: the top merge took 157 s and 385 s on two tries, so each call may
  take up to 900 s. Under the earlier 120 s limit, 7 of 57 calls failed.
- SWE-2 High was close, but sometimes paraphrased the user. It reasoned half
  as much, failed 3 of 61 calls under the 120 s limit, and built the tree in
  about 30 minutes against about 50 for Max.
- Muse Spark took seconds per call, but the free tier allowed about one call a
  minute (45 rate-limit stops in an hour). It sometimes pulled facts from the
  context into the line, and it reports no token usage.

## Tell your agents

Agents learn about `hist` from the history view in `view.inject` repositories,
and everywhere else only from their instructions. `hist instructions` prints a
short block for that; add it to your global instruction files,
`~/.codex/AGENTS.md` for Codex and `~/.claude/CLAUDE.md` for Claude Code, and
replace it after upgrading.

The block names the usual cues: the user points back, compaction took a
detail, an error looks familiar, a worker has finished. It also welcomes a
short probe whenever earlier work might change what the agent builds, tests,
or avoids, gives three probes that have paid off, and says how to search well.
In a review of real use, agents already reached for `hist` unprompted; what
wasted their time was technique, such as one-word searches and polling
running workers, so the block teaches technique rather than adding rules. The
view's header carries the same invitation.

## Install the hooks (optional)

Without hooks, every command still works and capture is complete. The hook
adds the view at session start, compaction, and clear in repositories listed in
`view.inject`, and records the parent of shell-launched workers. It serves the
last indexed snapshot without scanning or verifying transcripts. Recent messages
appear after a normal command such as `hist view` or an explicit `hist sync`;
transcript integrity checks still run during indexing and `hist verify`. If no
history is indexed for the repository yet, the hook prints nothing; run
`hist sync` (or `hist sync --all` for pre-install history) to populate it.
An unindexed Codex session can still be classified from its supplied transcript's
metadata record, so standalone workers receive the worker header, not the
interactive repository view; the hook does not resolve fork-parent history.

From the checkout, `uv run scripts/install_hooks.py hist` reports what it would
add to Claude Code's settings (in `CLAUDE_CONFIG_DIR`, default `~/.claude`) and
Codex's (in `CODEX_HOME`, default `~/.codex`); `--apply` writes it, keeping a
copy of each changed file. Run it once per Codex home. To merge by hand:

- Codex: merge [`integrations/codex.toml`](integrations/codex.toml) into the
  effective user `config.toml` (`codex doctor --json` shows which).
- Claude Code: merge the `hooks` entries of
  [`integrations/claude.json`](integrations/claude.json) into
  `~/.claude/settings.json` without replacing existing entries.

Either way, Codex then reports "Hooks need review" at its next start in each
Codex home, and the hooks do not run until you trust them there or in `/hooks`.

Codex workers and Claude Code subagents are linked to the session that started
them automatically. A Claude Code worker started from a Claude Code shell
cannot see its parent's session id, so the launcher you start it with must name
it. In a shell function that starts such a worker:

```bash
if [[ -n "${CLAUDE_CODE_SESSION_ID:-}" && -z "${CODEX_THREAD_ID:-}" ]]; then
  export HIST_PARENT="claude:${CLAUDE_CODE_SESSION_ID}"   # started from Claude Code
else
  unset HIST_PARENT
fi
```

Give the function a subshell body, `name() ( … )`, so the variable does not
outlive the launch. An inherited `HIST_PARENT` is ignored when
the shell's own session variable names a different session of the same host,
so that worker's own workers link to it rather than to its parent. A
[marker line](#link-workers-by-a-marker-line) in the task also links such a
worker, and is the only way to link a worker on a host without hooks.

Check a hook by hand from a repository in `view.inject`:

```bash
printf '{"session_id":"test","cwd":"%s","source":"startup"}' "$PWD" | hist hook claude SessionStart
```

It prints one JSON object with the view once the repository has history, and
nothing outside `view.inject`. Failures go to stderr and never block the host.

## Keep it healthy

- `hist status`: sources, freshness, backfill backlog, stopped sources,
  unknown record types, alarms (unclaimed stores, transcripts outside every
  source), disk.
- `hist verify [SESSION]`: re-hashes indexed transcripts to catch in-place
  rewrites that per-sync checks cannot see; exits 1 if any source stopped.
- `hist backup PATH`: a consistent copy of the store.
- `hist refresh [--dry-run]`: brings rows stored under older text rules up to
  the current ones; see below. `hist status` says when it is due.

A source stops, loudly, when its transcript no longer extends what was indexed
or breaks its adapter's expectations; the rest keep syncing.

Commits do not wait for the disk (SQLite's `synchronous = NORMAL`). A power
loss or OS crash can undo the commits since the last checkpoint, whole and in
order, and the next sync indexes those messages again from the transcripts. A
reference printed from them may then name a different message. A crash
of `hist` itself loses nothing. `hist refresh` waits for the disk on every
commit, so text it reports masked stays masked.

### Refresh stored text

The rules for stored text sometimes improve: a new secret format to redact,
or a host that saves large outputs apart from its transcript. New rows follow
the current rules. `hist refresh` brings the older rows up to them in place,
so every reference stays valid:

- a Claude Code saved-output preview becomes the whole output (head and tail,
  30,000 characters), when its file is still a regular file under that
  session's `tool-results/`;
- the current redaction rules mask what older ones missed;
- rows whose JSON escapes were indexed undecoded are reindexed (stores from
  before text rules version 2 only).

Run `hist refresh --dry-run` first: it counts what would change, per
redaction rule, with masked samples, and changes nothing. Take a
`hist backup` before the real run if you want a way back, then delete the
backup once you no longer need it: it still holds whatever the refresh
masked. A refresh commits in batches, waits out syncs, and resumes where it
stopped if interrupted. It zeroes the text SQLite frees and checkpoints the
log, but the full-text index keeps fragments of the old words, lowercased,
until it is optimized; that cleanup comes with purge.

Not implemented yet: `purge`, `restore`, `reset`, and background summary
builds (`hist build` runs synchronously). Every command, reads included, needs
write access to `~/.hist`, so `hist` fails inside a sandbox that blocks it, such
as Codex's `read-only` or `workspace-write` modes.

## Develop

```bash
uv run python -m unittest discover -s tests/hist -t .
uv run python benchmarks/hist_hook_latency.py          # hook p95 on a million-message timeline
uv run python scripts/hist_rates.py                    # weekly rates of how agents use hist
uv run python scripts/hist_replay.py                   # replay past finds and zooms: regressions, latency
```

Tests drive the `hist` command against fixture transcripts shaped like real
records (`tests/hist/fixtures.py`). A new host adds an adapter under
`hist/adapters/` and a subclass in `tests/hist/test_conformance.py`. A host
that keeps sessions in a database reads them through `hist/rows.py`, and its
subclass overrides `write`.
