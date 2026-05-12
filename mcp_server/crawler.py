"""BFS site crawler scoped to the same FQDN (or eTLD+1).

The main entry point is `crawl()`, which returns a dict matching the
`crawl_site` MCP tool's return shape.  Uses aiohttp for fetching and
lxml for link extraction.  Respects robots.txt via stdlib robotparser.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qs, urlencode
from urllib.robotparser import RobotFileParser

import aiohttp
import lxml.html

from mcp_server.scraper import (
    FetchResult,
    _DEFAULT_UA,
    _TIMEOUT,
    _decode_body,
    _extract_title,
    html_to_markdown,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Upstream helpers — imported from ventz/scrape-website when available,
# otherwise small local fallbacks (≤10 lines each).
# ---------------------------------------------------------------------------

try:
    from app import (  # type: ignore[import-untyped]
        _DEFAULT_EXCLUDE_PATTERNS,
        _DEFAULT_TRACKING_PARAMS,
        _strip_tracking_params,
        _url_excluded,
        _fetch_sitemap_urls,
    )
except ImportError:
    # TODO: remove fallbacks once ventz/scrape-website ships these symbols
    # (upstream module path: vendor/scrape-website/app.py)

    _DEFAULT_EXCLUDE_PATTERNS: list[str] = [  # type: ignore[no-redef]
        r"\.(jpg|jpeg|png|gif|svg|webp|ico|bmp|tiff?)$",
        r"\.(pdf|docx?|xlsx?|pptx?|zip|tar|gz|rar)$",
        r"\.(css|js|json|xml|woff2?|ttf|eot)$",
        r"[?&](action=edit|oldid=|diff=)",
    ]

    _DEFAULT_TRACKING_PARAMS: frozenset[str] = frozenset({  # type: ignore[no-redef]
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "fbclid", "gclid", "mc_cid", "mc_eid",
    })

    def _strip_tracking_params(url: str, tracking_params: frozenset[str] | None = None) -> str:  # type: ignore[misc]
        """Remove known tracking query params from *url*."""
        params = tracking_params or _DEFAULT_TRACKING_PARAMS
        parts = urlsplit(url)
        qs = parse_qs(parts.query, keep_blank_values=True)
        cleaned = {k: v for k, v in qs.items() if k.lower() not in params}
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(cleaned, doseq=True), ""))

    def _url_excluded(url: str, patterns: list[str]) -> bool:  # type: ignore[misc]
        """Return True if *url* matches any of *patterns* (regex list)."""
        for pat in patterns:
            if re.search(pat, url, re.IGNORECASE):
                return True
        return False

    def _fetch_sitemap_urls(host: str, scheme: str = "https", timeout: int = 10, max_urls: int = 5000) -> list[str]:  # type: ignore[misc]
        """Synchronous sitemap fetch (called from async context via to_thread)."""
        import urllib.request
        sitemap_url = f"{scheme}://{host}/sitemap.xml"
        try:
            req = urllib.request.Request(sitemap_url, headers={"User-Agent": _DEFAULT_UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read().decode("utf-8", errors="replace")
        except Exception:
            return []
        urls: list[str] = []
        try:
            root = ET.fromstring(data)
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            for loc in root.findall(".//sm:loc", ns):
                if loc.text:
                    urls.append(loc.text.strip())
                    if len(urls) >= max_urls:
                        break
        except ET.ParseError:
            pass
        return urls


# Schemes we will follow.
_ALLOWED_SCHEMES = frozenset({"http", "https"})

# Link prefixes to skip outright (before even parsing).
_SKIP_PREFIXES = ("mailto:", "tel:", "javascript:", "data:")


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _normalize_crawl_url(url: str) -> str:
    """Normalize a URL for dedup: drop fragment, lowercase scheme+host,
    collapse empty paths to '/'."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path or "/"
    # Drop trailing slash ONLY for non-root paths for consistency
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def _scope_host(seed_url: str) -> str:
    """Return lowercased hostname (no port) of the seed URL."""
    return urlsplit(seed_url).hostname or ""


def _base_domain(host: str) -> str:
    """Naive eTLD+1: last two dot-separated labels. Good enough for
    `.endswith('.' + base)` subdomain checks."""
    parts = host.rsplit(".", 2)
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def _is_in_scope(candidate_url: str, scope_host: str, include_subdomains: bool) -> bool:
    """Check whether a candidate URL is within scope."""
    parts = urlsplit(candidate_url)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        return False
    chost = (parts.hostname or "").lower()
    if not chost:
        return False
    if chost == scope_host:
        return True
    if include_subdomains:
        base = _base_domain(scope_host)
        return chost == base or chost.endswith("." + base)
    return False


# ---------------------------------------------------------------------------
# Link extraction from raw HTML
# ---------------------------------------------------------------------------

def extract_links(html: str, page_url: str) -> set[str]:
    """Extract normalized absolute HTTP(S) <a href> links from HTML."""
    links: set[str] = set()
    try:
        doc = lxml.html.fromstring(html)
        doc.make_links_absolute(page_url, resolve_base_href=True)
    except Exception:
        return links

    for element, _attr, link, _pos in doc.iterlinks():
        if element.tag != "a":
            continue
        if not link:
            continue
        # Skip non-HTTP schemes early.
        if any(link.lower().startswith(p) for p in _SKIP_PREFIXES):
            continue
        parts = urlsplit(link)
        if parts.scheme.lower() not in _ALLOWED_SCHEMES:
            continue
        links.add(_normalize_crawl_url(link))
    return links


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------

async def _fetch_robots(session: aiohttp.ClientSession, seed_url: str) -> RobotFileParser:
    """Fetch and parse robots.txt for the seed URL's origin."""
    parts = urlsplit(seed_url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    rp = RobotFileParser()
    rp.set_url(robots_url)
    try:
        async with session.get(robots_url, allow_redirects=True) as resp:
            if resp.status == 200:
                text = await _decode_body(resp)
                rp.parse(text.splitlines())
            else:
                # No robots.txt or error -> allow everything
                rp.parse([])
    except Exception:
        rp.parse([])
    return rp


# ---------------------------------------------------------------------------
# Single-page fetch (raw HTML + markdown in one shot)
# ---------------------------------------------------------------------------

async def _fetch_page(
    session: aiohttp.ClientSession, url: str
) -> tuple[FetchResult, str]:
    """Fetch a page, return (FetchResult with markdown, raw_html).

    raw_html is needed for link extraction; FetchResult carries
    the markdown + diagnostics.
    """
    started = time.perf_counter()
    result = FetchResult(url=url)
    raw_html = ""
    try:
        async with session.get(url, allow_redirects=True) as resp:
            result.http_status = resp.status
            result.etag = resp.headers.get("ETag")
            result.last_modified = resp.headers.get("Last-Modified")
            if resp.status >= 400:
                result.status = "failed"
                result.error = f"HTTP {resp.status}"
                return result, raw_html
            raw_html = await _decode_body(resp)
    except Exception as e:  # noqa: BLE001
        result.error = f"{type(e).__name__}: {e}"[:240]
        result.status = "failed"
        return result, raw_html
    finally:
        result.fetch_duration_ms = int((time.perf_counter() - started) * 1000)

    result.content_bytes = len(raw_html.encode("utf-8", errors="replace"))
    result.page_title = _extract_title(raw_html)
    result.markdown = html_to_markdown(raw_html, url)
    if not result.markdown.strip():
        result.status = "empty"
    return result, raw_html


# ---------------------------------------------------------------------------
# Main BFS crawl
# ---------------------------------------------------------------------------

def _page_dict(fr: FetchResult, depth: int) -> dict[str, Any]:
    return {
        "url": fr.url,
        "depth": depth,
        "status": fr.status,
        "markdown": fr.markdown if fr.status == "ok" else "",
        "http_status": fr.http_status,
        "page_title": fr.page_title,
        "content_bytes": fr.content_bytes,
        "fetch_duration_ms": fr.fetch_duration_ms,
        "etag": fr.etag,
        "last_modified": fr.last_modified,
        "error": fr.error,
    }


async def crawl(
    seed_url: str,
    *,
    max_pages: int = 200,
    max_depth: int = 3,
    delay_ms: int = 500,
    respect_robots: bool = True,
    include_subdomains: bool = False,
    exclude_patterns: list[str] | None = None,
    strip_tracking_params: bool = True,
    use_sitemap: bool = True,
) -> dict[str, Any]:
    """BFS crawl from *seed_url*.

    Returns the dict shape expected by the ``crawl_site`` MCP tool.

    *exclude_patterns*: list of regex strings; URLs matching any pattern are
    skipped.  Defaults to ``_DEFAULT_EXCLUDE_PATTERNS`` (images, PDFs, static
    assets).

    *strip_tracking_params*: when True, UTM and similar tracking query params
    are stripped from discovered URLs before dedup, preventing duplicates that
    differ only by tracking tags.

    *use_sitemap*: when True, ``/sitemap.xml`` is fetched before BFS begins
    and its URLs are seeded into the queue at depth 0.
    """
    seed_url = _normalize_crawl_url(seed_url)
    if strip_tracking_params:
        seed_url = _strip_tracking_params(seed_url)
        seed_url = _normalize_crawl_url(seed_url)
    host = _scope_host(seed_url)
    started_at = datetime.now(timezone.utc).isoformat()

    # Compile exclude patterns once.
    effective_patterns = exclude_patterns if exclude_patterns is not None else list(_DEFAULT_EXCLUDE_PATTERNS)
    compiled_patterns = [re.compile(p, re.IGNORECASE) for p in effective_patterns]

    def _is_excluded(url: str) -> bool:
        return any(pat.search(url) for pat in compiled_patterns)

    # Counters
    skipped_robots = 0
    skipped_offsite = 0
    skipped_max_pages = 0
    skipped_excluded = 0

    # BFS state
    queue: deque[tuple[str, int]] = deque()
    queue.append((seed_url, 0))
    seen: set[str] = {seed_url}
    pages: list[dict[str, Any]] = []
    max_depth_reached = 0

    delay_s = delay_ms / 1000.0

    # Pre-seed from sitemap if requested.
    if use_sitemap:
        try:
            sitemap_urls = await asyncio.to_thread(
                _fetch_sitemap_urls, host
            )
        except Exception:  # noqa: BLE001
            sitemap_urls = []
        for surl in sitemap_urls:
            norm = _normalize_crawl_url(surl)
            if strip_tracking_params:
                norm = _strip_tracking_params(norm)
                norm = _normalize_crawl_url(norm)
            if norm in seen:
                continue
            seen.add(norm)
            if not _is_in_scope(norm, host, include_subdomains):
                skipped_offsite += 1
                continue
            if _is_excluded(norm):
                skipped_excluded += 1
                continue
            queue.append((norm, 0))

    async with aiohttp.ClientSession(
        timeout=_TIMEOUT, headers={"User-Agent": _DEFAULT_UA}
    ) as session:
        # Fetch robots.txt once for the run.
        rp: RobotFileParser | None = None
        if respect_robots:
            rp = await _fetch_robots(session, seed_url)

        while queue and len(pages) < max_pages:
            url, depth = queue.popleft()

            if depth > max_depth:
                # Already past max depth; don't fetch, don't enqueue children.
                continue

            # robots.txt check
            if rp and not rp.can_fetch(_DEFAULT_UA, url):
                skipped_robots += 1
                continue

            # Fetch
            fr, raw_html = await _fetch_page(session, url)
            pages.append(_page_dict(fr, depth))
            if depth > max_depth_reached:
                max_depth_reached = depth

            # Extract and enqueue child links (only if we got HTML)
            if raw_html and depth < max_depth:
                child_links = extract_links(raw_html, url)
                for link in sorted(child_links):  # sorted for determinism
                    norm_link = link
                    if strip_tracking_params:
                        norm_link = _strip_tracking_params(link)
                        norm_link = _normalize_crawl_url(norm_link)
                    if norm_link in seen:
                        continue
                    seen.add(norm_link)
                    if not _is_in_scope(norm_link, host, include_subdomains):
                        skipped_offsite += 1
                        continue
                    if _is_excluded(norm_link):
                        skipped_excluded += 1
                        continue
                    if len(pages) + (len(queue)) >= max_pages:
                        # Queue already full enough; remaining new links are skipped.
                        skipped_max_pages += 1
                        continue
                    queue.append((norm_link, depth + 1))

            # Politeness delay
            if queue and delay_s > 0:
                await asyncio.sleep(delay_s)

    finished_at = datetime.now(timezone.utc).isoformat()
    return {
        "seed_url": seed_url,
        "host": host,
        "discovered": len(seen),
        "fetched": len(pages),
        "skipped_robots": skipped_robots,
        "skipped_offsite": skipped_offsite,
        "skipped_max_pages": skipped_max_pages,
        "skipped_excluded": skipped_excluded,
        "max_depth_reached": max_depth_reached,
        "started_at": started_at,
        "finished_at": finished_at,
        "pages": pages,
    }
