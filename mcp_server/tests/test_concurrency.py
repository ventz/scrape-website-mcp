"""Verify per-URL serialization so concurrent resync_url calls on the same
URL don't double-upload.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from mcp_server import server as srv


@pytest.fixture(autouse=True)
def _reset_store(tmp_path, monkeypatch):
    """Each test gets a fresh SQLite store."""
    from mcp_server.store import Store
    monkeypatch.setattr(srv, "_store", Store(db_path=tmp_path / "test.db"))
    # also reset the lock map so each test starts clean
    srv._per_url_locks.clear()
    yield


async def test_lock_serializes_same_url():
    """Two coroutines calling _lock_for(same_url) must not both be inside the
    critical section at the same time."""
    url = "https://example.com/x"
    inside = 0
    max_inside = 0

    async def critical():
        nonlocal inside, max_inside
        async with srv._lock_for(url):
            inside += 1
            max_inside = max(max_inside, inside)
            await asyncio.sleep(0.05)
            inside -= 1

    await asyncio.gather(*(critical() for _ in range(5)))
    assert max_inside == 1


async def test_lock_distinct_urls_run_in_parallel():
    """Different URLs should not contend for each other's lock."""
    inside = 0
    max_inside = 0

    async def critical(u: str):
        nonlocal inside, max_inside
        async with srv._lock_for(u):
            inside += 1
            max_inside = max(max_inside, inside)
            await asyncio.sleep(0.05)
            inside -= 1

    urls = [f"https://example.com/{i}" for i in range(4)]
    await asyncio.gather(*(critical(u) for u in urls))
    assert max_inside >= 2  # at least some parallelism
