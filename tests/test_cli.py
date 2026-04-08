import http.server
import json
import threading
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHONPATH = str(ROOT / "src")


class _HttpFixtureHandler(http.server.BaseHTTPRequestHandler):
    server_version = "mcp-smoke-fixture/0.1"
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        response: dict[str, object]

        if self.headers.get("Authorization") != "Bearer test-token":
            response = {
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "error": {"code": -32001, "message": "missing authorization header"},
            }
        else:
            method = payload.get("method")
            if method == "initialize":
                response = {
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": {
                        "protocolVersion": payload["params"]["protocolVersion"],
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "http-echo", "version": "0.1.0"},
                    },
                }
            elif method == "tools/list":
                response = {
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": {
                        "tools": [
                            {
                                "name": "http_echo",
                                "description": "Echo via streamable HTTP",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"text": {"type": "string"}},
                                    "required": ["text"],
                                },
                            }
                        ]
                    },
                }
            elif method == "tools/call":
                response = {
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": payload["params"]["arguments"].get("text", ""),
                            }
                        ],
                        "structuredContent": {
                            "echo": payload["params"]["arguments"].get("text", ""),
                            "auth": self.headers.get("Authorization"),
                        },
                        "isError": False,
                    },
                }
            else:
                response = {
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": {},
                }

        body = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return


class _HttpFixtureServer:
    def __init__(self) -> None:
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _HttpFixtureHandler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/mcp"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "_HttpFixtureServer":
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


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

    def test_missing_tool_failure_mode_is_reported(self) -> None:
        report_dir = Path(tempfile.mkdtemp())
        report_path = report_dir / "report.json"
        cmd = [
            sys.executable,
            "-m",
            "mcp_smoke.cli",
            "--scenario",
            str(ROOT / "examples" / "missing_tool_scenario.json"),
            "--json-out",
            str(report_path),
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
        self.assertNotEqual(result.returncode, 0)
        report = json.loads(report_path.read_text())
        self.assertEqual(report["failure_modes"][0]["category"], "missing_expected_tool")
        self.assertEqual(report["failure_modes"][0]["count"], 1)
        self.assertIn("failure_modes:", result.stdout)
        self.assertIn("missing_expected_tool", result.stdout)

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

    def test_assertion_mismatch_failure_mode_is_reported(self) -> None:
        report_dir = Path(tempfile.mkdtemp())
        report_path = report_dir / "report.json"
        scenario_path = report_dir / "scenario.json"
        scenario_path.write_text(
            json.dumps(
                {
                    "name": "assertion-mismatch",
                    "expected_tools": ["echo"],
                    "calls": [
                        {
                            "tool": "echo",
                            "arguments": {"text": "hello"},
                            "expect": {
                                "path": "structuredContent.echo",
                                "equals": "goodbye",
                            },
                        }
                    ],
                }
            )
        )
        cmd = [
            sys.executable,
            "-m",
            "mcp_smoke.cli",
            "--scenario",
            str(scenario_path),
            "--json-out",
            str(report_path),
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
        self.assertNotEqual(result.returncode, 0)
        report = json.loads(report_path.read_text())
        self.assertEqual(report["failure_modes"][0]["category"], "expectation_mismatch")
        self.assertEqual(report["failure_modes"][0]["count"], 1)
        self.assertIn("failure_modes:", result.stdout)
        self.assertIn("expectation_mismatch", result.stdout)

    def test_streamable_http_transport_supports_remote_server_and_headers(self) -> None:
        report_dir = Path(tempfile.mkdtemp())
        report_path = report_dir / "report.json"
        with _HttpFixtureServer() as fixture:
            cmd = [
                sys.executable,
                "-m",
                "mcp_smoke.cli",
                "--transport",
                "streamable-http",
                "--url",
                fixture.url,
                "--header",
                "Authorization=Bearer test-token",
                "--scenario",
                str(ROOT / "examples" / "http_echo_scenario.json"),
                "--json-out",
                str(report_path),
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
        self.assertEqual(report["summary"]["success_rate"], 1.0)
        self.assertEqual(report["tools"]["http_echo"]["successful_calls"], 1)

    def test_streamable_http_initialize_failure_is_classified_without_traceback(self) -> None:
        report_dir = Path(tempfile.mkdtemp())
        report_path = report_dir / "report.json"
        with _HttpFixtureServer() as fixture:
            cmd = [
                sys.executable,
                "-m",
                "mcp_smoke.cli",
                "--transport",
                "streamable-http",
                "--url",
                fixture.url,
                "--scenario",
                str(ROOT / "examples" / "http_echo_scenario.json"),
                "--json-out",
                str(report_path),
                "--strict",
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
        self.assertTrue(report_path.exists(), msg="expected report to be created")
        report = json.loads(report_path.read_text())
        self.assertEqual(report["summary"]["verdict"], "untrusted")
        self.assertEqual(report["failure_modes"][0]["category"], "setup_failure")
        self.assertIn("failure_modes:", result.stdout)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
