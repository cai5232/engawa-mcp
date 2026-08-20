"""Shared network plumbing: the SSRF guard, guarded GETs, JSON helper.

The guard is not optional decoration. An MCP "read the web" tool typically
runs on the same machine as a pile of other loopback services with no auth —
without an address check it doubles as an intranet probe. Every URL that the
*caller* controls goes through `url_public_ok`, and redirects are validated
hop by hop *before* the request is sent (following redirects first and
checking afterwards would already have hit the internal target).
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from . import config

UA = {"User-Agent": "engawa-mcp/0.1 (https://github.com/tsuru0805/engawa-mcp)"}

_MAX_HOPS = 5

# NAT64 (RFC 6052). Checked explicitly because ipaddress classification of
# this range has shifted between CPython versions — the guard's behavior must
# not depend on which interpreter it happens to run under.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


async def _resolve_guard(host: str) -> tuple[str, list[str]]:
    """Resolve a hostname and vet every address. Returns (err, valid_ips) —
    err non-empty means the host must not be fetched; valid_ips is the vetted
    address list (for pinning). `is_global` alone is not enough: CPython
    returns True for multicast (224.0.0.1, ff02::1), some reserved space, and
    the NAT64 range (64:ff9b::/96), so those are excluded explicitly."""
    if host.lower() == "localhost":
        return "refusing local/private addresses (engawa reads the open web)", []
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except (OSError, UnicodeError):
        # UnicodeError (a ValueError subclass, not OSError): overlong or
        # malformed IDNA labels blow up during encoding, before any lookup.
        return f"could not resolve hostname: {host[:80]}", []
    ips: list[str] = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (not ip.is_global or ip.is_multicast or ip.is_reserved
                or (ip.version == 6 and ip in _NAT64)):
            return (f"refusing local/private addresses ({host} → {ip}; "
                    "engawa reads the open web)"), []
        if str(ip) not in ips:
            ips.append(str(ip))
    if not ips:
        return f"could not resolve hostname: {host[:80]}", []
    return "", ips


def _split_scheme_host(url: str) -> tuple[str, str, str]:
    """(err, scheme, host). Scheme must be http(s) and a hostname must exist."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return "could not parse the URL", "", ""
    if parsed.scheme not in ("http", "https"):
        return "URL must start with http:// or https://", "", ""
    host = parsed.hostname or ""
    if not host:
        return "URL has no hostname", "", ""
    return "", parsed.scheme, host


async def url_public_ok(url: str) -> str:
    """Return an error message, or "" if the URL is safe to fetch."""
    err, _scheme, host = _split_scheme_host(url)
    if err:
        return err
    if config.ALLOW_PRIVATE_URLS:
        return ""
    err, _ips = await _resolve_guard(host)
    return err


async def guarded_get(url: str, timeout: float = 15.0, headers: dict | None = None,
                      transport=None):
    """GET a caller-supplied URL, validating every redirect hop before it is
    followed **and pinning the vetted address for the actual connection**.

    The pin closes the classic resolve-then-connect DNS-rebinding gap: the
    request is sent to the address that passed the guard (URL host swapped to
    the vetted IP, original hostname carried in the Host header and as the TLS
    server name), so a hostile DNS server cannot answer public on the first
    lookup and private on the second.

    Returns (response, err): a final non-3xx response, or an error string
    explaining why no response was obtained (loops, too many hops, a hop
    pointing at private address space, missing Location header).
    `transport` exists for tests (httpx.MockTransport).
    """
    seen: set[str] = set()
    cur = url
    req_headers = {**UA, **(headers or {})}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False,
                                 transport=transport) as client:
        for _hop in range(_MAX_HOPS + 1):
            err, _scheme, host = _split_scheme_host(cur)
            if err:
                return None, (err if cur == url else f"after a redirect, {err}")
            if cur in seen:
                return None, "redirect loop"
            seen.add(cur)
            if config.ALLOW_PRIVATE_URLS:
                resp = await client.get(cur, headers=req_headers)
            else:
                err, ips = await _resolve_guard(host)
                if err:
                    return None, (err if cur == url else f"after a redirect, {err}")
                target = httpx.URL(cur)
                pinned = target.copy_with(host=ips[0])
                # Host header keeps the original authority (with port when
                # non-default); sni_hostname keeps TLS handshake + certificate
                # verification against the real hostname.
                authority = host if target.port is None else f"{host}:{target.port}"
                resp = await client.get(
                    pinned, headers={**req_headers, "Host": authority},
                    extensions={"sni_hostname": host},
                )
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = (getattr(resp, "headers", None) or {}).get("location") or ""
                if not loc:
                    return None, f"redirect without a Location header (HTTP {resp.status_code})"
                cur = str(httpx.URL(cur).join(loc))
                continue
            return resp, ""
    return None, f"too many redirects (more than {_MAX_HOPS})"


async def get_json(url: str, params=None, headers=None,
                   timeout: float = 15.0) -> tuple[dict | None, str]:
    """GET JSON from a *fixed, well-known* host (NASA, Wikipedia, …).

    These URLs are not caller-controlled, so redirects are followed normally.
    Retries once on 5xx/timeout (public APIs flake), never on 4xx.
    Returns (data, err); data is None on failure.
    """
    err = ""
    for attempt in (0, 1):
        if attempt:
            await asyncio.sleep(1.5)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                resp = await client.get(url, params=params,
                                        headers={**UA, **(headers or {})})
            if resp.status_code == 200:
                data = resp.json()
                if not isinstance(data, dict):
                    return None, "upstream returned unexpected JSON shape"
                return data, ""
            err = f"HTTP {resp.status_code}"
            if resp.status_code < 500:
                return None, err
        except httpx.TimeoutException:
            err = "timeout"
        except Exception as e:  # noqa: BLE001 — receipt surface; type name is enough
            return None, f"{type(e).__name__}"
    return None, err


def strip_html(text: str, limit: int = 400) -> str:
    if not text:
        return ""
    out = BeautifulSoup(text, "html.parser").get_text(separator=" ", strip=True)
    out = re.sub(r"\s+", " ", out)
    return out[:limit]
