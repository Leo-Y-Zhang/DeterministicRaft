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
from test_node import make_node, replies_of

from deterministic_raft.cluster import Cluster
from deterministic_raft.invariants import InvariantChecker, InvariantViolation
from deterministic_raft.kv import Command, KVStateMachine, Snapshot
from deterministic_raft.linearizability import check
from deterministic_raft.node import (
    LEADER,
    AppendEntries,
    AppendReply,
    Entry,
    InstallSnapshot,
    RaftConfig,
)

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

    def test_run_summary_reports_the_applied_index_not_the_live_list_length(self):
        # The `applied` column of the run summary sits next to `commit` and `len`, which
        # are both LOGICAL indices; compaction trims the live `applied` list, so reporting
        # its length silently understates how far the state machine really got.
        c = Cluster(num_nodes=5, seed=3, faults="none", config=RaftConfig(
            snapshot_threshold=5))
        result = c.run(4000)
        assert any(n.base_index > 0 for n in c.nodes.values())  # the run really compacted
        for row in result.final:
            node = c.nodes[row["id"]]
            assert row["applied"] == node.last_applied


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
# Rebaselined once for membership: InstallSnapshot now carries the configuration in force
# at the snapshot index (a deliberate wire-format change; message reprs feed the trace).
SNAPSHOT_GOLDENS = {
    (5, 1, "chaos", 4000): "b60637aa24a1a2ce774c12e662da77d6904ed147849e354cb84e4aa118121018",
    (3, 4, "light", 3000): "bac9b391219428930fcda4d30c1080a057097f55c3e6831741624ec26c56f860",
    (5, 9, "chaos", 5000): "045420f3aca59e0e22459a8d46edf80397f3bcbdc2bdf093ba50f1e2e9cbd095",
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


def compacted_follower(term, base_index, base_term, tail, commit_index):
    """A follower whose prefix through ``base_index`` is folded into a snapshot, holding
    ``tail`` (term, command) pairs above it and having committed through ``commit_index``."""
    node, sent, _ = make_node(term=term, log=tail)
    node.base_index, node.base_term = base_index, base_term
    node.snapshot = Snapshot(base_index, base_term, {}, {}, (0, 1, 2))
    node.commit_index = node.last_applied = commit_index
    node.applied = [c for _, c in tail][: commit_index - base_index]
    return node, sent


class TestStaleTrafficAcrossTheSnapshotBoundary:
    """Delayed and duplicated RPCs that reach a follower after it compacted. Each of these
    was a real defect: the seeds below reproduced it in the chaos simulator before the fix."""

    def test_stale_append_below_the_base_never_truncates_committed_entries(self):
        # committed 6,7 live above a snapshot through 5 (term 2). A delayed AppendEntries
        # from this term's leader, sent before the compaction, starts at prev_index 0 with
        # a term-1 entry: comparing it against the compacted prefix read base_term (2) for
        # index 1, saw a "conflict" and deleted the live log at a negative offset.
        node, sent = compacted_follower(2, 5, 2, [(2, "f"), (2, "g")], commit_index=7)
        node.handle(1, AppendEntries(term=2, leader=1, prev_index=0, prev_term=0,
                                     entries=(Entry(1, "a"),), leader_commit=0))
        assert [(e.term, e.command) for e in node.log] == [(2, "f"), (2, "g")]
        assert node.last_log_index() == 7
        (reply,) = replies_of(sent, AppendReply)
        assert reply.success is True and reply.match_index <= node.commit_index

    def test_append_straddling_the_base_appends_only_the_new_suffix(self):
        node, sent = compacted_follower(2, 5, 2, [(2, "f")], commit_index=6)
        entries = tuple(Entry(2, c) for c in "defgh")  # logical indices 4..8
        node.handle(1, AppendEntries(term=2, leader=1, prev_index=3, prev_term=2,
                                     entries=entries, leader_commit=8))
        assert [e.command for e in node.log] == ["f", "g", "h"]
        assert node.commit_index == 8
        (reply,) = replies_of(sent, AppendReply)
        assert reply.success is True and reply.match_index == 8

    def test_covered_snapshot_reply_reports_only_the_committed_prefix(self):
        # base 5, then two UNCOMMITTED term-2 entries left over from an old leader. A
        # stale InstallSnapshot(4) from the term-3 leader used to be answered with
        # match_index = last_log_index (7): the leader then counted this follower as
        # holding indices 6-7 of ITS log, which it does not.
        node, sent = compacted_follower(3, 5, 1, [(2, "x"), (2, "y")], commit_index=5)
        node.handle(1, InstallSnapshot(term=3, leader=1, last_index=4, last_term=1,
                                       store={}, sessions={}, voters=(0, 1, 2)))
        (reply,) = replies_of(sent, AppendReply)
        assert reply.success is True and reply.match_index == 5

    def test_snapshot_below_the_commit_index_does_not_rewind_the_follower(self):
        # committed and applied through 10 with nothing compacted; a duplicated
        # InstallSnapshot(8) used to reset commit_index/last_applied to 8 and the state
        # machine to the older image (CommitIndexMonotonic in the simulator)
        node, sent, _ = make_node(term=1, log=[(1, f"c{i}") for i in range(10)])
        node.handle(1, AppendEntries(term=1, leader=1, prev_index=10, prev_term=1,
                                     entries=(), leader_commit=10))
        node.handle(1, InstallSnapshot(term=1, leader=1, last_index=8, last_term=1,
                                       store={}, sessions={}, voters=(0, 1, 2)))
        assert node.commit_index == node.last_applied == 10
        assert node.base_index == 0 and len(node.applied) == 10
        assert replies_of(sent, AppendReply)[-1].match_index == 10

    # (threshold, nodes, seed, steps): chaos runs that failed before the fix -- the first
    # two with CommitIndexMonotonic, the last crashing the leader with an IndexError after
    # a follower reported a match_index beyond the leader's own log
    @pytest.mark.parametrize("threshold,nodes,seed,steps",
                             [(3, 5, 2, 5500), (5, 3, 78, 2500), (2, 3, 1, 1500)])
    def test_small_threshold_chaos_seeds_that_used_to_fail(self, threshold, nodes, seed, steps):
        c = Cluster(num_nodes=nodes, seed=seed, faults="chaos",
                    config=RaftConfig(snapshot_threshold=threshold))
        c.run(steps)  # invariants asserted every step
        assert check(c.history).linearizable


class TestCheckerSeesCommitsBeforeCompaction:
    def test_every_commit_is_observed_even_when_it_triggers_compaction(self):
        # threshold 1: every commit compacts in the same step. The post-step check alone
        # saw commit_index == base_index and never recorded (or quorum-checked) anything.
        c = Cluster(num_nodes=3, seed=3, faults="none", config=RaftConfig(snapshot_threshold=1))
        c.run(3000)
        top = max(n.commit_index for n in c.nodes.values())
        assert top > 5
        assert set(c.checker.committed) == set(range(1, top + 1))
        assert set(c.checker.applied_at) == set(range(1, top + 1))

    def test_a_quorumless_commit_that_compacts_is_still_caught(self):
        c = Cluster(num_nodes=3, seed=3, faults="none", client_interval=None,
                    config=RaftConfig(snapshot_threshold=1))
        assert c.run_until(lambda c: c.leader() is not None)
        leader = c.leader()
        assert leader is not None
        leader.client_command("x")  # appended locally; replication still in flight
        with pytest.raises(InvariantViolation, match="CommitQuorum"):
            leader._set_commit_index(leader.last_log_index())  # commit it without a quorum
            c.checker.check(c.nodes, c.sim.steps)
