"""Golden-digest corpus: the determinism tripwire that guards Harmonia's core thesis.

Harmonia's one guarantee is byte-identical replay from a seed. This module pins a
fixed matrix of ``(nodes, seed, faults, steps)`` configs to two frozen values each:

  * ``digest``    -- the sha256 of the full recorded trace (the observable behaviour)
  * ``rng_calls`` -- how many times the run drew from the single ``sim.rng`` stream

The digest catches ANY behaviour change; the rng-call count catches the specific,
insidious failure mode of *inserting a new random draw mid-stream* (the thing that
silently desynchronises every later run) even in the unlikely event the digest were
to collide. Together they turn "don't break determinism" from a review vibe into a
red test the instant a draw moves.

This file is deliberately named with a leading underscore so pytest does not collect
it as a test module; ``tests/test_goldens.py`` imports from it. Regenerate the pinned
values after an *intentional* behaviour change with::

    .venv/Scripts/python.exe tests/_goldens.py

and review the resulting ``tests/goldens.json`` diff -- only the configs you meant to
change should move.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any
from unittest import mock

from harmonia.cluster import Cluster

# (nodes, seed, faults, steps) -- spans cluster sizes 1/3/5/7 and all three fault
# profiles, with enough steps that elections, commits, partitions and crashes all fire.
CONFIGS: list[tuple[int, int, str, int]] = [
    (1, 0, "none", 500),
    (3, 1, "none", 2000),
    (3, 2, "light", 2000),
    (3, 7, "chaos", 3000),
    (5, 0, "none", 2000),
    (5, 3, "light", 2500),
    (5, 7, "chaos", 3000),
    (5, 42, "chaos", 3000),
    (5, 99, "light", 2500),
    (7, 5, "none", 2000),
    (7, 11, "chaos", 3000),
    (7, 13, "chaos", 2500),
]

GOLDENS_PATH = Path(__file__).with_name("goldens.json")


class CountingRandom(random.Random):
    """A drop-in ``random.Random`` that counts public draws without changing outputs.

    Subtlety: CPython's ``Random.__init_subclass__`` inspects the subclass. If it sees
    ``random()`` overridden but not ``getrandbits()``, it switches ``_randbelow`` to the
    ``random()``-based path, which changes what ``randint``/``choice`` return. So we also
    define ``getrandbits`` (a pure pass-through, deliberately NOT counted since
    ``randint``/``choice`` route through it internally) to keep the base
    ``_randbelow_with_getrandbits`` path -- making the Mersenne-Twister stream, and
    therefore every digest, byte-identical to a plain ``random.Random``. We count only
    the three public entry points Harmonia actually draws from.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls = 0

    def random(self) -> float:
        self.calls += 1
        return super().random()

    def randint(self, a: int, b: int) -> int:
        self.calls += 1
        return super().randint(a, b)

    def choice(self, seq: Any) -> Any:
        self.calls += 1
        return super().choice(seq)

    def getrandbits(self, k: int) -> int:
        # Pass-through only (uncounted): its presence keeps CPython on the base
        # _randbelow_with_getrandbits path so outputs match plain random.Random.
        return super().getrandbits(k)


def key(cfg: tuple[int, int, str, int]) -> str:
    nodes, seed, faults, steps = cfg
    return f"{nodes}-{seed}-{faults}-{steps}"


def digest_for(cfg: tuple[int, int, str, int]) -> str:
    nodes, seed, faults, steps = cfg
    return Cluster(num_nodes=nodes, seed=seed, faults=faults).run(steps).digest


def rng_calls_for(cfg: tuple[int, int, str, int]) -> int:
    """Run the config with the draw-counting RNG in place and return the total draws."""
    nodes, seed, faults, steps = cfg
    with mock.patch("harmonia.sim.random.Random", CountingRandom):
        cluster = Cluster(num_nodes=nodes, seed=seed, faults=faults)
        cluster.run(steps)
        rng = cluster.sim.rng
        assert isinstance(rng, CountingRandom)
        return rng.calls


def compute_all() -> dict[str, dict[str, Any]]:
    return {
        key(cfg): {"digest": digest_for(cfg), "rng_calls": rng_calls_for(cfg)}
        for cfg in CONFIGS
    }


def load() -> dict[str, dict[str, Any]]:
    with GOLDENS_PATH.open(encoding="utf-8") as f:
        data: dict[str, dict[str, Any]] = json.load(f)
    return data


def save(data: dict[str, dict[str, Any]]) -> None:
    with GOLDENS_PATH.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


if __name__ == "__main__":
    save(compute_all())
    print(f"wrote {len(CONFIGS)} golden entries to {GOLDENS_PATH}")
