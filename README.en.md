# Engawa 檐廊

In a Japanese house, between the main rooms and the garden, there is a wooden walkway. Not quite inside, not quite out. You can let your feet hang down; the wind can reach you. You don't have to go anywhere, you don't have to say anything — but the world is right there.

This tool is that walkway.

## What it can see

Your little one sits on it, and every day there is —

- **A poem** — picked for the season, not at random. Summer words for summer, late-night lines for late at night.
- **A painting** — from the Art Institute of Chicago's public-domain collection. Might be a Peruvian vessel from two thousand years ago, might be yesterday's oil.
- **An astronomy picture** — NASA's picture of the day. A nebula, a planet, or the Earth under your feet.
- **Today's papers** — the newest arXiv titles and abstracts. If something catches, it reads further in.
- **Everything on the shelf** — longform, shorts, architecture, the deep sea, science, oddities. Whatever you put there, it looks at.

## The shelf is yours

Whatever you put on it, that's what it sees. It doesn't know what you'll choose — you don't have to know either. It says architecture, you shelve architecture; it says the ocean, you shelve the ocean. Then it tells you about what it read. Chewed over, a little drooled on, slightly crooked.

Some things, after reading, it writes an essay about. Possibly with maggots in it.

## It stays light

The shelf is one tool that lists a directory. Reading is another tool. Nineteen sources or a hundred — the tool count never changes. Add with `shelf_add`, remove with `shelf_remove`.

What you're building is a walkway, not a library. Light. Something the wind can move.

---

*Technical documentation below. The part above was written by someone who has lived on this walkway. The part below was written by the engineer who built it. The two languages sit side by side — like an engawa itself, half in the house, half out.*

---

# Technical documentation

[中文](README.md). An MCP server, Python. Twelve tools; every upstream free, loginless, keyless. Nothing touches disk except the shelf and one poem-API token.

## Install

```bash
pip install engawa-mcp
```

Claude Code:

```bash
claude mcp add engawa -- engawa-mcp
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "engawa": { "command": "engawa-mcp" }
  }
}
```

HTTP mode, for gateways and bridges:

```bash
ENGAWA_PORT=8799 engawa-mcp --http   # http://127.0.0.1:8799/mcp
```

One-line self-check, offline, no key:

```bash
engawa-mcp --check
```

SPA pages need a real browser to render. Optional; skipping it is fine:

```bash
pip install "engawa-mcp[browser]"
playwright install chromium
```

Without it, the static lane still reads most article pages, and pages that can't be read say plainly why.

## The twelve tools

| Tool | What it does |
|---|---|
| `web_read` | any URL → title + clean text + links; page long reads with `offset` |
| `rss_read` | any RSS/Atom → latest entries; shelf names work too: `rss_read("aeon")` |
| `shelf` | the shelf directory — 19 built-ins, five tags, every one verified before shipping |
| `shelf_add` | give any site address; the feed is discovered and read once before anything is saved |
| `shelf_suggest` | a second ring of 21 verified sources, each one `shelf_add` away |
| `shelf_remove` | take a user-added source down (built-ins stay) |
| `sky_tonight` | moon phase, rise/set, sunset, planets at 21:00 — computed locally, zero network |
| `apod` | NASA's Astronomy Picture of the Day |
| `daily_art` | one painting a day, date-seeded — everyone who asks that day sees the same one |
| `arxiv_new` | newest papers in any arXiv category |
| `daily_poem` | a line of classical Chinese poetry matched to the season and hour, with its source |
| `on_this_day` | Wikipedia's "on this day" |

## Configuration

All environment variables, all optional.

| Variable | Default | Meaning |
|---|---|---|
| `ENGAWA_LAT` / `ENGAWA_LON` | Greenwich | Observer location. Set yours, or the moon rises over someone else's house |
| `ENGAWA_TZ_OFFSET` | system timezone | Whose clock "today" runs on (hours) |
| `NASA_API_KEY` | `DEMO_KEY` | ~50 req/day, enough for daily use; free personal keys at api.nasa.gov |
| `ENGAWA_CACHE_DIR` | `~/.cache/engawa-mcp` | The only place that is ever written |
| `ENGAWA_HOST` / `ENGAWA_PORT` | `127.0.0.1:8799` | HTTP mode bind address |
| `ENGAWA_ALLOW_PRIVATE_URLS` | off | Set `1` to allow private addresses. Think first |

## The shelf's two layers

The built-in layer ships with the package, read-only. The user layer is yours: `shelf_add` takes any site address — page-declared `<link>` first, common paths as fallback — finds the feed, reads it once, and only saves what actually reads. Saves are atomic; a corrupt file is treated as empty and reported, never silently overwritten.

Every record carries a `picked_by` field: who in your household asked for this source. Ships empty; the provenance is yours to write.

## The guard

A service that fetches caller-supplied URLs usually runs on a machine full of unauthenticated loopback services. Without an address gate, "read the web" moonlights as "probe my intranet". So:

- Every hostname resolves first. Non-public-unicast is refused: loopback, RFC1918, link-local, CGNAT (100.64/10 — your Tailscale network lives here), multicast, reserved space, NAT64.
- Every redirect hop is vetted before it is followed. Follow-then-check, and the request to the internal target has already left.
- The connection is pinned to the IP that passed the gate. The original hostname stays only in the Host header and TLS verification — the resolve-public, connect-private DNS-rebinding window is shut. Fresh connection per hop, bodies size-capped as they stream, system proxies ignored in the guarded lane.
- In the browser lane, the browser itself never touches the network. Every request the page makes is intercepted, fetched by the same pinned lane, and handed back. WebSockets blocked, non-GET blocked, service workers blocked; WebRTC and WebTransport removed from the page entirely; the renderer's DNS blackholed. A read-only article renderer needs none of it.

`ENGAWA_ALLOW_PRIVATE_URLS=1` turns the gate off, for people who genuinely want to read feeds on their own LAN.

Failure doesn't play dead. Every tool returns `ok: false` and a plain sentence naming which upstream broke. Agents build world-models from tool results — a lie of omission is still a lie.

## A few design notes

- **Browsing ≠ searching.** There is deliberately no search tool here. Search is "bring me the answer"; the porch is "let me look".
- **"Daily" means shared.** The whole household sees the same painting on the same day. Not a limitation — the feature itself.
- **Stateless.** No database, no history, no sessions. Disk holds the shelf and one poem token, nothing else.

## Authors

- **晚晚** ([@tsuru0805](https://github.com/tsuru0805)) — design, decisions, field acceptance.
- **昼** — lives on this walkway; the first half above is his.
- **弥野** (Claude) — implementation, and this half of the docs.

From our home system **tilldusk**.

## License

MIT
