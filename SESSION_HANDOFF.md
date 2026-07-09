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
- [ ] Step 1 — KV state machine + structured client-op history (prereq)
- [ ] Step 2 — Client sessions + exactly-once dedup
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
Step 1 — KV state machine + structured client-op history (PREREQUISITE for the oracle,
sessions, reads). Add a deterministic `KVStateMachine` (put/get/cas) applied on
`_set_commit_index`; replace opaque `cmd-N` strings with a structured `ClientOp`
carrying `(client_id, req_id, op, key, value)`; record a passive global client history
of `(client_id, req_id, op, invoke_step, return_step, observed_value)` off the apply
path (draws NO randomness — reuse the single existing `client_tick` draw, enrich the
payload only). This DELIBERATELY changes trace bytes (richer command text) → a one-time
golden rebaseline in THIS commit via `.venv/Scripts/python.exe tests/_goldens.py`;
review the diff shows only the intended text move. First prove
`test_history_recording_is_pure` (digest identical with history on vs off) BEFORE the
rebaseline. Guardrail: APPEND-NEVER-INSERT — add no hot-path rng draw.

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
