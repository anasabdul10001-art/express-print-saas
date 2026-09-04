from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.routers import auth, ebay, orders, print_agents, print_jobs, products, tenants

app = FastAPI(title="eBay Seller SaaS API", version="0.1.0")

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