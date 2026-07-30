"""The replicated state machine: a linearizable key-value store, plus the structured
client commands and the client-observed history that the linearizability oracle checks.

Raft replicates an opaque command log; the *meaning* of those commands is the state
machine's business. Harmonia's state machine is a small string->string key-value store
supporting three operations:

  * ``put(key, value)``            -> always "ok"
  * ``get(key)``                   -> the current value ("" if absent)
  * ``cas(key, expected, value)``  -> "ok" if the current value == expected (then set),
                                      else "fail" (compare-and-set)

Commands travel through the log as a canonical ``str`` (``Entry.command``), so the Raft
core, the trace, the digest and the invariant checker are all unchanged. ``Command``
encodes/decodes that string. Decoding is *total*: any string that is not a well-formed
command (e.g. the opaque strings the unit tests use, or a future command type) decodes
to a NOOP that leaves the store untouched -- the state machine never raises on the log.

Everything here is a pure function of the (already deterministic) committed log: applying
the same command sequence always yields the same store and the same results, on every
node and every replay. No randomness is drawn here.
"""

from __future__ import annotations

from dataclasses import dataclass

PUT = "put"
GET = "get"
CAS = "cas"
NOOP = "noop"

_FIELD_SEP = ":"
_NFIELDS = 6


@dataclass(frozen=True)
class Command:
    """A structured client command. ``client_id``/``req_id`` identify the client request
    (used for exactly-once dedup in a later step); the rest describe the KV operation."""

    client_id: int
    req_id: int
    op: str
    key: str = ""
    value: str = ""
    expected: str = ""  # cas only: the value the client expects to overwrite

    def encode(self) -> str:
        """Canonical ``str`` stored in the Raft log. Fields never contain the separator
        (ids are ints; keys/values are generated as ``k<n>``/``v<n>``)."""
        return _FIELD_SEP.join(
            [str(self.client_id), str(self.req_id), self.op, self.key, self.value, self.expected]
        )

    @classmethod
    def decode(cls, s: str) -> Command:
        """Total inverse of :meth:`encode`. Anything not a well-formed command -> NOOP."""
        parts = s.split(_FIELD_SEP)
        if len(parts) != _NFIELDS:
            return cls(-1, -1, NOOP)
        cid, rid, op, key, value, expected = parts
        try:
            client_id, req_id = int(cid), int(rid)
        except ValueError:
            return cls(-1, -1, NOOP)
        if op not in (PUT, GET, CAS):
            return cls(-1, -1, NOOP)
        return cls(client_id, req_id, op, key, value, expected)

    @property
    def is_structured(self) -> bool:
        return self.client_id >= 0 and self.op in (PUT, GET, CAS)


class KVStateMachine:
    """A deterministic string->string key-value store with per-client exactly-once
    sessions (Ongaro dissertation 6.3). Applying a command mutates the store (put/cas)
    and returns the client-visible result; a *duplicate* command (a client re-request
    of a request id it has already applied) returns the CACHED result and re-executes
    nothing. This matters even though the store itself is nearly idempotent: a retried
    ``cas`` recomputed from scratch would return "fail" (the value already moved), but
    the client's operation actually succeeded -- the session cache returns the original
    "ok". The session table is part of the replicated state, so every node dedups
    identically. Monotonic per-client request ids (clients issue one op at a time) make
    "already applied" a simple ``req_id <= last`` test."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.sessions: dict[int, tuple[int, str]] = {}  # client_id -> (last_req_id, result)

    def is_duplicate(self, cmd: Command) -> bool:
        """True if this structured command has already been applied for its client."""
        last = self.sessions.get(cmd.client_id)
        return last is not None and cmd.req_id <= last[0]

    def apply(self, cmd: Command) -> str:
        if not cmd.is_structured:
            return ""  # NOOP / opaque command: no effect, empty result
        last = self.sessions.get(cmd.client_id)
        if last is not None and cmd.req_id <= last[0]:
            return last[1]  # duplicate: cached result, no re-execution
        result = self._execute(cmd)
        self.sessions[cmd.client_id] = (cmd.req_id, result)
        return result

    def apply_read(self, cmd: Command) -> str:
        """Serve a read directly against the current store, with no log entry and no dedup
        (used by ReadIndex reads once leadership is confirmed)."""
        if cmd.op == GET:
            return self.store.get(cmd.key, "")
        return ""

    def _execute(self, cmd: Command) -> str:
        if cmd.op == PUT:
            self.store[cmd.key] = cmd.value
            return "ok"
        if cmd.op == GET:
            return self.store.get(cmd.key, "")
        if cmd.op == CAS:
            if self.store.get(cmd.key, "") == cmd.expected:
                self.store[cmd.key] = cmd.value
                return "ok"
            return "fail"
        return ""

    def snapshot(self) -> dict[str, str]:
        """A copy of the store (deterministic: sorted keys)."""
        return dict(sorted(self.store.items()))

    def capture(self) -> tuple[dict[str, str], dict[int, tuple[int, str]]]:
        """Deep-ish copy of the full replicated state (store + sessions) for a snapshot."""
        return dict(sorted(self.store.items())), dict(sorted(self.sessions.items()))

    def restore(self, store: dict[str, str], sessions: dict[int, tuple[int, str]]) -> None:
        """Replace the state machine wholesale from a snapshot's captured state."""
        self.store = dict(store)
        self.sessions = dict(sessions)


@dataclass
class Snapshot:
    """A compacted state-machine image: everything committed at or below ``last_index``.
    Persistent (survives a crash) so the discarded log prefix can always be recovered.
    ``voters`` is the cluster configuration in force at ``last_index`` -- a configuration
    entry folded into the snapshot must survive compaction and restart like any other
    committed state."""

    last_index: int
    last_term: int
    store: dict[str, str]
    sessions: dict[int, tuple[int, str]]
    voters: tuple[int, ...]


@dataclass
class HistoryEntry:
    """One client-observed operation: what was invoked, and (once it completes) what the
    client saw and when. ``return_step``/``observed`` stay None for an operation that was
    submitted but never committed (an in-flight/indeterminate op) -- the oracle handles
    those. Real-time ordering is captured by the invoke/return simulator-step stamps."""

    client_id: int
    req_id: int
    op: str
    key: str
    value: str
    expected: str
    invoke_step: int
    return_step: int | None = None
    observed: str | None = None

    @property
    def completed(self) -> bool:
        return self.return_step is not None
