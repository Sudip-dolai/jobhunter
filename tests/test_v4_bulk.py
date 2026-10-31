"""Backfill and dedup apply: dry-run purity, backup + dry-run gates, resumability, leases, precision gate."""
import json
import os
import subprocess
import sys
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app import backup, bulk, dedup, migrations
from app.models import Job, JobEvent, MigrationRun
from tests.helpers_v4 import ROOT, make_v1_db, snapshot_user_data

LEGACY = ("source", "external_id", "company", "title", "location", "url", "description", "sector", "remote", "job_type",
          "country", "salary_min", "salary_max", "first_seen")


@pytest.fixture
def env(tmp_path):
    url = make_v1_db(tmp_path / "live.db", jobs=0, users=2)
    eng = create_engine(url, connect_args={"timeout": 30})
    migrations.ensure_schema(eng)
    desc = "Build and run services for the platform team with Python. " * 12
    rows = [("greenhouse", "g1", "Acme, Inc.", "Software Engineer II", "Pune, India", 12, 18),
            ("lever", "l1", "Acme Inc", "Software Engineer II (Remote) - Pune", "Pune, India", 12, 18),
            ("greenhouse", "g2", "Globex", "Data Analyst", "Mumbai, India", None, None),
            ("ashby", "a2", "Globex Corporation", "Data Analyst", "Mumbai, India", 9, 12),
            ("lever", "l3", "Initech", "Product Manager", "Bengaluru, India", 30, 40),
            ("workable", "w3", "Initech Ltd", "Backend Developer", "Delhi, India", None, None)]
    with Session(eng) as s:
        for src, ext, comp, title, loc, lo, hi in rows:         # legacy-shaped rows: no v2 columns filled
            s.add(Job(source=src, external_id=ext, company=comp, title=title, location=loc, url=f"https://{src}/{ext}",
                      description=desc, sector="SaaS", remote=False, job_type="Permanent", country="India", salary_min=lo, salary_max=hi))
        s.commit()
    return eng, tmp_path


def legacy_snapshot(eng):
    with Session(eng) as s:
        return [tuple(getattr(j, c) for c in LEGACY) for j in s.scalars(select(Job).order_by(Job.id))]


def prep(eng, tmp_path):
    return dict(outdir=tmp_path / "reports", backup_dir=tmp_path / "bk")


def good_backup(eng, tmp_path):
    return backup.create_verified_backup(eng, tmp_path / "bk")


def test_dry_run_changes_nothing_and_writes_a_report(env):
    eng, tp = env
    before = legacy_snapshot(eng)
    rep = bulk.backfill(eng, apply=False, outdir=tp / "reports")
    assert rep["rows_to_backfill"] == 6 and rep["acceptance_ok"] and rep["new_companies"] >= 3
    assert legacy_snapshot(eng) == before
    with Session(eng) as s:
        assert s.scalar(select(Job).where(Job.company_norm.is_not(None))) is None
        run = s.scalar(select(MigrationRun))
        assert run.mode == "dry_run" and run.report_hash and run.inputs_hash
    assert os.path.exists(rep["report_file"]) and list((tp / "reports").glob("*.csv"))


def test_apply_is_refused_without_backup_and_without_a_matching_dry_run(env):
    eng, tp = env
    kw = prep(eng, tp)
    with pytest.raises(backup.BackupGateError):
        bulk.backfill(eng, apply=True, **kw)
    good_backup(eng, tp)
    with pytest.raises(bulk.DryRunRequired, match="dry-run first"):
        bulk.backfill(eng, apply=True, **kw)
    bulk.backfill(eng, apply=False, outdir=kw["outdir"])
    with eng.begin() as c:                                      # the database changes after the dry-run
        c.execute(text("INSERT INTO jobs (source,external_id,company,title,location,url,description,first_seen) "
                       "VALUES ('x','n','C','T','','u','d',datetime('now'))"))
    with pytest.raises(bulk.DryRunRequired, match="changed since"):
        bulk.backfill(eng, apply=True, **kw)


def test_backfill_apply_fills_v2_columns_and_never_touches_legacy_or_user_data(env):
    eng, tp = env
    kw = prep(eng, tp)
    legacy_before, users_before = legacy_snapshot(eng), snapshot_user_data(eng)
    good_backup(eng, tp)
    bulk.backfill(eng, apply=False, outdir=kw["outdir"])
    out = bulk.backfill(eng, apply=True, **kw)
    assert out["rows_backfilled"] == 6 and out["protected_tables_unchanged"]
    assert legacy_snapshot(eng) == legacy_before, "legacy columns must be byte-identical"
    assert snapshot_user_data(eng) == users_before
    with Session(eng) as s:
        j = s.scalar(select(Job).where(Job.external_id == "g1"))
        assert (j.company_norm, j.title_norm, j.city_norm, j.status, j.miss_count) == ("acme", "software engineer ii", "pune", "active", 0)
        assert j.sort_date == j.first_seen and j.posted_at is None and j.posted_at_source == "first_seen"
        assert j.salary_source == "legacy_lpa" and j.salary_orig_min is None, "original currency of legacy LPA is unknown"
        assert j.last_seen_at is not None and j.company_id and j.search_text
        assert s.scalar(select(Job).where(Job.external_id == "g2")).salary_source is None
        run = s.scalar(select(MigrationRun).where(MigrationRun.mode == "apply"))
        assert run.outcome == "ok" and run.backup_manifest_hash and run.row_counts_before["jobs"] == 6 == run.row_counts_after["jobs"]
        from app.models import Company
        assert {c.name_norm for c in s.scalars(select(Company))} >= {"acme", "globex", "initech"}
        assert s.scalar(select(Job).where(Job.external_id == "g1")).company_id == s.scalar(select(Job).where(Job.external_id == "l1")).company_id


def test_backfill_is_idempotent(env):
    eng, tp = env
    kw = prep(eng, tp)
    good_backup(eng, tp)
    bulk.backfill(eng, apply=False, outdir=kw["outdir"]); bulk.backfill(eng, apply=True, **kw)
    rep = bulk.backfill(eng, apply=False, outdir=kw["outdir"])
    assert rep["rows_to_backfill"] == 0
    assert bulk.backfill(eng, apply=True, **kw)["rows_backfilled"] == 0


def test_interrupted_backfill_resumes_and_loses_nothing(env, monkeypatch):
    eng, tp = env
    kw = prep(eng, tp)
    good_backup(eng, tp)
    bulk.backfill(eng, apply=False, outdir=kw["outdir"])
    calls = []
    real = bulk._fault
    monkeypatch.setattr(bulk, "_fault", lambda p: (calls.append(p), (_ for _ in ()).throw(RuntimeError("crash")) if len(calls) == 2 else None))
    with pytest.raises(RuntimeError, match="crash"):
        bulk.backfill(eng, apply=True, batch=2, **kw)
    with Session(eng) as s:
        done = s.query(Job).filter(Job.company_norm.is_not(None)).count()
        assert done == 2, "the first batch was committed, the crashed batch rolled back"
        assert s.scalar(select(MigrationRun).where(MigrationRun.mode == "apply")).outcome == "failed"
    monkeypatch.setattr(bulk, "_fault", real)
    bulk.backfill(eng, apply=False, outdir=kw["outdir"])        # state changed: a fresh dry-run is required
    out = bulk.backfill(eng, apply=True, batch=2, **kw)
    assert out["rows_backfilled"] == 4
    with Session(eng) as s:
        assert s.query(Job).filter(Job.company_norm.is_(None)).count() == 0


def test_lease_blocks_a_second_runner_and_expired_leases_are_taken_over(env):
    eng, tp = env
    kw = prep(eng, tp)
    good_backup(eng, tp)
    bulk.backfill(eng, apply=False, outdir=kw["outdir"])
    with Session(eng) as s:
        s.add(MigrationRun(step="backfill", mode="apply", outcome="running", lease_expires_at=bulk._now() + timedelta(minutes=5)))
        s.commit()
    with pytest.raises(bulk.StepBusy):
        bulk.backfill(eng, apply=True, **kw)
    with Session(eng) as s:
        r = s.scalar(select(MigrationRun).where(MigrationRun.outcome == "running"))
        r.lease_expires_at = bulk._now() - timedelta(minutes=1); s.commit()
    assert bulk.backfill(eng, apply=True, **kw)["rows_backfilled"] == 6
    with Session(eng) as s:
        assert s.query(MigrationRun).filter_by(outcome="abandoned").count() == 1


def _backfilled(env):
    eng, tp = env
    kw = prep(eng, tp)
    good_backup(eng, tp)
    bulk.backfill(eng, apply=False, outdir=kw["outdir"]); bulk.backfill(eng, apply=True, **kw)
    return eng, tp, kw


def test_dedup_apply_groups_duplicates_logs_events_with_the_run_id_and_preserves_user_data(env):
    eng, tp, kw = _backfilled(env)
    users_before = snapshot_user_data(eng)
    good_backup(eng, tp)
    rep = bulk.dedup_apply(eng, apply=False, outdir=kw["outdir"])
    assert rep["golden"]["ok"] and rep["decisions"].get("merge:key", 0) >= 2
    out = bulk.dedup_apply(eng, apply=True, **kw)
    assert out["merged"] == 2 and out["protected_tables_unchanged"]
    assert snapshot_user_data(eng) == users_before
    with Session(eng) as s:
        assert dedup.validate_invariants(s) == []
        evs = s.query(JobEvent).filter_by(event_type="merge").all()
        assert len(evs) == 2 and all(e.migration_run_id for e in evs)
        roots = {(j.canonical_job_id or j.id) for j in s.scalars(select(Job))}
        assert len(roots) == 4, "6 listings, 2 duplicate pairs -> 4 canonical jobs"
        run = s.scalar(select(MigrationRun).where(MigrationRun.step == "dedup_apply", MigrationRun.mode == "apply"))
        assert run.outcome == "ok" and run.backup_manifest_hash
        # every merge made by the bulk step can be undone exactly
        for e in evs:
            dedup.unmerge(s, e.group_event_id)
        assert all(j.canonical_job_id is None for j in s.scalars(select(Job)))


def test_dedup_apply_needs_backup_and_dry_run(env):
    eng, tp, kw = _backfilled(env)                 # (this helper already took a verified backup in tp/"bk")
    with pytest.raises(backup.BackupGateError):
        bulk.dedup_apply(eng, apply=True, outdir=kw["outdir"], backup_dir=tp / "no-backups-here")
    with pytest.raises(bulk.DryRunRequired):
        bulk.dedup_apply(eng, apply=True, **kw)


def test_precision_gate_blocks_a_bad_decision_function(env, monkeypatch):
    eng, tp, kw = _backfilled(env)
    good_backup(eng, tp)
    monkeypatch.setattr(dedup, "decide", lambda a, b: dedup.Decision("merge", "key", 1.0, {}))   # a broken merger
    rep = bulk.dedup_apply(eng, apply=False, outdir=kw["outdir"])
    assert rep["golden"]["ok"] is False and rep["acceptance_ok"] is False
    with pytest.raises(bulk.AcceptanceFailed):
        bulk.dedup_apply(eng, apply=True, **kw)
    with Session(eng) as s:
        assert all(j.canonical_job_id is None for j in s.scalars(select(Job))), "nothing was merged"


def test_golden_pairs_pass_with_the_real_decision_function():
    g = bulk.golden_precision()
    assert g["ok"] and g["false_merges"] == 0 and g["precision"] == 1.0 and g["pairs"] >= 12, g


def test_cli_is_blocked_without_a_backup_and_reports_status(env):
    eng, tp = env
    e = dict(os.environ, DATABASE_URL=str(eng.url), PYTHONPATH=str(ROOT), BACKUP_DIR=str(tp / "bk"), REPORT_DIR=str(tp / "reports"))
    run = lambda *a: subprocess.run([sys.executable, "-m", "app.migrations", *a], cwd=ROOT, env=e, capture_output=True, text=True)  # noqa: E731
    st = run("status")
    assert st.returncode == 0 and json.loads(st.stdout)["schema_version"] == 1
    assert run("backfill").returncode == 0                       # dry-run is allowed
    blocked = run("backfill", "--apply")
    assert blocked.returncode == 3 and "BLOCKED" in blocked.stderr and "backup" in blocked.stderr.lower()
    assert run("backup").returncode == 0
    assert run("validate").stdout.strip() == "healthy"
