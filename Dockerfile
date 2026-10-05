FROM python:3.13-slim

# Pin of ventz/scrape-website (branch, tag, or full commit SHA) — the shared
# fetch/crawl engine, installed as an editable uv path dependency from vendor/
# (see [tool.uv.sources] in pyproject.toml). 6d701c6 = 0.7.3 (robots/sitemap
# curl_cffi fallback); until it is on origin, build with vendor/ in the context.
ARG SCRAPE_WEBSITE_REF=6d701c69555edf71687c989f3615c93c2520d369
ARG SCRAPE_WEBSITE_REPO=https://github.com/ventz/scrape-website.git

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

# Engine source: prefer a vendor/ checkout in the build context (from
# `make setup` — lets you build unpushed branches / local changes); fall back
# to cloning ${SCRAPE_WEBSITE_REF}.
COPY pyproject.toml ./
COPY vendo[r] /app/vendor
RUN [ -d /app/vendor/scrape-website ] || ( \
    git init -q /app/vendor/scrape-website \
    && git -C /app/vendor/scrape-website fetch --depth 1 ${SCRAPE_WEBSITE_REPO} ${SCRAPE_WEBSITE_REF} \
    && git -C /app/vendor/scrape-website reset --hard FETCH_HEAD )
RUN uv sync --no-dev

# Chromium for the JS-render escalation tier. chromium-headless-shell is the
# headless-only build (~150-200MB smaller than full Chromium); --with-deps
# pulls the OS libraries it needs. NOTE: this makes the image ~1.1-1.5GB —
# the price of real JS rendering.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN uv run playwright install --with-deps chromium-headless-shell \
 && rm -rf /var/lib/apt/lists/*

COPY mcp_server /app/mcp_server

ENV STATE_DIR=/app/data
# Container Chromium defaults: --disable-dev-shm-usage --no-sandbox
# (run with --shm-size=1g for extra headroom; see Makefile docker-run).
ENV SCRAPER_IN_DOCKER=1
RUN mkdir -p /app/data

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "mcp_server.server:app", "--host", "0.0.0.0", "--port", "8000"]
