# scrape-website-mcp

A self-hosted **MCP server** that scrapes URLs into clean markdown. Designed to be wired into an OpenAI-Assistants / Responses agent as a remote tool — either through a managing platform (which performs the OpenAI vector-store upload itself), or as a standalone server that uploads into your own vector store with **replace-by-URL semantics**.

Companion to **[github.com/ventz/scrape-website](https://github.com/ventz/scrape-website)** — this server imports upstream's `scrape_website` package (the shared `FetchEngine`) as a real library dependency, so the FULL upstream fetch stack runs here too:

- **JS/SPA rendering** — un-hydrated SPA shells auto-escalate to headless Chromium (`render_mode=auto|always|never`)
- **WAF/403 fallback** — curl_cffi Chrome-TLS-fingerprint retry for Cloudflare/Imperva/Akamai blocks (+ optional exported-cookie clearance via `SCRAPE_CF_COOKIES`)
- **PDF/Office → Markdown** — documents found while crawling are extracted with PyMuPDF4LLM / MarkItDown (`extract_docs`, default on)
- **Robustness** — retry/backoff + `Retry-After`, protego robots.txt + adaptive Crawl-Delay pacing, sitemap-index-aware seeding

Upstream improvements land with `make update-scraper` (which refreshes the pinned checkout and re-locks). No copied fetch code — the engine is one codebase shared with the standalone CLI.

---

## Table of Contents

1. [Why a separate repo?](#1-why-a-separate-repo)
2. [Two ways to use this server](#2-two-ways-to-use-this-server)
3. [Tools exposed](#3-tools-exposed)
4. [Quick start](#4-quick-start)
    - [Docker](#docker)
    - [Local dev](#local-dev)
5. [Wiring into the Harvard EA assistants platform](#5-wiring-into-the-harvard-ea-assistants-platform)
6. [Auth model](#6-auth-model)
7. [How "replace by URL" works](#7-how-replace-by-url-works)
8. [State](#8-state)
9. [Configuration reference](#9-configuration-reference)
10. [License](#10-license)

---

## 1. Why a separate repo?

`ventz/scrape-website` is a standalone CLI crawler — useful on its own for archiving sites to disk. This project is the **MCP-server adapter** that wraps the same extraction logic and exposes it as remote tools that an OpenAI-Assistants / Responses agent can call. Keeping them separate lets the CLI stay lean and lets the MCP server own its own deployment story (Docker, OpenAI-vector-store sync, bearer auth, state DB).

The MCP server **does not copy** scrape-website code. It installs upstream as an editable uv path dependency (`scrape-website[render,waf,docs]`, see `[tool.uv.sources]`):

- **Docker:** `git clone --depth 1 --branch ${SCRAPE_WEBSITE_REF}` into `/app/vendor/scrape-website` inside the image build, then `uv sync`.
- **Local dev:** `make setup` clones into `vendor/scrape-website/` (which is `.gitignore`d) and syncs.

Pin to a tag / SHA with `SCRAPE_WEBSITE_REF=<ref>`. The `human` extra (interactive `--human` challenge solving) is deliberately not installed — that flow needs a workstation with a real Chrome; on a headless server use `SCRAPE_CF_COOKIES` instead.

---

## 2. Two ways to use this server

| Mode | What the platform expects | OpenAI key needed here? |
|------|---------------------------|-------------------------|
| **Platform-driven (preferred)** — wired into the Harvard EA assistants platform's "Scrape Website (self-hosted)" prebuilt MCP card. | Platform calls **only** `fetch_url_as_markdown` and performs vector-store uploads itself using the *agent's* OpenAI key. | **No.** Leave `OPENAI_API_KEY` blank in `.env`. |
| **Standalone** — any MCP client calling this server directly. | Client calls `register_url` / `resync_url` / `unregister_url`. This server performs OpenAI uploads itself. | **Yes** — `OPENAI_API_KEY` must be a project-scoped key that owns the vector store you target. |

Both modes are supported by the same tools; the platform just exercises a smaller subset. See §6 for why platform-driven is the safer default.

---

## 3. Tools exposed

| Tool | Used by platform? | Purpose |
|------|------------------|---------|
| `fetch_url_as_markdown(url, render_mode?)` | ✓ | Live one-shot scrape, returns markdown + metadata (HTTP status, page title, content bytes, fetch duration, plus `rendered`/`via`/`content_kind`). No vector store, no state. |
| `register_url(url, vector_store_id)` | — | Scrape the URL → upload as markdown into the given vector store, tagged with `source_url`, `content_hash`, `fetched_at` attributes. Idempotent. |
| `resync_url(url)` | — | Re-scrape. If content hash changed, upload new file, wait for indexing, then delete the old VS file and the underlying File object. |
| `resync_all()` | — | Run `resync_url` for every registered URL with bounded concurrency. Cron-friendly. |
| `unregister_url(url)` | — | Remove the URL from the vector store and forget it. |
| `list_registered()` | — | List everything this server is tracking. |
| `crawl_site(seed_url, max_pages, max_depth, ...)` | ✓ | BFS crawl from a seed URL (same-FQDN scoped), returning markdown for every page reached. Respects robots.txt (protego; `Crawl-Delay` overrides `delay_ms` when declared), configurable depth/page limits and politeness delay. Supports `exclude_patterns` (regex list filtering CMS noise + images/static assets by default — **documents are no longer excluded**), `strip_tracking_params` (dedupes UTM variants), `use_sitemap` (seeds BFS from `/sitemap.xml`, sitemap-index aware), `render_mode` (`auto` renders SPA shells in headless Chromium), and `extract_docs` (default on: PDFs/Office files found while crawling are converted to Markdown; off skips fetching them). Emits MCP progress notifications per page (keep-alive on long crawls). |
| `server_health()` | — | Cheap status check — DB ok, registered count, last `resync_all` run. |

---

## 4. Quick start

### Docker

```bash
git clone https://github.com/ventz/scrape-website-mcp.git
cd scrape-website-mcp
cp .env.example .env
# edit .env: set MCP_BEARER_TOKEN (required); OPENAI_API_KEY is only needed for standalone use

docker build -t scrape-website-mcp .
docker run --rm -p 8000:8000 --shm-size=1g --env-file .env -v $(pwd)/data:/app/data scrape-website-mcp
```

> **Image size:** the image ships headless Chromium (`chromium-headless-shell`) for JS rendering plus the PDF/Office extraction stack — expect **~1.1–1.5 GB**. `--shm-size=1g` gives Chromium headroom (the default launch args already include `--disable-dev-shm-usage --no-sandbox` via `SCRAPER_IN_DOCKER=1`).

Build options:

```bash
# Track a branch
docker build --build-arg SCRAPE_WEBSITE_REF=main -t scrape-website-mcp .

# Pin to a tag or commit SHA
docker build --build-arg SCRAPE_WEBSITE_REF=v0.5.0 -t scrape-website-mcp .
```

### Local dev

```bash
make setup                                   # clones ventz/scrape-website into vendor/, uv sync, installs Chromium
cp .env.example .env                         # then edit
make run                                     # starts uvicorn on :8000 (sources .env first)
```

Refresh upstream scrape-website (this is how upstream improvements land):

```bash
make update-scraper                          # tracks $SCRAPE_WEBSITE_REF
make update-scraper SCRAPE_WEBSITE_REF=v0.5.0
```

Run tests:

```bash
make test
```

---

## 5. Wiring into the Harvard EA assistants platform

The platform has a **pre-built MCP catalog** entry for this server. In the agent admin:

1. Open the agent → **MCP Servers** tab (beta).
2. Click the **Scrape Website (self-hosted)** card.
3. Paste your server URL — **must end in `/mcp`** (e.g. `https://scraper.your-org.edu/mcp`) — and the `MCP_BEARER_TOKEN` you set in `.env`.
4. Save. The agent now sees the **Website Scraper** sub-tab on its Files page.

> **Why `/mcp`?** FastMCP's Streamable HTTP transport is mounted at `/mcp` by default in `mcp_server/server.py` (`mcp.http_app(path="/mcp")`). If you front this with nginx / Cloudflare / Cloud Run, make sure the `/mcp` path is reachable end-to-end — strip-prefix rules will break it.

In platform-driven mode the platform handles every upload, replace, and unregister against its own OpenAI key. This server is asked only for `fetch_url_as_markdown` — meaning it never touches OpenAI, and an `OPENAI_API_KEY` here is unused. See §6.

---

## 6. Auth model

Two secrets, two sides. Neither party ever holds both:

| Secret | Held by | Stored where |
|--------|---------|--------------|
| `MCP_BEARER_TOKEN` | The platform (or your MCP client) | Platform: AWS SSM under `/assistants/{agent_id}/mcp/scrape-website/bearer`. Standalone clients: their own config. |
| `OPENAI_API_KEY` | The platform (resolved per-agent), **OR** this server (standalone only) | Platform: same SSM chain chat uses. Standalone: this server's `.env`. |

The MCP server checks `Authorization: Bearer <MCP_BEARER_TOKEN>` on every request. In platform-driven mode the server is asked only to return markdown; the platform owns the OpenAI side and never hands its key over. Even if a user modifies this server to log requests, no OpenAI key ever crosses the boundary.

---

## 7. How "replace by URL" works

> Applies to standalone mode (`register_url` / `resync_url`). Platform-driven mode does this on the platform side, not here.

OpenAI Vector Store files support up to 16 `attributes` (key/value pairs, filterable). On upload we tag every file with:

```json
{
  "source_url":   "https://...",
  "content_hash": "sha256:...",
  "fetched_at":   "2026-05-11T...",
  "source_type":  "scraper",
  "source_name":  "Web Scraper"
}
```

On resync:

1. Scrape → hash → bail if hash unchanged.
2. Upload the new file → poll until `status="completed"` (don't delete the old until the new is indexed — avoids a retrieval gap).
3. `vector_stores.files.list(filter={"type":"eq","key":"source_url","value":url})` returns the old file.
4. `vector_stores.files.delete(...)` **then** `files.delete(...)` (both — the second reclaims org file quota).
5. Update our local SQLite map.

A per-URL `asyncio.Lock()` serializes concurrent `resync_url(same_url)` calls so we never double-upload.

---

## 8. State

SQLite at `$STATE_DIR/state.db` (default `./data/state.db`).

```sql
CREATE TABLE registered_urls (
    url TEXT PRIMARY KEY,
    vector_store_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    last_synced_at TEXT NOT NULL,
    -- diagnostic columns added on init via PRAGMA-guarded ALTER TABLE:
    last_content_change_at TEXT,
    last_status TEXT,           -- 'ok' | 'failed' | 'empty' | 'skipped'
    last_error TEXT,
    http_status INTEGER,
    content_bytes INTEGER,
    fetch_duration_ms INTEGER,
    page_title TEXT,
    etag TEXT,
    last_modified TEXT,
    registered_by TEXT,
    registered_at TEXT,
    retry_count INTEGER DEFAULT 0,
    robots_disallowed INTEGER DEFAULT 0
);
```

If you lose this file (e.g. forgot to mount the volume), you can recover by re-running `register_url` for each URL — the attribute filter will find any orphans in the VS and replace them cleanly.

Note: when this server runs in **platform-driven mode** the platform keeps its own per-(agent, url) state in its DB (`scraper_registrations` table). The SQLite store above is only exercised by this server's own OpenAI-side tools.

---

## 9. Configuration reference

All settings come from environment variables. `make run` and `docker run --env-file .env` both source `.env`; values there override the parent shell env.

| Variable | Required? | Default | Notes |
|----------|-----------|---------|-------|
| `MCP_BEARER_TOKEN` | **yes** | — | Bearer token clients must present. Generate with `openssl rand -hex 32`. |
| `OPENAI_API_KEY` | only for standalone use of OpenAI-side tools | — | Project-scoped key with `files:write` + `vector_stores:write`. **Not needed** in platform-driven mode. |
| `STATE_DIR` | no | `./data` | Where the SQLite state file lives. |
| `SCRAPE_WEBSITE_REF` | no | `main` | Git ref of `ventz/scrape-website` to pull in. Set at Docker build (`--build-arg`) or `make setup`/`make update-scraper`. |
| `SCRAPER_USER_AGENT` | no | upstream Chrome UA | User-Agent for outbound fetches. **0.2.0 policy change:** the default is now a real-Chrome UA (was an honest `scrape-website-mcp/0.1` bot UA) because the WAF-clearance tier replays cookies bound to a Chrome UA. Set this to restore the honest bot UA if you don't need WAF handling. |
| `SCRAPER_TIMEOUT` | no | `30` | Per-request timeout (seconds). |
| `SCRAPER_RENDER_MODE` | no | `auto` | Server-wide default for JS rendering: `auto` \| `always` \| `never`. Per-call `render_mode` overrides. |
| `SCRAPER_EXTRACT_DOCS` | no | `1` | Server-wide default for PDF/Office → Markdown extraction. Per-call `extract_docs` overrides. |
| `SCRAPER_MAX_FILE_SIZE` | no | `52428800` (50 MB) | Documents larger than this are not extracted (`status="failed"`). |
| `SCRAPER_BROWSER_ARGS` | no | (see notes) | Extra Chromium launch args (space-separated). Unset + `SCRAPER_IN_DOCKER=1` → `--disable-dev-shm-usage --no-sandbox`. |
| `SCRAPER_IN_DOCKER` | no | set in image | Enables the container-safe Chromium launch args above. |
| `SCRAPE_CF_COOKIES` | no | — | Path to an exported cookies file (JSON or Netscape) for WAF/Cloudflare clearance replay — the only headless-server-safe clearance source (`--human` is CLI/workstation-only). |
| `LOG_LEVEL` | no | `INFO` | Standard Python logging level. |

---

## 10. License

MIT — see [LICENSE](LICENSE).
