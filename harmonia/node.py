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

Log indices are 1-based, exactly as in the paper. With no snapshot the entry at logical
index i lives at self.log[i-1]; once a prefix is compacted (base_index > 0) all indexing
goes through the log helpers, which hide the physical offset.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .bugs import NO_BUGS, Bugs
from .kv import Command, KVStateMachine, Snapshot

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


@dataclass(frozen=True)
class InstallSnapshot:
    """Sent to a follower that has fallen behind the leader's compaction point (section 7).
    Carries the whole state-machine image instead of individual log entries."""
    term: int
    leader: int
    last_index: int
    last_term: int
    store: dict[str, str]
    sessions: dict[int, tuple[int, str]]


@dataclass(frozen=True)
class ReadHeartbeat:
    """A leadership-confirmation ping for a ReadIndex read (section 8). Kept separate from
    AppendEntries so ordinary runs are byte-identical when ReadIndex is off."""
    term: int
    leader: int
    read_id: int


@dataclass(frozen=True)
class ReadAck:
    term: int
    follower: int
    read_id: int


Message = (RequestVote | VoteReply | AppendEntries | AppendReply | InstallSnapshot
           | ReadHeartbeat | ReadAck)


@dataclass(frozen=True)
class RaftConfig:
    election_timeout_min: int = 150   # ms; each timer draws uniformly from this range,
    election_timeout_max: int = 300   # which is what breaks split votes (section 5.2)
    heartbeat_interval: int = 50      # ms
    snapshot_threshold: int = 0       # compact once this many applied entries pile up above
    #                                   the base; 0 disables snapshots (default)


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
        bugs: Bugs = NO_BUGS,
    ) -> None:
        self.id = node_id
        self.peers = sorted(peer_ids)
        self.sim = sim
        self._send = send
        self._record = record
        self._on_apply = on_apply     # (command_str, result) reported as each entry applies
        self.bugs = bugs              # deliberately-injected defects (default: none)
        self.config = config
        self.cluster_size = len(self.peers) + 1

        # Persistent state (Figure 2), rebuilt from stable storage after a crash.
        self.term: int = 0
        self.voted_for: int | None = None
        self.log: list[Entry] = []
        # Log compaction boundary: `log` holds entries at 1-based logical indices
        # (base_index+1 .. base_index+len). base_index==0 means no snapshot yet; a snapshot
        # (added later) advances it and discards the prefix. All indexing goes through the
        # helpers below so the rest of the algorithm never sees the physical offset.
        self.base_index: int = 0
        self.base_term: int = 0
        self.snapshot: Snapshot | None = None  # persisted compacted state (Figure 2 + §7)

        # Volatile state.
        self.role: str = FOLLOWER
        self.commit_index: int = 0
        self.last_applied: int = 0
        # applied command labels for logical indices (base_index, last_applied]; applied[k]
        # is logical index base_index+1+k. Feeds State Machine Safety.
        self.applied: list[str] = []
        self.kv = KVStateMachine()            # the replicated state machine (see kv.py)
        self.leader_id: int | None = None
        self.alive: bool = True
        self.log_version: int = 0             # bumped on any log mutation (for checker)
        self.incarnation: int = 0             # bumped on every crash-restart (for checker)

        # Leader-only volatile state, reinitialized after each election.
        self.next_index: dict[int, int] = {}
        self.match_index: dict[int, int] = {}
        self._votes: set[int] = set()
        # highest request id per client already present in this node's log; a leader uses
        # it to avoid appending a duplicate of a retried request (rebuilt on election).
        self._client_index: dict[int, int] = {}
        # in-flight ReadIndex reads: read_id -> (encoded command, read index, acking nodes)
        self._pending_reads: dict[int, tuple[str, int, set[int]]] = {}
        self._next_read_id: int = 0
        # a new leader may not know the true commit index until it commits an entry in its
        # OWN term (Ongaro 8); until then it must not serve ReadIndex reads (they'd be stale)
        self._committed_this_term: bool = False

        self._timer_seq: int = 0              # invalidates stale timer callbacks

    # -- log helpers (all indexing is logical; base_index hides log compaction) -----

    def last_log_index(self) -> int:
        return self.base_index + len(self.log)

    def _phys(self, index: int) -> int:
        """Physical offset into self.log for a 1-based logical index above the base."""
        return index - self.base_index - 1

    def term_at(self, index: int) -> int:
        """Term of the entry at 1-based logical `index`; the base term for anything at or
        below the compaction boundary (index 0 with no snapshot => term 0)."""
        if index <= self.base_index:
            return self.base_term
        return self.log[self._phys(index)].term

    def entry_at(self, index: int) -> Entry:
        return self.log[self._phys(index)]

    def log_suffix(self, from_index: int) -> tuple[Entry, ...]:
        """Entries at logical `from_index` and above (for replication). Callers only ever
        pass an index above the base; clamp defensively so an at/below-base index returns
        the whole tail instead of a negative-offset slice."""
        return tuple(self.log[max(0, self._phys(from_index)):])

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        self._arm_election_timer()

    def pause(self) -> None:
        """Simulated crash: lose ALL volatile state (Figure 2). Only currentTerm,
        votedFor and the log are persistent (stable storage) and survive; everything
        else -- role, commit/apply indexes, the state machine, leader bookkeeping -- is
        rebuilt after restart by re-applying the persisted log."""
        self.alive = False
        self._reset_volatile()

    def resume(self) -> None:
        """Restart from stable storage: come back as a follower holding only the
        persistent triple. The commit index and state machine are re-derived as the node
        re-learns the commit index from the leader and replays its log."""
        self.alive = True
        self._arm_election_timer()

    def _reset_volatile(self) -> None:
        """Discard volatile state and bump the incarnation. currentTerm/votedFor/log/
        base_index/snapshot are persisted and outlive a crash. The state machine is
        rebuilt from the persisted snapshot (if any); the commit index resets to the
        snapshot boundary and is re-learned from the leader as the log tail is re-applied."""
        self.role = FOLLOWER
        self.kv = KVStateMachine()
        if self.snapshot is not None:
            self.kv.restore(self.snapshot.store, self.snapshot.sessions)
            self.commit_index = self.snapshot.last_index
            self.last_applied = self.snapshot.last_index
        else:
            self.commit_index = 0
            self.last_applied = 0
        self.applied = []
        self.leader_id = None
        self.next_index = {}
        self.match_index = {}
        self._votes = set()
        self._client_index = {}
        self._pending_reads = {}
        self._committed_this_term = False
        self._timer_seq += 1        # invalidate any in-flight timer callbacks
        self.incarnation += 1

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
        self._pending_reads = {}  # a stepped-down leader cannot confirm its pending reads
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
        self._committed_this_term = False
        self._rebuild_client_index()
        self._broadcast_heartbeats()
        self._arm_heartbeat_timer()

    def _rebuild_client_index(self) -> None:
        """Recompute the highest request id per client from this node's log, so a fresh
        leader won't re-append requests it already holds (Ongaro 6.3, leader side)."""
        index: dict[int, int] = {}
        for entry in self.log:
            cmd = Command.decode(entry.command)
            if cmd.is_structured and cmd.req_id > index.get(cmd.client_id, -1):
                index[cmd.client_id] = cmd.req_id
        self._client_index = index

    def _maybe_win(self) -> None:
        if self.role == CANDIDATE and len(self._votes) * 2 > self.cluster_size:
            self._become_leader()

    # -- client interface -----------------------------------------------------

    def client_command(self, command: str) -> bool:
        """Append a client command if this node believes it is the leader.

        Leader-side dedup: a structured request already present in this leader's log (a
        retry) is acknowledged without appending a second entry (Ongaro 6.3). Duplicates
        that still slip in via a different leader are caught again at apply time."""
        if self.role != LEADER or not self.alive:
            return False
        cmd = Command.decode(command)
        if cmd.is_structured:
            if cmd.req_id <= self._client_index.get(cmd.client_id, -1):
                self._record("dedup", f"n{self.id}|{command}")
                return True
            self._client_index[cmd.client_id] = cmd.req_id
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
        elif isinstance(msg, InstallSnapshot):
            self._on_install_snapshot(msg)
        elif isinstance(msg, ReadHeartbeat):
            self._on_read_heartbeat(msg)
        elif isinstance(msg, ReadAck):
            self._on_read_ack(msg)

    # -- ReadIndex reads (section 8): confirm leadership, then serve locally ---

    def request_read(self, command: str) -> bool:
        """Begin a linearizable read: record the commit index and broadcast a heartbeat
        round to confirm we still lead. When a majority acks in this term, the read is
        served from local state via the apply callback. Returns False if not the leader,
        or if we have not yet committed an entry in this term (commit index may be stale)."""
        if self.role != LEADER or not self.alive or not self._committed_this_term:
            return False
        read_id = self._next_read_id
        self._next_read_id += 1
        self._pending_reads[read_id] = (command, self.commit_index, {self.id})
        self._record("read", f"n{self.id}|id={read_id}|index={self.commit_index}")
        for p in self.peers:
            self._send(self.id, p, ReadHeartbeat(self.term, self.id, read_id))
        self._try_serve_read(read_id)  # single-node cluster confirms immediately
        return True

    def _on_read_heartbeat(self, msg: ReadHeartbeat) -> None:
        if msg.term > self.term:
            self._become_follower(msg.term)
        if msg.term >= self.term:
            self.leader_id = msg.leader
            self._arm_election_timer()
        self._send(self.id, msg.leader, ReadAck(self.term, self.id, msg.read_id))

    def _on_read_ack(self, msg: ReadAck) -> None:
        if msg.term > self.term:
            self._become_follower(msg.term)
            return
        pending = self._pending_reads.get(msg.read_id)
        if pending is None or msg.term != self.term or self.role != LEADER:
            return
        pending[2].add(msg.follower)
        self._try_serve_read(msg.read_id)

    def _try_serve_read(self, read_id: int) -> None:
        pending = self._pending_reads.get(read_id)
        if pending is None:
            return
        command, read_index, acks = pending
        if len(acks) * 2 > self.cluster_size and self.last_applied >= read_index:
            del self._pending_reads[read_id]
            result = self.kv.apply_read(Command.decode(command))
            self._record("readserved", f"n{self.id}|id={read_id}|{command}")
            if self._on_apply is not None:
                self._on_apply(command, result)

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
            if self.bugs.vote_for_stale_candidate:
                up_to_date = True  # BUG: ignore the 5.4.1 log-freshness restriction
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
        # term matches prev_term (Log Matching, section 5.3). The BUG ignores the
        # term mismatch (but still guards the index bound to avoid a gap/crash).
        too_short = self.last_log_index() < msg.prev_index
        term_mismatch = not too_short and self.term_at(msg.prev_index) != msg.prev_term
        if msg.prev_index > 0 and (
            too_short or (term_mismatch and not self.bugs.skip_log_consistency)
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
                    del self.log[self._phys(index):]
                    self.log_version += 1
                else:
                    continue
            self.log.append(entry)
            self.log_version += 1
        match = msg.prev_index + len(msg.entries)

        # Advance commit index; the max() guards against a stale, reordered
        # AppendEntries lowering it. The BUG drops that guard, so a reordered message
        # with a smaller leader_commit lowers commit_index (CommitIndexMonotonic).
        if self.bugs.allow_commit_regression:
            self.commit_index = min(msg.leader_commit, match)
        elif msg.leader_commit > self.commit_index:
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

    # -- InstallSnapshot RPC (section 7, receiver implementation) --------------

    def _on_install_snapshot(self, msg: InstallSnapshot) -> None:
        if msg.term < self.term:
            self._send(self.id, msg.leader, AppendReply(self.term, self.id, False, 0))
            return
        if msg.term > self.term or self.role != FOLLOWER:
            self._become_follower(msg.term)
        self.leader_id = msg.leader
        self._arm_election_timer()
        if msg.last_index <= self.base_index:
            # we already cover this snapshot; just acknowledge our progress
            self._send(self.id, msg.leader,
                       AppendReply(self.term, self.id, True, self.last_log_index()))
            return
        # Install the image, keeping a consistent log tail beyond last_index if we have one
        # (otherwise discard the whole log). Uses the OLD base_index for the truncation.
        keep_tail = (self.last_log_index() >= msg.last_index
                     and self.term_at(msg.last_index) == msg.last_term)
        if keep_tail:
            del self.log[: msg.last_index - self.base_index]
        else:
            self.log = []
        self.kv = KVStateMachine()
        self.kv.restore(msg.store, msg.sessions)
        self.snapshot = Snapshot(msg.last_index, msg.last_term, dict(msg.store), dict(msg.sessions))
        self.base_index = msg.last_index
        self.base_term = msg.last_term
        self.commit_index = msg.last_index
        self.last_applied = msg.last_index
        self.applied = []
        self.log_version += 1
        self._record("installsnap", f"n{self.id}|index={msg.last_index}|term={msg.last_term}")
        self._send(self.id, msg.leader, AppendReply(self.term, self.id, True, msg.last_index))

    # -- replication ----------------------------------------------------------

    def _send_append_entries(self, peer: int) -> None:
        prev = self.next_index[peer] - 1
        if prev < self.base_index:
            self._send_install_snapshot(peer)  # follower is behind our compaction point
            return
        entries = self.log_suffix(prev + 1)
        self._send(self.id, peer, AppendEntries(
            self.term, self.id, prev, self.term_at(prev), entries, self.commit_index))

    def _send_install_snapshot(self, peer: int) -> None:
        snap = self.snapshot
        if snap is None:
            return
        self._send(self.id, peer, InstallSnapshot(
            self.term, self.id, snap.last_index, snap.last_term,
            dict(snap.store), dict(snap.sessions)))

    def _broadcast_heartbeats(self) -> None:
        for p in self.peers:
            self._send_append_entries(p)

    def _advance_commit(self) -> None:
        """Commit rule (sections 5.3 / 5.4.2): advance to the highest N replicated on
        a majority, but only if log[N].term == currentTerm. Entries from earlier
        terms commit indirectly, never by counting replicas."""
        for n in range(self.last_log_index(), self.commit_index, -1):
            if self.term_at(n) != self.term and not self.bugs.drop_commit_term_guard:
                break  # older-term entries below: the guard forbids direct commit
            votes = sum(1 for m in self.match_index.values() if m >= n)
            if votes * 2 > self.cluster_size:
                self._set_commit_index(n)
                self._committed_this_term = True  # commit index is now trustworthy
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
        self._maybe_compact()

    def _maybe_compact(self) -> None:
        """Compact the applied prefix into a snapshot once enough entries pile up above the
        base (section 7). Everything at or below last_applied is committed, so it is safe to
        fold into the state-machine image and discard from the log; the uncommitted tail is
        kept. The threshold comes from config (never rng), so snapshotting is deterministic
        and does not perturb the stream when disabled (threshold 0)."""
        threshold = self.config.snapshot_threshold
        if threshold <= 0 or self.last_applied - self.base_index < threshold:
            return
        upto = self.last_applied
        last_term = self.term_at(upto)
        store, sessions = self.kv.capture()
        del self.log[: upto - self.base_index]  # drop the compacted prefix
        self.applied = self.applied[upto - self.base_index:]  # ...and its applied labels
        self.base_index = upto
        self.base_term = last_term
        self.snapshot = Snapshot(upto, last_term, store, sessions)
        self.log_version += 1
        self._record("snapshot", f"n{self.id}|index={upto}|term={last_term}")
