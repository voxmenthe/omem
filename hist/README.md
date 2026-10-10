# hist

`hist` indexes every Codex and Claude Code session on this machine into one
SQLite store and lets agents navigate it with short, bounded commands. Sessions
in one repository share a timeline across hosts and accounts; workers and
subagents get their own linked timelines. It is read-only history: agents never
write it, and it never changes OMem.

## Run it

```bash
uv tool install .                           # from this checkout; installs `hist` beside `memory`
mkdir -p ~/.hist && cp hist/config.example.toml ~/.hist/config.toml   # default locations
hist sync --all && hist timelines
```

Every read command syncs first, within one second, so `hist sync` is needed only
for the first full index. `hist` with no arguments lists the commands.

## Ways to use

[USAGE.md](USAGE.md) covers each of these with examples:

- Let the view orient sessions in the repositories listed in `view.inject`.
- Continue in one tool or account what another did in the same repository.
- Ask for an earlier decision or exact detail instead of re-deriving it.
- Search everything: `hist find "<error text>" --all`.
- Pass a worker references such as `myrepo:12345` instead of pasted context.
- Read a worker's transcript, not just its report: `hist sessions --workers`,
  then `hist read <alias>`.
- Replay any session in order with `hist read`.
- Widen `view.inject` as the view proves useful, and summarize only small
  timelines.

## Configure

`~/.hist/config.toml` (or `$HIST_HOME/config.toml`) is the only source of
transcript locations; `hist` reads no host environment variables to find them.
The parser rejects unknown keys and overlapping roots. Keys:

- `[[sources]]`: one per host installation or account, with `adapter`
  (`codex` or `claude`), a `label` shown in message headers, and `roots`.
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

## Summaries (optional)

`hist build` writes summaries for the timelines in `summaries.timelines`
through `summaries.command`. `hist summarize` is a ready-made command over a
free agent CLI:

```toml
[summaries]
command = ["hist", "summarize", "devin"]      # SWE-2 Max; add "--model", "swe-2-high" for another
# command = ["hist", "summarize", "opencode"] # Muse Spark 1.3 free; "--model", "opencode/<id>"
timelines = ["omem"]
```

Each call runs the CLI headless in an empty scratch directory, without your
rules, hooks or plugins, and fails unless the agent made exactly one model
request and called no tool. Devin's tools are disabled or denied, and the
run's Devin session is deleted afterwards. OpenCode runs a private server
(`--standalone`) on a scratch config. Zen's free tier refuses requests that
list no OpenCode tools, so the tools stay listed with every call needing an
approval that a headless run cannot give.

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

## Install the hooks (optional)

Without hooks, every command still works and capture is complete. The hook
adds the view at session start, compaction, and clear in repositories listed in
`view.inject`, and records the parent of shell-launched workers.

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
so that worker's own workers link to it rather than to its parent.

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

A source stops, loudly, when its transcript no longer extends what was indexed
or breaks its adapter's expectations; the rest keep syncing.

Not implemented yet: `purge`, `restore`, `reset`, and background summary
builds (`hist build` runs synchronously). Every command, reads included, needs
write access to `~/.hist`, so `hist` fails inside a sandbox that blocks it, such
as Codex's `read-only` or `workspace-write` modes.

## Develop

```bash
uv run python -m unittest discover -s tests/hist -t .
uv run python benchmarks/hist_hook_latency.py          # hook p95 on a million-message timeline
```

Tests drive the `hist` command against fixture transcripts shaped like real
records (`tests/hist/fixtures.py`). A new host adds an adapter under
`hist/adapters/` and a subclass in `tests/hist/test_conformance.py`.
