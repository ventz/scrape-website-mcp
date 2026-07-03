"""Tests for the BFS crawler.

No real network traffic: the shared FetchEngine is replaced by a FakeEngine
served from canned pages (patched at the ``mcp_server.scraper.build_engine``
seam the crawler uses to construct its per-crawl engine).
"""

from __future__ import annotations

from unittest.mock import patch
from urllib.parse import urlsplit

import pytest

from scrape_website.extract import is_access_denied
from scrape_website.fetch import FetchOutcome, should_download_file

from mcp_server import crawler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_html(links: list[str] | None = None, title: str = "Test") -> str:
    """Build a minimal HTML page with the given <a> links."""
    link_tags = ""
    if links:
        link_tags = "\n".join(f'<a href="{u}">link</a>' for u in links)
    return f"""<html><head><title>{title}</title></head>
    <body><article><h1>{title}</h1><p>Content.</p>{link_tags}</article></body></html>"""


def _robots_txt(disallow: list[str] | None = None) -> str:
    rules = "\n".join(f"Disallow: {d}" for d in (disallow or []))
    return f"User-agent: *\n{rules}\n"


class FakeResponse:
    """Canned page: body + status (+ optional content_type/headers)."""

    def __init__(self, body: str | bytes, status: int = 200,
                 content_type: str | None = None, headers: dict | None = None):
        self.body = body
        self.status = status
        self.content_type = content_type or (
            "text/html" if isinstance(body, str) else "application/octet-stream")
        self.headers = headers or {}


class FakeEngine:
    """Stand-in for scrape_website.FetchEngine serving canned pages.

    Mirrors the real engine's fetch_page contract: file short-circuit,
    denied detection, run_extract on HTML, optional render escalation via
    a scripted ``render_map`` (url -> hydrated HTML)."""

    def __init__(self, pages: dict[str, FakeResponse], *,
                 respect_robots: bool = True,
                 render_map: dict[str, str] | None = None,
                 render_mode_default: str = "never"):
        self.pages = pages
        self.respect_robots = respect_robots
        self.render_map = render_map or {}
        self.render_mode_default = render_mode_default
        self.render_calls: list[str] = []
        self.politeness_waits = 0
        self.closed = False
        self._robots = None

    async def start(self):
        pass

    async def close(self):
        self.closed = True

    async def load_robots(self, seed_url: str):
        if not self.respect_robots:
            return
        parts = urlsplit(seed_url)
        resp = self.pages.get(f"{parts.scheme}://{parts.netloc}/robots.txt")
        if resp is not None and resp.status == 200 and resp.body:
            from protego import Protego
            self._robots = Protego.parse(resp.body)

    def robots_allows(self, url: str) -> bool:
        if not self.respect_robots or self._robots is None:
            return True
        return self._robots.can_fetch(url, "*")

    async def wait_politeness(self):
        self.politeness_waits += 1

    async def fetch_page(self, url: str, *, run_extract, render_mode=None):
        resp = self.pages.get(url) or FakeResponse("not found", 404)
        kind = "file" if (isinstance(resp.body, bytes)
                          or should_download_file(url, resp.content_type)) else "html"
        content = resp.body if kind == "file" else str(resp.body)
        outcome = FetchOutcome(content, resp.content_type, kind, resp.status,
                               headers=dict(resp.headers))
        if kind == "file":
            return outcome, set(), None
        if is_access_denied(outcome.content, outcome.status):
            outcome.denied = True
            return outcome, set(), None
        links, text = await run_extract(outcome.content, url)
        mode = render_mode if render_mode is not None else self.render_mode_default
        if mode != "never" and url in self.render_map:
            self.render_calls.append(url)
            outcome.content = self.render_map[url]
            outcome.rendered = True
            links, text = await run_extract(outcome.content, url)
        return outcome, links, text


@pytest.fixture
def _patch_engine():
    """Yield a helper that patches scraper.build_engine to a FakeEngine and
    returns the engine so tests can inspect it."""

    def _do(pages: dict[str, FakeResponse], **engine_kw):
        engine = FakeEngine(pages, **engine_kw)

        def _build_engine(*, respect_robots=True, delay_between_requests=None):
            engine.respect_robots = respect_robots
            return engine

        return patch("mcp_server.scraper.build_engine", _build_engine), engine

    return _do


# ---------------------------------------------------------------------------
# Tests — URL helpers
# ---------------------------------------------------------------------------

class TestNormalize:
    def test_drops_fragment(self):
        assert crawler._normalize_crawl_url("https://a.com/page#sec") == "https://a.com/page"

    def test_preserves_query(self):
        assert crawler._normalize_crawl_url("https://a.com/page?q=1") == "https://a.com/page?q=1"

    def test_strips_trailing_slash(self):
        assert crawler._normalize_crawl_url("https://a.com/page/") == "https://a.com/page"

    def test_keeps_root_slash(self):
        assert crawler._normalize_crawl_url("https://a.com/") == "https://a.com/"

    def test_lowercases_scheme_and_host(self):
        assert crawler._normalize_crawl_url("HTTPS://Example.COM/Path") == "https://example.com/Path"


class TestScope:
    def test_same_host_in_scope(self):
        assert crawler._is_in_scope("https://a.com/x", "a.com", False)

    def test_different_host_out_of_scope(self):
        assert not crawler._is_in_scope("https://b.com/x", "a.com", False)

    def test_subdomain_out_of_scope_strict(self):
        assert not crawler._is_in_scope("https://sub.a.com/x", "a.com", False)

    def test_subdomain_in_scope_relaxed(self):
        assert crawler._is_in_scope("https://sub.a.com/x", "a.com", True)

    def test_non_http_rejected(self):
        assert not crawler._is_in_scope("ftp://a.com/x", "a.com", False)


# ---------------------------------------------------------------------------
# Tests — link extraction
# ---------------------------------------------------------------------------

class TestExtractLinks:
    def test_extracts_absolute(self):
        html = _make_html(["https://a.com/page2"])
        links = crawler.extract_links(html, "https://a.com/")
        assert "https://a.com/page2" in links

    def test_resolves_relative(self):
        html = _make_html(["/page2"])
        links = crawler.extract_links(html, "https://a.com/dir/page1")
        assert "https://a.com/page2" in links

    def test_skips_mailto(self):
        html = _make_html(["mailto:x@y.com"])
        links = crawler.extract_links(html, "https://a.com/")
        assert len(links) == 0

    def test_skips_javascript(self):
        html = _make_html(["javascript:void(0)"])
        links = crawler.extract_links(html, "https://a.com/")
        assert len(links) == 0

    def test_strips_fragment_in_extracted(self):
        html = _make_html(["https://a.com/page#top"])
        links = crawler.extract_links(html, "https://a.com/")
        for link in links:
            assert "#" not in link


# ---------------------------------------------------------------------------
# Tests — BFS crawl (engine stubbed)
# ---------------------------------------------------------------------------

class TestCrawl:
    async def test_same_fqdn_rejects_offsite(self, _patch_engine):
        """Links to a different host must be skipped."""
        pages = {
            "https://a.com/": FakeResponse(
                _make_html(["https://a.com/p2", "https://evil.com/steal"])
            ),
            "https://a.com/p2": FakeResponse(_make_html()),
        }
        patcher, _ = _patch_engine(pages)
        with patcher:
            result = await crawler.crawl(
                "https://a.com/", max_pages=10, max_depth=2, delay_ms=0,
                use_sitemap=False,
            )
        fetched_urls = {p["url"] for p in result["pages"]}
        assert "https://evil.com/steal" not in fetched_urls
        assert result["skipped_offsite"] >= 1

    async def test_max_pages_honored(self, _patch_engine):
        """Crawler must stop after max_pages fetches."""
        child_links = [f"https://a.com/p{i}" for i in range(10)]
        pages = {url: FakeResponse(_make_html()) for url in child_links}
        pages["https://a.com/"] = FakeResponse(_make_html(child_links))
        patcher, _ = _patch_engine(pages)
        with patcher:
            result = await crawler.crawl(
                "https://a.com/", max_pages=3, max_depth=2, delay_ms=0,
                use_sitemap=False,
            )
        assert result["fetched"] <= 3

    async def test_max_depth_honored(self, _patch_engine):
        """Pages beyond max_depth should not be fetched."""
        pages = {
            "https://a.com/": FakeResponse(_make_html(["https://a.com/d1"])),
            "https://a.com/d1": FakeResponse(_make_html(["https://a.com/d2"])),
            "https://a.com/d2": FakeResponse(_make_html(["https://a.com/d3"])),
            "https://a.com/d3": FakeResponse(_make_html()),
        }
        patcher, _ = _patch_engine(pages)
        with patcher:
            result = await crawler.crawl(
                "https://a.com/", max_pages=100, max_depth=1, delay_ms=0,
                use_sitemap=False,
            )
        fetched_urls = {p["url"] for p in result["pages"]}
        assert "https://a.com/" in fetched_urls
        assert "https://a.com/d1" in fetched_urls
        assert "https://a.com/d2" not in fetched_urls

    async def test_robots_disallow_respected(self, _patch_engine):
        """URLs disallowed by robots.txt should be skipped."""
        pages = {
            "https://a.com/": FakeResponse(
                _make_html(["https://a.com/secret", "https://a.com/public"])
            ),
            "https://a.com/robots.txt": FakeResponse(_robots_txt(["/secret"])),
            "https://a.com/secret": FakeResponse(_make_html()),
            "https://a.com/public": FakeResponse(_make_html()),
        }
        patcher, _ = _patch_engine(pages)
        with patcher:
            result = await crawler.crawl(
                "https://a.com/", max_pages=10, max_depth=2, delay_ms=0,
                respect_robots=True, use_sitemap=False,
            )
        fetched_urls = {p["url"] for p in result["pages"]}
        assert "https://a.com/secret" not in fetched_urls
        assert result["skipped_robots"] >= 1

    async def test_robots_ignored_when_disabled(self, _patch_engine):
        """When respect_robots=False, disallowed URLs are still fetched."""
        pages = {
            "https://a.com/": FakeResponse(_make_html(["https://a.com/secret"])),
            "https://a.com/robots.txt": FakeResponse(_robots_txt(["/secret"])),
            "https://a.com/secret": FakeResponse(_make_html()),
        }
        patcher, _ = _patch_engine(pages)
        with patcher:
            result = await crawler.crawl(
                "https://a.com/", max_pages=10, max_depth=2, delay_ms=0,
                respect_robots=False, use_sitemap=False,
            )
        fetched_urls = {p["url"] for p in result["pages"]}
        assert "https://a.com/secret" in fetched_urls

    async def test_return_shape(self, _patch_engine):
        """Verify the returned dict has all expected top-level keys."""
        pages = {"https://a.com/": FakeResponse(_make_html())}
        patcher, _ = _patch_engine(pages)
        with patcher:
            result = await crawler.crawl(
                "https://a.com/", max_pages=1, max_depth=0, delay_ms=0,
                use_sitemap=False,
            )
        for key in (
            "seed_url", "host", "discovered", "fetched",
            "skipped_robots", "skipped_offsite", "skipped_max_pages",
            "skipped_excluded", "max_depth_reached", "started_at",
            "finished_at", "pages",
            # additive in 0.2.0:
            "skipped_documents", "rendered_count", "docs_extracted",
        ):
            assert key in result, f"missing key: {key}"

        page = result["pages"][0]
        for key in (
            "url", "depth", "status", "markdown", "http_status",
            "page_title", "content_bytes", "fetch_duration_ms",
            "etag", "last_modified", "error",
            # additive in 0.2.0:
            "rendered", "via", "content_kind",
        ):
            assert key in page, f"missing page key: {key}"

    async def test_include_subdomains(self, _patch_engine):
        """With include_subdomains=True, sub.a.com links should be followed."""
        pages = {
            "https://a.com/": FakeResponse(_make_html(["https://sub.a.com/page"])),
            "https://sub.a.com/page": FakeResponse(_make_html()),
        }
        patcher, _ = _patch_engine(pages)
        with patcher:
            result = await crawler.crawl(
                "https://a.com/", max_pages=10, max_depth=2, delay_ms=0,
                include_subdomains=True, use_sitemap=False,
            )
        fetched_urls = {p["url"] for p in result["pages"]}
        assert "https://sub.a.com/page" in fetched_urls

    async def test_failed_page_recorded(self, _patch_engine):
        """A 500 response should appear in pages with status='failed'."""
        pages = {
            "https://a.com/": FakeResponse(_make_html(["https://a.com/broken"])),
            "https://a.com/broken": FakeResponse("error", 500),
        }
        patcher, _ = _patch_engine(pages)
        with patcher:
            result = await crawler.crawl(
                "https://a.com/", max_pages=10, max_depth=2, delay_ms=0,
                use_sitemap=False,
            )
        broken = [p for p in result["pages"] if p["url"] == "https://a.com/broken"]
        assert len(broken) == 1
        assert broken[0]["status"] == "failed"
        assert broken[0]["markdown"] == ""

    # ------------------------------------------------------------------
    # exclude_patterns
    # ------------------------------------------------------------------

    async def test_exclude_patterns_drops_static_but_fetches_documents(self, _patch_engine):
        """Images/static assets are excluded by default; documents (.pdf) are
        NOT excluded anymore (0.2.0) — they get fetched for doc extraction."""
        pages = {
            "https://a.com/": FakeResponse(
                _make_html([
                    "https://a.com/page",
                    "https://a.com/logo.png",
                    "https://a.com/report.pdf",
                ])
            ),
            "https://a.com/page": FakeResponse(_make_html()),
            "https://a.com/logo.png": FakeResponse("binary", 200, content_type="image/png"),
            "https://a.com/report.pdf": FakeResponse(b"%PDF-1.4 not really", 200,
                                                     content_type="application/pdf"),
        }
        patcher, _ = _patch_engine(pages)
        with patcher:
            result = await crawler.crawl(
                "https://a.com/", max_pages=20, max_depth=2, delay_ms=0,
                use_sitemap=False,
            )
        fetched_urls = {p["url"] for p in result["pages"]}
        assert "https://a.com/page" in fetched_urls
        assert "https://a.com/logo.png" not in fetched_urls
        assert "https://a.com/report.pdf" in fetched_urls
        pdf_page = next(p for p in result["pages"] if p["url"].endswith(".pdf"))
        assert pdf_page["content_kind"] == "pdf"
        assert result["skipped_excluded"] >= 1

    async def test_extract_docs_false_skips_document_urls(self, _patch_engine):
        """extract_docs=False must not even fetch document URLs."""
        pages = {
            "https://a.com/": FakeResponse(
                _make_html(["https://a.com/page", "https://a.com/report.pdf"])
            ),
            "https://a.com/page": FakeResponse(_make_html()),
            "https://a.com/report.pdf": FakeResponse(b"%PDF", 200,
                                                     content_type="application/pdf"),
        }
        patcher, _ = _patch_engine(pages)
        with patcher:
            result = await crawler.crawl(
                "https://a.com/", max_pages=20, max_depth=2, delay_ms=0,
                use_sitemap=False, extract_docs=False,
            )
        fetched_urls = {p["url"] for p in result["pages"]}
        assert "https://a.com/report.pdf" not in fetched_urls
        assert result["skipped_documents"] >= 1

    async def test_custom_exclude_patterns(self, _patch_engine):
        """A caller-provided exclude_patterns overrides the defaults."""
        pages = {
            "https://a.com/": FakeResponse(
                _make_html(["https://a.com/keep", "https://a.com/nope-skip-me"])
            ),
            "https://a.com/keep": FakeResponse(_make_html()),
            "https://a.com/nope-skip-me": FakeResponse(_make_html()),
        }
        patcher, _ = _patch_engine(pages)
        with patcher:
            result = await crawler.crawl(
                "https://a.com/", max_pages=20, max_depth=2, delay_ms=0,
                exclude_patterns=[r"nope-skip"],
                use_sitemap=False,
            )
        fetched_urls = {p["url"] for p in result["pages"]}
        assert "https://a.com/keep" in fetched_urls
        assert "https://a.com/nope-skip-me" not in fetched_urls
        assert result["skipped_excluded"] >= 1

    # ------------------------------------------------------------------
    # strip_tracking_params
    # ------------------------------------------------------------------

    async def test_strip_tracking_params_dedupes_utm_variants(self, _patch_engine):
        """Two links that differ only by utm_source should be treated as one URL."""
        pages = {
            "https://a.com/": FakeResponse(
                _make_html([
                    "https://a.com/page?utm_source=twitter",
                    "https://a.com/page?utm_source=facebook",
                    "https://a.com/page",
                ])
            ),
            "https://a.com/page": FakeResponse(_make_html()),
        }
        patcher, _ = _patch_engine(pages)
        with patcher:
            result = await crawler.crawl(
                "https://a.com/", max_pages=20, max_depth=2, delay_ms=0,
                strip_tracking_params=True,
                use_sitemap=False,
            )
        page_hits = [p for p in result["pages"] if p["url"] == "https://a.com/page"]
        assert len(page_hits) == 1

    async def test_strip_tracking_params_disabled_keeps_variants(self, _patch_engine):
        """With strip_tracking_params=False, UTM variants are separate URLs."""
        pages = {
            "https://a.com/": FakeResponse(
                _make_html([
                    "https://a.com/page?utm_source=twitter",
                    "https://a.com/page?utm_source=facebook",
                ])
            ),
            "https://a.com/page?utm_source=twitter": FakeResponse(_make_html()),
            "https://a.com/page?utm_source=facebook": FakeResponse(_make_html()),
        }
        patcher, _ = _patch_engine(pages)
        with patcher:
            result = await crawler.crawl(
                "https://a.com/", max_pages=20, max_depth=2, delay_ms=0,
                strip_tracking_params=False,
                use_sitemap=False,
            )
        fetched_urls = {p["url"] for p in result["pages"]}
        assert len(fetched_urls) >= 3  # seed + 2 variants

    # ------------------------------------------------------------------
    # use_sitemap
    # ------------------------------------------------------------------

    async def test_use_sitemap_seeds_queue(self, _patch_engine):
        """When use_sitemap=True, URLs from sitemap.xml are enqueued at depth 0."""
        sitemap_urls = [
            "https://a.com/from-sitemap-1",
            "https://a.com/from-sitemap-2",
        ]
        pages = {
            "https://a.com/": FakeResponse(_make_html()),
            "https://a.com/from-sitemap-1": FakeResponse(_make_html()),
            "https://a.com/from-sitemap-2": FakeResponse(_make_html()),
        }
        patcher, _ = _patch_engine(pages)
        with patcher, \
             patch("mcp_server.crawler._fetch_sitemap_urls", return_value=sitemap_urls):
            result = await crawler.crawl(
                "https://a.com/", max_pages=20, max_depth=2, delay_ms=0,
                use_sitemap=True,
            )
        fetched_urls = {p["url"] for p in result["pages"]}
        assert "https://a.com/from-sitemap-1" in fetched_urls
        assert "https://a.com/from-sitemap-2" in fetched_urls
        for p in result["pages"]:
            if p["url"].startswith("https://a.com/from-sitemap"):
                assert p["depth"] == 0

    async def test_use_sitemap_false_skips_sitemap(self, _patch_engine):
        """When use_sitemap=False, _fetch_sitemap_urls should not be called."""
        pages = {"https://a.com/": FakeResponse(_make_html())}
        patcher, _ = _patch_engine(pages)
        with patcher, \
             patch("mcp_server.crawler._fetch_sitemap_urls") as mock_sm:
            await crawler.crawl(
                "https://a.com/", max_pages=5, max_depth=1, delay_ms=0,
                use_sitemap=False,
            )
        mock_sm.assert_not_called()

    async def test_sitemap_urls_filtered_by_exclude(self, _patch_engine):
        """Sitemap URLs that match exclude_patterns should be skipped."""
        sitemap_urls = [
            "https://a.com/good-page",
            "https://a.com/image.png",
        ]
        pages = {
            "https://a.com/": FakeResponse(_make_html()),
            "https://a.com/good-page": FakeResponse(_make_html()),
        }
        patcher, _ = _patch_engine(pages)
        with patcher, \
             patch("mcp_server.crawler._fetch_sitemap_urls", return_value=sitemap_urls):
            result = await crawler.crawl(
                "https://a.com/", max_pages=20, max_depth=2, delay_ms=0,
                use_sitemap=True,
            )
        fetched_urls = {p["url"] for p in result["pages"]}
        assert "https://a.com/good-page" in fetched_urls
        assert "https://a.com/image.png" not in fetched_urls
        assert result["skipped_excluded"] >= 1

    # ------------------------------------------------------------------
    # 0.2.0: render escalation + progress
    # ------------------------------------------------------------------

    async def test_spa_shell_rendered_once_and_reextracted(self, _patch_engine):
        """A page in the render_map gets exactly one render; links/markdown
        come from the hydrated DOM."""
        shell = '<html><body><div id="root"></div></body></html>'
        hydrated = _make_html(["https://a.com/hidden-behind-js"], title="Hydrated")
        pages = {
            "https://a.com/": FakeResponse(shell),
            "https://a.com/hidden-behind-js": FakeResponse(_make_html()),
        }
        patcher, engine = _patch_engine(pages, render_map={"https://a.com/": hydrated},
                                        render_mode_default="auto")
        with patcher:
            result = await crawler.crawl(
                "https://a.com/", max_pages=10, max_depth=2, delay_ms=0,
                use_sitemap=False, render_mode="auto",
            )
        assert engine.render_calls == ["https://a.com/"]
        fetched_urls = {p["url"] for p in result["pages"]}
        assert "https://a.com/hidden-behind-js" in fetched_urls
        seed_page = next(p for p in result["pages"] if p["url"] == "https://a.com/")
        assert seed_page["rendered"] is True
        assert result["rendered_count"] == 1

    async def test_render_mode_never_disables_rendering(self, _patch_engine):
        shell = '<html><body><div id="root"></div></body></html>'
        pages = {"https://a.com/": FakeResponse(shell)}
        patcher, engine = _patch_engine(pages, render_map={"https://a.com/": _make_html()})
        with patcher:
            result = await crawler.crawl(
                "https://a.com/", max_pages=5, max_depth=1, delay_ms=0,
                use_sitemap=False, render_mode="never",
            )
        assert engine.render_calls == []
        assert result["rendered_count"] == 0

    async def test_progress_cb_called_per_page(self, _patch_engine):
        pages = {
            "https://a.com/": FakeResponse(_make_html(["https://a.com/p2"])),
            "https://a.com/p2": FakeResponse(_make_html()),
        }
        calls: list[tuple[int, int]] = []

        async def progress(fetched: int, queued: int):
            calls.append((fetched, queued))

        patcher, _ = _patch_engine(pages)
        with patcher:
            result = await crawler.crawl(
                "https://a.com/", max_pages=10, max_depth=2, delay_ms=0,
                use_sitemap=False, progress_cb=progress,
            )
        assert len(calls) == result["fetched"]
        assert calls[-1][0] == result["fetched"]

    async def test_engine_closed_after_crawl(self, _patch_engine):
        pages = {"https://a.com/": FakeResponse(_make_html())}
        patcher, engine = _patch_engine(pages)
        with patcher:
            await crawler.crawl(
                "https://a.com/", max_pages=1, max_depth=0, delay_ms=0,
                use_sitemap=False,
            )
        assert engine.closed is True


# ---------------------------------------------------------------------------
# Platform contract test — the Harvard assistants platform's scraper_proxy
# reads these exact keys from crawl_site / fetch results. This is the
# back-compat tripwire: if it fails, the platform breaks.
# ---------------------------------------------------------------------------

class TestPlatformContract:
    async def test_crawl_site_contract_with_defaults(self, _patch_engine):
        """Simulate the platform's call: only the 9 original kwargs, none of
        the 0.2.0 additions. Every key the platform reads must be present."""
        pages = {
            "https://a.com/": FakeResponse(_make_html(["https://a.com/p2"])),
            "https://a.com/p2": FakeResponse(_make_html()),
        }
        patcher, _ = _patch_engine(pages)
        with patcher:
            result = await crawler.crawl(
                "https://a.com/",
                max_pages=200, max_depth=3, delay_ms=0,
                respect_robots=True, include_subdomains=False,
                exclude_patterns=None, strip_tracking_params=True,
                use_sitemap=False,
            )
        # Top-level keys the platform consumes (core/scraper_proxy.py).
        assert isinstance(result["pages"], list)
        assert isinstance(result["discovered"], int)
        assert isinstance(result["fetched"], int)
        # Per-page keys the platform consumes for upload + registration rows.
        for p in result["pages"]:
            assert isinstance(p["url"], str)
            assert isinstance(p["markdown"], str)
            assert p["status"] in ("ok", "empty", "failed", "skipped")
            assert "http_status" in p
            assert "page_title" in p
            assert "content_bytes" in p
            assert "fetch_duration_ms" in p
            assert "etag" in p
            assert "last_modified" in p
            assert "error" in p
        ok_pages = [p for p in result["pages"] if p["status"] == "ok"]
        assert ok_pages and all(p["markdown"].strip() for p in ok_pages)
