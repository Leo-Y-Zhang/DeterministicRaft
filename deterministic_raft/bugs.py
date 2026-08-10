"""A registry of deliberately-injectable algorithm bugs -- the "nemesis for the algorithm".

A verification harness is only as trustworthy as its ability to CATCH real defects. Each
flag here, when enabled, breaks one specific piece of Raft and must be caught by the
property it targets: five trip an internal safety invariant, and one (stale local reads)
slips past every internal invariant but is caught by the linearizability oracle -- the
positive control the oracle was built for. One of the six is not invented at all:
drop_config_commit_guard resurrects the single-server membership algorithm exactly as
published in the dissertation, before Ongaro's May-2015 raft-dev correction. This turns
"our checker works" from an assertion into a reproducible demonstration, and gives the
schedule shrinker genuine failures to minimise.

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
    # THE HISTORICAL ONE (Ongaro, raft-dev, May 2015): the dissertation's original
    # single-server membership algorithm let a fresh leader append a configuration change
    # before committing anything in its own term. Two leaders elected under the same base
    # configuration can then each install a different single-server change whose majorities
    # do not overlap, and one of them overwrites the other's COMMITTED entries. The
    # amendment (a leader must first commit an entry in its current term) fixes it; this
    # flag drops that guard, resurrecting the published algorithm as it stood 2014-2015 ->
    # LeaderCompleteness
    drop_config_commit_guard: bool = False

    @property
    def any_enabled(self) -> bool:
        return any(getattr(self, f.name) for f in fields(self))

    @classmethod
    def names(cls) -> list[str]:
        return [f.name for f in fields(cls)]


NO_BUGS = Bugs()
