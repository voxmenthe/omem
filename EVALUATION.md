# Release A automatic-orientation evaluation

Decision on 2026-08-10: **ship automatic orientation with a 10,000-record
ceiling per scope**. Keep manual orientation and scoped raw recall as fallbacks.
Do not add an index, daemon, model call, provider framework, or new persistent
format.

This decision is based on deterministic paired trials and a reproducible local
latency benchmark. It establishes the bounded Release A gate; it does not claim
that controlled fixtures prove downstream value in real coding sessions.

## Paired selection trials

`evaluation/orientation_trials.py` compares the automatic selector against the
previous static-checkpoint behavior on the exact rendered-output fixture set.

| Result | Count |
|---|---:|
| Positive trials | 11 |
| Automatic-orientation retrieval wins | 11 |
| Static-checkpoint retrieval wins | 0 |
| Negative trials | 5 |
| Correct negative abstention ties | 5 |
| Automatic-orientation false positives | 0 |
| Exact contract failures | 0 |

The positive set covers paths, symbols, error fragments, Unicode, current-dream
corrections, raw/dream deduplication, scope reservation, deterministic ties,
hostile control text, safe unrelated co-selection, and refusal to let a dream
resolve a conflict using unrelated evidence. The negative set covers
weak common words, near-tied raw conflicts, superseded procedures without a
valid dream, renamed paths without a valid dream, and contradictory status.

These are deliberately controlled cases. They show repeatable gains over a
checkpoint that cannot retrieve evidence and preserve conservative abstention
on the frozen hazards; they do not measure distraction on arbitrary prompts.

## Latency and support ceiling

`benchmarks/orientation_latency.py` builds structurally valid self and
repository stores at 1,000, 10,000, and 100,000 records per scope. Each scenario
uses 20 warm runs and is repeated with and without a valid dream. All times are
milliseconds on the local development machine.

| Records/scope | Valid dream | Warm total p50 | Warm total p95 | Deadline misses |
|---:|:---:|---:|---:|---:|
| 1,000 | no | 33.259 | 36.595 | 0/20 |
| 1,000 | yes | 34.741 | 36.440 | 0/20 |
| 10,000 | no | 146.307 | 148.991 | 0/20 |
| 10,000 | yes | 147.993 | 150.797 | 0/20 |
| 100,000 | no | 500.419 | 500.717 | 20/20 |
| 100,000 | yes | 500.328 | 500.687 | 20/20 |

At the selected 10,000-record ceiling, the warm p50 remains below 150 ms and
the warm p95 remains below 250 ms in both scenarios. The p50 phase breakdown at
that ceiling was:

| Valid dream | Repository resolution | Capture | Scan/selection | Encoding | Total |
|:---:|---:|---:|---:|---:|---:|
| no | 20.619 | 0.386 | 124.395 | 0.047 | 146.307 |
| yes | 20.802 | 0.957 | 125.654 | 0.018 | 147.993 |

The harness also records the first measured pass after fixture construction as
a cold proxy. The total proxy values were 33.336 and 32.385 ms at 1,000,
146.216 and 146.863 ms at 10,000, and 500.366 and 500.632 ms at 100,000
(without and with a dream, respectively). The operating-system cache was not
purged, so these values must not be represented as true cold-filesystem
measurements.

At 100,000 records per scope, direct scan/selection took roughly 1.2-1.3
seconds and every passive invocation reached its 500 ms deadline. That failure
is the reason for the 10,000-record ceiling. A scope above the ceiling abstains
before scanning; no index is introduced in Release A.

## Process-level hook comparison

A separate 30-sample subprocess probe measured the complete hook command,
including interpreter/process startup:

| Hook state | p50 | p95 | Max |
|---|---:|---:|---:|
| Static checkpoint baseline | 52.880 | 59.430 | 61.640 |
| Automatic orientation at 10,000 records/scope | 196.473 | 205.963 | 207.137 |

The automatic response was 463 decoded UTF-8 bytes in that matching case. The
passive service itself retains its 500 ms hard deadline; Codex's configured
3-second command timeout remains only an outer process envelope.

## Release decision and recovery posture

Automatic orientation ships because the paired relevant-memory gains are exact
and repeatable in the controlled set, the frozen negative cases add no false
positives, and the representative supported ceiling has no deadline misses and
meets both warm latency targets.

The recovery path remains deliberately small:

- remove only the Codex `UserPromptSubmit` handler to disable automatic
  orientation;
- retain the canonical static checkpoint, compact recovery handler, existing
  integrated stores, and `memory orient` manual command;
- use `memory recall <scope> <regex>` for canonical raw history above the
  orientation ceiling;
- make no data conversion or cleanup change when disabling the feature.

Real-session observation remains follow-up work. It should record useful and
missed recalls, stale or distracting evidence, hook delivery, deadline misses,
and whether the fixed ceiling remains appropriate. A repeated false-positive
or latency problem should first disable the prompt handler, not trigger a
heavier retrieval subsystem.

## Reproduce

```sh
uv run --locked python -m unittest tests.test_orientation tests.test_cli
uv run --locked python evaluation/orientation_trials.py
uv run --locked python benchmarks/orientation_latency.py
```
