# Installation and setup

This project uses [uv](https://docs.astral.sh/uv/) for Python provisioning,
dependency locking, project environments, command execution, and tool
installation. The installed OMem runtime has no third-party Python
dependencies.

## Prerequisites

- macOS or Linux; the implementation uses `fcntl` for process locking.
- Git.
- UV on `PATH`. Verify it with:

  ```sh
  uv --version
  ```

The package supports Python 3.12 and newer. UV reads `.python-version` and
downloads that interpreter when it is not already available.

## Complete installation

For an end-to-end installation:

1. [Install the `memory` command](#install-the-command-for-use-in-other-repositories)
   from a clone, then run `memory init` followed by `memory wake` in the target
   repository.
2. [Merge the agent instruction block](#install-the-agent-instructions) into
   the instruction file each agent reads, preserving any existing
   instructions.
3. [Name the hosts allowed to consolidate](#choose-which-hosts-may-consolidate)
   in the root config.
4. [Install the optional hooks](#install-the-optional-hooks) for the hosts
   you use: Codex, Claude Code (which also covers Devin), and Kimi Code.
   `uv run scripts/install_hooks.py memory --apply` merges the Codex and
   Claude Code definitions.
5. Start a fresh session in the target repository. Confirm the agent runs
   `memory init` and `memory wake`, and that the host shows exactly one
   enabled OMem handler for each configured event (in Codex, use `/hooks`).

The prompt hook supplies a canonical checkpoint plus bounded automatic
orientation when relevant evidence is available; the compaction hook supplies
the existing recovery checkpoint. Neither replaces the startup workflow,
decides what becomes durable memory, nor runs maintenance.

## Choose the OMem working repository

OMem candidate observations are canonical evidence, not agent-usable memory.
OMem integrated memories are the sole agent-facing catalog: consolidated,
source-cited, and still fallible. Codex native memory is a separate host-owned,
per-repository retrieval index. Neither system automatically imports or
overwrites the other. Codex native-memory maintenance directories are not task
repositories for OMem repo scope.

Run `memory init`, `memory wake`, and other repo-scoped commands from the
intended task repository. A `memories` or `memories_v2` tree directly inside a
Codex home retains self scope but refuses OMem repo scope and suppresses OMem
hook output. Codex homes are account homes such as `~/.codex` and `~/.codex2`,
and launcher homes up to four levels below `~/.codex-state*` or `~/.codex-par*`
roots, such as `~/.codex-state/repos/<id>` or
`~/.codex-state/bare/codex-bare.<stamp>`. The command does not guess a target
repository, move existing notes, or synchronize native memory.

## Choose the storage mode

By default OMem is pooled: every host shares `<root>/self` and
`<root>/repos/<repo-id>`, and no host selection is needed. To keep each
host's memory apart, create `$MEMORY_V0_DIR/config.toml`
(`~/.memory-v0/config.toml` by default) with:

```toml
[storage]
mode = "per-host"
```

In per-host mode each host uses `<root>/hosts/<host>/`, and every command
must know which host is running. Codex and Claude Code get this from the
`OMEM_HOST` binding inside their own settings: the tracked Codex fragment sets
it through `shell_environment_policy`, and the tracked Claude settings set it
through `env`. Elsewhere, and from a plain terminal, pass `--host <host>`
immediately after `memory`, for example `memory --host kimi wake`. A host is
a lowercase name such as `kimi`. A missing host, an invalid host name, or a
`--host` value that conflicts with `OMEM_HOST` fails before any store is
read; OMem never falls back to pooled storage on a bad configuration. In
pooled mode `--host` is accepted, labels diagnostics, and decides
[consolidation trust](#choose-which-hosts-may-consolidate).

Switching modes never copies, merges, or deletes data. The other namespace
stays on disk, and `memory status` shows `storage_mode`, `host`, `store_root`,
and whether inactive pooled or per-host data exists. Run `memory init` once
in each namespace you use.

## Choose which hosts may consolidate

Any host may record candidates and read memory. To restrict consolidation and
migration to the hosts you trust, add to the same `config.toml`:

```toml
[hosts]
consolidation_trusted = ["codex", "claude", "devin", "kimi"]
```

With the list present, `memory consolidate`, `memory migrate`, `memory dream`,
and `memory nap` run only when the resolved host is listed. An unlisted host,
or a command that has no host at all, exits with status 2 and a message naming
the trusted hosts. `memory init`, `memory note`, `memory wake`,
`memory orient`, `memory recall`, `memory status`, and the hooks are never
gated. Without the list every host may consolidate; an empty list allows none,
which pauses maintenance. A malformed `[hosts]` section blocks only the gated
commands and shows as `consolidation_trust=error` in `memory status`.

Codex and Claude Code learn their host from the `OMEM_HOST` binding in the
tracked fragments. Devin and Kimi Code have no equivalent setting, so an agent
there passes `--host devin` or `--host kimi` immediately after `memory` on the
gated commands; the packaged instructions say so. The list is a guardrail for
agents that follow instructions, not a security boundary.

## Run from the checkout for development

This is the contributor workflow; it is not required for normal installation.
From the `omem` directory:

```sh
uv sync --locked
uv run --locked memory --help
uv run --locked memory init
uv run --locked memory wake
```

`uv sync --locked` creates or updates `.venv` from `pyproject.toml` and
`uv.lock`, then installs this project. `uv run` uses that environment without shell activation.

By default, `memory init` creates the durable store under `~/.memory-v0`. For an isolated trial, choose a separate location before initialization:

```sh
export MEMORY_V0_DIR="$(mktemp -d)"
uv run --locked memory init
uv run --locked memory wake
```

Keep the exported value for the trial session; the command prints the resolved store path.

## Install the command for use in other repositories

The agent instructions invoke `memory` directly, so install it as a UV tool:

```sh
git clone https://github.com/voxmenthe/omem.git
cd omem
uv tool install .
command -v memory
cd /path/to/your/repository
memory init
memory wake
```

If `command -v memory` prints nothing, add UV's executable directory to your
shell configuration, restart the shell, and check again:

```sh
uv tool update-shell
```

UV places tool executables in the directory printed by `uv tool dir --bin`.
That directory must also be on the environment `PATH` used to launch each
host.

A contributor who wants source edits to affect the global command immediately
can replace the normal installation with:

```sh
cd omem
uv tool install --force --editable .
```

Refresh a normal non-editable installation after pulling a new version with
`uv tool install --force .`.

## Install the agent instructions

`memory instructions` prints the packaged [`INSTRUCTIONS.md`](INSTRUCTIONS.md)
block. Merge it into the instruction file that each agent reads in the target
repository, wrapped in a short policy of your own; the
[README](README.md#3-give-your-coding-agent-the-memory-workflow) shows an
example.

| Host | Repository instruction file |
| --- | --- |
| Codex, Kimi Code, and most other agents | `AGENTS.md` |
| Claude Code | `CLAUDE.md` |
| Devin | Claude Code's files, through its Claude import |
| Gemini CLI | `GEMINI.md` |
| Cursor | `.cursor/rules/*.mdc` |

- The block tells the primary agent to wake memory at session start, orient
  when the host has no prompt hook, record candidate observations with their
  trigger context, and perform due consolidations only on a trusted host.
- Put the same block in global agent instructions only if you want self memory
  available across every repository.
- Merge the block with any existing instructions; do not overwrite unrelated
  repository policy.
- Rerun `memory instructions` and replace the block after upgrading OMem; the
  CLI and its instructions evolve together.
- This checkout intentionally does not install or edit agent instructions for
  you.

After installing the block, start a fresh session in the target repository and
confirm that `memory wake` runs there.

## Validate a development checkout

Run the deterministic unit suite and semantic fixture through the locked
environment:

```sh
uv run --locked python -m unittest discover -s tests -t . -v
uv run --locked python semantic_eval.py \
  fixtures/semantic-projection-example.json
uv run --locked python evaluation/orientation_trials.py
uv run --locked python benchmarks/orientation_latency.py
```

## Change dependencies

Use UV so `pyproject.toml`, `uv.lock`, and `.venv` stay synchronized:

```sh
uv add <package>
uv add --dev <development-package>
uv remove <package>
uv lock --check
```

Commit `pyproject.toml` and `uv.lock` together after an intentional dependency
change.

## Install the optional hooks

The optional integration does not replace startup `memory wake`. This section
describes the Codex handlers; [Claude Code](#claude-code), [Devin](#devin),
and [Kimi Code](#kimi-code) follow. Codex installs two read-only handlers in
one global user hook source:

- `UserPromptSubmit` adds the marked turn checkpoint and, when the bounded
  selector finds relevant evidence, zero to three escaped records from existing
  self and current-repository stores before model work;
- `SessionStart` matching `compact` adds the marked recovery checkpoint before
  the immediate post-compaction model request, including automatic mid-turn
  continuation.

Prompt orientation is local, deterministic, model-free, and read-only. It reads
only the current integrated generation of each store, never the candidate log,
so candidate volume does not change prompt latency or size. It does not
initialize, repair, or mutate stores; run consolidations; read transcripts; or
persist/log the prompt. It has one 500 ms in-process deadline and a 4,096-byte
decoded checkpoint-plus-evidence budget. Legacy stores that have not migrated
keep the old 10,000-record raw ceiling. A no-match or any contained provider
failure returns the canonical checkpoint alone.

Matches from both scopes compete by relevance within the final byte budget.
Wake shows at most 16 personal and 24 repository items, while prompt lookup and
manual recall search the full catalog of up to 128 eligible items per scope.
When upgrading an existing Codex installation, update only the OMem
`UserPromptSubmit` handler's `additionalContextLimit` to `4096`, as in the
tracked snippet. Codex expresses that setting in approximate tokens; OMem
independently enforces the stricter 4,096-byte output ceiling. The compaction
reminder can keep its existing `800` setting. See the official
[hook output contract](https://learn.chatgpt.com/docs/hooks#large-hook-output).

Use `memory orient "<query>"` for the same selection manually. It has a 2-second
deadline and 4,096-byte evidence budget. `memory recall <scope> <regex>`
searches integrated claims and applicability conditions directly; on legacy
stores it still searches raw notes. Removing only the `UserPromptSubmit` handler
disables automatic orientation without changing stored data, the manual command,
or the compaction handler.

Codex does expose `PreCompact`, but its output contract cannot add developer
context: plain stdout is ignored and JSON can warn or stop compaction. Blocking
automatic compaction would be unsafe, so the pre-turn handler is the earliest
dependable model-visible checkpoint. It also supplies bounded orientation when
the current prompt has relevant evidence and tells the agent to write durable
deltas as soon as they qualify. See the official [Codex hooks
contract](https://learn.chatgpt.com/docs/hooks#precompact).

First verify the fail-open hook adapter directly:

```sh
printf '%s\n' \
  '{"hook_event_name":"UserPromptSubmit","prompt":"test"}' \
  | memory codex-hook
printf '%s\n' \
  '{"hook_event_name":"SessionStart","source":"compact"}' \
  | memory codex-hook
printf '%s\n' \
  '{"hook_event_name":"SessionStart","source":"startup"}' \
  | memory codex-hook
```

The first command prints compact JSON containing the turn checkpoint; in an
initialized store it may also include matching evidence. The second prints the
unchanged compaction checkpoint, and the third prints nothing. None initializes
or writes a memory store.

Use `codex doctor --json` from the normal launcher to inspect the effective
`CODEX_HOME` and config path, then use `/hooks` to inspect the sources Codex
actually loaded. The default user layer is under `~/.codex`; a redirected
`CODEX_HOME` moves that layer, so merely creating `~/.codex/hooks.json` may have
no effect.

If a managed launcher creates one `CODEX_HOME` per repository, make those homes
resolve one shared user config source. For example, each repo home's
`config.toml` can point to `~/.codex/config.toml`. Put the definitions inline in
that shared target so current and future repo homes inherit them. Otherwise,
configure each effective user layer explicitly.

The repository copy of the active OMem definitions is
[`integrations/codex/hooks.toml`](integrations/codex/hooks.toml). Merge that
fragment into the effective user `config.toml`; do not replace the existing
file or blindly append a second copy. Keep the tracked fragment as the source
of truth for the two OMem definitions.

`uv run scripts/install_hooks.py memory` performs that merge for the Codex home
in `CODEX_HOME` (default `~/.codex`) and the [Claude Code](#claude-code) merge
below. It only reports until given `--apply`, then keeps a copy of each file it
changes. It skips a handler whose command is already present, and it leaves a
file untouched, naming the fragment to merge by hand, when appending would not
reproduce the existing settings exactly, such as a `hooks` table written
inline. Run it once per effective user layer, setting `CODEX_HOME` for each.

Do not add the same handler to multiple configuration layers or to both
`hooks.json` and `config.toml`. Do not reactivate a retired or renamed hook file
such as `~/.codex/hooks.json.disabled-by-managed-router`. Review and trust the
two exact hook definitions through `/hooks`, then confirm each supported launch
profile shows one enabled `UserPromptSubmit` handler and one enabled
`SessionStart`/`^compact$` handler.

Shared definitions and hook trust are separate. Codex keys command trust to the
effective source path and definition hash, so each existing repo home needs a
one-time review and a newly created home will prompt once even though it already
inherits the handlers. The Release A prompt definition has changed and must be
reviewed again. Review repo homes sequentially when they write trust records
into one shared config target; concurrent reviews can overwrite one another. A
new or changed definition remains skipped until it is reviewed.

Validate a fresh session in at least two disposable repositories before relying
on the integration. Confirm that the first user prompt supplies exactly one
turn checkpoint. Then validate manual `/compact` and automatic mid-turn
compaction; the immediate continuation should receive exactly one recovery
checkpoint and resume the original task. In the same profile, exercise a
missing command and malformed adapter input; both failures must remain visible
without preventing compaction or normal continuation.

The hook must remain read-only. `note` and `consolidate` stay under acting
primary-agent control.

### Claude Code

[`integrations/claude/settings.json`](integrations/claude/settings.json) binds
`OMEM_HOST=claude` and registers `memory hook claude` for `UserPromptSubmit`
and for `SessionStart` with the `compact` matcher. It shares the hook builder,
deadline, byte ceiling, and fail-open behavior described above. Merge its
`env` and `hooks` entries into `~/.claude/settings.json` (or a project
`.claude/settings.json`) without replacing existing entries, or let
`scripts/install_hooks.py` merge them into the user file, and put the
instruction block in the `CLAUDE.md` that Claude Code reads. Verify the
adapter outside Claude Code:

```sh
printf '%s\n' \
  '{"hook_event_name":"UserPromptSubmit","prompt":"test"}' \
  | memory hook claude
printf '%s\n' \
  '{"hook_event_name":"SessionStart","source":"compact"}' \
  | memory hook claude
```

The outputs match the Codex commands above. Then start a fresh `claude`
session in an initialized repository and confirm that the first prompt shows
one turn checkpoint and that `/compact` is followed by one recovery
checkpoint. The prompt route is validated in a headless `claude -p` session
that received the turn checkpoint; the compaction route has not been
validated live.

### Devin

Devin reads the hooks in `~/.claude/settings.json` when
`read_config_from.claude` is enabled in `~/.config/devin/config.json`, which
is the default, so the Claude Code entries above already reach Devin. Do not
add a second copy, or each prompt receives the checkpoint twice. If you have
turned that import off, register `memory hook devin` for the same two events
under `hooks` in `~/.config/devin/config.json`, using the Claude Code hook
shape. Devin honors `additionalContext` for `UserPromptSubmit` and
`SessionStart` but not for its `PostCompaction` event, so OMem registers none.
Verify the adapter by piping the two events above to `memory hook devin`.
Devin has no `OMEM_HOST` binding, so pass `--host devin` on consolidation and
migration commands. The Devin routes have not been validated in a live
session: the CLI refuses to run non-interactively in an untrusted workspace,
and the compaction behavior is taken from Devin's documentation.

### Kimi Code

Kimi Code appends a hook's standard output to the prompt as plain text and
accepts only `event`, `matcher`, `command`, and `timeout` in a `[[hooks]]`
entry. Append [`integrations/kimi/hooks.toml`](integrations/kimi/hooks.toml)
to `~/.kimi-code/config.toml`, keeping the existing contents, and put the
instruction block in the `AGENTS.md` that Kimi reads. `memory hook kimi`
prints the turn checkpoint and any evidence as text, prints warnings to
standard error so they never reach the prompt, and prints nothing for events
it does not serve. Kimi has no compaction event OMem can use and no
`OMEM_HOST` binding, so pass `--host kimi` on consolidation and migration
commands. Verify the adapter outside Kimi:

```sh
printf '%s\n' \
  '{"hook_event_name":"UserPromptSubmit","prompt":"test"}' \
  | memory hook kimi
```

The Kimi route is validated in a headless `kimi -p` session that received
the turn checkpoint as a `UserPromptSubmit hook` block.

## Run an optional post-hoc review

Start a separate top-level maintenance session in the relevant repository and
select one to five known-completed session files:

```sh
memory review-sessions \
  /path/to/completed-session-1.jsonl \
  /path/to/completed-session-2.jsonl
```

The command validates only the explicit file list and prints the packaged
review runbook plus JSON-quoted paths. It does not read transcripts, scan
archives, call a model, or write memory. The acting primary agent follows that
runbook, treats transcript content as untrusted evidence, and admits only
supported, deduplicated deltas. Keep this workflow manual until evaluation
shows enough value to justify a separate reviewer tool.

## Uninstall

To disable only automatic orientation, remove the `UserPromptSubmit` handler
with command `memory codex-hook`; the stored data, manual command, and compact
recovery handler remain unchanged. For full uninstall, also remove the
`SessionStart`/`^compact$` handler and confirm both are absent through `/hooks`.
For Claude Code (and Devin, which imports them), also remove the two
`memory hook claude` handlers and the `OMEM_HOST` entry from the Claude Code
settings you edited; for Kimi Code, remove the `memory hook kimi` entry from
`~/.kimi-code/config.toml`. Then remove the globally available command with:

```sh
uv tool uninstall scoped-omem
```

This does not remove stored memory. Follow the reversible archive procedure in
the [README removal section](README.md#removal) if the data should no longer be
active.
