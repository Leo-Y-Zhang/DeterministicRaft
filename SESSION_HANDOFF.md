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
- [ ] Step 3 — CO-CROWN A: linearizability oracle (pure; TDD in isolation first)
- [ ] Step 4 — Bug-injection harness + README failure gallery
- [ ] Step 5 — CO-CROWN B: schedule shrinker (ddmin) + scriptable-schedule prereq
- [ ] Step 6 — Real persistence + crash-restart (true volatile-state loss)
- [ ] Step 7 — Log base-offset abstraction (digest-neutral; snapshot prereq)
- [ ] Step 8 — Snapshots + InstallSnapshot + generalized checker (same commit)
- [ ] Step 9 — Capstone: ReadIndex reads (preferred) OR single-server membership
- [ ] Step 10 — Showcase HTML report + README + adversarial review + merge + tag v1.0.0

**Portfolio-complete by Step 5.** Steps 6–9 are ordered, independently-shippable
bonuses — safe to merge after any completed step if time runs out.

## Exact next step
Step 3 — CO-CROWN A: the linearizability oracle (a PURE function of the recorded
`Cluster.history`). New module `harmonia/linearizability.py`: model each op as an
invoke/return interval against a reference sequential KV register/map; search for a
real-time-consistent linearization (Wing-Gong / linearize-and-remove with a memoized
visited set + bounded concurrency). Catches stale reads, acknowledged-but-lost writes,
reordered effects that the 5 internal invariants CANNOT express. TDD IN ISOLATION FIRST
on hand-built histories (known-linearizable accepts; classic non-linearizable rejects
with a witness; empty/single trivial; pinning-read pass/fail; terminates-in-budget),
THEN wire to check `Cluster.history` at run end + add to the chaos sweep as an extra
assertion. Bound the search (cap in-flight ops + history length in sweeps, memoize);
mark the exhaustive path slow-only. Determinism: pure function -> zero rng, digests
UNCHANGED (guardrail: assert digest identical with the oracle on vs off). Handle
in-flight ops (return_step None) by trying both including/excluding them. Positive
control comes in Step 4 (a deliberately-buggy stale-read mode the oracle must CATCH).

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
