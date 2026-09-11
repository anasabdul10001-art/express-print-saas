"""
scripts/backup_database.py - runs standalone, outside the FastAPI app, so
it's tested directly rather than through the API. dump_database() is
exercised against a real local Postgres (same one tests/conftest.py already
points DATABASE_URL at); the Supabase Storage calls are mocked, since this
environment doesn't have real Supabase credentials to test against - the
upload/list/delete HTTP shape mirrors the same pattern already proven
working in app/routers/uploads.py's product-image upload.
"""

import gzip
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from scripts import backup_database


def test_dump_database_produces_valid_gzipped_sql():
    """Real pg_dump call against the test database - no mocking. Confirms
    the actual subprocess invocation and flags work, not just the Python
    logic around it."""
    compressed = backup_database.dump_database(os.environ["DATABASE_URL"])
    sql = gzip.decompress(compressed).decode()
    assert "PostgreSQL database dump" in sql
    # Confirms --no-owner worked - a raw dump would include "OWNER TO testapp" lines.
    assert "OWNER TO" not in sql


def test_dump_database_raises_on_bad_connection_string():
    with pytest.raises(RuntimeError, match="pg_dump failed"):
        backup_database.dump_database("postgresql://nobody:nothing@localhost:1/doesnotexist")


def test_ensure_bucket_exists_tolerates_already_exists():
    with patch("scripts.backup_database.httpx.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=400, text="Bucket already exists")
        backup_database.ensure_bucket_exists("https://x.supabase.co", "key")  # must not raise


def test_ensure_bucket_exists_raises_on_real_error():
    with patch("scripts.backup_database.httpx.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=500, text="internal error")
        with pytest.raises(RuntimeError):
            backup_database.ensure_bucket_exists("https://x.supabase.co", "key")


def test_upload_backup_sends_correct_request():
    with patch("scripts.backup_database.httpx.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)
        backup_database.upload_backup("https://x.supabase.co", "key", "backup-test.sql.gz", b"data")

        args, kwargs = mock_post.call_args
        assert args[0] == "https://x.supabase.co/storage/v1/object/db-backups/backup-test.sql.gz"
        assert kwargs["headers"]["Authorization"] == "Bearer key"
        assert kwargs["content"] == b"data"


def test_upload_backup_raises_on_failure():
    with patch("scripts.backup_database.httpx.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=403, text="forbidden")
        with pytest.raises(RuntimeError):
            backup_database.upload_backup("https://x.supabase.co", "key", "f.sql.gz", b"data")


def test_prune_deletes_only_backups_older_than_retention():
    now = datetime.now(timezone.utc)
    old_name = f"backup-{(now - timedelta(days=45)).strftime('%Y-%m-%dT%H-%M-%SZ')}.sql.gz"
    recent_name = f"backup-{(now - timedelta(days=5)).strftime('%Y-%m-%dT%H-%M-%SZ')}.sql.gz"

    with patch("scripts.backup_database.httpx.post") as mock_post, \
         patch("scripts.backup_database.httpx.delete") as mock_delete:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: [{"name": old_name}, {"name": recent_name}, {"name": "not-a-backup-file.txt"}],
            raise_for_status=lambda: None,
        )
        mock_delete.return_value = MagicMock(status_code=200)

        backup_database.prune_old_backups("https://x.supabase.co", "key", retention_days=30)

        deleted_urls = [call.args[0] for call in mock_delete.call_args_list]
        assert any(old_name in url for url in deleted_urls)
        assert not any(recent_name in url for url in deleted_urls)
        assert len(deleted_urls) == 1  # the non-backup file was skipped, not deleted


def test_prune_never_raises_even_if_listing_fails():
    with patch("scripts.backup_database.httpx.post", side_effect=RuntimeError("network down")):
        backup_database.prune_old_backups("https://x.supabase.co", "key", retention_days=30)  # must not raise
