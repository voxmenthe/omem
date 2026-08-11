# Setup

This project uses [uv](https://docs.astral.sh/uv/) for Python provisioning,
dependency locking, project environments, command execution, and tool
installation. Do not create a virtual environment manually or install the
project with `pip`.

## Prerequisites

- macOS or Linux; the implementation uses `fcntl` for process locking.
- UV on `PATH`. Verify it with:

  ```sh
  uv --version
  ```

The package supports Python 3.12 and newer. UV reads `.python-version` and
downloads that interpreter when it is not already available.

## Complete installation

For an end-to-end Codex installation:

1. [Install the `memory` command](#install-the-command-for-use-in-other-repositories)
   from this checkout and run `memory init` followed by `memory wake`.
2. [Merge the agent instruction block](#install-the-agent-instructions) into
   the target repository's `AGENTS.md`, preserving any existing instructions.
3. [Merge and trust the two global hooks](#install-the-optional-global-hooks)
   from [`integrations/codex/hooks.toml`](integrations/codex/hooks.toml).
4. Start a fresh Codex session in the target repository. Confirm the agent
   runs `memory init` and `memory wake`, then use `/hooks` to confirm exactly
   one enabled OMem handler for each configured event.

The hooks supply lightweight turn and compaction reminders; they do not replace
the startup workflow, decide what becomes durable memory, or run maintenance.

## Choose the OMem working repository

OMem raw notes are explicitly admitted, portable memory. OMem dreams and tree
covers are derived, fallible projections; raw notes remain authoritative within
OMem. Codex native memory is a separate host-owned, per-repository retrieval
index. Neither system automatically imports or overwrites the other. Codex
native-memory maintenance directories are not task repositories for OMem repo
scope.

Run `memory init`, `memory wake`, and other repo-scoped commands from the
intended task repository. Recognized `~/.codex-state/repos/*/memories`,
`~/.codex-state/bare*/memories`, and `~/.codex/memories` trees retain self scope
but refuse OMem repo scope and suppress OMem hook output. The command does not
guess a target repository, move existing notes, or synchronize native memory.

## Run from the checkout

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
cd omem
uv tool install --editable .
command -v memory
memory init
memory wake
```

Editable installation makes source changes available without reinstalling the tool. If project metadata or dependencies change, refresh the tool environment with:

```sh
uv tool install --force --editable .
```

UV places tool executables in the directory printed by `uv tool dir --bin`.
That directory must be on the agent's `PATH`; otherwise, use the absolute
executable path in the installed instructions.

## Install the agent instructions

Merge the contents of [`INSTRUCTIONS.md`](INSTRUCTIONS.md) into the target
repository's root `AGENTS.md`.

- The block tells the primary agent to wake memory at session start, record
  selected durable notes, and perform due dreams.
- Put the same block in global agent instructions only if you want self memory
  available across every repository.
- Merge the block with any existing instructions; do not overwrite unrelated
  repository policy.
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
```

The semantic fixture verifies the scoring harness only. Follow
[`EVALUATION.md`](EVALUATION.md) for the real observation period.

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

## Install the optional global hooks

The optional integration does not replace startup `memory wake`. It installs
two read-only handlers in one global user hook source:

- `UserPromptSubmit` adds the marked turn checkpoint as developer context
  before model work on every user prompt, in every repository;
- `SessionStart` matching `compact` adds the marked recovery checkpoint before
  the immediate post-compaction model request, including automatic mid-turn
  continuation.

Codex does expose `PreCompact`, but its output contract cannot add developer
context: plain stdout is ignored and JSON can warn or stop compaction. Blocking
automatic compaction would be unsafe, so the pre-turn handler is the earliest
dependable model-visible reminder and tells the agent to write durable deltas as
soon as they qualify. See the official [Codex hooks
contract](https://learn.chatgpt.com/docs/hooks#precompact).

First verify the pure adapter directly:

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

The first two commands print compact JSON responses with different canonical
checkpoints; the third prints nothing. None initializes or opens a memory
store.

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

Do not add the same handler to multiple configuration layers or to both
`hooks.json` and `config.toml`. Do not reactivate a retired or renamed hook file
such as `~/.codex/hooks.json.disabled-by-managed-router`. Review and trust the
two exact hook definitions through `/hooks`, then confirm each supported launch
profile shows one enabled `UserPromptSubmit` handler and one enabled
`SessionStart`/`^compact$` handler.

Shared definitions and hook trust are separate. Codex keys command trust to the
effective source path and definition hash, so each existing repo home needs a
one-time review and a newly created home will prompt once even though it already
inherits the handlers. Review repo homes sequentially when they write trust
records into one shared config target; concurrent reviews can overwrite one
another. A new or changed definition remains skipped until it is reviewed.

Validate a fresh session in at least two disposable repositories before relying
on the integration. Confirm that the first user prompt supplies exactly one
turn checkpoint. Then validate manual `/compact` and automatic mid-turn
compaction; the immediate continuation should receive exactly one recovery
checkpoint and resume the original task. In the same profile, exercise a
missing command and malformed adapter input; both failures must remain visible
without preventing compaction or normal continuation.

The hook must remain read-only. `note`, `nap`, and `dream` stay under acting
primary-agent control.

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

First remove only the `UserPromptSubmit` and `SessionStart`/`^compact$`
handlers with command `memory codex-hook` and confirm both are absent through
`/hooks`. Then remove the globally available command with:

```sh
uv tool uninstall scoped-omem
```

This does not remove stored memory. Follow the reversible archive procedure in
the [README removal section](README.md#removal) if the data should no longer be
active.
