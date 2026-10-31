from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config, migrations

_sqlite = config.DATABASE_URL.startswith("sqlite")
# Hosted Postgres (Neon) closes idle connections: send TCP keepalives and recycle connections before they go stale.
_args = {"check_same_thread": False} if _sqlite else {"keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10, "keepalives_count": 5}
engine = create_engine(config.DATABASE_URL, pool_pre_ping=True, pool_recycle=-1 if _sqlite else 240, connect_args=_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def _add_missing_columns() -> None:
    """Legacy entry point kept for compatibility. Now runs under the migration lock (see app/migrations.py)."""
    migrations.ensure_additive_columns(engine)


def init_db() -> None:
    """Fast path: one SELECT when the schema is current. Otherwise a locked, transactional upgrade."""
    migrations.ensure_schema(engine)