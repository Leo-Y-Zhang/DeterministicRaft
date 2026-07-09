"""A linearizability oracle over the client-observed history (co-crown A).

The five internal Raft invariants check node logs; they cannot see whether what CLIENTS
observed is a legal single-copy history. This oracle can: given the recorded operations
(each an invoke/return interval against the KV register/map with an observed result), it
searches for a total order -- a *linearization* -- that (a) respects real-time order (if
op A returned before op B was invoked, A precedes B) and (b) makes every op's observed
result match applying it in that order to a sequential reference KV. If such an order
exists the history is linearizable; otherwise it is not, and we report the frontier where
the search got stuck.

Algorithm: Wing & Gong's "linearize and remove" (as popularised by Jepsen/Knossos). At
each step the next op must be one that is *minimal* in the real-time partial order among
the ops not yet placed (no un-placed op returned before it was invoked). Try each minimal
op whose result is legal in the current state, recurse, and memoise dead-end
(state, remaining) pairs. Concurrency width here is bounded by the number of clients, so
the search stays small.

Pending (never-returned) operations are EXCLUDED, and this is sound in Harmonia: a history
row is completed the instant the op is first applied, so an op that never returned never
committed and therefore never touched the state machine. Excluding it cannot hide a real
effect (no completed read could have observed it), so it produces no false positives.

Pure function of an already-deterministic history: draws no randomness, mutates no run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .kv import CAS, GET, PUT, HistoryEntry


@dataclass
class LinearizabilityResult:
    linearizable: bool
    message: str
    # witness ordering (the found linearization) when linearizable; the stuck frontier
    # (minimal ops that could not be legally placed) when not.
    linearization: list[HistoryEntry] | None = None
    stuck: list[HistoryEntry] | None = None
    checked_ops: int = 0

    def __bool__(self) -> bool:
        return self.linearizable


def _apply(store: dict[str, str], op: HistoryEntry) -> tuple[dict[str, str], str]:
    """Apply one operation to a copy of the reference store; return (new_store, result).

    No dedup here: the history already holds one row per logical operation."""
    new = dict(store)
    if op.op == PUT:
        new[op.key] = op.value
        return new, "ok"
    if op.op == GET:
        return new, new.get(op.key, "")
    if op.op == CAS:
        if new.get(op.key, "") == op.expected:
            new[op.key] = op.value
            return new, "ok"
        return new, "fail"
    return new, ""  # unknown op type: treated as a no-op returning ""


def check(
    history: list[HistoryEntry],
    *,
    max_ops: int | None = None,
    budget: int = 500_000,
) -> LinearizabilityResult:
    """Decide whether the completed operations of ``history`` are linearizable.

    ``max_ops`` bounds how many (earliest-invoked) completed ops are checked -- a
    non-linearizable prefix still proves the whole history non-linearizable, so a bounded
    check remains a sound violation detector for sweeps. ``budget`` caps states explored;
    exceeding it returns a non-committal result (linearizable=False, message says so) that
    callers can treat as 'undetermined' rather than a confirmed violation."""
    ops = [o for o in history if o.completed]
    ops.sort(key=lambda o: (o.invoke_step, o.return_step, o.client_id, o.req_id))
    if max_ops is not None:
        ops = ops[:max_ops]
    n = len(ops)
    if n == 0:
        return LinearizabilityResult(True, "empty history is trivially linearizable",
                                     linearization=[], checked_ops=0)

    inv = [o.invoke_step for o in ops]
    ret = [o.return_step or 0 for o in ops]  # completed => return_step is not None

    def minimal(pending: tuple[int, ...]) -> list[int]:
        """Indices in pending with no other pending op that must precede them."""
        if len(pending) == 1:
            return [pending[0]]
        # smallest and second-smallest return_step among pending (to exclude self)
        min1 = min2 = None
        for p in pending:
            r = ret[p]
            if min1 is None or r < ret[min1]:
                min2, min1 = min1, p
            elif min2 is None or r < ret[min2]:
                min2 = p
        out = []
        for p in pending:
            other_min = ret[min2] if p == min1 else ret[min1]  # type: ignore[index]
            if inv[p] <= other_min:
                out.append(p)
        return out

    memo: set[tuple[tuple[tuple[str, str], ...], tuple[int, ...]]] = set()
    deepest: list[int] = []
    explored = 0

    # iterative DFS with explicit stack to avoid Python recursion limits on long histories
    # frame: [store, pending, candidates_or_None, next_candidate_index, path]
    stack: list[list[Any]] = [[{}, tuple(range(n)), None, 0, []]]
    while stack:
        frame = stack[-1]
        store, pending, cands, ci, path = frame
        if not pending:
            witness = [ops[i] for i in path]
            return LinearizabilityResult(
                True, "linearizable", linearization=witness, checked_ops=n)
        if cands is None:
            frame[2] = cands = minimal(pending)
            if len(path) > len(deepest):
                deepest = list(path)
        if ci >= len(cands):
            stack.pop()
            continue
        frame[3] = ci + 1
        i = cands[ci]
        new_store, result = _apply(store, ops[i])
        if result != ops[i].observed:
            continue
        new_pending = tuple(p for p in pending if p != i)
        key = (tuple(sorted(new_store.items())), new_pending)
        if key in memo:
            continue
        memo.add(key)
        explored += 1
        if explored > budget:
            return LinearizabilityResult(
                False, f"search budget ({budget}) exhausted; result undetermined",
                stuck=None, checked_ops=n)
        stack.append([new_store, new_pending, None, 0, [*path, i]])

    placed = set(deepest)
    frontier = [ops[i] for i in range(n) if i not in placed][:8]
    return LinearizabilityResult(
        False,
        f"not linearizable: no legal ordering exists "
        f"(linearized {len(deepest)}/{n} ops before getting stuck)",
        stuck=frontier, checked_ops=n)
