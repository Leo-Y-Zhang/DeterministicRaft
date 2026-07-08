"""Invariant sweeps: all five safety properties across many seeded chaos runs.

The whole point of deterministic simulation testing: every one of these runs
executes drops, duplicates, reorders, partitions and crashes, with the full
invariant suite asserted after every single simulator step. Any violation
raises with its seed for exact replay.
"""

import pytest

from harmonia.cluster import Cluster

SWEEP_STEPS = 4000


def sweep(seeds, faults="chaos", steps=SWEEP_STEPS, nodes=5):
    stats_total = {"elections": 0, "partitions": 0, "crashes": 0, "dropped": 0,
                   "duplicated": 0, "commands_submitted": 0}
    checks = 0
    for seed in seeds:
        result = Cluster(num_nodes=nodes, seed=seed, faults=faults).run(steps)
        checks += result.stats["invariant_checks"]
        for k in stats_total:
            stats_total[k] += result.stats[k]
    return stats_total, checks


class TestChaosSweep:
    def test_invariants_hold_across_100_chaos_seeds(self):
        totals, checks = sweep(range(100))
        # completing without InvariantViolation IS the assertion; also prove the
        # sweep actually exercised the machinery rather than idling:
        assert checks == 100 * SWEEP_STEPS
        assert totals["elections"] > 100
        assert totals["partitions"] > 50
        assert totals["crashes"] > 50
        assert totals["dropped"] > 1000
        assert totals["duplicated"] > 500
        assert totals["commands_submitted"] > 500

    def test_invariants_hold_light_profile_30_seeds(self):
        _, checks = sweep(range(30), faults="light")
        assert checks == 30 * SWEEP_STEPS

    def test_invariants_hold_three_node_chaos(self):
        sweep(range(20), nodes=3)

    def test_invariants_hold_seven_node_chaos(self):
        sweep(range(10), nodes=7)


@pytest.mark.slow
class TestLongSweep:
    def test_invariants_hold_across_300_chaos_seeds_long(self):
        totals, checks = sweep(range(300), steps=5000)
        assert checks == 300 * 5000
        assert totals["elections"] > 300
