"""Automatic schedule shrinking: turn a failing seed into a MINIMAL counterexample (co-crown B).

A seed that fails somewhere in 20,000 steps with dozens of faults is a haystack. Real DST
systems (FoundationDB, Hypothesis) don't hand you the haystack -- they hand you the needle.
Given a failing scenario and the failure it produced, this shrinker delta-debugs it down to
the fewest fault injections and the fewest steps that still reproduce the SAME failure, then
reports exactly those events plus a replay you can run.

Two reductions, in order:
  1. ddmin over the fault ordinals -- the classic Zeller delta-debugging minimisation -- to
     a 1-minimal set of faults (suppressing any more makes the bug vanish);
  2. binary search on the step budget to the earliest step at which the failure still fires.

Every candidate is a FRESH seeded Cluster with a suppression mask (see cluster.py): the live
run is never mutated, and an empty mask is byte-identical to an un-shrunk run, so shrinking
is itself perfectly deterministic -- ``shrink`` of the same failure returns the same minimal
scenario every time.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from .bugs import NO_BUGS, Bugs
from .cluster import Cluster
from .invariants import InvariantViolation
from .linearizability import check


@dataclass(frozen=True)
class Scenario:
    nodes: int
    seed: int
    faults: str
    steps: int
    bugs: Bugs = NO_BUGS
    suppressed: frozenset[int] = frozenset()

    def replay_command(self) -> str:
        return (f"harmonia replay --nodes {self.nodes} --seed {self.seed} "
                f"--faults {self.faults} --steps {self.steps}")


@dataclass
class Counterexample:
    scenario: Scenario           # the minimal reproducing scenario
    signature: str               # the failure it reproduces (invariant name / "nonlinearizable")
    fault_events: list[str]      # the partition/crash injections that survived (with heals/resumes)
    injection_count: int         # partitions + crashes in the minimal run (the fault count)
    original_fault_count: int
    original_steps: int

    def summary(self) -> str:
        return (f"{self.signature}: minimal counterexample uses "
                f"{self.injection_count} of {self.original_fault_count} faults in "
                f"{self.scenario.steps} of {self.original_steps} steps")


def ddmin(elements: Sequence[int], reproduces: Callable[[list[int]], bool]) -> list[int]:
    """Zeller delta-debugging: return a 1-minimal subset of ``elements`` that still
    ``reproduces`` (removing any single remaining element would stop reproducing).
    ``reproduces(list(elements))`` is assumed True."""
    if reproduces([]):
        return []  # nothing is required
    kept = list(elements)
    n = 2
    while len(kept) >= 2:
        chunk = max(1, len(kept) // n)
        subsets = [kept[i:i + chunk] for i in range(0, len(kept), chunk)]
        for sub in subsets:
            complement = [e for e in kept if e not in sub]
            if complement and reproduces(complement):
                kept = complement
                n = max(n - 1, 2)
                break
        else:
            if n >= len(kept):
                break
            n = min(len(kept), n * 2)
    return kept


def failure_signature(scenario: Scenario) -> str | None:
    """Run the scenario and classify its failure: the invariant name if one fired,
    "nonlinearizable" if the client history is not linearizable, else None."""
    cluster = Cluster(num_nodes=scenario.nodes, seed=scenario.seed, faults=scenario.faults,
                      bugs=scenario.bugs, suppressed=scenario.suppressed)
    try:
        cluster.run(scenario.steps)
    except InvariantViolation as violation:
        return violation.invariant
    return None if check(cluster.history).linearizable else "nonlinearizable"


def _fault_count(scenario: Scenario) -> int:
    cluster = Cluster(num_nodes=scenario.nodes, seed=scenario.seed, faults=scenario.faults,
                      bugs=scenario.bugs, suppressed=scenario.suppressed)
    with contextlib.suppress(InvariantViolation):
        cluster.run(scenario.steps)
    return cluster.fault_count


def _fault_events(scenario: Scenario) -> list[str]:
    cluster = Cluster(num_nodes=scenario.nodes, seed=scenario.seed, faults=scenario.faults,
                      bugs=scenario.bugs, suppressed=scenario.suppressed)
    with contextlib.suppress(InvariantViolation):
        cluster.run(scenario.steps)
    return [f"{t}ms {kind} {detail}" for t, kind, detail in cluster.events
            if kind in ("partition", "crash", "heal", "resume")]


def _min_steps(scenario: Scenario, target: str) -> int:
    """Binary-search the earliest step budget that still reproduces ``target``."""
    lo, hi = 1, scenario.steps
    while lo < hi:
        mid = (lo + hi) // 2
        if failure_signature(replace(scenario, steps=mid)) == target:
            hi = mid
        else:
            lo = mid + 1
    return lo


def shrink(scenario: Scenario, *, target: str | None = None) -> Counterexample | None:
    """Delta-debug ``scenario`` to a minimal reproduction of its failure (or the given
    ``target`` failure). Returns None if the scenario does not fail."""
    signature = failure_signature(scenario)
    target = target or signature
    if signature is None or signature != target:
        return None

    original_steps = scenario.steps
    total_faults = _fault_count(scenario)

    # 1. ddmin the fault ordinals: kept = ordinals NOT suppressed.
    universe = range(total_faults)

    def reproduces(kept: list[int]) -> bool:
        suppressed = frozenset(universe) - frozenset(kept)
        return failure_signature(replace(scenario, suppressed=suppressed)) == target

    minimal_kept = ddmin(list(universe), reproduces) if total_faults else []
    suppressed = frozenset(universe) - frozenset(minimal_kept)
    reduced = replace(scenario, suppressed=suppressed)

    # 2. binary-search the step budget with the minimal fault set fixed.
    reduced = replace(reduced, steps=_min_steps(reduced, target))

    events = _fault_events(reduced)
    injections = sum(1 for e in events if e.split()[1] in ("partition", "crash"))
    return Counterexample(
        scenario=reduced,
        signature=target,
        fault_events=events,
        injection_count=injections,
        original_fault_count=total_faults,
        original_steps=original_steps,
    )
