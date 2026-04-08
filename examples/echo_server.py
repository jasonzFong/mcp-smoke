import json
import sys


TOOLS = [
    {
        "name": "echo",
        "description": "Return the provided text.",
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
    initialized = False
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
                        "serverInfo": {"name": "echo-server", "version": "0.1.0"}
                    }
                }
            )
            continue

        if method == "notifications/initialized":
            initialized = True
            continue

        if method == "tools/list":
            send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
            continue

        if method == "tools/call":
            arguments = request["params"].get("arguments", {})
            text = arguments.get("text", "")
            if text == "explode":
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32000, "message": "forced failure"}
                    }
                )
                continue
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": text}],
                        "structuredContent": {"echo": text, "initialized": initialized},
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
