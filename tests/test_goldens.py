"""The determinism tripwire tests (see tests/_goldens.py for the corpus + rationale).

If a change to Harmonia moves ANY pinned digest or rng-draw count, one of these fails.
That is the point: a determinism-neutral change must leave every golden untouched, and
an intentional behaviour change must be accompanied by a reviewed regeneration of
tests/goldens.json (``.venv/Scripts/python.exe tests/_goldens.py``).
"""

from __future__ import annotations

import pytest
from _goldens import (
    CONFIGS,
    CountingRandom,
    digest_for,
    key,
    load,
    rng_calls_for,
)

GOLDENS = load()


@pytest.mark.parametrize("cfg", CONFIGS, ids=key)
def test_golden_digests_pinned(cfg: tuple[int, int, str, int]) -> None:
    """Every corpus config reproduces its frozen trace digest exactly."""
    assert digest_for(cfg) == GOLDENS[key(cfg)]["digest"]


@pytest.mark.parametrize("cfg", CONFIGS, ids=key)
def test_rng_stream_length_stable(cfg: tuple[int, int, str, int]) -> None:
    """Every corpus config draws exactly the pinned number of times from sim.rng.

    Guards the APPEND-NEVER-INSERT rule: inserting a draw mid-stream changes this
    count even when a later behaviour change might otherwise mask it in the digest.
    """
    assert rng_calls_for(cfg) == GOLDENS[key(cfg)]["rng_calls"]


def test_goldens_file_matches_corpus() -> None:
    """The committed goldens.json has exactly one entry per config (no stale keys)."""
    assert set(GOLDENS) == {key(cfg) for cfg in CONFIGS}


def test_corpus_covers_all_sizes_and_profiles() -> None:
    """Sanity: the tripwire spans cluster sizes 1/3/5/7 and none/light/chaos."""
    sizes = {cfg[0] for cfg in CONFIGS}
    profiles = {cfg[2] for cfg in CONFIGS}
    assert sizes == {1, 3, 5, 7}
    assert profiles == {"none", "light", "chaos"}


def test_counting_instrument_does_not_perturb_the_stream() -> None:
    """The draw-counting RNG must produce byte-identical digests (a pure instrument).

    Otherwise rng_calls_for would be measuring a different run than digest_for.
    """
    from unittest import mock

    from harmonia.cluster import Cluster

    for cfg in [(5, 7, "chaos", 3000), (3, 2, "light", 2000)]:
        nodes, seed, faults, steps = cfg
        plain = Cluster(num_nodes=nodes, seed=seed, faults=faults).run(steps).digest
        with mock.patch("harmonia.sim.random.Random", CountingRandom):
            counted = Cluster(num_nodes=nodes, seed=seed, faults=faults).run(steps).digest
        assert counted == plain


def test_rng_call_count_is_itself_deterministic() -> None:
    """Counting the same config twice yields the same total (no hidden nondeterminism)."""
    cfg = (5, 42, "chaos", 3000)
    assert rng_calls_for(cfg) == rng_calls_for(cfg)
