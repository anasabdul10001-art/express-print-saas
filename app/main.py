from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.database import engine
from app.routers import admin, auth, ebay, expenses, market_research, orders, plans, print_agents, print_jobs, products, tenants, uploads

app = FastAPI(title="eBay Seller SaaS API", version="0.1.0")

# There's no Alembic here (see init_db.py) - create_all() only creates
# missing tables, it never adds a column to one that already exists in
# production. Running new-column ALTERs here, guarded with IF NOT EXISTS,
# keeps them applying automatically on every deploy without a manual
# migration step against the live database.
with engine.begin() as connection:
    connection.execute(text("ALTER TABLE plans ADD COLUMN IF NOT EXISTS trial_days INTEGER"))
    connection.execute(text("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS plan_id UUID REFERENCES plans(id)"))
    connection.execute(text("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS trial_ends_at TIMESTAMPTZ"))
    connection.execute(text("ALTER TABLE site_settings ADD COLUMN IF NOT EXISTS logo_height INTEGER"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
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