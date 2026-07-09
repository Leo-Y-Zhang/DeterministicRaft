"""Cluster harness: wires nodes + simulated network + fault driver + invariant checker.

A Cluster owns one Simulator (and therefore one seeded RNG stream). Running a
cluster produces a RunResult with a deterministic event trace: the same
(nodes, seed, faults, steps) always yields byte-identical traces and digests.

The invariant checker runs after EVERY simulator step; a safety violation raises
InvariantViolation with the seed and step baked in for exact replay.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .invariants import InvariantChecker
from .kv import CAS, GET, PUT, Command, HistoryEntry
from .node import DEFAULT_CONFIG, LEADER, Message, RaftConfig, RaftNode
from .sim import PROFILES, FaultProfile, Network, Simulator

CLIENT_INTERVAL = 100  # ms between client command attempts
FAULT_INTERVAL = 100   # ms between fault-driver decisions

# Deterministic client workload: a few clients hammering a small keyspace so operations
# contend on the same keys (which is what makes linearizability interesting to check).
# Purely counter-driven -> draws NO randomness, so recording it leaves the rng stream
# length unchanged; only the command text (and therefore the trace digest) moves.
WORKLOAD_CLIENTS = 3
WORKLOAD_KEYS = ("k0", "k1", "k2")
WORKLOAD_OPS = (PUT, PUT, GET, CAS)


def workload_command(seq: int) -> Command:
    """The seq-th client command. A pure function of ``seq`` (no randomness)."""
    client_id = seq % WORKLOAD_CLIENTS
    req_id = seq // WORKLOAD_CLIENTS
    key = WORKLOAD_KEYS[seq % len(WORKLOAD_KEYS)]
    op = WORKLOAD_OPS[seq % len(WORKLOAD_OPS)]
    if op == GET:
        return Command(client_id, req_id, GET, key)
    if op == CAS:
        return Command(client_id, req_id, CAS, key, f"v{seq}", f"v{seq - 1}" if seq else "")
    return Command(client_id, req_id, PUT, key, f"v{seq}")


@dataclass
class RunResult:
    seed: int
    faults: str
    num_nodes: int
    steps: int
    virtual_time: int
    trace: list[str]
    events: list[tuple[int, str, str]]
    stats: dict[str, int]
    final: list[dict[str, Any]]

    @property
    def digest(self) -> str:
        return hashlib.sha256("\n".join(self.trace).encode()).hexdigest()


class Cluster:
    def __init__(
        self,
        num_nodes: int = 5,
        seed: int = 0,
        faults: str = "none",
        config: RaftConfig = DEFAULT_CONFIG,
        client_interval: int | None = CLIENT_INTERVAL,
        record_history: bool = True,
    ) -> None:
        if num_nodes < 1:
            raise ValueError("need at least one node")
        if faults not in PROFILES:
            raise ValueError(f"unknown fault profile: {faults!r}")
        self.seed = seed
        self.faults = faults
        self.profile: FaultProfile = PROFILES[faults]
        self.sim = Simulator(seed)
        self.trace: list[str] = []
        self.events: list[tuple[int, str, str]] = []
        # Client-observed operation history (invoke/return + result), for the
        # linearizability oracle. Purely passive: recording it draws no randomness and
        # emits nothing to the trace, so it never perturbs a run's digest.
        self._record_history = record_history
        self.history: list[HistoryEntry] = []
        self._pending: dict[str, HistoryEntry] = {}   # encoded command -> its history row
        ids = list(range(num_nodes))
        self.net = Network(self.sim, ids, self.profile, self._deliver, self.record)
        self.nodes: dict[int, RaftNode] = {
            i: RaftNode(i, [p for p in ids if p != i], self.sim,
                        self.net.send, self.record, config, on_apply=self._on_apply)
            for i in ids
        }
        self.checker = InvariantChecker(
            seed,
            replay_hint=(f"harmonia replay --nodes {num_nodes} --seed {seed} "
                         f"--faults {faults}"),
        )
        self.stats: dict[str, int] = {
            "elections": 0, "leaders_elected": 0, "partitions": 0, "heals": 0,
            "crashes": 0, "resumes": 0, "commands_submitted": 0,
        }
        self._crash_limit = (num_nodes - 1) // 2  # keep a majority alive under chaos
        for i in ids:
            self.nodes[i].start()
        if client_interval:
            self._client_interval = client_interval
            self._next_command = 0
            self.sim.schedule(client_interval, self._client_tick)
        if self.profile.partitions or self.profile.crashes:
            self.sim.schedule(FAULT_INTERVAL, self._fault_tick)

    # -- recording ------------------------------------------------------------

    def record(self, kind: str, detail: str) -> None:
        self.trace.append(f"{self.sim.now}|{kind}|{detail}")
        if kind not in ("send", "deliver", "drop", "dup"):
            self.events.append((self.sim.now, kind, detail))
        if kind == "election":
            self.stats["elections"] += 1
        elif kind == "role" and "|leader|" in detail:
            self.stats["leaders_elected"] += 1

    def _deliver(self, src: int, dst: int, msg: Message) -> None:
        self.nodes[dst].handle(src, msg)

    # -- drivers --------------------------------------------------------------

    def _client_tick(self) -> None:
        """A client submits a command to a randomly chosen node; only a node that
        currently believes it is leader accepts. Occasionally that is a stale
        leader, which is exactly the kind of divergence Raft must repair.

        The single rng draw (which node to try) is unchanged from before; the command
        itself is derived deterministically from a counter, so no draw is inserted."""
        target = self.sim.rng.choice(sorted(self.nodes))
        node = self.nodes[target]
        if node.alive and node.role == LEADER:
            command = workload_command(self._next_command)
            self._next_command += 1
            self.stats["commands_submitted"] += 1
            encoded = command.encode()
            self.record("client", f"n{target}|{encoded}")
            self._invoke(command, encoded)
            node.client_command(encoded)
        self.sim.schedule(self._client_interval, self._client_tick)

    # -- client-observed history (passive: no randomness, no trace output) ------

    def _invoke(self, command: Command, encoded: str) -> None:
        """Record that a client submitted ``command`` at the current step."""
        if not self._record_history:
            return
        row = HistoryEntry(
            client_id=command.client_id, req_id=command.req_id, op=command.op,
            key=command.key, value=command.value, expected=command.expected,
            invoke_step=self.sim.steps,
        )
        self.history.append(row)
        self._pending[encoded] = row

    def _on_apply(self, encoded: str, result: str) -> None:
        """Complete the pending client op when the state machine first applies it.

        Fires once per node per applied entry; the first application (by the leader,
        which commits first) fixes the client-visible return step and result."""
        if not self._record_history:
            return
        row = self._pending.pop(encoded, None)
        if row is not None:
            row.return_step = self.sim.steps
            row.observed = result

    def _fault_tick(self) -> None:
        rng = self.sim.rng
        if self.profile.partitions:
            if self.net.is_partitioned():
                if rng.random() < 0.25:
                    self.heal_partition()
            elif rng.random() < 0.15:
                sides = {i: rng.randint(0, 1) for i in sorted(self.nodes)}
                groups = [{i for i, s in sides.items() if s == 0},
                          {i for i, s in sides.items() if s == 1}]
                if groups[0] and groups[1]:
                    self.set_partition(groups)
        if self.profile.crashes and rng.random() < 0.10:
            candidates = (
                [i for i in sorted(self.nodes) if self.nodes[i].alive]
                if len(self.net.crashed) < self._crash_limit
                else []
            )
            if candidates:
                victim = rng.choice(candidates)
                self.pause(victim)
                self.sim.schedule(rng.randint(100, 400), lambda: self._maybe_resume(victim))
        self.sim.schedule(FAULT_INTERVAL, self._fault_tick)

    def _maybe_resume(self, node_id: int) -> None:
        if node_id in self.net.crashed:
            self.resume(node_id)

    # -- fault controls (also used directly by tests) --------------------------

    def set_partition(self, groups: list[set[int]]) -> None:
        self.net.set_partition(groups)
        label = "/".join(",".join(f"n{i}" for i in sorted(g)) for g in groups)
        self.stats["partitions"] += 1
        self.record("partition", label)

    def heal_partition(self) -> None:
        self.net.heal()
        self.stats["heals"] += 1
        self.record("heal", "all")

    def pause(self, node_id: int) -> None:
        self.net.crashed.add(node_id)
        self.nodes[node_id].pause()
        self.stats["crashes"] += 1
        self.record("crash", f"n{node_id}")

    def resume(self, node_id: int) -> None:
        self.net.crashed.discard(node_id)
        self.nodes[node_id].resume()
        self.stats["resumes"] += 1
        self.record("resume", f"n{node_id}")

    # -- running ---------------------------------------------------------------

    def step(self) -> bool:
        """One simulator step followed by a full invariant check."""
        if not self.sim.step():
            return False
        self.checker.check(self.nodes, self.sim.steps)
        return True

    def run(self, steps: int) -> RunResult:
        for _ in range(steps):
            if not self.step():
                break
        return self.result()

    def run_until(self, pred: Callable[[Cluster], bool], max_steps: int = 50_000) -> bool:
        """Step until pred(cluster) is true. Returns False if the budget runs out."""
        for _ in range(max_steps):
            if pred(self):
                return True
            if not self.step():
                return False
        return pred(self)

    # -- inspection --------------------------------------------------------------

    def leaders(self) -> list[RaftNode]:
        return [n for i, n in sorted(self.nodes.items()) if n.role == LEADER and n.alive]

    def leader(self) -> RaftNode | None:
        """The alive leader with the highest term (there is at most one per term)."""
        ls = self.leaders()
        return max(ls, key=lambda n: n.term) if ls else None

    def result(self) -> RunResult:
        stats = dict(self.stats)
        stats.update(sent=self.net.sent, delivered=self.net.delivered,
                     dropped=self.net.dropped, duplicated=self.net.duplicated,
                     invariant_checks=self.checker.checks_run)
        final = []
        for i in sorted(self.nodes):
            n = self.nodes[i]
            log_digest = hashlib.sha256(repr(n.log).encode()).hexdigest()[:12]
            final.append({
                "id": i, "role": n.role, "term": n.term, "alive": n.alive,
                "commit_index": n.commit_index, "applied": len(n.applied),
                "log_length": n.last_log_index(), "log_sha256": log_digest,
            })
        return RunResult(
            seed=self.seed, faults=self.faults, num_nodes=len(self.nodes),
            steps=self.sim.steps, virtual_time=self.sim.now,
            trace=self.trace, events=self.events, stats=stats, final=final,
        )
