"""End-to-end CLI tests, both in-process (fast) and via a real subprocess."""

import subprocess
import sys

import pytest

from harmonia import __version__
from harmonia.cli import EXIT_OK, EXIT_USAGE, EXIT_VIOLATION, main


def run_cli(*argv):
    proc = subprocess.run([sys.executable, "-m", "harmonia", *argv],
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
    """Real end-to-end: python -m harmonia in a child process."""

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

    def test_version_flag(self):
        code, out, _ = run_cli("--version")
        assert code == 0 and __version__ in out

    def test_usage_error_exit_code(self):
        code, _, _ = run_cli("run", "--faults", "nope")
        assert code == 2
