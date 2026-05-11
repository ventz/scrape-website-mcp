"""Unit tests for the OpenAI vector-store replace-by-URL flow.

The OpenAI client is mocked. We verify:
  - upload_markdown sends correct `attributes` (source_url, content_hash, fetched_at).
  - replace_url_in_vector_store uploads BEFORE deleting old (no retrieval gap).
  - delete uses BOTH vector_stores.files.delete AND files.delete (no file-quota leak).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from mcp_server import openai_sync


def _mock_client(existing_file_ids: list[str] | None = None,
                 retrieve_status: str = "completed") -> MagicMock:
    """Build a mock AsyncOpenAI client that satisfies the calls openai_sync makes."""
    client = MagicMock()
    client.files = MagicMock()
    client.files.create = AsyncMock(return_value=SimpleNamespace(id="file_new"))
    client.files.delete = AsyncMock()

    client.vector_stores = MagicMock()
    client.vector_stores.files = MagicMock()
    client.vector_stores.files.create = AsyncMock()
    client.vector_stores.files.retrieve = AsyncMock(
        return_value=SimpleNamespace(status=retrieve_status)
    )
    client.vector_stores.files.delete = AsyncMock()
    listing = SimpleNamespace(data=[SimpleNamespace(id=fid) for fid in (existing_file_ids or [])])
    client.vector_stores.files.list = AsyncMock(return_value=listing)
    return client


async def test_upload_markdown_sends_attributes():
    client = _mock_client()
    file_id, status = await openai_sync.upload_markdown(
        client, "vs_abc", "https://example.com/page", "# hello\n", fetched_at="2026-01-01T00:00:00Z"
    )
    assert file_id == "file_new"
    assert status == "completed"

    args, kwargs = client.vector_stores.files.create.call_args
    assert kwargs["vector_store_id"] == "vs_abc"
    assert kwargs["file_id"] == "file_new"
    attrs = kwargs["attributes"]
    assert attrs["source_url"] == "https://example.com/page"
    assert attrs["content_hash"].startswith("sha256:")
    assert attrs["fetched_at"] == "2026-01-01T00:00:00Z"
    # source_type/source_name let the platform's file list show a globe icon.
    assert attrs["source_type"] == "scraper"
    assert attrs["source_name"] == "Web Scraper"


async def test_replace_uploads_before_deleting_old():
    client = _mock_client(existing_file_ids=["file_old_1", "file_old_2"])

    call_order: list[str] = []
    orig_vsf_create = client.vector_stores.files.create
    orig_vsf_delete = client.vector_stores.files.delete

    async def track_create(*a, **kw):
        call_order.append("vs_files.create")
        return await orig_vsf_create(*a, **kw)

    async def track_delete(*a, **kw):
        call_order.append("vs_files.delete")
        return await orig_vsf_delete(*a, **kw)

    client.vector_stores.files.create = track_create
    client.vector_stores.files.delete = track_delete

    new_id, status, deleted = await openai_sync.replace_url_in_vector_store(
        client, "vs_abc", "https://example.com/page", "new content"
    )

    assert new_id == "file_new"
    assert status == "completed"
    assert set(deleted) == {"file_old_1", "file_old_2"}
    # Upload (vs_files.create) must precede every delete
    first_delete = call_order.index("vs_files.delete")
    first_upload = call_order.index("vs_files.create")
    assert first_upload < first_delete


async def test_replace_deletes_both_vsf_and_files():
    client = _mock_client(existing_file_ids=["file_old_1"])
    await openai_sync.replace_url_in_vector_store(
        client, "vs_abc", "https://example.com/page", "new content"
    )
    # Detached from vector store
    client.vector_stores.files.delete.assert_any_call(
        vector_store_id="vs_abc", file_id="file_old_1"
    )
    # AND underlying File deleted
    client.files.delete.assert_any_call("file_old_1")


async def test_replace_does_not_delete_the_new_file_even_if_listed():
    """If the listing returns the new file_id (race), we must skip it."""
    client = _mock_client(existing_file_ids=["file_new", "file_old"])
    new_id, _, deleted = await openai_sync.replace_url_in_vector_store(
        client, "vs_abc", "https://example.com/page", "new content"
    )
    assert new_id == "file_new"
    assert "file_new" not in deleted
    assert deleted == ["file_old"]


async def test_find_existing_uses_eq_attribute_filter():
    client = _mock_client(existing_file_ids=["a", "b"])
    ids = await openai_sync.find_existing_file_ids(client, "vs_abc", "https://example.com/p")
    assert ids == ["a", "b"]
    args, kwargs = client.vector_stores.files.list.call_args
    assert kwargs["vector_store_id"] == "vs_abc"
    assert kwargs["filter"] == {"type": "eq", "key": "source_url", "value": "https://example.com/p"}


async def test_content_hash_stable():
    h1 = openai_sync.content_hash("hello")
    h2 = openai_sync.content_hash("hello")
    h3 = openai_sync.content_hash("hello ")
    assert h1 == h2
    assert h1 != h3
    assert h1.startswith("sha256:")
