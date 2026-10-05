"""BFS site crawler scoped to the same FQDN (or eTLD+1).

The main entry point is `crawl()`, which returns a dict matching the
`crawl_site` MCP tool's return shape. Fetching goes through the shared
``scrape_website.FetchEngine`` (one fresh, SSRF-guarded engine per crawl —
robots and Crawl-Delay state are per-host), which brings the full upstream
tier stack: retry/backoff + Retry-After, curl_cffi WAF/403 fallback,
headless-Chromium SPA render escalation, protego robots.txt, and PDF/Office
-> Markdown document extraction.

The crawl is SEQUENTIAL by default (BFS + politeness delay): the platform
parallelizes vector-store uploads on its side; a polite single-flight crawl
keeps us deterministic and friendly to the target host. ``concurrency > 1``
runs a worker pool over the same BFS queue; pacing stays global
(``wait_politeness``, Crawl-Delay still wins) and pages are returned sorted
by ``(depth, discovery order)``, which is exactly the sequential order.
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
from scrape_website.urls import _is_safe_fetch_target, _same_host, _strip_tracking_params

from mcp_server import netguard, scraper, settings
from mcp_server.scraper import FetchOptions, FetchResult, html_to_markdown

log = logging.getLogger(__name__)

# Upstream fetches sitemaps with stdlib urlopen; route it through the guard.
netguard.install_sitemap_guard()

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


def _onto_seed(url: str, scheme: str, netloc: str) -> str:
    """Rebuild *url* onto the seed's scheme + host (upstream
    ``_canonicalize_host``), so www/non-www and http/https aliases of a page
    collapse to one URL."""
    parts = urlsplit(url)
    return _normalize_crawl_url(urlunsplit((scheme, netloc, parts.path, parts.query, "")))


def _compile_patterns(name: str, patterns: list[str], flags: int) -> list[re.Pattern]:
    """Compile a caller regex list under the server caps. Bad input is
    rejected (not clamped): dropping an exclude/include pattern would
    silently widen the crawl."""
    if len(patterns) > settings.MAX_PATTERNS:
        raise ValueError(f"{name}: at most {settings.MAX_PATTERNS} patterns allowed")
    compiled = []
    for p in patterns:
        if len(p) > settings.MAX_PATTERN_LEN:
            raise ValueError(
                f"{name}: pattern longer than {settings.MAX_PATTERN_LEN} chars")
        try:
            compiled.append(re.compile(p, flags))
        except re.error as e:
            raise ValueError(f"{name}: invalid regex {p!r}: {e}") from None
    return compiled


# ---------------------------------------------------------------------------
# Link extraction from raw HTML
# ---------------------------------------------------------------------------

def extract_links(html: str, page_url: str, *, include_documents: bool = False) -> set[str]:
    """Extract normalized absolute HTTP(S) <a href> links from HTML.

    *include_documents*: also return ``<link>``/``<script>``/``<img>`` URLs
    that end in a document extension (upstream follows these to pick up
    CDN-hosted PDFs)."""
    links: set[str] = set()
    try:
        doc = lxml.html.fromstring(html)
        doc.make_links_absolute(page_url, resolve_base_href=True)
    except Exception:
        return links

    for element, _attr, link, _pos in doc.iterlinks():
        if element.tag != "a":
            if not (include_documents and element.tag in ("link", "script", "img")):
                continue
            if not link or not _is_document_url(link):
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

def _page_dict(fr: FetchResult, depth: int, opts: FetchOptions | None = None,
               warnings: list[str] | None = None) -> dict[str, Any]:
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
        # Additive (0.3.0):
        **scraper.result_extras(fr, opts, warnings),
    }


def _report(records: list[tuple[FetchResult, dict]], skipped_robots: int,
            rendered_count: int) -> dict[str, Any]:
    """Upstream-shaped stats plus the report lists (failed/denied/404/
    challenged), derived from the per-page results."""
    failed_urls: list[str] = []
    denied_urls: list[str] = []
    not_found_urls: list[str] = []
    challenged_urls: list[str] = []
    stats = {
        "pages_downloaded": 0, "files_downloaded": 0, "text_extracted": 0,
        "docs_extracted": 0, "rendered": rendered_count,
        "robots_skipped": skipped_robots, "errors": 0, "denied": 0,
        "not_found": 0, "challenged": 0, "total_bytes": 0,
    }
    for fr, _page in records:
        if fr.status == "failed":
            if fr.classification == "challenge":
                challenged_urls.append(fr.url)
            elif fr.classification == "denied":
                denied_urls.append(fr.url)
            elif fr.classification == "not_found":
                not_found_urls.append(fr.url)
            else:
                failed_urls.append(fr.url)
            continue
        if fr.status == "empty" and fr.classification == "not_found":
            not_found_urls.append(fr.url)  # soft 404
        if fr.status not in ("ok", "empty"):
            continue
        is_doc = fr.content_kind != "html"
        if is_doc:
            stats["files_downloaded"] += 1
            if fr.status == "ok":
                stats["docs_extracted"] += 1
        else:
            stats["pages_downloaded"] += 1
            if fr.status == "ok":
                stats["text_extracted"] += 1
        stats["total_bytes"] += fr.content_bytes or 0
    stats["errors"] = len(failed_urls)
    stats["denied"] = len(denied_urls)
    stats["not_found"] = len(not_found_urls)
    stats["challenged"] = len(challenged_urls)
    return {
        "stats": stats,
        "failed_urls": failed_urls,
        "denied_urls": denied_urls,
        "not_found_urls": not_found_urls,
        "challenged_urls": challenged_urls,
    }


def error_result(seed_url: str, error: str,
                 warnings: list[str] | None = None) -> dict[str, Any]:
    """Crawl-shaped result for a call rejected before any fetch (e.g. a
    disabled unsafe toggle). Same keys as a normal crawl, zero pages."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "seed_url": seed_url, "host": _scope_host(seed_url),
        "discovered": 0, "fetched": 0,
        "skipped_robots": 0, "skipped_offsite": 0, "skipped_max_pages": 0,
        "skipped_excluded": 0, "skipped_documents": 0,
        "skipped_not_included": 0, "max_depth_reached": 0,
        "rendered_count": 0, "docs_extracted": 0,
        "duplicate_documents": 0,
        "started_at": now, "finished_at": now, "pages": [],
        **_report([], 0, 0),
        "truncated": False, "truncated_reason": None,
        "warnings": list(warnings or []),
        "scrape_website_version": scraper.SCRAPE_WEBSITE_VERSION,
        "error": error,
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
    # 0.3.0 (all default to 0.2.0 behavior):
    concurrency: int = 1,
    fetch_options: FetchOptions | None = None,
    extra_exclude_patterns: list[str] | None = None,
    exclude_patterns_case_sensitive: bool = False,
    include_patterns: list[str] | None = None,
    additional_seed_urls: list[str] | None = None,
    collapse_host_aliases: bool = False,
    follow_offsite_documents: bool = False,
    dedupe_documents: bool = False,
    max_total_bytes: int | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """BFS crawl from *seed_url*.

    Returns the dict shape expected by the ``crawl_site`` MCP tool.

    *exclude_patterns*: list of regex strings; URLs matching any pattern are
    skipped.  Defaults to ``_DEFAULT_EXCLUDE_PATTERNS`` (CMS noise, images,
    static assets — NOT documents). *extra_exclude_patterns* are appended
    (to the defaults when *exclude_patterns* is None), like upstream ``-e``.
    Matching is case-insensitive unless *exclude_patterns_case_sensitive*.

    *include_patterns*: when set, a discovered URL must match at least one
    pattern to be enqueued (the seed and *additional_seed_urls* always are).

    *strip_tracking_params*: when True, UTM and similar tracking query params
    are stripped from discovered URLs before dedup, preventing duplicates that
    differ only by tracking tags.

    *use_sitemap*: when True, ``/sitemap.xml`` (including sitemap-index
    recursion) is fetched before BFS begins and its URLs are seeded at depth 0.
    Sitemap URLs on the seed's ``www.`` alias count as on-site and are
    rewritten onto the seed host.

    *render_mode*: 'auto' (default) renders only pages that look like
    un-hydrated SPA shells in headless Chromium; 'always'/'never' force it.

    *extract_docs*: when True (default), PDF/Office documents encountered
    during the crawl are downloaded and converted to Markdown; when False,
    document URLs are not fetched at all.

    *collapse_host_aliases*: treat ``www.``/non-``www.`` and http/https as one
    site, rewriting links onto the seed's scheme + host.
    *follow_offsite_documents*: fetch off-scope document links (``<a>``,
    ``<link>``, ``<script>``, ``<img>``) that pass the SSRF gate; never
    followed further.
    *dedupe_documents*: byte-identical documents after the first come back
    ``status='skipped'`` with ``duplicate_of``.
    *max_total_bytes*: stop fetching once the returned payload exceeds it.

    *progress_cb*: optional async callable ``(fetched, queued)`` invoked after
    every page — the MCP layer uses it for progress notifications that double
    as keep-alives on long crawls.
    """
    warnings = warnings if warnings is not None else []
    opts = fetch_options or FetchOptions()
    seed_url = _normalize_crawl_url(seed_url)
    if strip_tracking_params:
        seed_url = _strip_tracking_params(seed_url)
        seed_url = _normalize_crawl_url(seed_url)
    host = _scope_host(seed_url)
    seed_parts = urlsplit(seed_url)
    seed_scheme, seed_netloc = seed_parts.scheme, seed_parts.netloc
    started_at = datetime.now(timezone.utc).isoformat()
    do_docs = extract_docs if extract_docs is not None else scraper.extract_docs_default()

    # Server caps (clamped, reported in warnings).
    concurrency = settings.clamp("concurrency", int(concurrency), 1,
                                 settings.max_crawl_concurrency(), warnings)
    min_delay = settings.min_delay_ms()
    if delay_ms < min_delay:
        warnings.append(f"delay_ms={delay_ms} raised to {min_delay} (server minimum)")
        delay_ms = min_delay
    byte_cap = settings.max_response_bytes()
    if max_total_bytes is not None:
        hard = byte_cap if byte_cap is not None else settings.DEFAULT_MAX_RESPONSE_BYTES
        byte_cap = settings.clamp("max_total_bytes", int(max_total_bytes), 1, hard, warnings)
    extra_seeds = list(additional_seed_urls or [])
    if len(extra_seeds) > settings.MAX_ADDITIONAL_SEEDS:
        warnings.append(f"additional_seed_urls truncated to {settings.MAX_ADDITIONAL_SEEDS}")
        extra_seeds = extra_seeds[:settings.MAX_ADDITIONAL_SEEDS]

    # Compile patterns once.
    flags = 0 if exclude_patterns_case_sensitive else re.IGNORECASE
    base_patterns = exclude_patterns if exclude_patterns is not None else list(_DEFAULT_EXCLUDE_PATTERNS)
    compiled_patterns = _compile_patterns("exclude_patterns", base_patterns, flags)
    if extra_exclude_patterns:
        compiled_patterns += _compile_patterns(
            "extra_exclude_patterns", extra_exclude_patterns, flags)
    compiled_includes = (_compile_patterns("include_patterns", include_patterns, flags)
                         if include_patterns else [])

    def _is_excluded(url: str) -> bool:
        return any(pat.search(url) for pat in compiled_patterns)

    def _not_included(url: str) -> bool:
        return bool(compiled_includes) and not any(
            pat.search(url) for pat in compiled_includes)

    def _scoped(url: str) -> str | None:
        """In-scope form of *url* (possibly rewritten onto the seed host),
        or None when off-scope."""
        if collapse_host_aliases:
            parts = urlsplit(url)
            if (parts.scheme in _ALLOWED_SCHEMES
                    and _same_host(parts.netloc, seed_netloc)):
                return _onto_seed(url, seed_scheme, seed_netloc)
        return url if _is_in_scope(url, host, include_subdomains) else None

    def _clean(url: str) -> str:
        norm = _normalize_crawl_url(url)
        if strip_tracking_params:
            norm = _normalize_crawl_url(_strip_tracking_params(norm))
        return norm

    # Counters
    skipped_robots = 0
    skipped_offsite = 0
    skipped_max_pages = 0
    skipped_excluded = 0
    skipped_documents = 0
    skipped_not_included = 0
    rendered_count = 0
    docs_extracted = 0

    # BFS state. Queue items: (url, depth, discovery_index, offsite_document).
    queue: deque[tuple[str, int, int, bool]] = deque()
    discovery = 0

    def _push(url: str, depth: int, offsite: bool = False) -> None:
        nonlocal discovery
        queue.append((url, depth, discovery, offsite))
        discovery += 1

    _push(seed_url, 0)
    seen: set[str] = {seed_url}
    records: list[tuple[int, int, FetchResult, dict[str, Any]]] = []
    max_depth_reached = 0
    doc_seen: dict[str, str] | None = {} if dedupe_documents else None

    # Extra caller seeds: depth 0, never subject to include/exclude.
    for raw in extra_seeds:
        norm = _clean(raw)
        scoped = _scoped(norm)
        key = scoped or norm
        if key in seen:
            continue
        seen.add(key)
        if scoped is None:
            skipped_offsite += 1
            continue
        if not do_docs and _is_document_url(scoped):
            skipped_documents += 1
            continue
        _push(scoped, 0)

    # One fresh engine per crawl: robots + Crawl-Delay state are per-host.
    # wait_politeness() paces to robots.txt Crawl-Delay when declared, else
    # to delay_ms.
    delay_s = delay_ms / 1000.0
    engine = scraper.build_engine(
        respect_robots=respect_robots,
        delay_between_requests=delay_s,
        **opts.engine_overrides(),
    )

    # Pre-seed from sitemap if requested (upstream helper: recurses into
    # sitemap-index files; fetched with the seed's scheme).
    if use_sitemap:
        loop = asyncio.get_running_loop()

        def _sitemap_fallback(url: str) -> bytes | None:
            """401/403 sitemap: retry with the engine's curl_cffi fingerprint
            (no cookie bridge; SSRF-guarded), as upstream's crawler does.
            Runs in the sitemap worker thread, on this crawl's loop."""
            try:
                res = asyncio.run_coroutine_threadsafe(
                    engine._fetch_via_curl_cffi(url, cookie_bridge=False, raw=True),
                    loop).result()
            except Exception:  # noqa: BLE001
                return None
            if res is None or res[3] != 200:
                return None
            return res[0].encode("utf-8") if isinstance(res[0], str) else res[0]

        try:
            sitemap_urls = await asyncio.to_thread(
                _fetch_sitemap_urls, seed_netloc, scheme=seed_scheme,
                timeout=opts.timeout_s or settings.timeout_s(),
                allow_insecure_tls=opts.allow_insecure_tls,
                fallback=_sitemap_fallback,
            )
        except Exception:  # noqa: BLE001
            sitemap_urls = []
        for surl in sitemap_urls:
            norm = _clean(surl)
            scoped = _scoped(norm)
            if scoped is None and _same_host(urlsplit(norm).netloc, seed_netloc):
                # www-alias of the seed host: on-site, rewritten onto it.
                scoped = _onto_seed(norm, seed_scheme, seed_netloc)
            key = scoped or norm
            if key in seen:
                continue
            seen.add(key)
            if scoped is None:
                skipped_offsite += 1
                continue
            if _is_excluded(scoped):
                skipped_excluded += 1
                continue
            if _not_included(scoped):
                skipped_not_included += 1
                continue
            if not do_docs and _is_document_url(scoped):
                skipped_documents += 1
                continue
            _push(scoped, 0)

    in_flight = 0
    returned_bytes = 0
    stop_reason: str | None = None
    cond = asyncio.Condition()

    def _enqueue_children(child_links: set[str], depth: int) -> None:
        nonlocal skipped_offsite, skipped_excluded, skipped_not_included
        nonlocal skipped_documents, skipped_max_pages
        for link in sorted(child_links):  # sorted for determinism
            norm_link = link
            if strip_tracking_params:
                norm_link = _strip_tracking_params(link)
                norm_link = _normalize_crawl_url(norm_link)
            scoped = _scoped(norm_link)
            key = scoped or norm_link
            if key in seen:
                continue
            seen.add(key)
            offsite = False
            if scoped is None:
                if (follow_offsite_documents and do_docs
                        and _is_document_url(norm_link)
                        and _is_safe_fetch_target(norm_link)):
                    scoped, offsite = norm_link, True
                else:
                    skipped_offsite += 1
                    continue
            if _is_excluded(scoped):
                skipped_excluded += 1
                continue
            if _not_included(scoped):
                skipped_not_included += 1
                continue
            if not do_docs and _is_document_url(scoped):
                skipped_documents += 1
                continue
            if len(records) + in_flight + len(queue) >= max_pages:
                # Queue already full enough; remaining new links are skipped.
                skipped_max_pages += 1
                continue
            _push(scoped, depth + 1, offsite)

    async def _run_extract(html: str, page_url: str):
        links = extract_links(html, page_url,
                              include_documents=follow_offsite_documents)
        markdown = await asyncio.to_thread(html_to_markdown, html, page_url)
        return links, markdown

    async def _worker() -> None:
        nonlocal in_flight, skipped_robots, rendered_count, docs_extracted
        nonlocal max_depth_reached, returned_bytes, stop_reason
        while True:
            async with cond:
                while True:
                    if stop_reason is not None:
                        return
                    if queue and len(records) + in_flight < max_pages:
                        break
                    if in_flight == 0:
                        return  # nothing queued (or no budget) and nothing pending
                    await cond.wait()
                url, depth, idx, offsite = queue.popleft()
                if depth > max_depth:
                    # Already past max depth; don't fetch, don't enqueue children.
                    continue
                # robots.txt check (protego, same parser the upstream CLI
                # uses). Off-site documents aren't covered by the seed
                # host's robots.txt, so they skip it.
                if not offsite and not engine.robots_allows(url):
                    skipped_robots += 1
                    continue
                in_flight += 1

            try:
                # Politeness: robots Crawl-Delay when declared, else delay_ms.
                # Global slot booking, so it also paces concurrent workers.
                await engine.wait_politeness()
                # Fetch through the shared tier stack; links come from the
                # FINAL (possibly rendered) HTML via our scope-aware extractor.
                fr, child_links = await scraper.fetch_page_result(
                    url, run_extract=_run_extract, render_mode=render_mode,
                    extract_docs=do_docs, engine=engine,
                    max_file_size=opts.effective_max_file_size(),
                    keep_html=opts.include_html,
                    keep_document=opts.include_document_base64,
                    doc_seen=doc_seen)
            except BaseException:
                async with cond:
                    in_flight -= 1
                    cond.notify_all()
                raise

            async with cond:
                in_flight -= 1
                page = _page_dict(fr, depth, opts, warnings)
                records.append((depth, idx, fr, page))
                if fr.rendered:
                    rendered_count += 1
                if fr.content_kind != "html" and fr.status == "ok":
                    docs_extracted += 1
                if depth > max_depth_reached:
                    max_depth_reached = depth
                returned_bytes += len(page["markdown"].encode("utf-8"))
                returned_bytes += len((page.get("html") or "").encode("utf-8"))
                returned_bytes += len(page.get("document_base64") or "")
                if byte_cap is not None and returned_bytes > byte_cap:
                    stop_reason = "max_total_bytes"
                # Enqueue child links (only if the page produced any).
                # Off-site documents are leaves.
                if child_links and depth < max_depth and not offsite:
                    _enqueue_children(child_links, depth)
                fetched_now, queued_now = len(records), len(queue)
                cond.notify_all()

            if progress_cb is not None:
                try:
                    await progress_cb(fetched_now, queued_now)
                except Exception:  # noqa: BLE001
                    pass  # progress must never kill a crawl

    await engine.start()
    try:
        if respect_robots:
            await engine.load_robots(seed_url)
        async with asyncio.TaskGroup() as tg:
            for _ in range(concurrency):
                tg.create_task(_worker())
    finally:
        await engine.close()

    # Sequential BFS already produces (depth, discovery) order; workers
    # finish out of order, so sort to keep results deterministic.
    records.sort(key=lambda r: (r[0], r[1]))
    pages = [r[3] for r in records]
    report = _report([(r[2], r[3]) for r in records], skipped_robots, rendered_count)

    truncated_reason = stop_reason
    if truncated_reason is None and (skipped_max_pages or
                                     (queue and len(records) >= max_pages)):
        truncated_reason = "max_pages"

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
        # Additive (0.3.0):
        "skipped_not_included": skipped_not_included,
        "duplicate_documents": sum(1 for p in pages if p.get("duplicate_of")),
        **report,
        "truncated": truncated_reason is not None,
        "truncated_reason": truncated_reason,
        "warnings": warnings,
        "scrape_website_version": scraper.SCRAPE_WEBSITE_VERSION,
        "error": None,
    }
