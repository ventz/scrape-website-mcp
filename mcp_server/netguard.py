"""SSRF guard for every outbound fetch the server makes on a caller's behalf.

Upstream's ``_is_safe_fetch_target`` only checks IP literals in crawled links
(it treats the seed as trusted operator input). A network service can't make
that assumption: anyone holding the bearer token could point the server at
``http://169.254.169.254/`` (cloud metadata), ``169.254.170.2`` (ECS task
credentials), ``localhost`` or internal VPC hosts and read the response back
as Markdown. This module blocks those targets at every layer the engine uses:

- **Caller URL:** :func:`check_url` resolves the host and rejects the fetch
  before any request is sent (fast, clear error, no retries).
- **aiohttp tier, every redirect hop:** a resolver wrapper rejects any DNS
  answer that contains a blocked IP (this is also what defeats DNS rebinding,
  since the checked answer is the one the connection uses), and a client
  middleware rejects IP-literal hops and non-http(s) schemes.
- **curl_cffi tier:** redirects are followed manually; each hop is resolved,
  checked, and pinned with ``CURLOPT_RESOLVE`` so curl connects to the IP
  that was checked.
- **Headless Chromium:** a ``page.route`` handler aborts any request (the
  navigation and every subresource/XHR) whose host is blocked, and the final
  page URL is re-checked before the DOM is returned.
- **Sitemap fetches** (stdlib ``urllib`` inside upstream): ``urlopen`` is
  wrapped so the root sitemap and every redirect hop are checked.

Residual risk, documented rather than hidden: Chromium resolves DNS itself, so
a rebinding answer that flips between our check and Chromium's lookup is not
caught for rendered subresources; redirects inside the browser are followed
by Chromium without re-routing (cross-origin reads are still CORS-blocked, and
the final URL is re-checked). The urllib sitemap path has the same
check-then-connect window. Sitemap and robots responses are never returned to
the caller verbatim.

Gate: ``SCRAPER_ALLOW_PRIVATE_TARGETS=1`` disables all of the above (for
operators who deliberately scrape intranet hosts).
"""

from __future__ import annotations

import asyncio
import errno
import ipaddress
import logging
import socket
import ssl
import urllib.request
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

from scrape_website.fetch import FetchEngine, should_download_file
from scrape_website.waf import CF_SESSION

from mcp_server import settings

log = logging.getLogger(__name__)

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_MAX_REDIRECTS = 10


class BlockedTargetError(Exception):
    """The URL (or a redirect hop) points at a private/internal address."""


IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def is_blocked_ip(ip: str | IPAddress) -> bool:
    """True for anything that isn't a plain public unicast address:
    private, loopback, link-local (incl. 169.254.169.254 / 169.254.170.2),
    CGNAT 100.64/10, reserved, multicast, unspecified, documentation and
    benchmarking ranges. IPv4 addresses embedded in IPv6 (mapped, NAT64,
    6to4) are unwrapped and checked as IPv4."""
    if isinstance(ip, str):
        try:
            ip = ipaddress.ip_address(ip.split("%", 1)[0])
        except ValueError:
            return True
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_blocked_ip(ip.ipv4_mapped)
        if ip in _NAT64:
            return is_blocked_ip(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if ip.sixtofour is not None:
            return is_blocked_ip(ip.sixtofour)
    return (not ip.is_global or ip.is_multicast or ip.is_reserved
            or ip.is_unspecified or ip.is_loopback or ip.is_link_local)


def _literal_ip(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host.strip("[]").split("%", 1)[0])
    except ValueError:
        return None


def _split_target(url: str) -> tuple[str, int]:
    """Return (host, port) after scheme/host validation."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as e:
        raise BlockedTargetError(f"invalid URL: {e}") from None
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise BlockedTargetError(f"scheme not allowed: {parts.scheme or '(none)'}")
    host = parts.hostname or ""
    if not host:
        raise BlockedTargetError("URL has no host")
    if port is None:
        port = 443 if parts.scheme.lower() == "https" else 80
    return host, port


def _check_ips(host: str, ips: list[str]) -> None:
    for ip in ips:
        if is_blocked_ip(ip):
            raise BlockedTargetError(
                f"{host} resolves to a private/internal address ({ip})")


async def resolve(host: str, port: int) -> list[str]:
    """All addresses *host* resolves to. Empty on resolution failure (the
    fetch itself will then fail with a normal DNS error)."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    return list(dict.fromkeys(info[4][0] for info in infos))


def _resolve_sync(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    return list(dict.fromkeys(info[4][0] for info in infos))


async def check_url(url: str) -> list[str]:
    """Raise :class:`BlockedTargetError` if *url* targets a blocked address.
    Returns the resolved IPs (for pinning)."""
    host, port = _split_target(url)
    literal = _literal_ip(host)
    if literal is not None:
        if is_blocked_ip(literal):
            raise BlockedTargetError(f"private/internal address not allowed ({literal})")
        return [str(literal)]
    ips = await resolve(host, port)
    _check_ips(host, ips)
    return ips


def check_url_sync(url: str) -> list[str]:
    host, port = _split_target(url)
    literal = _literal_ip(host)
    if literal is not None:
        if is_blocked_ip(literal):
            raise BlockedTargetError(f"private/internal address not allowed ({literal})")
        return [str(literal)]
    ips = _resolve_sync(host, port)
    _check_ips(host, ips)
    return ips


# ---------------------------------------------------------------------------
# aiohttp: resolver wrapper + per-hop middleware
# ---------------------------------------------------------------------------

class GuardedResolver(AbstractResolver):
    """Wraps a real resolver and refuses any answer containing a blocked IP.
    aiohttp connects to exactly the addresses returned here, so a rebinding
    DNS server can't swap in an internal IP after the check."""

    def __init__(self, inner: AbstractResolver | None = None, *,
                 is_blocked=is_blocked_ip):
        self._inner = inner or aiohttp.AsyncResolver()
        self._is_blocked = is_blocked

    async def resolve(self, host: str, port: int = 0,
                      family: socket.AddressFamily = socket.AF_INET):
        results = await self._inner.resolve(host, port, family)
        for r in results:
            if self._is_blocked(r["host"]):
                # errno + strerror: aiohttp's connector error prints strerror.
                raise OSError(
                    errno.EACCES,
                    f"blocked target: {host} resolves to a private/internal "
                    f"address ({r['host']})")
        return results

    async def close(self) -> None:
        await self._inner.close()


def _make_hop_middleware(is_blocked):
    async def _guard_hop(req, handler):
        # Runs for the initial request AND every redirect hop. Hostnames are
        # covered by GuardedResolver; this catches IP literals (which aiohttp
        # never sends through the resolver) and scheme downgrades.
        if req.url.scheme not in _ALLOWED_SCHEMES:
            raise BlockedTargetError(f"scheme not allowed: {req.url.scheme}")
        literal = _literal_ip(req.url.host or "")
        if literal is not None and is_blocked(literal):
            raise BlockedTargetError(
                f"redirect to private/internal address not allowed ({literal})")
        return await handler(req)
    return _guard_hop


# ---------------------------------------------------------------------------
# Guarded engine
# ---------------------------------------------------------------------------

class GuardedFetchEngine(FetchEngine):
    """``FetchEngine`` with the SSRF guard wired into every tier.

    ``block_private=False`` makes it behave exactly like upstream. The
    ``is_blocked`` predicate is injectable for tests."""

    def __init__(self, *, block_private: bool = True, is_blocked=is_blocked_ip,
                 **kwargs):
        super().__init__(**kwargs)
        self.block_private = block_private
        self._is_blocked = is_blocked
        self._host_verdicts: dict[str, bool] = {}

    # -- caller URL pre-check ------------------------------------------------
    async def guard_url(self, url: str) -> None:
        if not self.block_private:
            return
        host, port = _split_target(url)
        literal = _literal_ip(host)
        ips = [str(literal)] if literal is not None else await resolve(host, port)
        for ip in ips:
            if self._is_blocked(ip):
                raise BlockedTargetError(
                    f"{host} is a private/internal address ({ip})")

    async def fetch(self, url: str, method: str = "GET"):
        await self.guard_url(url)
        return await super().fetch(url, method)

    # -- aiohttp tier --------------------------------------------------------
    async def start(self):
        """Mirror of upstream ``FetchEngine.start`` with a guarded resolver
        and a per-hop middleware. Keep in sync with upstream's connector
        settings when bumping the vendored engine."""
        if not self.block_private:
            return await super().start()
        if self.session is not None:
            return
        ssl_param: ssl.SSLContext | bool = True
        if self.allow_insecure_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            ssl_param = ctx
            self.logger.warning(
                "TLS verification DISABLED for this engine (allow_insecure_tls).")
        connector = aiohttp.TCPConnector(
            limit=self.max_concurrent,
            limit_per_host=self.max_concurrent,
            resolver=GuardedResolver(is_blocked=self._is_blocked),
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
            ssl=ssl_param,
        )
        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=self.timeout),
            headers={"User-Agent": self.user_agent},
            max_field_size=32768,
            middlewares=(_make_hop_middleware(self._is_blocked),),
        )

    # -- curl_cffi tier ------------------------------------------------------
    async def _curl_get(self, url: str, replay_cookies: bool = True,
                        raw: bool = False) -> tuple | None:
        """Upstream ``_curl_get`` with manual, per-hop-checked redirects and
        DNS pinning. Same contract: a fetch tuple, or None.
        ``replay_cookies=False`` sends no cookies (robots.txt / sitemap
        fallback); ``raw=True`` keeps an 'html' body as bytes."""
        if not self.block_private:
            return await super()._curl_get(url, replay_cookies=replay_cookies, raw=raw)
        try:
            from curl_cffi import CurlOpt
            from curl_cffi.requests import AsyncSession
        except ImportError:
            return None
        hop = url
        try:
            for _ in range(_MAX_REDIRECTS + 1):
                host, port = _split_target(hop)
                literal = _literal_ip(host)
                ips = [str(literal)] if literal is not None else await resolve(host, port)
                if not ips:
                    return None
                for ip in ips:
                    if self._is_blocked(ip):
                        raise BlockedTargetError(
                            f"{host} is a private/internal address ({ip})")
                headers = {"User-Agent": self.user_agent}
                cookie = CF_SESSION.cookie_header_for(hop) if replay_cookies else None
                if cookie:
                    headers["Cookie"] = cookie
                curl_options = {}
                if literal is None:
                    pinned = ",".join(f"[{ip}]" if ":" in ip else ip for ip in ips)
                    curl_options[CurlOpt.RESOLVE] = [f"{host}:{port}:{pinned}"]
                async with AsyncSession(curl_options=curl_options) as s:
                    resp = await s.get(
                        hop, impersonate="chrome", headers=headers,
                        timeout=self.timeout,
                        verify=not self.allow_insecure_tls,
                        allow_redirects=False,
                    )
                    location = resp.headers.get("Location")
                    if resp.status_code in (301, 302, 303, 307, 308) and location:
                        hop = urljoin(hop, location)
                        continue
                    content_type = resp.headers.get("Content-Type", "")
                    status = resp.status_code
                    if should_download_file(url, content_type):
                        if len(resp.content) > self.max_file_size:
                            self.logger.info(f"Skipping large file (curl_cffi): {url}")
                            return None
                        return resp.content, content_type, "file", status
                    if len(resp.content) > self.max_page_size:
                        self.logger.info(f"Page exceeded size cap (curl_cffi): {url}")
                        return None
                    return (resp.content if raw else resp.text), content_type, "html", status
            return None  # too many redirects
        except BlockedTargetError as e:
            self.logger.info("curl_cffi hop blocked for %s: %s", url, e)
            return None
        except Exception as e:  # noqa: BLE001
            self.logger.debug(f"curl_cffi fetch failed for {url}: {e}")
            return None

    # -- headless Chromium tier ---------------------------------------------
    async def host_blocked(self, url: str) -> bool:
        """Cached per-host verdict for browser requests."""
        try:
            host, port = _split_target(url)
        except BlockedTargetError:
            # data:/blob:/about: subresources never leave the browser.
            scheme = urlsplit(url).scheme.lower()
            return scheme not in ("data", "blob", "about")
        key = f"{host}:{port}"
        verdict = self._host_verdicts.get(key)
        if verdict is None:
            literal = _literal_ip(host)
            ips = [str(literal)] if literal is not None else await resolve(host, port)
            verdict = any(self._is_blocked(ip) for ip in ips)
            self._host_verdicts[key] = verdict
        return verdict

    async def _render_page(self, url: str) -> str | None:
        """Upstream ``_render_page`` plus an SSRF route guard: page JS can't
        reach internal hosts, and a navigation that lands on one is dropped."""
        if not self.block_private:
            return await super()._render_page(url)
        page = None
        try:
            page = await self._browser.new_page(user_agent=self.user_agent)

            async def _route(route):
                req = route.request
                if req.resource_type in ("image", "media"):
                    await route.abort()
                elif await self.host_blocked(req.url):
                    self.logger.info("render: blocked request to %s", req.url)
                    await route.abort("blockedbyclient")
                else:
                    await route.continue_()

            await page.route("**/*", _route)
            await page.goto(url, wait_until="domcontentloaded",
                            timeout=self.render_timeout * 1000)
            try:
                await page.wait_for_load_state(
                    "networkidle", timeout=self.render_settle_ms)
            except Exception:  # noqa: BLE001
                pass
            await page.wait_for_timeout(self.render_settle_ms)
            if await self.host_blocked(page.url):
                self.logger.info("render: final URL is internal, dropped: %s", page.url)
                return None
            return await page.content()
        except Exception as e:  # noqa: BLE001
            self.logger.debug(f"Render failed for {url}: {e}")
            return None
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass


# ---------------------------------------------------------------------------
# Sitemap (upstream uses stdlib urlopen)
# ---------------------------------------------------------------------------

class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url_sync(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_original_urlopen = urllib.request.urlopen


def _guarded_urlopen(req, timeout=None, context=None):
    if settings.allow_private_targets():
        return _original_urlopen(req, timeout=timeout, context=context)
    url = req.full_url if isinstance(req, urllib.request.Request) else req
    check_url_sync(url)
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context),
        _GuardedRedirectHandler(),
    )
    return opener.open(req, timeout=timeout)


def install_sitemap_guard() -> None:
    """Route upstream's sitemap fetches through the guard (idempotent)."""
    import scrape_website.sitemap as sitemap_mod
    sitemap_mod.urlopen = _guarded_urlopen
