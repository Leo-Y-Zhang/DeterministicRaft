"""ReadIndex linearizable reads (section 8): the capstone.

With ReadIndex enabled, a get is answered from local state WITHOUT a log entry, but only
after the leader confirms it still leads via a fresh heartbeat round (a majority of acks in
its term) AND has committed an entry in its own term. These tests pin the confirmation
requirement, prove reads are linearizable in the chaos sweep, and keep the naive
'read local state' bug as the oracle's positive control.
"""

import pytest

from raftverified.bugs import Bugs
from raftverified.cluster import Cluster
from raftverified.invariants import InvariantViolation
from raftverified.kv import GET, PUT, Command
from raftverified.linearizability import check
from raftverified.node import RaftNode, ReadAck
from raftverified.sim import Simulator


def _leader(store=None):
    sent = []
    served = []
    sim = Simulator(1)
    node = RaftNode(0, [1, 2], sim, lambda s, d, m: sent.append((d, m)),
                    lambda k, d: None, on_apply=lambda enc, res: served.append((enc, res)))
    node.term = 2
    node._become_leader()
    node._committed_this_term = True
    node.commit_index = node.last_applied = 0
    if store:
        node.kv.store.update(store)
    sent.clear()
    return node, sent, served


GET_K = Command(0, 0, GET, "k").encode()


class TestReadConfirmation:
    def test_read_not_served_without_majority_acks(self):
        node, sent, served = _leader(store={"k": "v1"})
        node.request_read(GET_K)
        # only the leader itself has "acked" -> no majority in a 3-node cluster
        assert served == []
        assert any(m.__class__.__name__ == "ReadHeartbeat" for _, m in sent)

    def test_read_served_after_majority_acks(self):
        node, _, served = _leader(store={"k": "v1"})
        node.request_read(GET_K)
        node.handle(1, ReadAck(term=2, follower=1, read_id=0))  # now 2/3 -> majority
        assert served == [(GET_K, "v1")]

    def test_read_refused_before_a_current_term_commit(self):
        node, _, served = _leader(store={"k": "v1"})
        node._committed_this_term = False  # fresh leader, commit index not yet trustworthy
        assert node.request_read(GET_K) is False
        assert served == []

    def test_stepped_down_leader_drops_pending_reads(self):
        node, _, served = _leader(store={"k": "v1"})
        node.request_read(GET_K)
        node._become_follower(3)  # a higher term arrives
        node.handle(1, ReadAck(term=2, follower=1, read_id=0))  # stale ack, ignored
        assert served == []


class TestReadIndexUnderChaos:
    @pytest.mark.parametrize("seed", range(20))
    def test_reads_are_linearizable(self, seed):
        c = Cluster(num_nodes=5, seed=seed, faults="chaos", read_index=True)
        c.run(5000)  # invariants asserted every step
        assert check(c.history).linearizable

    def test_reads_actually_get_served(self):
        total = 0
        for seed in range(10):
            c = Cluster(num_nodes=5, seed=seed, faults="chaos", read_index=True)
            c.run(4000)
            total += sum(1 for _, k, _ in c.events if k == "readserved")
        assert total > 0

    def test_replay_is_byte_identical(self):
        a = Cluster(num_nodes=5, seed=8, faults="chaos", read_index=True).run(5000)
        b = Cluster(num_nodes=5, seed=8, faults="chaos", read_index=True).run(5000)
        assert a.digest == b.digest

    def test_read_reflects_a_prior_committed_write(self):
        # a healthy run: every served read's value is consistent with a sequential replay
        c = Cluster(num_nodes=5, seed=1, faults="none", read_index=True)
        c.run(8000)
        assert check(c.history).linearizable
        reads = [e for e in c.history if e.op == GET and e.completed]
        writes = [e for e in c.history if e.op == PUT and e.completed]
        assert reads and writes  # both kinds exercised


class TestNaiveReadStillCaught:
    def test_stale_local_reads_bug_is_caught_even_with_readindex_available(self):
        # the bug skips the ReadIndex confirmation -> a stale leader serves old data,
        # which the linearizability oracle still catches.
        for seed in range(30):
            c = Cluster(num_nodes=3, seed=seed, faults="chaos", read_index=True,
                        bugs=Bugs(stale_local_reads=True))
            try:
                c.run(6000)
            except InvariantViolation:
                continue
            if not check(c.history).linearizable:
                return
        pytest.fail("oracle failed to catch a stale read")


# ReadIndex-enabled configs: own pinned golden matrix (default digests stay untouched).
READINDEX_GOLDENS = {
    (5, 2, "chaos", 4000): "d1ae4908c6212d8bf603fb1ff516b4b8718ce22fa72a0c12c066fee7488d3b7c",
    (3, 5, "light", 3000): "45defca94401cc1f846f9a15515a4b83a96dd9939fbb6867eee86b7c5b778c52",
    (5, 8, "chaos", 5000): "a029ba3b241ac1170dc50c6c97678262087de5c6986820f140fa23819f70191f",
}


@pytest.mark.parametrize("cfg", list(READINDEX_GOLDENS))
def test_readindex_golden_digests_pinned(cfg):
    nodes, seed, faults, steps = cfg
    digest = Cluster(num_nodes=nodes, seed=seed, faults=faults, read_index=True).run(steps).digest
    assert digest == READINDEX_GOLDENS[cfg]
