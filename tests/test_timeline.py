"""Timeline SVG renderer tests."""

import xml.etree.ElementTree as ET

from harmonia.cluster import Cluster
from harmonia.timeline import PALETTE, render_timeline, term_color


def chaos_result(seed=3, steps=4000):
    return Cluster(num_nodes=5, seed=seed, faults="chaos").run(steps)


class TestRenderTimeline:
    def test_produces_wellformed_svg(self):
        r = chaos_result()
        svg = render_timeline(r.events, r.num_nodes, r.virtual_time)
        assert svg.startswith("<svg")
        root = ET.fromstring(svg)  # must parse as XML
        assert root.tag.endswith("svg")

    def test_has_a_label_per_node(self):
        r = chaos_result()
        svg = render_timeline(r.events, r.num_nodes, r.virtual_time)
        for i in range(5):
            assert f">n{i}</text>" in svg

    def test_uses_term_palette_colors(self):
        r = chaos_result()
        svg = render_timeline(r.events, r.num_nodes, r.virtual_time)
        used = [c for c in PALETTE if c in svg]
        assert len(used) >= 2  # at least initial term + one election

    def test_marks_partitions_when_present(self):
        r = chaos_result()
        assert r.stats["partitions"] > 0  # chaos seed chosen to partition
        svg = render_timeline(r.events, r.num_nodes, r.virtual_time)
        assert ">partition</text>" in svg

    def test_marks_commits_and_votes(self):
        r = chaos_result()
        svg = render_timeline(r.events, r.num_nodes, r.virtual_time)
        assert 'stroke="#59a14c"' in svg  # commit ticks
        assert '<circle' in svg           # vote dots

    def test_leader_labels_present(self):
        r = chaos_result()
        svg = render_timeline(r.events, r.num_nodes, r.virtual_time)
        assert ">L" in svg  # L<term> labels above leader segments

    def test_deterministic_bytes_for_same_seed(self):
        a, b = chaos_result(seed=9), chaos_result(seed=9)
        svg_a = render_timeline(a.events, a.num_nodes, a.virtual_time)
        svg_b = render_timeline(b.events, b.num_nodes, b.virtual_time)
        assert svg_a.encode() == svg_b.encode()

    def test_title_and_legend(self):
        r = chaos_result()
        svg = render_timeline(r.events, r.num_nodes, r.virtual_time, title="hello raft")
        assert "hello raft" in svg
        assert "tick = commit" in svg

    def test_term_color_cycles(self):
        assert term_color(0) == PALETTE[0]
        assert term_color(len(PALETTE)) == PALETTE[0]

    def test_empty_run_renders(self):
        svg = render_timeline([], 3, 0)
        ET.fromstring(svg)
