"""The self-contained HTML report (harmonia/report.py + the `report` CLI command)."""

import hashlib

from harmonia.cli import main
from harmonia.cluster import Cluster
from harmonia.linearizability import check
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
