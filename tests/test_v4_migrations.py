"""v1 -> v2 upgrade, idempotence, compatibility with the v1 model, audit triggers."""
import importlib.util
import sqlite3

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app import migrations
from app.models import Job
from tests.helpers_v4 import FIXTURES, make_v1_db

NEW_JOB_COLS = {"canonical_job_id", "company_id", "apply_url", "company_norm", "title_norm", "city_norm", "dedup_key",
                "content_hash", "search_text", "status", "status_reason", "closed_at", "last_seen_at", "miss_count",
                "first_missed_at", "last_run_id", "posted_at", "posted_at_source", "valid_through", "sort_date",
                "salary_raw", "salary_currency", "salary_period", "salary_orig_min", "salary_orig_max", "salary_source",
                "category", "seniority"}


def _eng(tmp_path, **kw):
    url = make_v1_db(tmp_path / "v1.db", **kw)
    return create_engine(url), url


def test_v1_database_upgrades_and_keeps_every_row(tmp_path):
    eng, _ = _eng(tmp_path)
    before = migrations.row_counts(eng, ["users", "jobs", "matches", "applications"])
    assert migrations.ensure_schema(eng) is True
    cols = {c["name"] for c in inspect(eng).get_columns("jobs")}
    assert NEW_JOB_COLS <= cols
    assert migrations.row_counts(eng, ["users", "jobs", "matches", "applications"]) == before
    names = set(inspect(eng).get_table_names())
    assert {"companies", "company_aliases", "source_runs", "job_events", "schema_migrations", "migration_runs"} <= names
    idx = {i["name"] for i in inspect(eng).get_indexes("jobs")}
    assert {"ix_jobs_dedup_key", "ix_jobs_canonical_job_id", "ix_jobs_search_default", "ix_jobs_source_last_seen"} <= idx
    # legacy columns untouched and new ones NULL
    with eng.connect() as c:
        row = c.execute(text("SELECT title, company, status, canonical_job_id, dedup_key FROM jobs WHERE id=1")).one()
    assert row[0] == "Software Engineer 1" and row[1] == "Acme Inc" and row[2:] == (None, None, None)


def test_upgrade_is_idempotent(tmp_path):
    eng, _ = _eng(tmp_path)
    assert migrations.ensure_schema(eng) is True
    assert migrations.ensure_schema(eng) is False
    assert migrations.current_version(eng) == migrations.TARGET_VERSION
    with eng.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM schema_migrations")).scalar() == 1


def test_v1_model_still_works_against_a_v2_database(tmp_path):
    """Rolling deploys / code rollback: v1 inserts and reads need no DDL (plan section 8, rollback level 4)."""
    eng, _ = _eng(tmp_path)
    migrations.ensure_schema(eng)
    spec = importlib.util.spec_from_file_location("v1_models", FIXTURES / "v1_models.py")
    v1 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(v1)
    with Session(eng) as s:
        s.add(v1.Job(source="lever", external_id="zz", company="C", title="T", url="u", description="d"))
        s.commit()
        got = s.query(v1.Job).filter_by(external_id="zz").one()
        assert got.title == "T"
        assert s.query(v1.Match).count() == 1 and s.query(v1.Application).count() == 1


def test_legacy_add_missing_columns_entry_point_still_works(tmp_path):
    eng, _ = _eng(tmp_path)
    migrations.ensure_schema(eng)
    with eng.begin() as c:
        c.execute(text("ALTER TABLE jobs DROP COLUMN seniority"))      # simulate an older v2 database
    assert "seniority" not in {c["name"] for c in inspect(eng).get_columns("jobs")}
    added = migrations.ensure_additive_columns(eng)
    assert added == ["jobs.seniority"]


def test_audit_triggers_make_job_events_append_only(tmp_path):
    eng, _ = _eng(tmp_path)
    migrations.ensure_schema(eng)
    with eng.begin() as c:
        c.execute(text("INSERT INTO job_events (job_id,event_type,reason,created_at) VALUES (1,'merge','t',datetime('now'))"))
    with eng.begin() as c:                                             # undone_at may change
        c.execute(text("UPDATE job_events SET undone_at = datetime('now') WHERE job_id=1"))
    for sql in ("DELETE FROM job_events", "UPDATE job_events SET reason='x'", "UPDATE job_events SET to_value='x'"):
        with pytest.raises(Exception, match="append-only"):
            with eng.begin() as c:
                c.execute(text(sql))
    with eng.begin() as c:
        c.execute(text("INSERT INTO migration_runs (step, mode, outcome, started_at) VALUES ('s','dry_run','ok',datetime('now'))"))
    with pytest.raises(Exception, match="never deleted"):
        with eng.begin() as c:
            c.execute(text("DELETE FROM migration_runs"))


def test_failed_step_rolls_back_completely(tmp_path, monkeypatch):
    eng, _ = _eng(tmp_path)
    monkeypatch.setenv("JH_MIGRATION_FAULT", "after_ddl")
    with pytest.raises(migrations.MigrationError):
        migrations.ensure_schema(eng)
    monkeypatch.delenv("JH_MIGRATION_FAULT")
    cols = {c["name"] for c in inspect(eng).get_columns("jobs")}
    assert not (NEW_JOB_COLS & cols), "a failed step must not leave partial DDL behind"
    assert "schema_migrations" not in inspect(eng).get_table_names()
    assert migrations.ensure_schema(eng) is True                        # and a retry succeeds
