# Changelog

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
- CLI: raftlab run / check / replay - every run reproducible from its
  seed; committed example timelines under assets/.
- 151 pytest tests plus a marked long sweep; mypy clean; stdlib-only
  runtime.
