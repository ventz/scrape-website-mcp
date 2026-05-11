"""OpenAI vector-store I/O with replace-by-URL semantics.

The "replace" trick uses Vector Store File `attributes`: every uploaded file
is tagged with `source_url`. On resync we upload the new file first, wait for
indexing to complete, then look up old files by attribute filter and delete
them. This avoids a retrieval gap (the new file is searchable before the old
one disappears).

Important ordering: `vector_stores.files.delete` only detaches a file from
the store; the underlying File still counts against org file quota. Always
follow with `files.delete(file_id)` to fully reclaim.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
from datetime import datetime, timezone

from openai import AsyncOpenAI, NotFoundError, PermissionDeniedError, AuthenticationError

log = logging.getLogger(__name__)


class VectorStoreNotFoundError(RuntimeError):
    """Raised when the supplied vector_store_id doesn't exist *for this key*.

    Usually a project / org mismatch: the vector store was created under a
    different OpenAI project than the one our API key is scoped to. Carries
    a human-readable hint."""

    def __init__(self, vector_store_id: str):
        super().__init__(
            f"Vector store {vector_store_id!r} not found for the current OpenAI key. "
            f"This is almost always a project mismatch -- the vector store was "
            f"created under a different OpenAI project than the one your "
            f"OPENAI_API_KEY belongs to. Check that the MCP server is using "
            f"the same OpenAI key that owns this vector store."
        )
        self.vector_store_id = vector_store_id


def content_hash(markdown: str) -> str:
    return "sha256:" + hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def _filename_for(url: str) -> str:
    """Stable filename per URL — humans like to see this in the OpenAI dashboard."""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return f"page-{digest}.md"


async def _wait_until_indexed(
    client: AsyncOpenAI,
    vector_store_id: str,
    file_id: str,
    poll_interval: float = 1.0,
    timeout: float = 120.0,
) -> str:
    """Poll until the vector store file reaches a terminal state. Returns the final status."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        vsf = await client.vector_stores.files.retrieve(
            vector_store_id=vector_store_id, file_id=file_id
        )
        status = getattr(vsf, "status", None)
        if status in ("completed", "failed", "cancelled"):
            return status
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(
                f"Vector store file {file_id} did not finish indexing within {timeout}s "
                f"(last status: {status})"
            )
        await asyncio.sleep(poll_interval)


async def upload_markdown(
    client: AsyncOpenAI,
    vector_store_id: str,
    url: str,
    markdown: str,
    fetched_at: str | None = None,
) -> tuple[str, str]:
    """Upload markdown as a new File and attach to the vector store with
    `source_url`/`content_hash`/`fetched_at` attributes. Waits for indexing.

    Returns (file_id, status)."""
    h = content_hash(markdown)
    fetched_at = fetched_at or datetime.now(timezone.utc).isoformat()

    upload = await client.files.create(
        file=(_filename_for(url), io.BytesIO(markdown.encode("utf-8"))),
        purpose="assistants",
    )
    file_id = upload.id

    try:
        await client.vector_stores.files.create(
            vector_store_id=vector_store_id,
            file_id=file_id,
            # source_type/source_name match the conventional keys the assistants
            # platform's file list looks for to render a per-source icon.
            attributes={
                "source_url": url,
                "content_hash": h,
                "fetched_at": fetched_at,
                "source_type": "scraper",
                "source_name": "Web Scraper",
            },
        )
    except NotFoundError as e:
        # The vector store doesn't exist for this key/project. Roll back the
        # orphaned upload so we don't leak the user's file quota, then surface
        # the actionable error.
        try:
            await client.files.delete(file_id)
        except Exception:  # noqa: BLE001
            pass
        raise VectorStoreNotFoundError(vector_store_id) from e
    except (PermissionDeniedError, AuthenticationError) as e:
        try:
            await client.files.delete(file_id)
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(
            f"OpenAI rejected vector_stores.files.create: {e}. Check that "
            f"OPENAI_API_KEY has files:write and vector_stores:write scopes."
        ) from e

    status = await _wait_until_indexed(client, vector_store_id, file_id)
    if status != "completed":
        log.warning(
            "Vector store file %s reached terminal status %s (not 'completed')",
            file_id, status,
        )
    return file_id, status


async def find_existing_file_ids(
    client: AsyncOpenAI, vector_store_id: str, url: str
) -> list[str]:
    """Return all file_ids in this vector store whose `source_url` attribute matches `url`."""
    listing = await client.vector_stores.files.list(
        vector_store_id=vector_store_id,
        filter={"type": "eq", "key": "source_url", "value": url},
    )
    return [f.id for f in getattr(listing, "data", [])]


async def delete_file_completely(
    client: AsyncOpenAI, vector_store_id: str, file_id: str
) -> None:
    """Detach from vector store AND delete the underlying File object."""
    try:
        await client.vector_stores.files.delete(
            vector_store_id=vector_store_id, file_id=file_id
        )
    except Exception as e:
        log.warning("vector_stores.files.delete(%s, %s) failed: %s", vector_store_id, file_id, e)
    try:
        await client.files.delete(file_id)
    except Exception as e:
        log.warning("files.delete(%s) failed: %s", file_id, e)


async def replace_url_in_vector_store(
    client: AsyncOpenAI,
    vector_store_id: str,
    url: str,
    markdown: str,
) -> tuple[str, str, list[str]]:
    """Upload the new file, wait for indexing, then delete any prior files
    for this `source_url`. Returns (new_file_id, status, deleted_old_file_ids)."""
    new_file_id, status = await upload_markdown(
        client, vector_store_id, url, markdown
    )

    old_ids = await find_existing_file_ids(client, vector_store_id, url)
    old_ids = [fid for fid in old_ids if fid != new_file_id]

    for fid in old_ids:
        await delete_file_completely(client, vector_store_id, fid)

    return new_file_id, status, old_ids
