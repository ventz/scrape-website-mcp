"""BFS site crawler scoped to the same FQDN (or eTLD+1).

The main entry point is `crawl()`, which returns a dict matching the
`crawl_site` MCP tool's return shape. Fetching goes through the shared
``scrape_website.FetchEngine`` (one fresh engine per crawl — robots and
Crawl-Delay state are per-host), which brings the full upstream tier stack:
retry/backoff + Retry-After, curl_cffi WAF/403 fallback, headless-Chromium
SPA render escalation, protego robots.txt, and PDF/Office -> Markdown
document extraction.

The crawl is deliberately SEQUENTIAL (BFS + politeness delay): the platform
parallelizes vector-store uploads on its side; a polite single-flight crawl
keeps us deterministic and friendly to the target host.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import lxml.html

from scrape_website.config import (
    DOWNLOADABLE_EXTENSIONS,
    _DEFAULT_EXCLUDE_PATTERNS as _UPSTREAM_EXCLUDE_PATTERNS,
)
from scrape_website.sitemap import _fetch_sitemap_urls
from scrape_website.urls import _strip_tracking_params

from mcp_server import scraper
from mcp_server.scraper import FetchResult, html_to_markdown

log = logging.getLogger(__name__)

# Default URL excludes for MCP crawls: upstream's CMS-noise patterns (/tag/,
# /author/, feeds, pagination, ...) plus static-asset extensions. NOTE the
# 0.2.0 behavior change: documents (.pdf/.docx/...) are NO LONGER excluded —
# they are fetched and extracted to Markdown (disable with extract_docs=false).
_DEFAULT_EXCLUDE_PATTERNS: list[str] = [
    *_UPSTREAM_EXCLUDE_PATTERNS,
    r"\.(jpg|jpeg|png|gif|svg|webp|ico|bmp|tiff?)$",
    r"\.(css|js|json|xml|woff2?|ttf|eot)$",
    r"[?&](action=edit|oldid=|diff=)",
]

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


def _is_document_url(url: str) -> bool:
    """Cheap extension check: would this URL be fetched as a document?"""
    path = urlsplit(url).path.lower()
    return any(path.endswith(ext) for ext in DOWNLOADABLE_EXTENSIONS)


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
        # Additive (0.2.0):
        "rendered": fr.rendered,
        "via": fr.via,
        "content_kind": fr.content_kind,
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
    render_mode: str | None = None,
    extract_docs: bool | None = None,
    progress_cb=None,
) -> dict[str, Any]:
    """BFS crawl from *seed_url*.

    Returns the dict shape expected by the ``crawl_site`` MCP tool.

    *exclude_patterns*: list of regex strings; URLs matching any pattern are
    skipped.  Defaults to ``_DEFAULT_EXCLUDE_PATTERNS`` (CMS noise, images,
    static assets — NOT documents).

    *strip_tracking_params*: when True, UTM and similar tracking query params
    are stripped from discovered URLs before dedup, preventing duplicates that
    differ only by tracking tags.

    *use_sitemap*: when True, ``/sitemap.xml`` (including sitemap-index
    recursion) is fetched before BFS begins and its URLs are seeded at depth 0.

    *render_mode*: 'auto' (default) renders only pages that look like
    un-hydrated SPA shells in headless Chromium; 'always'/'never' force it.

    *extract_docs*: when True (default), PDF/Office documents encountered
    during the crawl are downloaded and converted to Markdown; when False,
    document URLs are not fetched at all.

    *progress_cb*: optional async callable ``(fetched, queued)`` invoked after
    every page — the MCP layer uses it for progress notifications that double
    as keep-alives on long crawls.
    """
    seed_url = _normalize_crawl_url(seed_url)
    if strip_tracking_params:
        seed_url = _strip_tracking_params(seed_url)
        seed_url = _normalize_crawl_url(seed_url)
    host = _scope_host(seed_url)
    started_at = datetime.now(timezone.utc).isoformat()
    do_docs = extract_docs if extract_docs is not None else scraper.extract_docs_default()

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
    skipped_documents = 0
    rendered_count = 0
    docs_extracted = 0

    # BFS state
    queue: deque[tuple[str, int]] = deque()
    queue.append((seed_url, 0))
    seen: set[str] = {seed_url}
    pages: list[dict[str, Any]] = []
    max_depth_reached = 0

    delay_s = delay_ms / 1000.0

    # Pre-seed from sitemap if requested (upstream helper: recurses into
    # sitemap-index files).
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
            if not do_docs and _is_document_url(norm):
                skipped_documents += 1
                continue
            queue.append((norm, 0))

    # One fresh engine per crawl: robots + Crawl-Delay state are per-host.
    # wait_politeness() paces to robots.txt Crawl-Delay when declared, else
    # to delay_ms.
    engine = scraper.build_engine(
        respect_robots=respect_robots,
        delay_between_requests=delay_s,
    )
    await engine.start()
    try:
        if respect_robots:
            await engine.load_robots(seed_url)

        while queue and len(pages) < max_pages:
            url, depth = queue.popleft()

            if depth > max_depth:
                # Already past max depth; don't fetch, don't enqueue children.
                continue

            # robots.txt check (protego, same parser the upstream CLI uses)
            if not engine.robots_allows(url):
                skipped_robots += 1
                continue

            # Fetch through the shared tier stack; links come from the FINAL
            # (possibly rendered) HTML via our scope-aware extractor.
            async def _run_extract(html: str, page_url: str):
                links = extract_links(html, page_url)
                markdown = await asyncio.to_thread(html_to_markdown, html, page_url)
                return links, markdown

            fr, child_links = await scraper.fetch_page_result(
                url, run_extract=_run_extract, render_mode=render_mode,
                extract_docs=do_docs, engine=engine)
            pages.append(_page_dict(fr, depth))
            if fr.rendered:
                rendered_count += 1
            if fr.content_kind != "html" and fr.status == "ok":
                docs_extracted += 1
            if depth > max_depth_reached:
                max_depth_reached = depth

            if progress_cb is not None:
                try:
                    await progress_cb(len(pages), len(queue))
                except Exception:  # noqa: BLE001
                    pass  # progress must never kill a crawl

            # Enqueue child links (only if the page produced any)
            if child_links and depth < max_depth:
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
                    if not do_docs and _is_document_url(norm_link):
                        skipped_documents += 1
                        continue
                    if len(pages) + (len(queue)) >= max_pages:
                        # Queue already full enough; remaining new links are skipped.
                        skipped_max_pages += 1
                        continue
                    queue.append((norm_link, depth + 1))

            # Politeness: robots Crawl-Delay when declared, else delay_ms.
            if queue:
                await engine.wait_politeness()
    finally:
        await engine.close()

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
        "skipped_documents": skipped_documents,
        "max_depth_reached": max_depth_reached,
        "rendered_count": rendered_count,
        "docs_extracted": docs_extracted,
        "started_at": started_at,
        "finished_at": finished_at,
        "pages": pages,
    }
