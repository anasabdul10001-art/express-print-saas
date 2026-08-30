from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import settings

# pool_pre_ping=True: checks a connection is still alive before using it.
# Important for cloud Postgres (Supabase/Render) which can silently drop
# idle connections - without this you'd get random "connection closed"
# errors after periods of low traffic.
engine = create_engine(settings.database_url, pool_pre_ping=True)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """
    FastAPI dependency: yields one DB session per request, closes it
    afterwards even if the request raised an exception (finally block).
    Usage in a route: db: Session = Depends(get_db)
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
