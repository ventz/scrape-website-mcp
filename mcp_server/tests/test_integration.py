"""End-to-end integration tests: real FastMCP tool calls (in-memory client)
against a local fixture HTTP server, through the REAL FetchEngine.

Marked `integration`: they bind local sockets and (for the render test) need
Chromium installed (`uv run playwright install chromium-headless-shell`).
Run with: uv run pytest -m integration
"""

from __future__ import annotations

import http.server
import json
import threading

import pytest

from mcp_server import scraper

pytestmark = pytest.mark.integration

FIXTURES = {
    "/": '<html><head><title>Home</title></head><body>'
         '<p>Fixture home page with enough prose that trafilatura keeps it '
         'and the extraction pipeline yields real markdown output.</p>'
         '<a href="/about">About</a></body></html>',
    "/about": '<html><head><title>About</title></head><body>'
              '<p>About page content, also long enough to extract cleanly.</p>'
              '</body></html>',
    "/spa": '<html><head><title>SPA</title></head><body><div id="root"></div>'
            '<script>document.getElementById("root").innerHTML = '
            '"<h1>Hydrated</h1><p>" + "Client-rendered content. ".repeat(30) + "</p>";'
            '</script></body></html>',
}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = FIXTURES.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        payload = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


@pytest.fixture
def fixture_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return bool(p.chromium.executable_path)
    except Exception:
        return False


def _tool_payload(result) -> dict:
    """Unwrap a FastMCP CallToolResult into the tool's dict payload."""
    if getattr(result, "structured_content", None):
        return result.structured_content
    return json.loads(result.content[0].text)


@pytest.fixture(autouse=True)
def _fresh_engine():
    scraper.reset_engine()
    yield
    scraper.reset_engine()


async def test_fetch_url_as_markdown_end_to_end(fixture_server):
    from fastmcp import Client
    from mcp_server.server import mcp

    async with Client(mcp) as client:
        result = await client.call_tool(
            "fetch_url_as_markdown",
            {"url": f"{fixture_server}/", "render_mode": "never"})
    payload = _tool_payload(result)
    assert payload["status"] == "ok"
    assert "Fixture home page" in payload["markdown"]
    assert payload["page_title"] == "Home"
    assert payload["via"] == "aiohttp"
    assert payload["rendered"] is False


async def test_crawl_site_end_to_end(fixture_server):
    from fastmcp import Client
    from mcp_server.server import mcp

    async with Client(mcp) as client:
        result = await client.call_tool("crawl_site", {
            "seed_url": f"{fixture_server}/",
            "max_pages": 10, "max_depth": 2, "delay_ms": 0,
            "use_sitemap": False, "render_mode": "never",
        })
    payload = _tool_payload(result)
    assert payload["fetched"] >= 2  # / and /about
    urls = {p["url"] for p in payload["pages"]}
    assert any(u.endswith("/about") for u in urls)
    ok = [p for p in payload["pages"] if p["status"] == "ok"]
    assert ok and all(p["markdown"].strip() for p in ok)


@pytest.mark.skipif(not _chromium_available(), reason="Chromium not installed")
async def test_spa_shell_render_escalation(fixture_server):
    """The /spa fixture is an un-hydrated shell: static extraction yields
    nothing, auto render escalation must recover the client-rendered text."""
    fetched = await scraper.fetch_and_extract(
        f"{fixture_server}/spa", render_mode="auto")
    assert fetched.rendered is True
    assert "Client-rendered content" in fetched.markdown
    assert fetched.status == "ok"
