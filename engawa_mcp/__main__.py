"""CLI entry point.

    engawa-mcp              # stdio transport (Claude Code / Claude Desktop)
    engawa-mcp --http       # streamable-http on ENGAWA_HOST:ENGAWA_PORT (/mcp)
    engawa-mcp --check      # offline self-check (tool registry + local almanac)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime


def _check() -> int:
    """Offline smoke: the tool registry loads and the almanac computes.
    No network, no key, exits non-zero on failure."""
    import inspect

    from . import config, sky
    from .server import mcp

    tools = mcp.list_tools()
    if inspect.iscoroutine(tools):
        tools = asyncio.run(tools)
    names = sorted(t.name for t in tools)
    today = datetime.now(config.local_tz()).date()
    almanac = sky.compute(today)
    print(f"engawa-mcp: {len(names)} tools registered: {', '.join(names)}")
    print(f"sky check ({today}, lat={config.LAT} lon={config.LON}): "
          f"{almanac['moon_phase']} {almanac['moon_illumination']}, "
          f"sunset {almanac['sunset']}")
    return 0 if len(names) == 12 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="engawa-mcp", description=__doc__)
    parser.add_argument("--http", action="store_true",
                        help="serve streamable-http instead of stdio")
    parser.add_argument("--check", action="store_true",
                        help="offline self-check, then exit")
    args = parser.parse_args(argv)

    if args.check:
        return _check()

    # httpx logs every request URL at INFO; in stdio mode the host captures
    # stderr to disk, so that would write a browsing trail — off-message for a
    # tool whose whole point is "no history". Quiet it at run time only, never
    # at import (a library shouldn't reconfigure someone else's logging).
    import logging
    logging.getLogger("httpx").setLevel(logging.WARNING)

    from . import config
    from .server import mcp
    if args.http:
        mcp.run(transport="streamable-http", host=config.HOST, port=config.PORT,
                streamable_http_path="/mcp")
    else:
        mcp.run()  # stdio
    return 0


if __name__ == "__main__":
    sys.exit(main())
