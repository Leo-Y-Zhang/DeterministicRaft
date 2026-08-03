"""Unit tests for the RaftNode RPC handlers, driven directly with constructed messages."""

from raftverified.node import (
    CANDIDATE,
    FOLLOWER,
    LEADER,
    AppendEntries,
    AppendReply,
    Entry,
    RaftConfig,
    RaftNode,
    RequestVote,
    VoteReply,
)
from raftverified.sim import Simulator


def make_node(node_id=0, n=3, term=0, log=(), seed=1):
    sim = Simulator(seed)
    sent = []
    node = RaftNode(node_id, [p for p in range(n) if p != node_id], sim,
                    send=lambda src, dst, msg: sent.append((dst, msg)),
                    record=lambda kind, detail: None,
                    config=RaftConfig())
    node.term = term
    node.log = [Entry(*e) for e in log]
    return node, sent, sim


def replies_of(sent, cls):
    return [m for _, m in sent if isinstance(m, cls)]


class TestRequestVote:
    def test_rejects_lower_term(self):
        node, sent, _ = make_node(term=5)
        node.handle(1, RequestVote(term=3, candidate=1, last_log_index=0, last_log_term=0))
        (reply,) = replies_of(sent, VoteReply)
        assert reply.granted is False and reply.term == 5

    def test_grants_first_vote(self):
        node, sent, _ = make_node(term=1)
        node.handle(1, RequestVote(term=1, candidate=1, last_log_index=0, last_log_term=0))
        (reply,) = replies_of(sent, VoteReply)
        assert reply.granted is True
        assert node.voted_for == 1

    def test_refuses_second_candidate_same_term(self):
        node, sent, _ = make_node(term=1)
        node.handle(1, RequestVote(term=1, candidate=1, last_log_index=0, last_log_term=0))
        node.handle(2, RequestVote(term=1, candidate=2, last_log_index=0, last_log_term=0))
        replies = replies_of(sent, VoteReply)
        assert [r.granted for r in replies] == [True, False]

    def test_regrants_same_candidate_idempotently(self):
        node, sent, _ = make_node(term=1)
        msg = RequestVote(term=1, candidate=1, last_log_index=0, last_log_term=0)
        node.handle(1, msg)
        node.handle(1, msg)  # duplicated message
        assert [r.granted for r in replies_of(sent, VoteReply)] == [True, True]
        assert node.voted_for == 1

    def test_higher_term_resets_vote_and_steps_down(self):
        node, _, _ = make_node(term=2)
        node.role = LEADER
        node.voted_for = 0
        node.handle(1, RequestVote(term=3, candidate=1, last_log_index=0, last_log_term=0))
        assert node.role == FOLLOWER and node.term == 3 and node.voted_for == 1

    def test_rejects_candidate_with_stale_last_term(self):
        node, sent, _ = make_node(term=2, log=[(1, "a"), (2, "b")])
        node.handle(1, RequestVote(term=2, candidate=1, last_log_index=5, last_log_term=1))
        (reply,) = replies_of(sent, VoteReply)
        assert reply.granted is False  # longer log loses to higher last term

    def test_rejects_candidate_with_shorter_log_same_term(self):
        node, sent, _ = make_node(term=2, log=[(1, "a"), (1, "b")])
        node.handle(1, RequestVote(term=2, candidate=1, last_log_index=1, last_log_term=1))
        (reply,) = replies_of(sent, VoteReply)
        assert reply.granted is False

    def test_grants_candidate_with_equal_log(self):
        node, sent, _ = make_node(term=2, log=[(1, "a"), (1, "b")])
        node.handle(1, RequestVote(term=2, candidate=1, last_log_index=2, last_log_term=1))
        (reply,) = replies_of(sent, VoteReply)
        assert reply.granted is True


class TestAppendEntries:
    def test_rejects_lower_term(self):
        node, sent, _ = make_node(term=5)
        node.handle(1, AppendEntries(term=4, leader=1, prev_index=0, prev_term=0,
                                     entries=(), leader_commit=0))
        (reply,) = replies_of(sent, AppendReply)
        assert reply.success is False and reply.term == 5

    def test_same_term_makes_candidate_follower(self):
        node, _, _ = make_node(term=3)
        node.role = CANDIDATE
        node.handle(1, AppendEntries(term=3, leader=1, prev_index=0, prev_term=0,
                                     entries=(), leader_commit=0))
        assert node.role == FOLLOWER and node.leader_id == 1

    def test_rejects_when_prev_index_beyond_log(self):
        node, sent, _ = make_node(term=1, log=[(1, "a")])
        node.handle(1, AppendEntries(term=1, leader=1, prev_index=3, prev_term=1,
                                     entries=(Entry(1, "d"),), leader_commit=0))
        (reply,) = replies_of(sent, AppendReply)
        assert reply.success is False

    def test_rejects_when_prev_term_mismatch(self):
        node, sent, _ = make_node(term=3, log=[(1, "a"), (2, "b")])
        node.handle(1, AppendEntries(term=3, leader=1, prev_index=2, prev_term=3,
                                     entries=(), leader_commit=0))
        (reply,) = replies_of(sent, AppendReply)
        assert reply.success is False

    def test_appends_at_matching_prev(self):
        node, sent, _ = make_node(term=1, log=[(1, "a")])
        node.handle(1, AppendEntries(term=1, leader=1, prev_index=1, prev_term=1,
                                     entries=(Entry(1, "b"), Entry(1, "c")), leader_commit=0))
        (reply,) = replies_of(sent, AppendReply)
        assert reply.success is True and reply.match_index == 3
        assert [e.command for e in node.log] == ["a", "b", "c"]

    def test_truncates_conflicting_suffix(self):
        node, _, _ = make_node(term=3, log=[(1, "a"), (2, "x"), (2, "y")])
        node.handle(1, AppendEntries(term=3, leader=1, prev_index=1, prev_term=1,
                                     entries=(Entry(3, "b"),), leader_commit=0))
        assert [(e.term, e.command) for e in node.log] == [(1, "a"), (3, "b")]

    def test_duplicate_append_is_idempotent(self):
        node, _, _ = make_node(term=1)
        msg = AppendEntries(term=1, leader=1, prev_index=0, prev_term=0,
                            entries=(Entry(1, "a"), Entry(1, "b")), leader_commit=0)
        node.handle(1, msg)
        version = node.log_version
        node.handle(1, msg)
        assert [e.command for e in node.log] == ["a", "b"]
        assert node.log_version == version  # no mutation on the duplicate

    def test_stale_shorter_append_does_not_truncate(self):
        node, _, _ = make_node(term=1, log=[(1, "a"), (1, "b"), (1, "c")])
        node.handle(1, AppendEntries(term=1, leader=1, prev_index=0, prev_term=0,
                                     entries=(Entry(1, "a"),), leader_commit=0))
        assert [e.command for e in node.log] == ["a", "b", "c"]

    def test_commit_follows_leader_commit_capped_at_last_new_entry(self):
        node, _, _ = make_node(term=1)
        node.handle(1, AppendEntries(term=1, leader=1, prev_index=0, prev_term=0,
                                     entries=(Entry(1, "a"),), leader_commit=99))
        assert node.commit_index == 1  # min(99, index of last new entry)

    def test_stale_append_cannot_lower_commit_index(self):
        node, _, _ = make_node(term=1, log=[(1, "a"), (1, "b"), (1, "c")])
        node.commit_index = 3
        node.last_applied = 3
        node.applied = ["a", "b", "c"]
        node.handle(1, AppendEntries(term=1, leader=1, prev_index=0, prev_term=0,
                                     entries=(Entry(1, "a"),), leader_commit=99))
        assert node.commit_index == 3

    def test_applies_committed_entries_in_order(self):
        node, _, _ = make_node(term=1)
        node.handle(1, AppendEntries(term=1, leader=1, prev_index=0, prev_term=0,
                                     entries=(Entry(1, "a"), Entry(1, "b")), leader_commit=2))
        assert node.applied == ["a", "b"] and node.last_applied == 2


class TestLeaderBehaviour:
    def make_leader(self, n=3, log=()):
        node, sent, sim = make_node(node_id=0, n=n, term=2, log=log)
        node._become_leader()
        sent.clear()
        return node, sent, sim

    def test_client_command_appends_to_own_log(self):
        node, _, _ = self.make_leader()
        assert node.client_command("x") is True
        assert node.log[-1] == Entry(2, "x")

    def test_client_command_rejected_by_follower(self):
        node, _, _ = make_node()
        assert node.client_command("x") is False
        assert node.log == []

    def test_success_reply_advances_match_and_next(self):
        node, _, _ = self.make_leader(log=[(2, "a"), (2, "b")])
        node.handle(1, AppendReply(term=2, follower=1, success=True, match_index=2))
        assert node.match_index[1] == 2 and node.next_index[1] == 3

    def test_stale_success_reply_never_regresses_progress(self):
        node, _, _ = self.make_leader(log=[(2, "a"), (2, "b")])
        node.handle(1, AppendReply(term=2, follower=1, success=True, match_index=2))
        node.handle(1, AppendReply(term=2, follower=1, success=True, match_index=1))
        assert node.match_index[1] == 2 and node.next_index[1] == 3

    def test_failure_reply_backs_off_next_index_and_retries(self):
        node, sent, _ = self.make_leader(log=[(1, "a"), (2, "b")])
        node.next_index[1] = 3
        node.handle(1, AppendReply(term=2, follower=1, success=False, match_index=0))
        assert node.next_index[1] == 2
        retry = [m for dst, m in sent if dst == 1 and isinstance(m, AppendEntries)]
        assert retry and retry[-1].prev_index == 1

    def test_commit_advances_on_majority_current_term(self):
        node, _, _ = self.make_leader(log=[(2, "a")])
        node.handle(1, AppendReply(term=2, follower=1, success=True, match_index=1))
        assert node.commit_index == 1  # self + n1 = 2 of 3

    def test_current_term_guard_blocks_prior_term_commit(self):
        # Figure 8 scenario: an old-term entry on a majority must NOT commit directly.
        node, _, _ = self.make_leader(log=[(1, "old")])
        node.handle(1, AppendReply(term=2, follower=1, success=True, match_index=1))
        node.handle(2, AppendReply(term=2, follower=2, success=True, match_index=1))
        assert node.commit_index == 0

    def test_prior_term_entry_commits_with_current_term_entry(self):
        node, _, _ = self.make_leader(log=[(1, "old")])
        node.client_command("new")  # term-2 entry at index 2
        node.handle(1, AppendReply(term=2, follower=1, success=True, match_index=2))
        assert node.commit_index == 2  # both entries commit together

    def test_steps_down_on_higher_term_reply(self):
        node, _, _ = self.make_leader()
        node.handle(1, AppendReply(term=9, follower=1, success=False, match_index=0))
        assert node.role == FOLLOWER and node.term == 9

    def test_ignores_reply_from_older_term(self):
        node, _, _ = self.make_leader(log=[(2, "a")])
        node.handle(1, AppendReply(term=1, follower=1, success=True, match_index=1))
        assert node.match_index[1] == 0 and node.commit_index == 0

    def test_paused_node_ignores_messages(self):
        node, sent, _ = make_node(term=1)
        node.pause()
        node.handle(1, RequestVote(term=2, candidate=1, last_log_index=0, last_log_term=0))
        assert sent == [] and node.term == 1
