"""The Raft node state machine, in one readable module.

Follows Ongaro & Ousterhout, "In Search of an Understandable Consensus Algorithm"
(USENIX ATC 2014), Figure 2. Implemented here: leader election with randomized
timeouts, RequestVote and AppendEntries RPCs, log replication, commitIndex
advancement (majority match with the current-term guard from section 5.4.2), and
follower log repair via nextIndex backoff.

Deliberately NOT implemented (see README limitations): membership changes,
snapshots/log compaction, persistence to disk. A "crash" in the simulator pauses
a node with its state intact, which is equivalent to synchronous persistence of
currentTerm/votedFor/log.

Log indices are 1-based, exactly as in the paper. Index i lives at self.log[i-1].
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .kv import Command, KVStateMachine

if TYPE_CHECKING:
    from .sim import Simulator

FOLLOWER = "follower"
CANDIDATE = "candidate"
LEADER = "leader"


@dataclass(frozen=True)
class Entry:
    term: int
    command: str


@dataclass(frozen=True)
class RequestVote:
    term: int
    candidate: int
    last_log_index: int
    last_log_term: int


@dataclass(frozen=True)
class VoteReply:
    term: int
    voter: int
    granted: bool


@dataclass(frozen=True)
class AppendEntries:
    term: int
    leader: int
    prev_index: int
    prev_term: int
    entries: tuple[Entry, ...]
    leader_commit: int


@dataclass(frozen=True)
class AppendReply:
    term: int
    follower: int
    success: bool
    match_index: int


Message = RequestVote | VoteReply | AppendEntries | AppendReply


@dataclass(frozen=True)
class RaftConfig:
    election_timeout_min: int = 150   # ms; each timer draws uniformly from this range,
    election_timeout_max: int = 300   # which is what breaks split votes (section 5.2)
    heartbeat_interval: int = 50      # ms


# Shared immutable default (frozen dataclass): safe to reuse as an argument default.
DEFAULT_CONFIG = RaftConfig()


class RaftNode:
    """One Raft server. All I/O goes through callables injected by the cluster."""

    def __init__(
        self,
        node_id: int,
        peer_ids: list[int],
        sim: Simulator,
        send: Callable[[int, int, Message], None],
        record: Callable[[str, str], None],
        config: RaftConfig = DEFAULT_CONFIG,
        on_apply: Callable[[str, str], None] | None = None,
    ) -> None:
        self.id = node_id
        self.peers = sorted(peer_ids)
        self.sim = sim
        self._send = send
        self._record = record
        self._on_apply = on_apply     # (command_str, result) reported as each entry applies
        self.config = config
        self.cluster_size = len(self.peers) + 1

        # Persistent state (Figure 2). We keep it in memory: simulator "crashes"
        # pause the node with state intact, equivalent to synchronous persistence.
        self.term: int = 0
        self.voted_for: int | None = None
        self.log: list[Entry] = []

        # Volatile state.
        self.role: str = FOLLOWER
        self.commit_index: int = 0
        self.last_applied: int = 0
        self.applied: list[str] = []          # applied command labels (feeds the checker)
        self.kv = KVStateMachine()            # the replicated state machine (see kv.py)
        self.leader_id: int | None = None
        self.alive: bool = True
        self.log_version: int = 0             # bumped on any log mutation (for checker)

        # Leader-only volatile state, reinitialized after each election.
        self.next_index: dict[int, int] = {}
        self.match_index: dict[int, int] = {}
        self._votes: set[int] = set()

        self._timer_seq: int = 0              # invalidates stale timer callbacks

    # -- log helpers ----------------------------------------------------------

    def last_log_index(self) -> int:
        return len(self.log)

    def term_at(self, index: int) -> int:
        """Term of the entry at 1-based `index`; 0 for the empty prefix (index 0)."""
        if index == 0:
            return 0
        return self.log[index - 1].term

    def entry_at(self, index: int) -> Entry:
        return self.log[index - 1]

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        self._arm_election_timer()

    def pause(self) -> None:
        """Simulated crash: stop reacting. State is retained (== persisted)."""
        self.alive = False
        self._timer_seq += 1

    def resume(self) -> None:
        self.alive = True
        self._timer_seq += 1
        if self.role == LEADER:
            # A paused leader resumes as leader of its (possibly stale) term; it
            # steps down as soon as it hears a higher term. This is safe: election
            # safety is per-term.
            self._broadcast_heartbeats()
            self._arm_heartbeat_timer()
        else:
            self._arm_election_timer()

    # -- timers ---------------------------------------------------------------

    def _arm_election_timer(self) -> None:
        self._timer_seq += 1
        seq = self._timer_seq
        timeout = self.sim.rng.randint(
            self.config.election_timeout_min, self.config.election_timeout_max
        )
        self.sim.schedule(timeout, lambda: self._on_election_timeout(seq))

    def _on_election_timeout(self, seq: int) -> None:
        if not self.alive or seq != self._timer_seq or self.role == LEADER:
            return
        self._record("timer", f"n{self.id}|election-timeout|term={self.term}")
        self.start_election()

    def _arm_heartbeat_timer(self) -> None:
        self._timer_seq += 1
        seq = self._timer_seq
        self.sim.schedule(self.config.heartbeat_interval, lambda: self._on_heartbeat(seq))

    def _on_heartbeat(self, seq: int) -> None:
        if not self.alive or seq != self._timer_seq or self.role != LEADER:
            return
        self._broadcast_heartbeats()
        self._arm_heartbeat_timer()

    # -- role transitions -----------------------------------------------------

    def _become_follower(self, term: int) -> None:
        changed = term > self.term
        self.term = term
        if changed:
            self.voted_for = None
        if self.role != FOLLOWER or changed:
            self.role = FOLLOWER
            self._record("role", f"n{self.id}|follower|term={self.term}")
        self._arm_election_timer()

    def start_election(self) -> None:
        """Become candidate: bump term, vote for self, solicit votes (section 5.2)."""
        self.term += 1
        self.role = CANDIDATE
        self.voted_for = self.id
        self.leader_id = None
        self._votes = {self.id}
        self._record("election", f"n{self.id}|term={self.term}")
        self._record("role", f"n{self.id}|candidate|term={self.term}")
        self._arm_election_timer()  # randomized retry breaks split votes
        req = RequestVote(self.term, self.id, self.last_log_index(),
                          self.term_at(self.last_log_index()))
        for p in self.peers:
            self._send(self.id, p, req)
        self._maybe_win()  # single-node cluster wins immediately

    def _become_leader(self) -> None:
        self.role = LEADER
        self.leader_id = self.id
        self._record("role", f"n{self.id}|leader|term={self.term}")
        last = self.last_log_index()
        self.next_index = dict.fromkeys(self.peers, last + 1)
        self.match_index = dict.fromkeys(self.peers, 0)
        self.match_index[self.id] = last
        self._broadcast_heartbeats()
        self._arm_heartbeat_timer()

    def _maybe_win(self) -> None:
        if self.role == CANDIDATE and len(self._votes) * 2 > self.cluster_size:
            self._become_leader()

    # -- client interface -----------------------------------------------------

    def client_command(self, command: str) -> bool:
        """Append a client command if this node believes it is the leader."""
        if self.role != LEADER or not self.alive:
            return False
        self.log.append(Entry(self.term, command))
        self.log_version += 1
        self.match_index[self.id] = self.last_log_index()
        self._record(
            "append",
            f"n{self.id}|index={self.last_log_index()}|term={self.term}|{command}",
        )
        self._broadcast_heartbeats()  # replicate eagerly instead of waiting a beat
        return True

    # -- message dispatch -----------------------------------------------------

    def handle(self, src: int, msg: Message) -> None:
        if not self.alive:
            return
        if isinstance(msg, RequestVote):
            self._on_request_vote(msg)
        elif isinstance(msg, VoteReply):
            self._on_vote_reply(msg)
        elif isinstance(msg, AppendEntries):
            self._on_append_entries(msg)
        elif isinstance(msg, AppendReply):
            self._on_append_reply(msg)

    # -- RequestVote RPC (Figure 2, receiver implementation) -------------------

    def _on_request_vote(self, msg: RequestVote) -> None:
        if msg.term > self.term:
            self._become_follower(msg.term)
        granted = False
        if msg.term == self.term and self.voted_for in (None, msg.candidate):
            # Election restriction (section 5.4.1): only vote for candidates whose
            # log is at least as up-to-date as ours.
            my_last_term = self.term_at(self.last_log_index())
            up_to_date = (msg.last_log_term, msg.last_log_index) >= (
                my_last_term, self.last_log_index())
            if up_to_date:
                granted = True
                self.voted_for = msg.candidate
                self._record("vote", f"n{self.id}->n{msg.candidate}|term={self.term}")
                self._arm_election_timer()
        self._send(self.id, msg.candidate, VoteReply(self.term, self.id, granted))

    def _on_vote_reply(self, msg: VoteReply) -> None:
        if msg.term > self.term:
            self._become_follower(msg.term)
            return
        if self.role != CANDIDATE or msg.term < self.term or not msg.granted:
            return
        self._votes.add(msg.voter)
        self._maybe_win()

    # -- AppendEntries RPC (Figure 2, receiver implementation) -----------------

    def _on_append_entries(self, msg: AppendEntries) -> None:
        if msg.term > self.term:
            self._become_follower(msg.term)
        if msg.term < self.term:
            self._send(self.id, msg.leader, AppendReply(self.term, self.id, False, 0))
            return
        # Same term: the sender is the legitimate leader for this term.
        if self.role != FOLLOWER:
            self._become_follower(msg.term)
        self.leader_id = msg.leader
        self._arm_election_timer()

        # Consistency check: our log must contain an entry at prev_index whose
        # term matches prev_term (Log Matching, section 5.3).
        if msg.prev_index > 0 and (
            self.last_log_index() < msg.prev_index
            or self.term_at(msg.prev_index) != msg.prev_term
        ):
            self._send(self.id, msg.leader, AppendReply(self.term, self.id, False, 0))
            return

        # Append new entries, deleting any conflicting suffix. Entries that already
        # match are kept untouched, which makes duplicated/reordered AppendEntries
        # idempotent and never truncates committed entries.
        index = msg.prev_index
        for entry in msg.entries:
            index += 1
            if self.last_log_index() >= index:
                if self.term_at(index) != entry.term:
                    del self.log[index - 1:]
                    self.log_version += 1
                else:
                    continue
            self.log.append(entry)
            self.log_version += 1
        match = msg.prev_index + len(msg.entries)

        # Advance commit index; the max() guards against a stale, reordered
        # AppendEntries lowering it.
        if msg.leader_commit > self.commit_index:
            self._set_commit_index(max(self.commit_index, min(msg.leader_commit, match)))
        self._send(self.id, msg.leader, AppendReply(self.term, self.id, True, match))

    def _on_append_reply(self, msg: AppendReply) -> None:
        if msg.term > self.term:
            self._become_follower(msg.term)
            return
        if self.role != LEADER or msg.term < self.term:
            return
        if msg.success:
            # max() so late/duplicated replies never move progress backwards.
            self.match_index[msg.follower] = max(
                self.match_index.get(msg.follower, 0), msg.match_index)
            self.next_index[msg.follower] = max(
                self.next_index.get(msg.follower, 1), msg.match_index + 1)
            self._advance_commit()
        else:
            # Log repair: back off nextIndex and retry (section 5.3).
            self.next_index[msg.follower] = max(1, self.next_index.get(msg.follower, 1) - 1)
            self._send_append_entries(msg.follower)

    # -- replication ----------------------------------------------------------

    def _send_append_entries(self, peer: int) -> None:
        prev = self.next_index[peer] - 1
        entries = tuple(self.log[prev:])
        self._send(self.id, peer, AppendEntries(
            self.term, self.id, prev, self.term_at(prev), entries, self.commit_index))

    def _broadcast_heartbeats(self) -> None:
        for p in self.peers:
            self._send_append_entries(p)

    def _advance_commit(self) -> None:
        """Commit rule (sections 5.3 / 5.4.2): advance to the highest N replicated on
        a majority, but only if log[N].term == currentTerm. Entries from earlier
        terms commit indirectly, never by counting replicas."""
        for n in range(self.last_log_index(), self.commit_index, -1):
            if self.term_at(n) != self.term:
                break  # older-term entries below: the guard forbids direct commit
            votes = sum(1 for m in self.match_index.values() if m >= n)
            if votes * 2 > self.cluster_size:
                self._set_commit_index(n)
                break

    def _set_commit_index(self, index: int) -> None:
        if index <= self.commit_index:
            return
        self.commit_index = index
        self._record("commit", f"n{self.id}|index={index}")
        while self.last_applied < self.commit_index:
            self.last_applied += 1
            command = self.entry_at(self.last_applied).command
            self.applied.append(command)
            result = self.kv.apply(Command.decode(command))
            self._record("apply", f"n{self.id}|index={self.last_applied}|{command}")
            if self._on_apply is not None:
                self._on_apply(command, result)
