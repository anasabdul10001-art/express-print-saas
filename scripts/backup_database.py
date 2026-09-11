"""
Daily database backup: dumps the production Postgres database (via pg_dump)
and uploads the gzip-compressed dump to a PRIVATE Supabase Storage bucket.
Run by .github/workflows/db-backup.yml on a schedule - see that file for
which secrets it needs.

This is a supplement to whatever backup your hosting provider offers, not a
replacement for checking that Render's own Postgres backups are actually
enabled for your plan (see the README section this script's PR added) -
free-tier Render Postgres databases have no backups at all and can even be
deleted after a period of inactivity, so don't treat this script alone as
sufficient without confirming that.

Standalone script (not part of the FastAPI app) - reads its own env vars
directly rather than importing app.config, so it never accidentally
requires unrelated app secrets (SECRET_KEY, DHL_API_KEY, ...) just to make
a database dump.

Usage: python scripts/backup_database.py
Required env vars: DATABASE_URL, SUPABASE_URL, SUPABASE_SERVICE_KEY
Optional: BACKUP_RETENTION_DAYS (default 30)
"""

import gzip
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import httpx

BUCKET = "db-backups"
DEFAULT_RETENTION_DAYS = 30


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"ERROR: {name} is not set.", file=sys.stderr)
        sys.exit(1)
    return value


def dump_database(database_url: str) -> bytes:
    """
    Plain-SQL dump (not the custom binary format) so a restore is just
    `gunzip -c backup.sql.gz | psql "$DATABASE_URL"` - no pg_restore version
    matching to worry about later.
    """
    result = subprocess.run(
        ["pg_dump", "--format=plain", "--no-owner", "--no-privileges", database_url],
        capture_output=True,
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {result.stderr.decode(errors='replace')}")
    return gzip.compress(result.stdout)


def ensure_bucket_exists(supabase_url: str, service_key: str) -> None:
    headers = {"Authorization": f"Bearer {service_key}", "apikey": service_key, "Content-Type": "application/json"}
    response = httpx.post(
        f"{supabase_url}/storage/v1/bucket",
        headers=headers,
        json={"id": BUCKET, "name": BUCKET, "public": False},
        timeout=30.0,
    )
    # 200/201 = created, 409/400-with-"already exists" = fine, anything else is a real problem.
    if response.status_code not in (200, 201) and "already exists" not in response.text.lower():
        raise RuntimeError(f"Could not create/verify backup bucket: {response.status_code} {response.text}")


def upload_backup(supabase_url: str, service_key: str, filename: str, data: bytes) -> None:
    headers = {
        "Authorization": f"Bearer {service_key}",
        "apikey": service_key,
        "Content-Type": "application/gzip",
    }
    response = httpx.post(
        f"{supabase_url}/storage/v1/object/{BUCKET}/{filename}",
        headers=headers,
        content=data,
        timeout=120.0,
    )
    if response.status_code not in (200, 201):
        raise RuntimeError(f"Backup upload failed: {response.status_code} {response.text}")


def prune_old_backups(supabase_url: str, service_key: str, retention_days: int) -> None:
    """
    Best-effort: a pruning failure must never fail the backup job itself -
    losing the ability to auto-delete old backups is a minor annoyance
    (fixable later, worst case Supabase storage usage grows), losing the
    backup itself is not.
    """
    headers = {"Authorization": f"Bearer {service_key}", "apikey": service_key, "Content-Type": "application/json"}
    try:
        response = httpx.post(
            f"{supabase_url}/storage/v1/object/list/{BUCKET}",
            headers=headers,
            json={"limit": 1000, "sortBy": {"column": "name", "order": "asc"}},
            timeout=30.0,
        )
        response.raise_for_status()
        objects = response.json()
    except Exception as exc:  # noqa: BLE001 - see docstring, this must never break the backup itself
        print(f"WARNING: could not list backups for pruning: {exc}", file=sys.stderr)
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    for obj in objects:
        name = obj.get("name", "")
        # Filenames are "backup-YYYY-MM-DDTHH-MM-SSZ.sql.gz" (see main()) -
        # parsed from the name itself rather than trusted Supabase metadata,
        # since we control the format and it's one less thing to get wrong.
        try:
            stamp = name.removeprefix("backup-").removesuffix(".sql.gz")
            backup_time = datetime.strptime(stamp, "%Y-%m-%dT%H-%M-%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue  # not one of our files - leave it alone

        if backup_time < cutoff:
            try:
                del_response = httpx.delete(f"{supabase_url}/storage/v1/object/{BUCKET}/{name}", headers=headers, timeout=30.0)
                if del_response.status_code in (200, 201):
                    print(f"Pruned old backup: {name}")
                else:
                    print(f"WARNING: could not prune {name}: {del_response.status_code} {del_response.text}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                print(f"WARNING: could not prune {name}: {exc}", file=sys.stderr)


def main() -> None:
    database_url = _env("DATABASE_URL")
    supabase_url = _env("SUPABASE_URL")
    service_key = _env("SUPABASE_SERVICE_KEY")
    retention_days = int(os.environ.get("BACKUP_RETENTION_DAYS", DEFAULT_RETENTION_DAYS))

    print("Dumping database...")
    compressed = dump_database(database_url)
    print(f"Dump complete: {len(compressed) / 1024:.1f} KB compressed")

    filename = f"backup-{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ')}.sql.gz"

    print("Ensuring backup bucket exists...")
    ensure_bucket_exists(supabase_url, service_key)

    print(f"Uploading {filename}...")
    upload_backup(supabase_url, service_key, filename, compressed)
    print("Upload complete.")

    print(f"Pruning backups older than {retention_days} days...")
    prune_old_backups(supabase_url, service_key, retention_days)

    print("Done.")


if __name__ == "__main__":
    main()
