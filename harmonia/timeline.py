"""Hand-rolled SVG timeline of a simulated Raft run. No dependencies.

Renders one horizontal lane per node over virtual time:
  - bar color = current term (palette cycles), bar thickness = role
    (thin follower, medium candidate, thick outlined leader)
  - gray bars = crashed (paused) intervals
  - orange triangle = election started; small dot = vote granted
  - green tick below the lane = commit index advanced
  - red-tinted background band = a network partition is active

The renderer consumes the structured event list a Cluster records, so the same
seed always produces byte-identical SVG output.
"""

from __future__ import annotations

import re

PALETTE = ["#4e79a7", "#f28e2b", "#59a14c", "#e15759", "#b07aa1",
           "#76b7b2", "#edc948", "#9c755f"]
CRASH_COLOR = "#c8c8c8"
ROLE_HEIGHT = {"follower": 8, "candidate": 14, "leader": 20}

LEFT, RIGHT, TOP, ROW, BOTTOM = 70, 30, 56, 48, 96


def term_color(term: int) -> str:
    return PALETTE[term % len(PALETTE)]


def _num(detail: str, key: str) -> int:
    m = re.search(rf"{key}=(\d+)", detail)
    return int(m.group(1)) if m else 0


def _node_of(detail: str) -> int:
    m = re.match(r"n(\d+)", detail)
    if not m:
        raise ValueError(f"event without node prefix: {detail!r}")
    return int(m.group(1))


def render_timeline(
    events: list[tuple[int, str, str]],
    num_nodes: int,
    duration: int,
    title: str = "Harmonia run",
) -> str:
    """Render the recorded events of one run as an SVG string."""
    duration = max(duration, 1)
    width = LEFT + 1000 + RIGHT
    height = TOP + num_nodes * ROW + BOTTOM

    def x(t: int) -> float:
        return LEFT + 1000 * t / duration

    def lane_y(node: int) -> float:  # vertical center of a node's lane
        return TOP + node * ROW + ROW / 2

    # -- reconstruct per-node (term, role, alive) intervals --------------------
    cur_term = {i: 0 for i in range(num_nodes)}
    cur_role = {i: "follower" for i in range(num_nodes)}
    cur_alive = {i: True for i in range(num_nodes)}
    since = {i: 0 for i in range(num_nodes)}
    segments: dict[int, list[tuple[int, int, int, str, bool]]] = {i: [] for i in range(num_nodes)}
    marks: list[str] = []
    partitions: list[tuple[int, int]] = []
    part_start: int | None = None

    def close_segment(node: int, t: int) -> None:
        if t > since[node]:
            segments[node].append(
                (since[node], t, cur_term[node], cur_role[node], cur_alive[node]))
        since[node] = t

    for t, kind, detail in events:
        if kind == "role":
            node = _node_of(detail)
            role = detail.split("|")[1]
            close_segment(node, t)
            cur_role[node] = role
            cur_term[node] = _num(detail, "term")
            if role == "leader":
                term = _num(detail, "term")
                marks.append(
                    f'<text x="{x(t):.1f}" y="{lane_y(node) - 14:.1f}" font-size="10" '
                    f'fill="{term_color(term)}" text-anchor="middle">L{term}</text>')
        elif kind == "election":
            node = _node_of(detail)
            xx, yy = x(t), lane_y(node) - 13
            marks.append(
                f'<path d="M {xx:.1f} {yy + 6:.1f} L {xx - 4:.1f} {yy:.1f} '
                f'L {xx + 4:.1f} {yy:.1f} Z" fill="#e15759"/>')
        elif kind == "vote":
            voter = _node_of(detail)
            marks.append(
                f'<circle cx="{x(t):.1f}" cy="{lane_y(voter):.1f}" r="2" fill="#333"/>')
        elif kind == "commit":
            node = _node_of(detail)
            marks.append(
                f'<line x1="{x(t):.1f}" y1="{lane_y(node) + 12:.1f}" '
                f'x2="{x(t):.1f}" y2="{lane_y(node) + 18:.1f}" '
                f'stroke="#59a14c" stroke-width="1"/>')
        elif kind == "crash":
            node = _node_of(detail)
            close_segment(node, t)
            cur_alive[node] = False
        elif kind == "resume":
            node = _node_of(detail)
            close_segment(node, t)
            cur_alive[node] = True
        elif kind == "partition":
            if part_start is None:
                part_start = t
        elif kind == "heal":
            if part_start is not None:
                partitions.append((part_start, t))
                part_start = None
    if part_start is not None:
        partitions.append((part_start, duration))
    for i in range(num_nodes):
        close_segment(i, duration)

    # -- emit SVG ---------------------------------------------------------------
    out: list[str] = []
    out.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="monospace">')
    out.append(f'<rect width="{width}" height="{height}" fill="#ffffff"/>')
    out.append(f'<text x="{LEFT}" y="20" font-size="14" fill="#111">{title}</text>')

    # partition bands (under everything else)
    for t0, t1 in partitions:
        out.append(
            f'<rect x="{x(t0):.1f}" y="{TOP - 6}" width="{max(x(t1) - x(t0), 1):.1f}" '
            f'height="{num_nodes * ROW + 6}" fill="#e15759" opacity="0.10"/>')
        out.append(
            f'<text x="{x(t0) + 2:.1f}" y="{TOP - 10}" font-size="9" '
            f'fill="#e15759">partition</text>')

    # time axis
    tick_ms = max(duration // 10, 1)
    t = 0
    while t <= duration:
        out.append(
            f'<line x1="{x(t):.1f}" y1="{TOP}" x2="{x(t):.1f}" '
            f'y2="{TOP + num_nodes * ROW}" stroke="#eeeeee" stroke-width="1"/>')
        out.append(
            f'<text x="{x(t):.1f}" y="{TOP + num_nodes * ROW + 16}" font-size="9" '
            f'fill="#777" text-anchor="middle">{t}ms</text>')
        t += tick_ms

    # node lanes
    for i in range(num_nodes):
        yy = lane_y(i)
        out.append(
            f'<text x="{LEFT - 10}" y="{yy + 3:.1f}" font-size="11" fill="#111" '
            f'text-anchor="end">n{i}</text>')
        for t0, t1, term, role, alive in segments[i]:
            h = ROLE_HEIGHT.get(role, 8)
            fill = term_color(term) if alive else CRASH_COLOR
            stroke = ' stroke="#333333" stroke-width="1"' if role == "leader" and alive else ""
            out.append(
                f'<rect x="{x(t0):.1f}" y="{yy - h / 2:.1f}" '
                f'width="{max(x(t1) - x(t0), 0.5):.1f}" height="{h}" '
                f'fill="{fill}"{stroke}/>')

    # event marks (votes, elections, commits, leader labels) over the lanes
    out.extend(marks)

    # legend
    ly = TOP + num_nodes * ROW + 40
    legend = [
        ("thick outlined bar = leader", None),
        ("medium bar = candidate", None),
        ("thin bar = follower", None),
        ("gray = crashed (paused)", CRASH_COLOR),
        ("triangle = election", "#e15759"),
        ("dot = vote", "#333333"),
        ("tick = commit", "#59a14c"),
        ("band = partition", "#e15759"),
    ]
    lx = LEFT
    for label, color in legend:
        if color:
            out.append(f'<rect x="{lx}" y="{ly - 8}" width="10" height="10" fill="{color}"/>')
            lx += 14
        out.append(f'<text x="{lx}" y="{ly}" font-size="10" fill="#333">{label}</text>')
        lx += 7 * len(label) + 18
    out.append(
        f'<text x="{LEFT}" y="{ly + 18}" font-size="10" fill="#333">'
        f'bar color = term (palette cycles every {len(PALETTE)} terms)</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"
