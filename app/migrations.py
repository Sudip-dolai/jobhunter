"""Schema migrations that are safe when the web app and the worker start at the same time.

Mechanism (see plan section 5):
  * fast path: one SELECT on schema_migrations; nothing else runs when the schema is current
  * locked path: ONE transaction per step -> take a lock -> re-check -> apply -> record -> commit
      - PostgreSQL: pg_advisory_xact_lock (transaction scoped, safe behind connection poolers)
      - SQLite:     BEGIN IMMEDIATE with a busy timeout
  * a failure rolls the whole step back and is raised, never swallowed
  * only additive DDL: nothing is dropped, renamed or retyped
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError, OperationalError

from app import config
from app.models import Base

log = logging.getLogger("jobhunter.migrations")

TARGET_VERSION = 1
APP_LOCK_KEY = 7405          # first half of the advisory-lock key; the second half is the step id
SCHEMA_STEP_ID = 1


class MigrationBusy(RuntimeError):
    """Another process holds the migration lock and did not release it within the timeout."""


class MigrationError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ----------------------------------------------------------------------------- locking
@contextmanager
def migration_lock(engine, step_id: int = SCHEMA_STEP_ID, timeout_s: int | None = None):
    """Yield a connection that holds the migration lock inside an open transaction.
    Commit on success, rollback on any exception. Raises MigrationBusy when the lock cannot be taken in time."""
    timeout_s = int(timeout_s or config.MIGRATION_LOCK_TIMEOUT_S)
    name = engine.dialect.name
    conn = engine.connect()
    try:
        if name == "postgresql":
            try:
                conn.exec_driver_sql(f"SET LOCAL lock_timeout = '{timeout_s * 1000}ms'")
                conn.execute(text("SELECT pg_advisory_xact_lock(:a, :b)"), {"a": APP_LOCK_KEY, "b": step_id})
            except DBAPIError as e:                       # lock_timeout (SQLSTATE 55P03) or similar
                conn.rollback()
                raise MigrationBusy(f"could not take the migration lock within {timeout_s}s: {e.orig}") from e
        elif name == "sqlite":
            conn.exec_driver_sql(f"PRAGMA busy_timeout = {timeout_s * 1000}")
            try:
                conn.exec_driver_sql("BEGIN IMMEDIATE")
            except OperationalError as e:
                conn.rollback()
                raise MigrationBusy(f"database is locked by another process: {e.orig}") from e
        # other dialects: plain transaction (no cross-process lock available)
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


# ----------------------------------------------------------------------------- state
def current_version(engine) -> int:
    insp = inspect(engine)
    if not insp.has_table("schema_migrations"):
        return 0
    with engine.connect() as c:
        try:
            return int(c.execute(text("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")).scalar() or 0)
        finally:
            c.rollback()


def _version_in(conn) -> int:
    insp = inspect(conn)
    if not insp.has_table("schema_migrations"):
        return 0
    return int(conn.execute(text("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")).scalar() or 0)


# ----------------------------------------------------------------------------- DDL pieces
def add_missing_columns(conn) -> list[str]:
    """ADD COLUMN for every model column the table lacks. Called while holding the lock."""
    insp = inspect(conn)
    pg = conn.dialect.name == "postgresql"
    added = []
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name not in have:
                ine = "IF NOT EXISTS " if pg else ""
                conn.exec_driver_sql(f"ALTER TABLE {table.name} ADD COLUMN {ine}{col.name} {col.type.compile(conn.dialect)}")
                added.append(f"{table.name}.{col.name}")
    return added


def create_indexes(conn) -> None:
    for table in Base.metadata.sorted_tables:
        for ix in table.indexes:
            cols = ", ".join(c.name for c in ix.columns)
            uniq = "UNIQUE " if ix.unique else ""
            conn.exec_driver_sql(f"CREATE {uniq}INDEX IF NOT EXISTS {ix.name} ON {table.name} ({cols})")


_EVENT_COLS = ["id", "job_id", "event_type", "from_value", "to_value", "reason", "score", "related_job_id",
               "run_id", "migration_run_id", "group_event_id", "created_at"]


def install_audit_triggers(conn) -> None:
    """job_events is append-only (only undone_at may change); migration_runs rows are never deleted."""
    name = conn.dialect.name
    if name == "sqlite":
        changed = " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in _EVENT_COLS + ["evidence"])
        conn.exec_driver_sql("CREATE TRIGGER IF NOT EXISTS job_events_no_delete BEFORE DELETE ON job_events "
                             "BEGIN SELECT RAISE(ABORT, 'job_events is append-only'); END")
        conn.exec_driver_sql(f"CREATE TRIGGER IF NOT EXISTS job_events_no_update BEFORE UPDATE ON job_events WHEN {changed} "
                             "BEGIN SELECT RAISE(ABORT, 'job_events is append-only (only undone_at may change)'); END")
        conn.exec_driver_sql("CREATE TRIGGER IF NOT EXISTS migration_runs_no_delete BEFORE DELETE ON migration_runs "
                             "BEGIN SELECT RAISE(ABORT, 'migration_runs rows are never deleted'); END")
    elif name == "postgresql":
        new = ", ".join(f"NEW.{c}" for c in _EVENT_COLS) + ", NEW.evidence::text"
        old = ", ".join(f"OLD.{c}" for c in _EVENT_COLS) + ", OLD.evidence::text"
        conn.exec_driver_sql(
            "CREATE OR REPLACE FUNCTION jobhunter_job_events_guard() RETURNS trigger AS $$ BEGIN "
            "IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'job_events is append-only'; END IF; "
            f"IF ({new}) IS DISTINCT FROM ({old}) THEN RAISE EXCEPTION 'job_events is append-only (only undone_at may change)'; END IF; "
            "RETURN NEW; END; $$ LANGUAGE plpgsql")
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS job_events_guard ON job_events")
        conn.exec_driver_sql("CREATE TRIGGER job_events_guard BEFORE UPDATE OR DELETE ON job_events "
                             "FOR EACH ROW EXECUTE FUNCTION jobhunter_job_events_guard()")
        conn.exec_driver_sql(
            "CREATE OR REPLACE FUNCTION jobhunter_runs_guard() RETURNS trigger AS $$ BEGIN "
            "RAISE EXCEPTION 'migration_runs rows are never deleted'; END; $$ LANGUAGE plpgsql")
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS migration_runs_guard ON migration_runs")
        conn.exec_driver_sql("CREATE TRIGGER migration_runs_guard BEFORE DELETE ON migration_runs "
                             "FOR EACH ROW EXECUTE FUNCTION jobhunter_runs_guard()")


# ----------------------------------------------------------------------------- steps
def _step_v1_expand(conn) -> None:
    """v2 expand: tables, additive columns, indexes, audit triggers. Purely additive."""
    Base.metadata.create_all(conn)
    add_missing_columns(conn)
    create_indexes(conn)
    install_audit_triggers(conn)


STEPS = {1: ("v2_expand", _step_v1_expand)}


def _fault(point: str) -> None:
    """Test hook: raise (or kill the process) at a named point when JH_MIGRATION_FAULT matches."""
    import os
    f = os.getenv("JH_MIGRATION_FAULT", "")
    if f == point:
        raise MigrationError(f"injected fault at {point}")
    if f == f"{point}:kill":
        os._exit(137)


def ensure_schema(engine, timeout_s: int | None = None) -> bool:
    """Bring the schema to TARGET_VERSION. Returns True if anything was applied.
    Fast path costs one query; everything else happens under the lock in one transaction per step."""
    if current_version(engine) >= TARGET_VERSION:
        return False
    applied = False
    for version in range(1, TARGET_VERSION + 1):
        name, fn = STEPS[version]
        with migration_lock(engine, step_id=version, timeout_s=timeout_s) as conn:
            if _version_in(conn) >= version:                  # re-check after the lock: someone else did it
                continue
            fn(conn)
            _fault("after_ddl")
            conn.execute(text("INSERT INTO schema_migrations (version, applied_at, note) VALUES (:v, :t, :n)"),
                         {"v": version, "t": _now(), "n": name})
            applied = True
            log.info("applied schema step %s (v%s)", name, version)
    return applied


def ensure_additive_columns(engine, timeout_s: int | None = None) -> list[str]:
    """Legacy helper entry point (db._add_missing_columns): same locked path, columns only."""
    with migration_lock(engine, step_id=SCHEMA_STEP_ID, timeout_s=timeout_s) as conn:
        return add_missing_columns(conn)


def row_counts(conn_or_engine, tables=None) -> dict:
    tables = tables or [t.name for t in Base.metadata.sorted_tables]
    out = {}
    eng = conn_or_engine
    with (eng.connect() if hasattr(eng, "connect") else _Nullctx(eng)) as c:
        insp = inspect(c)
        for t in tables:
            if insp.has_table(t):
                out[t] = int(c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() or 0)
        if hasattr(c, "rollback"):
            c.rollback()
    return out


class _Nullctx:
    def __init__(self, v): self.v = v
    def __enter__(self): return self.v
    def __exit__(self, *a): return False


if __name__ == "__main__":      # python -m app.migrations status|upgrade|backup|backfill|dedup|validate [--apply]
    import sys
    from app import bulk
    sys.exit(bulk.main())
