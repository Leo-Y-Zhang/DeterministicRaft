# Harmonia - Raft consensus, verified by deterministic simulation testing

[![CI](https://github.com/GreenPandaTech/Harmonia/actions/workflows/ci.yml/badge.svg)](https://github.com/GreenPandaTech/Harmonia/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A paper-faithful implementation of the Raft consensus algorithm (Ongaro &
Ousterhout, *In Search of an Understandable Consensus Algorithm*, USENIX ATC
2014) where the entire cluster runs inside a **seeded discrete-event
simulator**. Because the simulator owns the clock, the RNG and the network,
every message drop, delay, reorder, partition and crash is perfectly
reproducible from a single seed — and the paper's five safety properties are
machine-checked after *every* simulator step. The non-obvious part: the
network is not something the tests work around, it *is* the test harness, so
consensus bugs that normally hide in rare message interleavings get surfaced
and reproduced on demand.

*Harmonia is the Greek goddess of concord and agreement; this one drives five
quarrelling nodes to agreement and proves they got there safely.*

```
$ harmonia check --seeds 300 --faults chaos
seeds=300 faults=chaos invariant_checks=1500000 violations=0
```

That is 1.5 million invariant checks (300 adversarial network schedules x
5000 steps) with zero violations. When an invariant *does* fail during
development, the run prints its seed and step, and `harmonia replay --seed N`
reproduces the failure byte-for-byte. The technique — deterministic
simulation testing — is the one FoundationDB and TigerBeetle use for real
databases (no affiliation; it is the inspiration).

## Why this exists

Distributed consensus bugs are notoriously hard to reproduce: they live in
message interleavings that ordinary unit tests never explore. Deterministic
simulation inverts the problem by making the network schedule an explicit,
seeded input. Harmonia is a compact, readable demonstration of that idea
applied to the most teachable consensus algorithm.

## What a run looks like

```
$ harmonia run --nodes 5 --seed 42 --faults chaos --steps 20000
...
  n0 up   role=candidate term=212 commit=111  applied=111  len=111  log sha256:fb2319200137
  n1 down role=follower  term=211 commit=111  applied=111  len=111  log sha256:fb2319200137
  n2 up   role=follower  term=211 commit=111  applied=111  len=111  log sha256:fb2319200137
  n3 up   role=leader    term=211 commit=111  applied=111  len=112  log sha256:8ac10e064571
  n4 up   role=follower  term=211 commit=111  applied=111  len=112  log sha256:8ac10e064571
```

Read that closely: after 20,000 steps of chaos (drops, duplicates, delays,
reordering, partitions, crashes), every node's log is byte-identical up to
the commit index — matching SHA-256 through entry 111 — while a stale
candidate campaigns in term 212 and the leader carries one not-yet-committed
entry. That is Raft's safety guarantee, visible in a shell.

Pass `--timeline out.svg` to any `run` to emit a dependency-free SVG timeline
(terms, elections, commits and partitions per node over virtual time):

```
harmonia run --nodes 5 --seed 42 --faults chaos --steps 20000 --timeline out.svg
```

## The five safety properties -> checked invariants

All five live in `harmonia/invariants.py` and are evaluated together after
every simulator step by `InvariantChecker`:

| Paper property | Checked by |
|---|---|
| Election Safety — at most one leader per term | `_check_election_safety` |
| Leader Append-Only — a leader never overwrites its own log | `_check_leader_append_only` |
| Log Matching — same index+term implies identical logs up to it | `_check_log_matching` |
| Leader Completeness — committed entries survive into future leaders' logs | `_check_leader_completeness` |
| State Machine Safety — no two nodes apply different commands at an index | `_check_state_machine_safety` |

Each property also has focused unit tests (forced log divergence and repair,
stale leaders, split votes), plus a bounded liveness check: under
`none`/`light` faults, submitted commands must commit within a step budget.

## Beyond the log: a client-observable linearizability oracle

The five properties above check what nodes' *logs* do. They cannot see whether
what *clients* observed is a legal single-copy history. Harmonia's replicated
state machine is a small key-value store (`put` / `get` / `cas`) with per-client
exactly-once sessions, and every run records a client history of
invoke/return/observed operations. `harmonia/linearizability.py` then decides
whether that history is **linearizable** — whether some total order of the
operations respects real-time ordering and makes every observed result legal
(Wing–Gong "linearize and remove", as in Jepsen/Knossos). Raft guarantees it, so
every `none`/`light`/`chaos` run passes; the oracle catches the client-visible
bugs — stale reads, acknowledged-but-lost writes, double-applied retries — that
the internal invariants structurally cannot express.

## Testing the tester: injectable bugs

A verifier is only trustworthy if it catches real defects, so `harmonia/bugs.py`
carries a registry of deliberately-injectable consensus bugs, **all off by
default** (an armed-with-`NO_BUGS` run is byte-identical to an un-armed one). Each
is caught by exactly the property it targets:

| Injected bug | Breaks | Caught by |
|---|---|---|
| `drop_commit_term_guard` | §5.4.2 Figure 8 (commit prior-term entries directly) | Leader Completeness / State Machine Safety |
| `vote_for_stale_candidate` | §5.4.1 election restriction | Leader Completeness |
| `skip_log_consistency` | §5.3 log matching | Log Matching |
| `allow_commit_regression` | commit-index monotonicity | Commit Index Monotonic |
| `stale_local_reads` | reads bypass the log (no leadership check) | **the linearizability oracle only** |

The last one is the point: it slips past every internal invariant and is caught
only by the client-history oracle. See `tests/test_bugs.py`.

## Install & run

Requires Python 3.11+. Zero runtime dependencies; dev deps are pytest and mypy.

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"   # Windows
# .venv/bin/python -m pip install -e ".[dev]"          # Linux/macOS

python -m pytest -q          # 151 tests (a longer sweep is marked slow)
python -m mypy harmonia       # clean

harmonia run --nodes 5 --seed 42 --faults chaos --steps 20000 --timeline out.svg
harmonia check --seeds 300 --faults chaos
harmonia replay --seed 42     # identical to run: byte-for-byte event trace
```

The `harmonia` console script is installed by the editable install; the same
commands run via `python -m harmonia ...` without it.

Exit codes: `0` success / no violations, `1` invariant violation (with seed
and step), `2` usage error.

## Architecture

```
harmonia/
  sim.py         discrete-event core: virtual clock, event queue, seeded RNG,
                 network faults (drop/duplicate/delay/reorder/partition)
  node.py        the Raft state machine: follower/candidate/leader,
                 RequestVote + AppendEntries, commit rules, log repair
  invariants.py  the five safety properties, evaluated after every step
  cluster.py     wires N nodes to the simulated network; fault profiles
  timeline.py    SVG timeline renderer (no dependencies)
  cli.py         run / check / replay
tests/           151 tests: unit, scenario, invariant sweeps, determinism
```

Design rule: `node.py` contains *only* the algorithm — no I/O, no clocks, no
randomness of its own — which is exactly what makes it simulable.

## Scope & limitations

- **No cluster membership changes** (single fixed configuration) and **no log
  compaction / snapshots** — the two major Raft extensions are out of scope.
- **No disk persistence**: "stable storage" is simulated in memory; crash
  recovery restores from that simulated store.
- Fault injection covers message-level faults and node crash/restart; it does
  not model Byzantine behaviour (Raft assumes non-Byzantine nodes).
- Liveness is checked only under bounded fault profiles — under sustained
  chaos, Raft correctly prioritizes safety over progress, so no liveness
  guarantee is asserted there.

## Roadmap

- Membership changes (joint consensus), with invariants extended to config
  overlap.
- Log compaction with InstallSnapshot.
- A nemesis vocabulary for hand-authored fault scenarios.
- Linearizability checking of client reads.

## License

MIT — Copyright (c) 2026 GreenPandaTech.
