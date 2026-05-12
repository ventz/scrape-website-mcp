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
import hashlib
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from fastmcp import FastMCP
from openai import AsyncOpenAI

from mcp_server import auth, crawler, openai_sync, scraper
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


@mcp.tool()
async def fetch_url_as_markdown(url: str) -> dict[str, Any]:
    """Live one-shot scrape — no vector store, no state. Returns markdown."""
    url = scraper.normalize_url(url)
    fetched = await scraper.fetch_and_extract(url)
    return {
        "url": url,
        "markdown": fetched.markdown,
        "length": len(fetched.markdown),
        "http_status": fetched.http_status,
        "page_title": fetched.page_title,
        "status": fetched.status,
        "error": fetched.error,
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
) -> dict[str, Any]:
    """BFS crawl from seed_url, returning markdown for every page reached.

    Scope: same FQDN by default (include_subdomains=True relaxes to eTLD+1).
    Limits: max_pages caps total fetches; max_depth caps BFS depth.
    Politeness: delay_ms between requests; robots.txt respected by default.
    Filtering: exclude_patterns (list of regex strings, defaults to images/PDFs/
    static assets) drops matching URLs; strip_tracking_params removes UTM-style
    query params before dedup; use_sitemap seeds BFS from /sitemap.xml.
    """
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
    )


# ---------------------------------------------------------------------------
# ASGI app
# ---------------------------------------------------------------------------

# FastMCP v2 exposes `http_app(path=...)` returning an ASGI app speaking
# Streamable HTTP. Wrap with bearer-auth middleware.
_inner = mcp.http_app(path="/mcp")
app = auth.wrap(_inner)
