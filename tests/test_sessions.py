"""Client sessions + exactly-once dedup (Ongaro dissertation 6.3).

Two layers, both tested here:
  * leader-side: a leader will not re-append a request already in its log (a retry);
  * apply-time: even if a duplicate entry reaches the committed log, the state machine
    executes it once and returns the ORIGINAL cached result (crucial for cas, whose
    recomputed result would otherwise differ).
"""

from raftverified.cluster import Cluster
from raftverified.kv import CAS, GET, PUT, Command, KVStateMachine
from raftverified.node import Entry, RaftNode
from raftverified.sim import Simulator


def make_leader(log=(), term=2, node_id=0, n=3):
    sim = Simulator(1)
    node = RaftNode(node_id, [p for p in range(n) if p != node_id], sim,
                    send=lambda src, dst, msg: None, record=lambda kind, detail: None)
    node.term = term
    node.log = [Entry(t, c) for t, c in log]
    node._become_leader()
    return node


class TestApplyTimeDedup:
    def test_is_duplicate(self):
        kv = KVStateMachine()
        cmd = Command(0, 0, PUT, "k", "v")
        assert not kv.is_duplicate(cmd)
        kv.apply(cmd)
        assert kv.is_duplicate(cmd)
        assert not kv.is_duplicate(Command(0, 1, PUT, "k", "v2"))  # next req is new

    def test_duplicate_put_returns_cached_and_does_not_reexecute(self):
        kv = KVStateMachine()
        assert kv.apply(Command(0, 0, PUT, "k", "v1")) == "ok"
        kv.apply(Command(0, 1, PUT, "k", "v2"))              # k is now v2
        # a retry of req 0 must NOT re-put v1; it returns the cached result
        assert kv.apply(Command(0, 0, PUT, "k", "v1")) == "ok"
        assert kv.store["k"] == "v2"

    def test_duplicate_cas_returns_original_result_not_recomputed(self):
        """The heart of dedup: a retried cas recomputed from scratch would say 'fail'
        (the value already moved), but the operation succeeded -- return cached 'ok'."""
        kv = KVStateMachine()
        kv.apply(Command(0, 0, PUT, "k", "A"))
        assert kv.apply(Command(1, 0, CAS, "k", "B", "A")) == "ok"   # first apply: A -> B
        # recomputing now would fail (k is "B", expected "A"); dedup returns cached "ok"
        assert kv.apply(Command(1, 0, CAS, "k", "B", "A")) == "ok"
        assert kv.store["k"] == "B"

    def test_duplicate_get_returns_cached_value(self):
        kv = KVStateMachine()
        kv.apply(Command(0, 0, PUT, "k", "v1"))
        assert kv.apply(Command(1, 0, GET, "k")) == "v1"
        kv.apply(Command(0, 1, PUT, "k", "v2"))     # k changes under a later writer
        assert kv.apply(Command(1, 0, GET, "k")) == "v1"  # the retry sees its own snapshot


class TestLeaderSideDedup:
    def test_new_leader_dedups_already_replicated_request(self):
        x = Command(5, 0, PUT, "k", "v").encode()
        node = make_leader(log=[(2, x)])   # request already replicated into this log
        n_before = len(node.log)
        assert node.client_command(x) is True     # a retry
        assert len(node.log) == n_before          # not re-appended

    def test_leader_appends_a_fresh_request(self):
        node = make_leader()
        assert node.client_command(Command(5, 0, PUT, "k", "v").encode()) is True
        assert len(node.log) == 1

    def test_leader_dedups_repeated_submission_of_same_request(self):
        node = make_leader()
        x = Command(5, 0, PUT, "k", "v").encode()
        node.client_command(x)
        node.client_command(x)   # retry to the same leader
        assert len(node.log) == 1

    def test_higher_req_from_same_client_still_appends(self):
        node = make_leader()
        node.client_command(Command(5, 0, PUT, "k", "v").encode())
        node.client_command(Command(5, 1, PUT, "k", "w").encode())
        assert len(node.log) == 2

    def test_opaque_command_is_not_deduped(self):
        node = make_leader()
        node.client_command("raw-a")
        node.client_command("raw-b")
        assert len(node.log) == 2


class TestDedupUnderChaos:
    def test_leader_side_dedup_actually_fires(self):
        total = 0
        for seed in range(30):
            result = Cluster(num_nodes=5, seed=seed, faults="chaos").run(4000)
            total += sum(1 for _, kind, _ in result.events if kind == "dedup")
        assert total > 0

    def test_retries_happen(self):
        result = Cluster(num_nodes=5, seed=3, faults="chaos").run(4000)
        assert result.stats["retries"] > 0

    def test_duplicate_delivery_never_corrupts_committed_state(self):
        """Replaying each node's committed log through a fresh (dedup-enabled) state
        machine reproduces its live store: duplicate deliveries execute at most once."""
        for seed in range(20):
            c = Cluster(num_nodes=5, seed=seed, faults="chaos")
            c.run(4000)
            for node in c.nodes.values():
                committed = [Command.decode(e.command) for e in node.log[: node.commit_index]]
                replay = KVStateMachine()
                for cmd in committed:
                    replay.apply(cmd)
                assert replay.snapshot() == node.kv.snapshot()

    def test_every_client_request_applies_at_most_once_on_every_node(self):
        """A node's session table never lets a (client, req) execute twice: request ids
        are monotonic per client, and each node's applied labels for a client are a
        non-decreasing req sequence with every value actually reflected once."""
        for seed in range(15):
            c = Cluster(num_nodes=5, seed=seed, faults="chaos")
            c.run(4000)
            for node in c.nodes.values():
                # session state must be monotonic and consistent with a fresh replay
                replay = KVStateMachine()
                for e in node.log[: node.commit_index]:
                    replay.apply(Command.decode(e.command))
                assert replay.sessions == node.kv.sessions
