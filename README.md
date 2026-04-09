# mcp-smoke

`mcp-smoke` is a small CLI for replaying smoke scenarios against MCP servers and turning the result into a reliability verdict you can use in CI.

It focuses on a gap between:

- static MCP validation,
- runtime wrappers that require instrumentation, and
- opaque hosted quality scores.

The first release is intentionally small:

- stdio transport
- `streamable-http` transport in JSON response mode
- custom HTTP headers for remote servers
- JSON scenario files
- repeated `tools/call` execution
- scenario-level reliability budgets
- per-tool reliability aggregates
- failure-mode classification
- grouped failure-pattern reporting
- baseline report comparison for regression checks
- per-tool baseline diffs for actionable regression diagnosis
- readable terminal summary
- JSON report output

## Why this exists

If your MCP server passes schema checks once, that still does not tell you whether it is dependable across real tool calls. `mcp-smoke` runs the server, performs the MCP handshake, lists tools, executes scenario-defined calls, and summarizes whether the server looks trustworthy based on actual outcomes.

## Product focus

`mcp-smoke` is not trying to be:

- an interface diff tool
- a broad MCP conformance suite
- a many-assertion test framework

It is trying to answer a narrower question well:

> "If I replay the calls that matter, does this server stay reliable enough for my budget?"

And now:

> "Did this run get worse than the last known-good smoke report?"

## Quickstart

```bash
python3 -m unittest discover -s tests -p 'test_*.py'

PYTHONPATH=src python3 -m mcp_smoke.cli \
  --scenario examples/echo_scenario.json \
  --repeat 2 \
  --strict \
  --json-out /tmp/mcp-smoke-report.json \
  -- python3 examples/echo_server.py
```

### Compare against a baseline

Point `mcp-smoke` at a previous JSON report to see whether reliability improved, stayed steady, or regressed:

```bash
PYTHONPATH=src python3 -m mcp_smoke.cli \
  --scenario examples/flaky_budget_pass.json \
  --repeat 4 \
  --baseline /tmp/last-known-good.json \
  --fail-on-regression \
  --json-out /tmp/mcp-smoke-report.json \
  -- python3 examples/flaky_server.py
```

This keeps the tool narrow:

- it compares reliability verdicts and summary metrics from prior smoke reports
- it also points to which tool regressed or improved on success rate, latency, or failure modes
- it does not record or replay raw MCP traffic
- it can fail CI only when a real regression against the baseline is detected

### Remote `streamable-http`

`mcp-smoke` can also exercise a remote MCP endpoint in JSON response mode:

```bash
PYTHONPATH=src python3 -m mcp_smoke.cli \
  --transport streamable-http \
  --url http://127.0.0.1:8080/mcp \
  --header Authorization="Bearer test-token" \
  --scenario examples/http_echo_scenario.json
```

Current remote scope is intentionally narrow:

- supports `streamable-http`
- supports request headers
- expects JSON request/response flow
- does **not** implement SSE event-stream handling in this version

## Scenario format

```json
{
  "name": "echo-happy-path",
  "expected_tools": ["echo"],
  "calls": [
    {
      "tool": "echo",
      "arguments": { "text": "hello" },
      "expect": {
        "path": "structuredContent.echo",
        "equals": "hello"
      }
    }
  ]
}
```

### Reliability budgets

Scenarios can define optional reliability thresholds:

```json
{
  "name": "flaky-budget-pass",
  "expected_tools": ["flaky_echo"],
  "reliability": {
    "min_success_rate": 0.5,
    "max_p95_ms": 100
  },
  "calls": [
    {
      "tool": "flaky_echo",
      "arguments": { "text": "hello" },
      "expect": {
        "path": "structuredContent.echo",
        "equals": "hello"
      }
    }
  ]
}
```

Available thresholds today:

- `min_success_rate`
- `max_p50_ms`
- `max_p95_ms`

## Report shape

Terminal output includes:

- verdict
- success rate
- budget pass/fail status
- baseline tool-level regressions and improvements when `--baseline` is used
- failure-mode summary
- overall and per-tool call counts
- overall and per-tool latency summaries
- grouped failure patterns
- explicit errors

The JSON report contains:

- `summary.verdict`
- `summary.success_rate`
- `summary.budget_passed`
- `summary.latency_p50_ms`
- `summary.latency_p95_ms`
- `failure_modes[]`
- `tools.<tool>.success_rate`
- `tools.<tool>.failure_modes[]`
- `failure_patterns[]`
- `comparison.status` when `--baseline` is used
- `comparison.regressions[]` / `comparison.improvements[]`
- `comparison.tool_diffs.<tool>.status`
- `comparison.tool_diffs.<tool>.regressions[]`
- `comparison.tool_diffs.<tool>.improvements[]`
- per-call results
- error list

Current failure-mode categories include:

- `missing_expected_tool`
- `expectation_mismatch`
- `setup_failure`
- `timeout`
- `tool_call_failure`

## Roadmap

- scenario ergonomics for more real-world reliability checks
- fuller remote transport coverage only if clear demand shows up
- packaged GitHub Action wrapper after the reliability thesis is sharper

## License

MIT
