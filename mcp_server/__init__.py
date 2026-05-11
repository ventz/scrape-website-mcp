"""FastMCP-based MCP server that scrapes URLs into an OpenAI vector store
with replace-by-URL semantics. Uses ventz/scrape-website (pulled in at
build/setup time) for HTML-to-text extraction.
"""

__version__ = "0.1.0"
