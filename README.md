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
- readable terminal summary
- JSON report output

## Why this exists

If your MCP server passes schema checks once, that still does not tell you whether it is dependable across real tool calls. `mcp-smoke` runs the server, performs the MCP handshake, lists tools, executes scenario-defined calls, and summarizes whether the server looks trustworthy based on actual outcomes.

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

## Report shape

Terminal output includes:

- verdict
- success rate
- call counts
- per-call latency
- explicit errors

The JSON report contains:

- `summary.verdict`
- `summary.success_rate`
- `summary.latency_p50_ms`
- per-call results
- error list

## Roadmap

- SSE transport
- richer assertions
- packaged GitHub Action wrapper

## License

MIT
