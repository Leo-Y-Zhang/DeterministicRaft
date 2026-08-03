"""End-to-end CLI tests, both in-process (fast) and via a real subprocess."""

import shlex
import subprocess
import sys

import pytest

from raftverified import __version__
from raftverified.cli import EXIT_OK, EXIT_USAGE, EXIT_VIOLATION, build_parser, main
from raftverified.cluster import Cluster
from raftverified.nemesis import NemesisSchedule


def run_cli(*argv):
    proc = subprocess.run([sys.executable, "-m", "raftverified", *argv],
                          capture_output=True, text=True, timeout=300)
    return proc.returncode, proc.stdout, proc.stderr


class TestRun:
    def test_run_exits_zero_and_prints_digest(self, capsys):
        assert main(["run", "--nodes", "3", "--seed", "1",
                     "--faults", "none", "--steps", "800"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "trace digest: sha256:" in out
        assert "invariants: OK" in out

    def test_run_prints_final_logs(self, capsys):
        main(["run", "--nodes", "3", "--seed", "1", "--steps", "800"])
        out = capsys.readouterr().out
        assert "final logs:" in out
        for i in range(3):
            assert f"n{i}" in out

    def test_run_chaos_all_faults_reported(self, capsys):
        main(["run", "--nodes", "5", "--seed", "42", "--faults", "chaos",
              "--steps", "6000"])
        out = capsys.readouterr().out
        assert "partitions=" in out and "crashes=" in out and "dropped=" in out

    def test_run_writes_timeline_svg(self, tmp_path, capsys):
        out_svg = tmp_path / "run.svg"
        assert main(["run", "--nodes", "5", "--seed", "3", "--faults", "chaos",
                     "--steps", "3000", "--timeline", str(out_svg)]) == EXIT_OK
        text = out_svg.read_text(encoding="utf-8")
        assert text.startswith("<svg") and text.rstrip().endswith("</svg>")

    def test_run_digest_is_stable_across_invocations(self, capsys):
        main(["run", "--seed", "5", "--faults", "chaos", "--steps", "2000"])
        first = capsys.readouterr().out
        main(["run", "--seed", "5", "--faults", "chaos", "--steps", "2000"])
        second = capsys.readouterr().out
        digest = [line for line in first.splitlines() if "trace digest" in line]
        assert digest == [line for line in second.splitlines() if "trace digest" in line]


class TestMembershipFlag:
    def test_run_membership_exits_zero_and_reconfigures(self, capsys):
        assert main(["run", "--nodes", "5", "--seed", "1", "--faults", "none",
                     "--steps", "6000", "--membership"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "config_changes=" in out
        changes = int(out.split("config_changes=")[1].split()[0])
        assert changes > 0

    def test_replay_membership_is_byte_identical(self, capsys):
        assert main(["replay", "--seed", "9", "--faults", "chaos",
                     "--steps", "3000", "--membership"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "replay verified" in out and "byte-identical" in out


# One partition and one crash, declaratively; used across the --nemesis tests.
NEMESIS_JSON = ('[{"pattern":"partition_halves","at":400,"duration":600},'
                '{"pattern":"crash_node","node":1,"at":1200,"duration":400}]')


class TestNemesisFlag:
    def test_run_nemesis_fires_the_scheduled_faults(self, capsys):
        # faults=none injects nothing on its own -> both counters are the schedule
        assert main(["run", "--nodes", "3", "--seed", "1", "--faults", "none",
                     "--steps", "4000", "--nemesis", NEMESIS_JSON]) == EXIT_OK
        out = capsys.readouterr().out
        assert "partitions=1" in out and "crashes=1" in out

    def test_run_nemesis_digest_matches_a_direct_cluster_run(self, capsys):
        direct = Cluster(num_nodes=3, seed=1, faults="none",
                         nemesis=NemesisSchedule.from_json(NEMESIS_JSON)).run(4000)
        main(["run", "--nodes", "3", "--seed", "1", "--faults", "none",
              "--steps", "4000", "--nemesis", NEMESIS_JSON])
        out = capsys.readouterr().out
        assert f"trace digest: sha256:{direct.digest}" in out

    def test_replay_nemesis_is_byte_identical(self, capsys):
        assert main(["replay", "--seed", "4", "--faults", "light",
                     "--steps", "3000", "--nemesis", NEMESIS_JSON]) == EXIT_OK
        out = capsys.readouterr().out
        assert "replay verified" in out and "byte-identical" in out

    def test_bad_nemesis_json_is_a_usage_error(self):
        with pytest.raises(SystemExit) as exc:
            main(["run", "--nemesis", "{nope"])
        assert exc.value.code == EXIT_USAGE

    def test_unknown_pattern_is_a_usage_error(self):
        with pytest.raises(SystemExit) as exc:
            main(["run", "--nemesis", '[{"pattern":"meteor_strike","at":0}]'])
        assert exc.value.code == EXIT_USAGE

    def test_schedule_naming_a_missing_node_is_a_usage_error(self, capsys):
        code = main(["run", "--nodes", "3", "--steps", "500", "--nemesis",
                     '[{"pattern":"crash_node","node":7,"at":100,"duration":100}]'])
        assert code == EXIT_USAGE
        assert "n7" in capsys.readouterr().err

    def test_fractional_node_id_is_a_usage_error(self):
        # 1.5 satisfies node >= 0 and 1.5 < 3, but there is no node 1.5: it must be
        # rejected in validation (exit 2), never surface as a KeyError mid-run (exit 1)
        with pytest.raises(SystemExit) as exc:
            main(["run", "--nodes", "3", "--steps", "500", "--nemesis",
                  '[{"pattern":"crash_node","node":1.5,"at":100,"duration":100}]'])
        assert exc.value.code == EXIT_USAGE

    def test_runaway_flapping_cycles_is_a_usage_error(self):
        # 50 million cycles would materialise 50 million injections before step one;
        # the cycles cap turns that into an instant usage error instead of a hang
        with pytest.raises(SystemExit) as exc:
            main(["run", "--nodes", "3", "--steps", "500", "--nemesis",
                  '[{"pattern":"flapping_link","a":0,"b":1,"at":0,"period":1,'
                  '"cycles":50000000}]'])
        assert exc.value.code == EXIT_USAGE

    def test_replay_hint_from_a_nemesis_cluster_parses_back(self):
        """A violation under a nemesis run prints a replay hint carrying --nemesis;
        that hint must be a valid CLI invocation reconstructing the SAME schedule."""
        sched = NemesisSchedule.from_json(NEMESIS_JSON)
        cluster = Cluster(num_nodes=3, seed=1, faults="none", nemesis=sched)
        tokens = shlex.split(cluster.checker.replay_hint)
        assert tokens[0] == "raftverified"
        args = build_parser().parse_args(tokens[1:])
        assert args.nemesis == sched


class TestCheck:
    def test_check_reports_zero_violations(self, capsys):
        assert main(["check", "--seeds", "5", "--faults", "chaos",
                     "--steps", "1500"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "violations=0" in out
        assert "seeds=5" in out

    def test_check_counts_invariant_checks(self, capsys):
        main(["check", "--seeds", "3", "--faults", "light", "--steps", "1000"])
        out = capsys.readouterr().out
        assert "invariant_checks=3000" in out


class TestReplay:
    def test_replay_verifies_identical_traces(self, capsys):
        assert main(["replay", "--seed", "7", "--faults", "chaos",
                     "--steps", "2000"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "replay verified" in out and "byte-identical" in out

    def test_replay_requires_seed(self):
        with pytest.raises(SystemExit) as exc:
            main(["replay"])
        assert exc.value.code == EXIT_USAGE


class TestUsageErrors:
    def test_unknown_command_exits_2(self):
        with pytest.raises(SystemExit) as exc:
            main(["frobnicate"])
        assert exc.value.code == EXIT_USAGE

    def test_bad_fault_profile_exits_2(self):
        with pytest.raises(SystemExit) as exc:
            main(["run", "--faults", "apocalypse"])
        assert exc.value.code == EXIT_USAGE

    def test_exit_code_constants(self):
        assert (EXIT_OK, EXIT_VIOLATION, EXIT_USAGE) == (0, 1, 2)


class TestSubprocess:
    """Real end-to-end: python -m raftverified in a child process."""

    def test_run_subprocess(self):
        code, out, err = run_cli("run", "--nodes", "3", "--seed", "1",
                                 "--faults", "light", "--steps", "1000")
        assert code == 0, err
        assert "trace digest: sha256:" in out

    def test_check_subprocess(self):
        code, out, err = run_cli("check", "--seeds", "3", "--faults", "chaos",
                                 "--steps", "1000")
        assert code == 0, err
        assert "violations=0" in out

    def test_replay_subprocess_matches_run_digest(self):
        _, run_out, _ = run_cli("run", "--seed", "11", "--faults", "chaos",
                                "--steps", "1500")
        code, replay_out, err = run_cli("replay", "--seed", "11", "--faults", "chaos",
                                        "--steps", "1500")
        assert code == 0, err
        digest = next(line.split("sha256:")[1] for line in run_out.splitlines()
                      if "trace digest" in line)
        assert digest in replay_out

    def test_run_nemesis_subprocess(self):
        code, out, err = run_cli("run", "--nodes", "3", "--seed", "1", "--faults",
                                 "none", "--steps", "2000", "--nemesis", NEMESIS_JSON)
        assert code == 0, err
        assert "trace digest: sha256:" in out and "partitions=1" in out

    def test_version_flag(self):
        code, out, _ = run_cli("--version")
        assert code == 0 and __version__ in out

    def test_usage_error_exit_code(self):
        code, _, _ = run_cli("run", "--faults", "nope")
        assert code == 2
