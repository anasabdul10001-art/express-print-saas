"""
Shared pytest fixtures. Needs a real Postgres database (SQLite can't handle
this app's UUID/ARRAY/JSONB-ish column types) - either a local one you point
DATABASE_URL at, or the `postgres:` service container the CI workflow
(.github/workflows/tests.yml) spins up. Whatever database TEST_DATABASE_URL
(or DATABASE_URL) points at gets its public schema wiped on the FIRST test
run of the session - never point this at a database with real data.
"""

import os

from cryptography.fernet import Fernet

# Must be set before any `app.*` module is imported - app/config.py reads
# these from the environment at import time via pydantic-settings.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or os.environ.get(
    "DATABASE_URL", "postgresql://testapp:testpass@localhost:5432/testdb"
)
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")
os.environ.setdefault("FRONTEND_URL", "http://localhost:8899")
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("DHL_API_KEY", "test-dhl-api-key")
os.environ.setdefault("DHL_ENVIRONMENT", "SANDBOX")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.config import settings

# Reset to a bare, empty schema BEFORE importing app.main - its import runs
# Base.metadata.create_all() plus the manual ALTER TABLE statements (see
# app/main.py), which is the app's real startup schema-creation path. We
# want that exact path exercised fresh every test session, not whatever
# state a previous local run left behind.
_bootstrap_engine = create_engine(settings.database_url)
with _bootstrap_engine.begin() as _conn:
    _conn.execute(text("DROP SCHEMA public CASCADE"))
    _conn.execute(text("CREATE SCHEMA public"))
_bootstrap_engine.dispose()

from app.core.rate_limit import limiter  # noqa: E402
from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402 - importing builds the schema, see above


@pytest.fixture(autouse=True)
def _clean_tables():
    """
    Truncates every table before each test. Cheaper than dropping/recreating
    the whole schema per test, and simpler than wiring transaction-rollback
    isolation through FastAPI's `get_db` dependency (which opens its own
    session per request) - a full TRUNCATE keeps every test's assertions
    independent of test order without either complication.
    """
    with engine.begin() as conn:
        table_names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
        conn.execute(text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))
    # The rate limiter (app/core/rate_limit.py) keeps its counters in-process,
    # keyed by client IP - TestClient always looks like the same IP, so
    # without a reset here, login-rate-limit tests would bleed into whatever
    # other test happens to run next.
    limiter.reset()
    yield


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def register(client):
    """
    Registers a fresh tenant/user and returns (auth_headers, response_json).
    Uses an incrementing counter (not a fixed email) so multiple calls
    within one test never collide on the unique email constraint.
    """
    counter = {"n": 0}

    def _register(**overrides):
        counter["n"] += 1
        payload = {
            "email": f"test-user-{counter['n']}@example.com",
            "password": "testpass123",
            "company_name": f"Test Co {counter['n']}",
            "country_code": "DE",
            "early_service_consent": True,
        }
        payload.update(overrides)
        response = client.post("/auth/register", json=payload)
        assert response.status_code == 201, response.text
        token = response.json()["access_token"]
        return {"Authorization": f"Bearer {token}"}, payload

    return _register
