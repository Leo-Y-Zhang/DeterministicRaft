"""Harmonia command-line interface.

Commands:
  harmonia run    --nodes 5 --seed 42 --faults chaos --steps 20000 [--timeline out.svg]
  harmonia check  --seeds 300 --faults chaos [--nodes 5 --steps 5000]
  harmonia replay --seed N [--nodes 5 --faults chaos --steps 20000]

Exit codes:
  0  success (run clean / all invariants held / replay identical)
  1  an invariant violation was detected (the message contains the replay command)
  2  usage error (bad arguments; argparse default)
"""

from __future__ import annotations

import argparse
import sys
from typing import cast

from . import __version__
from .cluster import Cluster, RunResult
from .invariants import InvariantViolation
from .linearizability import check
from .report import render_report
from .timeline import render_timeline

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_USAGE = 2


def _execute(nodes: int, seed: int, faults: str, steps: int) -> RunResult:
    return Cluster(num_nodes=nodes, seed=seed, faults=faults).run(steps)


def _print_result(result: RunResult) -> None:
    s = result.stats
    print(f"virtual time: {result.virtual_time} ms  ({result.steps} steps)")
    print(f"messages: sent={s['sent']} delivered={s['delivered']} "
          f"dropped={s['dropped']} duplicated={s['duplicated']}")
    print(f"elections started={s['elections']} leaderships won={s['leaders_elected']}")
    print(f"faults: partitions={s['partitions']} heals={s['heals']} "
          f"crashes={s['crashes']} resumes={s['resumes']}")
    committed = max((n["commit_index"] for n in result.final), default=0)
    print(f"commands: submitted={s['commands_submitted']} committed={committed}")
    print(f"invariants: OK ({s['invariant_checks']} checks, one after every step)")
    print(f"trace digest: sha256:{result.digest}")
    print("final logs:")
    for n in result.final:
        alive = "up  " if n["alive"] else "down"
        print(f"  n{n['id']} {alive} role={n['role']:<9} term={n['term']:<3} "
              f"commit={n['commit_index']:<4} applied={n['applied']:<4} "
              f"len={n['log_length']:<4} log sha256:{n['log_sha256']}")


def cmd_run(args: argparse.Namespace) -> int:
    print(f"harmonia run: nodes={args.nodes} seed={args.seed} "
          f"faults={args.faults} steps={args.steps}")
    try:
        result = _execute(args.nodes, args.seed, args.faults, args.steps)
    except InvariantViolation as violation:
        print(f"INVARIANT VIOLATION: {violation}")
        return EXIT_VIOLATION
    _print_result(result)
    if args.timeline:
        svg = render_timeline(result.events, result.num_nodes, result.virtual_time,
                              title=(f"Harmonia seed={result.seed} faults={result.faults} "
                                     f"nodes={result.num_nodes} steps={result.steps}"))
        with open(args.timeline, "w", encoding="utf-8", newline="\n") as f:
            f.write(svg)
        print(f"timeline written: {args.timeline}")
    return EXIT_OK


def cmd_check(args: argparse.Namespace) -> int:
    print(f"harmonia check: seeds 0..{args.seeds - 1} nodes={args.nodes} "
          f"faults={args.faults} steps={args.steps}")
    violations: list[InvariantViolation] = []
    checks = 0
    for seed in range(args.seeds):
        try:
            result = _execute(args.nodes, seed, args.faults, args.steps)
            checks += result.stats["invariant_checks"]
        except InvariantViolation as violation:
            violations.append(violation)
            print(f"  seed {seed}: VIOLATION {violation}")
    print(f"seeds={args.seeds} faults={args.faults} invariant_checks={checks} "
          f"violations={len(violations)}")
    if violations:
        print("reproduce the first failure with:")
        print(f"  {violations[0].replay}")
        return EXIT_VIOLATION
    return EXIT_OK


def cmd_replay(args: argparse.Namespace) -> int:
    """Run the same configuration twice and prove the traces are byte-identical."""
    print(f"harmonia replay: nodes={args.nodes} seed={args.seed} "
          f"faults={args.faults} steps={args.steps}")
    outcomes = []
    for attempt in (1, 2):
        try:
            outcomes.append(_execute(args.nodes, args.seed, args.faults, args.steps))
        except InvariantViolation as violation:
            print(f"attempt {attempt}: INVARIANT VIOLATION: {violation}")
            return EXIT_VIOLATION
    a, b = outcomes
    identical = a.trace == b.trace
    print(f"attempt 1 digest: sha256:{a.digest}")
    print(f"attempt 2 digest: sha256:{b.digest}")
    if not identical:  # would indicate a determinism bug in Harmonia itself
        print("replay FAILED: traces differ")
        return EXIT_VIOLATION
    print(f"replay verified: {len(a.trace)} trace events, byte-identical")
    _print_result(a)
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    """Run once and write a self-contained HTML report (summary + timeline + verdict)."""
    print(f"harmonia report: nodes={args.nodes} seed={args.seed} "
          f"faults={args.faults} steps={args.steps}")
    cluster = Cluster(num_nodes=args.nodes, seed=args.seed, faults=args.faults)
    try:
        result = cluster.run(args.steps)
    except InvariantViolation as violation:
        print(f"INVARIANT VIOLATION: {violation}")
        return EXIT_VIOLATION
    verdict = check(cluster.history)
    svg = render_timeline(result.events, result.num_nodes, result.virtual_time,
                          title=(f"Harmonia seed={result.seed} faults={result.faults} "
                                 f"nodes={result.num_nodes} steps={result.steps}"))
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(render_report(result, verdict, svg))
    print(f"report written: {args.out}  (linearizable={verdict.linearizable}, "
          f"{verdict.checked_ops} ops checked)")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harmonia",
        description="Educational Raft verified by deterministic simulation testing.",
        epilog="exit codes: 0 ok, 1 invariant violation, 2 usage error",
    )
    parser.add_argument("--version", action="version", version=f"harmonia {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, steps_default: int) -> None:
        p.add_argument("--nodes", type=int, default=5, help="cluster size (default 5)")
        p.add_argument("--faults", choices=["none", "light", "chaos"], default="none",
                       help="fault profile (default none)")
        p.add_argument("--steps", type=int, default=steps_default,
                       help=f"simulator steps (default {steps_default})")

    p_run = sub.add_parser("run", help="run one seeded simulation and print a digest")
    p_run.add_argument("--seed", type=int, default=0)
    common(p_run, 20_000)
    p_run.add_argument("--timeline", metavar="OUT.SVG",
                       help="write an SVG timeline of the run")
    p_run.set_defaults(fn=cmd_run)

    p_check = sub.add_parser("check", help="invariant sweep across many seeds")
    p_check.add_argument("--seeds", type=int, default=300,
                         help="number of seeds to sweep, 0..N-1 (default 300)")
    common(p_check, 5_000)
    p_check.set_defaults(fn=cmd_check)

    p_replay = sub.add_parser("replay",
                              help="reproduce a run exactly (runs twice, compares traces)")
    p_replay.add_argument("--seed", type=int, required=True)
    common(p_replay, 20_000)
    p_replay.set_defaults(fn=cmd_replay)

    p_report = sub.add_parser("report",
                              help="write a self-contained HTML report of one run")
    p_report.add_argument("--seed", type=int, default=0)
    common(p_report, 20_000)
    p_report.add_argument("--out", metavar="OUT.HTML", default="harmonia-report.html",
                          help="output path (default harmonia-report.html)")
    p_report.set_defaults(fn=cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return cast(int, args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
