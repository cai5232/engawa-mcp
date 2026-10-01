# Hooking up the porch

## Claude Code

```bash
pip install git+https://github.com/tsuru0805/engawa-mcp
claude mcp add engawa -- engawa-mcp
```

Then ask your Claude anything porch-shaped: *"what's the moon doing tonight?"*,
*"read me something from the shelf"*, *"today's painting?"*

## Claude Desktop

`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "engawa": {
      "command": "engawa-mcp",
      "env": {
        "ENGAWA_LAT": "35.6762",
        "ENGAWA_LON": "139.6503"
      }
    }
  }
}
```

(Those coordinates are Tokyo — put in your own so `sky_tonight` rises and sets
where you actually live.)

## HTTP mode

For gateways, bridges, or anything that speaks MCP streamable-http:

```bash
ENGAWA_PORT=8799 engawa-mcp --http
# → http://127.0.0.1:8799/mcp
```

A minimal Python client:

```python
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    async with streamable_http_client("http://127.0.0.1:8799/mcp") as (read, write, *_):
        async with ClientSession(read, write) as session:
            await session.initialize()
            sky = await session.call_tool("sky_tonight", {})
            print(sky.content[0].text)

asyncio.run(main())
```

## A daily-brief agent prompt

If you run a companion or journaling agent, a standing instruction like this
turns the porch into a morning ritual:

> Each morning, look at `sky_tonight` and `daily_art`, skim one feed from
> `shelf` that matches your mood, and mention anything that genuinely
> interested you. Don't summarize dutifully — only bring up what you'd
> actually want to talk about.
