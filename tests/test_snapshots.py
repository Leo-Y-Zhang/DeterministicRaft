"""Snapshots / log compaction + InstallSnapshot (section 7) and the generalized checker.

Two halves:
  * the checker was generalized to reason over (compacted prefix + live tail); these tests
    prove it still CATCHES a real cross-boundary divergence and does NOT false-positive on
    legal compaction (a node that compacted a prefix another still holds raw);
  * end-to-end: compaction fires, a lagging follower catches up via InstallSnapshot, and a
    chaos+snapshot sweep keeps every safety invariant AND linearizability, deterministically.
"""

import pytest
from test_invariants import FakeNode

from harmonia.cluster import Cluster
from harmonia.invariants import InvariantChecker, InvariantViolation
from harmonia.kv import Command, KVStateMachine
from harmonia.linearizability import check
from harmonia.node import LEADER, RaftConfig

SNAP = RaftConfig(snapshot_threshold=10)


class TestGeneralizedCheckerAcrossCompaction:
    def test_log_matching_passes_when_one_node_compacted_a_prefix(self):
        # a holds the full log; b compacted the first two entries into a snapshot. They
        # describe the SAME logical log, so Log Matching must NOT fire.
        a = FakeNode(0, log=[(1, "x"), (1, "y"), (2, "z")])
        b = FakeNode(1, log=[(2, "z")], base_index=2, base_term=1)
        InvariantChecker(seed=1).check({0: a, 1: b}, 1)  # no raise

    def test_log_matching_catches_divergence_above_the_boundary(self):
        # both compacted to index 2, but disagree on the entry at logical index 3
        a = FakeNode(0, log=[(2, "z")], base_index=2, base_term=1)
        b = FakeNode(1, log=[(2, "W")], base_index=2, base_term=1)
        with pytest.raises(InvariantViolation, match="LogMatching"):
            InvariantChecker(seed=1).check({0: a, 1: b}, 1)

    def test_leader_completeness_satisfied_by_a_snapshotted_entry(self):
        checker = InvariantChecker(seed=1)
        checker.check({0: FakeNode(0, term=2, log=[(1, "a"), (2, "b")], commit_index=1)}, 1)
        # a term-2 leader that compacted index 1 into its snapshot still "has" it
        leader = FakeNode(1, role=LEADER, term=2, log=[(2, "b")],
                          base_index=1, base_term=1, commit_index=1)
        checker.check({1: leader}, 2)  # no raise

    def test_leader_completeness_catches_missing_entry_above_boundary(self):
        checker = InvariantChecker(seed=1)
        checker.check({0: FakeNode(0, term=3, log=[(1, "a"), (2, "b"), (3, "c")],
                                   commit_index=3)}, 1)
        # a term-3 leader compacted through index 2 but is missing committed index 3
        leader = FakeNode(1, role=LEADER, term=3, log=[], base_index=2, base_term=2,
                          commit_index=2)
        with pytest.raises(InvariantViolation, match="LeaderCompleteness"):
            checker.check({1: leader}, 2)

    def test_state_machine_safety_uses_logical_indices_across_a_snapshot(self):
        checker = InvariantChecker(seed=1)
        # node 0 applied logical indices 1,2,3
        checker.check({0: FakeNode(0, applied=["a", "b", "c"])}, 1)
        # node 1 compacted through logical index 2; its applied[0] is logical index 3 and
        # must match -> "c" is fine, but "Z" at logical 3 is a divergence.
        good = FakeNode(1, applied=["c"], base_index=2, base_term=1)
        checker.check({1: good}, 2)  # no raise
        bad = FakeNode(2, applied=["Z"], base_index=2, base_term=1)
        with pytest.raises(InvariantViolation, match="StateMachineSafety"):
            checker.check({2: bad}, 3)


class TestCompaction:
    def test_compaction_advances_base_and_shrinks_the_log(self):
        c = Cluster(num_nodes=3, seed=1, faults="none", config=SNAP)
        c.run_until(lambda c: any(n.base_index > 0 for n in c.nodes.values()), 20_000)
        node = max(c.nodes.values(), key=lambda n: n.base_index)
        assert node.base_index > 0
        assert node.snapshot is not None and node.snapshot.last_index == node.base_index
        assert node.last_log_index() >= node.base_index  # tail preserved
        assert len(node.applied) == node.last_log_index() - node.base_index

    def test_compaction_never_drops_the_uncommitted_tail(self):
        for seed in range(10):
            c = Cluster(num_nodes=5, seed=seed, faults="chaos", config=SNAP)
            c.run(4000)
            for n in c.nodes.values():
                # everything above the committed index is uncommitted tail and must survive
                assert n.last_log_index() >= n.commit_index

    def test_kv_matches_snapshot_plus_replayed_tail(self):
        c = Cluster(num_nodes=5, seed=2, faults="chaos", config=SNAP)
        c.run(4000)
        for n in c.nodes.values():
            rebuilt = KVStateMachine()
            if n.snapshot is not None:
                rebuilt.restore(n.snapshot.store, n.snapshot.sessions)
            for e in n.log[: n.commit_index - n.base_index]:
                rebuilt.apply(Command.decode(e.command))
            assert rebuilt.snapshot() == n.kv.snapshot()


class TestInstallSnapshot:
    def test_lagging_follower_catches_up_via_install_snapshot(self):
        # a crashed node that misses a long burst of commits must be re-seeded by snapshot
        c = Cluster(num_nodes=5, seed=7, faults="chaos", config=SNAP)
        c.run(5000)
        installs = sum(1 for _, k, _ in c.events if k == "installsnap")
        assert installs > 0

    def test_install_seeds_a_correct_state_machine(self):
        # a node that installed a snapshot must hold exactly the snapshot's committed state
        c = Cluster(num_nodes=5, seed=7, faults="chaos", config=SNAP)
        c.run(5000)
        installed = [n for n in c.nodes.values() if n.incarnation > 0 and n.base_index > 0]
        assert installed, "expected a crashed-and-resnapshotted node"
        for n in installed:
            assert n.snapshot is not None
            assert n.snapshot.last_index == n.base_index


# Pinned digests for snapshot-enabled configs -- the tripwire's own golden matrix, kept
# separate from tests/goldens.json so the default (snapshot-off) digests stay untouched.
SNAPSHOT_GOLDENS = {
    (5, 1, "chaos", 4000): "f814f448c8b954ce26ea9ce8dbc3d7b206f6c838174280f3414f50bca2fccf9c",
    (3, 4, "light", 3000): "42df01fd1359ef51008a5b0e7cd9374949e6db21fc247f46f442f513ce8e1f8e",
    (5, 9, "chaos", 5000): "3f185f68e7b8a4a97fdafb9cc97353f7405843ff4dcb605a6b17276ab77945f1",
}


class TestSnapshotSafetyAndDeterminism:
    @pytest.mark.parametrize("seed", range(20))
    def test_chaos_snapshot_keeps_invariants_and_linearizability(self, seed):
        c = Cluster(num_nodes=5, seed=seed, faults="chaos", config=SNAP)
        c.run(5000)  # invariants asserted every step during the run
        assert check(c.history).linearizable

    def test_snapshot_runs_replay_byte_identical(self):
        a = Cluster(num_nodes=5, seed=9, faults="chaos", config=SNAP).run(5000)
        b = Cluster(num_nodes=5, seed=9, faults="chaos", config=SNAP).run(5000)
        assert a.digest == b.digest

    @pytest.mark.parametrize("cfg", list(SNAPSHOT_GOLDENS))
    def test_snapshot_golden_digests_pinned(self, cfg):
        nodes, seed, faults, steps = cfg
        digest = Cluster(num_nodes=nodes, seed=seed, faults=faults, config=SNAP).run(steps).digest
        assert digest == SNAPSHOT_GOLDENS[cfg]

    def test_snapshots_actually_fire_in_the_sweep(self):
        total = 0
        for seed in range(10):
            c = Cluster(num_nodes=5, seed=seed, faults="chaos", config=SNAP)
            c.run(4000)
            total += sum(1 for _, k, _ in c.events if k == "snapshot")
        assert total > 0
