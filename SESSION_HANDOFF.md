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
- [ ] Step 0 — Determinism tripwire + tooling gate (ruff, mypy --strict, golden-digest
      corpus, rng-draw-order guard). NO behavior change; 151 tests unchanged.
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
Step 0: add ruff config + tighten mypy to `--strict`; fix the known findings (ruff:
unused `field` import in cluster.py, unused `LEADER` in test_safety_sweep.py, 3× `E741`
ambiguous `l` in test_cli.py; mypy-strict: `cli.py:155` returning Any); add a
golden-digest corpus test + rng-draw-count guard test + a `regenerate-goldens` path;
add ruff + mypy-strict to CI; fix README `SafetyChecker`→`InvariantChecker` slip. Keep
all 151 tests green and every existing digest UNCHANGED.

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
