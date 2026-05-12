"""Tests for the BFS crawler.

All HTTP calls are monkeypatched — no real network traffic.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.robotparser import RobotFileParser

import pytest

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
    """Minimal stand-in for aiohttp.ClientResponse."""

    def __init__(self, body: str, status: int = 200, headers: dict | None = None):
        self._body = body.encode("utf-8")
        self.status = status
        self.headers = headers or {}
        self.charset = None

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


class FakeSession:
    """Fake aiohttp.ClientSession that serves canned pages keyed by URL."""

    def __init__(self, pages: dict[str, FakeResponse]):
        self._pages = pages

    def get(self, url: str, **_kw) -> FakeResponse:
        # Return matching page or a 404
        return self._pages.get(url, FakeResponse("not found", 404))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


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
# Tests — BFS crawl (integration with mocked HTTP)
# ---------------------------------------------------------------------------

@pytest.fixture
def _patch_session():
    """Yield a helper that patches aiohttp.ClientSession to use FakeSession."""

    def _do(pages: dict[str, FakeResponse]):
        fake = FakeSession(pages)
        return patch("aiohttp.ClientSession", return_value=fake)

    return _do


class TestCrawl:
    async def test_same_fqdn_rejects_offsite(self, _patch_session):
        """Links to a different host must be skipped."""
        pages = {
            "https://a.com/": FakeResponse(
                _make_html(["https://a.com/p2", "https://evil.com/steal"])
            ),
            "https://a.com/robots.txt": FakeResponse("", 404),
            "https://a.com/p2": FakeResponse(_make_html()),
        }
        with _patch_session(pages):
            result = await crawler.crawl(
                "https://a.com/", max_pages=10, max_depth=2, delay_ms=0,
                use_sitemap=False,
            )
        fetched_urls = {p["url"] for p in result["pages"]}
        assert "https://evil.com/steal" not in fetched_urls
        assert result["skipped_offsite"] >= 1

    async def test_max_pages_honored(self, _patch_session):
        """Crawler must stop after max_pages fetches."""
        # Seed links to 10 pages; cap at 3.
        child_links = [f"https://a.com/p{i}" for i in range(10)]
        child_pages = {
            url: FakeResponse(_make_html()) for url in child_links
        }
        child_pages["https://a.com/"] = FakeResponse(_make_html(child_links))
        child_pages["https://a.com/robots.txt"] = FakeResponse("", 404)
        with _patch_session(child_pages):
            result = await crawler.crawl(
                "https://a.com/", max_pages=3, max_depth=2, delay_ms=0,
                use_sitemap=False,
            )
        assert result["fetched"] <= 3

    async def test_max_depth_honored(self, _patch_session):
        """Pages beyond max_depth should not be fetched."""
        # Chain: / -> /d1 -> /d2 -> /d3
        pages = {
            "https://a.com/": FakeResponse(_make_html(["https://a.com/d1"])),
            "https://a.com/d1": FakeResponse(_make_html(["https://a.com/d2"])),
            "https://a.com/d2": FakeResponse(_make_html(["https://a.com/d3"])),
            "https://a.com/d3": FakeResponse(_make_html()),
            "https://a.com/robots.txt": FakeResponse("", 404),
        }
        with _patch_session(pages):
            result = await crawler.crawl(
                "https://a.com/", max_pages=100, max_depth=1, delay_ms=0,
                use_sitemap=False,
            )
        fetched_urls = {p["url"] for p in result["pages"]}
        # depth 0 = /, depth 1 = /d1 — /d2 is depth 2, should NOT be fetched
        assert "https://a.com/" in fetched_urls
        assert "https://a.com/d1" in fetched_urls
        assert "https://a.com/d2" not in fetched_urls

    async def test_robots_disallow_respected(self, _patch_session):
        """URLs disallowed by robots.txt should be skipped."""
        robots = _robots_txt(["/secret"])
        pages = {
            "https://a.com/": FakeResponse(
                _make_html(["https://a.com/secret", "https://a.com/public"])
            ),
            "https://a.com/robots.txt": FakeResponse(robots),
            "https://a.com/secret": FakeResponse(_make_html()),
            "https://a.com/public": FakeResponse(_make_html()),
        }
        with _patch_session(pages):
            result = await crawler.crawl(
                "https://a.com/", max_pages=10, max_depth=2, delay_ms=0,
                respect_robots=True, use_sitemap=False,
            )
        fetched_urls = {p["url"] for p in result["pages"]}
        assert "https://a.com/secret" not in fetched_urls
        assert result["skipped_robots"] >= 1

    async def test_robots_ignored_when_disabled(self, _patch_session):
        """When respect_robots=False, disallowed URLs are still fetched."""
        robots = _robots_txt(["/secret"])
        pages = {
            "https://a.com/": FakeResponse(
                _make_html(["https://a.com/secret"])
            ),
            "https://a.com/robots.txt": FakeResponse(robots),
            "https://a.com/secret": FakeResponse(_make_html()),
        }
        with _patch_session(pages):
            result = await crawler.crawl(
                "https://a.com/", max_pages=10, max_depth=2, delay_ms=0,
                respect_robots=False, use_sitemap=False,
            )
        fetched_urls = {p["url"] for p in result["pages"]}
        assert "https://a.com/secret" in fetched_urls

    async def test_return_shape(self, _patch_session):
        """Verify the returned dict has all expected top-level keys."""
        pages = {
            "https://a.com/": FakeResponse(_make_html()),
            "https://a.com/robots.txt": FakeResponse("", 404),
        }
        with _patch_session(pages):
            result = await crawler.crawl(
                "https://a.com/", max_pages=1, max_depth=0, delay_ms=0,
                use_sitemap=False,
            )
        for key in (
            "seed_url", "host", "discovered", "fetched",
            "skipped_robots", "skipped_offsite", "skipped_max_pages",
            "skipped_excluded", "max_depth_reached", "started_at",
            "finished_at", "pages",
        ):
            assert key in result, f"missing key: {key}"

        page = result["pages"][0]
        for key in (
            "url", "depth", "status", "markdown", "http_status",
            "page_title", "content_bytes", "fetch_duration_ms",
            "etag", "last_modified", "error",
        ):
            assert key in page, f"missing page key: {key}"

    async def test_include_subdomains(self, _patch_session):
        """With include_subdomains=True, sub.a.com links should be followed."""
        pages = {
            "https://a.com/": FakeResponse(
                _make_html(["https://sub.a.com/page"])
            ),
            "https://a.com/robots.txt": FakeResponse("", 404),
            "https://sub.a.com/page": FakeResponse(_make_html()),
            "https://sub.a.com/robots.txt": FakeResponse("", 404),
        }
        with _patch_session(pages):
            result = await crawler.crawl(
                "https://a.com/", max_pages=10, max_depth=2, delay_ms=0,
                include_subdomains=True, use_sitemap=False,
            )
        fetched_urls = {p["url"] for p in result["pages"]}
        assert "https://sub.a.com/page" in fetched_urls

    async def test_failed_page_recorded(self, _patch_session):
        """A 500 response should appear in pages with status='failed'."""
        pages = {
            "https://a.com/": FakeResponse(
                _make_html(["https://a.com/broken"])
            ),
            "https://a.com/robots.txt": FakeResponse("", 404),
            "https://a.com/broken": FakeResponse("error", 500),
        }
        with _patch_session(pages):
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

    async def test_exclude_patterns_drops_matching_urls(self, _patch_session):
        """URLs matching exclude patterns should be skipped, not fetched."""
        pages = {
            "https://a.com/": FakeResponse(
                _make_html([
                    "https://a.com/page",
                    "https://a.com/logo.png",
                    "https://a.com/report.pdf",
                ])
            ),
            "https://a.com/robots.txt": FakeResponse("", 404),
            "https://a.com/page": FakeResponse(_make_html()),
            "https://a.com/logo.png": FakeResponse("binary", 200),
            "https://a.com/report.pdf": FakeResponse("binary", 200),
        }
        with _patch_session(pages):
            result = await crawler.crawl(
                "https://a.com/", max_pages=20, max_depth=2, delay_ms=0,
                use_sitemap=False,
            )
        fetched_urls = {p["url"] for p in result["pages"]}
        assert "https://a.com/page" in fetched_urls
        assert "https://a.com/logo.png" not in fetched_urls
        assert "https://a.com/report.pdf" not in fetched_urls
        assert result["skipped_excluded"] >= 2

    async def test_custom_exclude_patterns(self, _patch_session):
        """A caller-provided exclude_patterns overrides the defaults."""
        pages = {
            "https://a.com/": FakeResponse(
                _make_html([
                    "https://a.com/keep",
                    "https://a.com/nope-skip-me",
                ])
            ),
            "https://a.com/robots.txt": FakeResponse("", 404),
            "https://a.com/keep": FakeResponse(_make_html()),
            "https://a.com/nope-skip-me": FakeResponse(_make_html()),
        }
        with _patch_session(pages):
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

    async def test_strip_tracking_params_dedupes_utm_variants(self, _patch_session):
        """Two links that differ only by utm_source should be treated as one URL."""
        pages = {
            "https://a.com/": FakeResponse(
                _make_html([
                    "https://a.com/page?utm_source=twitter",
                    "https://a.com/page?utm_source=facebook",
                    "https://a.com/page",
                ])
            ),
            "https://a.com/robots.txt": FakeResponse("", 404),
            "https://a.com/page": FakeResponse(_make_html()),
        }
        with _patch_session(pages):
            result = await crawler.crawl(
                "https://a.com/", max_pages=20, max_depth=2, delay_ms=0,
                strip_tracking_params=True,
                use_sitemap=False,
            )
        # /page should appear exactly once despite 3 link variants.
        page_hits = [p for p in result["pages"] if p["url"] == "https://a.com/page"]
        assert len(page_hits) == 1

    async def test_strip_tracking_params_disabled_keeps_variants(self, _patch_session):
        """With strip_tracking_params=False, UTM variants are separate URLs."""
        pages = {
            "https://a.com/": FakeResponse(
                _make_html([
                    "https://a.com/page?utm_source=twitter",
                    "https://a.com/page?utm_source=facebook",
                ])
            ),
            "https://a.com/robots.txt": FakeResponse("", 404),
            "https://a.com/page?utm_source=twitter": FakeResponse(_make_html()),
            "https://a.com/page?utm_source=facebook": FakeResponse(_make_html()),
        }
        with _patch_session(pages):
            result = await crawler.crawl(
                "https://a.com/", max_pages=20, max_depth=2, delay_ms=0,
                strip_tracking_params=False,
                use_sitemap=False,
            )
        # Both variants should be fetched as separate pages.
        fetched_urls = {p["url"] for p in result["pages"]}
        assert len(fetched_urls) >= 3  # seed + 2 variants

    # ------------------------------------------------------------------
    # use_sitemap
    # ------------------------------------------------------------------

    async def test_use_sitemap_seeds_queue(self, _patch_session):
        """When use_sitemap=True, URLs from sitemap.xml are enqueued at depth 0."""
        sitemap_urls = [
            "https://a.com/from-sitemap-1",
            "https://a.com/from-sitemap-2",
        ]
        pages = {
            "https://a.com/": FakeResponse(_make_html()),
            "https://a.com/robots.txt": FakeResponse("", 404),
            "https://a.com/from-sitemap-1": FakeResponse(_make_html()),
            "https://a.com/from-sitemap-2": FakeResponse(_make_html()),
        }
        with _patch_session(pages), \
             patch("mcp_server.crawler._fetch_sitemap_urls", return_value=sitemap_urls):
            result = await crawler.crawl(
                "https://a.com/", max_pages=20, max_depth=2, delay_ms=0,
                use_sitemap=True,
            )
        fetched_urls = {p["url"] for p in result["pages"]}
        assert "https://a.com/from-sitemap-1" in fetched_urls
        assert "https://a.com/from-sitemap-2" in fetched_urls
        # All sitemap pages should be at depth 0.
        for p in result["pages"]:
            if p["url"].startswith("https://a.com/from-sitemap"):
                assert p["depth"] == 0

    async def test_use_sitemap_false_skips_sitemap(self, _patch_session):
        """When use_sitemap=False, _fetch_sitemap_urls should not be called."""
        pages = {
            "https://a.com/": FakeResponse(_make_html()),
            "https://a.com/robots.txt": FakeResponse("", 404),
        }
        with _patch_session(pages), \
             patch("mcp_server.crawler._fetch_sitemap_urls") as mock_sm:
            result = await crawler.crawl(
                "https://a.com/", max_pages=5, max_depth=1, delay_ms=0,
                use_sitemap=False,
            )
        mock_sm.assert_not_called()

    async def test_sitemap_urls_filtered_by_exclude(self, _patch_session):
        """Sitemap URLs that match exclude_patterns should be skipped."""
        sitemap_urls = [
            "https://a.com/good-page",
            "https://a.com/image.png",
        ]
        pages = {
            "https://a.com/": FakeResponse(_make_html()),
            "https://a.com/robots.txt": FakeResponse("", 404),
            "https://a.com/good-page": FakeResponse(_make_html()),
        }
        with _patch_session(pages), \
             patch("mcp_server.crawler._fetch_sitemap_urls", return_value=sitemap_urls):
            result = await crawler.crawl(
                "https://a.com/", max_pages=20, max_depth=2, delay_ms=0,
                use_sitemap=True,
            )
        fetched_urls = {p["url"] for p in result["pages"]}
        assert "https://a.com/good-page" in fetched_urls
        assert "https://a.com/image.png" not in fetched_urls
        assert result["skipped_excluded"] >= 1
