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
LATENCY_REGRESSION_GRACE_MS = 5.0
SUCCESS_RATE_EPSILON = 0.001
VERDICT_RANK = {"untrusted": 0, "caution": 1, "trustworthy": 2}


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
    parser.add_argument("--baseline", help="Optional JSON report from a previous run")
    parser.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="Exit non-zero when the current run regresses against --baseline",
    )
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


def load_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


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


def empty_tool_stats() -> dict[str, Any]:
    return {
        "total_calls": 0,
        "successful_calls": 0,
        "success_rate": 0.0,
        "latency_p50_ms": 0.0,
        "latency_p95_ms": 0.0,
        "failure_modes": [],
    }


def compare_failure_modes(
    current_modes: list[dict[str, Any]], baseline_modes: list[dict[str, Any]]
) -> tuple[list[str], list[str]]:
    regressions: list[str] = []
    improvements: list[str] = []
    current_counts = {item["category"]: item["count"] for item in current_modes}
    baseline_counts = {item["category"]: item["count"] for item in baseline_modes}
    for category in sorted(set(current_counts) | set(baseline_counts)):
        current_count = current_counts.get(category, 0)
        baseline_count = baseline_counts.get(category, 0)
        if current_count > baseline_count:
            regressions.append(
                f"failure mode {category} increased from {baseline_count} to {current_count}"
            )
        elif baseline_count > current_count:
            improvements.append(
                f"failure mode {category} dropped from {baseline_count} to {current_count}"
            )
    return regressions, improvements


def compare_tool_stats(
    current_tools: dict[str, dict[str, Any]], baseline_tools: dict[str, dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], list[str], list[str]]:
    tool_diffs: dict[str, dict[str, Any]] = {}
    regressions: list[str] = []
    improvements: list[str] = []

    for tool_name in sorted(set(current_tools) | set(baseline_tools)):
        current_stats = current_tools.get(tool_name)
        baseline_stats = baseline_tools.get(tool_name)
        tool_regressions: list[str] = []
        tool_improvements: list[str] = []

        if current_stats is None:
            tool_regressions.append("tool disappeared from current report")
        elif baseline_stats is None:
            tool_improvements.append("tool is new in current report")
        else:
            current_success_rate = float(current_stats.get("success_rate", 0.0))
            baseline_success_rate = float(baseline_stats.get("success_rate", 0.0))
            if current_success_rate + SUCCESS_RATE_EPSILON < baseline_success_rate:
                tool_regressions.append(
                    f"success rate regressed from {baseline_success_rate:.2f} to {current_success_rate:.2f}"
                )
            elif baseline_success_rate + SUCCESS_RATE_EPSILON < current_success_rate:
                tool_improvements.append(
                    f"success rate improved from {baseline_success_rate:.2f} to {current_success_rate:.2f}"
                )

            current_p95 = float(current_stats.get("latency_p95_ms", 0.0))
            baseline_p95 = float(baseline_stats.get("latency_p95_ms", 0.0))
            if current_p95 > baseline_p95 + LATENCY_REGRESSION_GRACE_MS:
                tool_regressions.append(
                    f"latency p95 regressed from {baseline_p95:.1f} ms to {current_p95:.1f} ms"
                )
            elif baseline_p95 > current_p95 + LATENCY_REGRESSION_GRACE_MS:
                tool_improvements.append(
                    f"latency p95 improved from {baseline_p95:.1f} ms to {current_p95:.1f} ms"
                )

            failure_mode_regressions, failure_mode_improvements = compare_failure_modes(
                current_stats.get("failure_modes", []),
                baseline_stats.get("failure_modes", []),
            )
            tool_regressions.extend(failure_mode_regressions)
            tool_improvements.extend(failure_mode_improvements)

        if tool_regressions:
            status = "regressed"
        elif tool_improvements:
            status = "improved"
        else:
            status = "steady"

        tool_diffs[tool_name] = {
            "status": status,
            "regressions": tool_regressions,
            "improvements": tool_improvements,
        }
        regressions.extend(f"tool {tool_name}: {item}" for item in tool_regressions)
        improvements.extend(f"tool {tool_name}: {item}" for item in tool_improvements)

    return tool_diffs, regressions, improvements


def compare_reports(current: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    current_summary = current["summary"]
    baseline_summary = baseline.get("summary", {})
    regressions: list[str] = []
    improvements: list[str] = []

    current_verdict = current_summary.get("verdict", "untrusted")
    baseline_verdict = baseline_summary.get("verdict", "untrusted")
    current_rank = VERDICT_RANK.get(current_verdict, 0)
    baseline_rank = VERDICT_RANK.get(baseline_verdict, 0)
    if current_rank < baseline_rank:
        regressions.append(f"verdict regressed from {baseline_verdict} to {current_verdict}")
    elif current_rank > baseline_rank:
        improvements.append(f"verdict improved from {baseline_verdict} to {current_verdict}")

    current_budget = bool(current_summary.get("budget_passed", False))
    baseline_budget = bool(baseline_summary.get("budget_passed", False))
    if baseline_budget and not current_budget:
        regressions.append("reliability budget changed from passing to failing")
    elif current_budget and not baseline_budget:
        improvements.append("reliability budget changed from failing to passing")

    current_success_rate = float(current_summary.get("success_rate", 0.0))
    baseline_success_rate = float(baseline_summary.get("success_rate", 0.0))
    if current_success_rate + SUCCESS_RATE_EPSILON < baseline_success_rate:
        regressions.append(
            f"success rate regressed from {baseline_success_rate:.2f} to {current_success_rate:.2f}"
        )
    elif baseline_success_rate + SUCCESS_RATE_EPSILON < current_success_rate:
        improvements.append(
            f"success rate improved from {baseline_success_rate:.2f} to {current_success_rate:.2f}"
        )

    current_p95 = float(current_summary.get("latency_p95_ms", 0.0))
    baseline_p95 = float(baseline_summary.get("latency_p95_ms", 0.0))
    if current_p95 > baseline_p95 + LATENCY_REGRESSION_GRACE_MS:
        regressions.append(
            f"latency p95 regressed from {baseline_p95:.1f} ms to {current_p95:.1f} ms"
        )
    elif baseline_p95 > current_p95 + LATENCY_REGRESSION_GRACE_MS:
        improvements.append(
            f"latency p95 improved from {baseline_p95:.1f} ms to {current_p95:.1f} ms"
        )

    failure_mode_regressions, failure_mode_improvements = compare_failure_modes(
        current.get("failure_modes", []),
        baseline.get("failure_modes", []),
    )
    regressions.extend(failure_mode_regressions)
    improvements.extend(failure_mode_improvements)

    tool_diffs, tool_regressions, tool_improvements = compare_tool_stats(
        current.get("tools", {}),
        baseline.get("tools", {}),
    )
    regressions.extend(tool_regressions)
    improvements.extend(tool_improvements)

    if regressions:
        status = "regressed"
    elif improvements:
        status = "improved"
    else:
        status = "steady"

    return {
        "status": status,
        "baseline_scenario": baseline.get("scenario"),
        "baseline_summary": baseline_summary,
        "regressions": regressions,
        "improvements": improvements,
        "tool_diffs": tool_diffs,
    }


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
    comparison = report.get("comparison")
    if comparison:
        lines.append(f"baseline_status: {comparison['status']}")
        if comparison.get("regressions"):
            lines.append("baseline_regressions:")
            for item in comparison["regressions"]:
                lines.append(f"- {item}")
        if comparison.get("improvements"):
            lines.append("baseline_improvements:")
            for item in comparison["improvements"]:
                lines.append(f"- {item}")
        changed_tool_diffs = {
            tool_name: diff
            for tool_name, diff in comparison.get("tool_diffs", {}).items()
            if diff["status"] != "steady"
        }
        if changed_tool_diffs:
            lines.append("baseline_tools:")
            for tool_name, diff in changed_tool_diffs.items():
                lines.append(f"- {tool_name}: {diff['status']}")
                for item in diff["regressions"]:
                    lines.append(f"  regression: {item}")
                for item in diff["improvements"]:
                    lines.append(f"  improvement: {item}")
    if report.get("failure_modes"):
        lines.append("failure_modes:")
        for failure_mode in report["failure_modes"]:
            lines.append(f"- {failure_mode['category']} x{failure_mode['count']}")
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


def classify_exception(exc: Exception, stage: str) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    if stage in {"initialize", "notifications/initialized", "tools/list"}:
        return "setup_failure"
    return "tool_call_failure"


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
    if args.fail_on_regression and not args.baseline:
        print("missing --baseline for --fail-on-regression", file=sys.stderr)
        return 2

    scenario = load_scenario(Path(args.scenario))
    baseline_report: dict[str, Any] | None = None
    if args.baseline:
        try:
            baseline_report = load_report(Path(args.baseline))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"failed to load baseline report: {exc}", file=sys.stderr)
            return 2
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
    failure_events: list[dict[str, str | None]] = []
    successful_calls = 0
    total_calls = 0

    try:
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
        except Exception as exc:  # noqa: BLE001
            category = classify_exception(exc, "initialize")
            message = str(exc)
            errors.append(message)
            failure_events.append({"category": category, "message": message, "tool": None})
            tools_result = {"tools": []}
        else:
            tools = tools_result.get("tools", [])
            tool_names = {tool["name"] for tool in tools}

            for expected_tool in scenario.get("expected_tools", []):
                if expected_tool not in tool_names:
                    message = f"missing expected tool: {expected_tool}"
                    errors.append(message)
                    violations.append(message)
                    failure_events.append(
                        {
                            "category": "missing_expected_tool",
                            "message": message,
                            "tool": expected_tool,
                        }
                    )

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
                                detail = (
                                    f"expected {expectation['path']}={expectation['equals']!r}, "
                                    f"got {actual!r}"
                                )
                                violations.append(f"{call['tool']}: {detail}")
                                failure_events.append(
                                    {
                                        "category": "expectation_mismatch",
                                        "message": f"{call['tool']}: {detail}",
                                        "tool": call["tool"],
                                    }
                                )
                            else:
                                successful_calls += 1
                        else:
                            successful_calls += 1
                    except Exception as exc:  # noqa: BLE001
                        status = "fail"
                        detail = str(exc)
                        failure_events.append(
                            {
                                "category": classify_exception(exc, "tools/call"),
                                "message": f"{call['tool']}: {detail}",
                                "tool": call["tool"],
                            }
                        )
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
            "failure_modes": [],
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

    failure_mode_counts: dict[str, int] = {}
    for item in failure_events:
        category = item["category"]
        failure_mode_counts[category] = failure_mode_counts.get(category, 0) + 1

    failure_modes = [
        {"category": category, "count": count}
        for category, count in sorted(
            failure_mode_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]

    tool_failure_mode_counts: dict[str, dict[str, int]] = {}
    for item in failure_events:
        tool_name = item.get("tool")
        if not tool_name:
            continue
        category = item["category"]
        tool_failure_mode_counts.setdefault(tool_name, {})
        tool_failure_mode_counts[tool_name][category] = (
            tool_failure_mode_counts[tool_name].get(category, 0) + 1
        )

    for tool_name, mode_counts in tool_failure_mode_counts.items():
        tool_stats.setdefault(tool_name, empty_tool_stats())
        tool_stats[tool_name]["failure_modes"] = [
            {"category": category, "count": count}
            for category, count in sorted(
                mode_counts.items(),
                key=lambda item: (-item[1], item[0]),
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

    budget_passed = len(errors) == 0 if not reliability else len(violations) == 0
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
        "failure_modes": failure_modes,
        "failure_patterns": failure_patterns,
        "results": results,
        "errors": errors,
        "violations": violations,
    }
    if baseline_report is not None:
        report["comparison"] = compare_reports(report, baseline_report)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2) + "\n")

    print(format_summary(report))
    strict_failures = violations if reliability else errors
    if args.strict and strict_failures:
        return 1
    comparison = report.get("comparison")
    if args.fail_on_regression and comparison and comparison["status"] == "regressed":
        return 1
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
