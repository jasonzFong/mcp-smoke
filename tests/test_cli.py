import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHONPATH = str(ROOT / "src")


class CliTests(unittest.TestCase):
    def run_cli(self, scenario_name: str, extra_args: list[str] | None = None) -> subprocess.CompletedProcess[str]:
        report_path = Path(tempfile.mkdtemp()) / "report.json"
        cmd = [
            sys.executable,
            "-m",
            "mcp_smoke.cli",
            "--scenario",
            str(ROOT / "examples" / scenario_name),
            "--json-out",
            str(report_path),
        ]
        if extra_args:
            cmd.extend(extra_args)
        cmd.extend(["--", sys.executable, str(ROOT / "examples" / "echo_server.py")])
        env = dict(PYTHONPATH=PYTHONPATH, **{})
        return subprocess.run(
            cmd,
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )

    def test_happy_path_report_contains_reliability_summary(self) -> None:
        result = self.run_cli("echo_scenario.json", extra_args=["--repeat", "2", "--strict"])
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
        self.assertIn("verdict", result.stdout.lower())
        self.assertIn("echo", result.stdout)

    def test_missing_tool_fails_in_strict_mode(self) -> None:
        result = self.run_cli("missing_tool_scenario.json", extra_args=["--strict"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing", (result.stdout + result.stderr).lower())

    def test_json_report_is_written(self) -> None:
        report_dir = Path(tempfile.mkdtemp())
        report_path = report_dir / "report.json"
        cmd = [
            sys.executable,
            "-m",
            "mcp_smoke.cli",
            "--scenario",
            str(ROOT / "examples" / "echo_scenario.json"),
            "--json-out",
            str(report_path),
            "--repeat",
            "2",
            "--strict",
            "--",
            sys.executable,
            str(ROOT / "examples" / "echo_server.py"),
        ]
        env = dict(PYTHONPATH=PYTHONPATH, **{})
        result = subprocess.run(
            cmd,
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
        self.assertTrue(report_path.exists(), msg="expected report to be created")
        report = json.loads(report_path.read_text())
        self.assertEqual(report["summary"]["verdict"], "trustworthy")
        self.assertEqual(report["summary"]["success_rate"], 1.0)

    def test_reliability_budget_failure_is_reported_in_strict_mode(self) -> None:
        report_dir = Path(tempfile.mkdtemp())
        report_path = report_dir / "report.json"
        cmd = [
            sys.executable,
            "-m",
            "mcp_smoke.cli",
            "--scenario",
            str(ROOT / "examples" / "flaky_budget_fail.json"),
            "--json-out",
            str(report_path),
            "--repeat",
            "4",
            "--strict",
            "--",
            sys.executable,
            str(ROOT / "examples" / "flaky_server.py"),
        ]
        env = dict(PYTHONPATH=PYTHONPATH, **{})
        result = subprocess.run(
            cmd,
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        self.assertNotEqual(result.returncode, 0)
        report = json.loads(report_path.read_text())
        self.assertEqual(report["summary"]["success_rate"], 0.5)
        self.assertEqual(report["summary"]["verdict"], "untrusted")
        self.assertFalse(report["summary"]["budget_passed"])
        self.assertIn("failure_patterns", report)
        self.assertEqual(report["failure_patterns"][0]["count"], 2)

    def test_reliability_budget_pass_outputs_tool_aggregate_stats(self) -> None:
        report_dir = Path(tempfile.mkdtemp())
        report_path = report_dir / "report.json"
        cmd = [
            sys.executable,
            "-m",
            "mcp_smoke.cli",
            "--scenario",
            str(ROOT / "examples" / "flaky_budget_pass.json"),
            "--json-out",
            str(report_path),
            "--repeat",
            "4",
            "--",
            sys.executable,
            str(ROOT / "examples" / "flaky_server.py"),
        ]
        env = dict(PYTHONPATH=PYTHONPATH, **{})
        result = subprocess.run(
            cmd,
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
        report = json.loads(report_path.read_text())
        self.assertTrue(report["summary"]["budget_passed"])
        tool_stats = report["tools"]["flaky_echo"]
        self.assertEqual(tool_stats["total_calls"], 4)
        self.assertEqual(tool_stats["successful_calls"], 2)
        self.assertEqual(tool_stats["success_rate"], 0.5)
        self.assertIn("latency_p95_ms", tool_stats)
        self.assertIn("failure_patterns", report)


if __name__ == "__main__":
    unittest.main()
