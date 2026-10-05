"""FastMCP server entrypoint.

Exposes tools that let an MCP client (the Harvard EA assistants platform's
per-agent MCP config, or any other) register URLs into an OpenAI vector
store and keep them in sync. The vector store lives in the operator's
OpenAI account; this server holds only an OpenAI API key for that account
plus an MCP bearer token that the platform presents on every request.

Transport: Streamable HTTP (what OpenAI Responses' `tools=[{"type":"mcp"...}]`
speaks). Mounted at `/mcp` by default by FastMCP.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import importlib.util
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastmcp import Context, FastMCP
from openai import AsyncOpenAI

from mcp_server import __version__ as SERVER_VERSION
from mcp_server import auth, crawler, openai_sync, scraper, settings
from mcp_server.openai_sync import VectorStoreNotFoundError
from mcp_server.store import Registration, Store

log = logging.getLogger(__name__)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

mcp = FastMCP("scrape-website-mcp")
_store = Store()
_client: AsyncOpenAI | None = None
_per_url_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_last_resync_all_at: str | None = None
_last_resync_all_result: dict[str, Any] | None = None


def _openai() -> AsyncOpenAI:
    """Lazy AsyncOpenAI client, re-read so tests can patch env."""
    global _client
    if _client is None:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY env var is not set")
        _client = AsyncOpenAI(api_key=key)
    return _client


def _lock_for(url: str) -> asyncio.Lock:
    return _per_url_locks[hashlib.sha256(url.encode("utf-8")).hexdigest()]


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def register_url(
    url: str, vector_store_id: str, registered_by: str | None = None
) -> dict[str, Any]:
    """Scrape `url` and upload as markdown into `vector_store_id`.

    Idempotent: if this URL is already registered, behaves like resync_url —
    no-op when content unchanged, replace when it changed. `registered_by` is
    an optional curator label persisted on first registration."""
    url = scraper.normalize_url(url)
    async with _lock_for(url):
        fetched = await scraper.fetch_and_extract(url)
        if fetched.status == "failed":
            _store.record_failure(
                url,
                error=fetched.error or "unknown",
                http_status=fetched.http_status,
                vector_store_id=vector_store_id,
            )
            return {
                "url": url, "registered": False, "status": "failed",
                "error": fetched.error, "http_status": fetched.http_status,
            }
        if fetched.status == "empty":
            # Persist as 'empty' so the user can see the attempt in the list.
            _store.record_failure(
                url,
                error="no extractable content",
                http_status=fetched.http_status,
                vector_store_id=vector_store_id,
            )
            # Promote sentinel row's status to 'empty' (record_failure wrote 'failed').
            try:
                with _store._lock:  # noqa: SLF001
                    _store._conn.execute(
                        "UPDATE registered_urls SET last_status='empty' WHERE url=?",
                        (url,),
                    )
            except Exception:  # noqa: BLE001
                pass
            return {
                "url": url, "registered": False, "status": "empty",
                "reason": "no extractable content",
            }

        h = openai_sync.content_hash(fetched.markdown)
        existing = _store.get(url)
        if existing and existing.vector_store_id == vector_store_id and existing.content_hash == h:
            # no-op resync — touch timestamps + diagnostics only
            _store.upsert(
                url, vector_store_id, existing.file_id, h,
                last_status="ok",
                last_error=None,
                http_status=fetched.http_status,
                content_bytes=fetched.content_bytes,
                fetch_duration_ms=fetched.fetch_duration_ms,
                page_title=fetched.page_title,
                etag=fetched.etag,
                last_modified=fetched.last_modified,
                registered_by=registered_by,
            )
            return {
                "url": url, "registered": True, "changed": False,
                "file_id": existing.file_id, "status": "ok",
            }

        try:
            new_file_id, vs_status, deleted = await openai_sync.replace_url_in_vector_store(
                _openai(), vector_store_id, url, fetched.markdown
            )
        except VectorStoreNotFoundError as e:
            _store.record_failure(url, error=str(e), vector_store_id=vector_store_id)
            return {
                "url": url, "registered": False, "status": "failed",
                "error": str(e), "vector_store_id": e.vector_store_id,
            }
        _store.upsert(
            url, vector_store_id, new_file_id, h,
            last_content_change_at=_utcnow_iso(),
            last_status="ok",
            last_error=None,
            http_status=fetched.http_status,
            content_bytes=fetched.content_bytes,
            fetch_duration_ms=fetched.fetch_duration_ms,
            page_title=fetched.page_title,
            etag=fetched.etag,
            last_modified=fetched.last_modified,
            registered_by=registered_by,
        )
        return {
            "url": url, "registered": True, "changed": True,
            "file_id": new_file_id, "vs_status": vs_status,
            "deleted_old_file_ids": deleted, "status": "ok",
        }


@mcp.tool()
async def resync_url(url: str) -> dict[str, Any]:
    """Re-scrape `url`. If content hash changed, upload a new file and remove
    the prior version from the vector store. No-op if hash is unchanged."""
    url = scraper.normalize_url(url)
    async with _lock_for(url):
        existing = _store.get(url)
        if existing is None:
            return {"url": url, "changed": False, "reason": "not registered"}
        if not existing.vector_store_id:
            return {
                "url": url, "changed": False, "status": "failed",
                "error": "this URL has no vector_store_id (only a failed attempt is on record); call register_url to retry",
            }

        fetched = await scraper.fetch_and_extract(url)
        if fetched.status == "failed":
            _store.record_failure(url, error=fetched.error or "unknown",
                                  http_status=fetched.http_status)
            return {
                "url": url, "changed": False, "status": "failed",
                "error": fetched.error, "http_status": fetched.http_status,
            }
        if fetched.status == "empty":
            _store.record_failure(url, error="no extractable content",
                                  http_status=fetched.http_status)
            return {
                "url": url, "changed": False, "status": "empty",
                "reason": "no extractable content",
            }

        h = openai_sync.content_hash(fetched.markdown)
        if h == existing.content_hash:
            # touch diagnostics only
            _store.upsert(
                url, existing.vector_store_id, existing.file_id, h,
                last_status="ok",
                last_error=None,
                http_status=fetched.http_status,
                content_bytes=fetched.content_bytes,
                fetch_duration_ms=fetched.fetch_duration_ms,
                page_title=fetched.page_title,
                etag=fetched.etag,
                last_modified=fetched.last_modified,
            )
            return {
                "url": url, "changed": False,
                "file_id": existing.file_id, "status": "ok",
            }

        new_file_id, vs_status, deleted = await openai_sync.replace_url_in_vector_store(
            _openai(), existing.vector_store_id, url, fetched.markdown
        )
        _store.upsert(
            url, existing.vector_store_id, new_file_id, h,
            last_content_change_at=_utcnow_iso(),
            last_status="ok",
            last_error=None,
            http_status=fetched.http_status,
            content_bytes=fetched.content_bytes,
            fetch_duration_ms=fetched.fetch_duration_ms,
            page_title=fetched.page_title,
            etag=fetched.etag,
            last_modified=fetched.last_modified,
        )
        return {
            "url": url, "changed": True,
            "file_id": new_file_id, "vs_status": vs_status,
            "deleted_old_file_ids": deleted, "status": "ok",
        }


@mcp.tool()
async def resync_all(concurrency: int = 4) -> dict[str, Any]:
    """Run `resync_url` for every registered URL. Cron-friendly."""
    global _last_resync_all_at, _last_resync_all_result
    rows = _store.list_all()
    sem = asyncio.Semaphore(max(1, concurrency))
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    async def one(url: str) -> None:
        async with sem:
            try:
                results.append(await resync_url(url))
            except Exception as e:
                errors.append({"url": url, "error": str(e)})

    await asyncio.gather(*(one(r.url) for r in rows))
    changed = sum(1 for r in results if r.get("changed"))
    failed = sum(1 for r in results if r.get("status") == "failed") + len(errors)
    summary = {
        "checked": len(rows),
        "changed": changed,
        "unchanged": len(results) - changed - failed,
        "failed": failed,
        "errors": errors,
        "finished_at": _utcnow_iso(),
    }
    _last_resync_all_at = summary["finished_at"]
    _last_resync_all_result = summary
    return summary


@mcp.tool()
async def unregister_url(url: str) -> dict[str, Any]:
    """Remove `url` from its vector store (deletes any matching file) and forget it."""
    url = scraper.normalize_url(url)
    async with _lock_for(url):
        existing = _store.get(url)
        if existing is None:
            return {"url": url, "deleted": False, "reason": "not registered"}

        client = _openai()
        file_ids: list[str] = []

        # Only hit OpenAI if we have a real vector_store_id to look in.
        # Failed-only rows may carry empty vector_store_id from `record_failure`.
        if existing.vector_store_id:
            try:
                file_ids = await openai_sync.find_existing_file_ids(
                    client, existing.vector_store_id, url
                )
            except Exception as e:  # noqa: BLE001
                log.warning("unregister: vector_stores.files.list failed: %s", e)
            if existing.file_id and existing.file_id not in file_ids:
                file_ids.append(existing.file_id)

            for fid in file_ids:
                await openai_sync.delete_file_completely(client, existing.vector_store_id, fid)

        _store.delete(url)
        return {"url": url, "deleted": True, "deleted_file_ids": file_ids}


@mcp.tool()
async def list_registered() -> dict[str, Any]:
    """Return all URLs currently tracked by this MCP server, with rich state."""
    rows = _store.list_all()
    return {
        "count": len(rows),
        "registered": [r.to_dict() for r in rows],
    }


def _fetch_payload(url: str, fetched: scraper.FetchResult,
                   opts: scraper.FetchOptions | None,
                   warnings: list[str]) -> dict[str, Any]:
    """The fetch_url_as_markdown result dict (also each batch result)."""
    payload = {
        "url": url,
        "markdown": fetched.markdown,
        "length": len(fetched.markdown),
        "http_status": fetched.http_status,
        "page_title": fetched.page_title,
        "status": fetched.status,
        "error": fetched.error,
        # Additive (0.2.0):
        "rendered": fetched.rendered,
        "via": fetched.via,
        "content_kind": fetched.content_kind,
        # Additive (0.3.0). The platform's scraper_proxy already reads these
        # four for registration rows.
        "content_bytes": fetched.content_bytes,
        "fetch_duration_ms": fetched.fetch_duration_ms,
        "etag": fetched.etag,
        "last_modified": fetched.last_modified,
    }
    payload.update(scraper.result_extras(fetched, opts, warnings))
    payload["warnings"] = warnings
    return payload


def _rejected_payload(url: str, error: str, warnings: list[str]) -> dict[str, Any]:
    return _fetch_payload(
        url, scraper.FetchResult(url=url, status="failed", error=error),
        None, warnings)


@contextlib.asynccontextmanager
async def _call_engine(opts: scraper.FetchOptions):
    """The shared singleton engine, unless this call changes an engine-level
    knob — then a private engine that is closed when the call ends."""
    overrides = opts.engine_overrides()
    if not overrides:
        yield None
        return
    engine = scraper.build_engine(respect_robots=False, **overrides)
    try:
        yield engine
    finally:
        await engine.close()


class _Pacer:
    """Global request pacing across a batch: each caller books the next free
    slot, so N concurrent tasks still start one per ``delay_s``."""

    def __init__(self, delay_s: float):
        self.delay_s = delay_s
        self._next_at = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if self.delay_s <= 0:
            return
        loop = asyncio.get_running_loop()
        async with self._lock:
            now = loop.time()
            wait = self._next_at - now
            self._next_at = max(now, self._next_at) + self.delay_s
        if wait > 0:
            await asyncio.sleep(wait)


@mcp.tool()
async def fetch_url_as_markdown(
    url: str,
    render_mode: str | None = None,
    extract_docs: bool | None = None,
    timeout_s: int | None = None,
    allow_insecure_tls: bool = False,
    render_settle_ms: int | None = None,
    max_file_size_bytes: int | None = None,
    respect_robots: bool = False,
    include_html: bool = False,
    include_document_base64: bool = False,
) -> dict[str, Any]:
    """Live one-shot scrape — no vector store, no state. Returns markdown.

    render_mode: 'auto' (default; headless-render only un-hydrated SPA
    shells), 'always', or 'never'. extract_docs: convert PDFs/Office files
    to Markdown (default from SCRAPER_EXTRACT_DOCS). timeout_s: per-request
    timeout (clamped to SCRAPER_MAX_TIMEOUT_S). allow_insecure_tls: skip
    certificate checks; only honored when the server sets
    SCRAPER_ALLOW_INSECURE_TLS=1. render_settle_ms: hydration wait after DOM
    load (0-15000). max_file_size_bytes: lower the document size cap.
    respect_robots: skip (status 'skipped') if robots.txt disallows the URL.
    include_html / include_document_base64: also return the raw page HTML
    or document bytes (size-capped). Private/internal targets are refused
    unless SCRAPER_ALLOW_PRIVATE_TARGETS=1."""
    url = scraper.normalize_url(url)
    warnings: list[str] = []
    try:
        opts = scraper.resolve_options(
            render_mode=render_mode, extract_docs=extract_docs,
            timeout_s=timeout_s, allow_insecure_tls=allow_insecure_tls,
            render_settle_ms=render_settle_ms,
            max_file_size_bytes=max_file_size_bytes,
            include_html=include_html,
            include_document_base64=include_document_base64,
            warnings=warnings)
    except scraper.OptionError as e:
        return _rejected_payload(url, str(e), warnings)
    async with _call_engine(opts) as engine:
        fetched = await scraper.fetch_one(
            url, opts, engine=engine, respect_robots=respect_robots)
    return _fetch_payload(url, fetched, opts, warnings)


@mcp.tool()
async def fetch_urls_as_markdown(
    urls: list[str],
    render_mode: str | None = None,
    extract_docs: bool | None = None,
    concurrency: int = 4,
    delay_ms: int = 0,
    timeout_s: int | None = None,
    allow_insecure_tls: bool = False,
    render_settle_ms: int | None = None,
    max_file_size_bytes: int | None = None,
    respect_robots: bool = False,
    include_html: bool = False,
    include_document_base64: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Fetch many URLs (any hosts) as Markdown without crawling — the
    equivalent of the CLI's --file, and a way to retry a crawl's failed_urls.

    Each entry of `results` is exactly a fetch_url_as_markdown result, in
    input order. concurrency (default 4) is clamped to
    SCRAPER_MAX_CRAWL_CONCURRENCY; delay_ms paces request starts across the
    whole batch (raised to SCRAPER_MIN_DELAY_MS if lower); the URL list is
    truncated to SCRAPER_MAX_BATCH_URLS. Every other knob behaves as in
    fetch_url_as_markdown. Clamps are reported in `warnings`."""
    warnings: list[str] = []
    urls = list(urls or [])
    cap = settings.max_batch_urls()
    if len(urls) > cap:
        warnings.append(f"urls truncated from {len(urls)} to {cap} (server limit)")
        urls = urls[:cap]
    concurrency = settings.clamp("concurrency", int(concurrency), 1,
                                 settings.max_crawl_concurrency(), warnings)
    min_delay = settings.min_delay_ms()
    if delay_ms < min_delay:
        warnings.append(f"delay_ms={delay_ms} raised to {min_delay} (server minimum)")
        delay_ms = min_delay

    normalized: list[str | None] = []
    for u in urls:
        try:
            normalized.append(scraper.normalize_url(u))
        except ValueError:
            normalized.append(None)

    try:
        opts = scraper.resolve_options(
            render_mode=render_mode, extract_docs=extract_docs,
            timeout_s=timeout_s, allow_insecure_tls=allow_insecure_tls,
            render_settle_ms=render_settle_ms,
            max_file_size_bytes=max_file_size_bytes,
            include_html=include_html,
            include_document_base64=include_document_base64,
            warnings=warnings)
    except scraper.OptionError as e:
        results = [_rejected_payload(n or u, str(e), [])
                   for u, n in zip(urls, normalized)]
        return _batch_summary(results, warnings)

    results: list[dict[str, Any] | None] = [None] * len(urls)
    sem = asyncio.Semaphore(concurrency)
    pacer = _Pacer(delay_ms / 1000.0)
    robots_cache: dict[str, Any] = {}
    done = 0

    async def one(i: int, raw: str, url: str | None, engine) -> None:
        nonlocal done
        if url is None:
            results[i] = _rejected_payload(raw, "invalid URL", [])
        else:
            async with sem:
                await pacer.wait()
                fetched = await scraper.fetch_one(
                    url, opts, engine=engine, respect_robots=respect_robots,
                    robots_cache=robots_cache)
                results[i] = _fetch_payload(url, fetched, opts, [])
        done += 1
        if ctx is not None:
            try:
                await ctx.report_progress(progress=done, total=len(urls))
            except Exception:  # noqa: BLE001
                pass  # progress must never kill a batch

    async with _call_engine(opts) as engine:
        await asyncio.gather(*(one(i, raw, n, engine)
                               for i, (raw, n) in enumerate(zip(urls, normalized))))
    return _batch_summary(results, warnings)


def _batch_summary(results: list[dict[str, Any]], warnings: list[str]) -> dict[str, Any]:
    def count(status: str) -> int:
        return sum(1 for r in results if r["status"] == status)
    return {
        "count": len(results),
        "ok": count("ok"),
        "empty": count("empty"),
        "failed": count("failed"),
        "skipped": count("skipped"),
        "results": results,
        "warnings": warnings,
    }


def _playwright_browsers_dir() -> Path:
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env and env != "0":
        return Path(env)
    home = Path.home()
    if os.uname().sysname == "Darwin":
        return home / "Library" / "Caches" / "ms-playwright"
    return home / ".cache" / "ms-playwright"


@functools.cache
def _static_capabilities() -> dict[str, bool]:
    """Install-time facts (cached: they can't change while running)."""
    def has(module: str) -> bool:
        return importlib.util.find_spec(module) is not None

    browsers = _playwright_browsers_dir()
    chromium = browsers.is_dir() and any(
        p.name.startswith("chromium") for p in browsers.iterdir())
    return {
        "render": has("playwright") and chromium,
        "waf": has("curl_cffi"),
        "docs": has("pymupdf4llm") and has("markitdown"),
        "docling": has("docling"),
    }


def _capabilities() -> dict[str, Any]:
    ua = scraper._default_user_agent()
    m = re.search(r"Chrome/(\d+)", ua)
    return {
        **_static_capabilities(),
        # Booleans only: never the cookie file path or any cookie value.
        "cookies_file_configured": bool(os.environ.get("SCRAPE_CF_COOKIES")
                                        or os.environ.get("IB_CF_COOKIES")),
        "insecure_tls_allowed": settings.allow_insecure_tls(),
        "private_targets_allowed": settings.allow_private_targets(),
        "max_crawl_concurrency": settings.max_crawl_concurrency(),
        "max_batch_urls": settings.max_batch_urls(),
        "max_timeout_s": settings.max_timeout_s(),
        "user_agent_major": m.group(1) if m else "",
    }


@mcp.tool()
async def server_health() -> dict[str, Any]:
    """Return server health + summary stats. Cheap; no network calls."""
    try:
        registered_count = _store.count()
        db_ok = True
        db_error = None
    except Exception as e:  # noqa: BLE001
        registered_count = -1
        db_ok = False
        db_error = str(e)[:200]

    return {
        "ok": db_ok,
        "db_ok": db_ok,
        "db_error": db_error,
        "registered_count": registered_count,
        "last_resync_all_at": _last_resync_all_at,
        "last_resync_all_result": _last_resync_all_result,
        "openai_configured": bool(os.environ.get("OPENAI_API_KEY")),
        # Additive (0.3.0):
        "server_version": SERVER_VERSION,
        "scrape_website_version": scraper.SCRAPE_WEBSITE_VERSION,
        "capabilities": _capabilities(),
    }


@mcp.tool()
async def crawl_site(
    seed_url: str,
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
    concurrency: int = 1,
    timeout_s: int | None = None,
    allow_insecure_tls: bool = False,
    render_settle_ms: int | None = None,
    max_file_size_bytes: int | None = None,
    extra_exclude_patterns: list[str] | None = None,
    exclude_patterns_case_sensitive: bool = False,
    include_patterns: list[str] | None = None,
    additional_seed_urls: list[str] | None = None,
    collapse_host_aliases: bool = False,
    follow_offsite_documents: bool = False,
    dedupe_documents: bool = False,
    include_html: bool = False,
    include_document_base64: bool = False,
    max_total_bytes: int | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """BFS crawl from seed_url, returning markdown for every page reached.

    Scope: same FQDN by default (include_subdomains=True relaxes to eTLD+1;
    collapse_host_aliases=True treats www/non-www and http/https as one
    site). additional_seed_urls adds more depth-0 seeds (same scope).
    Limits: max_pages caps total fetches; max_depth caps BFS depth;
    max_total_bytes stops once the returned payload exceeds it.
    Politeness: delay_ms between requests (robots.txt Crawl-Delay overrides
    when declared); robots.txt respected by default. concurrency (default 1,
    capped by SCRAPER_MAX_CRAWL_CONCURRENCY) fetches in parallel under the
    same global pacing.
    Filtering: exclude_patterns (list of regex strings, defaults to CMS noise +
    images/static assets — NOT documents) drops matching URLs;
    extra_exclude_patterns appends to them; include_patterns, when set,
    keeps only matching URLs; matching is case-insensitive unless
    exclude_patterns_case_sensitive. strip_tracking_params removes
    UTM-style query params before dedup; use_sitemap seeds BFS from
    /sitemap.xml (sitemap-index aware).
    Rendering: render_mode 'auto' (default) headless-renders only un-hydrated
    SPA shells; 'always'/'never' force it.
    Documents: extract_docs (default true) downloads PDFs/Office files found
    during the crawl and converts them to Markdown; false skips fetching them.
    follow_offsite_documents fetches off-site document links (e.g.
    CDN-hosted PDFs); dedupe_documents skips byte-identical repeats.
    Fetch knobs (timeout_s, allow_insecure_tls, render_settle_ms,
    max_file_size_bytes, include_html, include_document_base64) behave as in
    fetch_url_as_markdown.
    """
    warnings: list[str] = []
    try:
        opts = scraper.resolve_options(
            timeout_s=timeout_s, allow_insecure_tls=allow_insecure_tls,
            render_settle_ms=render_settle_ms,
            max_file_size_bytes=max_file_size_bytes,
            include_html=include_html,
            include_document_base64=include_document_base64,
            warnings=warnings)
    except scraper.OptionError as e:
        return crawler.error_result(seed_url, str(e), warnings)

    async def _progress(fetched: int, queued: int) -> None:
        # Doubles as a keep-alive on the Streamable-HTTP stream during
        # multi-minute crawls (the platform holds this call open).
        if ctx is not None:
            await ctx.report_progress(progress=fetched, total=None)

    return await crawler.crawl(
        seed_url,
        max_pages=max_pages,
        max_depth=max_depth,
        delay_ms=delay_ms,
        respect_robots=respect_robots,
        include_subdomains=include_subdomains,
        exclude_patterns=exclude_patterns,
        strip_tracking_params=strip_tracking_params,
        use_sitemap=use_sitemap,
        render_mode=render_mode,
        extract_docs=extract_docs,
        progress_cb=_progress,
        concurrency=concurrency,
        fetch_options=opts,
        extra_exclude_patterns=extra_exclude_patterns,
        exclude_patterns_case_sensitive=exclude_patterns_case_sensitive,
        include_patterns=include_patterns,
        additional_seed_urls=additional_seed_urls,
        collapse_host_aliases=collapse_host_aliases,
        follow_offsite_documents=follow_offsite_documents,
        dedupe_documents=dedupe_documents,
        max_total_bytes=max_total_bytes,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# ASGI app
# ---------------------------------------------------------------------------

# FastMCP v2 exposes `http_app(path=...)` returning an ASGI app speaking
# Streamable HTTP. Wrap with bearer-auth middleware.
_inner = mcp.http_app(path="/mcp")
app = auth.wrap(_inner)
