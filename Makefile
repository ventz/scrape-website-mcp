# Pin of ventz/scrape-website (branch, tag, or full SHA). The engine is installed
# as an editable uv path dependency from $(VENDOR_DIR) — see [tool.uv.sources].
# Current pin: 6d701c6 = 0.7.3, the robots.txt/sitemap.xml curl_cffi WAF
# fallback release. On origin/main as of 2026-10-05; no v0.7.3 tag yet
# (latest tag is v0.7.2). Switch SCRAPE_WEBSITE_REF to the tag once cut.
# Use the full SHA — short SHAs fail with `git fetch --depth 1 <ref>`.
SCRAPE_WEBSITE_REF ?= 6d701c69555edf71687c989f3615c93c2520d369
SCRAPE_WEBSITE_REPO ?= https://github.com/ventz/scrape-website.git
VENDOR_DIR := vendor/scrape-website

.PHONY: setup update-scraper run test docker-build docker-run lint clean

setup:
	@if [ ! -d $(VENDOR_DIR)/.git ]; then \
		echo "Cloning scrape-website@$(SCRAPE_WEBSITE_REF) into $(VENDOR_DIR)"; \
		git init -q $(VENDOR_DIR) \
			&& git -C $(VENDOR_DIR) fetch --depth 1 $(SCRAPE_WEBSITE_REPO) $(SCRAPE_WEBSITE_REF) \
			&& git -C $(VENDOR_DIR) reset --hard FETCH_HEAD; \
	else \
		echo "$(VENDOR_DIR) already present; run 'make update-scraper' to refresh."; \
	fi
	uv sync
	@# Chromium for the JS-render escalation tier (headless shell = smaller).
	uv run playwright install chromium-headless-shell

update-scraper:
	@if [ ! -d $(VENDOR_DIR)/.git ]; then \
		$(MAKE) setup; \
	else \
		cd $(VENDOR_DIR) && git fetch --depth 1 $(SCRAPE_WEBSITE_REPO) $(SCRAPE_WEBSITE_REF) \
			&& git reset --hard FETCH_HEAD; \
	fi
	@# Why `reset --hard FETCH_HEAD` instead of `checkout FETCH_HEAD`:
	@# the latter leaves the vendor repo in detached-HEAD state and prints
	@# a "leaving N commits behind" warning every time upstream advances
	@# (the shallow clone can't see the parent chain so git thinks the
	@# old tip is unreferenced). `reset --hard` produces the same working
	@# tree quietly. Either way the vendored engine is read-only — we never
	@# branch / commit / push from here. Fetching by ref (not `clone --branch`)
	@# is what lets the pin be a commit SHA; a SHA must be given in full.
	uv lock -P scrape-website
	uv sync

run:
	@# Load .env when present so users don't have to `source` it manually.
	@# `set -a` exports every var the dot-include defines; the parent shell's
	@# env survives for anything .env doesn't set (e.g. OPENAI_API_KEY).
	@set -a; \
	[ -f .env ] && . ./.env; \
	set +a; \
	if [ -z "$$OPENAI_API_KEY" ]; then \
		echo "INFO: OPENAI_API_KEY not set. fetch_url_as_markdown will still work (used by the assistants platform). The MCP server's own OpenAI-side tools (register_url etc.) will fail."; \
	fi; \
	if [ -z "$$MCP_BEARER_TOKEN" ]; then \
		echo "ERROR: MCP_BEARER_TOKEN not set. Auth middleware will reject all requests with 503."; \
	fi; \
	uv run uvicorn mcp_server.server:app --host 0.0.0.0 --port 8000

test:
	uv run pytest -q

docker-build:
	docker build --build-arg SCRAPE_WEBSITE_REF=$(SCRAPE_WEBSITE_REF) -t scrape-website-mcp .

docker-run:
	@# --shm-size: Chromium is happier with real /dev/shm even though the
	@# default launch args include --disable-dev-shm-usage.
	docker run --rm -p 8000:8000 \
		--shm-size=1g \
		--env-file .env \
		-v $$(pwd)/data:/app/data \
		scrape-website-mcp

clean:
	rm -rf vendor data .venv __pycache__ mcp_server/__pycache__ mcp_server/tests/__pycache__
