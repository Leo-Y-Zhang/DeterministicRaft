"""Nemesis vocabulary: declarative, replayable fault schedules (hand-authored chaos).

The random fault driver EXPLORES the fault space; a nemesis DIRECTS it. A
``NemesisSchedule`` is a plain-data list of named fault patterns -- split the cluster
into halves, isolate whoever currently believes it leads, flap or degrade one link,
crash one node -- each pinned to a virtual-time instant. The vocabulary is deliberately
Jepsen-shaped (their nemesis process is the inspiration; no affiliation) but keeps
Harmonia's one non-negotiable: everything is deterministic from the seed.

Three properties make schedules first-class citizens of the existing machinery rather
than a parallel mechanism:

* **Pure data.** Patterns are frozen dataclasses that validate on construction, draw
  ZERO randomness of their own, and serialize to/from a compact JSON form
  (``to_json``/``from_json``) that round-trips exactly -- so a failing schedule can be
  pasted into a replay command (CLI ``--nemesis``) and reproduced byte-for-byte.
* **Same suppression mask.** Every injection a schedule fires consumes one ordinal from
  the SAME ``_fire_fault`` counter the random driver uses (see cluster.py), so the ddmin
  shrinker minimises hand-authored schedules with no new code paths.
* **Composable.** Patterns overlay: several may be active at once, a schedule may run on
  top of any fault profile (``none`` for pure hand-authored runs, ``chaos`` to layer
  structure over noise), and same-instant injections fire in declaration order.

Times are virtual milliseconds, matching the simulator clock.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, fields

# One FlappingLink expands to one Injection PER CYCLE up-front, so cycles is the only
# field whose size multiplies memory; bound it (10k flaps outlasts any realistic run).
MAX_FLAP_CYCLES = 10_000


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _check_int(value: object, name: str) -> None:
    """Fields come straight from user JSON, where 1.5 and true are valid values; anything
    that is not a genuine int (bool is an int subclass -- excluded) is rejected here so a
    fractional node id fails validation instead of exploding as a KeyError mid-run."""
    _require(isinstance(value, int) and not isinstance(value, bool),
             f"{name} must be an integer, got {value!r}")


def _check_window(at: int, duration: int) -> None:
    _check_int(at, "at")
    _check_int(duration, "duration")
    _require(at >= 0, f"at must be >= 0 ms, got {at}")
    _require(duration >= 1, f"duration must be >= 1 ms, got {duration}")


def _check_link(a: int, b: int) -> None:
    _check_int(a, "a")
    _check_int(b, "b")
    _require(a >= 0 and b >= 0, f"link node ids must be >= 0, got ({a}, {b})")
    _require(a != b, f"link endpoints must be distinct, got ({a}, {b})")


@dataclass(frozen=True)
class PartitionHalves:
    """At ``at``, split the cluster into two halves -- low node ids (rounded up) versus
    high -- and heal ``duration`` ms later. The classic majority/minority split."""

    at: int
    duration: int

    def __post_init__(self) -> None:
        _check_window(self.at, self.duration)


@dataclass(frozen=True)
class IsolateLeader:
    """At ``at``, cut the node that currently believes it leads (the alive leader with
    the highest term) off from everyone else; heal ``duration`` ms later. If no node
    believes it leads at that instant, the injection is skipped (and consumes no
    suppression ordinal). Resolution happens at fire time, so the target is
    state-dependent but fully determined by the seed."""

    at: int
    duration: int

    def __post_init__(self) -> None:
        _check_window(self.at, self.duration)


@dataclass(frozen=True)
class FlappingLink:
    """Starting at ``at``, the (undirected) link between ``a`` and ``b`` goes down for
    ``period`` ms, comes back for ``period`` ms, and repeats ``cycles`` times. Each
    down-flap is its OWN suppressible injection, so the shrinker can discover that only
    one flap of many is load-bearing. Because each cycle materialises one injection,
    ``cycles`` is bounded by ``MAX_FLAP_CYCLES`` (a runaway value would otherwise
    allocate millions of injections before the first simulator step)."""

    a: int
    b: int
    at: int
    period: int
    cycles: int

    def __post_init__(self) -> None:
        _check_link(self.a, self.b)
        _check_int(self.at, "at")
        _check_int(self.period, "period")
        _check_int(self.cycles, "cycles")
        _require(self.at >= 0, f"at must be >= 0 ms, got {self.at}")
        _require(self.period >= 1, f"period must be >= 1 ms, got {self.period}")
        _require(1 <= self.cycles <= MAX_FLAP_CYCLES,
                 f"cycles must be in 1..{MAX_FLAP_CYCLES}, got {self.cycles}")


@dataclass(frozen=True)
class LossyLink:
    """From ``at`` for ``duration`` ms, messages sent between ``a`` and ``b`` (either
    direction) are each dropped with probability ``drop_p`` -- a degraded, not severed,
    link. The drop decisions draw from the run's single RNG stream at send time, so they
    are seed-deterministic; ``drop_p=1.0`` is a total (but still window-bounded) loss."""

    a: int
    b: int
    at: int
    duration: int
    drop_p: float

    def __post_init__(self) -> None:
        _check_link(self.a, self.b)
        _check_window(self.at, self.duration)
        _require(isinstance(self.drop_p, (int, float)) and not isinstance(self.drop_p, bool),
                 f"drop_p must be a number, got {self.drop_p!r}")
        _require(0.0 < self.drop_p <= 1.0,
                 f"drop_p must be in (0.0, 1.0], got {self.drop_p}")


@dataclass(frozen=True)
class CrashNode:
    """At ``at``, crash node ``node`` (volatile state is lost, as always) and restart it
    ``duration`` ms later. Crashing a node that is already down is skipped (no ordinal).
    Unlike the random driver, a hand-authored schedule MAY crash a majority."""

    node: int
    at: int
    duration: int

    def __post_init__(self) -> None:
        _check_int(self.node, "node")
        _require(self.node >= 0, f"node must be >= 0, got {self.node}")
        _check_window(self.at, self.duration)


NemesisOp = PartitionHalves | IsolateLeader | FlappingLink | LossyLink | CrashNode

_NAMES: dict[type[NemesisOp], str] = {
    PartitionHalves: "partition_halves",
    IsolateLeader: "isolate_leader",
    FlappingLink: "flapping_link",
    LossyLink: "lossy_link",
    CrashNode: "crash_node",
}
_PATTERNS: dict[str, Callable[..., NemesisOp]] = {
    name: cls for cls, name in _NAMES.items()
}


@dataclass(frozen=True)
class Injection:
    """One suppressible fault injection: pattern ``op`` firing at virtual time ``at``.
    A FlappingLink expands to one injection per down-flap; every other pattern is a
    single injection whose paired recovery (heal / link-up / restore / resume) fires
    only if the injection itself was not suppressed."""

    at: int
    op: NemesisOp


@dataclass(frozen=True)
class NemesisSchedule:
    """An immutable, order-preserving composition of nemesis patterns."""

    ops: tuple[NemesisOp, ...] = ()

    def injections(self) -> list[Injection]:
        """Expand to individual injections, sorted by fire time; same-instant ties keep
        declaration order (the sort is stable), which the simulator preserves."""
        out: list[Injection] = []
        for op in self.ops:
            if isinstance(op, FlappingLink):
                out.extend(Injection(op.at + 2 * op.period * i, op)
                           for i in range(op.cycles))
            else:
                out.append(Injection(op.at, op))
        return sorted(out, key=lambda inj: inj.at)

    def max_node(self) -> int:
        """The highest node id the schedule names explicitly (-1 if none), so a Cluster
        can reject a schedule that outreaches it."""
        top = -1
        for op in self.ops:
            if isinstance(op, (FlappingLink, LossyLink)):
                top = max(top, op.a, op.b)
            elif isinstance(op, CrashNode):
                top = max(top, op.node)
        return top

    # -- serialization (the replayable form) ----------------------------------

    def to_json(self) -> str:
        """A compact JSON array, one object per pattern; ``from_json`` inverts it
        exactly. This string IS the schedule's replay token (CLI ``--nemesis``)."""
        return json.dumps([self._encode(op) for op in self.ops], separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> NemesisSchedule:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"nemesis schedule is not valid JSON: {exc}") from exc
        if not isinstance(data, list):
            raise ValueError("nemesis schedule must be a JSON list of patterns")
        return cls(tuple(cls._decode(item) for item in data))

    @staticmethod
    def _encode(op: NemesisOp) -> dict[str, object]:
        encoded: dict[str, object] = {"pattern": _NAMES[type(op)]}
        for field in fields(op):
            encoded[field.name] = getattr(op, field.name)
        return encoded

    @staticmethod
    def _decode(item: object) -> NemesisOp:
        if not isinstance(item, dict):
            raise ValueError(f"each nemesis pattern must be a JSON object, got {item!r}")
        params = dict(item)
        name = params.pop("pattern", None)
        if not isinstance(name, str) or name not in _PATTERNS:
            raise ValueError(
                f"unknown pattern {name!r}; expected one of {sorted(_PATTERNS)}")
        try:
            return _PATTERNS[name](**params)
        except TypeError as exc:  # missing or unexpected fields
            raise ValueError(f"bad fields for pattern {name!r}: {exc}") from exc
