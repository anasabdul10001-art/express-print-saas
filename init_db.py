"""
init_db.py — creates all tables directly from the SQLAlchemy models.

This is an MVP shortcut. It's fine while the schema is still moving fast
and you're the only one deploying. Once the schema stabilizes (or a second
developer joins), switch to Alembic migrations instead - create_all() has
no concept of "change a column" or "track history," it only creates
tables that don't exist yet.

Usage:
    python init_db.py
"""

from app.database import Base, engine
from app import models  # noqa: F401 - import registers all models with Base

if __name__ == "__main__":
    print("Creating tables...")
    Base.metadata.create_all(bind=engine)
    print("Done.")
