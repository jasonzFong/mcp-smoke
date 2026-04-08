import json
import sys
import time


TOOLS = [
    {
        "name": "flaky_echo",
        "description": "Echoes text but fails every other request.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"}
            },
            "required": ["text"]
        }
    }
]


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main() -> int:
    call_count = 0
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        request = json.loads(line)
        method = request.get("method")
        request_id = request.get("id")

        if method == "initialize":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": request["params"]["protocolVersion"],
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "flaky-server", "version": "0.1.0"}
                    }
                }
            )
            continue

        if method == "notifications/initialized":
            continue

        if method == "tools/list":
            send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
            continue

        if method == "tools/call":
            call_count += 1
            time.sleep(0.01)
            if call_count % 2 == 1:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32001, "message": "intermittent upstream timeout"}
                    }
                )
                continue
            text = request["params"].get("arguments", {}).get("text", "")
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": text}],
                        "structuredContent": {"echo": text},
                        "isError": False
                    }
                }
            )
            continue

        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"unknown method: {method}"}
            }
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
