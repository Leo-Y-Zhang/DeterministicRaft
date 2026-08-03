# TDD — RaftVerified

**Status:** built (v1.2.0)
**Date:** 2026-08-03 · **PRD:** [PRD.md](PRD.md) · **Repo:** RaftVerified

> Written from the code, not from the README. Every claim below names the module it
> lives in; where the code and the marketing disagreed, the code won.

## Approach

One process, one seeded `random.Random`, one virtual clock. `Simulator` (`sim.py`) is a
priority queue of callbacks keyed by `(virtual_time, sequence)`; one *step* pops and runs
exactly one callback. Everything that could be nondeterministic — message delay, drop,
duplication, election timeouts, which node a client tries, fault-driver decisions — draws
from that single RNG in a fixed textual order, so `(nodes, seed, faults, steps)` fully
determines the run. `Network` sits between nodes and applies the faults; `RaftNode`
(`node.py`) is the algorithm alone, with no clock, no I/O and no RNG of its own, which is
the property that makes it simulable. `Cluster` (`cluster.py`) wires N nodes to one
network, runs the client/fault/membership/nemesis drivers, and — this is the part that
matters — calls `InvariantChecker.check()` after *every* single step, not at the end.

Three verification layers sit on top, all of them pure functions that draw zero
randomness so enabling them cannot perturb a run:

1. `invariants.py` — the paper's five safety properties plus commit-index monotonicity
   and a per-configuration commit quorum, over node *logs*.
2. `linearizability.py` — a Wing–Gong linearize-and-remove oracle over the *client*
   history, which sees things the log invariants structurally cannot (a stale read is
   perfectly legal in every log).
3. `shrink.py` — ddmin over the fault injections plus a binary search on the step
   budget, turning a failing 20,000-step seed into a minimal, replayable counterexample.

## Data model

No database, no files, no schema, no migrations. The persisted structures are all
in-memory dataclasses; "stable storage" is the subset of `RaftNode` fields that a
simulated crash does not clear.

| Structure | Where | Fields that matter |
|---|---|---|
| `Entry` | `node.py` | `term: int`, `command: str` — frozen; `command` is opaque to Raft |
| `Command` | `kv.py` | `client_id`, `req_id`, `op ∈ {put,get,cas}`, `key`, `value`, `expected`; encodes to a 6-field `:`-separated string. **`decode` is total** — anything malformed becomes a NOOP, so the state machine can never raise on a log entry |
| configuration entry | `node.py` | an ordinary log entry whose command is `cfg:0,1,2`; `Command.decode` sees a NOOP, the Raft layer decodes the voting set |
| `Snapshot` | `kv.py` | `last_index`, `last_term`, `store`, `sessions`, `voters` — the configuration is inside the snapshot, because membership must survive compaction |
| `HistoryEntry` | `kv.py` | one client operation: invoke step, return step, observed result. `return_step is None` ⇒ never completed |
| `RunResult` | `cluster.py` | `trace: list[str]` (the digest input), `events` (the timeline input), `stats`, `final` |
| `NemesisSchedule` | `nemesis.py` | frozen tuple of frozen patterns; `to_json` / `from_json` round-trip exactly and that JSON string *is* the replay token |

**Persistent vs volatile** (`node.py`, `pause`/`_reset_volatile`/`resume`) — this split is
the whole point of crash-restart, so it is written out rather than implied:

- **Survives a crash:** `term`, `voted_for`, `log`, `base_index`, `base_term`, `snapshot`.
- **Lost on a crash:** `role`, `commit_index`, `last_applied`, `applied`, the KV state
  machine, `leader_id`, `next_index`, `match_index`, `_votes`, `_client_index`,
  `_pending_reads`, `_committed_this_term`.
- **Derived on restart:** the state machine is rebuilt from the snapshot and re-applied
  log; the *configuration* is re-derived from the latest config entry in the log (falling
  back to the snapshot's, then the initial set), so membership needs no separate
  persistence.
- `incarnation` increments on every restart. The checker keys its per-node caches on it,
  so a legitimately reset commit index is not mistaken for a monotonicity violation.

Two counters exist purely to make verification cheap and correct:
`log_version` (bumped on any log mutation — the checker caches expensive pairwise
comparisons on it) and `incarnation` (above).

## Interfaces

```python
# sim.py
Simulator(seed: int)                     # .rng, .now, .steps, .schedule(delay, fn), .step()
Network(sim, node_ids, profile, deliver, record)
PROFILES: dict[str, FaultProfile]        # "none" | "light" | "chaos"

# node.py — the algorithm; no clock, no I/O, no RNG of its own
RaftNode(node_id, peer_ids, sim, send, record, config=DEFAULT_CONFIG,
         on_apply=None, bugs=NO_BUGS, initial_voters=None)
  .start() / .pause() / .resume()
  .client_command(command: str) -> bool          # False unless this node believes it leads
  .change_config(new_voters: tuple[int, ...]) -> bool
  .request_read(command: str) -> bool            # ReadIndex (§8)
  .handle(src: int, msg: Message) -> None        # the only entry point for the network
  .last_log_index() / .term_at(i) / .entry_at(i) # logical, 1-based, compaction-aware

# cluster.py
Cluster(num_nodes=5, seed=0, faults="none", config=DEFAULT_CONFIG, client_interval=100,
        record_history=True, bugs=NO_BUGS, suppressed=frozenset(), read_index=False,
        membership=False, initial_voters=None, nemesis=None)
  .run(steps) -> RunResult                       # steps, checking invariants after each
  .run_until(pred, max_steps=50_000) -> bool

# invariants.py
InvariantChecker(seed, replay_hint="").check(nodes: Mapping[int, NodeView], step: int)
  # raises InvariantViolation(invariant, detail, seed, step, replay)

# linearizability.py
check(history, *, max_ops=None, budget=500_000) -> LinearizabilityResult

# shrink.py
shrink(scenario: Scenario, *, target=None) -> Counterexample | None
```

`NodeView` (`invariants.py`) is a `Protocol`, not a base class: the checker depends only
on a narrow read-only surface (`role`, `term`, `log`, `commit_index`, `applied`,
`log_version`, `incarnation`, `base_index`, `base_term`, `voters`, three log accessors).
That is what lets `tests/test_invariants.py` plant divergence with fake nodes and prove
the checker *catches* it — a checker that has never been seen to fail proves nothing.

### The determinism contract

These are invariants of the codebase, enforced by tests rather than by convention:

1. **Append, never insert.** A new RNG draw goes at the end of an existing draw sequence.
   Inserting one shifts every subsequent value and rewrites history. `tests/goldens.json`
   pins both the trace digest *and* the RNG draw count for ~12 configurations
   (`tests/test_goldens.py` instruments `random.Random` to count).
2. **Sort before any draw, message or trace line.** Any iteration over node ids,
   `next_index`, configurations or session dicts that feeds an RNG draw, a message or the
   trace is `sorted()` first.
3. **Oracles are pure.** The invariant checker, the linearizability oracle, the shrinker's
   candidate replays and the HTML report draw zero randomness. `record_history` gates only
   whether the list is kept, never any behaviour.
4. **Suppression is ordinal, not positional.** Each fault injection takes the next integer
   from `Cluster._fault_seq` *after* all of its RNG draws have happened
   (`_fire_fault`). The shrinker suppresses ordinals; the draws still occur, so an empty
   mask is byte-identical to an unshrunk run and a masked run does not desync the stream.
5. **New RPC rounds are opt-in.** ReadIndex and snapshots have their own message types and
   their own config flags precisely so that a default run is byte-identical to one from
   before they existed.

## Access control

Not applicable, and worth being explicit about why rather than deleting the heading: there
is no server, no database, no authentication, no multi-tenancy and no stored data. Nothing
grants anything to anyone.

The security surface that *does* exist is small and worth naming:

- **`--nemesis JSON` is the only untrusted input.** Parsed with `json.loads` into frozen
  dataclasses that validate in `__post_init__`; unknown pattern names, non-integer fields
  (with `bool` explicitly excluded, since `True` is an `int` in Python), out-of-range
  probabilities and runaway `cycles` are all rejected before the first simulator step. A
  schedule naming a node the cluster does not have is rejected by `Cluster.__init__`.
  Nothing is `eval`'d or imported by name.
- **File writes** happen only to paths the caller passes (`--timeline`, `--out`).
- **The injectable-bug registry is not reachable from the CLI.** `bugs.py` is a library
  parameter used by tests. There is deliberately no `--bug` flag, so no invocation of the
  shipped binary can arm a defect.

## Migrations

None — there is no persistent store to migrate. The equivalent risk in this project is
**rebaselining the golden corpus**: changing `tests/goldens.json` silently is exactly as
dangerous as editing an applied migration, because it destroys the evidence that a change
was determinism-neutral. The rule the repo follows is that a rebaseline lands in one
reviewable commit that shows *only* the intended configurations moving, with the reason
stated. The `Harmonia → RaftVerified` rename is the worked example: the HTML report golden
moved because the page embeds the product name, and the commit proves it by showing that
substituting the old name back into the new page reproduces the old digest exactly.

## Failure modes

| What breaks | Who notices | How we detect it | How we undo it |
|---|---|---|---|
| An RNG draw is inserted mid-stream by a new feature | Anyone replaying an old seed; every pinned trace | `tests/test_goldens.py` — digest **and** draw-count mismatch | Move the draw to the end of its sequence; rebaseline only if the change was intended, in its own commit |
| The invariant checker is not generalised in lockstep with a feature (snapshots' compacted prefix, membership's per-node majorities) | Nobody — this is the dangerous one: it presents as a **false green** | Planted-bug tests with fake nodes (`tests/test_invariants.py`, `tests/test_membership.py`) that must still catch a cross-boundary / cross-configuration divergence | Revert the feature; the checker change ships in the same commit as the feature by rule |
| A client-visible defect that no log invariant can express (stale read, lost acknowledged write, double-applied retry) | The client, in a real system | The linearizability oracle; `bugs.stale_local_reads` is the standing positive control that must be missed by all five invariants and caught only by the oracle | Fix the read path; the oracle test is the regression |
| The oracle's search budget (500,000 states) is exhausted | The user, as a bogus "NOT LINEARIZABLE" | Nothing — **known defect**, see below | Raise `budget`, or bound with `max_ops` |
| A hand-authored nemesis schedule is malformed | The user | Total validation before step 0; exit code 2 with the specific field named | Fix the JSON |
| A counterexample from an *armed* bug prints a replay command that does not reproduce it | Whoever pastes the command | Nothing — the hint carries `--membership` and `--nemesis` but not the bug flags, which have no CLI representation | Only affects deliberately-injected bugs (test-only); a genuine defect's hint is exact |
| A shrink run reports a different failure from the original | The user | `shrink()` refuses to proceed unless the scenario's signature matches the target, and returns `None` otherwise | Nothing to undo; the shrinker never mutates the live run |

**Known defect (not fixed here, recorded deliberately):** `linearizability.check` returns
`linearizable=False` for two different situations — a genuine counterexample, and an
exhausted search budget whose result is *undetermined*. `shrink.failure_signature` maps
both to the signature `"nonlinearizable"`, so an undetermined search would be delta-debugged
as if it were a real violation. No configuration in the current corpus approaches the
budget, so this has never fired. The fix is a three-valued verdict, not a bigger budget.

## Rollback

There is no deployment, so rollback is `git revert` plus `pip install -e .` — seconds, and
the previous version is byte-identical because there is no state to migrate. The
substantive question is *how we would know* a change was wrong, and the answer is the
golden corpus: a revert that restores the pinned digests and RNG draw counts restores the
exact previous behaviour, provably. That is why rebaselining goldens is treated as the
irreversible operation in this repo and reverting code is not.

The rename in this session is reversible the same way: rename the package back, and the
report golden returns to its previous digest (demonstrated by name substitution before the
re-pin, not assumed).

## Test plan

469 tests across 24 files, plus one long sweep behind the `slow` marker. The ones that
would fail without the corresponding piece of the design:

- **Positive — the legitimate thing still works.** 100-seed chaos sweeps stay clean;
  bounded liveness holds under `none`/`light`; legal compaction and legal reconfiguration
  do **not** false-positive the checker.
- **Negative — the thing being prevented is prevented.** Each of the six injectable bugs
  trips exactly the property it targets (`tests/test_bugs.py`), and again under *directed*
  nemesis campaigns (`tests/test_nemesis.py`). The May-2015 membership bug is reproduced
  both as a hand-driven mechanism and as an in-the-wild 1000-seed hunt, with the guarded
  twin surviving the identical campaign (`tests/test_membership.py`).
- **Boundary.** Log indexing across a compaction boundary at `base_index == 0` and above
  (`tests/test_log_offset.py`, `tests/test_snapshots.py`); commit-index reset across a
  crash incarnation (`tests/test_persistence.py`); empty and single-op histories in the
  oracle (`tests/test_linearizability.py`); malformed nemesis JSON in every rejected shape
  (`tests/test_nemesis.py`); a report whose replay command must carry `--membership` and
  `--nemesis` or it would document one run and replay another (`tests/test_report.py`).
- **Determinism.** Run-twice-compare per feature, the pinned golden matrix, and a real
  subprocess invocation of `python -m raftverified` (`tests/test_cli.py`) — which is also
  the only test that would catch cross-process nondeterminism such as hash-order leakage.
  That residual risk is currently low because every set and dict that feeds a draw, a
  message or the trace is keyed by `int` or sorted first, but it is not separately proven.

## Build order (as actually built)

Determinism tripwire and tooling gate → KV state machine and client history → exactly-once
sessions → linearizability oracle → injectable-bug registry → ddmin shrinker → real
crash-restart → log base-offset abstraction → snapshots + `InstallSnapshot` (checker
generalised in the same commit) → ReadIndex and single-server membership → HTML report and
the nemesis vocabulary. The ordering rule throughout: nothing that changes indexing lands
before the abstraction that hides indexing, and no feature lands before the checker that
can judge it.
