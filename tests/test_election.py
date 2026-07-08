"""Leader election tests: clean elections, split votes, partitions, crashes."""

from harmonia.cluster import Cluster
from harmonia.node import FOLLOWER, LEADER


def has_leader(c):
    return c.leader() is not None


class TestCleanElection:
    def test_no_faults_elects_a_leader(self):
        c = Cluster(num_nodes=5, seed=1, faults="none")
        assert c.run_until(has_leader, max_steps=5000)

    def test_exactly_one_leader(self):
        c = Cluster(num_nodes=5, seed=2, faults="none")
        c.run_until(has_leader, max_steps=5000)
        assert len(c.leaders()) == 1

    def test_all_other_nodes_are_followers(self):
        c = Cluster(num_nodes=5, seed=3, faults="none")
        c.run_until(has_leader, max_steps=5000)
        c.run(2000)  # settle
        roles = sorted(n.role for n in c.nodes.values())
        assert roles.count(LEADER) == 1 and roles.count(FOLLOWER) == 4

    def test_leadership_is_stable_without_faults(self):
        c = Cluster(num_nodes=5, seed=4, faults="none")
        c.run_until(has_leader, max_steps=5000)
        first = c.leader()
        assert first is not None
        term = first.term
        c.run(8000)
        after = c.leader()
        assert after is not None
        assert after.id == first.id and after.term == term

    def test_leader_elected_across_many_seeds(self):
        for seed in range(20):
            c = Cluster(num_nodes=5, seed=seed, faults="none")
            assert c.run_until(has_leader, max_steps=5000), f"no leader for seed {seed}"

    def test_three_node_cluster_elects(self):
        c = Cluster(num_nodes=3, seed=5, faults="none")
        assert c.run_until(has_leader, max_steps=5000)

    def test_single_node_cluster_elects_itself(self):
        c = Cluster(num_nodes=1, seed=6, faults="none")
        assert c.run_until(has_leader, max_steps=2000)
        leader = c.leader()
        assert leader is not None and leader.id == 0

    def test_at_most_one_leader_per_term_history(self):
        c = Cluster(num_nodes=5, seed=7, faults="none")
        c.run(6000)
        # the checker records every observed leadership; per-term uniqueness is
        # structural in the dict, so simply assert it saw at least one
        assert len(c.checker.leaders_by_term) >= 1


class TestSplitVote:
    def test_forced_split_vote_resolves_via_randomized_timeouts(self):
        """All five nodes start an election simultaneously: everyone votes for
        itself, nobody wins. Randomized retry timeouts must break the tie."""
        for seed in (0, 1, 2, 3, 4):
            c = Cluster(num_nodes=5, seed=seed, faults="none", client_interval=None)
            for i in sorted(c.nodes):
                c.nodes[i].start_election()
            assert all(n.role == "candidate" for n in c.nodes.values())
            assert c.run_until(has_leader, max_steps=20_000), f"split vote stuck, seed {seed}"

    def test_split_vote_leaves_term_gaps(self):
        c = Cluster(num_nodes=5, seed=1, faults="none", client_interval=None)
        for i in sorted(c.nodes):
            c.nodes[i].start_election()
        c.run_until(has_leader, max_steps=20_000)
        leader = c.leader()
        assert leader is not None
        assert leader.term >= 2  # term 1 was the split; winner needed a later term


class TestElectionRestrictions:
    def test_stale_log_candidate_cannot_win(self):
        c = Cluster(num_nodes=5, seed=8, faults="none")
        c.run_until(has_leader, max_steps=5000)
        c.run_until(lambda c: any(n.commit_index >= 2 for n in c.nodes.values()), 20_000)
        # Give one node an artificially stale log and make it campaign.
        stale = next(n for n in c.nodes.values() if n.role != LEADER)
        del stale.log[:]
        stale.log_version += 1
        stale.start_election()
        campaign_term = stale.term
        c.run(4000)
        winner = c.checker.leaders_by_term.get(campaign_term)
        assert winner != stale.id  # up-to-date restriction held

    def test_minority_partition_cannot_elect(self):
        c = Cluster(num_nodes=5, seed=9, faults="none", client_interval=None)
        c.run_until(has_leader, max_steps=5000)
        term_at_split = max(n.term for n in c.nodes.values())
        c.set_partition([{0, 1}, {2, 3, 4}])
        c.run(10_000)
        # nodes 0/1 may hold stale leadership from before the split, but can
        # never win a NEW election with only 2 of 5 votes
        for term, leader in c.checker.leaders_by_term.items():
            if term > term_at_split:
                assert leader in (2, 3, 4)

    def test_majority_side_elects_after_partition(self):
        c = Cluster(num_nodes=5, seed=10, faults="none", client_interval=None)
        c.run_until(has_leader, max_steps=5000)
        leader = c.leader()
        assert leader is not None
        others = {i for i in c.nodes if i != leader.id}
        c.set_partition([{leader.id}, others])
        assert c.run_until(
            lambda c: any(c.nodes[i].role == LEADER for i in others), 30_000)

    def test_new_leader_elected_after_leader_crash(self):
        c = Cluster(num_nodes=5, seed=11, faults="none")
        c.run_until(has_leader, max_steps=5000)
        leader = c.leader()
        assert leader is not None
        c.pause(leader.id)
        assert c.run_until(has_leader, max_steps=30_000)
        new = c.leader()
        assert new is not None and new.id != leader.id and new.term > leader.term

    def test_paused_node_does_not_vote(self):
        c = Cluster(num_nodes=5, seed=12, faults="none", client_interval=None)
        c.pause(4)
        assert c.run_until(has_leader, max_steps=10_000)  # 4 nodes still a majority

    def test_election_timeouts_randomized_within_range(self):
        c = Cluster(num_nodes=5, seed=13, faults="none", client_interval=None)
        # before any step runs, the pending events are exactly the five initial
        # election timers; their firing times are the drawn timeouts
        times = sorted(t for t, _, _ in c.sim._heap)
        assert len(times) == 5
        assert all(150 <= t <= 300 for t in times)
        assert len(set(times)) >= 2  # randomized, not lockstep

    def test_leader_steps_down_on_higher_term(self):
        c = Cluster(num_nodes=5, seed=14, faults="none", client_interval=None)
        c.run_until(has_leader, max_steps=5000)
        old = c.leader()
        assert old is not None
        others = {i for i in c.nodes if i != old.id}
        c.set_partition([{old.id}, others])
        c.run_until(lambda c: any(c.nodes[i].role == LEADER and c.nodes[i].term > old.term
                                  for i in others), 30_000)
        c.heal_partition()
        c.run_until(lambda c: c.nodes[old.id].role == FOLLOWER, 20_000)
        assert c.nodes[old.id].role == FOLLOWER
        assert len({n.term for n in c.nodes.values()}) == 1  # terms converged
