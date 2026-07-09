"""Isolation tests for the linearizability oracle (harmonia/linearizability.py).

Hand-built histories with known verdicts, checked BEFORE the oracle is ever pointed at a
real cluster -- so a checker bug can't hide behind a (possibly also buggy) run. Real-time
intervals are given as (invoke_step, return_step); ops with disjoint intervals are ordered
by real time, overlapping ops may be linearized either way.
"""

from harmonia.cluster import Cluster
from harmonia.kv import CAS, GET, PUT, HistoryEntry
from harmonia.linearizability import check

_counter = [0]


def op(kind, key="x", value="", expected="", inv=0, ret=1, obs=""):
    _counter[0] += 1
    return HistoryEntry(_counter[0], 0, kind, key, value, expected,
                        invoke_step=inv, return_step=ret, observed=obs)


class TestTrivial:
    def test_empty_history_is_linearizable(self):
        assert check([]).linearizable

    def test_single_put_is_linearizable(self):
        assert check([op(PUT, value="1", obs="ok")]).linearizable

    def test_single_get_of_absent_key(self):
        assert check([op(GET, obs="")]).linearizable


class TestSequential:
    def test_put_then_get_sees_value(self):
        h = [op(PUT, value="1", inv=0, ret=2, obs="ok"),
             op(GET, inv=3, ret=4, obs="1")]
        assert check(h).linearizable

    def test_get_after_two_puts_sees_latest(self):
        h = [op(PUT, value="1", inv=0, ret=2, obs="ok"),
             op(PUT, value="2", inv=3, ret=5, obs="ok"),
             op(GET, inv=6, ret=7, obs="2")]
        assert check(h).linearizable

    def test_stale_read_after_completed_write_is_rejected(self):
        # put x=2 returned at 5, strictly before the get was invoked at 6, so the get
        # must observe 2; observing 1 has no legal ordering.
        h = [op(PUT, value="1", inv=0, ret=2, obs="ok"),
             op(PUT, value="2", inv=3, ret=5, obs="ok"),
             op(GET, inv=6, ret=7, obs="1")]
        result = check(h)
        assert not result.linearizable
        assert result.stuck  # reports a frontier


class TestConcurrent:
    def test_read_overlapping_write_may_see_new_value(self):
        # get overlaps the put; ordering put-before-get is legal
        h = [op(PUT, value="1", inv=0, ret=4, obs="ok"),
             op(GET, inv=1, ret=3, obs="1")]
        assert check(h).linearizable

    def test_read_overlapping_write_may_see_old_value(self):
        h = [op(PUT, value="1", inv=0, ret=4, obs="ok"),
             op(GET, inv=1, ret=3, obs="")]
        assert check(h).linearizable

    def test_pinning_reads_bracket_the_write(self):
        # early read sees old, later read sees new: the write linearizes between them
        h = [op(PUT, value="1", inv=0, ret=6, obs="ok"),
             op(GET, inv=1, ret=2, obs=""),
             op(GET, inv=3, ret=5, obs="1")]
        assert check(h).linearizable

    def test_non_monotonic_reads_within_a_write_are_rejected(self):
        # first read sees new (so the write is already linearized), a later read seeing
        # old would require the value to move backwards -- impossible for a register.
        h = [op(PUT, value="1", inv=0, ret=6, obs="ok"),
             op(GET, inv=1, ret=2, obs="1"),
             op(GET, inv=3, ret=5, obs="")]
        assert not check(h).linearizable


class TestCompareAndSet:
    def test_cas_success_then_get(self):
        h = [op(PUT, value="A", inv=0, ret=1, obs="ok"),
             op(CAS, value="B", expected="A", inv=2, ret=3, obs="ok"),
             op(GET, inv=4, ret=5, obs="B")]
        assert check(h).linearizable

    def test_cas_reporting_ok_but_value_never_changed_is_rejected(self):
        # cas claims ok (A->B) but a later definite read still sees A
        h = [op(PUT, value="A", inv=0, ret=1, obs="ok"),
             op(CAS, value="B", expected="A", inv=2, ret=3, obs="ok"),
             op(GET, inv=4, ret=5, obs="A")]
        assert not check(h).linearizable

    def test_two_concurrent_cas_only_one_wins(self):
        # both cas ops try A->? concurrently; exactly one may report ok
        h = [op(PUT, value="A", inv=0, ret=1, obs="ok"),
             op(CAS, value="B", expected="A", inv=2, ret=6, obs="ok"),
             op(CAS, value="C", expected="A", inv=3, ret=7, obs="fail"),
             op(GET, inv=8, ret=9, obs="B")]
        assert check(h).linearizable

    def test_two_concurrent_cas_both_reporting_ok_is_rejected(self):
        h = [op(PUT, value="A", inv=0, ret=1, obs="ok"),
             op(CAS, value="B", expected="A", inv=2, ret=6, obs="ok"),
             op(CAS, value="C", expected="A", inv=3, ret=7, obs="ok"),
             op(GET, inv=8, ret=9, obs="C")]
        assert not check(h).linearizable


class TestOracleProperties:
    def test_pending_ops_are_ignored(self):
        # an op that never returned imposes no constraint (never took effect here)
        h = [op(PUT, value="1", inv=0, ret=2, obs="ok"),
             HistoryEntry(99, 0, PUT, "x", "999", "", invoke_step=1, return_step=None),
             op(GET, inv=3, ret=4, obs="1")]
        assert check(h).linearizable

    def test_verdict_is_deterministic(self):
        h = [op(PUT, value="1", inv=0, ret=2, obs="ok"),
             op(GET, inv=6, ret=7, obs="1")]
        assert check(h).linearizable == check(h).linearizable
        assert check(h).linearization is not None

    def test_multi_key_independence(self):
        h = [op(PUT, key="a", value="1", inv=0, ret=1, obs="ok"),
             op(PUT, key="b", value="2", inv=0, ret=1, obs="ok"),
             op(GET, key="a", inv=2, ret=3, obs="1"),
             op(GET, key="b", inv=2, ret=3, obs="2")]
        assert check(h).linearizable

    def test_terminates_within_budget_on_wide_history(self):
        # many overlapping ops on distinct keys: must still terminate quickly
        h = []
        for i in range(40):
            h.append(op(PUT, key=f"k{i}", value="v", inv=0, ret=100, obs="ok"))
        result = check(h, budget=200_000)
        assert result.linearizable


class TestClusterHistories:
    """The oracle applied to real recorded runs: Raft guarantees linearizability, so
    every run's client history must pass -- and the check must not perturb the run."""

    def test_fault_free_runs_are_linearizable(self):
        for seed in range(10):
            c = Cluster(num_nodes=5, seed=seed, faults="none")
            c.run(6000)
            result = check(c.history)
            assert result.linearizable, f"seed {seed}: {result.message}"

    def test_light_fault_runs_are_linearizable(self):
        for seed in range(10):
            c = Cluster(num_nodes=5, seed=seed, faults="light")
            c.run(6000)
            assert check(c.history).linearizable, f"seed {seed}"

    def test_chaos_runs_are_linearizable(self):
        for seed in range(30):
            c = Cluster(num_nodes=5, seed=seed, faults="chaos")
            c.run(5000)
            result = check(c.history)
            assert result.linearizable, f"seed {seed}: {result.message}"

    def test_three_and_seven_node_chaos_are_linearizable(self):
        for nodes in (3, 7):
            for seed in range(8):
                c = Cluster(num_nodes=nodes, seed=seed, faults="chaos")
                c.run(4000)
                assert check(c.history).linearizable, f"nodes {nodes} seed {seed}"

    def test_oracle_is_a_pure_post_hoc_function(self):
        c = Cluster(num_nodes=5, seed=7, faults="chaos")
        result = c.run(4000)
        before = result.digest
        returns = [e.return_step for e in c.history]
        check(c.history)  # running the oracle must not mutate the run or its history
        assert c.result().digest == before
        assert [e.return_step for e in c.history] == returns

    def test_a_real_linearizable_run_yields_a_full_witness(self):
        c = Cluster(num_nodes=5, seed=1, faults="none")
        c.run(6000)
        result = check(c.history)
        assert result.linearizable
        completed = [e for e in c.history if e.completed]
        assert result.linearization is not None
        assert len(result.linearization) == len(completed)
