"""Verify the Store migration is idempotent and the new columns work."""

from __future__ import annotations

import sqlite3

from mcp_server.store import Store


def test_migration_adds_all_columns(tmp_path):
    s = Store(db_path=tmp_path / "fresh.db")
    cols = s._existing_columns()
    expected = {
        "url", "vector_store_id", "file_id", "content_hash", "last_synced_at",
        "last_content_change_at", "last_status", "last_error", "http_status",
        "content_bytes", "fetch_duration_ms", "page_title", "etag",
        "last_modified", "registered_by", "registered_at", "retry_count",
        "robots_disallowed",
    }
    assert expected <= cols
    s.close()


def test_migration_is_idempotent(tmp_path):
    db = tmp_path / "twice.db"
    s1 = Store(db_path=db)
    s1.close()
    # Re-open: must not raise, must not drop data.
    s2 = Store(db_path=db)
    cols = s2._existing_columns()
    assert "last_content_change_at" in cols
    s2.close()


def test_migration_upgrades_legacy_db(tmp_path):
    """Simulate a v1 DB (5 columns) and confirm we upgrade in place."""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE registered_urls (
            url TEXT PRIMARY KEY,
            vector_store_id TEXT NOT NULL,
            file_id TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            last_synced_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO registered_urls VALUES (?, ?, ?, ?, ?)",
        ("https://a", "vs_1", "file_1", "sha256:x", "2026-01-01T00:00:00Z"),
    )
    conn.commit()
    conn.close()

    s = Store(db_path=db)
    cols = s._existing_columns()
    assert "page_title" in cols
    assert "last_status" in cols

    reg = s.get("https://a")
    assert reg is not None
    assert reg.url == "https://a"
    assert reg.page_title is None
    assert reg.last_status is None
    s.close()


def test_upsert_populates_new_fields(tmp_path):
    s = Store(db_path=tmp_path / "u.db")
    reg = s.upsert(
        "https://x", "vs_1", "file_1", "sha256:abc",
        last_status="ok", http_status=200, content_bytes=1024,
        page_title="Hello",
    )
    assert reg.last_status == "ok"
    assert reg.http_status == 200
    assert reg.page_title == "Hello"
    assert reg.registered_at is not None
    s.close()


def test_upsert_preserves_registered_at_on_update(tmp_path):
    s = Store(db_path=tmp_path / "u2.db")
    first = s.upsert("https://x", "vs_1", "f1", "h1")
    second = s.upsert("https://x", "vs_1", "f2", "h2")
    assert first.registered_at == second.registered_at
    s.close()


def test_record_failure_bumps_retry(tmp_path):
    s = Store(db_path=tmp_path / "f.db")
    s.upsert("https://x", "vs_1", "f1", "h1", last_status="ok")
    s.record_failure("https://x", error="boom", http_status=500)
    s.record_failure("https://x", error="boom2", http_status=502)
    reg = s.get("https://x")
    assert reg.last_status == "failed"
    assert reg.last_error == "boom2"
    assert reg.http_status == 502
    assert reg.retry_count == 2
    s.close()
