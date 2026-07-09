"""Real persistence + crash-restart with true volatile-state loss (Figure 2).

A crash now discards ALL volatile state; only currentTerm, votedFor and the log survive.
The state machine is rebuilt by re-applying the persisted log after restart. These tests
pin the persistent/volatile split, the "no double vote after restart" guarantee, and that
crash-restart never corrupts state or breaks safety/linearizability under chaos.
"""

from harmonia.cluster import Cluster
from harmonia.kv import Command, KVStateMachine
from harmonia.linearizability import check
from harmonia.node import FOLLOWER, LEADER, Entry, RaftNode, RequestVote, VoteReply
from harmonia.sim import Simulator


def make_node(node_id=0, n=3, term=0):
    sim = Simulator(1)
    sent = []
    node = RaftNode(node_id, [p for p in range(n) if p != node_id], sim,
                    send=lambda src, dst, msg: sent.append((dst, msg)),
                    record=lambda kind, detail: None)
    node.term = term
    return node, sent


class TestPersistentVolatileSplit:
    def test_persistent_state_survives_a_crash(self):
        node, _ = make_node(term=7)
        node.voted_for = 3
        node.log = [Entry(7, "a"), Entry(7, "b")]
        node.commit_index = node.last_applied = 2
        node.applied = ["a", "b"]
        node.kv.store["x"] = "1"
        node.role = LEADER
        node.next_index = {1: 3}

        node.pause()

        # persistent triple intact
        assert node.term == 7
        assert node.voted_for == 3
        assert [e.command for e in node.log] == ["a", "b"]
        # everything volatile is gone
        assert node.commit_index == 0 and node.last_applied == 0
        assert node.applied == [] and node.kv.store == {}
        assert node.role == FOLLOWER and node.next_index == {}
        assert node.incarnation == 1

    def test_incarnation_increments_per_crash(self):
        node, _ = make_node()
        assert node.incarnation == 0
        node.pause()
        node.resume()
        assert node.incarnation == 1
        node.pause()
        node.resume()
        assert node.incarnation == 2


class TestVotePersistence:
    def test_no_double_vote_after_restart(self):
        node, sent = make_node(term=5)
        node.handle(1, RequestVote(term=5, candidate=1, last_log_index=0, last_log_term=0))
        assert node.voted_for == 1

        node.pause()
        node.resume()  # votedFor is persistent -> survives the crash

        sent.clear()
        node.handle(2, RequestVote(term=5, candidate=2, last_log_index=0, last_log_term=0))
        reply = next(m for _, m in sent if isinstance(m, VoteReply))
        assert reply.granted is False  # already voted for n1 in term 5
        assert node.voted_for == 1


class TestCrashRestartUnderChaos:
    def test_state_machine_rebuilt_after_crash_without_double_apply(self):
        """Replaying a (possibly crash-restarted) node's committed log through a fresh
        state machine reproduces its live store and sessions: recovery re-applies the log
        exactly once, never doubly."""
        saw_crash = False
        for seed in range(15):
            c = Cluster(num_nodes=5, seed=seed, faults="chaos")
            c.run(5000)
            for node in c.nodes.values():
                if node.incarnation > 0:
                    saw_crash = True
                replay = KVStateMachine()
                for e in node.log[: node.commit_index]:
                    replay.apply(Command.decode(e.command))
                assert replay.snapshot() == node.kv.snapshot()
                assert replay.sessions == node.kv.sessions
        assert saw_crash, "expected chaos to crash and restart at least one node"

    def test_chaos_with_real_crashes_keeps_invariants_and_linearizability(self):
        # invariants are asserted after every step during run(); linearizability after.
        for seed in range(20):
            c = Cluster(num_nodes=5, seed=seed, faults="chaos")
            c.run(5000)
            assert check(c.history).linearizable, f"seed {seed}"

    def test_replay_is_byte_identical_with_real_crashes(self):
        a = Cluster(num_nodes=5, seed=3, faults="chaos").run(5000)
        b = Cluster(num_nodes=5, seed=3, faults="chaos").run(5000)
        assert a.digest == b.digest
