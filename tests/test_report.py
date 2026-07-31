"""The self-contained HTML report (harmonia/report.py + the `report` CLI command)."""

import hashlib
import html as html_module
import re
import shlex

from harmonia.cli import build_parser, main
from harmonia.cluster import Cluster
from harmonia.linearizability import check
from harmonia.nemesis import NemesisSchedule
from harmonia.report import render_report
from harmonia.timeline import render_timeline


def _report(nodes=5, seed=7, faults="chaos", steps=4000, title="t"):
    c = Cluster(num_nodes=nodes, seed=seed, faults=faults)
    r = c.run(steps)
    svg = render_timeline(r.events, r.num_nodes, r.virtual_time, title=title)
    return render_report(r, check(c.history), svg)


def test_report_is_a_pure_function():
    assert _report() == _report()


def test_report_is_self_contained_html():
    html = _report()
    assert html.startswith("<!doctype html>")
    assert html.rstrip().endswith("</html>")
    # inline svg + node table, no external assets other than the svg xml namespace
    assert "<svg" in html and "</svg>" in html
    assert "log sha256" in html
    assert "http://" not in html.replace("http://www.w3.org", "")


def test_report_shows_the_linearizability_verdict():
    assert "LINEARIZABLE" in _report(faults="none", seed=1, steps=6000)


def test_report_golden():
    # pins the exact bytes for a fixed run so any drift in the report is caught
    digest = hashlib.sha256(_report().encode()).hexdigest()
    assert digest == "57be19e4a8e8d3f06f9dc5f211657f550981e5017274dcb9d6c673b8392d5604"


def test_cli_report_writes_a_file(tmp_path):
    out = tmp_path / "r.html"
    code = main(["report", "--nodes", "3", "--seed", "2", "--faults", "light",
                 "--steps", "3000", "--out", str(out)])
    assert code == 0
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_report_replay_command_carries_membership_and_nemesis(tmp_path):
    """The footer's replay command must reproduce THE RUN THE REPORT DESCRIBES: a report
    of a membership+nemesis run whose replay line dropped those flags would document one
    run and replay a fault-free other. Parse the printed command back through the real
    CLI parser and demand both flags survive."""
    sched_json = '[{"pattern":"crash_node","node":1,"at":500,"duration":300}]'
    out = tmp_path / "r.html"
    code = main(["report", "--nodes", "3", "--seed", "1", "--faults", "none",
                 "--steps", "2000", "--membership", "--nemesis", sched_json,
                 "--out", str(out)])
    assert code == 0
    page = out.read_text(encoding="utf-8")
    match = re.search(r"Reproduce this run with\n<code>(.*?)</code>", page, re.DOTALL)
    assert match, "report footer must contain the replay command"
    tokens = shlex.split(html_module.unescape(match.group(1)))
    assert tokens[0] == "harmonia"
    args = build_parser().parse_args(tokens[1:])
    assert args.membership is True
    assert args.nemesis == NemesisSchedule.from_json(sched_json)
    assert (args.nodes, args.seed, args.faults, args.steps) == (3, 1, "none", 2000)
