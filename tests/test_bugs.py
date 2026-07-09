"""The bug-injection harness: proof the checker and oracle catch real consensus bugs.

Each injected bug, when armed, must be caught by the property it targets within a bounded
seed search (four by an internal safety invariant, one -- stale local reads -- ONLY by the
linearizability oracle). With every bug off the system is byte-identical to an un-armed
run, so the harness is invisible until deliberately enabled.
"""

import pytest

from harmonia.bugs import NO_BUGS, Bugs
from harmonia.cluster import Cluster
from harmonia.invariants import InvariantViolation
from harmonia.linearizability import check


def first_violation(bugs, nodes, seeds, steps, faults="chaos"):
    """Return (seed, invariant_name) for the first run that trips a safety invariant."""
    for seed in seeds:
        try:
            Cluster(num_nodes=nodes, seed=seed, faults=faults, bugs=bugs).run(steps)
        except InvariantViolation as v:
            return seed, v.invariant
    return None, None


def first_nonlinearizable(bugs, nodes, seeds, steps, faults="chaos"):
    """Return the first seed whose history the oracle rejects (no internal violation)."""
    for seed in seeds:
        try:
            c = Cluster(num_nodes=nodes, seed=seed, faults=faults, bugs=bugs)
            c.run(steps)
        except InvariantViolation:
            continue
        if not check(c.history).linearizable:
            return seed
    return None


class TestInvariantCatchesInjectedBugs:
    def test_vote_for_stale_candidate_breaks_leader_completeness(self):
        seed, inv = first_violation(Bugs(vote_for_stale_candidate=True), 5, range(25), 4000)
        assert inv == "LeaderCompleteness", f"got {inv} @ {seed}"

    def test_skip_log_consistency_breaks_log_matching(self):
        seed, inv = first_violation(Bugs(skip_log_consistency=True), 5, range(25), 4000)
        assert inv == "LogMatching", f"got {inv} @ {seed}"

    def test_allow_commit_regression_breaks_commit_monotonicity(self):
        seed, inv = first_violation(Bugs(allow_commit_regression=True), 5, range(25), 4000)
        assert inv == "CommitIndexMonotonic", f"got {inv} @ {seed}"

    def test_drop_commit_term_guard_breaks_a_safety_property(self):
        # the Figure 8 bug is subtle -- it needs a specific interleaving, so search wider
        seed, inv = first_violation(Bugs(drop_commit_term_guard=True), 3, range(100), 6000)
        assert inv in ("LeaderCompleteness", "StateMachineSafety"), f"got {inv} @ {seed}"


class TestOracleCatchesStaleReads:
    def test_stale_local_reads_are_only_caught_by_the_oracle(self):
        # internal invariants stay happy across the whole search (no InvariantViolation);
        # the linearizability oracle is what flags the client-visible staleness.
        seed = first_nonlinearizable(Bugs(stale_local_reads=True), 3, range(40), 6000)
        assert seed is not None, "oracle failed to catch a stale read in 40 seeds"

    def test_stale_reads_do_not_trip_internal_invariants(self):
        # a whole sweep with the bug armed completes without any invariant firing
        for seed in range(20):
            Cluster(num_nodes=3, seed=seed, faults="chaos",
                    bugs=Bugs(stale_local_reads=True)).run(4000)  # no InvariantViolation


class TestHarnessIsInvisibleWhenOff:
    def test_default_bugs_are_all_off(self):
        assert not NO_BUGS.any_enabled
        assert not Bugs().any_enabled
        assert set(Bugs.names()) == {
            "drop_commit_term_guard", "vote_for_stale_candidate", "skip_log_consistency",
            "allow_commit_regression", "stale_local_reads",
        }

    @pytest.mark.parametrize("faults", ["none", "light", "chaos"])
    def test_arming_no_bugs_is_byte_identical(self, faults):
        plain = Cluster(num_nodes=5, seed=9, faults=faults).run(4000)
        armed = Cluster(num_nodes=5, seed=9, faults=faults, bugs=NO_BUGS).run(4000)
        assert plain.digest == armed.digest

    def test_no_bugs_sweep_stays_linearizable(self):
        for seed in range(15):
            c = Cluster(num_nodes=5, seed=seed, faults="chaos", bugs=NO_BUGS)
            c.run(4000)
            assert check(c.history).linearizable
