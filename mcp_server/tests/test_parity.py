"""Tests for the 0.3.0 parity work: SSRF guard, P0 fixes, new knobs, the
batch tool, additive result keys, and server_health capabilities.

Unit tests stub the engine (FakeEngine from test_crawler) or the guard's
resolver; the tests marked `integration` bind a local fixture server and
drive the REAL GuardedFetchEngine with an injected is-blocked predicate, so
the guard is exercised on actual redirects without leaving the machine.
"""

from __future__ import annotations

import http.server
import io
import ipaddress
import json
import threading
import zipfile
from unittest.mock import patch

import pytest

from scrape_website.extract import _extract_document_to_markdown
from scrape_website.fetch import FetchOutcome

from mcp_server import crawler, netguard, scraper, settings
from mcp_server.netguard import BlockedTargetError, GuardedFetchEngine
from mcp_server.tests.test_crawler import FakeResponse, _make_html

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Keys fetch_url_as_markdown returned in 0.2.0 — must all survive.
FETCH_KEYS_020 = ("url", "markdown", "length", "http_status", "page_title",
                  "status", "error", "rendered", "via", "content_kind")
ADDITIVE_PAGE_KEYS = ("classification", "classification_detail",
                      "content_type", "metadata", "duplicate_of")


@pytest.fixture(autouse=True)
def _guard_on(monkeypatch):
    """Every test starts with the default (guarded) server config."""
    for var in ("SCRAPER_ALLOW_PRIVATE_TARGETS", "SCRAPER_ALLOW_INSECURE_TLS",
                "SCRAPER_MAX_RESPONSE_BYTES", "SCRAPER_MIN_DELAY_MS",
                "SCRAPE_CF_COOKIES"):
        monkeypatch.delenv(var, raising=False)
    scraper.reset_engine()
    yield
    scraper.reset_engine()


def _payload(result) -> dict:
    if getattr(result, "structured_content", None):
        return result.structured_content
    return json.loads(result.content[0].text)


def _minimal_docx(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '</Types>')
        z.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '</Relationships>')
        z.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f'<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>')
    return buf.getvalue()


# ---------------------------------------------------------------------------
# SSRF: address classification + URL checks (no network)
# ---------------------------------------------------------------------------

class TestBlockedIp:
    @pytest.mark.parametrize("ip", [
        "127.0.0.1", "10.0.0.1", "172.16.5.4", "192.168.1.1",
        "169.254.169.254",          # cloud metadata
        "169.254.170.2",            # ECS task credentials
        "100.64.0.1",               # CGNAT
        "0.0.0.0", "224.0.0.1", "255.255.255.255", "198.18.0.1",
        "::1", "fe80::1", "fc00::1", "::",
        "::ffff:127.0.0.1", "::ffff:169.254.169.254",   # IPv4-mapped
        "64:ff9b::a9fe:a9fe",                           # NAT64 169.254.169.254
        "2002:7f00:1::1",                               # 6to4 127.0.0.1
    ])
    def test_blocked(self, ip):
        assert netguard.is_blocked_ip(ip)

    @pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34",
                                    "2606:4700:4700::1111"])
    def test_public_allowed(self, ip):
        assert not netguard.is_blocked_ip(ip)


class TestCheckUrl:
    @pytest.mark.parametrize("url", [
        "http://169.254.169.254/latest/meta-data/",
        "http://169.254.170.2/v2/credentials",
        "http://[::1]:8000/",
        "http://127.0.0.1/",
        "file:///etc/passwd",
        "gopher://example.com/",
        "http:///nohost",
    ])
    async def test_rejects(self, url):
        with pytest.raises(BlockedTargetError):
            await netguard.check_url(url)

    async def test_hostname_resolving_private_rejected(self, monkeypatch):
        async def fake_resolve(host, port):
            return ["93.184.216.34", "10.1.2.3"]  # ANY private answer blocks
        monkeypatch.setattr(netguard, "resolve", fake_resolve)
        with pytest.raises(BlockedTargetError, match="10.1.2.3"):
            await netguard.check_url("https://internal.example.com/")

    async def test_hostname_resolving_public_allowed(self, monkeypatch):
        async def fake_resolve(host, port):
            return ["93.184.216.34"]
        monkeypatch.setattr(netguard, "resolve", fake_resolve)
        assert await netguard.check_url("https://example.com/") == ["93.184.216.34"]


class TestGuardedResolver:
    class _Inner:
        def __init__(self, ip):
            self.ip = ip

        async def resolve(self, host, port=0, family=0):
            return [{"hostname": host, "host": self.ip, "port": port,
                     "family": family, "proto": 0, "flags": 0}]

        async def close(self):
            pass

    async def test_private_answer_rejected(self):
        # The DNS-rebinding defense: the answer aiohttp would connect to.
        r = netguard.GuardedResolver(self._Inner("169.254.169.254"))
        with pytest.raises(OSError, match="blocked target"):
            await r.resolve("rebind.example.com", 80)

    async def test_public_answer_passes(self):
        r = netguard.GuardedResolver(self._Inner("93.184.216.34"))
        assert (await r.resolve("example.com", 80))[0]["host"] == "93.184.216.34"


class TestSitemapGuard:
    def test_installed_on_upstream_module(self):
        import scrape_website.sitemap as sm
        assert sm.urlopen is netguard._guarded_urlopen

    def test_blocks_metadata_sitemap(self):
        with pytest.raises(BlockedTargetError):
            netguard._guarded_urlopen("http://169.254.169.254/sitemap.xml", timeout=1)


class TestToolLevelSsrf:
    async def test_fetch_metadata_endpoint_refused(self):
        from fastmcp import Client
        from mcp_server.server import mcp
        async with Client(mcp) as client:
            res = await client.call_tool("fetch_url_as_markdown", {
                "url": "http://169.254.169.254/latest/meta-data/iam/"})
        payload = _payload(res)
        assert payload["status"] == "failed"
        assert payload["error"].startswith("blocked:")
        assert payload["markdown"] == ""

    async def test_fetch_localhost_name_refused(self):
        fr = await scraper.fetch_and_extract("http://localhost:9/")
        assert fr.status == "failed"
        assert "blocked" in fr.error

    async def test_crawl_seed_refused_without_sitemap_or_robots_leak(self):
        result = await crawler.crawl(
            "http://169.254.169.254/", max_pages=5, max_depth=1, delay_ms=0)
        assert result["fetched"] == 1
        page = result["pages"][0]
        assert page["status"] == "failed" and page["error"].startswith("blocked:")
        assert result["failed_urls"] == ["http://169.254.169.254/"]

    async def test_allow_private_env_disables_guard(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_ALLOW_PRIVATE_TARGETS", "1")
        eng = scraper.build_engine()
        assert eng.block_private is False
        await eng.guard_url("http://127.0.0.1/")  # no raise


# ---------------------------------------------------------------------------
# P0 fixes
# ---------------------------------------------------------------------------

class _StubEngine:
    """Minimal engine returning one canned outcome from fetch_page."""

    def __init__(self, outcome):
        self.outcome = outcome

    async def fetch_page(self, url, *, run_extract, render_mode=None):
        if self.outcome.kind == "file":
            return self.outcome, set(), None
        links, text = await run_extract(self.outcome.content, url)
        return self.outcome, links, text


class TestP0:
    def test_build_engine_passes_server_file_cap(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_MAX_FILE_SIZE", "12345")
        assert scraper.build_engine().max_file_size == 12345

    def test_build_engine_env_limits(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_MAX_RETRIES", "5")
        monkeypatch.setenv("SCRAPER_RENDER_SETTLE_MS", "1500")
        monkeypatch.setenv("SCRAPER_MAX_PAGE_SIZE", "999")
        eng = scraper.build_engine()
        assert (eng.max_retries, eng.render_settle_ms, eng.max_page_size) == (5, 1500, 999)

    async def test_engine_skipped_oversize_doc_reported_failed(self):
        # aiohttp tier skips before buffering: content b'' + detail.
        outcome = FetchOutcome(b"", "application/pdf", "file", 200,
                               detail="file too large",
                               headers={"Content-Length": "999999999"})
        fr, _ = await scraper.fetch_page_result(
            "https://a.com/huge.pdf", run_extract=None,
            engine=_StubEngine(outcome))
        assert fr.status == "failed"
        assert fr.error.startswith("file too large")
        assert fr.content_bytes == 999999999

    def test_file_kind_from_content_type(self):
        assert scraper._file_kind("https://a.com/resource/guidance", DOCX_MIME) == "docx"
        assert scraper._file_kind("https://a.com/x/report.pdf", DOCX_MIME) == "pdf"
        assert scraper._file_kind("https://a.com/download.php", "application/pdf") == "pdf"
        assert scraper._file_kind("https://a.com/blob", None) == "bin"

    def test_document_filename(self):
        assert scraper._document_filename(
            "https://a.com/resource/guidance", DOCX_MIME, "docx") == "guidance.docx"
        # PDFs keep their name (magic-sniffed upstream; stable content hash).
        assert scraper._document_filename(
            "https://a.com/resource/guidance", "application/pdf", "pdf") == "guidance"
        assert scraper._document_filename(
            "https://a.com/files/report.xlsx", DOCX_MIME, "xlsx") == "report.xlsx"
        assert scraper._document_filename("https://a.com/", DOCX_MIME, "docx") == "document.docx"
        assert scraper._document_filename("https://a.com/a/..", None, "bin") == "document.bin"

    async def test_extensionless_docx_extracts(self, tmp_path):
        content = _minimal_docx("Extensionless guidance text")
        # Regression proof by a different route: upstream on a bare
        # extension-less file returns nothing.
        bare = tmp_path / "guidance"
        bare.write_bytes(content)
        assert _extract_document_to_markdown(
            str(bare), "https://a.com/resource/guidance", "a.com") is None
        outcome = FetchOutcome(content, DOCX_MIME, "file", 200)
        fr, _ = await scraper.fetch_page_result(
            "https://a.com/resource/guidance", run_extract=None,
            engine=_StubEngine(outcome))
        assert fr.status == "ok", fr
        assert fr.content_kind == "docx"
        assert "Extensionless guidance text" in fr.markdown
        assert fr.metadata["filetype"] == "docx"

    def test_extraction_holds_lock(self):
        seen = []

        def fake_extract(html, url):
            seen.append(scraper._EXTRACT_LOCK.locked())
            return "x"

        with patch("mcp_server.scraper._extract_text_trafilatura", fake_extract):
            scraper.html_to_markdown("<p>x</p>", "https://a.com/")
            scraper._parse_and_extract_locked("<p>x</p>", "https://a.com/", "a.com")
        assert seen == [True, True]

    async def test_fetch_tool_returns_platform_metadata(self):
        """P0-4: content_bytes/fetch_duration_ms/etag/last_modified."""
        from mcp_server import server as srv
        fr = scraper.FetchResult(url="https://a.com/", markdown="hi",
                                 content_bytes=10, fetch_duration_ms=5,
                                 etag='"abc"', last_modified="Mon, 01 Jan 2026")
        with patch("mcp_server.scraper.fetch_one", return_value=fr):
            payload = await srv.fetch_url_as_markdown("https://a.com/")
        for k in FETCH_KEYS_020:
            assert k in payload
        assert payload["content_bytes"] == 10
        assert payload["fetch_duration_ms"] == 5
        assert payload["etag"] == '"abc"'
        assert payload["last_modified"] == "Mon, 01 Jan 2026"

    async def test_sitemap_gets_curl_fallback(self, _patch_engine):
        patcher, _ = _patch_engine({"https://a.com/": FakeResponse(_make_html())})
        with patcher, patch("mcp_server.crawler._fetch_sitemap_urls",
                            return_value=[]) as sm:
            await crawler.crawl("https://a.com/", max_pages=1, delay_ms=0)
        assert callable(sm.call_args.kwargs["fallback"])

    async def test_sitemap_scheme_and_www_alias(self, _patch_engine):
        pages = {
            "http://a.com/": FakeResponse(_make_html()),
            "http://a.com/from-www": FakeResponse(_make_html()),
        }
        patcher, _ = _patch_engine(pages)
        with patcher, patch("mcp_server.crawler._fetch_sitemap_urls",
                            return_value=["http://www.a.com/from-www"]) as sm:
            result = await crawler.crawl(
                "http://a.com/", max_pages=10, max_depth=1, delay_ms=0)
        assert sm.call_args.kwargs["scheme"] == "http"
        assert sm.call_args.args[0] == "a.com"
        urls = [p["url"] for p in result["pages"]]
        assert "http://a.com/from-www" in urls  # rewritten onto the seed host
        assert result["skipped_offsite"] == 0


@pytest.fixture
def _patch_engine():
    from mcp_server.tests.test_crawler import FakeEngine

    def _do(pages, **engine_kw):
        engine = FakeEngine(pages, **engine_kw)

        def _build_engine(*, respect_robots=True, delay_between_requests=None, **_kw):
            engine.respect_robots = respect_robots
            return engine

        return patch("mcp_server.scraper.build_engine", _build_engine), engine

    return _do


# ---------------------------------------------------------------------------
# P1-2 options
# ---------------------------------------------------------------------------

class TestOptions:
    def test_defaults_need_no_private_engine(self):
        assert scraper.resolve_options(warnings=[]).engine_overrides() == {}

    def test_clamps_reported(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_MAX_FILE_SIZE", "1000")
        w: list[str] = []
        opts = scraper.resolve_options(timeout_s=999, render_settle_ms=99999,
                                       max_file_size_bytes=5000, warnings=w)
        assert opts.timeout_s == settings.max_timeout_s()
        assert opts.render_settle_ms == settings.MAX_RENDER_SETTLE_MS
        assert opts.max_file_size_bytes == 1000  # can only lower the cap
        assert len(w) == 3

    def test_insecure_tls_needs_operator_opt_in(self, monkeypatch):
        with pytest.raises(scraper.OptionError, match="insecure TLS disabled"):
            scraper.resolve_options(allow_insecure_tls=True, warnings=[])
        monkeypatch.setenv("SCRAPER_ALLOW_INSECURE_TLS", "1")
        opts = scraper.resolve_options(allow_insecure_tls=True, warnings=[])
        assert opts.engine_overrides() == {"allow_insecure_tls": True}

    async def test_insecure_tls_rejected_by_tools(self):
        from mcp_server import server as srv
        payload = await srv.fetch_url_as_markdown(
            "https://a.com/", allow_insecure_tls=True)
        assert payload["status"] == "failed"
        assert payload["error"] == scraper.INSECURE_TLS_DISABLED
        crawl = await srv.crawl_site("https://a.com/", allow_insecure_tls=True)
        assert crawl["pages"] == [] and crawl["error"] == scraper.INSECURE_TLS_DISABLED

    async def test_include_flags_default_off(self):
        from mcp_server import server as srv
        fr = scraper.FetchResult(url="https://a.com/", markdown="hi",
                                 html="<p>hi</p>", document=b"x")
        with patch("mcp_server.scraper.fetch_one", return_value=fr):
            payload = await srv.fetch_url_as_markdown("https://a.com/")
        for k in ("html", "html_truncated", "document_base64", "document_bytes"):
            assert k not in payload
        for k in ADDITIVE_PAGE_KEYS:
            assert k in payload

    async def test_include_html_truncated(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_MAX_INLINE_HTML_BYTES", "4")
        fr = scraper.FetchResult(url="https://a.com/", html="<p>hello</p>")
        opts = scraper.FetchOptions(include_html=True)
        out = scraper.result_extras(fr, opts)
        assert out["html"] == "<p>h" and out["html_truncated"] is True

    async def test_document_base64_over_cap_omitted_with_warning(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_MAX_INLINE_DOC_BYTES", "2")
        fr = scraper.FetchResult(url="https://a.com/x.pdf", document=b"abc")
        w: list[str] = []
        out = scraper.result_extras(
            fr, scraper.FetchOptions(include_document_base64=True), w)
        assert out["document_bytes"] == 3 and "document_base64" not in out
        assert w and "inline cap" in w[0]

    async def test_respect_robots_skips(self):
        with patch("mcp_server.scraper.robots_allowed", return_value=False):
            fr = await scraper.fetch_one(
                "https://a.com/private", scraper.FetchOptions(), respect_robots=True)
        assert fr.status == "skipped" and fr.error == "robots.txt disallows"


# ---------------------------------------------------------------------------
# P1-1 batch tool
# ---------------------------------------------------------------------------

class TestBatch:
    async def test_order_counts_and_caps(self, monkeypatch):
        from mcp_server import server as srv
        monkeypatch.setenv("SCRAPER_MAX_BATCH_URLS", "3")
        monkeypatch.setenv("SCRAPER_MAX_CRAWL_CONCURRENCY", "2")

        async def fake_fetch_one(url, opts, **kw):
            status = {"a": "ok", "b": "empty", "c": "failed"}[url.rsplit("/", 1)[1]]
            return scraper.FetchResult(url=url, status=status,
                                       markdown="md" if status == "ok" else "")

        with patch("mcp_server.scraper.fetch_one", side_effect=fake_fetch_one):
            out = await srv.fetch_urls_as_markdown(
                ["https://x.com/a", "https://y.com/b", "https://z.com/c",
                 "https://w.com/d"], concurrency=10)
        assert [r["url"] for r in out["results"]] == [
            "https://x.com/a", "https://y.com/b", "https://z.com/c"]
        assert (out["count"], out["ok"], out["empty"], out["failed"]) == (3, 1, 1, 1)
        assert any("truncated" in w for w in out["warnings"])
        assert any("concurrency=10 clamped to 2" in w for w in out["warnings"])
        for r in out["results"]:
            for k in FETCH_KEYS_020:
                assert k in r

    async def test_blocked_targets_in_batch(self):
        from mcp_server import server as srv
        out = await srv.fetch_urls_as_markdown(
            ["http://169.254.169.254/", "http://10.0.0.1/admin"])
        assert out["failed"] == 2
        assert all(r["error"].startswith("blocked:") for r in out["results"])


# ---------------------------------------------------------------------------
# P1-3 / P1-4 crawl parity (engine stubbed)
# ---------------------------------------------------------------------------

def _site() -> dict[str, FakeResponse]:
    return {
        "https://a.com/": FakeResponse(_make_html([
            "https://a.com/p1", "https://a.com/p2", "https://a.com/Docs/Guide",
            "https://a.com/missing", "https://a.com/broken",
        ])),
        "https://a.com/p1": FakeResponse(_make_html(["https://a.com/p1/child"])),
        "https://a.com/p2": FakeResponse(_make_html()),
        "https://a.com/p1/child": FakeResponse(_make_html()),
        "https://a.com/Docs/Guide": FakeResponse(_make_html()),
        "https://a.com/broken": FakeResponse("error", 500),
    }


class TestCrawlParity:
    async def test_platform_call_unchanged_and_additive_keys(self, _patch_engine):
        patcher, _ = _patch_engine(_site())
        with patcher:
            result = await crawler.crawl(
                "https://a.com/", max_pages=200, max_depth=3, delay_ms=0,
                respect_robots=True, include_subdomains=False,
                exclude_patterns=None, strip_tracking_params=True,
                use_sitemap=False)
        for k in ("stats", "failed_urls", "denied_urls", "not_found_urls",
                  "challenged_urls", "duplicate_documents", "truncated",
                  "truncated_reason", "warnings", "scrape_website_version"):
            assert k in result, k
        assert result["warnings"] == [] and result["truncated"] is False
        assert result["not_found_urls"] == ["https://a.com/missing"]
        assert result["failed_urls"] == ["https://a.com/broken"]
        assert result["stats"]["not_found"] == 1 and result["stats"]["errors"] == 1
        assert result["stats"]["text_extracted"] == result["stats"]["pages_downloaded"] == 5
        for p in result["pages"]:
            for k in ADDITIVE_PAGE_KEYS:
                assert k in p
            assert "html" not in p and "document_base64" not in p
        assert [p["url"] for p in result["pages"]] == [
            "https://a.com/", "https://a.com/Docs/Guide", "https://a.com/broken",
            "https://a.com/missing", "https://a.com/p1", "https://a.com/p2",
            "https://a.com/p1/child"]

    async def test_concurrency_keeps_sequential_order(self, _patch_engine):
        patcher, _ = _patch_engine(_site())
        with patcher:
            seq = await crawler.crawl("https://a.com/", delay_ms=0, use_sitemap=False)
        patcher, _ = _patch_engine(_site())
        with patcher:
            par = await crawler.crawl("https://a.com/", delay_ms=0,
                                      use_sitemap=False, concurrency=4)
        assert [p["url"] for p in par["pages"]] == [p["url"] for p in seq["pages"]]
        assert par["fetched"] == seq["fetched"]

    async def test_concurrency_clamped(self, _patch_engine, monkeypatch):
        monkeypatch.setenv("SCRAPER_MAX_CRAWL_CONCURRENCY", "2")
        patcher, _ = _patch_engine(_site())
        with patcher:
            r = await crawler.crawl("https://a.com/", delay_ms=0,
                                    use_sitemap=False, concurrency=50)
        assert "concurrency=50 clamped to 2 (server limit)" in r["warnings"]

    async def test_max_pages_still_honored_concurrently(self, _patch_engine):
        patcher, _ = _patch_engine(_site())
        with patcher:
            r = await crawler.crawl("https://a.com/", max_pages=3, delay_ms=0,
                                    use_sitemap=False, concurrency=4)
        assert r["fetched"] == 3
        assert r["truncated"] is True and r["truncated_reason"] == "max_pages"

    async def test_extra_exclude_appends_to_defaults(self, _patch_engine):
        site = _site()
        site["https://a.com/"] = FakeResponse(_make_html(
            ["https://a.com/p1", "https://a.com/tag/x", "https://a.com/p2"]))
        patcher, _ = _patch_engine(site)
        with patcher:
            r = await crawler.crawl("https://a.com/", delay_ms=0, use_sitemap=False,
                                    max_depth=1, extra_exclude_patterns=[r"/p2$"])
        urls = {p["url"] for p in r["pages"]}
        assert "https://a.com/p2" not in urls          # extra pattern
        assert "https://a.com/tag/x" not in urls       # default still active
        assert "https://a.com/p1" in urls

    async def test_case_sensitive_patterns(self, _patch_engine):
        patcher, _ = _patch_engine(_site())
        with patcher:
            insensitive = await crawler.crawl(
                "https://a.com/", delay_ms=0, use_sitemap=False, max_depth=1,
                extra_exclude_patterns=[r"/docs/"])
        patcher, _ = _patch_engine(_site())
        with patcher:
            sensitive = await crawler.crawl(
                "https://a.com/", delay_ms=0, use_sitemap=False, max_depth=1,
                extra_exclude_patterns=[r"/docs/"],
                exclude_patterns_case_sensitive=True)
        assert "https://a.com/Docs/Guide" not in {p["url"] for p in insensitive["pages"]}
        assert "https://a.com/Docs/Guide" in {p["url"] for p in sensitive["pages"]}

    async def test_include_patterns(self, _patch_engine):
        patcher, _ = _patch_engine(_site())
        with patcher:
            r = await crawler.crawl("https://a.com/", delay_ms=0, use_sitemap=False,
                                    include_patterns=[r"/p1"])
        urls = [p["url"] for p in r["pages"]]
        assert urls == ["https://a.com/", "https://a.com/p1", "https://a.com/p1/child"]
        assert r["skipped_not_included"] >= 3

    async def test_pattern_caps_and_bad_regex_rejected(self):
        with pytest.raises(ValueError, match="at most"):
            await crawler.crawl("https://a.com/", include_patterns=["x"] * 101)
        with pytest.raises(ValueError, match="longer than"):
            await crawler.crawl("https://a.com/", extra_exclude_patterns=["x" * 201])
        with pytest.raises(ValueError, match="invalid regex"):
            await crawler.crawl("https://a.com/", extra_exclude_patterns=["("])

    async def test_additional_seeds(self, _patch_engine):
        patcher, _ = _patch_engine(_site())
        with patcher:
            r = await crawler.crawl(
                "https://a.com/", delay_ms=0, use_sitemap=False, max_depth=0,
                additional_seed_urls=["https://a.com/p2", "https://other.com/x",
                                      "https://a.com/tag/explicit"])
        by_url = {p["url"]: p for p in r["pages"]}
        assert by_url["https://a.com/p2"]["depth"] == 0
        assert "https://a.com/tag/explicit" in by_url  # explicit seeds bypass excludes
        assert "https://other.com/x" not in by_url
        assert r["skipped_offsite"] >= 1

    async def test_collapse_host_aliases(self, _patch_engine):
        site = {
            "https://a.com/": FakeResponse(_make_html(
                ["https://www.a.com/p1", "http://a.com/p1", "http://www.a.com/p2"])),
            "https://a.com/p1": FakeResponse(_make_html()),
            "https://a.com/p2": FakeResponse(_make_html()),
        }
        patcher, _ = _patch_engine(site)
        with patcher:
            default = await crawler.crawl("https://a.com/", delay_ms=0, use_sitemap=False)
        patcher, _ = _patch_engine(site)
        with patcher:
            collapsed = await crawler.crawl("https://a.com/", delay_ms=0,
                                            use_sitemap=False, collapse_host_aliases=True)
        assert [p["url"] for p in collapsed["pages"]] == [
            "https://a.com/", "https://a.com/p1", "https://a.com/p2"]
        # Default: www alias is off-site; http variant is a separate page.
        default_urls = {p["url"] for p in default["pages"]}
        assert "https://a.com/p2" not in default_urls
        assert "http://a.com/p1" in default_urls

    async def test_follow_offsite_documents(self, _patch_engine):
        html = ('<html><head><title>T</title><link rel="alternate" '
                'href="https://cdn.example.net/files/annual.pdf"></head><body>'
                '<article><p>Content.</p>'
                '<a href="https://cdn.example.net/files/brochure.pdf">pdf</a>'
                '<a href="https://cdn.example.net/page">offsite page</a>'
                '<a href="http://169.254.169.254/creds.pdf">bad</a>'
                '</article></body></html>')
        site = {
            "https://a.com/": FakeResponse(html),
            "https://cdn.example.net/files/annual.pdf": FakeResponse(
                b"%PDF-1.4 a", content_type="application/pdf"),
            "https://cdn.example.net/files/brochure.pdf": FakeResponse(
                b"%PDF-1.4 b", content_type="application/pdf"),
        }
        patcher, _ = _patch_engine(site)
        with patcher:
            default = await crawler.crawl("https://a.com/", delay_ms=0, use_sitemap=False)
        assert [p["url"] for p in default["pages"]] == ["https://a.com/"]
        patcher, _ = _patch_engine(site)
        with patcher:
            r = await crawler.crawl("https://a.com/", delay_ms=0, use_sitemap=False,
                                    follow_offsite_documents=True)
        urls = {p["url"] for p in r["pages"]}
        assert "https://cdn.example.net/files/annual.pdf" in urls
        assert "https://cdn.example.net/files/brochure.pdf" in urls
        assert "https://cdn.example.net/page" not in urls
        assert "http://169.254.169.254/creds.pdf" not in urls  # SSRF-gated

    async def test_dedupe_documents(self, _patch_engine):
        same = b"%PDF-1.4 identical bytes"
        site = {
            "https://a.com/": FakeResponse(_make_html(
                ["https://a.com/one.pdf", "https://a.com/two.pdf"])),
            "https://a.com/one.pdf": FakeResponse(same, content_type="application/pdf"),
            "https://a.com/two.pdf": FakeResponse(same, content_type="application/pdf"),
        }
        patcher, _ = _patch_engine(site)
        with patcher:
            r = await crawler.crawl("https://a.com/", delay_ms=0, use_sitemap=False,
                                    dedupe_documents=True)
        two = next(p for p in r["pages"] if p["url"].endswith("two.pdf"))
        assert two["status"] == "skipped"
        assert two["error"] == "duplicate document"
        assert two["duplicate_of"] == "https://a.com/one.pdf"
        assert r["duplicate_documents"] == 1

    async def test_max_total_bytes_truncates(self, _patch_engine):
        patcher, _ = _patch_engine(_site())
        with patcher:
            r = await crawler.crawl("https://a.com/", delay_ms=0, use_sitemap=False,
                                    max_total_bytes=1)
        assert r["fetched"] == 1
        assert r["truncated"] is True and r["truncated_reason"] == "max_total_bytes"

    async def test_challenge_classified(self, _patch_engine):
        site = {
            "https://a.com/": FakeResponse(_make_html(["https://a.com/wall"])),
            "https://a.com/wall": FakeResponse(
                "<html><body>Just a moment... cf_chl_opt</body></html>"),
        }
        patcher, _ = _patch_engine(site)
        with patcher:
            r = await crawler.crawl("https://a.com/", delay_ms=0, use_sitemap=False)
        wall = next(p for p in r["pages"] if p["url"] == "https://a.com/wall")
        assert wall["status"] == "failed"
        assert wall["classification"] == "challenge"
        assert wall["error"] == "challenge: Cloudflare interstitial (HTTP 200)"
        assert r["challenged_urls"] == ["https://a.com/wall"]

    async def test_include_html(self, _patch_engine):
        patcher, _ = _patch_engine(_site())
        with patcher:
            r = await crawler.crawl(
                "https://a.com/", delay_ms=0, use_sitemap=False, max_depth=0,
                fetch_options=scraper.FetchOptions(include_html=True))
        page = r["pages"][0]
        assert "<title>Test</title>" in page["html"]
        assert page["html_truncated"] is False
        assert page["metadata"].get("title") == "Test"


# ---------------------------------------------------------------------------
# P1-5 server_health
# ---------------------------------------------------------------------------

class TestHealth:
    async def test_versions_and_capabilities(self, monkeypatch, tmp_path):
        from mcp_server import __version__
        from mcp_server import server as srv
        from mcp_server.store import Store
        monkeypatch.setattr(srv, "_store", Store(db_path=tmp_path / "h.db"))
        secret_path = str(tmp_path / "very-secret-cookies.txt")
        monkeypatch.setenv("SCRAPE_CF_COOKIES", secret_path)
        h = await srv.server_health()
        assert h["server_version"] == __version__
        assert h["scrape_website_version"] == "0.7.3"
        caps = h["capabilities"]
        for k in ("render", "waf", "docs", "docling", "cookies_file_configured",
                  "insecure_tls_allowed", "private_targets_allowed",
                  "max_crawl_concurrency", "user_agent_major"):
            assert k in caps
        assert caps["cookies_file_configured"] is True
        assert caps["private_targets_allowed"] is False
        assert "very-secret-cookies" not in json.dumps(h)


# ---------------------------------------------------------------------------
# Integration: the REAL guarded engine against a local fixture server
# ---------------------------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        port = self.server.server_address[1]
        if self.path == "/":
            self._send(200, "<html><head><title>Home</title></head><body><p>"
                       + "Fixture page with enough prose to extract. " * 10
                       + "</p></body></html>")
        elif self.path == "/redirect-ip":
            self._redirect(f"http://127.0.0.2:{port}/")
        elif self.path == "/redirect-host":
            self._redirect(f"http://localhost:{port}/")
        elif self.path == "/secret":
            self._send(200, "INTERNAL-SECRET-TOKEN",
                       extra={"Access-Control-Allow-Origin": "*"},
                       ctype="text/plain")
        elif self.path == "/exfil":
            self._send(200, (
                '<html><head><title>X</title></head><body><div id="root"></div><script>'
                f'fetch("http://localhost:{port}/secret").then(r => r.text())'
                '.then(t => { document.getElementById("root").innerHTML ='
                ' "<h1>Leak</h1><p>" + (t + " ").repeat(30) + "</p>"; })'
                '.catch(() => { document.getElementById("root").innerHTML ='
                ' "<h1>Blocked</h1><p>" + "nothing to see here. ".repeat(30) + "</p>"; });'
                '</script></body></html>'))
        elif self.path in ("/fetch-hop", "/fetch-chain", "/fetch-ok"):
            # Page JS reads a same-origin URL that redirects: straight to the
            # internal host, via an allowed hop first, or (control) to an
            # allowed URL.
            target = {"/fetch-hop": "/hop-internal", "/fetch-chain": "/hop-chain",
                      "/fetch-ok": "/hop-ok"}[self.path]
            self._send(200, _render_probe_page(
                f'fetch("{target}").then(r => r.text())'))
        elif self.path == "/hop-internal":
            self._redirect(f"http://localhost:{port}/secret")
        elif self.path == "/hop-chain":
            self._redirect(f"http://127.0.0.1:{port}/hop-internal")
        elif self.path == "/hop-ok":
            self._redirect(f"http://127.0.0.1:{port}/secret")
        elif self.path == "/sw.js":
            self._send(200, (
                "self.addEventListener('install', e => self.skipWaiting());"
                "self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));"
                "self.addEventListener('message', async e => {"
                " let t = 'SW-FAILED';"
                " try { t = await (await fetch(e.data)).text(); } catch (err) {}"
                " e.source.postMessage(t); });"), ctype="application/javascript")
        elif self.path == "/sw-exfil":
            self._send(200, _render_probe_page(
                'navigator.serviceWorker.register("/sw.js")'
                '.then(() => navigator.serviceWorker.ready)'
                '.then(reg => new Promise(resolve => {'
                ' navigator.serviceWorker.onmessage = e => resolve(e.data);'
                f' reg.active.postMessage("http://localhost:{port}/secret"); }}))'))
        elif self.path == "/guidance":
            body = _minimal_docx("Docx served without an extension")
            self.send_response(200)
            self.send_header("Content-Type", DOCX_MIME)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/robots.txt":
            self._send(200, "User-agent: *\nDisallow: /secret\n", ctype="text/plain")
        else:
            self._send(404, "not found")

    def _send(self, status, body, extra=None, ctype="text/html; charset=utf-8"):
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


def _render_probe_page(promise_js: str) -> str:
    """Page whose JS writes the resolved text of *promise_js* (or 'Blocked')
    into the DOM, padded so it extracts as content."""
    return (
        '<html><head><title>X</title></head><body><div id="root"></div><script>'
        'const show = (h, t) => { document.getElementById("root").innerHTML ='
        ' "<h1>" + h + "</h1><p>" + (t + " ").repeat(30) + "</p>"; };'
        + promise_js + '.then(t => show("Read", t))'
        '.catch(() => show("Blocked", "nothing to see here."));'
        '</script></body></html>')


@pytest.fixture
def fixture_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _only_v4_loopback(ip) -> bool:
    """Test predicate: allow 127.0.0.1 (the fixture), block everything else
    — so a hop to 127.0.0.2 or to ::1 (localhost's v6 answer) is 'internal'."""
    return str(ipaddress.ip_address(str(ip).split("%")[0])) != "127.0.0.1"


def _engine(**kw) -> GuardedFetchEngine:
    return GuardedFetchEngine(block_private=True, is_blocked=_only_v4_loopback,
                              max_retries=1, render_settle_ms=300, **kw)


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return bool(p.chromium.executable_path)
    except Exception:
        return False


@pytest.mark.integration
class TestGuardedEngineIntegration:
    async def test_allowed_target_fetches(self, fixture_server):
        eng = _engine()
        try:
            fr = await scraper.fetch_and_extract(f"{fixture_server}/", engine=eng)
        finally:
            await eng.close()
        assert fr.status == "ok" and "Fixture page" in fr.markdown

    async def test_redirect_to_internal_ip_blocked(self, fixture_server):
        eng = _engine()
        try:
            fr = await scraper.fetch_and_extract(
                f"{fixture_server}/redirect-ip", engine=eng)
        finally:
            await eng.close()
        assert fr.status == "failed"
        assert "private/internal" in fr.error

    async def test_redirect_to_internal_hostname_blocked(self, fixture_server):
        eng = _engine()
        try:
            fr = await scraper.fetch_and_extract(
                f"{fixture_server}/redirect-host", engine=eng)
        finally:
            await eng.close()
        assert fr.status == "failed"
        assert "blocked target" in fr.error

    async def test_curl_tier_follows_and_blocks_hops(self, fixture_server):
        eng = _engine()
        ok = await eng._curl_get(f"{fixture_server}/")
        assert ok is not None and ok[3] == 200 and "Fixture page" in ok[0]
        assert await eng._curl_get(f"{fixture_server}/redirect-ip") is None

    async def test_curl_tier_matches_upstream_073_signature(self, fixture_server):
        # Upstream 0.7.3 load_robots/sitemap call _curl_get(replay_cookies=,
        # raw=); a mismatch would be swallowed inside load_robots.
        eng = _engine()
        res = await eng._curl_get(f"{fixture_server}/robots.txt",
                                  replay_cookies=False, raw=True)
        assert res is not None and isinstance(res[0], bytes)
        assert b"Disallow: /secret" in res[0]
        via = await eng._fetch_via_curl_cffi(
            f"{fixture_server}/robots.txt", cookie_bridge=False, raw=True)
        assert via is not None and via[3] == 200

    async def test_curl_tier_pins_resolved_hostname(self, fixture_server):
        port = fixture_server.rsplit(":", 1)[1]
        eng = GuardedFetchEngine(block_private=True, max_retries=1,
                                 is_blocked=lambda ip: False)
        res = await eng._curl_get(f"http://localhost:{port}/")
        assert res is not None and res[3] == 200

    async def test_extensionless_docx_end_to_end(self, fixture_server):
        eng = _engine()
        try:
            fr = await scraper.fetch_and_extract(f"{fixture_server}/guidance", engine=eng)
        finally:
            await eng.close()
        assert fr.status == "ok", fr.error
        assert fr.content_kind == "docx"
        assert "Docx served without an extension" in fr.markdown

    async def test_single_fetch_respect_robots(self, fixture_server, monkeypatch):
        monkeypatch.setenv("SCRAPER_ALLOW_PRIVATE_TARGETS", "1")
        from mcp_server import server as srv
        skipped = await srv.fetch_url_as_markdown(
            f"{fixture_server}/secret", respect_robots=True)
        assert skipped["status"] == "skipped"
        assert skipped["error"] == "robots.txt disallows"
        allowed = await srv.fetch_url_as_markdown(f"{fixture_server}/secret")
        assert allowed["status"] == "ok"

    async def test_batch_end_to_end(self, fixture_server, monkeypatch):
        monkeypatch.setenv("SCRAPER_ALLOW_PRIVATE_TARGETS", "1")
        from fastmcp import Client
        from mcp_server.server import mcp
        async with Client(mcp) as client:
            res = await client.call_tool("fetch_urls_as_markdown", {
                "urls": [f"{fixture_server}/", f"{fixture_server}/nope"],
                "render_mode": "never", "timeout_s": 5})
        out = _payload(res)
        assert out["count"] == 2 and out["ok"] == 1 and out["failed"] == 1
        assert out["results"][1]["classification"] == "not_found"

    @pytest.mark.skipif(not _chromium_available(), reason="Chromium not installed")
    async def test_render_cannot_reach_internal_hosts(self, fixture_server):
        # Control: unguarded, the page's JS reads the internal endpoint.
        open_eng = GuardedFetchEngine(block_private=False, render_settle_ms=500)
        try:
            leaked = await open_eng.render(f"{fixture_server}/exfil")
        finally:
            await open_eng.close()
        assert leaked and "INTERNAL-SECRET-TOKEN" in leaked
        eng = _engine()
        try:
            html = await eng.render(f"{fixture_server}/exfil")
        finally:
            await eng.close()
        assert html and "INTERNAL-SECRET-TOKEN" not in html
        assert "Blocked" in html

    @pytest.mark.skipif(not _chromium_available(), reason="Chromium not installed")
    @pytest.mark.parametrize("path", ["/fetch-hop", "/fetch-chain", "/sw-exfil"])
    async def test_render_redirect_and_service_worker_cannot_reach_internal(
            self, fixture_server, path):
        # page.route never sees redirect hops or service-worker requests;
        # the guard has to cover both.
        eng = _engine()
        try:
            html = await eng.render(f"{fixture_server}{path}")
        finally:
            await eng.close()
        assert html and "INTERNAL-SECRET-TOKEN" not in html

    @pytest.mark.skipif(not _chromium_available(), reason="Chromium not installed")
    async def test_render_allowed_fetch_redirect_still_works(self, fixture_server):
        eng = _engine()
        try:
            html = await eng.render(f"{fixture_server}/fetch-ok")
        finally:
            await eng.close()
        assert html and "<h1>Read</h1>" in html and "INTERNAL-SECRET-TOKEN" in html
