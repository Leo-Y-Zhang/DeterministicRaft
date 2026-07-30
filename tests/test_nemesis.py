"""The nemesis vocabulary: declarative, replayable fault schedules (harmonia/nemesis.py).

The random fault driver explores; a nemesis DIRECTS. These tests pin the vocabulary's
whole contract: patterns are validated pure data; a schedule serializes to JSON and
round-trips exactly; a scheduled run is byte-identical replayable from that serialized
form; every injection routes through the SAME suppression-mask ordinals the shrinker
ddmins over; and the injectable bug registry is still caught when the faults are
nemesis-driven instead of random (the registry tests live at the bottom).
"""

import json

import pytest
from _goldens import digest_for, key
from _goldens import load as load_goldens

from harmonia.cluster import Cluster
from harmonia.nemesis import (
    CrashNode,
    FlappingLink,
    IsolateLeader,
    LossyLink,
    NemesisSchedule,
    PartitionHalves,
)

# A schedule using every pattern once (no overlaps, no leader-dependence ambiguity).
FULL_SCHEDULE = NemesisSchedule((
    PartitionHalves(at=300, duration=400),
    CrashNode(node=2, at=900, duration=300),
    FlappingLink(a=0, b=1, at=1400, period=100, cycles=3),
    LossyLink(a=0, b=2, at=2200, duration=500, drop_p=0.5),
    IsolateLeader(at=3000, duration=400),
))


class TestVocabularyValidation:
    def test_negative_start_time_rejected(self):
        with pytest.raises(ValueError, match="at"):
            PartitionHalves(at=-1, duration=100)

    def test_zero_duration_rejected(self):
        with pytest.raises(ValueError, match="duration"):
            IsolateLeader(at=0, duration=0)

    def test_lossy_probability_bounds(self):
        LossyLink(a=0, b=1, at=0, duration=100, drop_p=1.0)  # total loss is legal
        with pytest.raises(ValueError, match="drop_p"):
            LossyLink(a=0, b=1, at=0, duration=100, drop_p=0.0)
        with pytest.raises(ValueError, match="drop_p"):
            LossyLink(a=0, b=1, at=0, duration=100, drop_p=1.5)

    def test_link_endpoints_must_differ(self):
        with pytest.raises(ValueError, match="distinct"):
            FlappingLink(a=1, b=1, at=0, period=100, cycles=2)
        with pytest.raises(ValueError, match="distinct"):
            LossyLink(a=2, b=2, at=0, duration=100, drop_p=0.5)

    def test_negative_node_ids_rejected(self):
        with pytest.raises(ValueError, match="node"):
            CrashNode(node=-1, at=0, duration=100)
        with pytest.raises(ValueError, match="node"):
            LossyLink(a=-1, b=1, at=0, duration=100, drop_p=0.5)

    def test_flapping_needs_at_least_one_cycle(self):
        with pytest.raises(ValueError, match="cycles"):
            FlappingLink(a=0, b=1, at=0, period=100, cycles=0)
        with pytest.raises(ValueError, match="period"):
            FlappingLink(a=0, b=1, at=0, period=0, cycles=2)


class TestScheduleExpansion:
    def test_injections_sorted_by_time(self):
        sched = NemesisSchedule((
            CrashNode(node=0, at=900, duration=100),
            PartitionHalves(at=200, duration=100),
        ))
        assert [inj.at for inj in sched.injections()] == [200, 900]

    def test_flapping_expands_to_one_injection_per_flap(self):
        sched = NemesisSchedule((FlappingLink(a=0, b=1, at=500, period=100, cycles=3),))
        assert [inj.at for inj in sched.injections()] == [500, 700, 900]
        assert all(isinstance(inj.op, FlappingLink) for inj in sched.injections())

    def test_same_instant_keeps_declaration_order(self):
        first = CrashNode(node=1, at=400, duration=100)
        second = PartitionHalves(at=400, duration=100)
        sched = NemesisSchedule((first, second))
        assert [inj.op for inj in sched.injections()] == [first, second]

    def test_empty_schedule_has_no_injections(self):
        assert NemesisSchedule().injections() == []


class TestSerialization:
    def test_round_trip_every_pattern_exactly(self):
        assert NemesisSchedule.from_json(FULL_SCHEDULE.to_json()) == FULL_SCHEDULE

    def test_json_is_plain_declarative_data(self):
        data = json.loads(FULL_SCHEDULE.to_json())
        assert isinstance(data, list) and len(data) == 5
        assert [d["pattern"] for d in data] == [
            "partition_halves", "crash_node", "flapping_link", "lossy_link",
            "isolate_leader",
        ]

    def test_to_json_is_stable(self):
        assert FULL_SCHEDULE.to_json() == FULL_SCHEDULE.to_json()

    def test_unknown_pattern_rejected(self):
        with pytest.raises(ValueError, match="unknown pattern"):
            NemesisSchedule.from_json('[{"pattern": "meteor_strike", "at": 0}]')

    def test_bad_json_rejected(self):
        with pytest.raises(ValueError, match="not valid JSON"):
            NemesisSchedule.from_json("{nope")

    def test_non_list_rejected(self):
        with pytest.raises(ValueError, match="list"):
            NemesisSchedule.from_json('{"pattern": "isolate_leader"}')

    def test_missing_field_rejected(self):
        with pytest.raises(ValueError, match="isolate_leader"):
            NemesisSchedule.from_json('[{"pattern": "isolate_leader", "at": 5}]')

    def test_unknown_field_rejected(self):
        with pytest.raises(ValueError, match="crash_node"):
            NemesisSchedule.from_json(
                '[{"pattern": "crash_node", "at": 5, "duration": 10, "node": 1, "x": 2}]'
            )


class TestPatternSemantics:
    """Each pattern does what its name says, at exactly its declared virtual times."""

    def test_partition_halves_forms_and_heals(self):
        sched = NemesisSchedule((PartitionHalves(at=500, duration=1000),))
        c = Cluster(num_nodes=3, seed=1, faults="none", nemesis=sched)
        assert c.run_until(lambda cl: cl.sim.now > 600)
        assert c.net.is_partitioned()
        assert not c.net.reachable(0, 2) and c.net.reachable(0, 1)  # low half {0,1}
        assert c.run_until(lambda cl: cl.sim.now > 1600)
        assert not c.net.is_partitioned()
        assert [t for t, k, _ in c.events if k == "partition"] == [500]
        assert [t for t, k, _ in c.events if k == "heal"] == [1500]

    def test_isolate_leader_cuts_off_the_believed_leader(self):
        sched = NemesisSchedule((IsolateLeader(at=1500, duration=1500),))
        c = Cluster(num_nodes=3, seed=1, faults="none", nemesis=sched)
        assert c.run_until(lambda cl: cl.sim.now >= 1400)
        old = c.leader()
        assert old is not None  # a quiet 3-node cluster elects well before 1400ms
        assert c.run_until(lambda cl: cl.sim.now > 1600)
        others = [i for i in c.nodes if i != old.id]
        assert all(not c.net.reachable(old.id, i) for i in others)
        # the majority side elects a replacement while the old leader is cut off
        assert c.run_until(
            lambda cl: any(cl.nodes[i].role == "leader" for i in others), 30_000
        )

    def test_isolate_leader_before_any_election_skips_without_an_ordinal(self):
        sched = NemesisSchedule((IsolateLeader(at=1, duration=500),))
        c = Cluster(num_nodes=3, seed=1, faults="none", nemesis=sched)
        c.run(2000)
        assert c.fault_count == 0  # nothing to isolate -> no ordinal consumed
        assert not c.net.is_partitioned()

    def test_flapping_link_cycles_exactly(self):
        sched = NemesisSchedule((FlappingLink(a=0, b=1, at=300, period=100, cycles=3),))
        c = Cluster(num_nodes=3, seed=2, faults="none", nemesis=sched)
        c.run_until(lambda cl: cl.sim.now > 1000)
        assert [t for t, k, _ in c.events if k == "linkdown"] == [300, 500, 700]
        assert [t for t, k, _ in c.events if k == "linkup"] == [400, 600, 800]

    def test_lossy_link_drops_only_during_its_window(self):
        # the link must carry traffic: with seed 3 the leader sits on one end of (0, 2)
        sched = NemesisSchedule((LossyLink(a=0, b=2, at=500, duration=1500, drop_p=1.0),))
        c = Cluster(num_nodes=3, seed=3, faults="none", nemesis=sched)
        c.run_until(lambda cl: cl.sim.now > 3000)
        lossy_drops = [line for line in c.trace if "|drop|" in line and "lossy-link" in line]
        assert lossy_drops, "a p=1.0 lossy link must drop traffic"
        # every lossy drop happened inside the window; sends outside it are untouched
        for line in lossy_drops:
            t = int(line.split("|", 1)[0])
            assert 500 <= t < 2000
        # deliveries across the pair resume after the window heals
        assert any(
            ("|deliver|n0->n2|" in line or "|deliver|n2->n0|" in line)
            and int(line.split("|", 1)[0]) > 2000
            for line in c.trace
        )

    def test_crash_node_pauses_then_resumes(self):
        sched = NemesisSchedule((CrashNode(node=2, at=400, duration=300),))
        c = Cluster(num_nodes=3, seed=1, faults="none", nemesis=sched)
        c.run_until(lambda cl: cl.sim.now > 450)
        assert not c.nodes[2].alive
        c.run_until(lambda cl: cl.sim.now > 750)
        assert c.nodes[2].alive
        assert [t for t, k, d in c.events if k == "crash"] == [400]
        assert [t for t, k, d in c.events if k == "resume"] == [700]

    def test_crashing_an_already_crashed_node_is_skipped(self):
        sched = NemesisSchedule((
            CrashNode(node=2, at=400, duration=1000),
            CrashNode(node=2, at=600, duration=1000),  # overlaps: node 2 already down
        ))
        c = Cluster(num_nodes=3, seed=1, faults="none", nemesis=sched)
        c.run_until(lambda cl: cl.sim.now > 2000)
        assert c.fault_count == 1  # the overlap consumed no ordinal
        assert c.stats["crashes"] == 1 and c.stats["resumes"] == 1

    def test_schedule_referencing_a_missing_node_is_rejected(self):
        sched = NemesisSchedule((CrashNode(node=7, at=100, duration=100),))
        with pytest.raises(ValueError, match="n7"):
            Cluster(num_nodes=3, seed=0, faults="none", nemesis=sched)


# Nemesis-driven configs: own pinned golden digests (default goldens stay untouched).
NEMESIS_GOLDENS = {
    (3, 2, "none", 2500): "c9c256d22d80df28540acf01eea1607fc1b98c63fc0e5baf2e787bdf8e0546ef",
    (5, 7, "chaos", 3000): "fecb070df42c72e092d93643e2167e372ecbd3e52b9952bed74b9f5216550772",
}


class TestDeterminismAndReplay:
    def test_same_schedule_same_seed_is_byte_identical(self):
        a = Cluster(num_nodes=5, seed=11, faults="none", nemesis=FULL_SCHEDULE).run(3000)
        b = Cluster(num_nodes=5, seed=11, faults="none", nemesis=FULL_SCHEDULE).run(3000)
        assert a.digest == b.digest

    def test_replay_from_the_serialized_form_is_exact(self):
        revived = NemesisSchedule.from_json(FULL_SCHEDULE.to_json())
        a = Cluster(num_nodes=5, seed=11, faults="none", nemesis=FULL_SCHEDULE).run(3000)
        b = Cluster(num_nodes=5, seed=11, faults="none", nemesis=revived).run(3000)
        assert a.digest == b.digest

    def test_nemesis_composes_with_a_random_fault_profile(self):
        # a hand-authored schedule layered ON TOP of chaos, still byte-identical
        a = Cluster(num_nodes=5, seed=7, faults="chaos", nemesis=FULL_SCHEDULE).run(3000)
        b = Cluster(num_nodes=5, seed=7, faults="chaos", nemesis=FULL_SCHEDULE).run(3000)
        assert a.digest == b.digest
        assert a.stats["partitions"] >= 1

    def test_empty_schedule_is_byte_identical_to_no_nemesis(self):
        plain = Cluster(num_nodes=5, seed=7, faults="chaos").run(3000)
        empty = Cluster(num_nodes=5, seed=7, faults="chaos",
                        nemesis=NemesisSchedule()).run(3000)
        assert plain.digest == empty.digest

    def test_no_nemesis_leaves_default_goldens_untouched(self):
        """With the nemesis machinery present but unused, the pinned default corpus is
        BYTE-IDENTICAL (the whole corpus is re-pinned by test_goldens.py; this spot-checks
        the tie explicitly, as test_membership.py does for membership)."""
        goldens = load_goldens()
        for cfg in [(3, 1, "none", 2000), (5, 7, "chaos", 3000)]:
            assert digest_for(cfg) == goldens[key(cfg)]["digest"]

    @pytest.mark.parametrize("cfg", list(NEMESIS_GOLDENS))
    def test_nemesis_golden_digests_pinned(self, cfg):
        nodes, seed, faults, steps = cfg
        digest = Cluster(num_nodes=nodes, seed=seed, faults=faults,
                         nemesis=FULL_SCHEDULE).run(steps).digest
        assert digest == NEMESIS_GOLDENS[cfg]


class TestSuppressionMaskIntegration:
    """Nemesis injections use the SAME ordinal/suppression mechanism as the random fault
    driver, so the ddmin shrinker can minimise hand-authored schedules unchanged."""

    def test_each_injection_consumes_exactly_one_ordinal(self):
        c = Cluster(num_nodes=5, seed=11, faults="none", nemesis=FULL_SCHEDULE)
        c.run(4000)
        # 1 partition + 1 crash + 3 flaps + 1 lossy window + 1 isolate-leader = 7
        assert c.fault_count == 7

    def test_suppressing_every_ordinal_disarms_the_whole_schedule(self):
        c = Cluster(num_nodes=5, seed=11, faults="none", nemesis=FULL_SCHEDULE,
                    suppressed=frozenset(range(7)))
        c.run(4000)
        assert c.stats["partitions"] == 0 and c.stats["crashes"] == 0
        assert not [1 for _, k, _ in c.events if k in ("linkdown", "lossy")]

    def test_suppressing_one_flap_removes_exactly_one_downtime(self):
        sched = NemesisSchedule((FlappingLink(a=0, b=1, at=300, period=100, cycles=3),))
        full = Cluster(num_nodes=3, seed=2, faults="none", nemesis=sched)
        full.run(2000)
        masked = Cluster(num_nodes=3, seed=2, faults="none", nemesis=sched,
                         suppressed=frozenset({1}))
        masked.run(2000)
        assert [t for t, k, _ in full.events if k == "linkdown"] == [300, 500, 700]
        assert [t for t, k, _ in masked.events if k == "linkdown"] == [300, 700]

    def test_masked_nemesis_run_replays_byte_identically(self):
        mask = frozenset({0, 3})
        a = Cluster(num_nodes=5, seed=11, faults="none", nemesis=FULL_SCHEDULE,
                    suppressed=mask).run(3000)
        b = Cluster(num_nodes=5, seed=11, faults="none", nemesis=FULL_SCHEDULE,
                    suppressed=mask).run(3000)
        assert a.digest == b.digest
