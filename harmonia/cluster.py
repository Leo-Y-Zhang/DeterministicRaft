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
from .nemesis import (
    CrashNode,
    FlappingLink,
    Injection,
    IsolateLeader,
    LossyLink,
    NemesisOp,
    NemesisSchedule,
    PartitionHalves,
)
from .node import DEFAULT_CONFIG, LEADER, Message, RaftConfig, RaftNode
from .sim import PROFILES, FaultProfile, Network, Simulator

CLIENT_INTERVAL = 100      # ms between client command attempts
FAULT_INTERVAL = 100       # ms between fault-driver decisions
MEMBERSHIP_INTERVAL = 100  # ms between membership-change attempts (membership mode); as
#                            fast as the client driver, so a fresh leader sees a change
#                            proposal while its current-term commit may still be pending
#                            (the window the May-2015 guard exists to make safe)

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
        suppressed: frozenset[int] = frozenset(),
        read_index: bool = False,
        membership: bool = False,
        initial_voters: tuple[int, ...] | None = None,
        nemesis: NemesisSchedule | None = None,
    ) -> None:
        if num_nodes < 1:
            raise ValueError("need at least one node")
        if faults not in PROFILES:
            raise ValueError(f"unknown fault profile: {faults!r}")
        if membership and num_nodes < 3:
            raise ValueError("membership mode needs at least three nodes")
        if initial_voters is not None and not set(initial_voters) <= set(range(num_nodes)):
            raise ValueError("initial_voters must name existing nodes")
        if nemesis is not None and nemesis.max_node() >= num_nodes:
            raise ValueError(f"nemesis schedule names node n{nemesis.max_node()}; "
                             f"this cluster only has n0..n{num_nodes - 1}")
        self.seed = seed
        self.faults = faults
        self.bugs = bugs
        self._read_index = read_index  # serve GETs via ReadIndex instead of through the log
        # Membership mode: start with one spare server outside the configuration and run
        # a deterministic churn driver (add it back in, rotate single-server removals).
        # initial_voters alone (no driver) supports hand-driven reconfiguration tests.
        self._membership = membership
        if membership and initial_voters is None:
            initial_voters = tuple(range(num_nodes - 1))
        self._member_turn = 0
        # Each fault injection (partition-form / crash) gets an ordinal; the shrinker can
        # SUPPRESS specific ordinals to test whether a bug still reproduces without them.
        # The rng is drawn identically regardless, so an empty mask is byte-identical.
        self._suppressed = suppressed
        self._fault_seq = 0
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
                        self.record, config, on_apply=self._on_apply, bugs=bugs,
                        initial_voters=initial_voters)
            for i in ids
        }
        self._nemesis = nemesis
        self.checker = InvariantChecker(
            seed,
            replay_hint=(f"harmonia replay --nodes {num_nodes} --seed {seed} "
                         f"--faults {faults}" + (" --membership" if membership else "")
                         + (f" --nemesis '{nemesis.to_json()}'"
                            if nemesis is not None and nemesis.ops else "")),
        )
        self.stats: dict[str, int] = {
            "elections": 0, "leaders_elected": 0, "partitions": 0, "heals": 0,
            "crashes": 0, "resumes": 0, "commands_submitted": 0, "retries": 0,
            "config_changes": 0,
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
        if membership:
            self.sim.schedule(MEMBERSHIP_INTERVAL, self._membership_tick)
        if nemesis is not None:
            # Every injection is scheduled up-front (expansion is sorted; same-instant
            # ties keep declaration order, which the event queue preserves). Each fires
            # through _nemesis_fire -> _fire_fault, so hand-authored faults share the
            # random driver's suppression-mask ordinals and the shrinker just works.
            for injection in nemesis.injections():
                self._schedule_injection(injection)

    # -- recording ------------------------------------------------------------

    def record(self, kind: str, detail: str) -> None:
        self.trace.append(f"{self.sim.now}|{kind}|{detail}")
        if kind not in ("send", "deliver", "drop", "dup"):
            self.events.append((self.sim.now, kind, detail))
        if kind == "election":
            self.stats["elections"] += 1
        elif kind == "role" and "|leader|" in detail:
            self.stats["leaders_elected"] += 1
        elif kind == "cfgchange":
            self.stats["config_changes"] += 1

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
            elif self._read_index and cmd.op == GET:
                node.request_read(encoded)  # confirm leadership, then serve (section 8)
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

    def _fire_fault(self) -> bool:
        """Assign the next fault an ordinal; return False if the shrinker suppressed it.
        Called only after all of a fault's rng draws, so suppression never desyncs the
        stream and an empty mask leaves every digest untouched."""
        idx = self._fault_seq
        self._fault_seq += 1
        return idx not in self._suppressed

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
                if groups[0] and groups[1] and self._fire_fault():
                    self.set_partition(groups)
        if self.profile.crashes and rng.random() < 0.10:
            candidates = (
                [i for i in sorted(self.nodes) if self.nodes[i].alive]
                if len(self.net.crashed) < self._crash_limit
                else []
            )
            if candidates:
                victim = rng.choice(candidates)
                resume_delay = rng.randint(100, 400)
                if self._fire_fault():
                    self.pause(victim)
                    self.sim.schedule(resume_delay, lambda: self._maybe_resume(victim))
        self.sim.schedule(FAULT_INTERVAL, self._fault_tick)

    def _maybe_resume(self, node_id: int) -> None:
        if node_id in self.net.crashed:
            self.resume(node_id)

    def _membership_tick(self) -> None:
        """Deterministic membership churn: add the lowest server missing from the current
        configuration back in, otherwise remove the next server in rotation (never the
        leader -- a leader refuses to remove itself). Purely counter-driven, so it draws
        NO randomness. The proposal is submitted to EVERY node that currently believes it
        is leader (sorted, so iteration is deterministic), each judged against that
        leader's OWN configuration -- a partitioned stale leader keeps pushing its own
        change, which is exactly the concurrency the single-server guards must survive.
        Each leader's guards decide whether its change is accepted now or retried."""
        universe = sorted(self.nodes)
        for leader in self.leaders():
            missing = [i for i in universe if i not in leader.voters]
            if missing:
                proposal = tuple(sorted([*leader.voters, missing[0]]))
            else:
                victim = universe[self._member_turn % len(universe)]
                if victim == leader.id:
                    self._member_turn += 1
                    victim = universe[self._member_turn % len(universe)]
                proposal = tuple(v for v in leader.voters if v != victim)
            if leader.change_config(proposal) and not missing:
                self._member_turn += 1
        self.sim.schedule(MEMBERSHIP_INTERVAL, self._membership_tick)

    # -- nemesis: declarative fault schedules (see harmonia/nemesis.py) --------

    def _schedule_injection(self, injection: Injection) -> None:
        """Schedule one expanded injection (the closure binds this call's injection,
        so a loop over injections cannot late-bind to the last one)."""
        self.sim.schedule(injection.at, lambda: self._nemesis_fire(injection.op))

    def _nemesis_fire(self, op: NemesisOp) -> None:
        """Fire one scheduled injection. State-dependent targets (the believed leader,
        an already-crashed node) are resolved FIRST -- drawing no randomness -- and an
        injection with nothing to do is skipped without consuming an ordinal; otherwise
        the mask decides via _fire_fault, exactly as for the random driver's faults.
        Every recovery (heal / link-up / restore / resume) is scheduled only when its
        injection actually fired, so a suppressed injection leaves no orphan events."""
        if isinstance(op, PartitionHalves):
            ids = sorted(self.nodes)
            half = (len(ids) + 1) // 2
            low, high = set(ids[:half]), set(ids[half:])
            if not high:
                self.record("nemesis", "partition_halves|single-node|skipped")
                return
            if not self._fire_fault():
                return
            self.set_partition([low, high])
            self.sim.schedule(op.duration, self.heal_partition)
        elif isinstance(op, IsolateLeader):
            target = self.leader()
            if target is None or len(self.nodes) < 2:
                self.record("nemesis", "isolate_leader|no-leader|skipped")
                return
            if not self._fire_fault():
                return
            victim = target.id
            self.set_partition([{victim}, {i for i in self.nodes if i != victim}])
            self.sim.schedule(op.duration, self.heal_partition)
        elif isinstance(op, FlappingLink):
            if not self._fire_fault():
                return
            self.link_down(op.a, op.b)
            self.sim.schedule(op.period, lambda: self.link_up(op.a, op.b))
        elif isinstance(op, LossyLink):
            if not self._fire_fault():
                return
            self.set_lossy_link(op.a, op.b, op.drop_p)
            self.sim.schedule(op.duration, lambda: self.clear_lossy_link(op.a, op.b))
        elif isinstance(op, CrashNode):
            if not self.nodes[op.node].alive:
                self.record("nemesis", f"crash_node|n{op.node}|already-down|skipped")
                return
            if not self._fire_fault():
                return
            self.pause(op.node)
            self.sim.schedule(op.duration, lambda: self._maybe_resume(op.node))

    # -- fault controls (also used directly by tests) --------------------------

    def link_down(self, a: int, b: int) -> None:
        """Take the (undirected) link between a and b down; idempotent."""
        if not self.net.link_is_down(a, b):
            self.net.set_link_down(a, b)
            self.record("linkdown", f"n{a}<->n{b}")

    def link_up(self, a: int, b: int) -> None:
        if self.net.link_is_down(a, b):
            self.net.set_link_up(a, b)
            self.record("linkup", f"n{a}<->n{b}")

    def set_lossy_link(self, a: int, b: int, drop_p: float) -> None:
        """Degrade the link between a and b: each message dropped with drop_p."""
        self.net.set_link_lossy(a, b, drop_p)
        self.record("lossy", f"n{a}<->n{b}|p={drop_p}")

    def clear_lossy_link(self, a: int, b: int) -> None:
        if self.net.link_is_lossy(a, b):
            self.net.clear_link_lossy(a, b)
            self.record("lossyheal", f"n{a}<->n{b}")

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

    @property
    def fault_count(self) -> int:
        """How many fault injections the driver reached this run (the ordinal universe
        the shrinker suppresses over)."""
        return self._fault_seq

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
