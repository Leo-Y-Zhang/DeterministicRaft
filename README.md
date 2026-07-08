# Harmonia

*Harmonia — the goddess of concord and agreement; this one drives five
quarrelling nodes to agreement and proves they got there safely.*

**The Raft consensus algorithm, verified by deterministic simulation
testing.** An educational, paper-faithful implementation of Raft (Ongaro &
Ousterhout, *In Search of an Understandable Consensus Algorithm*, USENIX ATC
2014) where the entire cluster runs inside a seeded discrete-event simulator
— so every message drop, partition and election is perfectly reproducible
from its seed, and the paper's five safety properties are machine-checked
after **every single simulator step**.

```
$ harmonia check --seeds 300 --faults chaos
seeds=300 faults=chaos invariant_checks=1500000 violations=0
```

1.5 million invariant evaluations across 300 adversarial network schedules;
zero violations. When an invariant *does* fail during development, the run
prints its seed and step — `harmonia replay --seed N` reproduces the failure
exactly, every time. This is the testing approach pioneered for real systems
by FoundationDB and TigerBeetle (no affiliation — the technique is the
inspiration).

## Why this exists

Distributed consensus bugs are notoriously unreproducible: they hide in
message interleavings that unit tests never explore. Deterministic
simulation inverts the problem — the network *is* the test harness. Harmonia
is a compact, readable demonstration of that idea applied to the most
teachable consensus algorithm.

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
reordering, partitions, crashes), every node's log is **byte-identical up to
the commit index** (matching SHA-256 through entry 111) — while a stale
candidate campaigns in term 212 and the leader carries one not-yet-committed
entry. That is Raft's safety guarantee, visible in a shell.

Timeline visualizations (hand-rolled SVG, committed under `assets/`):

| clean election (`--faults none --seed 7`) | light faults (`--seed 11`) | chaos (`--seed 42`) |
|---|---|---|
| ![clean](assets/clean-seed7.svg) | ![light](assets/light-seed11.svg) | ![chaos](assets/chaos-seed42.svg) |

## The five safety properties → checked invariants

| Paper property | Enforced by |
|---|---|
| Election Safety — at most one leader per term | `invariants.py: election_safety`, asserted every step |
| Leader Append-Only — a leader never overwrites its own log | `invariants.py: leader_append_only` |
| Log Matching — same index+term ⇒ identical logs up to it | `invariants.py: log_matching` |
| Leader Completeness — committed entries survive into future leaders' logs | `invariants.py: leader_completeness` |
| State Machine Safety — no two nodes apply different commands at an index | `invariants.py: state_machine_safety` |

Each also has focused unit tests (forced log divergence and repair, stale
leaders, split votes), plus a bounded liveness check: under `none`/`light`
faults, commands must commit within a step budget.

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

Exit codes: `0` success / no violations, `1` invariant violation (with seed
and step), `2` usage errors.

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

Design rule: `node.py` contains *only* the algorithm — no I/O, no clocks,
no randomness of its own — which is exactly what makes it simulable.

## Honest limitations

- **No cluster membership changes** (single-config only) and **no log
  compaction/snapshots** — the two major Raft extensions are out of scope.
- **No disk persistence**: "stable storage" is simulated in memory; crash
  recovery restores from that simulated store.
- Fault injection covers message-level faults and node crash/restart; it
  does not model Byzantine behaviour (Raft assumes non-Byzantine nodes).
- Liveness is checked only under bounded fault profiles — under sustained
  chaos, Raft (correctly) prioritizes safety over progress.

## Roadmap

- Membership changes (joint consensus) with invariants extended to config
  overlap.
- Log compaction with InstallSnapshot.
- A nemesis vocabulary for hand-authored fault scenarios.
- Linearizability checking of client reads.

## License

MIT — Copyright (c) 2026 GreenPandaTech.
