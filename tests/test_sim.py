"""Tests for the discrete-event simulator and the faulty network."""

import pytest

from raftverified.sim import PROFILES, Network, Simulator


def make_net(seed=1, profile="none", n=3):
    sim = Simulator(seed)
    inbox = []
    log = []
    net = Network(sim, list(range(n)), PROFILES[profile],
                  deliver=lambda src, dst, msg: inbox.append((src, dst, msg)),
                  record=lambda kind, detail: log.append((kind, detail)))
    return sim, net, inbox, log


def drain(sim):
    while sim.step():
        pass


class TestSimulator:
    def test_events_execute_in_time_order(self):
        sim = Simulator(0)
        order = []
        sim.schedule(30, lambda: order.append("c"))
        sim.schedule(10, lambda: order.append("a"))
        sim.schedule(20, lambda: order.append("b"))
        drain(sim)
        assert order == ["a", "b", "c"]

    def test_same_time_events_execute_in_schedule_order(self):
        sim = Simulator(0)
        order = []
        for name in "abcde":
            sim.schedule(5, lambda name=name: order.append(name))
        drain(sim)
        assert order == list("abcde")

    def test_negative_delay_raises(self):
        sim = Simulator(0)
        with pytest.raises(ValueError):
            sim.schedule(-1, lambda: None)

    def test_step_returns_false_when_empty(self):
        assert Simulator(0).step() is False

    def test_now_advances_to_event_time(self):
        sim = Simulator(0)
        sim.schedule(42, lambda: None)
        sim.step()
        assert sim.now == 42

    def test_steps_counter_increments(self):
        sim = Simulator(0)
        sim.schedule(1, lambda: None)
        sim.schedule(2, lambda: None)
        drain(sim)
        assert sim.steps == 2

    def test_nested_scheduling_uses_current_time(self):
        sim = Simulator(0)
        times = []
        sim.schedule(10, lambda: sim.schedule(5, lambda: times.append(sim.now)))
        drain(sim)
        assert times == [15]

    def test_rng_deterministic_for_same_seed(self):
        a, b = Simulator(7), Simulator(7)
        assert [a.rng.randint(0, 1000) for _ in range(50)] == \
               [b.rng.randint(0, 1000) for _ in range(50)]

    def test_rng_differs_across_seeds(self):
        a, b = Simulator(1), Simulator(2)
        assert [a.rng.randint(0, 10**9) for _ in range(5)] != \
               [b.rng.randint(0, 10**9) for _ in range(5)]


class TestNetwork:
    def test_delivers_message(self):
        sim, net, inbox, _ = make_net()
        net.send(0, 1, "hello")
        drain(sim)
        assert inbox == [(0, 1, "hello")]

    def test_none_profile_never_drops(self):
        sim, net, inbox, _ = make_net(profile="none")
        for i in range(200):
            net.send(0, 1, i)
        drain(sim)
        assert len(inbox) == 200

    def test_chaos_profile_drops_some(self):
        sim, net, inbox, _ = make_net(profile="chaos")
        for i in range(1000):
            net.send(0, 1, i)
        drain(sim)
        # drop_p=0.10, dup_p=0.05: expect roughly 950 deliveries; loose bounds
        assert 800 < len(inbox) < 1000
        assert net.dropped > 30

    def test_chaos_profile_duplicates_some(self):
        sim, net, inbox, _ = make_net(profile="chaos")
        for i in range(1000):
            net.send(0, 1, i)
        drain(sim)
        assert net.duplicated > 10
        counts = {}
        for _, _, msg in inbox:
            counts[msg] = counts.get(msg, 0) + 1
        assert any(c == 2 for c in counts.values())

    def test_chaos_profile_reorders(self):
        sim, net, inbox, _ = make_net(profile="chaos")
        for i in range(100):
            net.send(0, 1, i)
        drain(sim)
        received = [msg for _, _, msg in inbox]
        assert received != sorted(received)

    def test_delays_respect_profile_bounds(self):
        sim, net, inbox, _ = make_net(profile="light")
        net.send(0, 1, "x")
        sim.step()
        p = PROFILES["light"]
        assert p.min_delay <= sim.now <= p.max_delay

    def test_partition_blocks_cross_group_delivery(self):
        sim, net, inbox, log = make_net()
        net.set_partition([{0}, {1, 2}])
        net.send(0, 1, "blocked")
        net.send(1, 2, "ok")
        drain(sim)
        assert inbox == [(1, 2, "ok")]
        assert ("drop", "n0->n1|partitioned") in log

    def test_heal_restores_delivery(self):
        sim, net, inbox, _ = make_net()
        net.set_partition([{0}, {1, 2}])
        net.heal()
        net.send(0, 1, "through")
        drain(sim)
        assert inbox == [(0, 1, "through")]

    def test_partition_must_cover_all_nodes(self):
        _, net, _, _ = make_net()
        with pytest.raises(ValueError):
            net.set_partition([{0}, {1}])  # node 2 missing

    def test_message_in_flight_lost_when_partition_forms(self):
        sim, net, inbox, _ = make_net()
        net.send(0, 1, "doomed")  # in flight...
        net.set_partition([{0}, {1, 2}])  # ...partition forms before delivery
        drain(sim)
        assert inbox == []

    def test_crashed_destination_drops_at_delivery(self):
        sim, net, inbox, log = make_net()
        net.send(0, 1, "x")
        net.crashed.add(1)
        drain(sim)
        assert inbox == []
        assert ("drop", "n0->n1|dst-crashed") in log

    def test_crashed_sender_sends_nothing(self):
        sim, net, inbox, _ = make_net()
        net.crashed.add(0)
        net.send(0, 1, "x")
        drain(sim)
        assert inbox == []

    def test_is_partitioned(self):
        _, net, _, _ = make_net()
        assert not net.is_partitioned()
        net.set_partition([{0, 1}, {2}])
        assert net.is_partitioned()
        net.heal()
        assert not net.is_partitioned()
