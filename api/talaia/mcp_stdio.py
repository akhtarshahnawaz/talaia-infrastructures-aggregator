"""stdio bridge to a TALAIA MCP endpoint.

Clients such as Claude Desktop launch a local process and talk newline-delimited JSON-RPC
over stdin/stdout. TALAIA's MCP server is HTTP, so this is the adapter between the two.

It is a pure proxy and holds no logic of its own. Every message is forwarded verbatim to
``$TALAIA_URL/mcp`` with the API key attached, and the reply is written back unchanged.
That is deliberate: the limits live on the server, and a bridge that understood the
protocol well enough to shortcut a call would be a bridge that could get the limits
wrong. Anything this process could decide locally is something an attacker could decide
locally too, by running their own copy.

Usage:

    TALAIA_URL=https://talaia.up.railway.app \\
    TALAIA_API_KEY=talaia_sk_... \\
    python -m talaia.mcp_stdio

Claude Desktop configuration (``claude_desktop_config.json``):

    {"mcpServers": {"talaia": {
        "command": "python",
        "args": ["-m", "talaia.mcp_stdio"],
        "env": {"TALAIA_URL": "https://talaia.up.railway.app",
                "TALAIA_API_KEY": "talaia_sk_..."}}}}

stdout carries protocol traffic and nothing else; diagnostics go to stderr.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx

DEFAULT_URL = "http://127.0.0.1:8000"
TIMEOUT_S = float(os.environ.get("TALAIA_MCP_TIMEOUT_S", "120"))


def _log(message: str) -> None:
    print(f"talaia-mcp: {message}", file=sys.stderr, flush=True)


def _error(rpc_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def main() -> int:
    base = os.environ.get("TALAIA_URL", DEFAULT_URL).rstrip("/")
    key = os.environ.get("TALAIA_API_KEY", "").strip()
    if not key:
        _log("TALAIA_API_KEY is not set. The server will reject every call; "
             "get a key from POST /v1/signup or your operator.")
    endpoint = f"{base}/mcp"
    headers = {"content-type": "application/json",
               "accept": "application/json, text/event-stream"}
    if key:
        headers["x-api-key"] = key
    _log(f"proxying to {endpoint}")

    with httpx.Client(timeout=TIMEOUT_S, headers=headers) as client:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                _emit(_error(None, -32700, "Bridge received invalid JSON on stdin."))
                continue

            rpc_id = message.get("id") if isinstance(message, dict) else None
            try:
                response = client.post(endpoint, content=line.encode("utf-8"))
            except httpx.HTTPError as exc:
                _log(f"transport error: {exc}")
                if rpc_id is not None:
                    _emit(_error(rpc_id, -32001,
                                 f"Cannot reach TALAIA at {endpoint}: {exc}"))
                continue

            # 202 is a notification acknowledgement: nothing to write back.
            if response.status_code == 202 or not response.content:
                continue
            if response.status_code in (401, 403, 429):
                detail = _detail(response)
                _log(f"{response.status_code}: {detail}")
                if rpc_id is None:
                    continue
                if message.get("method") == "tools/call":
                    # Shape it as a failed tool result so the model reads the reason and
                    # can act on it - wait out a rate limit, or split an oversized area.
                    _emit({"jsonrpc": "2.0", "id": rpc_id,
                           "result": {"content": [{"type": "text", "text": detail}],
                                      "isError": True}})
                else:
                    # Every other method returns a typed result the client parses, so a
                    # tool-shaped body would fail validation instead of surfacing the
                    # cause. Those get a real JSON-RPC error.
                    _emit(_error(rpc_id, -32002, detail))
                continue
            try:
                _emit(response.json())
            except ValueError:
                if rpc_id is not None:
                    _emit(_error(rpc_id, -32603,
                                 f"TALAIA returned a non-JSON response "
                                 f"({response.status_code})."))
    return 0


def _detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP {response.status_code} from TALAIA."
    return str(payload.get("detail") or payload.get("error")
               or f"HTTP {response.status_code} from TALAIA.")


def _emit(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, default=str) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
