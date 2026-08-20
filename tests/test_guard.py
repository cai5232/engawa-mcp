"""SSRF guard: the address table is the contract.

IP-literal URLs are used throughout so getaddrinfo resolves without DNS.
"""
import httpx
import pytest

from engawa_mcp import config, net

BLOCKED = [
    "http://127.0.0.1/x",                 # loopback
    "http://10.0.0.5/",                   # RFC1918
    "http://192.168.1.1/",                # RFC1918
    "http://172.16.0.1/",                 # RFC1918
    "http://169.254.1.1/",                # link-local
    "http://100.64.1.5/",                 # CGNAT (Tailscale lives here)
    "http://224.0.0.1/",                  # multicast v4
    "http://[ff02::1]/",                  # multicast v6
    "http://[64:ff9b::102:304]/",         # NAT64
    "http://[::1]/",                      # loopback v6
    "http://localhost/",                  # name special-case
]

ALLOWED = [
    "http://93.184.216.34/",              # public v4
    "https://[2606:4700:4700::1111]/",    # public v6
]


@pytest.mark.parametrize("url", BLOCKED)
async def test_blocked(url):
    assert await net.url_public_ok(url) != ""


@pytest.mark.parametrize("url", ALLOWED)
async def test_allowed(url):
    assert await net.url_public_ok(url) == ""


async def test_scheme_and_host_errors():
    assert "http://" in await net.url_public_ok("ftp://example.com/")
    assert await net.url_public_ok("http:///nohost") != ""


async def test_idna_overlong_label_does_not_crash():
    err = await net.url_public_ok("http://" + "a" * 300 + ".example/")
    assert "resolve" in err


async def test_allow_private_escape_hatch(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_PRIVATE_URLS", True)
    assert await net.url_public_ok("http://127.0.0.1/x") == ""


# ── guarded_get: redirects validated hop by hop ─────────────────────────

def _mock(routes):
    """routes: {url_prefix: (status, headers, body)} — first prefix match wins."""
    def handler(request: httpx.Request) -> httpx.Response:
        for prefix, (status, headers, body) in routes.items():
            if str(request.url).startswith(prefix):
                return httpx.Response(status, headers=headers, content=body)
        return httpx.Response(404)
    return httpx.MockTransport(handler)


async def test_redirect_to_private_is_refused_before_fetch():
    hits = []

    def handler(request):
        hits.append(str(request.url))
        if "93.184.216.34" in str(request.url):
            return httpx.Response(302, headers={"location": "http://10.0.0.1/secret"})
        return httpx.Response(200, content=b"leaked")

    resp, err = await net.guarded_get("http://93.184.216.34/a",
                                      transport=httpx.MockTransport(handler))
    assert resp is None
    assert "redirect" in err
    # The private hop must never have been requested.
    assert all("10.0.0.1" not in h for h in hits)


async def test_redirect_loop_detected():
    t = _mock({
        "http://93.184.216.34/a": (302, {"location": "http://93.184.216.34/b"}, b""),
        "http://93.184.216.34/b": (302, {"location": "http://93.184.216.34/a"}, b""),
    })
    resp, err = await net.guarded_get("http://93.184.216.34/a", transport=t)
    assert resp is None and "loop" in err


async def test_redirect_without_location():
    t = _mock({"http://93.184.216.34/": (302, {}, b"")})
    resp, err = await net.guarded_get("http://93.184.216.34/", transport=t)
    assert resp is None and "Location" in err


async def test_plain_200_passes_through():
    t = _mock({"http://93.184.216.34/": (200, {}, b"hello")})
    resp, err = await net.guarded_get("http://93.184.216.34/", transport=t)
    assert err == "" and resp.status_code == 200 and resp.content == b"hello"
