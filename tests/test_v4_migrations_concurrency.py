"""C1-C9 from the plan: concurrent web + worker startup. SQLite always; PostgreSQL when TEST_POSTGRES_URL is set."""
import importlib.util
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text

from tests.helpers_v4 import FIXTURES, ROOT, make_v1_db, run_concurrent_init, spawn_init

STRESS = int(os.getenv("JH_STRESS", "5"))
PG = os.getenv("TEST_POSTGRES_URL", "")


def _check_final_state(url, expect_jobs=3):
    eng = create_engine(url)
    cols = [c["name"] for c in inspect(eng).get_columns("jobs")]
    assert len(cols) == len(set(cols)), "a column was added twice"
    assert "canonical_job_id" in cols and "search_text" in cols
    with eng.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM schema_migrations")).scalar() == 1
        assert c.execute(text("SELECT COUNT(*) FROM jobs")).scalar() == expect_jobs
    idx = {i["name"] for i in inspect(eng).get_indexes("jobs")}
    assert "ix_jobs_dedup_key" in idx
    return eng


@pytest.mark.parametrize("round_", range(STRESS))
def test_c1_sqlite_eight_processes_start_together(tmp_path, round_):
    url = make_v1_db(tmp_path / "c1.db")
    outs = run_concurrent_init(url, tmp_path / "go", n=8)
    assert all(rc == 0 for rc, _ in outs), outs
    _check_final_state(url)
    con = sqlite3.connect(tmp_path / "c1.db")
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_c2_sqlite_waits_for_a_held_write_lock(tmp_path):
    url = make_v1_db(tmp_path / "c2.db")
    holder = sqlite3.connect(tmp_path / "c2.db", isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    p = spawn_init(url, tmp_path / "go2", {"MIGRATION_LOCK_TIMEOUT_S": "20"})
    (tmp_path / "go2").write_text("go")
    time.sleep(2.5)
    assert p.poll() is None, "the migration should still be waiting for the lock"
    holder.execute("ROLLBACK")
    out, _ = p.communicate(timeout=60)
    assert p.returncode == 0, out
    _check_final_state(url)


def test_c2b_sqlite_busy_timeout_raises_migration_busy_and_changes_nothing(tmp_path):
    url = make_v1_db(tmp_path / "c2b.db")
    holder = sqlite3.connect(tmp_path / "c2b.db", isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    p = spawn_init(url, tmp_path / "gob", {"MIGRATION_LOCK_TIMEOUT_S": "1"})
    (tmp_path / "gob").write_text("go")
    out, _ = p.communicate(timeout=60)
    holder.execute("ROLLBACK")
    assert p.returncode == 3 and "BUSY" in out, out
    eng = create_engine(url)
    assert "schema_migrations" not in inspect(eng).get_table_names()
    assert "canonical_job_id" not in {c["name"] for c in inspect(eng).get_columns("jobs")}


def test_c3_killed_mid_migration_leaves_a_consistent_database(tmp_path):
    url = make_v1_db(tmp_path / "c3.db")
    p = spawn_init(url, tmp_path / "go3", {"JH_MIGRATION_FAULT": "after_ddl:kill"})
    (tmp_path / "go3").write_text("go")
    p.communicate(timeout=60)
    assert p.returncode == 137
    con = sqlite3.connect(tmp_path / "c3.db")
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()
    outs = run_concurrent_init(url, tmp_path / "go3b", n=2)
    assert all(rc == 0 for rc, _ in outs), outs
    _check_final_state(url)


def test_c8_v1_code_inserting_during_a_v2_migration(tmp_path):
    """A rolling deploy: an old (v1) process keeps inserting while a new process migrates."""
    url = make_v1_db(tmp_path / "c8.db")
    spec = importlib.util.spec_from_file_location("v1_models", FIXTURES / "v1_models.py")
    v1 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(v1)
    from sqlalchemy.orm import Session
    stop, errors, inserted = threading.Event(), [], [0]

    def writer():
        eng = create_engine(url, connect_args={"timeout": 30})
        i = 0
        while not stop.is_set():
            try:
                with Session(eng) as s:
                    s.add(v1.Job(source="lever", external_id=f"live{i}", company="C", title="T", url="u", description="d"))
                    s.commit()
                inserted[0] += 1
            except Exception as e:     # a v1 process must never see a schema error
                errors.append(repr(e))
            i += 1
            time.sleep(0.005)

    t = threading.Thread(target=writer)
    t.start()
    outs = run_concurrent_init(url, tmp_path / "go8", n=3)
    stop.set(); t.join()
    assert all(rc == 0 for rc, _ in outs), outs
    assert not [e for e in errors if "no such column" in e or "has no column" in e], errors[:3]
    _check_final_state(url, expect_jobs=3 + inserted[0] - len([e for e in errors]))


def test_c9_running_twice_and_against_a_current_database_is_a_noop(tmp_path):
    url = make_v1_db(tmp_path / "c9.db")
    run_concurrent_init(url, tmp_path / "go9a", n=1)
    outs = run_concurrent_init(url, tmp_path / "go9b", n=4)
    assert all(rc == 0 for rc, _ in outs)
    _check_final_state(url)


# ------------------------------------------------------------------ PostgreSQL (CI service container)
pg = pytest.mark.skipif(not PG, reason="set TEST_POSTGRES_URL to run the PostgreSQL concurrency tests (C4-C6)")


def _pg_schema_url():
    """A throw-away schema so tests never touch real data."""
    name = "jh_" + uuid.uuid4().hex[:10]
    eng = create_engine(PG)
    with eng.begin() as c:
        c.execute(text(f"CREATE SCHEMA {name}"))
    sep = "&" if "?" in PG else "?"
    return name, f"{PG}{sep}options=-csearch_path%3D{name}", eng


def _pg_seed_v1(url):
    eng = create_engine(url)
    from sqlalchemy.orm import Session  # noqa
    spec = importlib.util.spec_from_file_location("v1_models", FIXTURES / "v1_models.py")
    v1 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(v1)
    v1.Base.metadata.create_all(eng)
    with Session(eng) as s:
        for j in range(3):
            s.add(v1.Job(source="greenhouse", external_id=f"e{j}", company="Acme", title=f"Engineer {j}", url="u", description="d"))
        s.commit()
    return eng


@pg
def test_c4_postgres_eight_processes_start_together(tmp_path):
    name, url, admin = _pg_schema_url()
    try:
        _pg_seed_v1(url)
        outs = run_concurrent_init(url, tmp_path / "pg4", n=8)
        assert all(rc == 0 for rc, _ in outs), outs
        for _, out in outs:
            assert "duplicate" not in out.lower() and "already exists" not in out.lower(), out
        _check_final_state(url)
    finally:
        with admin.begin() as c:
            c.execute(text(f"DROP SCHEMA {name} CASCADE"))


@pg
def test_c5_postgres_held_advisory_lock_gives_migration_busy_without_partial_ddl(tmp_path):
    from app import migrations
    name, url, admin = _pg_schema_url()
    try:
        eng = _pg_seed_v1(url)
        holder = eng.connect()
        holder.execute(text("SELECT pg_advisory_xact_lock(:a,:b)"), {"a": migrations.APP_LOCK_KEY, "b": migrations.SCHEMA_STEP_ID})
        p = spawn_init(url, tmp_path / "pg5", {"MIGRATION_LOCK_TIMEOUT_S": "2"})
        (tmp_path / "pg5").write_text("go")
        out, _ = p.communicate(timeout=60)
        holder.rollback(); holder.close()
        assert p.returncode == 3 and "BUSY" in out, out
        assert "canonical_job_id" not in {c["name"] for c in inspect(create_engine(url)).get_columns("jobs")}
    finally:
        with admin.begin() as c:
            c.execute(text(f"DROP SCHEMA {name} CASCADE"))


@pg
def test_c6_postgres_fault_between_statements_rolls_the_whole_step_back(tmp_path):
    name, url, admin = _pg_schema_url()
    try:
        _pg_seed_v1(url)
        p = spawn_init(url, tmp_path / "pg6", {"JH_MIGRATION_FAULT": "after_ddl"})
        (tmp_path / "pg6").write_text("go")
        p.communicate(timeout=60)
        assert p.returncode == 1
        eng = create_engine(url)
        assert "canonical_job_id" not in {c["name"] for c in inspect(eng).get_columns("jobs")}
        assert "schema_migrations" not in inspect(eng).get_table_names()
    finally:
        with admin.begin() as c:
            c.execute(text(f"DROP SCHEMA {name} CASCADE"))
