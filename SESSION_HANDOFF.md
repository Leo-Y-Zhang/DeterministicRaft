# Session handoff — Harmonia (v1.1.0 SHIPPED: single-server membership)

**STATUS: v1.1.0 complete on `main` — single-server membership changes (dissertation
ch. 4) + the real May-2015 membership bug as the sixth injectable. 407 tests (+1 slow),
ruff + mypy --strict clean, replay byte-identical, 100-seed chaos sweep clean. Nothing
pending.** History below records the v1.0.0 "to the max" program.

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
- [x] Step 10 — Ship v1.0.0. `harmonia report` self-contained HTML (report.py, golden-file
      test); README overhaul (all features + honest scope); CHANGELOG 1.0.0 + version bump
      0.1.0->1.0.0; adversarial 3-lens review (6 findings, 5 refuted, 1 defensive fix to
      log_suffix); final gates green (343 tests + slow, ruff, mypy --strict, replay
      byte-identical, 100-seed sweep 0 violations). Merged --no-ff to main + tagged v1.0.0.

## STATUS: v1.0.0 shipped (all 11 steps 0-10, 151 -> 343 tests), then the v1.1.0
membership round (below). Next repo in the max-upgrades program (#5): Talos (renamed
Themis; see [[project_repo_max_upgrades]]).

## v1.1.0 round (2026-07-30) — single-server membership, COMPLETE
- Membership changes per dissertation ch. 4: config entries effective on APPEND
  (pre-commit); per-configuration majorities everywhere (elections, commits, ReadIndex);
  guards = one-in-flight + current-term-commit (the May-2015 amendment). Derived state:
  rebuilt from log/snapshot on restart/truncation/InstallSnapshot (whose wire format now
  carries voters -> snapshot goldens rebaselined once).
- `Cluster(membership=True)` / CLI `--membership`: one spare server + deterministic
  zero-rng churn driver proposing to EVERY believed leader at the client tick rate; own
  golden matrix; default goldens byte-identical (asserted).
- Checker generalized SAME COMMIT: per-config majorities + new CommitQuorum property;
  planted-bug FakeNode tests both ways.
- Sixth injectable `drop_config_commit_guard` = the REAL May-2015 bug: hand-driven
  mechanism test replays the raft-dev example exactly (+ guarded twin); pinned natural
  repro n=6 chaos seed 354 (1000-seed hunt; 5-server control sweep clean by majority
  geometry); ddmin shrinks it (test_shrink.py).
- Docs: README membership section + historical-bug story; CHANGELOG 1.1.0; version
  1.1.0; test counts corrected (343 -> 407 +1 slow).

## Exact next step
Nothing pending. v1.1.0 is complete and gate-green on `main`.

## Verify commands
```
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m pytest -q -m slow
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m mypy --strict harmonia
.venv/Scripts/python.exe -m harmonia replay --seed 7 --faults chaos --steps 4000
.venv/Scripts/python.exe -m harmonia check --seeds 100 --faults chaos --membership
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
