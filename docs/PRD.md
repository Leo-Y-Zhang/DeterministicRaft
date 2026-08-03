# PRD — RaftVerified

**Status:** built (v1.2.0)
**Date:** 2026-08-03 · **Repo:** RaftVerified · **Related:** [TDD](TDD.md), [App Flow](APP_FLOW.md), [Design Brief](DESIGN_BRIEF.md)

> **Retrospective.** The code came first; this document was reconstructed from it and
> from the build spec at `docs/superpowers/specs/to-the-max.md`. It is not a record of a
> prior agreement. Every requirement below is one the code actually meets, and each names
> the check that proves it — so the document can be falsified by running something.

## Problem

Consensus bugs do not live in the code you can read; they live in message
interleavings. A drop here, a 40 ms delay there, a partition that heals one step before
an election completes — the combination that breaks Raft's Leader Completeness may occur
in one schedule out of a thousand, and when it does occur, an ordinary test suite reports
"assertion failed" with no way to see it again. Reading the Raft paper does not fix this:
the paper's five safety properties are stated over cluster state at an instant, and
nothing in a normal unit test ever evaluates them.

The person with this problem is the author — a student implementing Raft from the 2014
paper who wanted to know whether the implementation was *actually* safe, not whether it
passed a handful of hand-written scenarios. The second reader is anyone assessing the
work: they need to be able to check the claim themselves, from a clean clone, without
trusting the README.

## Who it is for

1. **The author, learning.** One person, writing Raft from the paper, who needs a
   failure to be reproducible before it is worth debugging.
2. **A reader assessing the work** — an engineer or an admissions tutor — who has ten
   minutes, no context, and a healthy suspicion of README claims.

There are no other users. The project is private, has no deployment, no server, no
account system, and no data belonging to anyone. That is not modesty; it is the fact
that makes several sections of this template inapplicable, and they have been cut rather
than padded.

## Success looks like

Each of these is a command a stranger can run from a clean clone.

- [x] A run is reproducible from a seed alone: `raftverified replay --seed 42 --faults chaos`
      runs the configuration twice and reports byte-identical event traces (it does —
      6042 trace events at seed 7, digest `fd5cd28b…`, from the CI step that ships).
- [x] The paper's five safety properties are evaluated **after every simulator step**, not
      at the end: `raftverified check --seeds 100 --faults chaos` reports
      `invariant_checks=500000` for 100 × 5000 steps — one check per step, zero violations.
- [x] A violation names its own reproduction. `InvariantViolation` carries the seed, the
      step and a complete replay command, including `--membership` and the `--nemesis`
      schedule quoted back verbatim (`tests/test_invariants.py`, `tests/test_report.py`).
- [x] The verifier is proved to catch real defects rather than asserted to: six
      deliberately-injectable consensus bugs, each caught by exactly the property it
      breaks (`raftverified/bugs.py`, `tests/test_bugs.py`, `tests/test_nemesis.py`).
- [x] A failing seed becomes a minimal counterexample automatically: ddmin over the fault
      injections to a 1-minimal set, then a binary search on the step budget
      (`raftverified/shrink.py`, `tests/test_shrink.py`).
- [x] What *clients* saw is checked independently of what the logs did: a linearizability
      oracle over the recorded client history (`raftverified/linearizability.py`).
- [x] The whole gate is one CI job: ruff, mypy `--strict`, 469 pytest tests, a 100-seed
      chaos sweep and a replay-determinism check.

## Requirements

**Must**

- Every source of nondeterminism draws from one seeded RNG stream owned by the simulator.
  No wall clock, no threads, no ambient randomness anywhere in the algorithm.
- The Raft node module contains the algorithm and nothing else — no I/O, no clock, no
  RNG of its own. This is what makes it simulable, and it is a design rule, not a style
  preference (`raftverified/node.py`).
- Safety invariants evaluated after *every* step, with the checker reading node state and
  never mutating it.
- Golden digests and RNG-draw counts pinned in `tests/goldens.json`, so any accidental
  change to the random stream fails a test rather than silently changing history.
- Zero runtime dependencies. Python 3.11+ standard library only.

**Should**

- A visual artefact per run: an SVG timeline and a single self-contained HTML report, both
  pure functions of the run so they are byte-reproducible.
- A vocabulary for *directed* chaos (the nemesis schedules) as well as random chaos, so a
  specific adversarial story can be written down, replayed and shrunk.

**Won't (this time)**

- Joint-consensus (C-old,new) membership. Single-server changes teach the same
  quorum-overlap lesson with far less determinism surface.
- Real disk I/O for "stable storage". Modelled in memory.
- Byzantine faults. Raft assumes non-Byzantine nodes; simulating them would test nothing.
- Any GUI, web dashboard, gRPC transport, or multi-Raft sharding.

## Explicitly out of scope

- **A production consensus library.** This is an educational implementation. It has no
  network transport, no persistence to disk, and no operational tooling. Nobody should
  run it as infrastructure, and the README says so in the same words.
- **A general model checker.** The linearizability oracle is a bounded *detector*: it
  searches within a states-explored budget over the KV workload actually generated here.
  It is sound for the histories it checks and makes no claim beyond them.
- **Proof.** Nothing here is a proof of Raft's correctness. It is empirical: a large
  number of adversarial schedules, each checked exhaustively, each replayable. The
  distinction is stated in the README's Scope & limitations section and is not softened
  anywhere in this repo.

## Safety and privacy

- **Personal data touched: none.** There is no user, no account, no database, no
  telemetry and no network. The "cluster" is five Python objects in one process.
- **What it writes:** only files the caller names — `--timeline OUT.SVG` and
  `--out OUT.HTML` — plus stdout. Nothing else is created or read.
- **What it accepts from outside:** exactly one untrusted input, the `--nemesis` JSON
  schedule from the shell. It is validated totally before the first simulator step
  (unknown pattern, missing/extra/non-integer field, out-of-range probability, runaway
  flap count) and rejected as a usage error, so a malformed schedule can never become a
  mid-run crash. It is parsed with `json.loads` into frozen dataclasses; nothing is
  `eval`'d.
- **Revocation:** not applicable — there is no access to revoke. Recorded here explicitly
  so that a future version growing a server surface has to change this line.
- **Worst outcome if this is wrong:** a *false green* — the checker reports safety on a
  run that was not safe, and the author learns Raft wrong. That risk is the reason the
  injectable-bug registry exists: each of the six bugs must be caught by the specific
  property it breaks, and one of them (`stale_local_reads`) must be missed by every
  internal invariant and caught only by the linearizability oracle. A verifier that
  cannot be shown to fail is worthless.

## Open questions

- **Budget exhaustion is reported as a violation.** `linearizability.check` returns
  `linearizable=False` with the message "search budget exhausted; result undetermined",
  and `shrink.failure_signature` maps any non-linearizable result to the signature
  `"nonlinearizable"`. An undetermined search would therefore be shrunk as if it were a
  real counterexample. No run in the current corpus reaches the 500,000-state budget, so
  this has never fired — but the correct design is a third verdict
  (`linearizable` / `not linearizable` / `undetermined`), and the callers should branch on
  it. This is a known defect, recorded rather than quietly fixed.

## Not doing / rejected alternatives

| Considered | Rejected because |
|---|---|
| Wall-clock leader leases for reads (§6.4) | A real clock destroys reproducibility, which is the entire premise. Message-driven ReadIndex (§8) gives the same guarantee with no clock. |
| Hypothesis or another third-party fuzzer | Would add a runtime dependency, and the seeded sweep plus hand-rolled ddmin already *is* the equivalent. Writing the shrinker was also the point. |
| Threads or asyncio for the "network" | Destroys determinism outright. A discrete-event queue over a virtual clock gives the same interleavings, reproducibly. |
| Real files for stable storage | Injects OS-level nondeterminism (fsync ordering, timing) for no correctness gain; crash-restart semantics are modelled exactly without it. |
| Joint consensus (C-old,new) | Roughly doubles the configuration state space and the determinism surface to teach the same quorum-overlap lesson single-server membership already teaches. Kept on the roadmap, honestly, rather than half-built. |
| A richer replicated state machine | A KV store with `put`/`get`/`cas` is the smallest state machine on which linearizability is *interesting* (CAS makes retries observable). Anything larger adds surface without adding a lesson. |
| A web dashboard | The SVG timeline and the standalone HTML report carry the whole visual story with zero dependencies and no server. |
