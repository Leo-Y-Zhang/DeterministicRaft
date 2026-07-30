"""Single-server cluster membership changes (dissertation ch. 4) + the per-configuration
checker generalization that lands in the same commit.

Three layers:
  * mechanism: a configuration entry takes effect on APPEND (pre-commit); the leader's two
    guards (one change in flight, current-term commit first) refuse unsafe changes; voters
    are counted per-configuration for elections, commits and reads;
  * durability: membership is DERIVED state -- it survives crash-restart, log truncation,
    compaction into a snapshot, and InstallSnapshot, always rebuilt from the log/snapshot;
  * the generalized InvariantChecker: majorities are judged against each node's CURRENT
    configuration (planted-bug FakeNodes prove it catches a quorumless commit and does not
    false-positive on a legal configuration transition), and membership churn under chaos
    keeps every safety invariant AND linearizability, deterministically.
"""

import pytest
from _goldens import digest_for, key
from _goldens import load as load_goldens
from test_invariants import FakeNode

from harmonia.bugs import NO_BUGS, Bugs
from harmonia.cluster import Cluster
from harmonia.invariants import InvariantChecker, InvariantViolation
from harmonia.kv import PUT, Command
from harmonia.linearizability import check
from harmonia.node import (
    CANDIDATE,
    FOLLOWER,
    LEADER,
    AppendEntries,
    AppendReply,
    Entry,
    InstallSnapshot,
    RaftConfig,
    RaftNode,
    RequestVote,
    VoteReply,
    decode_config,
    encode_config,
)
from harmonia.sim import Simulator


def make_node(node_id=0, n=5, initial_voters=None, term=0, config=None):
    sim = Simulator(1)
    sent = []
    node = RaftNode(node_id, [p for p in range(n) if p != node_id], sim,
                    send=lambda src, dst, msg: sent.append((dst, msg)),
                    record=lambda kind, detail: None,
                    config=config or RaftConfig(),
                    initial_voters=initial_voters)
    node.term = term
    return node, sent


def make_leader(node_id=0, n=5, initial_voters=None, term=2):
    node, sent = make_node(node_id, n, initial_voters, term)
    node._become_leader()
    node._committed_this_term = True  # a real leader earns this via a current-term commit
    sent.clear()
    return node, sent


def cfg_entry(voters, term=1):
    return Entry(term, encode_config(voters))


class TestConfigEncoding:
    def test_roundtrip(self):
        assert decode_config(encode_config((2, 0, 1))) == (0, 1, 2)

    def test_non_config_commands_decode_to_none(self):
        assert decode_config("0:1:put:k:v:") is None
        assert decode_config("opaque") is None

    def test_malformed_config_decodes_to_none(self):
        assert decode_config("cfg:1,zzz") is None


class TestChangeConfigGuards:
    def test_refuses_when_not_leader(self):
        node, _ = make_node(0, n=3)
        assert node.change_config((0, 1)) is False

    def test_refuses_a_two_server_change(self):
        node, _ = make_leader(0, n=5, initial_voters=(0, 1, 2))
        assert node.change_config((0, 1, 2, 3, 4)) is False
        assert node.voters == (0, 1, 2)

    def test_refuses_a_no_op_change(self):
        node, _ = make_leader(0, n=3)
        assert node.change_config((0, 1, 2)) is False

    def test_leader_never_removes_itself(self):
        node, _ = make_leader(0, n=3)
        assert node.change_config((1, 2)) is False

    def test_refuses_until_a_current_term_commit(self):
        # The May-2015 raft-dev amendment: a fresh leader must commit an entry in its OWN
        # term before it may append a configuration change (see also the injected bug that
        # drops this guard and loses a committed entry).
        node, _ = make_leader(0, n=5, initial_voters=(0, 1, 2))
        node._committed_this_term = False
        assert node.change_config((0, 1, 2, 3)) is False
        node._committed_this_term = True
        assert node.change_config((0, 1, 2, 3)) is True

    def test_refuses_while_previous_change_is_uncommitted(self):
        node, _ = make_leader(0, n=4, initial_voters=(0, 1, 2, 3))
        assert node.change_config((0, 1, 2)) is True   # in flight, not yet committed
        assert node.change_config((0, 1)) is False     # one change at a time (ch. 4.1)
        # a majority of the NEW configuration {0,1,2} acks -> the change commits
        node.handle(1, AppendReply(term=2, follower=1, success=True, match_index=1))
        node.handle(2, AppendReply(term=2, follower=2, success=True, match_index=1))
        assert node.commit_index == 1
        assert node.change_config((0, 1)) is True      # previous committed -> next allowed

    def test_change_is_effective_immediately_on_append(self):
        # dissertation ch. 4: the configuration governs as soon as it is APPENDED,
        # before it commits -- and replication immediately includes the new server.
        node, sent = make_leader(0, n=4, initial_voters=(0, 1, 2))
        assert node.change_config((0, 1, 2, 3)) is True
        assert node.voters == (0, 1, 2, 3)
        assert node.commit_index == 0  # not committed yet
        assert node.entry_at(1) == cfg_entry((0, 1, 2, 3), term=2)
        assert any(dst == 3 and isinstance(m, AppendEntries) for dst, m in sent)


class TestConfigAdoptionOnFollowers:
    def test_follower_adopts_config_on_append_pre_commit(self):
        node, _ = make_node(1, n=4, initial_voters=(0, 1, 2))
        node.handle(0, AppendEntries(term=1, leader=0, prev_index=0, prev_term=0,
                                     entries=(cfg_entry((0, 1, 2, 3)),), leader_commit=0))
        assert node.voters == (0, 1, 2, 3)
        assert node.commit_index == 0

    def test_truncating_the_config_entry_reverts_the_configuration(self):
        node, _ = make_node(1, n=4, initial_voters=(0, 1, 2))
        node.handle(0, AppendEntries(term=1, leader=0, prev_index=0, prev_term=0,
                                     entries=(cfg_entry((0, 1, 2, 3)),), leader_commit=0))
        # a new leader overwrites the uncommitted config entry with its own entry
        node.handle(2, AppendEntries(term=2, leader=2, prev_index=0, prev_term=0,
                                     entries=(Entry(2, "unrelated"),), leader_commit=0))
        assert node.voters == (0, 1, 2)

    def test_a_server_outside_the_configuration_never_campaigns(self):
        node, sent = make_node(3, n=4, initial_voters=(0, 1, 2))
        node.start_election()
        assert node.role == FOLLOWER
        assert node.term == 0
        assert sent == []

    def test_votes_are_counted_against_the_current_configuration(self):
        node, sent = make_node(0, n=5, initial_voters=(0, 1, 2))
        node.start_election()
        assert node.role == CANDIDATE
        assert {dst for dst, m in sent if isinstance(m, RequestVote)} == {1, 2}
        node.handle(4, VoteReply(term=1, voter=4, granted=True))
        assert node.role == CANDIDATE  # n4 is not a voter; its vote must not count
        node.handle(1, VoteReply(term=1, voter=1, granted=True))
        assert node.role == LEADER     # {0,1} is a majority of {0,1,2}

    def test_commit_counts_a_majority_of_the_current_configuration(self):
        node, _ = make_leader(0, n=5, initial_voters=(0, 1, 2))
        node.client_command("op")
        node.handle(4, AppendReply(term=2, follower=4, success=True, match_index=1))
        assert node.commit_index == 0  # n4 is not a voter; its ack must not count
        node.handle(1, AppendReply(term=2, follower=1, success=True, match_index=1))
        assert node.commit_index == 1


class TestMembershipSurvivesRestartAndCompaction:
    def test_crash_restart_rebuilds_voters_from_the_log(self):
        node, _ = make_node(1, n=4, initial_voters=(0, 1, 2))
        node.handle(0, AppendEntries(term=1, leader=0, prev_index=0, prev_term=0,
                                     entries=(cfg_entry((0, 1, 2, 3)),), leader_commit=0))
        node.pause()
        node.resume()
        assert node.voters == (0, 1, 2, 3)  # derived from the persisted log

    def test_compaction_folds_the_config_into_the_snapshot(self):
        node, _ = make_node(1, n=4, initial_voters=(0, 1, 2),
                            config=RaftConfig(snapshot_threshold=2))
        entries = (cfg_entry((0, 1, 2, 3)),
                   Entry(1, Command(0, 0, PUT, "k", "v1").encode()),
                   Entry(1, Command(0, 1, PUT, "k", "v2").encode()))
        node.handle(0, AppendEntries(term=1, leader=0, prev_index=0, prev_term=0,
                                     entries=entries, leader_commit=3))
        assert node.base_index == 3 and node.snapshot is not None
        assert node.snapshot.voters == (0, 1, 2, 3)
        node.pause()
        node.resume()
        assert node.voters == (0, 1, 2, 3)  # rebuilt from the persisted snapshot

    def test_snapshot_records_the_config_at_its_boundary_not_a_newer_tail(self):
        node, _ = make_node(1, n=4, initial_voters=(0, 1, 2),
                            config=RaftConfig(snapshot_threshold=2))
        entries = (cfg_entry((0, 1, 2, 3)),
                   Entry(1, Command(0, 0, PUT, "k", "v1").encode()),
                   cfg_entry((0, 1, 2)))  # newer config, NOT covered by the snapshot
        node.handle(0, AppendEntries(term=1, leader=0, prev_index=0, prev_term=0,
                                     entries=entries, leader_commit=2))
        assert node.base_index == 2
        assert node.snapshot is not None and node.snapshot.voters == (0, 1, 2, 3)
        assert node.voters == (0, 1, 2)  # the uncompacted tail entry still governs
        # a new leader truncates that tail entry -> the snapshot config governs again
        node.handle(2, AppendEntries(term=2, leader=2, prev_index=2, prev_term=1,
                                     entries=(Entry(2, "z"),), leader_commit=2))
        assert node.voters == (0, 1, 2, 3)

    def test_install_snapshot_carries_the_configuration(self):
        node, _ = make_node(1, n=4, initial_voters=(0, 1, 2))
        node.handle(0, InstallSnapshot(term=1, leader=0, last_index=5, last_term=1,
                                       store={"k": "v"}, sessions={}, voters=(0, 1, 3)))
        assert node.base_index == 5
        assert node.voters == (0, 1, 3)


class TestPerConfigQuorumChecker:
    def test_commit_without_a_config_majority_detected(self):
        # planted bug: a "leader" claims a commit that only it holds -- 1 of 3 voters
        a = FakeNode(0, role=LEADER, term=1, log=[(1, "x")], commit_index=1,
                     voters=(0, 1, 2))
        b = FakeNode(1, voters=(0, 1, 2))
        c = FakeNode(2, voters=(0, 1, 2))
        with pytest.raises(InvariantViolation, match="CommitQuorum"):
            InvariantChecker(seed=1).check({0: a, 1: b, 2: c}, 1)

    def test_commit_with_a_config_majority_passes(self):
        a = FakeNode(0, role=LEADER, term=1, log=[(1, "x")], commit_index=1,
                     voters=(0, 1, 2))
        b = FakeNode(1, log=[(1, "x")], voters=(0, 1, 2))
        c = FakeNode(2, voters=(0, 1, 2))
        InvariantChecker(seed=1).check({0: a, 1: b, 2: c}, 1)  # 2 of 3 hold it

    def test_majority_of_the_new_smaller_config_is_legal(self):
        # after removals the configuration is {0,1}: a 2-of-5-universe commit is legal
        # because majorities are judged against the CURRENT config, never the universe
        a = FakeNode(0, role=LEADER, term=1, log=[(1, "x")], commit_index=1, voters=(0, 1))
        b = FakeNode(1, log=[(1, "x")], voters=(0, 1))
        rest = {i: FakeNode(i, voters=(0, 1)) for i in (2, 3, 4)}
        InvariantChecker(seed=1).check({0: a, 1: b, **rest}, 1)  # no false positive

    def test_a_compacted_holder_still_counts_toward_the_quorum(self):
        # n1 folded the committed entry into its snapshot; it still holds it
        a = FakeNode(0, role=LEADER, term=1, log=[(1, "x")], commit_index=1,
                     voters=(0, 1, 2))
        b = FakeNode(1, base_index=1, base_term=1, voters=(0, 1, 2))
        c = FakeNode(2, voters=(0, 1, 2))
        InvariantChecker(seed=1).check({0: a, 1: b, 2: c}, 1)

    def test_disjoint_majorities_committing_different_entries_detected(self):
        # cross-config divergence: config {0,1} committed "x" at index 1 while config
        # {3,4} committed "y" there -- the generalized checker still catches it
        checker = InvariantChecker(seed=1)
        a = FakeNode(0, role=LEADER, term=1, log=[(1, "x")], commit_index=1, voters=(0, 1))
        b = FakeNode(1, log=[(1, "x")], voters=(0, 1))
        c = FakeNode(3, role=LEADER, term=2, log=[(2, "y")], commit_index=1, voters=(3, 4))
        d = FakeNode(4, log=[(2, "y")], voters=(3, 4))
        with pytest.raises(InvariantViolation, match="StateMachineSafety|LeaderCompleteness"):
            checker.check({0: a, 1: b, 3: c, 4: d}, 1)


class TestMembershipEndToEnd:
    def test_membership_mode_needs_three_nodes(self):
        with pytest.raises(ValueError):
            Cluster(num_nodes=2, membership=True)

    def test_initial_voters_must_name_existing_nodes(self):
        with pytest.raises(ValueError):
            Cluster(num_nodes=3, initial_voters=(0, 7))

    def test_churn_driver_actually_reconfigures(self):
        c = Cluster(num_nodes=5, seed=1, faults="none", membership=True)
        c.run(6000)
        assert c.stats["config_changes"] >= 3
        leader = c.leader()
        assert leader is not None and set(leader.voters) <= set(range(5))

    def test_spare_server_gets_added_and_catches_up(self):
        # membership mode starts n4 OUTSIDE the configuration; the churn driver adds it
        c = Cluster(num_nodes=5, seed=2, faults="none", membership=True)
        added = c.run_until(
            lambda c: (lea := c.leader()) is not None and 4 in lea.voters
            and c.nodes[4].commit_index > 0, 30_000)
        assert added

    @pytest.mark.parametrize("seed", range(20))
    def test_chaos_churn_keeps_invariants_and_linearizability(self, seed):
        c = Cluster(num_nodes=5, seed=seed, faults="chaos", membership=True)
        c.run(5000)  # invariants asserted every step during the run
        assert check(c.history).linearizable

    def test_chaos_churn_with_snapshots_stays_safe(self):
        # membership x compaction interplay: configs cross snapshot boundaries and
        # InstallSnapshot re-seeds voters, under partitions and crash-restarts
        snap = RaftConfig(snapshot_threshold=10)
        snapshots = installs = changes = 0
        for seed in range(10):
            c = Cluster(num_nodes=5, seed=seed, faults="chaos", membership=True, config=snap)
            c.run(5000)
            assert check(c.history).linearizable
            snapshots += sum(1 for _, k, _ in c.events if k == "snapshot")
            installs += sum(1 for _, k, _ in c.events if k == "installsnap")
            changes += c.stats["config_changes"]
        assert snapshots > 0 and installs > 0 and changes > 0  # all three actually mixed

    def test_replay_is_byte_identical(self):
        a = Cluster(num_nodes=5, seed=9, faults="chaos", membership=True).run(5000)
        b = Cluster(num_nodes=5, seed=9, faults="chaos", membership=True).run(5000)
        assert a.digest == b.digest


BUGGY = Bugs(drop_config_commit_guard=True)


class TestOngaroMembershipBug:
    """The May-2015 raft-dev bug, replayed EXACTLY (universe {0..4}, voters {0,1,2,3}):
    n0 (leader, term 1) starts adding n4 with nothing committed in its own term;
    concurrently n1 (leader, term 2) removes n0 and commits entries under the shrunken
    configuration with majority {1,2}; n0 is then re-elected with votes {0,3,4} -- a
    majority of ITS five-server configuration, DISJOINT from {1,2} -- and overwrites the
    committed entries. The amended algorithm (commit in your own term first) refuses both
    unguarded appends, and the very commit that satisfies the guard also blocks the
    disjoint election."""

    def _wire(self, bugs=NO_BUGS):
        sim = Simulator(1)
        outbox = []
        nodes = {
            i: RaftNode(i, [p for p in range(5) if p != i], sim,
                        send=lambda src, dst, msg: outbox.append((src, dst, msg)),
                        record=lambda kind, detail: None,
                        bugs=bugs, initial_voters=(0, 1, 2, 3))
            for i in range(5)
        }
        return nodes, outbox

    @staticmethod
    def _deliver(nodes, outbox, dst, kind):
        """Deliver (and consume) every queued message of ``kind`` addressed to ``dst``;
        everything else stays queued until explicitly dropped (a partition, scripted)."""
        matched = [(s, d, m) for s, d, m in outbox if d == dst and isinstance(m, kind)]
        for item in matched:
            outbox.remove(item)
        for src, _, msg in matched:
            nodes[dst].handle(src, msg)

    def _elect(self, nodes, out, cand, granters):
        nodes[cand].start_election()
        for g in granters:
            self._deliver(nodes, out, g, RequestVote)
        self._deliver(nodes, out, cand, VoteReply)

    def test_the_original_algorithm_loses_a_committed_entry(self):
        nodes, out = self._wire(BUGGY)
        checker = InvariantChecker(seed=0)
        n0, n1 = nodes[0], nodes[1]

        # 1. n0 wins term 1 with votes {0,1,2}; its heartbeats are lost (partition)
        self._elect(nodes, out, 0, granters=[1, 2])
        assert n0.role == LEADER
        out.clear()

        # 2. BUG: n0 starts adding n4 with NO commit in its own term; the config entry
        #    C1 = {0,1,2,3,4} reaches only n4 before n0 is cut off
        assert n0.change_config((0, 1, 2, 3, 4)) is True
        self._deliver(nodes, out, 4, AppendEntries)
        assert nodes[4].voters == (0, 1, 2, 3, 4)
        out.clear()
        checker.check(nodes, 1)  # nothing has gone wrong yet

        # 3. n1 wins term 2 with votes {1,2,3} -- a majority of {0,1,2,3}, none of whom
        #    ever saw C1
        self._elect(nodes, out, 1, granters=[2, 3])
        assert n1.role == LEADER and n1.term == 2
        out.clear()

        # 4. BUG again: n1 removes n0 pre-commit -> C2 = {1,2,3}, which reaches n2 and
        #    commits with the two-server majority {1,2} of the NEW configuration
        assert n1.change_config((1, 2, 3)) is True
        self._deliver(nodes, out, 2, AppendEntries)
        self._deliver(nodes, out, 1, AppendReply)
        assert n1.commit_index == 1
        out.clear()

        # 5. n1 commits a client entry the same way -- the entry that will be lost
        assert n1.client_command("committed-then-lost") is True
        self._deliver(nodes, out, 2, AppendEntries)
        self._deliver(nodes, out, 1, AppendReply)
        assert n1.commit_index == 2
        out.clear()
        checker.check(nodes, 2)  # still legal: every commit is held by its majority

        # 6. n0, which heard none of it, reaches term 3 and wins with {0,3,4} -- a
        #    majority of ITS configuration C1, disjoint from n1's commit majority {1,2}
        n0.start_election()  # term 2: nobody hears it
        out.clear()
        self._elect(nodes, out, 0, granters=[3, 4])
        assert n0.role == LEADER and n0.term == 3

        # 7. the checker catches the loss the moment the stale leader returns
        with pytest.raises(InvariantViolation, match="LeaderCompleteness"):
            checker.check(nodes, 3)

    def test_the_amended_algorithm_refuses_the_same_script(self):
        nodes, out = self._wire(NO_BUGS)
        checker = InvariantChecker(seed=0)
        n0, n1 = nodes[0], nodes[1]

        self._elect(nodes, out, 0, granters=[1, 2])
        assert n0.role == LEADER
        out.clear()

        # the guard refuses n0's add: nothing committed in term 1 yet
        assert n0.change_config((0, 1, 2, 3, 4)) is False

        self._elect(nodes, out, 1, granters=[2, 3])
        assert n1.role == LEADER and n1.term == 2
        out.clear()

        # ...and refuses n1's remove for the same reason
        assert n1.change_config((1, 2, 3)) is False

        # the fix in action: n1 must first commit an entry in ITS term under the OLD
        # four-server configuration, which takes three acks -- {1,2,3}...
        assert n1.client_command("current-term-commit") is True
        self._deliver(nodes, out, 2, AppendEntries)
        self._deliver(nodes, out, 3, AppendEntries)
        self._deliver(nodes, out, 1, AppendReply)
        assert n1.commit_index == 1
        # ...and only then may it reconfigure
        assert n1.change_config((1, 2, 3)) is True
        out.clear()

        # n0 attempts the disjoint election -- and cannot win it: n3 now holds a term-2
        # entry so it refuses, and n4 is not even in n0's (unchanged) configuration
        n0.start_election()  # term 2: nobody hears it
        out.clear()
        self._elect(nodes, out, 0, granters=[3, 4])
        assert n0.role == CANDIDATE  # never leader: the committed entry stays safe
        checker.check(nodes, 1)  # and the checker agrees nothing was lost


class TestHistoricalBugUnderChurn:
    def test_pinned_seed_reproduces_the_loss_naturally(self):
        # Found by a 1000-seed hunt. The interleaving needs a six-server universe (only
        # there can two five-server configurations have disjoint 3-majorities), a leader
        # cut off mid-change, a second unguarded leader, and a later re-election -- rare
        # enough that the bug survived public review from 2014 to May 2015.
        with pytest.raises(InvariantViolation, match="LeaderCompleteness"):
            Cluster(num_nodes=6, seed=354, faults="chaos", membership=True,
                    bugs=BUGGY).run(6000)

    def test_five_server_universe_survives_even_the_buggy_algorithm(self):
        # Geometry as a control: with add-first churn on a five-server universe, every
        # reachable pair of configurations has overlapping majorities (3+3 > 5), so the
        # sweep stays clean even with the guard dropped -- the bug NEEDS the sixth server.
        for seed in range(10):
            Cluster(num_nodes=5, seed=seed, faults="chaos", membership=True,
                    bugs=BUGGY).run(4000)  # no InvariantViolation


# Membership-enabled configs: own pinned golden matrix (default digests stay untouched).
MEMBERSHIP_GOLDENS = {
    (5, 1, "chaos", 4000): "e2a45649bd89ee47d04a45a0a756ce1dadf01efdd43ee1e99e42730a912f6761",
    (3, 4, "light", 3000): "6fb6d3d36b1df0c4505dc3a519bd72d772a3229411cc69b164b0a016a56c2678",
    (5, 9, "chaos", 5000): "9cbe63cccdff94fbd7c4b7104acb3a9e21d964a5d0ae3e6151e2bb8dcf33882e",
}


@pytest.mark.parametrize("cfg", list(MEMBERSHIP_GOLDENS))
def test_membership_golden_digests_pinned(cfg):
    nodes, seed, faults, steps = cfg
    digest = Cluster(num_nodes=nodes, seed=seed, faults=faults, membership=True).run(steps).digest
    assert digest == MEMBERSHIP_GOLDENS[cfg]


def test_membership_off_leaves_default_goldens_untouched():
    """With membership present-but-off, the v1.0.0 default-config goldens are BYTE-IDENTICAL
    (the whole corpus is re-pinned by test_goldens.py; this spot-checks the tie explicitly)."""
    goldens = load_goldens()
    for cfg in [(3, 1, "none", 2000), (5, 7, "chaos", 3000)]:
        assert digest_for(cfg) == goldens[key(cfg)]["digest"]


def test_unarmed_bug_registry_leaves_membership_goldens_untouched():
    """A membership run with the (all-off) bug registry explicitly armed is byte-identical
    to its pinned golden -- the sixth injectable is invisible until enabled."""
    nodes, seed, faults, steps = cfg = (5, 1, "chaos", 4000)
    digest = Cluster(num_nodes=nodes, seed=seed, faults=faults, membership=True,
                     bugs=Bugs()).run(steps).digest
    assert digest == MEMBERSHIP_GOLDENS[cfg]
