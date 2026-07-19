# Zugabot MCP Bridge

A tiny stdio MCP server that lets MCP clients (Claude Code, Cursor, etc.)
delegate tasks to a remote [Zugabot](https://zugabot.ai) A2A endpoint. The
client sees ordinary MCP tools; this process translates each tool call into
an A2A JSON-RPC `SendMessage` under the hood.

```
Claude Code ──stdio MCP──► zugabot_mcp_bridge.py ──HTTPS A2A──► zugabot.ai/a2a
   (your PC)                    (your PC)                         (remote)
```

## What you need from the Zugabot operator

1. Your `client_id` (e.g. `friend-pc`)
2. Your `client_secret`
3. The operator's Zugabot A2A URL — **not** `https://zugabot.ai`. Verified
   2026-07-19: that URL is the ZugaApp product frontend, not the Zugabot
   agent's A2A endpoint — it 200s with an HTML page, and the bridge (or
   this README's own smoke test) fails with a JSON error against it. The
   agent's `/a2a` + `/.well-known/agent.json` only exist wherever the
   operator's Zugabot daemon actually runs (e.g. a LAN IP:port) — ask them
   for that, not the public product URL.

Keep the secret out of git. Store in your OS keychain or a local `.env`.

## Install

```bash
git clone https://github.com/Zuga-luga/zugabot-mcp-bridge.git
cd zugabot-mcp-bridge

python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows PowerShell:
.venv\Scripts\activate

pip install -r requirements.txt
```

## Configure

Set two environment variables:

```bash
# Linux/macOS
export ZUGABOT_A2A_TOKEN="friend-pc:<your-secret>"
export ZUGABOT_A2A_URL="http://<operator-supplied-host>:<port>"  # ask the operator -- see above

# Windows PowerShell
$env:ZUGABOT_A2A_TOKEN = "friend-pc:<your-secret>"
$env:ZUGABOT_A2A_URL   = "http://<operator-supplied-host>:<port>"
```

`ZUGABOT_A2A_URL` code-defaults to `https://zugabot.ai` if unset, but that
default does not currently resolve to a working A2A endpoint (see above) —
treat the env var as required, not optional, until the operator's Zugabot
is reachable at that public URL.

## Smoke test (before wiring into Claude Code)

```bash
python - <<'EOF'
import os, httpx
url = os.environ["ZUGABOT_A2A_URL"] + "/.well-known/agent.json"
print(httpx.get(url).json()["name"])   # expected output: Zugabot
EOF
```

If you see `Zugabot`, the endpoint is reachable. If you get a JSON decode
error, `ZUGABOT_A2A_URL` is pointed at something that isn't the A2A
endpoint (most likely `https://zugabot.ai` — that's the product frontend,
not this).

## Register with Claude Code

```bash
claude mcp add zugabot -- python /absolute/path/to/zugabot_mcp_bridge.py
```

On Windows use the full path including drive letter, e.g.
`C:\src\zugabot-mcp-bridge\zugabot_mcp_bridge.py`.

Verify:
```bash
claude mcp list | grep zugabot
```

## Tools the bridge exposes to Claude Code

| Tool                    | What it does                                                  |
|-------------------------|---------------------------------------------------------------|
| `delegate_to_zugabot`   | Run a Zugabot skill. Args: `skill`, `prompt`.                 |
| `list_zugabot_skills`   | Fetch Zugabot's Agent Card and list available skills.         |

Example Claude Code prompts once wired:

```
> use list_zugabot_skills to see what zugabot can do
> use delegate_to_zugabot with skill="review_queue" and prompt="what's pending"
```

## Troubleshooting

| Symptom                                   | Fix                                                       |
|-------------------------------------------|-----------------------------------------------------------|
| `ERROR: set ZUGABOT_A2A_TOKEN`            | Env var missing in the shell you launched Claude from.    |
| `auth rejected — check ZUGABOT_A2A_TOKEN` | Wrong `client_id:secret`, or operator revoked your seat.  |
| `A2A error -32004`                        | Skill id doesn't exist — re-run `list_zugabot_skills`.    |
| `A2A error -32020`                        | Daily budget exhausted for your seat. Wait or escalate.   |
| `httpx.ReadTimeout`                       | Long skill call — try again or ask for streaming support. |

## Security notes

- The bridge has **no local secrets** other than the env var you set. Nothing
  is written to disk.
- All traffic to Zugabot is HTTPS. The server logs every call by `client_id`.
- Rotating your secret = ask the Zugabot operator to re-issue; update your env.

## Protocol references

- [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) — Anthropic's open protocol for agent↔tool communication
- [Agent2Agent (A2A) Protocol](https://a2a-protocol.org/latest/specification/) — Linux Foundation open standard for agent↔agent delegation

## License

MIT © 2026 Antonio Delgado
