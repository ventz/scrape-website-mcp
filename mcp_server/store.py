"""SQLite-backed mapping of registered URLs to OpenAI file IDs, plus rich
per-URL diagnostic state.

State directory comes from $STATE_DIR (default ./data).

Migration: `_migrate()` runs on every Store init. It is idempotent — uses
`PRAGMA table_info` to discover which columns already exist before issuing
`ALTER TABLE ... ADD COLUMN ...`. SQLite < 3.35 doesn't support
`ADD COLUMN IF NOT EXISTS`, so we check first.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Registration:
    url: str
    vector_store_id: str
    file_id: str
    content_hash: str
    last_synced_at: str
    # Increment-2 diagnostic fields (all nullable / defaulted)
    last_content_change_at: str | None = None
    last_status: str | None = None  # 'ok' | 'failed' | 'skipped' | 'empty'
    last_error: str | None = None
    http_status: int | None = None
    content_bytes: int | None = None
    fetch_duration_ms: int | None = None
    page_title: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    registered_by: str | None = None
    registered_at: str | None = None
    retry_count: int = 0
    robots_disallowed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Column name → SQL type (excluding the core 5 set by initial CREATE TABLE).
_INCREMENT_2_COLUMNS: list[tuple[str, str]] = [
    ("last_content_change_at", "TEXT"),
    ("last_status", "TEXT"),
    ("last_error", "TEXT"),
    ("http_status", "INTEGER"),
    ("content_bytes", "INTEGER"),
    ("fetch_duration_ms", "INTEGER"),
    ("page_title", "TEXT"),
    ("etag", "TEXT"),
    ("last_modified", "TEXT"),
    ("registered_by", "TEXT"),
    ("registered_at", "TEXT"),
    ("retry_count", "INTEGER DEFAULT 0"),
    ("robots_disallowed", "INTEGER DEFAULT 0"),
]

_ALL_COLUMNS = [
    "url", "vector_store_id", "file_id", "content_hash", "last_synced_at",
    *[c for c, _ in _INCREMENT_2_COLUMNS],
]


class Store:
    def __init__(self, db_path: Path | None = None):
        if db_path is None:
            state_dir = Path(os.environ.get("STATE_DIR", "./data")).resolve()
            state_dir.mkdir(parents=True, exist_ok=True)
            db_path = state_dir / "state.db"
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS registered_urls (
                url TEXT PRIMARY KEY,
                vector_store_id TEXT NOT NULL,
                file_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                last_synced_at TEXT NOT NULL
            )
            """
        )
        self._migrate()

    def _existing_columns(self) -> set[str]:
        rows = self._conn.execute("PRAGMA table_info(registered_urls)").fetchall()
        return {r[1] for r in rows}

    def _migrate(self) -> None:
        """Idempotently add any missing Increment-2 columns."""
        existing = self._existing_columns()
        for col, type_decl in _INCREMENT_2_COLUMNS:
            if col not in existing:
                self._conn.execute(
                    f"ALTER TABLE registered_urls ADD COLUMN {col} {type_decl}"
                )

    def _row_to_registration(self, row: tuple) -> Registration:
        return Registration(**dict(zip(_ALL_COLUMNS, row)))

    def get(self, url: str) -> Registration | None:
        cols = ", ".join(_ALL_COLUMNS)
        with self._lock:
            row = self._conn.execute(
                f"SELECT {cols} FROM registered_urls WHERE url = ?", (url,)
            ).fetchone()
        return self._row_to_registration(row) if row else None

    def upsert(
        self,
        url: str,
        vector_store_id: str,
        file_id: str,
        content_hash: str,
        *,
        last_synced_at: str | None = None,
        last_content_change_at: str | None = None,
        last_status: str | None = None,
        last_error: str | None = None,
        http_status: int | None = None,
        content_bytes: int | None = None,
        fetch_duration_ms: int | None = None,
        page_title: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        registered_by: str | None = None,
        retry_count: int | None = None,
        robots_disallowed: int | None = None,
    ) -> Registration:
        """Insert-or-update. Unset kwargs preserve prior values on UPDATE
        (we use COALESCE in the SET clause). On INSERT, unset kwargs are NULL/0.

        `last_synced_at` is always touched (defaults to now).
        `registered_at` is set only on INSERT (preserved on UPDATE).
        """
        now = _utcnow_iso()
        last_synced_at = last_synced_at or now

        # Read prior row so we can preserve registered_at on update and
        # know whether this is a new row (for retry_count default).
        prior = self.get(url)
        registered_at = prior.registered_at if prior and prior.registered_at else now

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO registered_urls (
                    url, vector_store_id, file_id, content_hash, last_synced_at,
                    last_content_change_at, last_status, last_error, http_status,
                    content_bytes, fetch_duration_ms, page_title, etag, last_modified,
                    registered_by, registered_at, retry_count, robots_disallowed
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    vector_store_id        = excluded.vector_store_id,
                    file_id                = excluded.file_id,
                    content_hash           = excluded.content_hash,
                    last_synced_at         = excluded.last_synced_at,
                    last_content_change_at = COALESCE(excluded.last_content_change_at, registered_urls.last_content_change_at),
                    last_status            = COALESCE(excluded.last_status, registered_urls.last_status),
                    last_error             = excluded.last_error,  -- explicit clear allowed
                    http_status            = COALESCE(excluded.http_status, registered_urls.http_status),
                    content_bytes          = COALESCE(excluded.content_bytes, registered_urls.content_bytes),
                    fetch_duration_ms      = COALESCE(excluded.fetch_duration_ms, registered_urls.fetch_duration_ms),
                    page_title             = COALESCE(excluded.page_title, registered_urls.page_title),
                    etag                   = COALESCE(excluded.etag, registered_urls.etag),
                    last_modified          = COALESCE(excluded.last_modified, registered_urls.last_modified),
                    registered_by          = COALESCE(excluded.registered_by, registered_urls.registered_by),
                    retry_count            = COALESCE(excluded.retry_count, registered_urls.retry_count),
                    robots_disallowed      = COALESCE(excluded.robots_disallowed, registered_urls.robots_disallowed)
                """,
                (
                    url, vector_store_id, file_id, content_hash, last_synced_at,
                    last_content_change_at, last_status, last_error, http_status,
                    content_bytes, fetch_duration_ms, page_title, etag, last_modified,
                    registered_by, registered_at,
                    retry_count if retry_count is not None else 0,
                    robots_disallowed if robots_disallowed is not None else 0,
                ),
            )
        return self.get(url)  # type: ignore[return-value]

    def record_failure(
        self,
        url: str,
        *,
        error: str,
        http_status: int | None = None,
        vector_store_id: str | None = None,
    ) -> None:
        """Persist a failed attempt for `url`. UPSERT semantics:
        - existing row: updates last_status/error/http_status, bumps retry_count
        - new row: inserts with sentinel empty file_id/content_hash so the
          attempt is visible in `list_all()` and retry-able via `resync_url`
          (which needs a vector_store_id to know where to ultimately upload).

        Passing `vector_store_id` only matters for first-time-failed URLs —
        existing rows already carry one.
        """
        prior = self.get(url)
        now = _utcnow_iso()
        error_trunc = (error or "")[:240]

        if prior is None:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT INTO registered_urls (
                        url, vector_store_id, file_id, content_hash, last_synced_at,
                        last_status, last_error, http_status, registered_at,
                        retry_count
                    )
                    VALUES (?, ?, '', '', ?, 'failed', ?, ?, ?, 1)
                    """,
                    (url, vector_store_id or "", now, error_trunc, http_status, now),
                )
            return

        with self._lock:
            self._conn.execute(
                """
                UPDATE registered_urls
                SET last_synced_at = ?, last_status = 'failed',
                    last_error = ?, http_status = COALESCE(?, http_status),
                    retry_count = retry_count + 1
                WHERE url = ?
                """,
                (now, error_trunc, http_status, url),
            )

    def delete(self, url: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM registered_urls WHERE url = ?", (url,))
        return cur.rowcount > 0

    def list_all(self) -> list[Registration]:
        cols = ", ".join(_ALL_COLUMNS)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {cols} FROM registered_urls ORDER BY url"
            ).fetchall()
        return [self._row_to_registration(r) for r in rows]

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM registered_urls"
            ).fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()
