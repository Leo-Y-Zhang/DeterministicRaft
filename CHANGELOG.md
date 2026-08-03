# Changelog

## Unreleased

- **Renamed: Harmonia -> RaftVerified.** The Python package (`harmonia/` ->
  `raftverified/`), the distribution name, the console script and the CLI's
  printed prefixes all move with it; the `harmonia` command no longer exists.
  Entries below are the original release notes with module paths and command
  names rewritten to the current ones - the code they describe is unchanged.
  No behavioural change: the golden trace digests and rng-draw counts in
  `tests/goldens.json` are unchanged across the rename.

## 1.2.0 - 2026-07-31

The nemesis vocabulary: declarative, hand-authored fault schedules as first-class
citizens of the existing determinism machinery (Jepsen's nemesis is the inspiration;
no affiliation).

- **`raftverified/nemesis.py`**: five composable fault patterns (`partition_halves`,
  `isolate_leader` resolved at fire time, `flapping_link`, `lossy_link`,
  `crash_node`) as frozen, construction-validated dataclasses pinned to virtual-time
  instants; a `NemesisSchedule` serializes to a compact JSON form that round-trips
  exactly. Patterns draw zero randomness of their own, so a scheduled run replays
  byte-identically and layers deterministically over any fault profile (default and
  membership golden corpora stay byte-identical, asserted; nemesis configs get their
  own pinned digests).
- **Same suppression mask as the random driver**: every injection consumes a
  `_fire_fault` ordinal, so the ddmin shrinker minimises hand-authored schedules
  with no parallel mechanism, and `Scenario` carries the schedule through
  shrink/replay. State-dependent injections with nothing to do (no believed leader,
  node already down) skip without consuming an ordinal.
- **CLI `--nemesis JSON` on all four commands**; a violation's printed replay hint
  quotes the schedule back verbatim and parses back through the real parser to the
  identical schedule (asserted end-to-end). Validation is total and exits 2 on any
  bad schedule: unknown patterns, missing/extra fields, non-integer fields (a
  fractional node id previously escaped as a mid-run KeyError), out-of-range
  probabilities, and flap cycles above `MAX_FLAP_CYCLES` (10000; each cycle
  materialises one injection up-front, so a runaway value was a memory DoS).
- **The bug registry, re-caught under direction** (`tests/test_nemesis.py`): a
  hand-authored campaign of minority-starving halves splits, leader isolations and
  a crash over `light` noise (which injects no partitions or crashes of its own)
  catches `vote_for_stale_candidate` -> LeaderCompleteness, `skip_log_consistency`
  -> LogMatching/StateMachineSafety, `allow_commit_regression` ->
  CommitIndexMonotonic, `stale_local_reads` -> the linearizability oracle only, with
  the unbugged twin clean and linearizable over the same campaign. The May-2015
  membership bug reproduces under directed 3|3 halves splits at six servers at
  pinned seed 171 (`drop_config_commit_guard`); the guarded twin survives the
  identical campaign at seeds 170-172. `drop_commit_term_guard` (Figure 8) is
  deliberately not searched for under nemesis - no driven search in this repo
  reliably reproduces the full prior-term overwrite - and stays the deterministic
  mechanism test in `tests/test_bugs.py`.
- **Report footer fixed**: the HTML report's replay command now carries
  `--membership` and `--nemesis` (it previously omitted `--membership` since 1.1.0,
  and would have omitted `--nemesis`), so pasting it reproduces the documented run,
  not a fault-free lookalike; the default report golden is byte-identical (pinned).
- mypy `--strict` made clean again (an uninferable lambda in `cluster.py` was
  replaced by a typed helper, proven digest-neutral by the pinned golden corpus).
- 407 -> 469 tests plus the marked slow sweep; ruff and mypy `--strict` clean.

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
- **Linearizability oracle** (`raftverified/linearizability.py`): Wing-Gong
  linearize-and-remove over the client history, catching stale reads,
  acknowledged-but-lost writes and double-applied retries that the internal
  invariants cannot express.
- **Injectable-bug harness** (`raftverified/bugs.py`): five deliberately-planted
  consensus bugs, all off by default, each caught by exactly the property it targets
  (four internal invariants, one only by the oracle) - proof the checker works.
- **Automatic schedule shrinker** (`raftverified/shrink.py`): ddmin over the fault
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
- **`raftverified report`**: a self-contained HTML report (summary + timeline + verdict).
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
- CLI: raftverified run / check / replay - every run reproducible from its
  seed; run --timeline emits a dependency-free SVG timeline.
- 151 pytest tests plus a marked long sweep; mypy clean; stdlib-only
  runtime.
