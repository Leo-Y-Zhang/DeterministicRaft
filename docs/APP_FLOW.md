# App Flow — the RaftVerified command line

**Date:** 2026-08-03 · **PRD:** [PRD.md](PRD.md) · **TDD:** [TDD.md](TDD.md)

There is no GUI and no interactive prompt. The user-facing surface is one console script
with four subcommands, two file artefacts and three exit codes. This document describes
that surface exhaustively, because for a verification tool the *output* is the product:
a violation that cannot be reproduced from what was printed is a bug report nobody can act
on. Every transcript below is real output from the shipped version, not an illustration.

## Entry points

```bash
raftverified <command> [args]          # console script from the editable install
python -m raftverified <command>       # identical; no install step needed
```

`run`, `check`, `replay`, `report`. All four accept `--nodes`, `--faults
{none,light,chaos}`, `--steps`, `--membership` and `--nemesis JSON`. There is deliberately
no flag that arms an injectable bug — `bugs.py` is reachable only from the test suite, so
no invocation of the shipped binary can turn a defect on.

## The happy path

1. **`check`** — the ten-second version. Sweep many seeds and get one number.

   ```
   $ raftverified check --seeds 100 --faults chaos
   raftverified check: seeds 0..99 nodes=5 faults=chaos steps=5000
   seeds=100 faults=chaos invariant_checks=500000 violations=0
   ```

2. **`run`** — one seed, in detail: message and fault counts, the trace digest, and the
   final state of every node.

3. **`replay --seed N`** — the claim, made checkable. It runs the same configuration
   twice and compares the two event traces byte for byte.

   ```
   $ raftverified replay --seed 7 --faults chaos --steps 4000
   attempt 1 digest: sha256:fd5cd28b418e8db42cfed9fd13a865fea54d40c7489735c14056ec9294f7ae73
   attempt 2 digest: sha256:fd5cd28b418e8db42cfed9fd13a865fea54d40c7489735c14056ec9294f7ae73
   replay verified: 6042 trace events, byte-identical
   ```

4. **`report --out page.html`** — the shareable artefact: summary cards, fault and
   verification counts, the inline SVG timeline, the linearizability verdict, and a footer
   carrying the exact command that reproduces the run.

The loop this is all built for is step 5, and it is the only one that matters:

5. **A violation prints its own reproduction.** Real output, from the historical
   May-2015 membership bug armed at six servers, chaos seed 354:

   ```
   [LeaderCompleteness] leader n4 (term 84) is missing committed entry
   Entry(term=83, command='cfg:0,1,2,3,5') at index 60 (seed=354 step=5784)
   -- reproduce with: raftverified replay --nodes 6 --seed 354 --faults chaos --membership
   ```

   The message carries the property that failed, the concrete entry that was lost, the seed
   *and* the step, and a runnable command — the whole point of deterministic simulation
   compressed into one line. From a library script, `shrink()` then reduces it to a
   1-minimal fault set and the earliest failing step budget.

   One caveat, stated because it matters for exactly this transcript: the replay hint
   carries `--membership` and `--nemesis` but **not** the injectable-bug flags, since those
   have no CLI representation. For a genuine defect — the case the hint exists for — it
   reproduces exactly. For a deliberately-armed bug like the one above, the printed command
   runs the *fixed* algorithm and correctly finds nothing.

## Every state of every command

| Command | Nothing to do | Clean | Violation | Usage error |
|---|---|---|---|---|
| `run` | `--steps 0` prints a complete, honest zero report (`0 checks`, empty-string digest `e3b0c442…`) rather than nothing | full summary + final logs, exit **0**; with `--timeline`, `timeline written: <path>` | `INVARIANT VIOLATION: [name] detail (seed=… step=…) -- reproduce with: …`, exit **1** | exit **2** |
| `check` | `--seeds 0` prints the totals line with zeroes | `seeds=N … violations=0`, exit **0** | one `seed N: VIOLATION …` line per failing seed, then `reproduce the first failure with:` and the command, exit **1** | exit **2** |
| `replay` | — | both digests plus `replay verified: N trace events, byte-identical`, exit **0** | either attempt raising ⇒ `attempt K: INVARIANT VIOLATION: …`; or `replay FAILED: traces differ`, which means a determinism bug in RaftVerified itself, exit **1** | exit **2** |
| `report` | — | `report written: <path> (linearizable=True, N ops checked)`, exit **0** | violation before the page is written ⇒ nothing written, exit **1** | exit **2** |

**Usage errors are total and early.** Two layers produce them, both exit 2:

```
$ raftverified run --nodes 3 --steps 10 --nemesis '[{"pattern":"nope","at":1}]'
raftverified run: error: argument --nemesis: unknown pattern 'nope';
  expected one of ['crash_node', 'flapping_link', 'isolate_leader', 'lossy_link', 'partition_halves']

$ raftverified run --nodes 3 --steps 10 --nemesis '[{"pattern":"crash_node","node":9,"at":100,"duration":50}]'
raftverified: error: nemesis schedule names node n9; this cluster only has n0..n2

$ raftverified run --nodes 2 --membership --steps 100
raftverified: error: membership mode needs at least three nodes
```

The first is argparse (the schedule failed to parse or validate); the second and third are
`Cluster.__init__` rejecting a schedule or configuration the parser could not know was
wrong, caught in `main()` and reported as a usage error rather than a traceback. Every one
of them fires **before the first simulator step**, so a bad argument can never surface as
a mid-run crash. No stack trace ever reaches the user.

**There is no unauthorised state, no loading state and no offline state.** No network, no
account, no remote call. Saying so is more useful than inventing rows for them.

## Transitions

```mermaid
stateDiagram-v2
    [*] --> Parse
    Parse --> UsageError: bad flags / bad schedule (exit 2)
    Parse --> Construct
    Construct --> UsageError: schedule or config rejected (exit 2)
    Construct --> Stepping
    Stepping --> Stepping: one event, then all invariants
    Stepping --> Violation: an invariant fails (exit 1)
    Stepping --> Summary: step budget reached (exit 0)
    Summary --> Artefact: --timeline / report
    Violation --> Replay: paste the printed command
    Replay --> Shrink: shrink() in a script
    Shrink --> Violation: 1-minimal faults, earliest failing step
    Artefact --> [*]
    UsageError --> [*]
```

The `Violation → Replay → Shrink → Violation` cycle is the design centre of the tool. It
terminates because each pass is strictly smaller: ddmin returns a 1-minimal fault set, then
the step budget is binary-searched down to the earliest step at which the same failure
signature still fires.

## Dead ends

Two, both honest:

1. **A single-node cluster never commits.** `raftverified run --nodes 1` elects `n0`
   leader and accepts client commands, but `_advance_commit` is only ever called from
   `_on_append_reply` — with no peers, no reply ever arrives, so the commit index stays at
   0 forever (verified: 5000 steps, `submitted=3 committed=0`, `len=3`). Safety invariants
   hold trivially, so nothing reports a problem; the run simply does nothing. The same
   stall would apply to any configuration that shrank to one voter, which the membership
   churn driver cannot reach but a library caller could construct via `initial_voters`.
   This is a real defect, recorded here and in the TDD rather than papered over. The fix is
   to attempt a commit after a leader's own append, not only on a reply.
2. **The oracle's search budget produces a verdict the user cannot act on.** Exhausting
   500,000 states prints `NOT LINEARIZABLE` with the message "search budget exhausted;
   result undetermined". A user reading only the badge would chase a violation that may not
   exist. No current configuration reaches the budget; the fix is a three-valued verdict.

Neither is a state the user can get *stuck* in interactively — every command still
terminates and returns an exit code — but both are states where the output is less useful
than it looks, which is the same defect class.

## Accessibility and scripting

- **No ANSI colour anywhere** in the CLI output (verified: no escape sequences in the
  source). Nothing is conveyed by colour in the terminal, so the tool reads identically in
  a pipe, a log file, a CI transcript and a screen reader.
- **Exit codes are the machine-readable channel**: `0` clean, `1` violation, `2` usage
  error. CI depends on exactly this and nothing else.
- Every number printed is also available structurally (`RunResult.stats`) for callers who
  would rather not parse text.
- The columns in `final logs` are fixed-width and stable across runs, so a diff between
  two runs is readable line by line.
