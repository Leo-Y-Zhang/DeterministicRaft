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

from .bugs import NO_BUGS, Bugs
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


def client_workload(client_id: int, req_id: int) -> Command:
    """The (client_id, req_id)-th client command. A pure function of its inputs (no
    randomness), so a retry of the same request reproduces byte-identical bytes."""
    seq = req_id * WORKLOAD_CLIENTS + client_id
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
        bugs: Bugs = NO_BUGS,
    ) -> None:
        if num_nodes < 1:
            raise ValueError("need at least one node")
        if faults not in PROFILES:
            raise ValueError(f"unknown fault profile: {faults!r}")
        self.seed = seed
        self.faults = faults
        self.bugs = bugs
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
            i: RaftNode(i, [p for p in ids if p != i], self.sim, self.net.send,
                        self.record, config, on_apply=self._on_apply, bugs=bugs)
            for i in ids
        }
        self.checker = InvariantChecker(
            seed,
            replay_hint=(f"harmonia replay --nodes {num_nodes} --seed {seed} "
                         f"--faults {faults}"),
        )
        self.stats: dict[str, int] = {
            "elections": 0, "leaders_elected": 0, "partitions": 0, "heals": 0,
            "crashes": 0, "resumes": 0, "commands_submitted": 0, "retries": 0,
        }
        self._crash_limit = (num_nodes - 1) // 2  # keep a majority alive under chaos
        for i in ids:
            self.nodes[i].start()
        # Client-driver state (always initialised so the apply hook is safe even when no
        # client driver is scheduled); the tick only runs when client_interval is set.
        self._client_interval = client_interval or CLIENT_INTERVAL
        self._next_req = [0] * WORKLOAD_CLIENTS    # per-client next request id
        self._outstanding: dict[int, str] = {}     # client_id -> its unfinished op (encoded)
        self._client_turn = 0                      # round-robin cursor over clients
        if client_interval:
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
        """One round-robin client acts each tick. A client keeps ONE operation
        outstanding at a time; it invokes a fresh op when idle, otherwise RETRIES its
        pending op. Each attempt goes to a randomly chosen node and only lands if that
        node currently believes it is leader -- so under faults the same request reaches
        several leaders and must be deduplicated exactly once.

        The single rng draw (which node to try) keeps its position; client selection and
        command bytes are derived deterministically, so no draw is inserted mid-stream.
        Retries do append more entries/messages, which legitimately lengthens the stream
        (an intentional behaviour change, not a desync)."""
        client_id = self._client_turn % WORKLOAD_CLIENTS
        self._client_turn += 1
        target = self.sim.rng.choice(sorted(self.nodes))
        node = self.nodes[target]

        encoded = self._outstanding.get(client_id)
        if encoded is None:  # idle client invokes a new operation
            command = client_workload(client_id, self._next_req[client_id])
            encoded = command.encode()
            self._outstanding[client_id] = encoded
            self.stats["commands_submitted"] += 1
            self.record("client", f"n{target}|c{client_id}|{encoded}")
            self._invoke(command, encoded)
        else:  # a retry of the client's still-unfinished operation
            self.stats["retries"] += 1
            self.record("retry", f"n{target}|c{client_id}|{encoded}")

        if node.alive and node.role == LEADER:
            cmd = Command.decode(encoded)
            if self.bugs.stale_local_reads and cmd.op == GET:
                # BUG: answer the read straight from local state, with no log entry and
                # no leadership confirmation. A stale/partitioned leader returns old data.
                result = node.kv.store.get(cmd.key, "")
                self.record("staleread", f"n{target}|{encoded}|{result}")
                self._on_apply(encoded, result)
            else:
                node.client_command(encoded)
        self.sim.schedule(self._client_interval, self._client_tick)

    # -- client-observed history + retry bookkeeping ----------------------------

    def _invoke(self, command: Command, encoded: str) -> None:
        """Record (once) that a client invoked ``command`` at the current step."""
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
        """When the state machine first applies an operation, let its client move on and
        fix the client-visible return step + result. Fires once per node per applied
        entry; the first application (the leader, which commits first) wins. Client
        bookkeeping runs regardless of ``record_history`` so recording never alters a
        run's behaviour (the history list is the only thing it gates)."""
        cmd = Command.decode(encoded)
        if not cmd.is_structured:
            return
        if self._outstanding.get(cmd.client_id) == encoded:
            del self._outstanding[cmd.client_id]
            self._next_req[cmd.client_id] += 1
        if self._record_history:
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
