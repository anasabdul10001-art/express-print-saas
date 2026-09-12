from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text

from app.config import settings
from app.core.rate_limit import limiter
from app.core.sentry import init_sentry
from app.database import Base, engine
from app import models  # noqa: F401 - import registers all models with Base, needed for create_all() below
from app.routers import admin, affiliate_portal, auth, billing, dhl, ebay, expenses, market_research, orders, plans, print_agents, print_jobs, products, tenants, uploads

# Before the app is created: a no-op until settings.sentry_dsn is set (see
# app/core/sentry.py) - safe to always call.
init_sentry()

app = FastAPI(title="eBay Seller SaaS API", version="0.1.0")

# There's no Alembic here (see init_db.py) - create_all() only creates
# missing tables, it never adds a column to one that already exists in
# production. Calling it on every startup means a brand-new model (like
# PaymentMethod) gets its table automatically instead of requiring a
# manual `python init_db.py` run against the live database; it's a no-op
# for tables that already exist. Column changes on an existing table still
# need an explicit ALTER below.
Base.metadata.create_all(bind=engine)

with engine.begin() as connection:
    connection.execute(text("ALTER TABLE plans ADD COLUMN IF NOT EXISTS trial_days INTEGER"))
    connection.execute(text("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS plan_id UUID REFERENCES plans(id)"))
    connection.execute(text("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS trial_ends_at TIMESTAMPTZ"))
    connection.execute(text("ALTER TABLE site_settings ADD COLUMN IF NOT EXISTS logo_height INTEGER"))
    connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT"))
    connection.execute(text("ALTER TABLE site_settings ADD COLUMN IF NOT EXISTS company_legal_name VARCHAR(255)"))
    connection.execute(text("ALTER TABLE site_settings ADD COLUMN IF NOT EXISTS company_address VARCHAR(1000)"))
    connection.execute(text("ALTER TABLE site_settings ADD COLUMN IF NOT EXISTS company_tax_id VARCHAR(100)"))
    connection.execute(text("ALTER TABLE site_settings ADD COLUMN IF NOT EXISTS company_email VARCHAR(255)"))
    connection.execute(text("ALTER TABLE site_settings ADD COLUMN IF NOT EXISTS last_invoice_number INTEGER NOT NULL DEFAULT 0"))
    connection.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS weight_kg DECIMAL(6,3)"))
    connection.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS length_cm INTEGER"))
    connection.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS width_cm INTEGER"))
    connection.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS height_cm INTEGER"))
    connection.execute(text("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS default_package_weight_kg DECIMAL(6,3) NOT NULL DEFAULT 1.0"))
    connection.execute(text("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS default_package_length_cm INTEGER NOT NULL DEFAULT 20"))
    connection.execute(text("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS default_package_width_cm INTEGER NOT NULL DEFAULT 15"))
    connection.execute(text("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS default_package_height_cm INTEGER NOT NULL DEFAULT 10"))
    connection.execute(text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS recipient_name VARCHAR(255)"))
    connection.execute(text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS recipient_street1 VARCHAR(255)"))
    connection.execute(text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS recipient_street2 VARCHAR(255)"))
    connection.execute(text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS recipient_city VARCHAR(100)"))
    connection.execute(text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS recipient_state VARCHAR(100)"))
    connection.execute(text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS recipient_zip VARCHAR(20)"))
    connection.execute(text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS recipient_country_code VARCHAR(2)"))
    connection.execute(text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS recipient_phone VARCHAR(50)"))
    connection.execute(text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS tracking_number VARCHAR(50)"))
    connection.execute(text("ALTER TABLE print_jobs ADD COLUMN IF NOT EXISTS label_pdf_data BYTEA"))
    connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS early_service_consent_at TIMESTAMPTZ"))
    connection.execute(text("ALTER TABLE affiliates ADD COLUMN IF NOT EXISTS password_hash VARCHAR"))
    connection.execute(text("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS stripe_customer_id VARCHAR(255)"))
    connection.execute(text("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS stripe_subscription_id VARCHAR(255)"))
    connection.execute(text("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS subscription_status VARCHAR(30)"))

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Restricted to known frontend origins rather than "*". In production the
# frontend is served from this same app (see the StaticFiles mount below),
# so this mainly matters for local development and testing against a
# separately-hosted frontend. No cookies are used for auth (JWT goes in the
# Authorization header - see app/core/deps.py), so allow_credentials stays
# False; there's nothing it would protect anyway.
ALLOWED_ORIGINS = [
    settings.frontend_url,
    "https://express-print-saas.onrender.com",
    "http://localhost:5500",
    "http://localhost:8899",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(dict.fromkeys(ALLOWED_ORIGINS)),  # de-dup, keep order
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(tenants.router)
app.include_router(products.router)
app.include_router(orders.router)
app.include_router(print_agents.router)
app.include_router(print_jobs.router)
app.include_router(ebay.router)
app.include_router(uploads.router)
app.include_router(expenses.router)
app.include_router(market_research.router)
app.include_router(admin.router)
app.include_router(plans.router)
app.include_router(dhl.router)
app.include_router(affiliate_portal.router)
app.include_router(billing.router)


@app.get("/health")
def health_check():
    """Used by Render to confirm the service is alive. No DB call on purpose -
    keep this fast and dependency-free so it can't false-negative from a
    slow database, only from the app process itself being down."""
    return {"status": "ok"}


# Serves the frontend (index.html, login.html, app/dashboard.html, app/ebay.html)
# from the same service, at the same domain, so the API and the UI share one
# Render deployment. Mounted last so it never shadows the API routes above.
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")