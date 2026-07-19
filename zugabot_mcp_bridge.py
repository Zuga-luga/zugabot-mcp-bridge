"""Zugabot MCP ↔ A2A bridge.

A stdio MCP server that lets MCP clients (Claude Code, Cursor, etc.) delegate
tasks to a remote Zugabot A2A endpoint. The client sees ordinary MCP tools;
this process translates each tool call into an A2A JSON-RPC SendMessage.

Config (env):
    ZUGABOT_A2A_URL    Base URL of the Zugabot A2A server   (default: https://zugabot.ai)
    ZUGABOT_A2A_TOKEN  Bearer credential as `client_id:secret` (required)

Run:
    python zugabot_mcp_bridge.py          # stdio MCP server

Register with Claude Code:
    claude mcp add zugabot -- python /absolute/path/to/zugabot_mcp_bridge.py
"""

from __future__ import annotations

import os
import sys
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP


A2A_URL = os.environ.get("ZUGABOT_A2A_URL", "https://zugabot.ai").rstrip("/")
A2A_TOKEN = os.environ.get("ZUGABOT_A2A_TOKEN", "").strip()

if not A2A_TOKEN:
    print(
        "ERROR: set ZUGABOT_A2A_TOKEN=<client_id>:<secret> before starting the bridge.",
        file=sys.stderr,
    )
    sys.exit(2)

HEADERS = {
    "Authorization": f"Bearer {A2A_TOKEN}",
    "Content-Type": "application/json",
}

mcp = FastMCP("zugabot-bridge")


def _parse_json_or_raise(r: httpx.Response, what: str) -> Any:
    """r.json() with a clear error instead of a bare JSONDecodeError -- a
    wrong ZUGABOT_A2A_URL (e.g. a product frontend that 200s with an HTML
    shell instead of the A2A endpoint) fails silently otherwise."""
    try:
        return r.json()
    except ValueError:
        snippet = r.text[:120].replace("\n", " ")
        raise RuntimeError(
            f"{what} at {r.request.url} returned non-JSON (content-type="
            f"{r.headers.get('content-type', '?')}): {snippet!r}... "
            f"Check ZUGABOT_A2A_URL points at an actual Zugabot A2A endpoint, "
            f"not a web frontend."
        ) from None


async def _a2a_rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """POST one JSON-RPC call and return the parsed result. Raises on RPC error."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with httpx.AsyncClient(timeout=300.0) as http:
        r = await http.post(f"{A2A_URL}/a2a", json=payload, headers=HEADERS)
    if r.status_code == 401:
        raise RuntimeError("Zugabot auth rejected — check ZUGABOT_A2A_TOKEN")
    r.raise_for_status()
    body = _parse_json_or_raise(r, "A2A endpoint")
    if body.get("error"):
        e = body["error"]
        raise RuntimeError(f"A2A error {e['code']}: {e['message']}")
    return body["result"]


def _extract_text(task: dict[str, Any]) -> str:
    """Flatten artifact text parts into a single string for the MCP client."""
    chunks: list[str] = []
    for art in task.get("artifacts", []):
        for part in art.get("parts", []):
            if part.get("kind") == "text" and part.get("text"):
                chunks.append(part["text"])
    if not chunks and task.get("status", {}).get("state") == "failed":
        msg = task.get("status", {}).get("message", {})
        for part in msg.get("parts", []):
            if part.get("kind") == "text":
                chunks.append(f"[task failed] {part.get('text', '')}")
    return "\n".join(chunks) or f"(no text output; state={task.get('status', {}).get('state')})"


@mcp.tool()
async def delegate_to_zugabot(skill: str, prompt: str) -> str:
    """Delegate a task to Zugabot via A2A.

    Args:
        skill: Agent-Card skill id (e.g. "run_zug_command"). Fetch the card with
            `list_zugabot_skills` to see what's available.
        prompt: Free-text input passed as the user message.
    """
    task = await _a2a_rpc(
        "SendMessage",
        {
            "skill": skill,
            "message": {"role": "user", "parts": [{"kind": "text", "text": prompt}]},
        },
    )
    return _extract_text(task)


@mcp.tool()
async def list_zugabot_skills() -> str:
    """Fetch Zugabot's Agent Card and return the available skills as markdown."""
    async with httpx.AsyncClient(timeout=30.0) as http:
        r = await http.get(f"{A2A_URL}/.well-known/agent.json")
    r.raise_for_status()
    card = _parse_json_or_raise(r, "Agent Card")
    lines = [f"# {card.get('name', 'Zugabot')} v{card.get('version', '?')}", ""]
    for s in card.get("skills", []):
        lines.append(f"- **{s['id']}** — {s.get('description', '')}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
