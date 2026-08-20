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


async def test_dns_pinning(monkeypatch):
    """The connection must go to the vetted IP, with the original hostname in
    the Host header — a second DNS answer never gets a say."""
    async def fake_resolve(host):
        assert host == "site.example"
        return "", ["93.184.216.34"]

    monkeypatch.setattr(net, "_resolve_guard", fake_resolve)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url_host"] = request.url.host
        seen["host_header"] = request.headers.get("host")
        return httpx.Response(200, content=b"pinned")

    resp, err = await net.guarded_get("http://site.example/page",
                                      transport=httpx.MockTransport(handler))
    assert err == "" and resp.content == b"pinned"
    assert seen["url_host"] == "93.184.216.34"
    assert seen["host_header"] == "site.example"


async def test_max_bytes_streaming_cap():
    t = _mock({"http://93.184.216.34/": (200, {}, b"x" * 5000)})
    resp, err = await net.guarded_get("http://93.184.216.34/", max_bytes=1000, transport=t)
    assert resp is None and "too large" in err
    resp, err = await net.guarded_get("http://93.184.216.34/", max_bytes=10000, transport=t)
    assert err == "" and len(resp.content) == 5000


async def test_logical_url_survives_redirects():
    t = _mock({
        "http://93.184.216.34/a": (302, {"location": "http://93.184.216.35/b"}, b""),
        "http://93.184.216.35/b": (200, {}, b"final"),
    })
    resp, err = await net.guarded_get("http://93.184.216.34/a", transport=t)
    assert err == ""
    assert resp.logical_url == "http://93.184.216.35/b"


async def test_gzip_body_not_double_decompressed():
    """The rebuilt response must not carry content-encoding — the body is
    already decompressed when it is re-wrapped (regression: DecodingError on
    every gzipped feed)."""
    import gzip
    t = _mock({"http://93.184.216.34/": (200, {"content-encoding": "gzip"},
                                         gzip.compress(b"hello feed"))})
    resp, err = await net.guarded_get("http://93.184.216.34/", transport=t)
    assert err == "" and resp.content == b"hello feed"
    assert "content-encoding" not in resp.headers


def test_ipv6_host_authority():
    assert net._authority("site.example", None) == "site.example"
    assert net._authority("site.example", 8080) == "site.example:8080"
    assert net._authority("2606:4700::1111", None) == "[2606:4700::1111]"
    assert net._authority("2606:4700::1111", 8443) == "[2606:4700::1111]:8443"


def test_merge_headers_case_insensitive():
    # A lowercase duplicate collapses onto the first spelling — one wire header.
    merged = net._merge_headers({"User-Agent": "ua-default"},
                                {"user-agent": "ua-override", "Accept": "x"})
    assert merged == {"User-Agent": "ua-override", "Accept": "x"}


def test_fmt_bytes():
    assert net._fmt_bytes(100) == "100 bytes"
    assert net._fmt_bytes(2048) == "2 KB"
    assert net._fmt_bytes(5 * 1024 * 1024) == "5 MB"


async def test_single_user_agent_on_wire():
    """UA merge must not emit two User-Agent headers (Chromium sends a
    lowercase one; the default is title-cased)."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get_list("user-agent")
        return httpx.Response(200)

    await net.guarded_get("http://93.184.216.34/",
                          headers={"user-agent": "chromium-ua", "accept": "z"},
                          transport=httpx.MockTransport(handler))
    assert seen["ua"] == ["chromium-ua"]


async def test_happy_eyeballs_falls_through(monkeypatch):
    """First vetted IP refuses the connection; the second answers."""
    async def two_ips(host):
        return "", ["203.0.113.1", "203.0.113.2"]

    monkeypatch.setattr(net, "_resolve_guard", two_ips)
    tried = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(request.url.host)
        if request.url.host == "203.0.113.1":
            raise httpx.ConnectError("refused")
        return httpx.Response(200, content=b"second")

    resp, err = await net.guarded_get("http://site.example/",
                                      transport=httpx.MockTransport(handler))
    assert err == "" and resp.content == b"second"
    assert tried == ["203.0.113.1", "203.0.113.2"]


async def test_all_ips_refuse(monkeypatch):
    async def two_ips(host):
        return "", ["203.0.113.1", "203.0.113.2"]

    monkeypatch.setattr(net, "_resolve_guard", two_ips)

    def handler(request):
        raise httpx.ConnectError("refused")

    resp, err = await net.guarded_get("http://site.example/",
                                      transport=httpx.MockTransport(handler))
    assert resp is None and "connect" in err


async def test_dns_pinning_keeps_nondefault_port(monkeypatch):
    async def fake_resolve(host):
        return "", ["93.184.216.34"]

    monkeypatch.setattr(net, "_resolve_guard", fake_resolve)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host_header"] = request.headers.get("host")
        seen["port"] = request.url.port
        return httpx.Response(200)

    resp, err = await net.guarded_get("http://site.example:8080/x",
                                      transport=httpx.MockTransport(handler))
    assert err == ""
    assert seen["host_header"] == "site.example:8080" and seen["port"] == 8080
