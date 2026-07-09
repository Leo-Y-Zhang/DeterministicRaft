"""A registry of deliberately-injectable algorithm bugs -- the "nemesis for the algorithm".

A verification harness is only as trustworthy as its ability to CATCH real defects. Each
flag here, when enabled, breaks one specific piece of Raft and must be caught by the
property it targets: four trip an internal safety invariant, and one (stale local reads)
slips past every internal invariant but is caught by the linearizability oracle -- the
positive control the oracle was built for. This turns "our checker works" from an
assertion into a reproducible demonstration, and gives the schedule shrinker (next step)
genuine failures to minimise.

ALL flags default to False. With the default ``NO_BUGS`` the system behaves exactly as
before -- byte-identical digests -- so the harness is invisible until deliberately armed.
"""

from __future__ import annotations

from dataclasses import dataclass, fields


@dataclass(frozen=True)
class Bugs:
    # 5.4.2 / Figure 8: commit prior-term entries directly by replica count (unsafe) ->
    # StateMachineSafety / LeaderCompleteness
    drop_commit_term_guard: bool = False
    # 5.4.1 election restriction: grant a vote to a candidate whose log is behind ours ->
    # a stale leader loses committed entries -> LeaderCompleteness
    vote_for_stale_candidate: bool = False
    # 5.3 log matching: accept AppendEntries whose prev-term does not match -> LogMatching
    skip_log_consistency: bool = False
    # drop the commit-index monotonicity guard -> a reordered AppendEntries lowers it ->
    # CommitIndexMonotonic
    allow_commit_regression: bool = False
    # answer reads from the leader's local state without going through the log; a stale
    # (e.g. partitioned) leader then returns old data -> caught by the linearizability
    # oracle, NOT by any internal invariant
    stale_local_reads: bool = False

    @property
    def any_enabled(self) -> bool:
        return any(getattr(self, f.name) for f in fields(self))

    @classmethod
    def names(cls) -> list[str]:
        return [f.name for f in fields(cls)]


NO_BUGS = Bugs()
