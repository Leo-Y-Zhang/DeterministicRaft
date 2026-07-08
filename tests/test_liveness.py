"""Bounded liveness: under benign fault profiles the cluster makes progress.

Raft only guarantees liveness during stable-enough periods (section 5.6 / FLP),
so these checks are bounded: some command must commit within a step budget.
"""

from harmonia.cluster import Cluster
from harmonia.node import LEADER

BUDGET = 12_000  # steps; generous but bounded


def commits_something(c):
    return any(n.commit_index >= 1 for n in c.nodes.values())


class TestLiveness:
    def test_none_profile_commits_within_budget(self):
        for seed in range(10):
            c = Cluster(num_nodes=5, seed=seed, faults="none")
            assert c.run_until(commits_something, BUDGET), f"no commit, seed {seed}"

    def test_light_profile_commits_within_budget(self):
        for seed in range(10):
            c = Cluster(num_nodes=5, seed=seed, faults="light")
            assert c.run_until(commits_something, BUDGET), f"no commit, seed {seed}"

    def test_three_node_cluster_commits(self):
        c = Cluster(num_nodes=3, seed=3, faults="light")
        assert c.run_until(commits_something, BUDGET)

    def test_progress_resumes_after_leader_crash(self):
        c = Cluster(num_nodes=5, seed=4, faults="none")
        assert c.run_until(commits_something, BUDGET)
        leader = c.leader()
        assert leader is not None
        before = max(n.commit_index for n in c.nodes.values())
        c.pause(leader.id)
        assert c.run_until(
            lambda c: max(n.commit_index for n in c.nodes.values() if n.alive) > before,
            30_000)

    def test_progress_resumes_after_partition_heals(self):
        c = Cluster(num_nodes=5, seed=5, faults="none")
        assert c.run_until(commits_something, BUDGET)
        c.set_partition([{0, 1}, {2, 3, 4}])
        c.run(3000)
        c.heal_partition()
        before = max(n.commit_index for n in c.nodes.values())
        assert c.run_until(
            lambda c: max(n.commit_index for n in c.nodes.values()) > before, 30_000)

    def test_commits_keep_flowing_without_faults(self):
        c = Cluster(num_nodes=5, seed=6, faults="none")
        assert c.run_until(
            lambda c: all(n.commit_index >= 10 for n in c.nodes.values()), 60_000)
