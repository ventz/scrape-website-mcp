FROM python:3.13-slim

# Pin of ventz/scrape-website (branch or tag) — the shared fetch/crawl engine,
# installed as an editable uv path dependency from vendor/ (see
# [tool.uv.sources] in pyproject.toml).
ARG SCRAPE_WEBSITE_REF=feat/mcp-web-scraper
ARG SCRAPE_WEBSITE_REPO=https://github.com/ventz/scrape-website.git

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

RUN git clone --depth 1 --branch ${SCRAPE_WEBSITE_REF} \
    ${SCRAPE_WEBSITE_REPO} /app/vendor/scrape-website

COPY pyproject.toml ./
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
