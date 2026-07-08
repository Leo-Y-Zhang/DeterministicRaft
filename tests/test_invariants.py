"""Unit tests for the invariant checker: it must actually catch violations.

Fake nodes exercise each detector; real cluster runs prove healthy histories pass.
"""

import pytest

from harmonia.cluster import Cluster
from harmonia.invariants import InvariantChecker, InvariantViolation
from harmonia.node import FOLLOWER, LEADER, Entry


class FakeNode:
    def __init__(self, node_id, role=FOLLOWER, term=1, log=(), commit_index=0, applied=()):
        self.id = node_id
        self.role = role
        self.term = term
        self.log = [Entry(*e) for e in log]
        self.commit_index = commit_index
        self.applied = list(applied)
        self.log_version = 0

    def set_log(self, log):
        self.log = [Entry(*e) for e in log]
        self.log_version += 1


def check(nodes, checker=None, step=1):
    checker = checker or InvariantChecker(seed=42)
    checker.check({n.id: n for n in nodes}, step)
    return checker


class TestElectionSafety:
    def test_two_leaders_same_term_detected(self):
        a = FakeNode(0, role=LEADER, term=3)
        b = FakeNode(1, role=LEADER, term=3)
        with pytest.raises(InvariantViolation, match="ElectionSafety"):
            check([a, b])

    def test_two_leaders_different_terms_ok(self):
        a = FakeNode(0, role=LEADER, term=3)
        b = FakeNode(1, role=LEADER, term=4)
        check([a, b])

    def test_remembers_past_leaders(self):
        checker = InvariantChecker(seed=1)
        a = FakeNode(0, role=LEADER, term=3)
        b = FakeNode(1, role=FOLLOWER, term=3)
        check([a, b], checker)
        a.role, b.role = FOLLOWER, LEADER  # different node claims the SAME term later
        with pytest.raises(InvariantViolation, match="ElectionSafety"):
            check([a, b], checker, step=2)


class TestLeaderAppendOnly:
    def test_leader_truncating_own_log_detected(self):
        checker = InvariantChecker(seed=1)
        a = FakeNode(0, role=LEADER, term=2, log=[(1, "a"), (2, "b")])
        check([a], checker)
        a.set_log([(1, "a")])  # leader deleted its own entry
        with pytest.raises(InvariantViolation, match="LeaderAppendOnly"):
            check([a], checker, step=2)

    def test_leader_rewriting_entry_detected(self):
        checker = InvariantChecker(seed=1)
        a = FakeNode(0, role=LEADER, term=2, log=[(2, "b")])
        check([a], checker)
        a.set_log([(2, "changed")])
        with pytest.raises(InvariantViolation, match="LeaderAppendOnly"):
            check([a], checker, step=2)

    def test_leader_appending_is_fine(self):
        checker = InvariantChecker(seed=1)
        a = FakeNode(0, role=LEADER, term=2, log=[(2, "a")])
        check([a], checker)
        a.set_log([(2, "a"), (2, "b")])
        check([a], checker, step=2)

    def test_follower_truncation_is_allowed(self):
        checker = InvariantChecker(seed=1)
        a = FakeNode(0, role=FOLLOWER, term=2, log=[(1, "a"), (1, "x")])
        check([a], checker)
        a.set_log([(1, "a")])  # followers repair their logs; that is legal
        check([a], checker, step=2)


class TestLogMatching:
    def test_same_term_same_index_different_prefix_detected(self):
        a = FakeNode(0, log=[(1, "a"), (2, "c")])
        b = FakeNode(1, log=[(1, "B"), (2, "c")])  # agree at index 2, differ at 1
        with pytest.raises(InvariantViolation, match="LogMatching"):
            check([a, b])

    def test_same_term_different_command_at_index_detected(self):
        a = FakeNode(0, log=[(1, "a")])
        b = FakeNode(1, log=[(1, "z")])
        with pytest.raises(InvariantViolation, match="LogMatching"):
            check([a, b])

    def test_prefix_logs_ok(self):
        a = FakeNode(0, log=[(1, "a"), (1, "b")])
        b = FakeNode(1, log=[(1, "a")])
        check([a, b])

    def test_divergent_tails_with_different_terms_ok(self):
        # legal mid-repair state: same prefix, conflicting unCOMMITTED tails
        a = FakeNode(0, log=[(1, "a"), (2, "x")])
        b = FakeNode(1, log=[(1, "a"), (3, "y")])
        check([a, b])

    def test_rechecks_only_on_log_change(self):
        checker = InvariantChecker(seed=1)
        a = FakeNode(0, log=[(1, "a")])
        b = FakeNode(1, log=[(1, "a")])
        check([a, b], checker)
        a.log[0] = Entry(1, "HACKED")  # mutate WITHOUT bumping log_version
        check([a, b], checker, step=2)  # cache hides it: documents the contract
        a.log_version += 1
        with pytest.raises(InvariantViolation, match="LogMatching"):
            check([a, b], checker, step=3)


class TestLeaderCompleteness:
    def test_leader_missing_committed_entry_detected(self):
        checker = InvariantChecker(seed=1)
        a = FakeNode(0, role=LEADER, term=2, log=[(1, "a"), (2, "b")], commit_index=2)
        check([a], checker)
        b = FakeNode(1, role=LEADER, term=5, log=[(1, "a")])  # missing committed "b"
        a.role = FOLLOWER
        with pytest.raises(InvariantViolation, match="LeaderCompleteness"):
            check([a, b], checker, step=2)

    def test_stale_leader_of_earlier_term_exempt(self):
        checker = InvariantChecker(seed=1)
        stale = FakeNode(0, role=LEADER, term=1, log=[(1, "old")])
        current = FakeNode(1, role=LEADER, term=4, log=[(4, "new")], commit_index=1)
        # commit observed at term 4; the term-1 stale leader is not bound by it
        check([stale, current], checker)

    def test_complete_leader_passes(self):
        checker = InvariantChecker(seed=1)
        a = FakeNode(0, role=LEADER, term=2, log=[(2, "a")], commit_index=1)
        check([a], checker)
        b = FakeNode(1, role=LEADER, term=3, log=[(2, "a")])
        a.role = FOLLOWER
        check([a, b], checker, step=2)


class TestStateMachineSafety:
    def test_different_applied_command_at_same_index_detected(self):
        a = FakeNode(0, applied=["x"])
        b = FakeNode(1, applied=["y"])
        with pytest.raises(InvariantViolation, match="StateMachineSafety"):
            check([a, b])

    def test_same_applied_prefix_ok(self):
        a = FakeNode(0, applied=["x", "y"])
        b = FakeNode(1, applied=["x"])
        check([a, b])

    def test_conflicting_committed_entries_detected(self):
        checker = InvariantChecker(seed=1)
        a = FakeNode(0, term=1, log=[(1, "a")], commit_index=1)
        check([a], checker)
        # different terms at index 1, so LogMatching passes; but BOTH claim the
        # entry is committed, which is a state machine safety violation
        b = FakeNode(1, term=2, log=[(2, "z")], commit_index=1)
        with pytest.raises(InvariantViolation, match="StateMachineSafety"):
            check([a, b], checker, step=2)


class TestCommitMonotonicity:
    def test_commit_regression_detected(self):
        checker = InvariantChecker(seed=1)
        a = FakeNode(0, term=1, log=[(1, "a"), (1, "b")], commit_index=2)
        check([a], checker)
        a.commit_index = 1
        with pytest.raises(InvariantViolation, match="CommitIndexMonotonic"):
            check([a], checker, step=2)


class TestViolationErgonomics:
    def test_violation_carries_seed_and_step(self):
        a = FakeNode(0, role=LEADER, term=3)
        b = FakeNode(1, role=LEADER, term=3)
        with pytest.raises(InvariantViolation) as exc:
            check([a, b], InvariantChecker(seed=1234), step=77)
        assert exc.value.seed == 1234 and exc.value.step == 77
        assert "seed=1234" in str(exc.value) and "step=77" in str(exc.value)

    def test_violation_includes_replay_command(self):
        checker = InvariantChecker(seed=9, replay_hint="harmonia replay --nodes 5 --seed 9 --faults chaos")
        a = FakeNode(0, role=LEADER, term=3)
        b = FakeNode(1, role=LEADER, term=3)
        with pytest.raises(InvariantViolation) as exc:
            checker.check({0: a, 1: b}, 5)
        assert "harmonia replay --nodes 5 --seed 9 --faults chaos" in str(exc.value)

    def test_checker_runs_after_every_step(self):
        c = Cluster(num_nodes=3, seed=50, faults="light")
        result = c.run(2000)
        assert result.stats["invariant_checks"] == result.steps == 2000

    def test_healthy_chaos_run_passes(self):
        c = Cluster(num_nodes=5, seed=51, faults="chaos")
        c.run(5000)  # raises on any violation

    def test_committed_map_grows_with_commits(self):
        c = Cluster(num_nodes=3, seed=52, faults="none")
        c.run_until(lambda c: len(c.checker.committed) >= 3, 40_000)
        assert len(c.checker.committed) >= 3
