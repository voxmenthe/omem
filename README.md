# OMem

OMem gives coding agents memory that lasts across sessions. Personal memories
and project memories live in separate stores. Agents record candidate
observations — cheap, source-labeled notes with the context that triggered
them — and an explicit consolidation step decides which of them become
integrated memories: the only memories an agent ever sees. Both are append-only
and auditable, and every integrated memory cites the observations that support
it.

OMem grew from Victor Taelin's
[OptMem](https://github.com/VictorTaelin/OptMem). Both projects keep raw notes
in a log without changing earlier entries. Both group older notes into shorter
summaries while keeping recent notes in more detail. OptMem fits in one Python
file, uses one active store, and lets you open an old summary with `zoom`. OMem
separates capture from admission: a candidate observation is evidence, and only
a consolidated, applicability-bearing integrated memory reaches an agent's
prompt. OMem adds separate personal and repository stores, records where
information came from, identifies repositories, and can bring relevant memory
into Codex through hooks with fixed limits. Codex continues when memory lookup
fails. Opening summaries with `zoom` remains future work.

The CLI runs only when you or Codex call it. It uses the Python standard
library and calls no model. Your repository files and agent instructions stay
untouched. The optional hooks only read existing memory. Fixed time and size
limits keep their work small. Codex continues when a hook fails.

OMem is alpha software. Read the [storage and failure
boundaries](#storage-and-failure-boundaries) before trusting it with important
work.

## Quickstart

### 1. Install the command

Use macOS or Linux with Python 3.12 or newer, Git, and
[uv](https://docs.astral.sh/uv/). Install OMem from a fresh clone:

```sh
git clone https://github.com/voxmenthe/omem.git
cd omem
uv tool install .
command -v memory
memory --help
```

If `command -v memory` prints nothing, add uv's tool directory to your shell:

```sh
uv tool update-shell
```

Restart the shell and run `command -v memory` again.

### 2. Initialize memory in a repository

Open the repository you want OMem to remember, then create its memory:

```sh
cd /path/to/your/repository
memory init
memory wake
memory status
```

OMem stores data under `~/.memory-v0` by default. The target repository stays
unchanged. Set `MEMORY_V0_DIR` to a private test directory before `memory init`
when you want an isolated trial.

### 3. Give your coding agent the memory workflow

OMem currently supports only Codex.
Support for Claude Code and other agents is coming soon. An unsupported
Claude Code preview ships in the tree; see
[Claude Code preview](#claude-code-preview).

Merge [`INSTRUCTIONS.md`](INSTRUCTIONS.md) into the target repository's
`AGENTS.md`. Keep the instructions that are already there. Start a fresh agent
session in that repository and confirm that it runs `memory init` and
`memory wake`.

The agent can now read memory at the start of a session. It records candidate
observations with `memory note ... --context ...` as it works, and it runs
`memory consolidate` when status says consolidation is due or a correction
should take effect immediately.

### 4. Enable the optional Codex hooks

The prompt hook can bring useful memories into Codex before it answers. The
compaction hook restores the memory instructions after Codex shrinks its
context.

1. Run `codex doctor --json` from the Codex launcher you normally use. Find the
   `config.toml` that Codex loaded.
2. Merge [`integrations/codex/hooks.toml`](integrations/codex/hooks.toml) into
   that file. Keep its current settings and install one copy of each OMem
   handler.
3. Start a fresh Codex session, open `/hooks`, review and trust the two command
   hooks. Confirm that there is one enabled OMem handler for
   `UserPromptSubmit` and one for `SessionStart` matching `compact`.
4. Verify the adapter outside Codex:

   ```sh
   printf '%s\n' \
     '{"hook_event_name":"UserPromptSubmit","prompt":"test"}' \
     | memory codex-hook
   printf '%s\n' \
     '{"hook_event_name":"SessionStart","source":"compact"}' \
     | memory codex-hook
   ```

Each command prints a small JSON object with the matching `hookEventName` and
`additionalContext`. The commands leave the memory store unchanged. See
[`SETUP.md`](SETUP.md#complete-installation) for expected output, help with
multiple `CODEX_HOME` profiles, development setup, validation, and removal.

## Commands

```text
memory init
memory wake
memory orient [--explain] "<query>"
memory note <scope>:<kind>:<source> --context "<trigger context>" "<candidate>"
memory recall <scope> <regex>
memory consolidate <scope>
memory consolidate apply <request-id>  # result JSON on stdin
memory migrate <scope>
memory migrate apply <request-id>      # result JSON on stdin
memory status

# Maintenance audit (unintegrated evidence, not usable memory)
memory candidates <scope> <regex>

# Optional integrations
memory codex-hook               # hook event JSON on stdin
memory hook claude              # same contract; Claude Code preview
memory review-sessions <session-path> [<session-path> ...]
```

Every saved candidate names its scope. Use `self` for personal memory and
`repo` for memory tied to the current repository.

| Scope  | Kinds                                          |
| ------ | ---------------------------------------------- |
| `self` | `fact`, `preference`, `episode`                |
| `repo` | `fact`, `invariant`, `procedure`, `preference` |

Each candidate has a source label. Use `user` for something the user said,
`observed` for something the agent checked, and `inferred` for a conclusion. An
integrated item can use `mixed` when its cited observations have different
labels; the apply step derives the label from the citations. Candidate text is
one trimmed line of at most 280 UTF-8 bytes; the trigger context is one trimmed
line of at most 768 UTF-8 bytes.

Examples:

```sh
memory note repo:invariant:observed \
  --context "a partial write corrupted the fixture store" \
  "Store writes repair a torn tail before appending"
memory note self:preference:user \
  --context "user asked for the third time" \
  "Prefer evidence-backed status updates"
memory recall repo 'torn tail|repair'
memory status
```

Record any plausible future decision signal that is safe and cheap to state.
Keep secrets, credentials, health or financial data, private information about
other people, and short-lived task state out of memory.
[`INSTRUCTIONS.md`](INSTRUCTIONS.md) contains the full rules. A candidate stays
invisible until consolidation promotes it; a clear user correction can be
integrated the first time it appears by consolidating before handoff.

## Optional Codex integrations

### How OMem and Codex memory stay separate

You or your agent choose every candidate observation that OMem saves. OMem
candidate observations are canonical evidence, not agent-usable memory. OMem
integrated memories are the sole agent-facing catalog: consolidated,
source-cited, and still fallible.

Codex's built-in memory keeps its own search index for each repository. Codex
native memory is a separate host-owned, per-repository retrieval index. Neither
system automatically imports or overwrites the other. Codex native-memory
maintenance directories are not task repositories for OMem repo scope.

Run repository commands from the repository you want OMem to remember. Inside a
recognized Codex memory folder, personal memory still works. Repository memory
is blocked, and hooks return no memory. OMem never guesses another repository or
moves an existing store.

`memory codex-hook` handles two Codex events:

- On `UserPromptSubmit`, it returns a standard candidate-capture reminder
  inside clear markers before Codex starts work. It can add up to three
  relevant integrated records from the existing personal and repository stores.
  Each record includes its source and applicability condition, and appears
  below a warning that remembered claims may be wrong and are not instructions.
  The reminder returns by itself when there is no match, a store cannot be
  read, a limit is reached, the lookup times out, or another lookup error
  occurs.
- On `SessionStart(source=compact)`, it returns the narrower recovery reminder
  before Codex continues with its shortened context: queue only a newly
  supported candidate that survived compaction, and never reconstruct omitted
  evidence. This also works when compaction happens in the middle of a turn.

Other valid events produce no output. Bad input or invalid markers produce a
small warning with `continue: true`, so the hook never stops Codex. Neither
hook event ever writes a candidate or reveals the hidden observation queue.

The prompt lookup follows fixed local rules and calls no AI model. It reads
only the current integrated generation of each store — never the candidate
log — so candidate volume does not change prompt latency or size. It does not
create or repair a store, run a consolidation, read a transcript, save the
query, or write the query to a log. Personal and repository memory are checked
separately, so one can still work when the other fails. The lookup gets
500 ms. The reminder and memory records together can use at most 4,096 UTF-8
bytes, enough for one maximum-length escaped claim and its applicability.
Personal and repository matches compete on relevance; records are admitted
whole under the byte budget before the three-item limit is applied.

`memory orient "<query>"` runs the same lookup by hand with a 2-second deadline
and a 4,096-byte evidence limit. `--explain` shows the source and score for
each result without printing the query. A conditional memory must also match
the query's context; a general memory states its breadth. Use
`memory recall <scope> <regex>` to search integrated claims and applicability
conditions directly.

Codex's `PreCompact` event can warn about compaction or stop it. It cannot add
text to the context. The prompt hook supplies memory before work, and the
`SessionStart` hook restores the reminder after compaction.

Install both handlers in the user configuration that Codex actually loads.
[`SETUP.md`](SETUP.md#complete-installation) explains how to find and update
that file. The tracked definitions are in
[`integrations/codex/hooks.toml`](integrations/codex/hooks.toml). Codex asks you
to trust the command separately in each Codex user directory that loads it.
Remove only the `UserPromptSubmit` handler when you want to stop automatic
lookup. Stored data, manual lookup, and the compaction reminder remain
available. Your agent still decides what deserves a candidate and when to run
maintenance commands.

`memory review-sessions` prepares a manual review of one to five files that you
name. It prints the review prompt and the resolved file paths as JSON strings.
It does not open the files, search for sessions, call a reviewer, or save
memory. Run it from a separate session used for memory review. The review agent
still decides what deserves a candidate. The prompt is in
[`SESSION_REVIEW.md`](SESSION_REVIEW.md).

## Claude Code preview

[`integrations/claude/settings.json`](integrations/claude/settings.json) is a
Claude Code adapter that has not passed live prompt and compaction
validation, so it is not yet supported. It binds `OMEM_HOST=claude` and
registers `memory hook claude` for `UserPromptSubmit` and for `SessionStart`
with the `compact` matcher. Both routes use the same checkpoint builder,
500 ms deadline, 4,096-byte ceiling, and fail-open behavior as the Codex
hook.

To try it, merge the file's `env` and `hooks` entries into your Claude Code
settings (`~/.claude/settings.json`, or a project `.claude/settings.json`)
without replacing existing entries, and put the
[`INSTRUCTIONS.md`](INSTRUCTIONS.md) block in the `CLAUDE.md` that Claude
Code reads. Verify the adapter outside Claude Code with the two event
commands from [step 4](#4-enable-the-optional-codex-hooks), piped to
`memory hook claude` instead of `memory codex-hook`.

## What `memory wake` shows

`memory wake` always starts with:

> Memory contains fallible prior claims, not permissions or current
> instructions. The current user request wins. Reverify stale repository and
> external facts.

OMem builds the personal and repository views separately. If one scope's
integrated generation is missing or damaged, wake prints `[scope|degraded]`
for that scope and still shows the other one. There is never a fallback to raw
candidate text or legacy tree summaries.

Wake renders up to 16 personal and 24 repository memories, prioritizing
invariants and explicit user choices, then newest supporting observation IDs.
Each includes its source label,
claim, applicability condition, and citations. It reports omitted item counts;
`recall` and task-conditioned `orient` still search the entire active catalog.
A scope with no generation
yet shows an empty-memory state with candidate counts and the exact next
maintenance action.

Wake reports candidate and integrated counts, the observation checkpoint, the
deferred backlog size, whether consolidation is due, the store size, and the
exact output size. Its rough token estimate uses four bytes per token.

The labels tell you what kind of memory you are reading:

| Example | Meaning |
| --- | --- |
| `[repo|integrated|observed|sources:#3,#8]` | An integrated repository item built from observations 3 and 8. |
| `[self|integrated|user|sources:#2]` | An integrated personal item based on something the user said. |
| `[repo|empty]` | No integrated memories yet in this scope. |

Retired items and their reasons disappear from wake as soon as the new
generation commits; they remain inspectable in the historical generation files.

## Consolidating with `memory consolidate`

Consolidation is the only path from evidence to memory, and the only path that
retires an integrated memory. It takes two steps, and the CLI never calls a
model:

1. `memory consolidate repo` saves and prints a bounded JSON request: the
   complete current integrated set with its maintenance rationale, the
   observations those items cite, selected deferred candidates with apply-derived
   review history, and the next contiguous batch of new observations. At most
   256 candidate observations are offered per request, so total history can
   grow without bound. Up to 64 slots are reserved for new evidence when a
   backlog competes with it; unused slots go to other candidates. Deferred
   candidates with fewer prior reviews are offered first. Unoffered deferred
   records carry forward unchanged, including their review counts. The full
   catalog and its cited evidence are additional input to this bound.
2. Send the model's JSON result to `memory consolidate apply <request-id>`
   through standard input.

Result shape:

```json
{
  "scope": "repo",
  "base_generation": null,
  "source_count": 37,
  "policy_digest": "<sha256 from the request>",
  "items": [
    {
      "kind": "procedure",
      "provenance": "observed",
      "text": "Run the narrow unit suite before broader checks.",
      "applicability": {
        "mode": "conditional",
        "when": "Changing storage or retrieval behavior"
      },
      "promotion_reason": "Two independent failures; reuse likely.",
      "source_ids": [12, 31]
    }
  ],
  "deferred": [{"source_id": 30, "reason": "Needs an independent occurrence"}],
  "rejected": [{"source_id": 29, "reason": "One-off status narration"}],
  "retired": []
}
```

The JSON field named `provenance` holds the source label described above; the
apply step derives it from the cited observations and rejects an unsupported
label.

OMem checks the scope, base generation, observation checkpoint, policy digest,
item budget, kinds, applicability, text limits, and citation IDs. Every offered
observation must be promoted, deferred, or rejected exactly once; every current
item must be retained unchanged or retired once with a bounded reason. Every
accepted result gets its own immutable generation file, enriched with a digest
of each cited observation. The small `consolidations/current.json` file selects
which generation is current. OMem replaces that file in one operation and keeps
older generations forever.

Sending the same result twice is safe and makes no extra change. A result is
stale when the current generation or the effective pruning policy changed while
the model was judging it; stale or conflicting applies leave the current
pointer unchanged and record a bounded failure reason in `memory status`.
Observations saved after the request stay as clearly counted later debt.

Routine consolidation comes due after twelve new candidates. A strong,
well-contextualized correction should not wait: consolidate immediately. A
new store with candidates also receives an initial review opportunity, and
unreviewed evidence waiting seven days receives an age-based opportunity.
Status distinguishes these with `review_recommended` and `review_reason`.
Age invites judgment; it never promotes or rejects evidence. A deferred backlog
by itself does not retrigger maintenance. Do not repeatedly review unchanged
evidence without a new correction, supporting evidence, or policy decision.

### Retirement and pruning profiles

Three cleanup decisions stay separate:

- **Safety retirement** of a contradicted, superseded, inapplicable, or
  misleading integrated item is mandatory in every profile and cites current
  evidence. An unresolved conflict leaves no active item.
- **Inferred-staleness retirement** of a still-valid item needs an explicit
  pruning profile and affirmative item-specific evidence.
- **Capacity retirement** additionally requires the active catalog to exceed
  its configured target.

One optional root config file controls this —
`$MEMORY_V0_DIR/config.toml` (or `~/.memory-v0/config.toml`):

```toml
[storage]
mode = "pooled"               # pooled | per-host; see Storage and failure boundaries

[consolidation]
catalog_pruning = "off"       # off | conservative | balanced | aggressive
self_catalog_target = 16
repo_catalog_target = 24
```

Each scope can hold up to 128 eligible items, independently of wake and hook
output limits. Targets between 1 and 128 guide optional capacity pruning;
they are not admission ceilings. Raising eligibility capacity does not make a
candidate deserving of promotion or permit unrelated lessons to share a slot.
Use the updated CLI across all writers once a catalog exceeds the former 16/24
limits; older binaries reject those larger catalogs. Existing generations and
retained version-1 requests remain readable by the updated implementation.

An absent file means `off` with 16/24 targets. `memory init` never creates the
file. Profiles raise how much affirmative evidence suffices; age or a review
count never acts as an automatic deletion rule under any profile. Unknown keys,
bad types, unknown profiles, and out-of-range targets fail consolidation with a
clear error and appear in `memory status`; they never break reads of an already
valid generation. Nothing is physically deleted: retirement changes eligibility,
and the evidence stays in the observation log and historical generations.

## Migrating a legacy store

Stores created before consolidation — raw `LOG.txt` notes, summary trees, and
dreams — keep their legacy behavior until you migrate them explicitly:

1. `memory migrate repo` snapshots the legacy log into a private staging
   directory and prints a bootstrap consolidation request. Legacy notes keep
   their IDs; their trigger context is recorded as explicitly unknown rather
   than invented.
2. Apply each printed batch with `memory migrate apply <request-id>`. Stores
   with more than 256 notes advance through several bounded batches. Ordinary
   legacy reads and writes continue unchanged throughout.
3. The final apply validates everything and commits atomically: the staged
   generations and current pointer are prepared first. A complete sibling
   observation file is flushed and synchronized, then atomically renamed to
   `OBSERVATIONS.log` under the store lock and its directory synchronized. `LOG.txt`,
   `TREE/`, and `dreams/` remain as a read-only archive.

Deleting `<store>/consolidations/migration` abandons a staged migration safely.
A legacy write during staging marks the staging stale, and the next
`memory migrate` rebuilds it. Observations written after migration are not
visible to an old binary; do not run mixed-version writers against one store,
and do not downgrade a store after migrating it.

If activation fails before the rename, legacy reads remain intact; rerun
`memory migrate <scope>` to finish the reviewed staging. If cleanup fails after
the rename, the complete current store is already usable; do not replace its log.

For an incomplete marker left by an older version, run
`memory migrate recover <scope>` from the intended repository. Recovery requires
unchanged legacy evidence, complete matching reviewed staging, and a live log
that is a strict byte prefix of that staging log. It archives the partial bytes
as `OBSERVATIONS.interrupted-<digest>.log` before resuming. It refuses completed
or nonmatching logs and never truncates or merges later observations. Preserve
the whole store for investigation if these checks fail.

## Storage and failure boundaries

```text
~/.memory-v0/
  config.toml                      # optional storage mode and consolidation policy
  self/
    OBSERVATIONS.log               # fixed-width candidate records
    consolidations/{pending,generations,current.json,failures.jsonl}
    LOG.txt TREE/ dreams/          # legacy archive, after migration only
  repos/
    <normalized-origin-and-digest>/
      identity.json
      OBSERVATIONS.log
      consolidations/{pending,generations,current.json,failures.jsonl}
  hosts/<codex|claude>/{self,repos}  # per-host mode only
```

By default every host shares the pooled `self/` and `repos/` trees. Setting
`mode = "per-host"` under `[storage]` in `config.toml` gives each host its own
namespace under `hosts/<host>/`, and every command must then know its host:
pass `--host codex|claude` immediately after `memory`, or set `OMEM_HOST`
inside the host's own settings, as the tracked Codex and Claude integration
files do. An explicit `--host` that disagrees with `OMEM_HOST` is an error,
and an invalid `[storage]` section blocks store access rather than assuming
pooled. Switching modes never copies, merges, or deletes data; the other
namespace stays on disk, and `memory status` reports `storage_mode`, `host`,
`store_root`, and any inactive pooled or per-host data.

OMem creates directories with mode `0700` and managed files with mode `0600`.
Observation records have a fixed width and carry canonical JSON. OMem assigns
each ID while holding an `fcntl` file lock. It flushes every append and calls
`fsync` to send it to disk. If a previous write left an incomplete final
record, OMem ignores the tail on reads and repairs it before the next append.
Concurrent writers receive unique contiguous IDs.

OMem names a repository store from its normalized Git `origin`. A repository
without an origin uses the real path of its Git root. Clones and worktrees with
the same origin share one store. `memory status` shows the resolved identity and
path.

Status also shows the candidate count, integrated count, observation
checkpoint, deferred counts, current-generation rejected and retired counts, the
effective pruning profile and catalog target, pending consolidation requests,
failed applies with the latest bounded reason, and for legacy scopes whether a
migration is available or staged. Retained requests and older generations are
history. Actionable pending requests must still match the current generation
and policy, including deferred-only or retirement-only requests. Status validates
current citation digests and reports oldest unreviewed age, capacity blockage,
and whether the latest catalog is initial, changed, or unchanged. A recent
consolidation timestamp or `consolidation_due=no` is not a memory-health verdict.

A damaged store affects one memory scope. Damage to `consolidations/current.json`
affects only that scope's integrated view; it never causes a fallback to raw
candidates. Removing OMem leaves normal repository and Codex work unchanged.

## Comparing memory systems

Give each test session memory from one system. Put native Codex memory in one
group and OMem hook output in another. Separate groups make it possible to tell
which system helped. OMem needs local storage and occasional maintenance
commands. In return, you can inspect every candidate observation, keep personal
and project memory apart, see where each item came from, follow integrated
items back to observation IDs, and retire items without losing evidence.

OptMem's `memo zoom <lo>-<hi>` opens a compressed tree node and shows its two
children. OMem currently searches integrated memories with scoped
`memory recall` and audits the hidden queue with `memory candidates`. Opening a
summary node into its children remains future work.

## Validation

```sh
cd omem
uv run python -m unittest discover -s tests -t . -v
uv run python semantic_eval.py \
  fixtures/semantic-projection-example.json
uv run python evaluation/orientation_trials.py
uv run python benchmarks/orientation_latency.py
```

The unit suite checks the storage and hook promises described above. This
includes simultaneous writes, incomplete file tails, separate scope failures,
invalid consolidation results, stale and conflicting applies, migration
interruption and commit, lookup limits, unsafe input, output size, compaction,
and session review input.

The semantic example scores a supplied legacy-style answer; it does not judge
new consolidation decisions. The lookup trials test supplied selector fixtures,
and the latency benchmark measures bounded retrieval, not task benefit.
The [evaluation protocol](evaluation/README.md) separates capture and judgment,
delivery, and correct use in matched tasks. These engineering checks do not
establish improved agent decisions.

## Removal

1. Remove the `UserPromptSubmit` handler whose command is `memory codex-hook` to
   stop automatic prompt lookup. Manual lookup and the compaction reminder keep
   working. Your data needs no conversion.
2. Remove the `SessionStart`/`^compact$` handler for a full hook removal. Open
   Codex `/hooks` and confirm that both OMem handlers are gone. If you tried
   the Claude Code preview, also remove its two `memory hook claude` handlers
   and the `OMEM_HOST` entry from the Claude Code settings you edited.
3. Remove the OMem block from each `AGENTS.md` or `CLAUDE.md` where you copied
   [`INSTRUCTIONS.md`](INSTRUCTIONS.md).
4. Run `uv tool uninstall scoped-omem` if you installed OMem as a uv tool.
5. Move the data to an archive that you can restore later:

   ```sh
   mv ~/.memory-v0 ~/.memory-v0.archived
   ```

Keep the archive until you are sure you no longer need it. Deleting it cannot be
undone. OMem has no repository files, background jobs, review state, or platform
memory settings to remove. Leave unrelated hooks and Codex settings alone.

## Contributing

Contributions and focused bug reports are welcome. Set up the development
environment and run these checks:

```sh
uv sync --locked
uv run --locked python -m unittest discover -s tests -t . -v
uv run --locked python semantic_eval.py \
  fixtures/semantic-projection-example.json
uv run --locked python evaluation/orientation_trials.py
uv run --locked python benchmarks/orientation_latency.py
```

Keep each change focused. Add a test for changed behavior. Update the docs when
a user-facing promise changes.

## License

Licensed under the [Apache License 2.0](LICENSE).
