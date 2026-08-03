# RaftVerified — what it has to do, and how you would check

v1.2.0. Reconstructed from the code and from the build spec at
`docs/superpowers/specs/to-the-max.md`. Every requirement below names the check that
proves it, so this document can be falsified by running something.

See also: [TDD](TDD.md) · [App Flow](APP_FLOW.md) · [Design Brief](DESIGN_BRIEF.md)

## Consensus bugs live in interleavings

They do not live in the code you can read. A drop here, a 40 ms delay there, a partition
that heals one step before an election completes — the combination that breaks Raft's
Leader Completeness may occur in one schedule out of a thousand, and when it does, an
ordinary test suite reports "assertion failed" with no way to see it again. Reading the
Raft paper does not fix this either: the paper's five safety properties are stated over
cluster state at an instant, and nothing in a normal unit test ever evaluates them.

Two people have that problem here. The author, a student implementing Raft from the 2014
paper, who wants to know whether the implementation is *actually* safe rather than
whether it passes a handful of hand-written scenarios — and who needs a failure to be
reproducible before it is worth debugging. And a reader assessing the work, an engineer
or an admissions tutor with ten minutes, no context and a healthy suspicion of README
claims, who needs to check the thing from a clean clone without trusting anybody.

There are no other users. No deployment, no server, no account system, no data belonging
to anyone; the "cluster" is five Python objects in one process.

## The verifier has to be shown to fail

The worst outcome for a project like this is not a crash. It is a **false green** — the
checker reports safety on a run that was not safe, and the author learns Raft wrong. A
verifier that cannot be demonstrated to catch anything is worthless, and no amount of
green output distinguishes the two cases.

So the repository carries six deliberately injectable consensus bugs (`raftverified/bugs.py`),
and each must be caught by exactly the property it breaks — asserted in `tests/test_bugs.py`
and `tests/test_nemesis.py`, not claimed in prose. One of them, `stale_local_reads`, must
be **missed by every internal invariant** and caught only by the linearizability oracle.
That bug is the reason the oracle exists as a separate check over the client history
rather than as another invariant over node state.

## What a stranger can run

Each of these is a command, from a clean clone.

- [x] A run is reproducible from a seed alone: `raftverified replay --seed 42 --faults chaos`
      runs the configuration twice and reports byte-identical event traces. It does —
      6042 trace events at seed 7, digest `fd5cd28b…`, from the CI step that ships.
- [x] The paper's five safety properties are evaluated **after every simulator step**, not
      at the end. `raftverified check --seeds 100 --faults chaos` reports
      `invariant_checks=500000` for 100 × 5000 steps: one check per step, zero violations.
- [x] A violation names its own reproduction. `InvariantViolation` carries the seed, the
      step and a complete replay command, including `--membership` and the `--nemesis`
      schedule quoted back verbatim (`tests/test_invariants.py`, `tests/test_report.py`).
- [x] A failing seed becomes a minimal counterexample automatically: ddmin over the fault
      injections to a 1-minimal set, then a binary search on the step budget
      (`raftverified/shrink.py`, `tests/test_shrink.py`).
- [x] What *clients* saw is checked independently of what the logs did, by a
      linearizability oracle over the recorded client history
      (`raftverified/linearizability.py`).
- [x] The whole gate is one CI job: ruff, mypy `--strict`, 469 pytest tests, a 100-seed
      chaos sweep and a replay-determinism check.

## Requirements

**Must**

- Every source of nondeterminism draws from one seeded RNG stream owned by the simulator.
  No wall clock, no threads, no ambient randomness anywhere in the algorithm.
- The Raft node module contains the algorithm and nothing else — no I/O, no clock, no RNG
  of its own. This is what makes it simulable; it is a design rule, not a style
  preference (`raftverified/node.py`).
- Safety invariants evaluated after *every* step, with the checker reading node state and
  never mutating it.
- Golden digests and RNG-draw counts pinned in `tests/goldens.json`, so an accidental
  change to the random stream fails a test rather than silently changing history.
- Zero runtime dependencies. Python 3.11+ standard library only.

**Should**

- A visual artefact per run: an SVG timeline and a single self-contained HTML report, both
  pure functions of the run so they are byte-reproducible.
- A vocabulary for *directed* chaos — the nemesis schedules — as well as random chaos, so
  a specific adversarial story can be written down, replayed and shrunk.

Deliberately unbuilt, and listed so nobody looks for it: joint-consensus (C-old,new)
membership, because single-server changes teach the same quorum-overlap lesson with far
less determinism surface; real disk I/O for "stable storage", which is modelled in memory;
Byzantine faults, since Raft assumes non-Byzantine nodes and simulating them would test
nothing; and any GUI, web dashboard, gRPC transport or multi-Raft sharding.

## Three things this is not

**Not a production consensus library.** This is an educational implementation with no
network transport, no persistence to disk and no operational tooling. Nobody should run
it as infrastructure, and the README says so in the same words.

**Not a general model checker.** The linearizability oracle is a bounded *detector*: it
searches within a states-explored budget, over the KV workload actually generated here.
It is sound for the histories it checks and makes no claim beyond them.

**Not a proof.** Nothing here proves Raft correct. It is empirical — a large number of
adversarial schedules, each checked exhaustively, each replayable. That distinction is
stated in the README's Scope & limitations section and is softened nowhere in this repo.

## The alternatives, and what each would have cost

| Alternative | What taking it would have cost |
|---|---|
| Wall-clock leader leases for reads (§6.4) | A real clock destroys reproducibility, which is the entire premise. Message-driven ReadIndex (§8) gives the same guarantee with no clock. |
| Hypothesis or another third-party fuzzer | Would add a runtime dependency, and the seeded sweep plus hand-rolled ddmin already *is* the equivalent. Writing the shrinker was also the point. |
| Threads or asyncio for the "network" | Destroys determinism outright. A discrete-event queue over a virtual clock gives the same interleavings, reproducibly. |
| Real files for stable storage | Injects OS-level nondeterminism (fsync ordering, timing) for no correctness gain; crash-restart semantics are modelled exactly without it. |
| Joint consensus (C-old,new) | Roughly doubles the configuration state space and the determinism surface to teach the same quorum-overlap lesson single-server membership already teaches. Kept on the roadmap, honestly, rather than half-built. |
| A richer replicated state machine | A KV store with `put`/`get`/`cas` is the smallest state machine on which linearizability is *interesting* — CAS makes retries observable. Anything larger adds surface without adding a lesson. |
| A web dashboard | The SVG timeline and the standalone HTML report carry the whole visual story with zero dependencies and no server. |

## What it touches

No personal data: no user, no account, no database, no telemetry, no network. It writes
only files the caller names (`--timeline OUT.SVG`, `--out OUT.HTML`) plus stdout, and
creates or reads nothing else.

Exactly one untrusted input crosses the boundary: the `--nemesis` JSON schedule from the
shell. It is validated totally before the first simulator step — unknown pattern,
missing, extra or non-integer field, out-of-range probability, runaway flap count — and
rejected as a usage error, so a malformed schedule can never become a mid-run crash. It
is parsed with `json.loads` into frozen dataclasses; nothing is `eval`'d.

Revocation does not apply, because there is no access to revoke. That is recorded
explicitly so a future version that grows a server surface has to come back and change
this line.

## A known defect, recorded rather than quietly fixed

Budget exhaustion is reported as a violation. `linearizability.check` returns
`linearizable=False` with the message "search budget exhausted; result undetermined", and
`shrink.failure_signature` maps any non-linearizable result to the signature
`"nonlinearizable"`. An undetermined search would therefore be shrunk as if it were a
real counterexample.

No run in the current corpus reaches the 500,000-state budget, so this has never fired.
The correct design is a third verdict — `linearizable` / `not linearizable` /
`undetermined` — with the callers branching on it.

