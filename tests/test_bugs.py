"""The bug-injection harness: proof the checker and oracle catch real consensus bugs.

Each injected bug, when armed, must be caught by the property it targets within a bounded
seed search (five by an internal safety invariant, one -- stale local reads -- ONLY by the
linearizability oracle). With every bug off the system is byte-identical to an un-armed
run, so the harness is invisible until deliberately enabled. The sixth bug (the historical
May-2015 single-server membership bug) is exercised in tests/test_membership.py and
tests/test_shrink.py; only its registry membership is asserted here.
"""

import pytest

from harmonia.bugs import NO_BUGS, Bugs
from harmonia.cluster import Cluster
from harmonia.invariants import InvariantViolation
from harmonia.linearizability import check
from harmonia.node import AppendReply, Entry, RaftNode
from harmonia.sim import Simulator


def _leader(log, bugs):
    """A 3-node leader (term 2) preloaded with ``log``, wired to swallow I/O."""
    sim = Simulator(1)
    node = RaftNode(0, [1, 2], sim, lambda a, b, c: None, lambda k, d: None, bugs=bugs)
    node.term = 2
    node.log = [Entry(t, c) for t, c in log]
    node._become_leader()
    return node


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

    def test_skip_log_consistency_breaks_safety(self):
        # ignoring the term check lets divergent entries in -> Log Matching or, once such
        # entries are applied, State Machine Safety.
        seed, inv = first_violation(Bugs(skip_log_consistency=True), 5, range(40), 4000)
        assert inv in ("LogMatching", "StateMachineSafety"), f"got {inv} @ {seed}"

    def test_allow_commit_regression_breaks_commit_monotonicity(self):
        seed, inv = first_violation(Bugs(allow_commit_regression=True), 5, range(25), 4000)
        assert inv == "CommitIndexMonotonic", f"got {inv} @ {seed}"

    def test_drop_commit_term_guard_commits_a_prior_term_entry_by_count(self):
        # The Figure 8 bug: without the 5.4.2 guard, a leader commits an entry from an
        # EARLIER term purely on replica count -- the exact unsafety Raft forbids, because
        # such an entry can still be overwritten by a future leader. This is deterministic;
        # the full overwrite is famously rare to hit by random search, so we pin the
        # mechanism directly (and test_node proves the guard blocks it).
        guarded = _leader([(1, "old")], NO_BUGS)
        guarded.handle(1, AppendReply(term=2, follower=1, success=True, match_index=1))
        guarded.handle(2, AppendReply(term=2, follower=2, success=True, match_index=1))
        assert guarded.commit_index == 0  # guard refuses the prior-term commit

        buggy = _leader([(1, "old")], Bugs(drop_commit_term_guard=True))
        buggy.handle(1, AppendReply(term=2, follower=1, success=True, match_index=1))
        buggy.handle(2, AppendReply(term=2, follower=2, success=True, match_index=1))
        assert buggy.commit_index == 1  # BUG: unsafe prior-term entry committed by count


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
            "allow_commit_regression", "stale_local_reads", "drop_config_commit_guard",
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
