# Harmonia "to the max" — spec (self-approved)

Program: repo max-upgrades #4 (after Hephaestus, Helios, Daedalus). Posture:
"deep but distinctive" — the most ambitious *genuinely-coherent* version of this
repo's core idea, hand-crafted (not uniform AI-polish). Self-approved under the
founder's standing program directive.

## Core idea (unchanged, taken to the max)

Harmonia is **an educational Raft consensus implementation verified end-to-end by
deterministic simulation testing (DST)**. Everything reproduces byte-identically
from a seed. Today it proves only the **five internal Raft safety invariants** over
node logs; it never checks **what a client observes**, and when a seed fails it
hands you a 20,000-step haystack.

## The upgrade in one sentence

Turn a paper-faithful Raft-in-a-simulator into a **Jepsen-grade, self-minimizing
DST consensus testbed**: add a client-observable **linearizability oracle** and an
automatic **schedule shrinker** (delta-debug any failure to a minimal, replayable,
SVG-visualizable counterexample, proven real by a toggleable bug-injection harness),
then make the Raft itself *real* — crash-rebuild with true volatile-state loss,
snapshots/InstallSnapshot, ReadIndex + exactly-once sessions, and (budget
permitting) single-server membership — each landing in lockstep with the
invariant-checker generalization it forces.

## Co-crowns

- **A — Linearizability oracle** over recorded client histories (Wing-Gong /
  Knossos-style linearize-and-remove with a memoized visited-set and bounded
  concurrency). A NEW oracle, not a re-check: catches stale-leader reads,
  acknowledged-but-lost writes, and double-applied retries that the internal
  invariants structurally cannot see. A **pure function** of an already-deterministic
  history → injects zero randomness.
- **B — Automatic schedule shrinker** (ddmin to a minimal counterexample): the
  FoundationDB/Hypothesis-grade hallmark that turns any failing seed into a handful
  of decisive fault decisions + a byte-identical replay command + an SVG of exactly
  that interleaving. Determinism-safe: spins a fresh seeded `Cluster` per candidate,
  never mutates the live stream.

The two compound: the oracle gives the shrinker richer failures to minimize; the
shrinker makes every oracle failure teachable.

## Test target

151 → **~310–320 tests over 11 steps (Step 0..10)**, clearing the calibration band
(Hephaestus 219, Helios 193, Daedalus 398). **Portfolio-complete by Step 5**; Steps
6–9 are ordered, independently-shippable bonuses (merge after any completed step).

## Determinism guardrails (the crown constraint — enforce on EVERY step)

1. **APPEND-NEVER-INSERT.** The single `sim.rng` stream is drawn in a fixed textual
   order (`Network.send`: drop-check, delay, dup-check, dup-delay; `_fault_tick`:
   heal, form, per-node sides, crash, victim, resume-delay; election-timeout). Any
   new draw is **appended at the end** of an existing sequence, never inserted before
   an existing draw. New RPC rounds (ReadIndex, InstallSnapshot, membership) get their
   own config flag AND their own golden matrix.
2. **STEP-0 TRIPWIRE IS LAW.** A golden-digest corpus (~12 fixed `(nodes,seed,faults,
   steps)` configs pinned to sha256 constants) + an rng-draw-count/order guard exist
   before any feature. Determinism-neutral steps leave those digests UNCHANGED;
   intentional-behavior steps rebaseline them in ONE committed diff via a reviewable
   `regenerate-goldens` path showing only the intended configs moved.
3. **REPLAY-TWICE PER FEATURE.** Keep the run-twice-assert-identical gate; add it as
   an explicit test for each new feature (incl. `shrink(H)==shrink(H)` byte-for-byte
   and masked-run digest stability).
4. **SORT-BEFORE-ANY-DRAW-OR-TRACE.** Always `sorted(...)` before iterating
   `next_index`/`match_index`/config sets/session dicts/node ids when the order feeds
   an rng draw, a message, or the trace.
5. **ORACLES ARE PURE.** Linearizability checker, invariant checker, shrinker
   per-candidate replays, and the HTML report draw ZERO randomness; assert digest is
   byte-identical with each oracle enabled vs disabled at a fixed seed.
6. **CHECKER GENERALIZED IN LOCKSTEP, SAME COMMIT.** Snapshots (compacted prefix
   breaks raw `log[idx-1]`) and membership (majority ≠ `len(peers)+1`) ship their
   `InvariantChecker` generalization in the same commit, each with a planted-bug
   "fake node" test proving the generalized checker still CATCHES a real
   cross-boundary/cross-config divergence AND does not false-positive on legal
   compaction/reconfig.
7. **BASE-OFFSET BEFORE SNAPSHOTS.** No snapshot work touches raw 1-based indexing
   until the Step-7 base-offset abstraction has landed and been proven digest-neutral
   at `base_index == 0`.
8. **INJECTABLE BUGS ARE OFF BY DEFAULT.** Every bug in the injection registry sits
   behind an explicit config flag, defaults OFF, and is covered by a test asserting
   the default sweep is green AND the baseline digest equals the pre-feature constant.

## Steps

### Step 0 — Determinism tripwire + tooling gate (NO behavior change)
Lock the guardrail with machinery. Add ruff config; flip mypy toward `--strict`
(module-by-module where the discrete-event `Callable`s need it), strict-clean; both
as CI gates alongside pytest + replay. Add `test_golden_digests_pinned` (~12-config
matrix pinned to sha256 constants) + a reviewable `regenerate-goldens` path; add
`test_rng_stream_length_stable` (instrument the `Random` to count draws per config,
pin count/order). Reconcile the README `SafetyChecker`→`InvariantChecker` naming
slip. All 151 tests unchanged; zero digest movement.

### Step 1 — KV state machine + structured client-op history (PREREQUISITE)
Give the sim a deterministic key-value register state machine (put/get/cas) applied
at commit, and record a global client history of `(client_id, req_id, op, invoke_step,
return_step, observed_value)`. Near-pure refactor reusing the SINGLE existing
`client_tick` draw (enrich the payload, add NO hot-path draw). Documented one-time
digest rebaseline (richer command text). Tests: kv-apply-deterministic, get reflects
committed puts, history records invoke/return steps, history-recording-is-pure.

### Step 2 — Client sessions + exactly-once dedup (Ongaro 6.3)
Per-`(client_id, seq)` session state + leader-side dedup/response cache + a retry
driver (deterministic counters; retry-target selection APPENDED to the existing
client draw). Deduped completed ops feed the history. Deliberate golden rebaseline
(`applied[]` changes). Tests: duplicate-seq-applies-once, cached-result-on-retry,
dedup-survives-leader-change, chaos property (every `(client_id, seq)` applies ≤ once
on every node).

### Step 3 — CO-CROWN A: Linearizability oracle (PURE function)
Model each op as an invoke/complete interval against a reference sequential
register/map; search for a real-time-consistent linearization (linearize-and-remove,
memoized visited-set, bounded concurrency). TDD in ISOLATION on hand-built histories
FIRST, then wire to extract-and-check the cluster history at run end / per
client-completion (NOT per step); add to the chaos sweep; bounded search (cap
in-flight, cap history in sweeps, memoize), exhaustive path slow-only. Tests:
isolation corpus (known-good accepts, classic non-linearizable rejects with witness,
empty/single trivial, pinning-read pass/fail), healthy-run-linearizable property,
positive control (stale-leader read CAUGHT), determinism verdict, terminates-in-budget.

### Step 4 — Bug-injection harness + README failure gallery
A registry of TOGGLEABLE algorithm bugs, all OFF by default behind config flags: drop
the 5.4.2 current-term commit guard (Fig 8), vote for a less-up-to-date candidate
(break 5.4.1), skip the AppendEntries consistency check, let `commit_index` regress.
Each, when enabled, violates the specific invariant/oracle it targets within a bounded
sweep. Seeds the README before/after gallery. Tests: parametrized per bug → exact
invariant/oracle tripped; all-OFF sweep green AND baseline digest == pre-feature
constant; each bug pins a golden (seed+step).

### Step 5 — CO-CROWN B: Automatic schedule shrinker (ddmin) + scriptable-schedule prereq
Two ordered sub-parts: (a) lift fault-driver decisions into an explicit replayable
`Schedule` (a deterministic suppression MASK threaded through the driver, still
rng-fed by default) — a determinism-neutral refactor proven digest-identical; then
(b) ddmin over that schedule (binary-search earliest failing step, then fault-mask
reduction, then node/command-count reduction), each candidate replayed on a FRESH
seeded `Cluster`. Emit a replayable scenario + an SVG of just those events. Tests:
ddmin-finds-planted-faults (500-decision → exactly the 2 needed), shrunk still
reproduces SAME violation, local-minimality, idempotence, monotone+terminates,
masked-run digest stable, healthy run → empty counterexample, byte-identical minimal
schedule per seed.

### Step 6 — Real persistence + crash-restart with true volatile-state loss
Remove the cheat where a crash merely pauses with state intact. An in-memory "stable
store" modeling fsync of `(currentTerm, votedFor, log)`; crash CLEARS volatile state
(commit_index, last_applied, applied, role/leader_id, next/match_index, votes);
restart REBUILDS from persisted log, re-applying up to commit_index; session/dedup
table reconstructed from the replayed log. Crash/restart timing draws APPENDED.
Deliberate golden rebaseline for crash-enabled configs. Tests: re-derive `applied[]`
identically (no double-apply), no double-vote in a term after restart, Election Safety
across a crash sweep, chaos+real-crash keeps all five invariants AND linearizability.

### Step 7 — Log base-offset abstraction (determinism-NEUTRAL, snapshot prereq)
Replace raw `log[index-1]` with helpers carrying a `base_index` (=0 today) across
node.py (`term_at`/`entry_at`/`_advance_commit`/`_send_append_entries`) AND
invariants.py (direct `log[idx-1]` reads). Extend the `NodeView` Protocol with
base-offset accessors. Ship with `base_index == 0` → digests UNCHANGED. Tests:
all-existing-pass-unchanged, golden-digests-unchanged (the exact oracle), property
`term_at(index)` at base=0 == old behavior, ruff+mypy --strict clean.

### Step 8 — Snapshots / log compaction + InstallSnapshot, WITH generalized checker (SAME COMMIT)
Compact a committed prefix into `(lastIncludedIndex/Term + state)`; InstallSnapshot
for followers below the compacted prefix (own config flag + own golden matrix). The
`InvariantChecker` generalized IN THE SAME COMMIT to treat the compacted prefix as
committed and compare over the reconstructed logical log; timeline gets a snapshot
mark; threshold from config, not RNG. Tests: indexing correct across the snapshot
boundary, follower below snapshot receives InstallSnapshot and converges, Log Matching
+ Leader Completeness pass with one node compacted AND still CATCH a planted
cross-boundary divergence without false-positives, chaos+snapshot sweep green +
linearizable, determinism-with-snapshots golden matrix, compaction never drops the
uncommitted tail.

### Step 9 — Capstone (pick ONE): ReadIndex reads (PREFERRED) OR single-server membership
PREFERRED: ReadIndex reads — leader confirms it still leads via a heartbeat round at
the committed index before answering (message-driven, NOT a wall-clock lease); reads
recorded into the history with a proper linearization point (own flag + goldens).
ALTERNATE (bigger headline, more risk, only with runway): single-server add/remove
membership with per-config majorities (same-commit checker change; config persisted in
the Step-8 snapshot). Whichever ships, the other is documented honestly in Scope &
limitations. Tests: ReadIndex — partitioned former leader without a confirmed round
REFUSES a read; read after confirm reflects prior committed writes; bug-inject naive
local read → oracle CATCHES; reads linearizable in the sweep. Membership — majority
from current config, add/remove churn keeps all five invariants + linearizability,
never two leaders in one term across a boundary, new member catches up via
InstallSnapshot, per-config majority checker planted-bug test.

### Step 10 — Showcase + README + review + merge + tag
`harmonia report` → a single self-contained stdlib HTML page (run summary +
message/fault stats + SVG timeline + linearizability PASS/verdict + on-failure the
shrunk minimal repro with a mini-timeline). README rewrite with the failure gallery
and the injected-bug → N-event minimal-repro command; reconcile over-claims (state
precisely what the bounded linearizability search does/does not do; keep the honest
"educational / no affiliation" framing); Scope & limitations updated (single-server
only or membership-out, real-disk out, Byzantine out); CHANGELOG → 1.0.0 with the real
test count; full code-review pass (adversarial 3-lens: Raft-correctness, determinism,
oracle/checker-correctness); final CI green (pytest + replay + ruff + mypy --strict);
tag v1.0.0. Report is a pure function → golden-file HTML test.

## Cut list (explicitly out of scope)

Byzantine/adversarial nodes (off-thesis, Raft assumes non-Byzantine); real disk I/O /
torn writes (models "stable storage" in-memory instead — real files inject OS
nondeterminism); full joint-consensus C-old,new (single-server delivers the same
quorum-overlap lesson with far less determinism surface); wall-clock leader-lease
reads (determinism landmine — use message-driven ReadIndex); multi-threading / asyncio
/ real-time (destroys reproducibility); third-party fuzzing (Hypothesis) or any
runtime dep (stdlib-only — the hand-rolled sweep + ddmin IS the equivalent); GUI / web
dashboard / gRPC / multi-Raft sharding (scope creep; the SVG + HTML report carry the
visual story); a rich app state machine (a register + small KV map is enough).

## Build rules

Commit identity GreenPandaTech noreply only; repo stays PRIVATE. Small TDD increments,
each committed + pushed and green (pytest + determinism replay + ruff + mypy --strict).
Every optimization/refactor semantics-preserving. Keep `SESSION_HANDOFF.md` current
with the exact next step for flawless cross-session resume.
