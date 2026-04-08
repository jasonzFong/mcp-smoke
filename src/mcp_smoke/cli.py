from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = "2025-06-18"


class McpClient:
    def __init__(self, command: list[str], timeout: float) -> None:
        self._timeout = timeout
        self._next_id = 0
        self._proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def close(self) -> None:
        if self._proc.stdin:
            self._proc.stdin.close()
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        if not self._proc.stdin or not self._proc.stdout:
            raise RuntimeError("MCP process pipes are unavailable")
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()

        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            line = self._proc.stdout.readline()
            if not line:
                stderr = ""
                if self._proc.stderr:
                    stderr = self._proc.stderr.read().strip()
                raise RuntimeError(f"MCP server closed unexpectedly: {stderr}".strip())
            message = json.loads(line)
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message["error"]
                raise RuntimeError(error.get("message", "unknown MCP error"))
            return message["result"]
        raise TimeoutError(f"timed out waiting for response to {method}")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        if not self._proc.stdin:
            raise RuntimeError("MCP process stdin is unavailable")
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run smoke scenarios against an MCP server.")
    parser.add_argument("--scenario", required=True, help="Path to a JSON scenario file")
    parser.add_argument("--json-out", help="Optional path to write a JSON report")
    parser.add_argument("--repeat", type=int, default=1, help="Number of times to repeat each tool call")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero on any failure")
    parser.add_argument("--timeout", type=float, default=5.0, help="Per-request timeout in seconds")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Server command after --")
    return parser.parse_args(argv)


def load_scenario(path: Path) -> dict[str, Any]:
    scenario = json.loads(path.read_text())
    if "calls" not in scenario or not isinstance(scenario["calls"], list):
        raise ValueError("scenario must contain a calls array")
    return scenario


def get_by_path(value: Any, dotted_path: str) -> Any:
    current = value
    for segment in dotted_path.split("."):
        if isinstance(current, dict) and segment in current:
            current = current[segment]
            continue
        raise KeyError(f"path not found: {dotted_path}")
    return current


def verdict_for(success_rate: float, hard_failures: int) -> str:
    if hard_failures > 0:
        return "untrusted"
    if success_rate >= 0.99:
        return "trustworthy"
    if success_rate >= 0.8:
        return "caution"
    return "untrusted"


def format_summary(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        f"verdict: {summary['verdict']}",
        f"success_rate: {summary['success_rate']:.2f}",
        f"calls: {summary['successful_calls']}/{summary['total_calls']}",
    ]
    for item in report["results"]:
        lines.append(
            f"{item['tool']}: {item['status']} ({item['latency_ms']:.1f} ms)"
        )
    if report["errors"]:
        lines.append("errors:")
        for error in report["errors"]:
            lines.append(f"- {error}")
    return "\n".join(lines)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("missing server command", file=sys.stderr)
        return 2

    scenario = load_scenario(Path(args.scenario))
    client = McpClient(command=command, timeout=args.timeout)

    results: list[dict[str, Any]] = []
    errors: list[str] = []
    successful_calls = 0
    total_calls = 0

    try:
        client.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"roots": {"listChanged": False}, "sampling": {}},
                "clientInfo": {"name": "mcp-smoke", "version": "0.1.0"},
            },
        )
        client.notify("notifications/initialized")
        tools_result = client.request("tools/list")
        tools = tools_result.get("tools", [])
        tool_names = {tool["name"] for tool in tools}

        for expected_tool in scenario.get("expected_tools", []):
            if expected_tool not in tool_names:
                errors.append(f"missing expected tool: {expected_tool}")

        for call in scenario["calls"]:
            for _ in range(args.repeat):
                total_calls += 1
                started_at = time.perf_counter()
                status = "pass"
                detail = ""
                try:
                    result = client.request(
                        "tools/call",
                        {
                            "name": call["tool"],
                            "arguments": call.get("arguments", {}),
                        },
                    )
                    expectation = call.get("expect")
                    if expectation:
                        actual = get_by_path(result, expectation["path"])
                        if actual != expectation["equals"]:
                            status = "fail"
                            detail = f"expected {expectation['path']}={expectation['equals']!r}, got {actual!r}"
                        else:
                            successful_calls += 1
                    else:
                        successful_calls += 1
                except Exception as exc:  # noqa: BLE001
                    status = "fail"
                    detail = str(exc)
                latency_ms = (time.perf_counter() - started_at) * 1000
                if status == "fail":
                    errors.append(f"{call['tool']}: {detail}")
                results.append(
                    {
                        "tool": call["tool"],
                        "status": status,
                        "detail": detail,
                        "latency_ms": latency_ms,
                    }
                )
    finally:
        client.close()

    success_rate = 0.0 if total_calls == 0 else successful_calls / total_calls
    hard_failures = len(errors)
    latencies = [item["latency_ms"] for item in results]
    latency_p50 = statistics.median(latencies) if latencies else 0.0
    report = {
        "scenario": scenario.get("name", Path(args.scenario).stem),
        "summary": {
            "verdict": verdict_for(success_rate, hard_failures),
            "success_rate": success_rate,
            "total_calls": total_calls,
            "successful_calls": successful_calls,
            "latency_p50_ms": latency_p50,
        },
        "results": results,
        "errors": errors,
    }

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2) + "\n")

    print(format_summary(report))
    if args.strict and errors:
        return 1
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
