# mcp-smoke

`mcp-smoke` is a small CLI for replaying smoke scenarios against MCP servers and turning the result into a reliability verdict you can use in CI.

It focuses on a gap between:

- static MCP validation,
- runtime wrappers that require instrumentation, and
- opaque hosted quality scores.

The first release is intentionally small:

- stdio transport
- JSON scenario files
- repeated `tools/call` execution
- scenario-level reliability budgets
- per-tool reliability aggregates
- grouped failure-pattern reporting
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
- overall and per-tool call counts
- overall and per-tool latency summaries
- grouped failure patterns
- explicit errors

The JSON report contains:

- `summary.verdict`
- `summary.success_rate`
- `summary.latency_p50_ms`
- `summary.latency_p95_ms`
- `tools.<tool>.success_rate`
- `failure_patterns[]`
- per-call results
- error list

## Roadmap

- scenario ergonomics for more real-world reliability checks
- SSE transport if remote-server demand shows up
- packaged GitHub Action wrapper after the reliability thesis is sharper

## License

MIT
