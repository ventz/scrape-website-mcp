FROM python:3.13-slim

ARG SCRAPE_WEBSITE_REF=main

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch ${SCRAPE_WEBSITE_REF} \
    https://github.com/ventz/scrape-website.git /opt/scrape-website
ENV PYTHONPATH=/opt/scrape-website

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml ./
RUN uv sync --no-dev

COPY mcp_server /app/mcp_server

ENV STATE_DIR=/app/data
RUN mkdir -p /app/data

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "mcp_server.server:app", "--host", "0.0.0.0", "--port", "8000"]
