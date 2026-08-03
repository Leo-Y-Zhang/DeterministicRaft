"""Safety invariant checker, run after every simulator step.

Asserts the five Raft safety properties from Figure 3 of the paper, plus commit
index monotonicity and a per-configuration commit quorum (a newly committed entry
must be held by a majority of the committing node's CURRENT configuration --
membership changes make cluster majorities per-node state, never a fixed size).
A violation raises InvariantViolation carrying the seed and step so the run can
be replayed exactly.

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
    """The narrow read-only interface the checker needs (real or fake nodes).

    Log access is LOGICAL (1-based indices), so the checker never sees the physical
    compaction offset: base_index is the highest index folded into a snapshot, and the
    live `log`/`applied` cover indices above it (`applied[k]` is logical base_index+1+k).
    `voters` is the node's CURRENT cluster configuration (membership changes make this
    per-node, per-moment state); majorities are judged against it, never a fixed size."""

    id: int
    role: str
    term: int
    log: list[Entry]
    commit_index: int
    applied: list[str]
    log_version: int
    incarnation: int
    base_index: int
    base_term: int
    voters: tuple[int, ...]

    def last_log_index(self) -> int: ...
    def term_at(self, index: int) -> int: ...
    def entry_at(self, index: int) -> Entry: ...


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
        self.replay_hint = replay_hint or f"raftverified replay --seed {seed}"
        self.checks_run = 0
        # Election Safety: term -> the single node id ever seen as leader for it.
        self.leaders_by_term: dict[int, int] = {}
        # Leader Completeness / State Machine Safety bookkeeping:
        # index -> (Entry, term in force when the commit was first observed)
        self.committed: dict[int, tuple[Entry, int]] = {}
        # index -> command first seen applied at that index, by any node
        self.applied_at: dict[int, str] = {}
        # highest commit index observed on ANY node so far; a commit index above it is a
        # global first observation, which is where the per-configuration quorum is judged
        self._max_commit: int = 0
        # per-node caches
        self._prev_commit: dict[int, int] = {}
        self._applied_seen: dict[int, int] = {}
        self._incarnation: dict[int, int] = {}  # last-seen incarnation, for crash rebasing
        # _leader_snapshot: id -> (term, ver, base_index, log copy) for append-only checks
        self._leader_snapshot: dict[int, tuple[int, int, int, list[Entry]]] = {}
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
                self._applied_seen[i] = nodes[i].base_index + len(nodes[i].applied)

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
            cur = (n.term, n.log_version, n.base_index, list(n.log))
            if snap is None or snap[0] != n.term:
                self._leader_snapshot[i] = cur
                continue
            _, ver, old_base, old_log = snap
            if ver == n.log_version:
                continue
            # A leader may extend its log and compact its committed prefix, but never shrink
            # at the top or change an entry it already holds. Compare the logical overlap.
            old_top, new_top = old_base + len(old_log), n.last_log_index()
            mutated = new_top < old_top
            if not mutated:
                for idx in range(max(old_base, n.base_index) + 1, old_top + 1):
                    if old_log[idx - old_base - 1] != n.entry_at(idx):
                        mutated = True
                        break
            if mutated:
                self._fail("LeaderAppendOnly",
                           f"leader n{n.id} (term {n.term}) mutated its log "
                           f"(top was {old_top}, now {new_top})", step)
            self._leader_snapshot[i] = cur

    # -- Log Matching: same index+term => identical entries up to that index ---

    def _check_log_matching(self, nodes: Mapping[int, NodeView], ids: list[int], step: int) -> None:
        for ai in range(len(ids)):
            for bi in range(ai + 1, len(ids)):
                a, b = nodes[ids[ai]], nodes[ids[bi]]
                key = (a.id, b.id)
                vers = (a.log_version, b.log_version)
                if self._pair_checked.get(key) == vers:
                    continue
                # Compare only the overlapping, non-compacted logical range; the compacted
                # prefixes below it are committed and equal by induction.
                lo = max(a.base_index, b.base_index) + 1
                hi = min(a.last_log_index(), b.last_log_index())
                top = 0
                for idx in range(hi, lo - 1, -1):
                    if a.term_at(idx) == b.term_at(idx):
                        top = idx
                        break
                # everything from lo up to the highest agreed index must be identical
                for idx in range(lo, top + 1):
                    if a.entry_at(idx) != b.entry_at(idx):
                        self._fail("LogMatching",
                                   f"n{a.id} and n{b.id} agree on term at index {top} "
                                   f"but diverge at index {idx}", step)
                self._pair_checked[key] = vers

    # -- commit bookkeeping (feeds Leader Completeness + monotonicity + quorum) ---------

    def _holds(self, n: NodeView, idx: int, entry: Entry) -> bool:
        """Does this node hold the committed entry? Either live in its log, or folded
        into its snapshot (a compacted prefix is committed state by construction)."""
        if idx <= n.base_index:
            return True
        return n.last_log_index() >= idx and n.entry_at(idx) == entry

    def _observe_commits(self, nodes: Mapping[int, NodeView], ids: list[int], step: int) -> None:
        for i in ids:
            n = nodes[i]
            prev = self._prev_commit.get(i, 0)
            if n.commit_index < prev:
                self._fail("CommitIndexMonotonic",
                           f"n{n.id} commit_index regressed {prev} -> {n.commit_index}", step)
            # skip indices at or below the compaction boundary (already observed, now folded
            # into the snapshot); observe only newly committed entries still in the log
            for idx in range(max(prev, n.base_index) + 1, n.commit_index + 1):
                entry = n.entry_at(idx)
                known = self.committed.get(idx)
                if known is None:
                    self.committed[idx] = (entry, n.term)
                elif known[0] != entry:
                    self._fail("StateMachineSafety",
                               f"index {idx} committed as {known[0]} on one node "
                               f"but {entry} on n{n.id}", step)
                # Commit quorum, per CONFIGURATION (membership makes majorities per-node
                # state): a globally NEW commit must be backed by a majority of the
                # committing node's current voting set actually holding the entry. The
                # first observer of a new commit is always the leader that counted the
                # majority (the checker runs after every step, before replies propagate),
                # so its `voters` is exactly the configuration the count was taken over.
                if idx > self._max_commit:
                    holders = sum(1 for v in n.voters
                                  if v in nodes and self._holds(nodes[v], idx, entry))
                    if holders * 2 <= len(n.voters):
                        self._fail(
                            "CommitQuorum",
                            f"n{n.id} committed index {idx} but only {holders} of its "
                            f"{len(n.voters)}-server configuration hold the entry", step)
            self._max_commit = max(self._max_commit, n.commit_index)
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
                if idx <= n.base_index:
                    continue  # folded into this leader's snapshot -> it holds the entry
                if n.last_log_index() < idx or n.entry_at(idx) != entry:
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
            seen = self._applied_seen.get(i, 0)          # highest logical index checked
            top = n.base_index + len(n.applied)          # highest applied logical index
            for logical in range(max(seen, n.base_index) + 1, top + 1):
                cmd = n.applied[logical - n.base_index - 1]
                known = self.applied_at.setdefault(logical, cmd)
                if known != cmd:
                    self._fail("StateMachineSafety",
                               f"index {logical} applied as {known!r} on one node "
                               f"but {cmd!r} on n{n.id}", step)
            self._applied_seen[i] = max(seen, top)
