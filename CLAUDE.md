# scrape-website-mcp

## Overview

StreamableHTTP MCP server that exposes [ventz/scrape-website](https://github.com/ventz/scrape-website)'s fetch/crawl engine as MCP tools for URL-to-markdown conversion, with optional OpenAI vector-store integration. Designed primarily as a backend for the Harvard EA assistants platform but usable by any MCP client.

## Architecture

- **Transport**: StreamableHTTP (FastMCP v2), mounted at `/mcp`.
- **Entrypoint**: `mcp_server/server.py` defines all MCP tools; `app.py` is a thin stub.
- **Scraping**: `mcp_server/scraper.py` **imports the `scrape_website` package as a library** (editable uv path dependency on `vendor/scrape-website/`, cloned by `make setup` / the Dockerfile — see `[tool.uv.sources]`). It does NOT shell out to the CLI and does NOT copy fetch code. The shared `FetchEngine` provides the full tier stack: aiohttp retry/backoff + Retry-After → curl_cffi WAF/403 fallback (+`SCRAPE_CF_COOKIES` clearance replay) → headless-Chromium SPA render escalation; plus protego robots + Crawl-Delay, and PDF/Office→Markdown document extraction.
  - Single-page fetches share a lazy module singleton engine (`scraper._engine()`, reset hook `scraper.reset_engine()`); each `crawl()` builds a FRESH engine (robots/Crawl-Delay state is per-host) and closes it in a `finally`.
  - `scraper.fetch_page_result()` is the shared core (fetch → extract → render-escalate → `FetchResult`); `fetch_and_extract()` wraps it for single pages, `crawler.crawl()` passes its own scope-aware link extractor.
- **Crawler**: `mcp_server/crawler.py` — sequential BFS (deliberate: platform parallelizes uploads; we stay polite). Default excludes = upstream CMS-noise patterns + images/static assets; **documents are NOT excluded** (0.2.0): they're fetched and extracted unless `extract_docs=false`.
- **State**: SQLite database via `mcp_server/store.py` (default dir: `./data/`). Tracks registered URLs and their content hashes.
- **Auth**: Bearer-token middleware in `mcp_server/auth.py`; token set via `MCP_BEARER_TOKEN` env var.
- **OpenAI sync**: `mcp_server/openai_sync.py` uploads markdown to an OpenAI vector store with replace-by-URL semantics.
- **Per-URL locking**: `defaultdict(asyncio.Lock)` in server.py prevents concurrent syncs of the same URL.
- **Progress**: `crawl_site` takes an injected FastMCP `Context` and emits `report_progress` per page — doubles as a keep-alive while the platform holds the call open (600s client timeout on its side).

## Key Commands

```bash
make setup            # Clone vendor/scrape-website + uv sync + install chromium-headless-shell
make run              # Start server on :8000 (loads .env automatically)
make test             # Run pytest suite (unit + integration; SPA-render test auto-skips without Chromium)
make update-scraper   # Pull latest scrape-website ref + re-lock (this is how upstream improvements land)
make docker-build     # Build Docker image (~1.1-1.5GB: ships Chromium + doc-extraction stack)
make docker-run       # Run via Docker with .env, data volume, --shm-size=1g
make clean            # Remove vendor/, data/, caches
```

## Development

- **Python**: Requires >=3.13, managed with `uv`.
- **Dependencies**: FastMCP >=2.0, openai, aiohttp, uvicorn, `scrape-website[render,waf,docs]` (editable path dep on `vendor/scrape-website`; the `human` extra is intentionally NOT installed — interactive solving is workstation-only).
- **Dev dependencies**: pytest, pytest-asyncio, pytest-mock.
- **Test config**: `asyncio_mode = "auto"` in pyproject.toml; tests in `mcp_server/tests/`. `test_crawler.py` stubs the engine at the `mcp_server.scraper.build_engine` seam (class `FakeEngine`); `TestPlatformContract` is the back-compat tripwire for every key the Harvard platform's `core/scraper_proxy.py` reads — don't break it.

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `MCP_BEARER_TOKEN` | Yes | Auth token for incoming MCP requests |
| `OPENAI_API_KEY` | No* | Needed only for register/resync/unregister tools |
| `STATE_DIR` | No | SQLite data dir (default: `./data`) |
| `SCRAPE_WEBSITE_REF` | No | Git ref of the vendored engine (branch pin; move to a tag after upstream release) |
| `SCRAPER_USER_AGENT` | No | Outbound UA. **Default is upstream's Chrome UA** (0.2.0 policy change — WAF cookie replay is UA-bound); set explicitly to restore an honest bot UA |
| `SCRAPER_TIMEOUT` | No | Per-request timeout, seconds (default 30) |
| `SCRAPER_RENDER_MODE` | No | `auto` (default) / `always` / `never`; per-call `render_mode` overrides |
| `SCRAPER_EXTRACT_DOCS` | No | `1` (default); per-call `extract_docs` overrides |
| `SCRAPER_MAX_FILE_SIZE` | No | Doc-extraction size cap, bytes (default 50MB) |
| `SCRAPER_BROWSER_ARGS` | No | Chromium launch args; unset + `SCRAPER_IN_DOCKER=1` → `--disable-dev-shm-usage --no-sandbox` |
| `SCRAPE_CF_COOKIES` | No | Exported cookies file for WAF clearance replay (headless-safe alternative to `--human`) |

*The assistants platform calls `fetch_url_as_markdown` and `crawl_site`, neither of which has an OpenAI dependency.

## Important Patterns

- `.env` is gitignored; `.env.example` has safe placeholder values.
- The vendored engine (`vendor/`) is gitignored and cloned at setup/build time; it is read-only here — never commit from inside it.
- `OPENAI_API_KEY` is optional by design: the platform-facing tools work without it.
- Docker build accepts `SCRAPE_WEBSITE_REF` as a build arg to pin the engine version; `PLAYWRIGHT_BROWSERS_PATH=/ms-playwright` bakes Chromium into the image.
- Tool results are additive-only across versions: 0.2.0 added `rendered`/`via`/`content_kind` per page and `skipped_documents`/`rendered_count`/`docs_extracted` at crawl top level. Never rename/remove existing keys — the platform contract test enforces this.
- Front-matter note: markdown now includes upstream's YAML front matter (title/url/hostname) — this changed content hashes once at 0.2.0, causing a one-time resync churn.
