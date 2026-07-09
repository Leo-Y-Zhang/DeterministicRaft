"""The client-observed history recorded by a Cluster (harmonia/cluster.py + kv.py).

These tests are the substrate for the linearizability oracle: they establish that the
history is deterministic, faithfully records invoke/return, and -- in the fault-free case
-- is exactly consistent with a sequential replay of the committed log (a linearizability
precursor). They also lock the guarantee that recording the history never perturbs a run.
"""

from harmonia.cluster import Cluster
from harmonia.kv import Command, KVStateMachine


def _completed(history):
    return [e for e in history if e.completed]


def test_kv_apply_deterministic():
    """Same seed -> identical per-node KV stores and identical history, twice."""
    def run():
        c = Cluster(num_nodes=5, seed=31, faults="chaos")
        c.run(4000)
        stores = {i: c.nodes[i].kv.snapshot() for i in sorted(c.nodes)}
        history = [
            (e.client_id, e.req_id, e.op, e.key, e.value, e.expected,
             e.invoke_step, e.return_step, e.observed)
            for e in c.history
        ]
        return stores, history

    assert run() == run()


def test_history_records_invoke_and_return_steps():
    c = Cluster(num_nodes=3, seed=5, faults="none")
    c.run_until(lambda c: len(_completed(c.history)) >= 10, 60_000)
    completed = _completed(c.history)
    assert completed
    for e in completed:
        assert e.return_step is not None and e.invoke_step <= e.return_step
        assert e.observed is not None
    # every completed op was first invoked, so history holds at least as many rows
    assert len(c.history) >= len(completed)


def test_get_reflects_committed_puts():
    """In a fault-free run, every recorded result equals a sequential replay of the
    committed log -- i.e. gets observe the values prior puts committed. This is
    sequential consistency, the property the full linearizability oracle generalises."""
    c = Cluster(num_nodes=5, seed=17, faults="none")
    c.run_until(lambda c: len(_completed(c.history)) >= 20, 80_000)

    # replay the most-advanced node's committed log through a fresh state machine
    node = max(c.nodes.values(), key=lambda n: len(n.applied))
    kv = KVStateMachine()
    replay_result: dict[str, str] = {}
    for encoded in node.applied:
        replay_result[encoded] = kv.apply(Command.decode(encoded))

    checked = 0
    for e in _completed(c.history):
        encoded = Command(e.client_id, e.req_id, e.op, e.key, e.value, e.expected).encode()
        if encoded in replay_result:
            assert e.observed == replay_result[encoded]
            checked += 1
    assert checked >= 15  # actually verified a meaningful number of ops


def test_history_captures_a_successful_get_of_a_written_key():
    c = Cluster(num_nodes=3, seed=8, faults="none")
    c.run_until(lambda c: len(_completed(c.history)) >= 30, 80_000)
    gets = [e for e in _completed(c.history) if e.op == "get" and e.observed]
    # with writes hammering the same 3 keys, some get must observe a non-empty value
    assert gets, "expected at least one get to observe a committed value"


def test_history_recording_is_pure():
    """Recording the history must not change the trace digest (it is passive)."""
    for nodes, seed, faults, steps in [(5, 7, "chaos", 3000), (3, 2, "light", 2000)]:
        con = Cluster(num_nodes=nodes, seed=seed, faults=faults, record_history=True)
        coff = Cluster(num_nodes=nodes, seed=seed, faults=faults, record_history=False)
        assert con.run(steps).digest == coff.run(steps).digest
        assert con.history and not coff.history
