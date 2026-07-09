"""The log base-offset abstraction (harmonia/node.py).

All log indexing goes through helpers carrying a base_index so a future snapshot can
compact a prefix. This step ships with base_index == 0, so the helpers must behave exactly
like raw 1-based indexing -- that equivalence (and the unchanged golden digests) is the
whole correctness argument for the refactor.
"""

import random

from harmonia.node import Entry, RaftNode
from harmonia.sim import Simulator


def _node(log):
    sim = Simulator(1)
    node = RaftNode(0, [1, 2], sim, lambda a, b, c: None, lambda k, d: None)
    node.log = [Entry(t, c) for t, c in log]
    return node


def test_base_index_defaults_to_zero():
    node = _node([])
    assert node.base_index == 0 and node.base_term == 0


def test_helpers_match_raw_indexing_at_base_zero():
    rng = random.Random(0)
    for _ in range(200):
        log = [(rng.randint(1, 5), f"c{i}") for i in range(rng.randint(0, 12))]
        node = _node(log)
        assert node.last_log_index() == len(node.log)
        assert node.term_at(0) == 0
        for i in range(1, len(node.log) + 1):
            assert node.term_at(i) == node.log[i - 1].term
            assert node.entry_at(i) == node.log[i - 1]
        for start in range(1, len(node.log) + 2):
            assert node.log_suffix(start) == tuple(node.log[start - 1:])


def test_log_suffix_is_a_tuple_from_the_index():
    node = _node([(1, "a"), (1, "b"), (2, "c")])
    assert node.log_suffix(2) == (Entry(1, "b"), Entry(2, "c"))
    assert node.log_suffix(4) == ()
