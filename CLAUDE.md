# scrape-website-mcp

## Overview

StreamableHTTP MCP server that wraps [ventz/scrape-website](https://github.com/ventz/scrape-website) for URL-to-markdown conversion, with optional OpenAI vector-store integration. Designed primarily as a backend for the Harvard EA assistants platform but usable by any MCP client.

## Architecture

- **Transport**: StreamableHTTP (FastMCP v2), mounted at `/mcp`.
- **Entrypoint**: `mcp_server/server.py` defines all MCP tools; `app.py` is a thin stub.
- **Scraping**: `mcp_server/scraper.py` shells out to the vendored `ventz/scrape-website` CLI (cloned into `vendor/scrape-website/` by `make setup`).
- **State**: SQLite database via `mcp_server/store.py` (default dir: `./data/`). Tracks registered URLs and their content hashes.
- **Auth**: Bearer-token middleware in `mcp_server/auth.py`; token set via `MCP_BEARER_TOKEN` env var.
- **OpenAI sync**: `mcp_server/openai_sync.py` uploads markdown to an OpenAI vector store with replace-by-URL semantics.
- **Per-URL locking**: `defaultdict(asyncio.Lock)` in server.py prevents concurrent syncs of the same URL.

## Key Commands

```bash
make setup            # Clone vendored scraper + uv sync
make run              # Start server on :8000 (loads .env automatically)
make test             # Run pytest suite
make update-scraper   # Pull latest ventz/scrape-website ref
make docker-build     # Build Docker image
make docker-run       # Run via Docker with .env and data volume
make clean            # Remove vendor/, data/, caches
```

## Development

- **Python**: Requires >=3.13, managed with `uv`.
- **Dependencies**: FastMCP >=2.0, openai, aiohttp, trafilatura, lxml, uvicorn.
- **Dev dependencies**: pytest, pytest-asyncio, pytest-mock.
- **Test config**: `asyncio_mode = "auto"` in pyproject.toml; tests in `mcp_server/tests/`.

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `MCP_BEARER_TOKEN` | Yes | Auth token for incoming MCP requests |
| `OPENAI_API_KEY` | No* | Needed only for register/resync/unregister tools |
| `STATE_DIR` | No | SQLite data dir (default: `./data`) |
| `SCRAPE_WEBSITE_REF` | No | Git ref of vendored scraper (default: `main`) |
| `SCRAPER_USER_AGENT` | No | User-Agent for outbound scrapes |

*The assistants platform only calls `fetch_url_as_markdown`, which has no OpenAI dependency.

## Important Patterns

- `.env` is gitignored; `.env.example` has safe placeholder values.
- The vendored scraper (`vendor/`) is gitignored and cloned at setup time.
- `OPENAI_API_KEY` is optional by design: the platform-facing tool (`fetch_url_as_markdown`) works without it.
- Docker build accepts `SCRAPE_WEBSITE_REF` as a build arg to pin the scraper version.
