"""Discrete-event simulator: virtual clock, priority event queue, seeded RNG, faulty network.

Everything that happens in a RaftVerified run flows through this module. The simulator owns
a single seeded random.Random instance; every source of nondeterminism (message delays,
drops, duplicates, election timeouts, fault injection) draws from that one stream in a
deterministic order, so the same seed always reproduces the exact same run.

Time is integer virtual milliseconds. One simulator *step* pops and executes exactly one
event from the queue. Events scheduled for the same instant execute in the order they
were scheduled (a monotonically increasing sequence number breaks ties).
"""

from __future__ import annotations

import heapq
import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class Simulator:
    """A minimal discrete-event simulator with a virtual clock and seeded RNG."""

    def __init__(self, seed: int) -> None:
        self.seed = seed
        self.rng = random.Random(seed)
        self.now: int = 0
        self.steps: int = 0
        self._seq: int = 0
        self._heap: list[tuple[int, int, Callable[[], None]]] = []

    def schedule(self, delay: int, fn: Callable[[], None]) -> None:
        """Schedule fn() to run `delay` virtual milliseconds from now."""
        if delay < 0:
            raise ValueError(f"negative delay: {delay}")
        heapq.heappush(self._heap, (self.now + int(delay), self._seq, fn))
        self._seq += 1

    def step(self) -> bool:
        """Pop and execute the next event. Returns False if the queue is empty."""
        if not self._heap:
            return False
        t, _, fn = heapq.heappop(self._heap)
        self.now = t
        self.steps += 1
        fn()
        return True

    @property
    def pending(self) -> int:
        return len(self._heap)


@dataclass(frozen=True)
class FaultProfile:
    """Knobs for how hostile the simulated network and environment are."""

    name: str
    drop_p: float        # probability an individual message is lost
    dup_p: float         # probability an individual message is delivered twice
    min_delay: int       # per-message delay lower bound (ms)
    max_delay: int       # per-message delay upper bound (ms); jitter => reordering
    partitions: bool     # fault driver may partition the network
    crashes: bool        # fault driver may pause nodes (state retained; see README)


PROFILES: dict[str, FaultProfile] = {
    "none": FaultProfile("none", drop_p=0.0, dup_p=0.0, min_delay=1, max_delay=5,
                         partitions=False, crashes=False),
    "light": FaultProfile("light", drop_p=0.05, dup_p=0.02, min_delay=1, max_delay=20,
                          partitions=False, crashes=False),
    "chaos": FaultProfile("chaos", drop_p=0.10, dup_p=0.05, min_delay=1, max_delay=50,
                          partitions=True, crashes=True),
}


class Network:
    """A simulated message network that can drop, duplicate, delay and reorder messages,
    and partition the cluster into groups that cannot reach each other.

    Reordering falls out of randomized per-message delays. Partitions and crashes are
    checked at *delivery* time, so a message in flight when a partition forms (or when
    its destination pauses) is lost, just like on a real network.
    """

    def __init__(
        self,
        sim: Simulator,
        node_ids: list[int],
        profile: FaultProfile,
        deliver: Callable[[int, int, Any], None],
        record: Callable[[str, str], None],
    ) -> None:
        self.sim = sim
        self.node_ids = sorted(node_ids)
        self.profile = profile
        self._deliver = deliver
        self._record = record
        self._group: dict[int, int] = dict.fromkeys(self.node_ids, 0)
        self.crashed: set[int] = set()
        # Link-level faults (the nemesis vocabulary): a down link silently eats traffic
        # between one pair of nodes; a lossy link drops each message with a probability.
        # Both are empty by default and, when empty, cost ZERO extra rng draws -- so a
        # run without link faults is byte-identical to one on the pre-nemesis network.
        self.down_links: set[frozenset[int]] = set()
        self.lossy_links: dict[frozenset[int], float] = {}
        self.sent = 0
        self.delivered = 0
        self.dropped = 0
        self.duplicated = 0

    # -- partitions ---------------------------------------------------------

    def set_partition(self, groups: list[set[int]]) -> None:
        """Split the cluster into the given groups. Nodes in different groups
        cannot exchange messages until heal() is called."""
        seen: set[int] = set()
        for gi, group in enumerate(groups):
            for node in group:
                self._group[node] = gi
                seen.add(node)
        if seen != set(self.node_ids):
            raise ValueError("partition groups must cover all nodes exactly once")

    def heal(self) -> None:
        for node in self.node_ids:
            self._group[node] = 0

    def is_partitioned(self) -> bool:
        return len({self._group[n] for n in self.node_ids}) > 1

    def reachable(self, a: int, b: int) -> bool:
        return self._group[a] == self._group[b] and frozenset((a, b)) not in self.down_links

    # -- link faults (undirected; see raftverified/nemesis.py) --------------------

    def set_link_down(self, a: int, b: int) -> None:
        self.down_links.add(frozenset((a, b)))

    def set_link_up(self, a: int, b: int) -> None:
        self.down_links.discard(frozenset((a, b)))

    def link_is_down(self, a: int, b: int) -> bool:
        return frozenset((a, b)) in self.down_links

    def set_link_lossy(self, a: int, b: int, drop_p: float) -> None:
        self.lossy_links[frozenset((a, b))] = drop_p

    def clear_link_lossy(self, a: int, b: int) -> None:
        self.lossy_links.pop(frozenset((a, b)), None)

    def link_is_lossy(self, a: int, b: int) -> bool:
        return frozenset((a, b)) in self.lossy_links

    # -- sending ------------------------------------------------------------

    def send(self, src: int, dst: int, msg: Any) -> None:
        """Send msg from src to dst, subject to the fault profile."""
        self.sent += 1
        self._record("send", f"n{src}->n{dst}|{msg!r}")
        if src in self.crashed:
            self.dropped += 1
            self._record("drop", f"n{src}->n{dst}|sender-crashed")
            return
        if self.lossy_links:  # nemesis lossy link: decided at send time, like drop_p
            drop_p = self.lossy_links.get(frozenset((src, dst)))
            if drop_p is not None and self.sim.rng.random() < drop_p:
                self.dropped += 1
                self._record("drop", f"n{src}->n{dst}|lossy-link")
                return
        if self.profile.drop_p > 0 and self.sim.rng.random() < self.profile.drop_p:
            self.dropped += 1
            self._record("drop", f"n{src}->n{dst}|lost")
            return
        delay = self.sim.rng.randint(self.profile.min_delay, self.profile.max_delay)
        self.sim.schedule(delay, lambda: self._delivery(src, dst, msg))
        if self.profile.dup_p > 0 and self.sim.rng.random() < self.profile.dup_p:
            dup_delay = self.sim.rng.randint(self.profile.min_delay, self.profile.max_delay)
            self.duplicated += 1
            self._record("dup", f"n{src}->n{dst}|{msg!r}")
            self.sim.schedule(dup_delay, lambda: self._delivery(src, dst, msg))

    def _delivery(self, src: int, dst: int, msg: Any) -> None:
        if dst in self.crashed:
            self.dropped += 1
            self._record("drop", f"n{src}->n{dst}|dst-crashed")
            return
        if frozenset((src, dst)) in self.down_links:
            self.dropped += 1
            self._record("drop", f"n{src}->n{dst}|link-down")
            return
        if not self.reachable(src, dst):
            self.dropped += 1
            self._record("drop", f"n{src}->n{dst}|partitioned")
            return
        self.delivered += 1
        self._record("deliver", f"n{src}->n{dst}|{msg!r}")
        self._deliver(src, dst, msg)
