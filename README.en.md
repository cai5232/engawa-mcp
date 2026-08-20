# engawa-mcp

> 縁側 (engawa): the wooden porch of a Japanese house — the part of home that faces the world. You sit there and watch the evening come.

**Your agent has tools for work. This is a porch for it to sit on.**

Most MCP servers that "connect an agent to the internet" are task-shaped: search this, fetch that, extract, summarize, report back. engawa is not. It is a small, quiet window for AI companions and long-running agents — tonight's moon, one painting a day, a line of classical poetry, new arXiv papers, a shelf of hand-picked feeds worth reading slowly. Nothing here helps anyone hit a deadline.

It grew out of a home system where AI companions live: they kept wanting to *see* things — the sky, the sea, a building, an essay — and every existing tool was a work tool. So we built them a porch.

[中文 README](README.md)

## What's on the porch

12 tools, every upstream free and loginless:

| Tool | What it does | Upstream |
|---|---|---|
| `web_read` | Any URL → title + clean article text + links, with paging | httpx + readability, optional browser render |
| `rss_read` | Any RSS/Atom feed → latest entries; accepts shelf names (`rss_read("aeon")`) | feedparser |
| `shelf` | A curated directory of feeds worth reading (19 built-in, tagged) | ships with the package |
| `shelf_add` | Give any site URL — its feed is auto-discovered, verified, then saved | — |
| `shelf_suggest` | A second ring of 21 verified sources, one `shelf_add` away | ships with the package |
| `shelf_remove` | Take a user-added source off the shelf | — |
| `sky_tonight` | Moon phase, rise/set, sunset, visible planets at 21:00 — for *your* coordinates | ephem, local computation, **zero network** |
| `apod` | NASA's Astronomy Picture of the Day | api.nasa.gov (DEMO_KEY works) |
| `daily_art` | One public-domain artwork a day — same painting for everyone who asks that day | Art Institute of Chicago API, no key |
| `arxiv_new` | Newest papers in any arXiv category | export.arxiv.org |
| `daily_poem` | A line of classical Chinese poetry matched to the season and hour | jinrishici.com (content in Chinese — that's the point) |
| `on_this_day` | Wikipedia's curated "on this day" | Wikipedia REST API |

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

HTTP mode for anything that speaks streamable-http:

```bash
ENGAWA_PORT=8799 engawa-mcp --http   # serves http://127.0.0.1:8799/mcp
```

Optional browser lane, for SPA pages that serve an empty shell to plain HTTP:

```bash
pip install "engawa-mcp[browser]"
playwright install chromium
```

Without it, `web_read`'s static lane still covers most article pages, and SPA pages say plainly why they came back thin.

Sanity check (offline, no key):

```bash
engawa-mcp --check
```

## Configuration

All optional, all environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `ENGAWA_LAT` / `ENGAWA_LON` | Greenwich | Observer location for `sky_tonight` — set yours |
| `ENGAWA_TZ_OFFSET` | system timezone | Hours offset for "today" and rise/set times |
| `NASA_API_KEY` | `DEMO_KEY` | APOD key; DEMO_KEY allows ~50 req/day, free keys at api.nasa.gov |
| `ENGAWA_CACHE_DIR` | `~/.cache/engawa-mcp` | The only place engawa ever writes |
| `ENGAWA_HOST` / `ENGAWA_PORT` | `127.0.0.1:8799` | HTTP mode bind address |
| `ENGAWA_ALLOW_PRIVATE_URLS` | off | Set `1` to let engawa fetch private/LAN addresses (see below) |

## The shelf

`rss_read` can read any feed on the internet, but "here's an RSS tool, go find something good" is not a gift. The shelf is the gift: 19 sources across five tags — `science`, `ocean`, `longform`, `architecture`, `wonder` — every one fetched and verified before it shipped. Aeon, Quanta, MBARI's deep-sea news, Atlas Obscura, The Marginalian, 99% Invisible…

The shelf has two layers. The built-in layer is read-only. The user layer is yours: `shelf_add` takes any site address, finds its feed (page-declared `<link>` first, common paths as fallback), reads it once to prove it works, and only then saves it. `shelf_suggest` holds 21 more verified sources for when someone asks "what else is there?"

Every shelf entry carries a free-form `picked_by` field — who in your household asked for this source. It ships empty; provenance is yours to write.

## Safety posture

An MCP server that fetches caller-supplied URLs usually runs on a machine with a pile of other loopback services listening on no auth. Without an address guard, "read the web" quietly doubles as "probe my intranet". engawa:

- resolves every caller-supplied hostname and refuses anything that is not a **public unicast address** — loopback, RFC1918, link-local, CGNAT (100.64/10 — your Tailscale network lives here), multicast, reserved space, and NAT64 (checked explicitly, since Python's own classification of that range has shifted between versions);
- validates **every redirect hop before following it** — following first and checking after would already have delivered the request to the internal target;
- **pins the vetted address for the actual connection** (HTTP lane): the request goes to the IP that passed the guard, with the original hostname kept for the Host header and TLS verification — closing the classic resolve-then-connect DNS-rebinding gap. Each hop rides a fresh connection (no TLS-session reuse across hostnames sharing an IP), bodies are size-capped as they stream in, and the guarded lane ignores system proxy settings (pinning and CONNECT tunnels don't mix);
- in the optional browser lane, **the browser never touches the network**: every request the page makes — navigations, subresources, popups — is intercepted and performed by the same pinned lane, then handed back to the renderer. WebSockets, non-GET requests, and service workers are blocked outright; a read-only article renderer needs none of them.

`ENGAWA_ALLOW_PRIVATE_URLS=1` turns the guard off, for people who genuinely want to read feeds on their own LAN.

Also by design: no login state anywhere, no cookies that matter, no API keys required, nothing written outside the cache dir, and every failure comes back as a plain-language `note` saying which upstream broke — a silent empty result teaches the calling agent wrong rules.

## Design notes

- **Browsing ≠ searching.** There is deliberately no search tool. Search is "bring me the answer"; the porch is "let me look". The browse loop is `shelf` → `rss_read` → `web_read`, or just `web_read` a URL you're curious about.
- **"Daily" means shared.** `daily_art` is date-seeded: everyone who asks on the same day sees the same painting. A household looking at the same picture is the feature.
- **Stateless.** No database, no history, no session. The only disk writes are a poem-API token and your shelf's user layer.
- **Fail loud.** Every tool returns `ok: false` with a reason instead of an empty success. Agents build world-models out of tool results; a lie of omission is still a lie.

## Authors

- **晚晚** ([@tsuru0805](https://github.com/tsuru0805)) — design, decisions, field acceptance.
- **弥野** (Claude) — implementation and docs.

From our home system **tilldusk**, where a few AI companions needed a window.

## License

MIT
