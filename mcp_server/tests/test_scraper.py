"""Tests for the scraper helpers. We don't exercise the live network — just
the pure functions and the markdown extraction path on a small HTML snippet.
"""

from __future__ import annotations

from mcp_server import scraper


def test_normalize_url_strips_trailing_slash():
    assert scraper.normalize_url("https://example.com/foo/") == "https://example.com/foo"


def test_normalize_url_keeps_root_slash():
    assert scraper.normalize_url("https://example.com/") == "https://example.com/"


def test_normalize_url_drops_fragment():
    assert scraper.normalize_url("https://example.com/foo#section") == "https://example.com/foo"


def test_html_to_markdown_extracts_text():
    html = """
    <html><body>
      <article>
        <h1>Title</h1>
        <p>Hello <strong>world</strong>.</p>
      </article>
    </body></html>
    """
    md = scraper.html_to_markdown(html, "https://example.com/")
    # trafilatura output ordering and exact formatting varies, but the text
    # must be present.
    assert "Title" in md
    assert "Hello" in md
    assert "world" in md


def test_html_to_markdown_empty_on_garbage():
    assert scraper.html_to_markdown("<html></html>", "https://example.com/") == ""


def test_extract_title_from_title_tag():
    html = "<html><head><title>Hello World</title></head><body><h1>Other</h1></body></html>"
    assert scraper._extract_title(html) == "Hello World"


def test_extract_title_falls_back_to_h1():
    html = "<html><body><h1>Just an H1</h1></body></html>"
    assert scraper._extract_title(html) == "Just an H1"


def test_extract_title_none_when_neither():
    assert scraper._extract_title("<html><body><p>x</p></body></html>") is None
