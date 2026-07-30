# Changelog

## 1.1.0 - 2026-07-30

Single-server cluster membership changes (Ongaro dissertation ch. 4), plus the real
May-2015 membership bug as the sixth injectable.

- **Single-server membership changes**: a configuration entry takes effect the moment
  it is appended (pre-commit); elections, commits and ReadIndex confirmations count
  majorities over the current configuration. Two leader guards make that safe: one
  change in flight at a time, and no change before a current-term commit (the
  May-2015 raft-dev amendment). Membership is derived state - rebuilt from the
  log/snapshot across crash-restart, truncation, compaction and `InstallSnapshot`
  (whose wire format now carries the configuration at the snapshot index; the
  snapshot golden matrix was rebaselined once for it).
- **Membership mode** behind `Cluster(membership=True)` / CLI `--membership` (all
  four commands): starts one server outside the configuration and runs a
  deterministic, zero-randomness churn driver that proposes single-server changes to
  every node that believes it is leader. Own golden-digest matrix; default goldens
  byte-identical (asserted).
- **Checker generalized in lockstep** (same commit as the feature): per-configuration
  majorities plus a new machine-checked property, CommitQuorum - a newly committed
  entry must be held by a majority of the committing node's current configuration.
  Planted-bug FakeNode tests prove catch and no-false-positive.
- **The historical injectable** (`drop_config_commit_guard`): resurrects the
  dissertation-published algorithm (no current-term-commit guard). A hand-driven test
  replays Ongaro's raft-dev example exactly (disjoint majorities, committed entries
  overwritten, Leader Completeness fires); a pinned natural repro (6 servers, chaos
  seed 354) trips it in ordinary churn and ddmin shrinks it to a 1-minimal
  counterexample; a 5-server control sweep shows the bug needs the sixth server.
- Test-count corrections: 1.0.0 shipped 343 tests (not the 342 stated below);
  now 343 -> 407 tests plus the marked slow sweep.

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
