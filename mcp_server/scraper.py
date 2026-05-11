"""Async URL fetch + markdown extraction with rich result metadata.

Reuses upstream `ventz/scrape-website`'s URL canonicalization. The text
extraction path calls trafilatura directly with `output_format='markdown'`
(upstream's `_extract_text_trafilatura` hard-codes 'txt').

`fetch_and_extract(url)` returns a `FetchResult` carrying not just the
markdown but the diagnostics the admin UI surfaces: http_status, content
length, fetch duration, page title, ETag/Last-Modified, and error string
(when applicable). This shape lines up 1:1 with the new Store columns.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import lxml.html
import trafilatura

# Make ventz/scrape-website importable. Two layouts supported:
#   - local dev:  vendor/scrape-website/  (cloned by `make setup`)
#   - docker:     /opt/scrape-website/    (cloned in Dockerfile)
_VENDORED = Path(__file__).resolve().parent.parent / "vendor" / "scrape-website"
_DOCKERED = Path("/opt/scrape-website")
_found = False
for _candidate in (_VENDORED, _DOCKERED):
    if _candidate.exists():
        _found = True
        cand = str(_candidate)
        if cand not in sys.path:
            sys.path.insert(0, cand)
        break
if not _found:
    raise RuntimeError(
        "ventz/scrape-website not found. For local dev run `make setup`; "
        "for Docker rebuild the image."
    )

from app import _normalize_url  # noqa: E402  (upstream helper)


_DEFAULT_UA = os.environ.get(
    "SCRAPER_USER_AGENT",
    "scrape-website-mcp/0.1 (+https://github.com/ventz/scrape-website-mcp)",
)
_TIMEOUT = aiohttp.ClientTimeout(total=int(os.environ.get("SCRAPER_TIMEOUT", "30")))


@dataclass
class FetchResult:
    url: str
    markdown: str = ""
    http_status: int | None = None
    content_bytes: int | None = None
    fetch_duration_ms: int | None = None
    page_title: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None
    # "ok" | "empty" | "failed" — "skipped" is reserved for callers (e.g. robots).
    status: str = "ok"


def normalize_url(url: str) -> str:
    """Canonicalize a URL (delegates to upstream scrape-website helper)."""
    return _normalize_url(url)


def _extract_title(html: str) -> str | None:
    """Cheap title extraction — first <title>, fallback to first <h1>."""
    try:
        doc = lxml.html.fromstring(html)
    except Exception:
        return None
    title_el = doc.find(".//title")
    if title_el is not None and (title_el.text or "").strip():
        return title_el.text.strip()[:240]
    h1 = doc.find(".//h1")
    if h1 is not None:
        text = (h1.text_content() or "").strip()
        if text:
            return text[:240]
    return None


def html_to_markdown(html: str, url: str) -> str:
    """Extract clean markdown from HTML. Returns empty string if extraction fails."""
    text = trafilatura.extract(
        html,
        url=url,
        include_comments=False,
        include_tables=True,
        include_links=True,
        include_images=False,
        favor_recall=True,
        deduplicate=True,
        output_format="markdown",
    )
    return text or ""


async def fetch_and_extract(url: str) -> FetchResult:
    """Fetch the URL, extract markdown, capture all the diagnostic state the
    admin UI needs. Never raises for transport errors — failures go into
    FetchResult.error and result.status='failed'."""
    started = time.perf_counter()
    result = FetchResult(url=url)
    try:
        async with aiohttp.ClientSession(
            timeout=_TIMEOUT, headers={"User-Agent": _DEFAULT_UA}
        ) as session:
            async with session.get(url, allow_redirects=True) as resp:
                result.http_status = resp.status
                result.etag = resp.headers.get("ETag")
                result.last_modified = resp.headers.get("Last-Modified")
                if resp.status >= 400:
                    result.status = "failed"
                    result.error = f"HTTP {resp.status}"
                    return result
                html = await resp.text(errors="replace")
    except Exception as e:  # noqa: BLE001
        result.error = f"{type(e).__name__}: {e}"[:240]
        result.status = "failed"
        return result
    finally:
        result.fetch_duration_ms = int((time.perf_counter() - started) * 1000)

    result.content_bytes = len(html.encode("utf-8", errors="replace"))
    result.page_title = _extract_title(html)
    result.markdown = html_to_markdown(html, url)
    if not result.markdown.strip():
        result.status = "empty"
    return result


# Back-compat helper used by older tests; keep a thin wrapper.
async def scrape(url: str) -> str:
    r = await fetch_and_extract(url)
    if r.error:
        raise RuntimeError(r.error)
    return r.markdown


async def fetch_html(url: str) -> str:
    """Plain HTML fetch — used by tests; raises on non-2xx."""
    async with aiohttp.ClientSession(
        timeout=_TIMEOUT, headers={"User-Agent": _DEFAULT_UA}
    ) as session:
        async with session.get(url, allow_redirects=True) as resp:
            resp.raise_for_status()
            return await resp.text()
