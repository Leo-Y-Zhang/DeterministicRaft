"""The automatic schedule shrinker (raftverified/shrink.py, co-crown B).

ddmin is tested in isolation with synthetic predicates (so a shrinker bug can't hide
behind a real run), then end-to-end: a real injected-bug failure is delta-debugged to a
smaller reproduction that still trips the SAME failure, deterministically.
"""

from raftverified.bugs import Bugs
from raftverified.cluster import Cluster
from raftverified.nemesis import (
    CrashNode,
    IsolateLeader,
    NemesisSchedule,
    PartitionHalves,
)
from raftverified.shrink import Counterexample, Scenario, ddmin, failure_signature, shrink


class TestDdmin:
    def test_finds_the_single_planted_pair(self):
        # predicate reproduces iff BOTH 137 and 402 are kept
        calls = [0]

        def reproduces(kept):
            calls[0] += 1
            return 137 in kept and 402 in kept

        assert sorted(ddmin(range(500), reproduces)) == [137, 402]
        assert calls[0] < 500  # far fewer than a linear scan

    def test_single_required_element(self):
        assert ddmin(range(64), lambda kept: 7 in kept) == [7]

    def test_all_elements_required(self):
        need = {3, 8, 15}
        assert set(ddmin(range(30), lambda kept: need <= set(kept))) == need

    def test_is_one_minimal(self):
        need = {10, 20, 30}
        minimal = ddmin(range(50), lambda kept: need <= set(kept))
        # removing any single kept element must break reproduction
        for e in minimal:
            assert not (need <= (set(minimal) - {e}))

    def test_empty_when_nothing_required(self):
        assert ddmin(range(20), lambda kept: True) == []


# A fast, reliable injected failure to shrink.
BUG_SCENARIO = Scenario(nodes=5, seed=0, faults="chaos", steps=4000,
                        bugs=Bugs(vote_for_stale_candidate=True))


class TestShrinkRealFailure:
    def test_shrinks_and_reproduces_the_same_failure(self):
        ce = shrink(BUG_SCENARIO)
        assert isinstance(ce, Counterexample)
        assert ce.signature == "LeaderCompleteness"
        # the reduced scenario really does reproduce the same failure
        assert failure_signature(ce.scenario) == ce.signature

    def test_reduction_does_not_grow_the_problem(self):
        ce = shrink(BUG_SCENARIO)
        assert ce.scenario.steps <= BUG_SCENARIO.steps
        assert ce.injection_count <= ce.original_fault_count
        # at least one dimension was actually reduced
        assert ce.scenario.steps < BUG_SCENARIO.steps or ce.scenario.suppressed

    def test_is_deterministic(self):
        a = shrink(BUG_SCENARIO)
        b = shrink(BUG_SCENARIO)
        assert a.scenario == b.scenario
        assert a.injection_count == b.injection_count

    def test_reshrinking_is_stable(self):
        ce = shrink(BUG_SCENARIO)
        again = shrink(ce.scenario)
        assert again is not None
        assert again.signature == ce.signature
        assert again.scenario.steps <= ce.scenario.steps

    def test_summary_is_informative(self):
        ce = shrink(BUG_SCENARIO)
        text = ce.summary()
        assert "LeaderCompleteness" in text and "faults" in text and "steps" in text


class TestShrinkOracleFailure:
    def test_shrinks_a_nonlinearizable_stale_read_failure(self):
        sc = Scenario(nodes=3, seed=0, faults="chaos", steps=6000,
                      bugs=Bugs(stale_local_reads=True))
        ce = shrink(sc)
        assert ce is not None
        assert ce.signature == "nonlinearizable"
        assert failure_signature(ce.scenario) == "nonlinearizable"
        assert ce.scenario.steps <= 6000


class TestShrinkHistoricalMembershipBug:
    def test_shrinks_the_may_2015_membership_failure(self):
        # the pinned natural repro of the dissertation-era single-server membership bug
        # (see tests/test_membership.py); the counterexample stays deep -- ddmin proves
        # nearly every fault in the schedule is load-bearing -- but it is 1-minimal,
        # step-trimmed, and replayable verbatim
        sc = Scenario(nodes=6, seed=354, faults="chaos", steps=6000, membership=True,
                      bugs=Bugs(drop_config_commit_guard=True))
        ce = shrink(sc)
        assert ce is not None
        assert ce.signature == "LeaderCompleteness"
        assert failure_signature(ce.scenario) == "LeaderCompleteness"  # still reproduces
        assert ce.scenario.steps <= sc.steps
        assert ce.scenario.replay_command().endswith("--membership")


# A hand-authored nemesis campaign over the "light" profile: light injects message noise
# (drops, dups, jitter) but NEVER partitions or crashes on its own, so every partition,
# isolation and crash in the run below is the schedule's doing. Seed 0 trips
# vote_for_stale_candidate against this campaign (bounded search in test_nemesis.py);
# the violation fires at step 1506, so 2500 steps is a comfortable budget.
NEMESIS_CAMPAIGN = NemesisSchedule((
    PartitionHalves(at=800, duration=900),
    IsolateLeader(at=2200, duration=900),
    CrashNode(node=1, at=3600, duration=600),
    PartitionHalves(at=4600, duration=900),
    IsolateLeader(at=6000, duration=900),
    PartitionHalves(at=7400, duration=900),
))
NEMESIS_SCENARIO = Scenario(nodes=5, seed=0, faults="light", steps=2500,
                            bugs=Bugs(vote_for_stale_candidate=True),
                            nemesis=NEMESIS_CAMPAIGN)


class TestShrinkNemesisSchedule:
    """Hand-authored schedules are shrunk by the SAME ddmin machinery: every nemesis
    injection consumes a suppression-mask ordinal (see cluster.py), so a Scenario that
    carries a schedule delta-debugs with no nemesis-specific code paths."""

    def test_shrinks_a_nemesis_driven_failure_to_the_same_signature(self):
        ce = shrink(NEMESIS_SCENARIO)
        assert isinstance(ce, Counterexample)
        assert ce.signature == "LeaderCompleteness"
        assert failure_signature(ce.scenario) == ce.signature  # still reproduces
        # shrinking suppresses injections; the schedule itself is never rewritten
        assert ce.scenario.nemesis == NEMESIS_CAMPAIGN

    def test_reduction_does_not_grow_the_problem(self):
        ce = shrink(NEMESIS_SCENARIO)
        assert ce.scenario.steps <= NEMESIS_SCENARIO.steps
        assert ce.injection_count <= ce.original_fault_count
        # at least one dimension was actually reduced
        assert ce.scenario.steps < NEMESIS_SCENARIO.steps or ce.scenario.suppressed

    def test_is_deterministic(self):
        a = shrink(NEMESIS_SCENARIO)
        b = shrink(NEMESIS_SCENARIO)
        assert a.scenario == b.scenario
        assert a.injection_count == b.injection_count

    def test_replay_command_carries_the_serialized_schedule(self):
        cmd = shrink(NEMESIS_SCENARIO).scenario.replay_command()
        assert "--nemesis" in cmd
        # the embedded serialized form round-trips to the exact schedule
        payload = cmd.split("--nemesis '", 1)[1].rstrip("'")
        assert NemesisSchedule.from_json(payload) == NEMESIS_CAMPAIGN

    def test_plain_scenario_replay_command_has_no_nemesis_flag(self):
        assert "--nemesis" not in BUG_SCENARIO.replay_command()
        assert "--nemesis" not in Scenario(nodes=5, seed=1, faults="chaos", steps=100,
                                           nemesis=NemesisSchedule()).replay_command()


class TestShrinkHealthy:
    def test_healthy_scenario_yields_no_counterexample(self):
        assert shrink(Scenario(nodes=5, seed=1, faults="chaos", steps=4000)) is None

    def test_healthy_membership_scenario_yields_no_counterexample(self):
        # membership threads through the shrinker's per-candidate replays
        assert shrink(Scenario(nodes=5, seed=1, faults="chaos", steps=4000,
                               membership=True)) is None

    def test_membership_scenario_replay_command_carries_the_flag(self):
        sc = Scenario(nodes=5, seed=3, faults="chaos", steps=2000, membership=True)
        assert sc.replay_command().endswith("--membership")
        plain = Scenario(nodes=5, seed=3, faults="chaos", steps=2000)
        assert "--membership" not in plain.replay_command()

    def test_target_mismatch_yields_none(self):
        # a real failure, but asked to shrink a DIFFERENT target
        assert shrink(BUG_SCENARIO, target="LogMatching") is None


class TestMaskDeterminism:
    def test_masked_run_replays_byte_identically(self):
        mask = frozenset({0, 2})
        a = Cluster(num_nodes=5, seed=7, faults="chaos", suppressed=mask).run(4000)
        b = Cluster(num_nodes=5, seed=7, faults="chaos", suppressed=mask).run(4000)
        assert a.digest == b.digest

    def test_empty_mask_is_byte_identical_to_no_mask(self):
        a = Cluster(num_nodes=5, seed=7, faults="chaos").run(4000)
        b = Cluster(num_nodes=5, seed=7, faults="chaos", suppressed=frozenset()).run(4000)
        assert a.digest == b.digest
