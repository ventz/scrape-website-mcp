"""Async URL fetch + markdown extraction with rich result metadata.

Thin adapter over the ``scrape_website`` package's :class:`FetchEngine` —
the SAME tiered fetcher the standalone CLI uses, so upstream improvements
(retry/backoff + Retry-After, curl_cffi WAF/403 fallback + cookie bridge,
headless-Chromium SPA render escalation, PDF/Office -> Markdown extraction)
genuinely flow through to this server.

`fetch_and_extract(url)` returns a `FetchResult` carrying not just the
markdown but the diagnostics the admin UI surfaces: http_status, content
length, fetch duration, page title, ETag/Last-Modified, and error string
(when applicable). This shape lines up 1:1 with the Store columns; the
fields added in 0.2.0 (`rendered`, `via`, `content_kind`) are strictly
additive so existing consumers are unaffected.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import lxml.html

from scrape_website.config import CONFIG as _UPSTREAM_CONFIG
from scrape_website.extract import (
    _extract_document_to_markdown,
    _extract_text_trafilatura,
    _parse_and_extract,
)
from scrape_website.fetch import FetchEngine, should_download_file
from scrape_website.urls import _normalize_url

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Engine configuration (env-driven server defaults; per-call params override)
# ---------------------------------------------------------------------------

def _default_user_agent() -> str:
    """Default UA is upstream's Chrome UA (SCRAPER_USER_AGENT / SCRAPE_USER_AGENT
    override). Deliberate 0.2.0 policy change from the old honest bot UA: the
    curl_cffi WAF tier replays cookies bound to a real-Chrome UA, so a bot UA
    would neuter it. Set SCRAPER_USER_AGENT to restore the honest UA."""
    return (os.environ.get("SCRAPER_USER_AGENT")
            or _UPSTREAM_CONFIG["user_agent"])


def _default_browser_args() -> list[str]:
    raw = os.environ.get("SCRAPER_BROWSER_ARGS")
    if raw is not None:
        return raw.split()
    if os.environ.get("SCRAPER_IN_DOCKER"):
        # Container Chromium: tiny /dev/shm and a root user are the norm.
        return ["--disable-dev-shm-usage", "--no-sandbox"]
    return []


def render_mode_default() -> str:
    mode = os.environ.get("SCRAPER_RENDER_MODE", "auto").lower()
    return mode if mode in ("auto", "never", "always") else "auto"


def extract_docs_default() -> bool:
    return os.environ.get("SCRAPER_EXTRACT_DOCS", "1").lower() not in (
        "0", "false", "no", "off")


def max_file_size() -> int:
    return int(os.environ.get("SCRAPER_MAX_FILE_SIZE", str(50 * 1024 * 1024)))


def build_engine(*, respect_robots: bool = True,
                 delay_between_requests: float | None = None) -> FetchEngine:
    """A FetchEngine configured from this server's env. The crawler builds a
    fresh one per crawl (robots/Crawl-Delay state is per-host); single-page
    fetches share the module singleton below."""
    return FetchEngine(
        user_agent=_default_user_agent(),
        timeout=int(os.environ.get("SCRAPER_TIMEOUT", "30")),
        delay_between_requests=delay_between_requests,
        render_mode=render_mode_default(),
        respect_robots=respect_robots,
        browser_launch_args=_default_browser_args(),
        logger=log,
    )


_engine_instance: FetchEngine | None = None


def _engine() -> FetchEngine:
    """Lazy module-wide engine for single-page fetches: one shared aiohttp
    session + one lazy Chromium for the process lifetime. Never loads robots
    (single fetches are operator-initiated, matching 0.1.x behavior)."""
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = build_engine()
    return _engine_instance


def reset_engine() -> None:
    """Test hook: drop the singleton so env changes take effect."""
    global _engine_instance
    _engine_instance = None


# Kept for tests / back-compat: same UA the server presents everywhere.
_DEFAULT_UA = _default_user_agent()


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
    # Additive diagnostics (0.2.0):
    rendered: bool = False        # markdown came from a headless-Chromium snapshot
    via: str = "aiohttp"          # 'aiohttp' | 'curl_cffi' | 'playwright'
    content_kind: str = "html"    # 'html' | 'pdf' | 'docx' | 'txt' | ...


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
    """Extract clean markdown from HTML (upstream extractor, YAML front matter
    included — front matter helps RAG retrieval). Empty string on failure."""
    return _extract_text_trafilatura(html, url) or ""


def _file_kind(url: str, content_type: str | None) -> str:
    ext = os.path.splitext(urlsplit(url).path)[1].lower().lstrip(".")
    if ext:
        return ext
    if content_type and "pdf" in content_type:
        return "pdf"
    return "bin"


async def _document_to_markdown(content: bytes, url: str, kind: str) -> str | None:
    """Write document bytes to a temp file and run the upstream extractor
    (PyMuPDF4LLM / MarkItDown) off-thread. The temp file keeps the URL's
    basename because the extractor uses the filename as the front-matter
    title (which surfaces in vector-store citations)."""
    host = urlsplit(url).netloc
    basename = os.path.basename(urlsplit(url).path) or f"document.{kind}"
    tmpdir = tempfile.mkdtemp(prefix="scrape-mcp-doc-")
    path = os.path.join(tmpdir, basename)
    try:
        with open(path, "wb") as fh:
            fh.write(content)
        return await asyncio.to_thread(
            _extract_document_to_markdown, path, url, host)
    finally:
        try:
            os.unlink(path)
            os.rmdir(tmpdir)
        except OSError:
            pass


async def fetch_page_result(
    url: str,
    *,
    run_extract,
    render_mode: str | None = None,
    extract_docs: bool | None = None,
    engine: FetchEngine | None = None,
) -> tuple[FetchResult, set[str]]:
    """Core fetch pipeline shared by `fetch_and_extract` (single page) and the
    BFS crawler: tiered fetch -> extraction (via *run_extract*) -> SPA render
    escalation -> FetchResult mapping. Returns ``(result, links)`` where
    *links* are whatever *run_extract* produced for the FINAL (possibly
    rendered) HTML — the crawler passes its own scope-aware link extractor.

    Never raises for transport errors — failures go into FetchResult.error
    and result.status='failed'."""
    started = time.perf_counter()
    result = FetchResult(url=url)
    eng = engine or _engine()
    do_docs = extract_docs if extract_docs is not None else extract_docs_default()

    try:
        outcome, links, text = await eng.fetch_page(
            url, run_extract=run_extract, render_mode=render_mode)
    except Exception as e:  # noqa: BLE001
        result.error = f"{type(e).__name__}: {e}"[:240]
        result.status = "failed"
        result.fetch_duration_ms = int((time.perf_counter() - started) * 1000)
        return result, set()

    result.fetch_duration_ms = int((time.perf_counter() - started) * 1000)
    result.http_status = outcome.status
    result.etag = outcome.headers.get("ETag")
    result.last_modified = outcome.headers.get("Last-Modified")
    result.via = outcome.via
    result.rendered = outcome.rendered

    if outcome.status >= 400 or outcome.denied:
        result.status = "failed"
        result.error = f"HTTP {outcome.status}"
        return result, set()

    if outcome.kind == "file":
        result.content_kind = _file_kind(url, outcome.content_type)
        result.content_bytes = len(outcome.content)
        result.page_title = os.path.basename(urlsplit(url).path) or None
        if not do_docs:
            result.status = "empty"
            result.error = "document extraction disabled (extract_docs=false)"
            return result, set()
        if len(outcome.content) > max_file_size():
            result.status = "failed"
            result.error = (f"file too large "
                            f"({len(outcome.content) / (1024 * 1024):.1f} MB)")
            return result, set()
        markdown = await _document_to_markdown(
            outcome.content, url, result.content_kind)
        result.markdown = markdown or ""
        if not result.markdown.strip():
            result.status = "empty"
        return result, set()

    html = outcome.content
    result.content_bytes = len(html.encode("utf-8", errors="replace"))
    result.page_title = _extract_title(html)
    result.markdown = text or ""
    if not result.markdown.strip():
        result.status = "empty"
    return result, links


async def fetch_and_extract(
    url: str,
    *,
    render_mode: str | None = None,
    extract_docs: bool | None = None,
    engine: FetchEngine | None = None,
) -> FetchResult:
    """Fetch the URL through the tiered engine, extract markdown, and capture
    all the diagnostic state the admin UI needs. Never raises for transport
    errors — failures go into FetchResult.error and result.status='failed'.

    ``render_mode`` / ``extract_docs`` override the env defaults per call;
    ``engine`` lets a caller supply its own engine (e.g. one with robots +
    Crawl-Delay state loaded)."""
    base_domain = urlsplit(url).netloc

    async def run_extract(html: str, page_url: str):
        # Upstream combined link+text extraction; the link set only feeds the
        # SPA-shell heuristic here (single fetches follow nothing).
        return await asyncio.to_thread(
            _parse_and_extract, html, page_url, base_domain, False, None)

    result, _links = await fetch_page_result(
        url, run_extract=run_extract, render_mode=render_mode,
        extract_docs=extract_docs, engine=engine)
    return result


# Back-compat helper used by older tests; keep a thin wrapper.
async def scrape(url: str) -> str:
    r = await fetch_and_extract(url)
    if r.error:
        raise RuntimeError(r.error)
    return r.markdown


async def fetch_html(url: str) -> str:
    """Plain HTML fetch — used by tests; raises on non-2xx."""
    outcome = await _engine().fetch(url)
    if outcome.status >= 400:
        raise RuntimeError(f"HTTP {outcome.status} for {url}")
    if outcome.kind != "html":
        raise RuntimeError(f"non-HTML response for {url}")
    return outcome.content
