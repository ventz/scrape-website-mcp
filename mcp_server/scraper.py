"""Async URL fetch + markdown extraction with rich result metadata.

Thin adapter over the ``scrape_website`` package's :class:`FetchEngine` —
the SAME tiered fetcher the standalone CLI uses, so upstream improvements
(retry/backoff + Retry-After, curl_cffi WAF/403 fallback + cookie bridge,
headless-Chromium SPA render escalation, PDF/Office -> Markdown extraction)
genuinely flow through to this server. Every engine built here is a
:class:`~mcp_server.netguard.GuardedFetchEngine`, so the SSRF guard covers
all tiers.

`fetch_and_extract(url)` returns a `FetchResult` carrying not just the
markdown but the diagnostics the admin UI surfaces: http_status, content
length, fetch duration, page title, ETag/Last-Modified, and error string
(when applicable). This shape lines up 1:1 with the Store columns; fields
added since (`rendered`, `via`, `content_kind` in 0.2.0; classification,
content type, metadata, raw html/document in 0.3.0) are strictly additive.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import mimetypes
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import lxml.html

from scrape_website import __version__ as SCRAPE_WEBSITE_VERSION  # noqa: F401
from scrape_website.config import CONFIG as _UPSTREAM_CONFIG
from scrape_website.config import DOWNLOADABLE_EXTENSIONS
from scrape_website.extract import (
    _extract_document_to_markdown,
    _extract_links_lxml,
    _extract_text_trafilatura,
)
from scrape_website.fetch import FetchEngine, should_download_file  # noqa: F401
from scrape_website.urls import _normalize_url

from mcp_server import settings
from mcp_server.netguard import BlockedTargetError, GuardedFetchEngine

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


# Alias for functions whose `max_file_size` parameter shadows the above.
_server_max_file_size = max_file_size


def build_engine(*, respect_robots: bool = True,
                 delay_between_requests: float | None = None,
                 timeout: int | None = None,
                 allow_insecure_tls: bool = False,
                 render_settle_ms: int | None = None,
                 max_file_size: int | None = None) -> FetchEngine:
    """A guarded FetchEngine configured from this server's env. The crawler
    builds a fresh one per crawl (robots/Crawl-Delay state is per-host);
    single-page fetches share the module singleton below unless a call
    overrides an engine-level knob (timeout, TLS, settle, file-size cap)."""
    return GuardedFetchEngine(
        block_private=not settings.allow_private_targets(),
        user_agent=_default_user_agent(),
        timeout=timeout if timeout is not None else settings.timeout_s(),
        max_retries=settings.max_retries(),
        delay_between_requests=delay_between_requests,
        render_timeout=settings.render_timeout(),
        render_settle_ms=(render_settle_ms if render_settle_ms is not None
                          else settings.render_settle_ms()),
        render_concurrency=settings.render_concurrency(),
        # P0-1: hand the server cap to the engine so the aiohttp tier skips
        # oversized documents BEFORE buffering them (it checks
        # Content-Length, then bails mid-stream). fetch_page_result keeps a
        # post-download check for the curl_cffi/browser tiers.
        max_file_size=(max_file_size if max_file_size is not None
                       else _server_max_file_size()),
        max_page_size=settings.max_page_size(),
        render_mode=render_mode_default(),
        allow_insecure_tls=allow_insecure_tls,
        respect_robots=respect_robots,
        browser_launch_args=_default_browser_args(),
        logger=log,
    )


_engine_instance: FetchEngine | None = None


def _engine() -> FetchEngine:
    """Lazy module-wide engine for single-page fetches: one shared aiohttp
    session + one lazy Chromium for the process lifetime. Never loads robots
    (single fetches check robots only on request, via robots_allowed())."""
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


# ---------------------------------------------------------------------------
# Per-call options (P1-2)
# ---------------------------------------------------------------------------

class OptionError(ValueError):
    """A caller asked for something this server's env doesn't allow."""


INSECURE_TLS_DISABLED = "insecure TLS disabled on this server"


@dataclass
class FetchOptions:
    render_mode: str | None = None
    extract_docs: bool | None = None
    timeout_s: int | None = None
    allow_insecure_tls: bool = False
    render_settle_ms: int | None = None
    max_file_size_bytes: int | None = None
    include_html: bool = False
    include_document_base64: bool = False

    def engine_overrides(self) -> dict[str, Any]:
        """Engine-level kwargs this call changes. Non-empty means the call
        needs its own engine (TLS mode, timeout, settle and the file cap
        are fixed per engine, and the singleton is shared)."""
        kw: dict[str, Any] = {}
        if self.timeout_s is not None:
            kw["timeout"] = self.timeout_s
        if self.allow_insecure_tls:
            kw["allow_insecure_tls"] = True
        if self.render_settle_ms is not None:
            kw["render_settle_ms"] = self.render_settle_ms
        if self.max_file_size_bytes is not None:
            kw["max_file_size"] = self.max_file_size_bytes
        return kw

    def effective_max_file_size(self) -> int:
        return self.max_file_size_bytes or max_file_size()


def resolve_options(*, render_mode: str | None = None,
                    extract_docs: bool | None = None,
                    timeout_s: int | None = None,
                    allow_insecure_tls: bool = False,
                    render_settle_ms: int | None = None,
                    max_file_size_bytes: int | None = None,
                    include_html: bool = False,
                    include_document_base64: bool = False,
                    warnings: list[str]) -> FetchOptions:
    """Validate + clamp caller knobs against the server caps. Clamps are
    reported in *warnings*; a disabled unsafe toggle raises OptionError."""
    if allow_insecure_tls and not settings.allow_insecure_tls():
        raise OptionError(INSECURE_TLS_DISABLED)
    if timeout_s is not None:
        timeout_s = settings.clamp("timeout_s", int(timeout_s), 1,
                                   settings.max_timeout_s(), warnings)
    if render_settle_ms is not None:
        render_settle_ms = settings.clamp(
            "render_settle_ms", int(render_settle_ms), 0,
            settings.MAX_RENDER_SETTLE_MS, warnings)
    if max_file_size_bytes is not None:
        # Can only LOWER the server cap.
        max_file_size_bytes = settings.clamp(
            "max_file_size_bytes", int(max_file_size_bytes), 1,
            max_file_size(), warnings)
    if allow_insecure_tls and os.environ.get("SCRAPE_CF_COOKIES"):
        warnings.append("allow_insecure_tls with a cookies file configured: "
                        "replayed cookies are exposed to a man-in-the-middle")
    return FetchOptions(
        render_mode=render_mode, extract_docs=extract_docs,
        timeout_s=timeout_s, allow_insecure_tls=allow_insecure_tls,
        render_settle_ms=render_settle_ms,
        max_file_size_bytes=max_file_size_bytes,
        include_html=include_html,
        include_document_base64=include_document_base64,
    )


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

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
    # "ok" | "empty" | "failed" | "skipped" (robots / duplicate document).
    status: str = "ok"
    # Additive diagnostics (0.2.0):
    rendered: bool = False        # markdown came from a headless-Chromium snapshot
    via: str = "aiohttp"          # 'aiohttp' | 'curl_cffi' | 'playwright'
    content_kind: str = "html"    # 'html' | 'pdf' | 'docx' | 'txt' | ...
    # Additive (0.3.0):
    # 'content' | 'challenge' | 'not_found' | 'denied' | 'search'; None when
    # no response was classified (transport error, blocked, skipped).
    classification: str | None = None
    classification_detail: str = ""
    content_type: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    duplicate_of: str | None = None
    # Raw payloads, only kept when the caller asked (include_html /
    # include_document_base64) — never kept by default.
    html: str | None = field(default=None, repr=False)
    document: bytes | None = field(default=None, repr=False)


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


# P0-3: trafilatura's dedup cache (LRU_TEST) is process-global, and upstream
# clears it at the start of each extraction. Extraction runs in worker
# threads here, so two overlapping extractions (concurrent tool calls, or
# crawl concurrency) could clear/fill each other's cache and silently drop
# content that repeats across pages. Serialize the trafilatura call.
_EXTRACT_LOCK = threading.Lock()


def html_to_markdown(html: str, url: str) -> str:
    """Extract clean markdown from HTML (upstream extractor, YAML front matter
    included — front matter helps RAG retrieval). Empty string on failure."""
    with _EXTRACT_LOCK:
        return _extract_text_trafilatura(html, url) or ""


def _parse_and_extract_locked(html: str, url: str,
                              base_domain: str) -> tuple[set[str], str | None]:
    """Upstream ``_parse_and_extract`` with the extraction lock held."""
    links = _extract_links_lxml(html, url, base_domain)
    return links, (html_to_markdown(html, url) or None)


def parse_front_matter(markdown: str) -> dict[str, str]:
    """Parse the simple ``key: value`` YAML front matter that upstream puts
    at the top of every page/document (title, url, hostname, sitename,
    date, filetype...). Returns {} when there is none."""
    if not markdown.startswith("---\n"):
        return {}
    end = markdown.find("\n---", 4)
    if end == -1:
        return {}
    meta: dict[str, str] = {}
    for line in markdown[4:end].splitlines():
        key, sep, value = line.partition(":")
        if not sep or not key.strip() or key.startswith((" ", "\t", "-")):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        meta[key.strip()] = value
    return meta


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

# Explicit table first (container mimetypes tables vary), mimetypes second.
_MIME_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "application/zip": ".zip",
    "application/rtf": ".rtf",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/vnd.oasis.opendocument.presentation": ".odp",
}


def _content_type_extension(content_type: str | None) -> str | None:
    """Downloadable extension for a Content-Type (mirrors upstream
    ``WebsiteScraper.get_file_extension``), or None."""
    if not content_type:
        return None
    mime = content_type.lower().split(";")[0].strip()
    ext = _MIME_EXTENSIONS.get(mime) or mimetypes.guess_extension(mime)
    return ext if ext in DOWNLOADABLE_EXTENSIONS else None


def _url_extension(url: str) -> str:
    return os.path.splitext(urlsplit(url).path)[1].lower()


def _file_kind(url: str, content_type: str | None) -> str:
    """Document kind: URL extension when it's a known document type, else
    derived from Content-Type (P0-2: extension-less /resource/guidance
    served as DOCX is 'docx', not 'bin')."""
    ext = _url_extension(url)
    if ext in DOWNLOADABLE_EXTENSIONS:
        return ext.lstrip(".")
    ct_ext = _content_type_extension(content_type)
    if ct_ext:
        return ct_ext.lstrip(".")
    if ext:
        return ext.lstrip(".")
    return "bin"


def _document_filename(url: str, content_type: str | None, kind: str) -> str:
    """Temp filename for the extractor. It keeps the URL's basename because
    the extractor uses the filename as the front-matter title (which
    surfaces in vector-store citations).

    P0-2: when the basename has no document extension, append one derived
    from Content-Type (as upstream 0.7.2 does) — upstream's extractor picks
    its converter from the extension, and only sniffs %PDF magic, so an
    extension-less DOCX/XLSX/PPTX otherwise extracts to nothing. PDFs are
    left alone: the sniff already handles them, and renaming would change
    their front-matter title and so their content hash."""
    basename = os.path.basename(urlsplit(url).path)
    if basename in ("", ".", ".."):
        return f"document.{kind}"
    stem, ext = os.path.splitext(basename)
    if len(basename.encode("utf-8")) > 200:
        basename = stem[:150] + ext[:20]
    if ext.lower() not in DOWNLOADABLE_EXTENSIONS:
        ct_ext = _content_type_extension(content_type)
        if ct_ext and ct_ext != ".pdf":
            basename += ct_ext
    return basename


async def _document_to_markdown(content: bytes, url: str,
                                content_type: str | None = None,
                                kind: str = "bin") -> str | None:
    """Write document bytes to a temp file and run the upstream extractor
    (PyMuPDF4LLM / MarkItDown) off-thread."""
    host = urlsplit(url).netloc
    tmpdir = tempfile.mkdtemp(prefix="scrape-mcp-doc-")
    path = os.path.join(tmpdir, _document_filename(url, content_type, kind))
    try:
        with open(path, "wb") as fh:
            fh.write(content)
        return await asyncio.to_thread(
            _extract_document_to_markdown, path, url, host)
    except OSError as e:
        log.info("document temp write failed for %s: %s", url, e)
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Fetch pipeline
# ---------------------------------------------------------------------------

def _failure_error(outcome) -> str:
    """Error string for a non-content response. Classification-driven
    failures say what was detected instead of a bare (and, for 200
    interstitials, misleading) 'HTTP 200'."""
    cls, detail, status = outcome.classification, outcome.detail, outcome.status
    if cls == "challenge":
        return f"challenge: {detail} (HTTP {status})"
    if status < 400 and cls in ("denied", "not_found"):
        return f"{cls}: {detail} (HTTP {status})"
    return f"HTTP {status}"


async def fetch_page_result(
    url: str,
    *,
    run_extract,
    render_mode: str | None = None,
    extract_docs: bool | None = None,
    engine: FetchEngine | None = None,
    max_file_size: int | None = None,
    keep_html: bool = False,
    keep_document: bool = False,
    doc_seen: dict[str, str] | None = None,
) -> tuple[FetchResult, set[str]]:
    """Core fetch pipeline shared by `fetch_and_extract` (single page) and the
    BFS crawler: tiered fetch -> extraction (via *run_extract*) -> SPA render
    escalation -> FetchResult mapping. Returns ``(result, links)`` where
    *links* are whatever *run_extract* produced for the FINAL (possibly
    rendered) HTML — the crawler passes its own scope-aware link extractor.

    *max_file_size*: per-call document cap (defaults to the server cap).
    *keep_html* / *keep_document*: retain the raw payload on the result.
    *doc_seen*: md5 -> first URL map; when given, a byte-identical document
    already seen comes back ``status='skipped'`` with ``duplicate_of`` set.

    Never raises for transport errors — failures go into FetchResult.error
    and result.status='failed'."""
    started = time.perf_counter()
    result = FetchResult(url=url)
    eng = engine or _engine()
    do_docs = extract_docs if extract_docs is not None else extract_docs_default()
    file_cap = max_file_size if max_file_size is not None else _server_max_file_size()

    try:
        outcome, links, text = await eng.fetch_page(
            url, run_extract=run_extract, render_mode=render_mode)
    except BlockedTargetError as e:
        result.error = f"blocked: {e}"[:240]
        result.status = "failed"
        result.fetch_duration_ms = int((time.perf_counter() - started) * 1000)
        return result, set()
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
    result.content_type = outcome.content_type or None
    result.classification = outcome.classification
    result.classification_detail = outcome.detail or ""

    if outcome.kind != "file" and keep_html:
        result.html = outcome.content

    if outcome.status >= 400 or outcome.denied:
        result.status = "failed"
        result.error = _failure_error(outcome)
        return result, set()

    if outcome.kind == "file":
        result.content_kind = _file_kind(url, outcome.content_type)
        result.content_bytes = len(outcome.content)
        result.page_title = os.path.basename(urlsplit(url).path) or None
        if not do_docs:
            result.status = "empty"
            result.error = "document extraction disabled (extract_docs=false)"
            return result, set()
        if outcome.detail == "file too large" or len(outcome.content) > file_cap:
            # aiohttp skips before buffering (content is b'', detail set);
            # curl_cffi/browser tiers still buffer, hence the length check.
            clen = outcome.headers.get("Content-Length", "")
            if clen.isdigit():
                result.content_bytes = int(clen)
            result.status = "failed"
            result.error = (f"file too large "
                            f"(> {file_cap / (1024 * 1024):.1f} MB cap)")
            return result, set()
        if doc_seen is not None:
            digest = hashlib.md5(outcome.content).hexdigest()  # noqa: S324 (dedup key, not security)
            first = doc_seen.get(digest)
            if first is not None:
                result.status = "skipped"
                result.error = "duplicate document"
                result.duplicate_of = first
                return result, set()
            doc_seen[digest] = url
        if keep_document:
            result.document = outcome.content
        markdown = await _document_to_markdown(
            outcome.content, url, outcome.content_type, result.content_kind)
        result.markdown = markdown or ""
        result.metadata = parse_front_matter(result.markdown)
        if not result.markdown.strip():
            result.status = "empty"
        return result, set()

    html = outcome.content
    result.content_bytes = len(html.encode("utf-8", errors="replace"))
    result.page_title = _extract_title(html)
    result.markdown = text or ""
    result.metadata = parse_front_matter(result.markdown)
    if not result.markdown.strip():
        result.status = "empty"
    return result, links


async def fetch_and_extract(
    url: str,
    *,
    render_mode: str | None = None,
    extract_docs: bool | None = None,
    engine: FetchEngine | None = None,
    max_file_size: int | None = None,
    keep_html: bool = False,
    keep_document: bool = False,
) -> FetchResult:
    """Fetch the URL through the tiered engine, extract markdown, and capture
    all the diagnostic state the admin UI needs. Never raises for transport
    errors — failures go into FetchResult.error and result.status='failed'.

    ``render_mode`` / ``extract_docs`` override the env defaults per call;
    ``engine`` lets a caller supply its own engine (e.g. one with robots +
    Crawl-Delay state loaded, or a per-call timeout/TLS mode)."""
    base_domain = urlsplit(url).netloc

    async def run_extract(html: str, page_url: str):
        # Combined link+text extraction; the link set only feeds the
        # SPA-shell heuristic here (single fetches follow nothing).
        return await asyncio.to_thread(
            _parse_and_extract_locked, html, page_url, base_domain)

    result, _links = await fetch_page_result(
        url, run_extract=run_extract, render_mode=render_mode,
        extract_docs=extract_docs, engine=engine,
        max_file_size=max_file_size, keep_html=keep_html,
        keep_document=keep_document)
    return result


async def robots_allowed(url: str, *, opts: FetchOptions | None = None,
                         cache: dict[str, FetchEngine] | None = None) -> bool:
    """Opt-in robots.txt check for single fetches (P1-2 ``respect_robots``).
    *cache* (origin -> loaded engine) lets a batch load each host's robots
    once. Missing/unreadable robots.txt allows the fetch."""
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    eng = cache.get(origin) if cache is not None else None
    if eng is None:
        overrides = opts.engine_overrides() if opts else {}
        eng = build_engine(
            respect_robots=True,
            timeout=overrides.get("timeout"),
            allow_insecure_tls=overrides.get("allow_insecure_tls", False))
        try:
            guard = getattr(eng, "guard_url", None)
            if guard is not None:
                try:
                    await guard(url)
                except BlockedTargetError:
                    return True  # the fetch itself reports the block
            await eng.load_robots(url)
        finally:
            await eng.close()
        if cache is not None:
            cache[origin] = eng
    return eng.robots_allows(url)


async def fetch_one(url: str, opts: FetchOptions, *,
                    engine: FetchEngine | None = None,
                    respect_robots: bool = False,
                    robots_cache: dict[str, FetchEngine] | None = None,
                    ) -> FetchResult:
    """Single-URL fetch with per-call options (used by fetch_url_as_markdown
    and fetch_urls_as_markdown)."""
    if respect_robots and not await robots_allowed(
            url, opts=opts, cache=robots_cache):
        return FetchResult(url=url, status="skipped",
                           error="robots.txt disallows")
    return await fetch_and_extract(
        url, render_mode=opts.render_mode, extract_docs=opts.extract_docs,
        engine=engine, max_file_size=opts.effective_max_file_size(),
        keep_html=opts.include_html,
        keep_document=opts.include_document_base64)


def result_extras(fr: FetchResult, opts: FetchOptions | None,
                  warnings: list[str] | None = None) -> dict[str, Any]:
    """Additive (0.3.0) result keys shared by every tool's per-page dicts.
    Raw HTML / document bytes appear only when the caller opted in, and are
    capped by SCRAPER_MAX_INLINE_HTML_BYTES / SCRAPER_MAX_INLINE_DOC_BYTES."""
    out: dict[str, Any] = {
        "classification": fr.classification,
        "classification_detail": fr.classification_detail,
        "content_type": fr.content_type,
        "metadata": fr.metadata,
        "duplicate_of": fr.duplicate_of,
    }
    if opts is not None and opts.include_html:
        html = fr.html
        truncated = False
        if html is not None:
            cap = settings.max_inline_html_bytes()
            raw = html.encode("utf-8", errors="replace")
            if len(raw) > cap:
                html = raw[:cap].decode("utf-8", errors="ignore")
                truncated = True
        out["html"] = html
        out["html_truncated"] = truncated
    if opts is not None and opts.include_document_base64 and fr.document is not None:
        size = len(fr.document)
        out["document_bytes"] = size
        if size <= settings.max_inline_doc_bytes():
            out["document_base64"] = base64.b64encode(fr.document).decode("ascii")
        elif warnings is not None:
            warnings.append(
                f"document_base64 omitted for {fr.url}: {size} bytes exceeds "
                f"the {settings.max_inline_doc_bytes()}-byte inline cap")
    return out


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
