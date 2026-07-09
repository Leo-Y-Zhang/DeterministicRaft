# Session handoff — Harmonia "to the max" (targeting v1.0.0)

Program: repo max-upgrades #4 (after Hephaestus, Helios, Daedalus). Spec:
`docs/superpowers/specs/to-the-max.md` (self-approved). Branch: `feature/to-the-max`.
Baseline v0.1.0 = **151 tests green** (+1 slow sweep), mypy clean (not strict yet),
ruff not configured. Venv: `.venv/Scripts/python.exe` (Python 3.13).

## Narrative
Turn a paper-faithful Raft-in-a-simulator into a Jepsen-grade, self-minimizing DST
testbed. Two co-crowns: **(A) linearizability oracle** over client histories, **(B)
schedule shrinker** (ddmin to a minimal counterexample). Then make the Raft real
(crash-rebuild, snapshots, ReadIndex/sessions, maybe membership). Non-negotiable:
byte-identical replay from a seed — guarded by a golden-digest tripwire from Step 0.

## Build order (spec steps) — status
- [x] Step 0 — Determinism tripwire + tooling gate. ruff (E/F/W/I/UP/B/C4/SIM) + mypy
      --strict clean + in CI; golden-digest corpus (12 configs) + rng-draw-count guard in
      `tests/_goldens.py`/`goldens.json` (regenerate: `.venv/Scripts/python.exe
      tests/_goldens.py`); README SafetyChecker->InvariantChecker. Proven behavior-neutral
      vs pristine code (worktree digest check). 151 -> 179 tests.
- [x] Step 1 — KV state machine + structured client-op history. New `harmonia/kv.py`
      (`Command` encode/decode [total: opaque strings -> NOOP], `KVStateMachine`
      put/get/cas, `HistoryEntry`); node applies decoded commands to `self.kv` and reports
      `(command, result)` via an `on_apply` hook; cluster records a passive
      invoke/return/observed `history` via a counter-driven `workload_command` (3 clients,
      3 keys). ZERO new rng draws (rng_calls unchanged on all 12 goldens; digests
      rebaselined for the richer command text only). 179 -> 199 tests.
- [x] Step 2 — Client sessions + exactly-once dedup. Two layers: (1) apply-time in
      `KVStateMachine.sessions` (a duplicate `(client_id, req_id)` returns the CACHED
      result, never re-executes — matters for cas, whose recompute would say "fail");
      (2) leader-side in `client_command` (a leader won't re-append a request already in
      its log; `_client_index` rebuilt from the log on `_become_leader`). Retry-capable
      client driver: 3 clients, one op outstanding each, round-robin, retry-until-commit.
      Deliberate golden rebaseline (retries lengthen the stream -> digests + rng_calls
      both move, intentional). 199 -> 212 tests; replay byte-identical.
- [x] Step 3 — CO-CROWN A: linearizability oracle. New `harmonia/linearizability.py`
      (`check(history)`): Wing-Gong linearize-and-remove, iterative DFS (no recursion
      limit), minimal-candidate real-time pruning (O(pending) via two-min trick), memoised
      dead ends, `budget` cap, `max_ops` bound. Pending ops soundly EXCLUDED (committed =>
      completed in our model, so pending never took effect). Returns a witness order on
      pass / stuck frontier on fail. TDD in isolation on 18 hand-built histories first,
      then validated on real none/light/chaos runs (0 false positives, <3ms). PURE ->
      goldens unchanged. 212 -> 236 tests.
- [x] Step 4 — Bug-injection harness + README gallery. New `harmonia/bugs.py` (`Bugs`
      frozen dataclass, all flags OFF by default, `NO_BUGS`). 5 toggleable bugs threaded
      through RaftNode/Cluster: drop_commit_term_guard (Fig 8 -> LeaderCompleteness @3-node
      seed 63), vote_for_stale_candidate (-> LeaderCompleteness @seed 0), skip_log_consistency
      (-> LogMatching @seed 6), allow_commit_regression (-> CommitIndexMonotonic @seed 1),
      stale_local_reads (-> ORACLE-ONLY, non-linearizable @3-node seed 14, no internal
      invariant fires). Harness invisible when off (goldens unchanged; NO_BUGS run
      byte-identical). Tests use bounded search (robust to seed shifts). README seeds the
      failure gallery + documents the oracle. 236 -> 247 tests.
- [x] Step 5 — CO-CROWN B: schedule shrinker. (5a) Deterministic fault suppression MASK
      in `_fault_tick` via `_fire_fault`/`suppressed` param + `fault_count` — proven
      digest-neutral for an empty mask (goldens unchanged). (5b) New `harmonia/shrink.py`:
      generic Zeller `ddmin` (1-minimal, tested in isolation on synthetic predicates ->
      500 to exactly {137,402}); `shrink(Scenario)` ddmins the fault ordinals then binary-
      searches the step budget to a minimal reproduction of the SAME failure signature
      (invariant name / "nonlinearizable"), each candidate a FRESH masked Cluster.
      Deterministic, idempotent-stable. Shrinks real bug repros + the stale-read oracle
      failure. 247 -> 262 tests. **PORTFOLIO-COMPLETE reached** (both co-crowns done).
- [x] Step 6 — Real persistence + crash-restart. A crash (`pause`) now discards ALL
      volatile state via `_reset_volatile` (role/commit/apply/kv/leader bookkeeping) and
      bumps `incarnation`; only currentTerm/votedFor/log persist. Restart comes back as a
      follower; the state machine is rebuilt by re-applying the log (verified: no double
      apply; sessions survive). InvariantChecker made crash-aware (`_handle_restarts`
      rebases commit/applied caches per incarnation — monotonicity holds within an
      incarnation, log-based invariants keep checking across the crash). Bug repros
      re-found (skip_log now trips SMS@7; fig8 pinned as a DETERMINISTIC mechanism test —
      commits prior-term by count; stale-read nonlinearizable@0). Chaos goldens rebaselined
      (crashes reset volatile); none/light unchanged. 262 -> 268 tests; 100-seed sweep 0
      violations.
- [x] Step 7 — Log base-offset abstraction. node.py log indexing routes through helpers
      carrying `base_index`/`base_term` (`last_log_index`/`term_at`/`entry_at`/`_phys`/
      `log_suffix`); `_send_append_entries` + truncation use them. Ships base_index==0 ->
      digests UNCHANGED (the refactor's correctness oracle). Property test: helpers == raw
      1-based indexing across random logs. 268 -> 271 tests. (Checker still reads raw logs;
      its generalization is Step 8's same-commit job.)
- [x] Step 8 — Snapshots / log compaction + InstallSnapshot + generalized checker (same
      commit). Behind `RaftConfig.snapshot_threshold` (default 0 = OFF -> default goldens
      untouched). Compaction folds the applied prefix into a `Snapshot` (kv store + session
      table + last_index/term), advances base_index, keeps the uncommitted tail;
      InstallSnapshot re-seeds a follower behind the compaction point; crash-restart
      restores the kv from the persisted snapshot. InvariantChecker generalized to reason
      over (compacted prefix + live tail) via logical accessors on NodeView -- planted-bug
      FakeNode tests prove it still CATCHES cross-boundary divergence and does NOT
      false-positive on legal compaction. Chaos+snapshot sweep: all 5 invariants +
      linearizability hold across 100s of compactions/installs; own pinned golden matrix;
      timeline gets a snapshot diamond. 271 -> 306 tests.
- [x] Step 9 — Capstone: ReadIndex linearizable reads (section 8). Behind `Cluster(
      read_index=True)` (own golden matrix; default digests untouched). Dedicated
      ReadHeartbeat/ReadAck messages (AppendEntries digests unchanged); a leader answers a
      get from local state only after a majority acks in its term AND it has committed an
      entry in its OWN term (Ongaro 8 -- the oracle CAUGHT a stale-read bug when this guard
      was missing; fixed). Reads recorded into the history with proper invoke/return.
      Chaos sweep linearizable; stale_local_reads bug stays the oracle's positive control.
      306 -> 337 tests. Membership documented as out-of-scope (README).
- [ ] Step 10 — Showcase HTML report + README + adversarial review + merge + tag v1.0.0

**Portfolio-complete by Step 5.** Steps 6–9 are ordered, independently-shippable
bonuses — safe to merge after any completed step if time runs out.

## Exact next step
Step 10 — Ship v1.0.0. (a) `harmonia report` command: a single self-contained stdlib HTML
page (run summary + message/fault stats + the SVG timeline inline + linearizability PASS/
verdict; on a shrunk failure, the minimal repro). Pure function of RunResult+verdict ->
golden-file HTML test. (b) README overhaul: document the full feature set (KV state machine,
sessions/dedup, linearizability oracle, bug gallery, schedule shrinker, crash-restart,
snapshots+InstallSnapshot, ReadIndex) with the injected-bug -> minimal-repro demo; update
Scope & limitations (single-server membership OUT; real-disk OUT; Byzantine OUT; state
precisely what the bounded linearizability search does/does not do); fix the test-count and
`python -m pytest` line (now ~337). (c) CHANGELOG -> 1.0.0 with the real test count + bump
pyproject version to 1.0.0. (d) Adversarial 3-lens review via a Workflow (Raft-correctness /
determinism / oracle+checker-correctness) over the whole branch diff; fix confirmed
findings. (e) Final gates green (pytest + replay + ruff + mypy --strict + the 100-seed CLI
sweep). (f) merge --no-ff to main, tag v1.0.0, push, confirm CI green (CI runs on main).
Guardrail: honest claims (bounded KV-register linearizability, educational, no affiliation).
Two ordered sub-parts. (5a) Lift the fault-driver decisions into an explicit, replayable
`Schedule` object: a deterministic suppression MASK threaded through `_fault_tick` (each
fault decision gets an index; a mask can suppress specific ones) — still rng-fed by
default, and PROVEN digest-identical when the mask suppresses nothing (a determinism-
neutral refactor; goldens unchanged). (5b) ddmin over that schedule: given a failing
`(nodes, seed, faults, steps)` + a predicate (invariant fires OR oracle rejects), binary-
search the earliest failing step, then reduce the fault-mask to the minimal set that still
reproduces the SAME violation, each candidate replayed on a FRESH seeded Cluster (never
mutating the live stream). Emit a replayable scenario + an SVG of just those events. Use
the Step-4 bug repros as real failures to minimize (Bugs repros: vote@5/seed0,
skiplog@5/seed6, commitreg@5/seed1, fig8@3/seed63, staleread@3/seed14). Tests:
ddmin-finds-planted-faults (a synthetic predicate failing iff >=2 specific faults present
-> ddmin reduces a noisy schedule to exactly those 2), shrunk still reproduces SAME
violation, local-minimality, idempotence, monotone+terminates, masked-run digest stable,
healthy run -> empty counterexample, byte-identical minimal schedule per seed. Guardrails:
mask suppression is a deterministic replayable mask (never a re-roll); assert masked-run
digest stability; land 5a as its own proven-neutral commit before ddmin.
Add a registry of TOGGLEABLE algorithm bugs, ALL OFF BY DEFAULT behind explicit config
flags (e.g. a `Bugs` dataclass threaded into RaftNode/Cluster). Each, when enabled, must
violate the specific invariant OR the linearizability oracle it targets within a bounded
seed sweep: (a) drop the 5.4.2 current-term commit guard (Figure 8) -> StateMachineSafety/
LeaderCompleteness; (b) vote for a less-up-to-date candidate (break 5.4.1) -> Leader
Completeness / non-linearizable; (c) skip the AppendEntries log-matching consistency check
-> LogMatching; (d) let commit_index regress -> CommitIndexMonotonic; (e) a stale-leader
local read bypassing the log -> the linearizability oracle CATCHES it (the positive
control the oracle was built for). Tests: parametrized per bug -> the exact invariant/
oracle it trips within a bounded sweep; with ALL bugs OFF the full sweep stays green AND
the baseline digest equals the pre-feature constant (assert goldens UNCHANGED with the
harness present-but-off); each bug pins a (seed, step) golden for the shrinker (Step 5) to
minimize. Guardrail: INJECTABLE BUGS OFF BY DEFAULT (a test asserts default sweep green +
digest unchanged). This is the positive control that proves the oracle/checker catch real
consensus bugs; seeds the README before/after gallery.

## Verify commands
```
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m pytest -q -m slow
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m mypy --strict harmonia
.venv/Scripts/python.exe -m harmonia replay --seed 7 --faults chaos --steps 4000
```

## Determinism guardrails (see spec §"Determinism guardrails")
APPEND-NEVER-INSERT new rng draws; Step-0 golden tripwire is law; replay-twice per
feature; sort before any draw/trace; oracles are pure (zero draws); checker
generalized in the SAME commit as snapshots/membership; base-offset before snapshots;
injectable bugs OFF by default.

## Rules
Commit identity GreenPandaTech noreply only; repo stays PRIVATE. Push after every green
increment (this working copy is NOT cloud-synced; GitHub is source of truth). Every
refactor semantics-preserving.
