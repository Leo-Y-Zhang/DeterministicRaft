"""Safety invariant checker, run after every simulator step.

Asserts the five Raft safety properties from Figure 3 of the paper, plus commit
index monotonicity. A violation raises InvariantViolation carrying the seed and
step so the run can be replayed exactly.

The checker only reads node state (role, term, log, commit_index, applied,
log_version), never mutates it. Caches keyed on log_version keep the per-step
cost near-constant: expensive pairwise log comparisons only rerun when one of
the logs actually changed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from .node import LEADER, Entry


class NodeView(Protocol):
    """The narrow read-only interface the checker needs (real or fake nodes)."""

    id: int
    role: str
    term: int
    log: list[Entry]
    commit_index: int
    applied: list[str]
    log_version: int
    incarnation: int


class InvariantViolation(AssertionError):
    def __init__(self, invariant: str, detail: str, seed: int, step: int, replay: str) -> None:
        self.invariant = invariant
        self.detail = detail
        self.seed = seed
        self.step = step
        self.replay = replay
        super().__init__(
            f"[{invariant}] {detail} (seed={seed} step={step}) -- reproduce with: {replay}"
        )


class InvariantChecker:
    def __init__(self, seed: int, replay_hint: str = "") -> None:
        self.seed = seed
        self.replay_hint = replay_hint or f"harmonia replay --seed {seed}"
        self.checks_run = 0
        # Election Safety: term -> the single node id ever seen as leader for it.
        self.leaders_by_term: dict[int, int] = {}
        # Leader Completeness / State Machine Safety bookkeeping:
        # index -> (Entry, term in force when the commit was first observed)
        self.committed: dict[int, tuple[Entry, int]] = {}
        # index -> command first seen applied at that index, by any node
        self.applied_at: dict[int, str] = {}
        # per-node caches
        self._prev_commit: dict[int, int] = {}
        self._applied_seen: dict[int, int] = {}
        self._incarnation: dict[int, int] = {}  # last-seen incarnation, for crash rebasing
        # _leader_snapshot: id -> (term, ver, log copy); _completeness_checked: id -> (term, ver, n)
        self._leader_snapshot: dict[int, tuple[int, int, list[Entry]]] = {}
        self._pair_checked: dict[tuple[int, int], tuple[int, int]] = {}
        self._completeness_checked: dict[int, tuple[int, int, int]] = {}

    def _fail(self, invariant: str, detail: str, step: int) -> None:
        raise InvariantViolation(invariant, detail, self.seed, step, self.replay_hint)

    def check(self, nodes: Mapping[int, NodeView], step: int) -> None:
        """Run all invariants against the current cluster state."""
        self.checks_run += 1
        ids = sorted(nodes)
        self._handle_restarts(nodes, ids)
        self._check_election_safety(nodes, ids, step)
        self._check_leader_append_only(nodes, ids, step)
        self._check_log_matching(nodes, ids, step)
        self._observe_commits(nodes, ids, step)
        self._check_leader_completeness(nodes, ids, step)
        self._check_state_machine_safety(nodes, ids, step)

    # -- crash rebasing -------------------------------------------------------

    def _handle_restarts(self, nodes: Mapping[int, NodeView], ids: list[int]) -> None:
        """A crash-restart legitimately discards volatile state, so commit index and
        applied progress reset. Those are tracked PER INCARNATION: when a node's
        incarnation changes, rebase its caches to the fresh state instead of flagging the
        reset as a regression. Persistent state (log/term) is unaffected, so the log-based
        invariants (Log Matching, Leader Completeness) keep checking across the crash."""
        for i in ids:
            inc = nodes[i].incarnation
            prev = self._incarnation.get(i)
            self._incarnation[i] = inc
            if prev is not None and prev != inc:  # a real restart, not first sight
                self._prev_commit[i] = nodes[i].commit_index
                self._applied_seen[i] = len(nodes[i].applied)

    # -- Election Safety: at most one leader can be elected in a given term ----

    def _check_election_safety(
        self, nodes: Mapping[int, NodeView], ids: list[int], step: int
    ) -> None:
        for i in ids:
            n = nodes[i]
            if n.role == LEADER:
                prev = self.leaders_by_term.setdefault(n.term, n.id)
                if prev != n.id:
                    self._fail("ElectionSafety",
                               f"term {n.term} has two leaders: n{prev} and n{n.id}", step)

    # -- Leader Append-Only: a leader never overwrites or deletes its entries --

    def _check_leader_append_only(
        self, nodes: Mapping[int, NodeView], ids: list[int], step: int
    ) -> None:
        for i in ids:
            n = nodes[i]
            if n.role != LEADER:
                self._leader_snapshot.pop(i, None)
                continue
            snap = self._leader_snapshot.get(i)
            if snap is None or snap[0] != n.term:
                self._leader_snapshot[i] = (n.term, n.log_version, list(n.log))
                continue
            _, ver, old_log = snap
            if ver == n.log_version:
                continue
            if len(n.log) < len(old_log) or n.log[: len(old_log)] != old_log:
                self._fail("LeaderAppendOnly",
                           f"leader n{n.id} (term {n.term}) mutated its log "
                           f"(was {len(old_log)} entries, now {len(n.log)})", step)
            self._leader_snapshot[i] = (n.term, n.log_version, list(n.log))

    # -- Log Matching: same index+term => identical entries up to that index ---

    def _check_log_matching(self, nodes: Mapping[int, NodeView], ids: list[int], step: int) -> None:
        for ai in range(len(ids)):
            for bi in range(ai + 1, len(ids)):
                a, b = nodes[ids[ai]], nodes[ids[bi]]
                key = (a.id, b.id)
                vers = (a.log_version, b.log_version)
                if self._pair_checked.get(key) == vers:
                    continue
                # Find the highest index where both logs agree on the term...
                top = 0
                for idx in range(min(len(a.log), len(b.log)), 0, -1):
                    if a.log[idx - 1].term == b.log[idx - 1].term:
                        top = idx
                        break
                # ...then everything up to it must be identical.
                if top and a.log[:top] != b.log[:top]:
                    diverge = next(k for k in range(top)
                                   if a.log[k] != b.log[k]) + 1
                    self._fail("LogMatching",
                               f"n{a.id} and n{b.id} agree on term at index {top} "
                               f"but diverge at index {diverge}", step)
                self._pair_checked[key] = vers

    # -- commit bookkeeping (feeds Leader Completeness + monotonicity) ---------

    def _observe_commits(self, nodes: Mapping[int, NodeView], ids: list[int], step: int) -> None:
        for i in ids:
            n = nodes[i]
            prev = self._prev_commit.get(i, 0)
            if n.commit_index < prev:
                self._fail("CommitIndexMonotonic",
                           f"n{n.id} commit_index regressed {prev} -> {n.commit_index}", step)
            for idx in range(prev + 1, n.commit_index + 1):
                entry = n.log[idx - 1]
                known = self.committed.get(idx)
                if known is None:
                    self.committed[idx] = (entry, n.term)
                elif known[0] != entry:
                    self._fail("StateMachineSafety",
                               f"index {idx} committed as {known[0]} on one node "
                               f"but {entry} on n{n.id}", step)
            self._prev_commit[i] = n.commit_index

    # -- Leader Completeness: committed entries appear in all future leaders ---

    def _check_leader_completeness(
        self, nodes: Mapping[int, NodeView], ids: list[int], step: int
    ) -> None:
        for i in ids:
            n = nodes[i]
            if n.role != LEADER:
                self._completeness_checked.pop(i, None)
                continue
            state = (n.term, n.log_version, len(self.committed))
            if self._completeness_checked.get(i) == state:
                continue
            for idx in sorted(self.committed):
                entry, commit_term = self.committed[idx]
                if n.term < commit_term:
                    continue  # the property binds leaders of later terms only
                if len(n.log) < idx or n.log[idx - 1] != entry:
                    self._fail("LeaderCompleteness",
                               f"leader n{n.id} (term {n.term}) is missing committed "
                               f"entry {entry} at index {idx}", step)
            self._completeness_checked[i] = state

    # -- State Machine Safety: no two nodes apply different commands at an index

    def _check_state_machine_safety(
        self, nodes: Mapping[int, NodeView], ids: list[int], step: int
    ) -> None:
        for i in ids:
            n = nodes[i]
            seen = self._applied_seen.get(i, 0)
            for idx in range(seen + 1, len(n.applied) + 1):
                cmd = n.applied[idx - 1]
                known = self.applied_at.setdefault(idx, cmd)
                if known != cmd:
                    self._fail("StateMachineSafety",
                               f"index {idx} applied as {known!r} on one node "
                               f"but {cmd!r} on n{n.id}", step)
            self._applied_seen[i] = len(n.applied)
