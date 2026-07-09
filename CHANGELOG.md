# Changelog

## 1.0.0 - 2026-07-09

From an internal-invariant Raft simulator to a Jepsen-grade, self-minimizing DST
consensus testbed. Everything below is verified by the invariant checker (run after
every simulator step) and stays perfectly reproducible from a seed.

- **Replicated key-value state machine** (`put` / `get` / `cas`) with structured
  commands, per-client **exactly-once sessions** (Ongaro 6.3), and a recorded
  client-operation history.
- **Linearizability oracle** (`harmonia/linearizability.py`): Wing-Gong
  linearize-and-remove over the client history, catching stale reads,
  acknowledged-but-lost writes and double-applied retries that the internal
  invariants cannot express.
- **Injectable-bug harness** (`harmonia/bugs.py`): five deliberately-planted
  consensus bugs, all off by default, each caught by exactly the property it targets
  (four internal invariants, one only by the oracle) - proof the checker works.
- **Automatic schedule shrinker** (`harmonia/shrink.py`): ddmin over the fault
  injections plus a step-budget binary search, delta-debugging any failure to a
  minimal, replayable counterexample; deterministic fault-suppression mask.
- **Real crash-restart**: a crash discards all volatile state (only currentTerm,
  votedFor and the log persist); restart rebuilds the state machine from the log.
  The checker became crash-aware via per-node incarnations.
- **Log compaction + `InstallSnapshot`** (section 7): committed prefixes fold into a
  snapshot; lagging followers are re-seeded; the invariant checker was generalized in
  lockstep to reason over (compacted prefix + live tail).
- **ReadIndex linearizable reads** (section 8): a get served from local state only
  after a confirmed heartbeat round and a current-term commit - no log entry.
- **`harmonia report`**: a self-contained HTML report (summary + timeline + verdict).
- Tooling: golden-digest determinism tripwire + rng-draw guard, ruff, mypy `--strict`,
  CI. 151 -> 342 tests, stdlib-only runtime.

## 0.1.0 - 2026-07-07

Initial release.

- Discrete-event simulator: virtual clock, priority event queue, seeded
  RNG; simulated network with message drop, duplication, delay, reordering
  and partitions, driven by fault profiles (none / light / chaos).
- Raft node state machine per the 2014 paper: randomized election
  timeouts, RequestVote / AppendEntries, log replication, majority commit
  with the current-term guard (figure 8), nextIndex backoff log repair.
- Invariant checker run after every simulator step, enforcing the paper's
  five safety properties: Election Safety, Leader Append-Only, Log
  Matching, Leader Completeness, State Machine Safety.
- Bounded liveness check under none/light fault profiles.
- Hand-rolled SVG timeline renderer (terms, elections, commits,
  partitions per node over virtual time).
- CLI: harmonia run / check / replay - every run reproducible from its
  seed; run --timeline emits a dependency-free SVG timeline.
- 151 pytest tests plus a marked long sweep; mypy clean; stdlib-only
  runtime.
