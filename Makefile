SCRAPE_WEBSITE_REF ?= main
VENDOR_DIR := vendor/scrape-website

.PHONY: setup update-scraper run test docker-build docker-run lint clean

setup:
	@if [ ! -d $(VENDOR_DIR)/.git ]; then \
		echo "Cloning ventz/scrape-website@$(SCRAPE_WEBSITE_REF) into $(VENDOR_DIR)"; \
		git clone --depth 1 --branch $(SCRAPE_WEBSITE_REF) \
			https://github.com/ventz/scrape-website.git $(VENDOR_DIR); \
	else \
		echo "$(VENDOR_DIR) already present; run 'make update-scraper' to refresh."; \
	fi
	uv sync

update-scraper:
	@if [ ! -d $(VENDOR_DIR)/.git ]; then \
		$(MAKE) setup; \
	else \
		cd $(VENDOR_DIR) && git fetch --depth 1 origin $(SCRAPE_WEBSITE_REF) \
			&& git checkout FETCH_HEAD; \
	fi

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
	docker run --rm -p 8000:8000 \
		--env-file .env \
		-v $$(pwd)/data:/app/data \
		scrape-website-mcp

clean:
	rm -rf vendor data .venv __pycache__ mcp_server/__pycache__ mcp_server/tests/__pycache__
