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

import json
import os
import sys
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP


DEFAULT_A2A_URL = "https://zugabot.ai"

# How long to wait on each call. A2A skills can be genuinely slow (a queued
# render is minutes), the Agent Card is a static document.
A2A_TIMEOUT_S = 300.0
CARD_TIMEOUT_S = 30.0

_raw_url = os.environ.get("ZUGABOT_A2A_URL", "").strip()
A2A_URL = (_raw_url or DEFAULT_A2A_URL).rstrip("/")
A2A_TOKEN = os.environ.get("ZUGABOT_A2A_TOKEN", "").strip()

if not A2A_TOKEN:
    print(
        "ERROR: set ZUGABOT_A2A_TOKEN=<client_id>:<secret> before starting the bridge.",
        file=sys.stderr,
    )
    sys.exit(2)

# The token is read from the environment, so an unexpanded `${VAR}` placeholder
# (e.g. copied verbatim out of an .mcp.json env block that was never
# substituted) arrives here as a literal string and produces a 401 on the first
# delegate call -- while `list_zugabot_skills` keeps working, because the Agent
# Card is unauthenticated. That asymmetry is confusing enough to be worth
# naming at startup rather than leaving to be discovered mid-task.
TOKEN_IS_PLACEHOLDER = A2A_TOKEN.startswith("${") and A2A_TOKEN.endswith("}")
if TOKEN_IS_PLACEHOLDER:
    # Warn, do NOT exit: the Agent Card is unauthenticated, so
    # `list_zugabot_skills` still works and is still worth serving. Only the
    # authenticated delegate path is doomed, and it says so when called.
    print(
        f"WARNING: ZUGABOT_A2A_TOKEN is the literal placeholder {A2A_TOKEN!r} -- the\n"
        "variable was never substituted, so every delegate_to_zugabot call will 401.\n"
        "Export the real value in the environment that launches this bridge.\n"
        "list_zugabot_skills will still work (the Agent Card is unauthenticated).",
        file=sys.stderr,
    )

if not _raw_url:
    print(
        f"WARNING: ZUGABOT_A2A_URL is unset, falling back to {DEFAULT_A2A_URL}, "
        "which serves the product\nfrontend (HTML), not the A2A endpoint. Set it "
        "to the host actually running the Zugabot\ndaemon, e.g. http://<host>:8000.",
        file=sys.stderr,
    )

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
    try:
        async with httpx.AsyncClient(timeout=A2A_TIMEOUT_S) as http:
            r = await http.post(f"{A2A_URL}/a2a", json=payload, headers=HEADERS)
    except httpx.TimeoutException:
        raise RuntimeError(
            f"Zugabot did not answer within {A2A_TIMEOUT_S:.0f}s at {A2A_URL}/a2a. "
            "The skill may still be running on the server — check its queue "
            "before re-sending, so the work is not done twice."
        ) from None
    except httpx.RequestError as exc:
        raise RuntimeError(
            f"Cannot reach Zugabot at {A2A_URL}/a2a ({type(exc).__name__}). "
            "Check ZUGABOT_A2A_URL and that the Zugabot backend is running."
        ) from None

    if r.status_code == 401:
        if TOKEN_IS_PLACEHOLDER:
            raise RuntimeError(
                "Zugabot auth rejected — ZUGABOT_A2A_TOKEN is the unexpanded "
                f"placeholder {A2A_TOKEN!r}, not a real credential. Export the "
                "real value in the environment that launches this bridge; "
                "list_zugabot_skills works without it because the Agent Card "
                "is unauthenticated, which is why this failure looks selective."
            )
        raise RuntimeError("Zugabot auth rejected — check ZUGABOT_A2A_TOKEN")
    if r.status_code >= 400:
        raise RuntimeError(
            f"A2A endpoint returned HTTP {r.status_code} for {A2A_URL}/a2a: "
            f"{r.text[:200]!r}"
        )

    body = _parse_json_or_raise(r, "A2A endpoint")
    if not isinstance(body, dict):
        raise RuntimeError(
            f"A2A endpoint returned {type(body).__name__}, expected a JSON-RPC object: "
            f"{str(body)[:200]!r}"
        )
    if body.get("error"):
        # A malformed error object must not turn into a bare KeyError -- that
        # hides the real server-side problem behind an unrelated crash.
        err = body["error"]
        if isinstance(err, dict):
            code = err.get("code", "?")
            message = err.get("message") or json.dumps(err)[:200]
        else:
            code, message = "?", str(err)[:200]
        raise RuntimeError(f"A2A error {code}: {message}")
    if "result" not in body:
        raise RuntimeError(
            "A2A response contained neither 'error' nor 'result' — the endpoint "
            f"is not speaking JSON-RPC 2.0: {json.dumps(body)[:200]}"
        )
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
    url = f"{A2A_URL}/.well-known/agent.json"
    try:
        async with httpx.AsyncClient(timeout=CARD_TIMEOUT_S) as http:
            r = await http.get(url)
    except httpx.TimeoutException:
        raise RuntimeError(
            f"Agent Card fetch timed out after {CARD_TIMEOUT_S:.0f}s at {url}."
        ) from None
    except httpx.RequestError as exc:
        raise RuntimeError(
            f"Cannot reach Zugabot at {url} ({type(exc).__name__}). Check "
            "ZUGABOT_A2A_URL and that the Zugabot backend is running."
        ) from None

    if r.status_code >= 400:
        raise RuntimeError(
            f"Agent Card fetch returned HTTP {r.status_code} for {url}: {r.text[:200]!r}"
        )

    card = _parse_json_or_raise(r, "Agent Card")
    if not isinstance(card, dict):
        raise RuntimeError(
            f"Agent Card was {type(card).__name__}, expected an object: {str(card)[:200]!r}"
        )
    lines = [f"# {card.get('name', 'Zugabot')} v{card.get('version', '?')}", ""]
    for s in card.get("skills", []) or []:
        if not isinstance(s, dict):
            continue
        # A skill entry with no id is unusable but must not crash the listing --
        # the other skills are still worth returning.
        skill_id = s.get("id")
        if not skill_id:
            continue
        lines.append(f"- **{skill_id}** — {s.get('description', '')}")
    return "\n".join(lines)


async def _library_get(route: str, params: dict[str, Any]) -> Any:
    """GET one /api/library route on the Zugabot backend (same host and token as A2A)."""
    url = f"{A2A_URL}/api/library/{route}"
    try:
        async with httpx.AsyncClient(timeout=CARD_TIMEOUT_S) as http:
            r = await http.get(url, params=params, headers=HEADERS)
    except httpx.RequestError as exc:
        raise RuntimeError(
            f"Cannot reach the Library at {url} ({type(exc).__name__}). Check "
            "ZUGABOT_A2A_URL and that the Zugabot backend is running."
        ) from None
    if r.status_code == 401:
        raise RuntimeError("Library auth rejected — check ZUGABOT_A2A_TOKEN")
    if r.status_code >= 400:
        raise RuntimeError(f"Library returned HTTP {r.status_code} for {url}: {r.text[:200]!r}")
    return _parse_json_or_raise(r, "Library")


@mcp.tool()
async def library_ask(question: str, k: int = 5) -> str:
    """Ask Zugabot's Library (his Obsidian vault) a question by meaning.

    Returns the top notes, best first, each with its vault path so the answer
    can be cited. Same search chat and zugamind use.
    """
    return json.dumps(await _library_get("ask", {"q": question, "k": k}), indent=2)


@mcp.tool()
async def library_grep(pattern: str, glob: str = "**/*.md") -> str:
    """Regex search over every vault note, logs included. Returns path, line, text."""
    return json.dumps(await _library_get("grep", {"pattern": pattern, "glob": glob}), indent=2)


@mcp.tool()
async def library_find(glob: str) -> str:
    """List vault-relative note paths matching a glob, e.g. ``Atlas/*.md``."""
    return json.dumps(await _library_get("find", {"glob": glob}), indent=2)


if __name__ == "__main__":
    mcp.run()
