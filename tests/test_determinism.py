"""Determinism: the same seed must reproduce the exact same run, byte for byte."""

from harmonia.cluster import Cluster


def run(seed=99, faults="chaos", steps=3000, nodes=5):
    return Cluster(num_nodes=nodes, seed=seed, faults=faults).run(steps)


class TestDeterminism:
    def test_same_seed_identical_trace(self):
        a, b = run(), run()
        assert a.trace == b.trace

    def test_same_seed_byte_identical_trace(self):
        a, b = run(), run()
        assert "\n".join(a.trace).encode() == "\n".join(b.trace).encode()

    def test_same_seed_identical_digest(self):
        a, b = run(), run()
        assert a.digest == b.digest and len(a.digest) == 64

    def test_same_seed_identical_final_state(self):
        a, b = run(), run()
        assert a.final == b.final and a.stats == b.stats

    def test_different_seed_different_trace(self):
        assert run(seed=1).digest != run(seed=2).digest

    def test_different_faults_different_trace(self):
        assert run(faults="none").digest != run(faults="chaos").digest

    def test_determinism_across_fault_profiles(self):
        for faults in ("none", "light", "chaos"):
            assert run(seed=7, faults=faults).digest == run(seed=7, faults=faults).digest

    def test_trace_is_prefix_stable(self):
        # running longer must extend, never rewrite, the shorter run's trace
        short, long = run(steps=1000), run(steps=2000)
        assert long.trace[: len(short.trace)] == short.trace
