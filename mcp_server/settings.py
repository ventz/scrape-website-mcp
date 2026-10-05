"""Env-driven server knobs and caps.

Every value is read at call time (not import time) so tests and operators can
change env without a restart of the module. Defaults keep 0.2.0 behavior;
caps exist so a direct MCP client can't push the server past what the
operator allows. Caller values outside a cap are clamped and reported in a
``warnings`` list rather than rejected.
"""

from __future__ import annotations

import os

_FALSY = ("0", "false", "no", "off", "")


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in _FALSY


# -- engine limits (FetchEngine kwargs) ------------------------------------

def timeout_s() -> int:
    return env_int("SCRAPER_TIMEOUT", 30)


def max_timeout_s() -> int:
    return max(1, env_int("SCRAPER_MAX_TIMEOUT_S", 120))


def max_retries() -> int:
    return max(1, env_int("SCRAPER_MAX_RETRIES", 3))


def max_page_size() -> int:
    return env_int("SCRAPER_MAX_PAGE_SIZE", 50 * 1024 * 1024)


def render_timeout() -> int:
    return env_int("SCRAPER_RENDER_TIMEOUT", 30)


def render_settle_ms() -> int:
    return env_int("SCRAPER_RENDER_SETTLE_MS", 3000)


MAX_RENDER_SETTLE_MS = 15000


def render_concurrency() -> int:
    return max(1, env_int("SCRAPER_RENDER_CONCURRENCY", 4))


# -- unsafe toggles (off unless the operator opts in) -----------------------

def allow_insecure_tls() -> bool:
    return env_bool("SCRAPER_ALLOW_INSECURE_TLS", False)


def allow_private_targets() -> bool:
    return env_bool("SCRAPER_ALLOW_PRIVATE_TARGETS", False)


# -- crawl / batch caps -----------------------------------------------------

def max_crawl_concurrency() -> int:
    return max(1, env_int("SCRAPER_MAX_CRAWL_CONCURRENCY", 8))


def min_delay_ms() -> int:
    return max(0, env_int("SCRAPER_MIN_DELAY_MS", 0))


def max_batch_urls() -> int:
    return max(1, env_int("SCRAPER_MAX_BATCH_URLS", 100))


def max_inline_html_bytes() -> int:
    return env_int("SCRAPER_MAX_INLINE_HTML_BYTES", 2 * 1024 * 1024)


def max_inline_doc_bytes() -> int:
    return env_int("SCRAPER_MAX_INLINE_DOC_BYTES", 10 * 1024 * 1024)


def max_response_bytes() -> int | None:
    """Crawl-wide cap on returned bytes. Unset means no server cap (0.2.0
    behavior); when set, it also applies to calls that pass no
    ``max_total_bytes``."""
    raw = os.environ.get("SCRAPER_MAX_RESPONSE_BYTES")
    if raw is None or not raw.strip():
        return None
    return env_int("SCRAPER_MAX_RESPONSE_BYTES", 50 * 1024 * 1024)


DEFAULT_MAX_RESPONSE_BYTES = 50 * 1024 * 1024

# Pattern caps for exclude/include regex lists (same caps the platform uses).
MAX_PATTERNS = 100
MAX_PATTERN_LEN = 200
MAX_ADDITIONAL_SEEDS = 1000


def clamp(name: str, value: int, lo: int, hi: int,
          warnings: list[str]) -> int:
    """Clamp *value* to [lo, hi], recording a warning when it moved."""
    clamped = min(max(value, lo), hi)
    if clamped != value:
        warnings.append(f"{name}={value} clamped to {clamped} (server limit)")
    return clamped
