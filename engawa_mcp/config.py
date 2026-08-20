"""Runtime configuration, all overridable via environment variables.

engawa is stateless by design; the only thing it ever writes to disk lives
under CACHE_DIR (a poem-API token and the user layer of the feed shelf).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOST = os.getenv("ENGAWA_HOST", "127.0.0.1")
PORT = int(os.getenv("ENGAWA_PORT", "8799"))

# Observer location for sky_tonight. Defaults to the Royal Observatory,
# Greenwich — set your own coordinates to get meaningful rise/set times.
LAT = float(os.getenv("ENGAWA_LAT", "51.4769"))
LON = float(os.getenv("ENGAWA_LON", "-0.0005"))

# NASA APOD works out of the box with DEMO_KEY (rate-limited to ~50 req/day).
# Get a free personal key at https://api.nasa.gov if you need more.
NASA_API_KEY = os.getenv("NASA_API_KEY", "DEMO_KEY")

# Escape hatch for people who *want* to read feeds on their own LAN.
# Off by default: with it off, engawa refuses to fetch anything that does not
# resolve to a public unicast address (SSRF guard).
ALLOW_PRIVATE_URLS = os.getenv("ENGAWA_ALLOW_PRIVATE_URLS", "") == "1"


def _default_cache_dir() -> Path:
    xdg = os.getenv("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "engawa-mcp"


CACHE_DIR = Path(os.getenv("ENGAWA_CACHE_DIR", str(_default_cache_dir())))


def local_tz() -> timezone:
    """Timezone used for sky_tonight and "today" semantics.

    ENGAWA_TZ_OFFSET (hours, e.g. "9" or "-5.5") wins if set; otherwise the
    system's local timezone.
    """
    raw = os.getenv("ENGAWA_TZ_OFFSET", "")
    if raw:
        return timezone(timedelta(hours=float(raw)))
    tz = datetime.now().astimezone().tzinfo
    if isinstance(tz, timezone):
        return tz
    # Non-fixed tzinfo (zoneinfo etc.) — freeze the current UTC offset, which
    # is all the almanac formatting needs.
    offset = datetime.now().astimezone().utcoffset() or timedelta(0)
    return timezone(offset)
