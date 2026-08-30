from fastapi import FastAPI

from app.routers import auth, orders, print_agents, print_jobs, tenants

app = FastAPI(title="eBay Seller SaaS API", version="0.1.0")

app.include_router(auth.router)
app.include_router(tenants.router)
app.include_router(orders.router)
app.include_router(print_agents.router)
app.include_router(print_jobs.router)


@app.get("/health")
def health_check():
    """Used by Render to confirm the service is alive. No DB call on purpose -
    keep this fast and dependency-free so it can't false-negative from a
    slow database, only from the app process itself being down."""
    return {"status": "ok"}
