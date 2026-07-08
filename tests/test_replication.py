"""Log replication tests: commits, repair after divergence, nextIndex backoff."""

from harmonia.cluster import Cluster
from harmonia.node import FOLLOWER, LEADER


def has_leader(c):
    return c.leader() is not None


def elected(seed, **kw):
    c = Cluster(num_nodes=5, seed=seed, faults="none", **kw)
    assert c.run_until(has_leader, max_steps=5000)
    return c


def logs_converged(c):
    logs = [tuple(n.log) for n in c.nodes.values() if n.alive]
    return len(set(logs)) == 1 and len(logs[0]) > 0


class TestBasicReplication:
    def test_command_commits_at_leader(self):
        c = elected(seed=20)
        assert c.run_until(lambda c: (c.leader() or c.nodes[0]).commit_index >= 1, 20_000)

    def test_command_replicates_to_all_followers(self):
        c = elected(seed=21)
        c.run_until(lambda c: all(n.last_log_index() >= 1 for n in c.nodes.values()), 20_000)
        first = [n.log[0] for n in c.nodes.values()]
        assert len(set(first)) == 1

    def test_all_nodes_apply_committed_commands(self):
        c = elected(seed=22)
        assert c.run_until(lambda c: all(len(n.applied) >= 3 for n in c.nodes.values()), 40_000)
        prefixes = {tuple(n.applied[:3]) for n in c.nodes.values()}
        assert len(prefixes) == 1

    def test_commands_apply_in_submission_order(self):
        c = elected(seed=23)
        c.run_until(lambda c: all(len(n.applied) >= 5 for n in c.nodes.values()), 60_000)
        applied = c.nodes[0].applied[:5]
        nums = [int(cmd.split("-")[1]) for cmd in applied]
        assert nums == sorted(nums)

    def test_commit_index_monotonic_under_chaos(self):
        # the checker enforces CommitIndexMonotonic after every step; a full
        # chaos run passing means no regression ever happened
        c = Cluster(num_nodes=5, seed=24, faults="chaos")
        result = c.run(6000)
        assert result.stats["invariant_checks"] == result.steps

    def test_follower_commit_follows_leader_commit(self):
        c = elected(seed=25)
        c.run_until(lambda c: all(n.commit_index >= 2 for n in c.nodes.values()), 40_000)
        assert min(n.commit_index for n in c.nodes.values()) >= 2


class TestDivergenceAndRepair:
    def isolate_leader_with_unreplicated_entries(self, seed=30):
        """Classic divergence: leader is partitioned alone, accepts commands that
        never replicate, cluster elects a new leader and moves on."""
        c = Cluster(num_nodes=5, seed=seed, faults="none", client_interval=None)
        assert c.run_until(has_leader, max_steps=5000)
        old = c.leader()
        assert old is not None
        others = {i for i in c.nodes if i != old.id}
        c.set_partition([{old.id}, others])
        c.run(50)
        # unreplicated entries on the isolated old leader
        for k in range(3):
            assert old.client_command(f"orphan-{k}")
        orphan_len = old.last_log_index()
        # majority side elects a new leader and commits new commands
        assert c.run_until(
            lambda c: any(c.nodes[i].role == LEADER and c.nodes[i].term > old.term
                          for i in others), 30_000)
        new = max((c.nodes[i] for i in others if c.nodes[i].role == LEADER),
                  key=lambda n: n.term)
        for k in range(3):
            assert new.client_command(f"real-{k}")
        assert c.run_until(lambda c: new.commit_index >= 3, 30_000)
        return c, old, new, orphan_len

    def test_old_leader_diverges_while_partitioned(self):
        c, old, new, orphan_len = self.isolate_leader_with_unreplicated_entries()
        assert orphan_len >= 3
        assert [e.command for e in old.log[-3:]] == ["orphan-0", "orphan-1", "orphan-2"]
        assert old.log != new.log

    def test_heal_repairs_old_leader_log(self):
        c, old, new, _ = self.isolate_leader_with_unreplicated_entries()
        c.heal_partition()
        assert c.run_until(lambda c: old.log == new.log, 40_000)
        assert "orphan-0" not in [e.command for e in old.log]
        assert [e.command for e in old.log if e.command.startswith("real")] == \
               ["real-0", "real-1", "real-2"]

    def test_orphaned_entries_never_commit_or_apply(self):
        c, old, new, _ = self.isolate_leader_with_unreplicated_entries()
        c.heal_partition()
        c.run_until(lambda c: old.log == new.log, 40_000)
        c.run(4000)
        for n in c.nodes.values():
            assert not any(cmd.startswith("orphan") for cmd in n.applied)

    def test_old_leader_becomes_follower_after_heal(self):
        c, old, new, _ = self.isolate_leader_with_unreplicated_entries()
        c.heal_partition()
        assert c.run_until(lambda c: old.role == FOLLOWER, 40_000)

    def test_next_index_backoff_repairs_lagging_follower(self):
        c, old, new, _ = self.isolate_leader_with_unreplicated_entries()
        c.heal_partition()
        assert c.run_until(lambda c: new.match_index.get(old.id, 0) >= new.commit_index,
                           40_000)
        assert new.next_index[old.id] == new.match_index[old.id] + 1

    def test_crashed_leader_with_unreplicated_entries_repaired_on_resume(self):
        c = Cluster(num_nodes=5, seed=31, faults="none", client_interval=None)
        assert c.run_until(has_leader, max_steps=5000)
        old = c.leader()
        assert old is not None
        # isolate first so the appends cannot replicate, then crash
        others = {i for i in c.nodes if i != old.id}
        c.set_partition([{old.id}, others])
        c.run(30)
        old.client_command("lost-a")
        old.client_command("lost-b")
        c.run(300)  # in-flight AppendEntries die against the partition
        c.pause(old.id)
        c.heal_partition()
        assert c.run_until(
            lambda c: any(c.nodes[i].role == LEADER for i in others), 30_000)
        new = c.leader()
        assert new is not None
        new.client_command("survives")
        assert c.run_until(lambda c: new.commit_index >= 1, 30_000)
        c.resume(old.id)
        assert c.run_until(lambda c: old.log == new.log, 40_000)
        assert [e.command for e in old.log].count("survives") == 1
        assert not any(e.command.startswith("lost") for e in old.log)

    def test_logs_converge_after_chaos(self):
        """After a chaos run, stop the faults, heal everything, verify convergence."""
        from harmonia.sim import PROFILES
        c = Cluster(num_nodes=5, seed=32, faults="chaos")
        c.run(8000)
        c.profile = PROFILES["none"]      # fault driver goes quiet
        c.net.profile = PROFILES["none"]  # network becomes reliable
        c.heal_partition()
        for i in sorted(c.net.crashed):
            c.resume(i)
        assert c.run_until(
            lambda c: len({tuple(n.log) for n in c.nodes.values()}) == 1, 60_000)
