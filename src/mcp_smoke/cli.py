from __future__ import annotations

import argparse
import math
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib import error, request


PROTOCOL_VERSION = "2025-06-18"


class BaseMcpClient:
    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        raise NotImplementedError

    def close(self) -> None:
        return


class StdioMcpClient(BaseMcpClient):
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


class StreamableHttpMcpClient(BaseMcpClient):
    def __init__(self, url: str, timeout: float, headers: dict[str, str] | None = None) -> None:
        self._url = url
        self._timeout = timeout
        self._headers = headers or {}
        self._next_id = 0

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        http_request = request.Request(
            self._url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                **self._headers,
            },
        )
        try:
            with request.urlopen(http_request, timeout=self._timeout) as response:
                raw_body = response.read()
        except error.HTTPError as exc:
            raw_body = exc.read()
            detail = raw_body.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"HTTP transport error: {exc.reason}") from exc

        if not raw_body:
            return {}
        return json.loads(raw_body.decode("utf-8"))

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        response = self._post(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }
        )
        if response.get("id") != request_id:
            raise RuntimeError(f"unexpected response id for {method}")
        if "error" in response:
            error_payload = response["error"]
            raise RuntimeError(error_payload.get("message", "unknown MCP error"))
        return response.get("result", {})

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._post(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params or {},
            }
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run smoke scenarios against an MCP server.")
    parser.add_argument("--scenario", required=True, help="Path to a JSON scenario file")
    parser.add_argument("--json-out", help="Optional path to write a JSON report")
    parser.add_argument("--repeat", type=int, default=1, help="Number of times to repeat each tool call")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero on any failure")
    parser.add_argument("--timeout", type=float, default=5.0, help="Per-request timeout in seconds")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="Transport type to use.",
    )
    parser.add_argument("--url", help="Remote MCP endpoint URL for streamable-http transport")
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        help="Additional HTTP header in KEY=VALUE format. Repeatable.",
    )
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


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    lower_value = ordered[lower]
    upper_value = ordered[upper]
    return lower_value + (upper_value - lower_value) * (rank - lower)


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
        f"latency_p50_ms: {summary['latency_p50_ms']:.1f}",
        f"latency_p95_ms: {summary['latency_p95_ms']:.1f}",
    ]
    if "budget_passed" in summary:
        lines.append(f"budget_passed: {summary['budget_passed']}")
    for tool_name, stats in report.get("tools", {}).items():
        lines.append(
            f"{tool_name}: {stats['successful_calls']}/{stats['total_calls']} "
            f"success ({stats['success_rate']:.2f}), p95 {stats['latency_p95_ms']:.1f} ms"
        )
    for item in report["results"]:
        lines.append(
            f"{item['tool']}: {item['status']} ({item['latency_ms']:.1f} ms)"
        )
    if report.get("failure_patterns"):
        lines.append("failure_patterns:")
        for pattern in report["failure_patterns"]:
            lines.append(f"- {pattern['tool']}: {pattern['message']} x{pattern['count']}")
    if report.get("violations"):
        lines.append("violations:")
        for violation in report["violations"]:
            lines.append(f"- {violation}")
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
    if args.transport == "stdio" and not command:
        print("missing server command", file=sys.stderr)
        return 2
    if args.transport == "streamable-http" and not args.url:
        print("missing --url for streamable-http transport", file=sys.stderr)
        return 2

    scenario = load_scenario(Path(args.scenario))
    if args.transport == "stdio":
        client: BaseMcpClient = StdioMcpClient(command=command, timeout=args.timeout)
    else:
        headers: dict[str, str] = {}
        for item in args.header:
            if "=" not in item:
                print(f"invalid --header value: {item}", file=sys.stderr)
                return 2
            key, value = item.split("=", 1)
            headers[key] = value
        client = StreamableHttpMcpClient(url=args.url, timeout=args.timeout, headers=headers)

    results: list[dict[str, Any]] = []
    errors: list[str] = []
    violations: list[str] = []
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
                message = f"missing expected tool: {expected_tool}"
                errors.append(message)
                violations.append(message)

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
                            violations.append(f"{call['tool']}: {detail}")
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
    latencies = [item["latency_ms"] for item in results]
    latency_p50 = statistics.median(latencies) if latencies else 0.0
    latency_p95 = percentile(latencies, 0.95)

    tool_buckets: dict[str, list[dict[str, Any]]] = {}
    for item in results:
        tool_buckets.setdefault(item["tool"], []).append(item)

    tool_stats: dict[str, dict[str, Any]] = {}
    for tool_name, tool_results in tool_buckets.items():
        tool_latencies = [item["latency_ms"] for item in tool_results]
        tool_successes = sum(1 for item in tool_results if item["status"] == "pass")
        tool_total = len(tool_results)
        tool_stats[tool_name] = {
            "total_calls": tool_total,
            "successful_calls": tool_successes,
            "success_rate": 0.0 if tool_total == 0 else tool_successes / tool_total,
            "latency_p50_ms": statistics.median(tool_latencies) if tool_latencies else 0.0,
            "latency_p95_ms": percentile(tool_latencies, 0.95),
        }

    failure_pattern_counts: dict[tuple[str, str], int] = {}
    for item in results:
        if item["status"] != "fail":
            continue
        key = (item["tool"], item["detail"])
        failure_pattern_counts[key] = failure_pattern_counts.get(key, 0) + 1

    failure_patterns = [
        {"tool": tool, "message": message, "count": count}
        for (tool, message), count in sorted(
            failure_pattern_counts.items(),
            key=lambda item: (-item[1], item[0][0], item[0][1]),
        )
    ]

    reliability = scenario.get("reliability", {})
    if "min_success_rate" in reliability and success_rate < reliability["min_success_rate"]:
        violations.append(
            f"success rate {success_rate:.2f} below minimum {reliability['min_success_rate']:.2f}"
        )
    if "max_p50_ms" in reliability and latency_p50 > reliability["max_p50_ms"]:
        violations.append(
            f"latency p50 {latency_p50:.1f} ms above maximum {reliability['max_p50_ms']:.1f} ms"
        )
    if "max_p95_ms" in reliability and latency_p95 > reliability["max_p95_ms"]:
        violations.append(
            f"latency p95 {latency_p95:.1f} ms above maximum {reliability['max_p95_ms']:.1f} ms"
        )

    budget_passed = True if not reliability else len(violations) == 0
    hard_failures = len(violations) if reliability else len(errors)
    report = {
        "scenario": scenario.get("name", Path(args.scenario).stem),
        "summary": {
            "verdict": verdict_for(success_rate, hard_failures),
            "success_rate": success_rate,
            "total_calls": total_calls,
            "successful_calls": successful_calls,
            "latency_p50_ms": latency_p50,
            "latency_p95_ms": latency_p95,
            "budget_passed": budget_passed,
        },
        "tools": tool_stats,
        "failure_patterns": failure_patterns,
        "results": results,
        "errors": errors,
        "violations": violations,
    }

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2) + "\n")

    print(format_summary(report))
    strict_failures = violations if reliability else errors
    if args.strict and strict_failures:
        return 1
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
