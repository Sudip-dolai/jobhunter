"""Bulk (heavy-tier) steps: backfill and dedup apply. Never run from init_db() or the web process.

Every step: dry-run first (default) -> report JSON/CSV in reports/ + a migration_runs row -> apply only with
  (1) a verified, restore-tested, current backup  (app.backup)   and
  (2) an unexpired dry-run whose inputs_hash equals the present state (what was reported is what runs).
Apply works in short batches, checkpoints its progress, holds a lease row, and is resumable and idempotent.
It writes only NEW columns (backfill) or jobs.canonical_job_id (dedup); legacy data is never rewritten.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import backup, config, dedup, normalize
from app.companies import CompanyResolver, seed_aliases
from app.models import Job, MigrationRun

CODE_VERSION = "bulk-1"
LEASE_MINUTES = 10
DRY_RUN_MAX_AGE_HOURS = 24


class DryRunRequired(RuntimeError):
    pass


class StepBusy(RuntimeError):
    pass


class AcceptanceFailed(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def inputs_hash(engine, step: str) -> str:
    with Session(engine) as s:
        pending = (s.scalar(select(func.count()).select_from(Job).where(Job.company_norm.is_(None))) if step == "backfill"
                   else s.scalar(select(func.count()).select_from(Job).where(Job.canonical_job_id.is_(None))))
        max_id = s.scalar(select(func.max(Job.id))) or 0
    return _hash({"step": step, "counts": backup.table_counts(engine), "max_id": max_id, "pending": pending,
                  "version": CODE_VERSION, "dedup": [config.DEDUP_TITLE_SIM, config.DEDUP_DESC_SIM, config.DEDUP_MIN_DESC_CHARS]})


def _write_report(step: str, run_id: int, report: dict, outdir: Path | None = None) -> str:
    d = Path(outdir or config.REPORT_DIR)
    d.mkdir(parents=True, exist_ok=True)
    base = d / f"{step}-{run_id}-{_now():%Y%m%dT%H%M%S}"
    base.with_suffix(".json").write_text(json.dumps(report, indent=2, default=str))
    rows = report.get("samples") or []
    if rows:
        with open(base.with_suffix(".csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=sorted({k for r in rows for k in r}))
            w.writeheader(); w.writerows(rows)
    return str(base.with_suffix(".json"))


def _record_dry_run(engine, step: str, report: dict, outdir=None) -> int:
    with Session(engine) as s:
        run = MigrationRun(step=step, mode="dry_run", outcome="ok", report=report, report_hash=_hash(report),
                           inputs_hash=inputs_hash(engine, step), finished_at=_now(), row_counts_before=backup.table_counts(engine))
        s.add(run); s.commit()
        report["report_file"] = _write_report(step, run.id, report, outdir)
        return run.id


def _require_dry_run(engine, step: str, now: datetime | None = None) -> MigrationRun:
    now = now or _now()
    with Session(engine) as s:
        run = s.scalars(select(MigrationRun).where(MigrationRun.step == step, MigrationRun.mode == "dry_run",
                                                   MigrationRun.outcome == "ok").order_by(MigrationRun.id.desc()).limit(1)).first()
        if run is None:
            raise DryRunRequired(f"{step}: run the dry-run first (default mode) and review its report")
        if (now - run.finished_at) > timedelta(hours=DRY_RUN_MAX_AGE_HOURS):
            raise DryRunRequired(f"{step}: the dry-run is older than {DRY_RUN_MAX_AGE_HOURS}h; run it again")
        if run.inputs_hash != inputs_hash(engine, step):
            raise DryRunRequired(f"{step}: the database changed since the dry-run; run the dry-run again")
        s.expunge(run)
        return run


def _take_lease(s: Session, step: str) -> MigrationRun:
    now = _now()
    live = s.scalars(select(MigrationRun).where(MigrationRun.step == step, MigrationRun.mode == "apply",
                                                MigrationRun.outcome == "running")).all()
    for r in live:
        if r.lease_expires_at and r.lease_expires_at > now:
            raise StepBusy(f"{step} is already running (lease until {r.lease_expires_at})")
        r.outcome = "abandoned"                          # a crashed run: take over, resuming from its checkpoint
    resume = max([r.last_checkpoint or 0 for r in live], default=0)
    run = MigrationRun(step=step, mode="apply", outcome="running", started_at=now, lease_expires_at=now + timedelta(minutes=LEASE_MINUTES),
                       last_checkpoint=resume)
    s.add(run); s.flush()
    return run


def _heartbeat(run: MigrationRun, checkpoint: int) -> None:
    run.last_checkpoint = checkpoint
    run.lease_expires_at = _now() + timedelta(minutes=LEASE_MINUTES)


def _fault(point: str) -> None:
    import os
    if os.getenv("JH_BULK_FAULT") == point:
        raise RuntimeError(f"injected fault at {point}")


# ================================================================== backfill
def _backfill_fields(j: Job, resolver: CompanyResolver, now: datetime) -> dict:
    legacy = {"title": j.title, "company": j.company, "location": j.location, "url": j.url, "description": j.description or "",
              "salary_min": j.salary_min, "salary_max": j.salary_max, "remote": j.remote, "country": j.country, "first_seen": j.first_seen}
    d = normalize.derived_fields(legacy, company_id=resolver.resolve(j.company, j.sector), now=now)
    d["sort_date"] = j.first_seen                                    # no posted date is known for legacy rows
    d["posted_at"], d["posted_at_source"] = None, "first_seen"
    if d.get("salary_source") == "structured":                       # original currency of legacy LPA values is unknown
        d["salary_source"] = "legacy_lpa"
    return d


def backfill(engine, apply: bool = False, batch: int = 500, outdir=None, backup_dir=None) -> dict:
    """Fill the v2 columns of rows that lack them. Legacy columns are never modified."""
    if not apply:
        with Session(engine) as s:
            pending = s.scalars(select(Job).where(Job.company_norm.is_(None)).order_by(Job.id)).all()
            resolver, errors, nulls, samples = CompanyResolver(s), [], 0, []
            known_companies = set(resolver.by_alias)
            new_companies = set()
            for j in pending:
                try:
                    key = normalize.company_key(j.company)
                    if key not in known_companies:
                        new_companies.add(key)
                    d = normalize.derived_fields({"title": j.title, "company": j.company, "location": j.location, "description": j.description or "",
                                                  "remote": j.remote, "country": j.country, "salary_min": j.salary_min, "first_seen": j.first_seen})
                    nulls += (not d["company_norm"]) or (not d["title_norm"])
                    if len(samples) < 15:
                        samples.append({"id": j.id, "company": j.company, "company_norm": d["company_norm"], "title_norm": d["title_norm"], "city_norm": d["city_norm"]})
                except Exception as e:
                    errors.append({"id": j.id, "error": str(e)[:200]})
            s.rollback()
        report = {"step": "backfill", "rows_to_backfill": len(pending), "new_companies": len(new_companies), "row_errors": len(errors),
                  "unexpected_null_keys": nulls, "errors": errors[:20], "samples": samples,
                  "acceptance": {"zero_row_errors": not errors, "no_unexpected_null_keys": nulls == 0}}
        report["acceptance_ok"] = all(report["acceptance"].values())
        report["run_id"] = _record_dry_run(engine, "backfill", report, outdir)
        return report

    manifest_hash = backup.require_verified_backup(engine, "backfill", backup_dir)
    dry = _require_dry_run(engine, "backfill")
    if not (dry.report or {}).get("acceptance_ok"):
        raise AcceptanceFailed("backfill: the dry-run did not meet the acceptance thresholds: " + json.dumps((dry.report or {}).get("acceptance")))
    before = backup.table_counts(engine)
    with Session(engine) as s:
        run = _take_lease(s, "backfill")
        run.backup_manifest_hash, run.row_counts_before, run.inputs_hash = manifest_hash, before, dry.inputs_hash
        run_id, last = run.id, run.last_checkpoint or 0
        s.commit()
    done = 0
    try:
        while True:
            with Session(engine) as s:
                rows = s.scalars(select(Job).where(Job.id > last, Job.company_norm.is_(None)).order_by(Job.id).limit(batch)).all()
                if not rows:
                    break
                resolver, now = CompanyResolver(s), _now()
                for j in rows:
                    for k, v in _backfill_fields(j, resolver, now).items():
                        setattr(j, k, v)
                    done += 1
                last = rows[-1].id
                _fault("mid_backfill")
                run = s.get(MigrationRun, run_id)
                _heartbeat(run, last)
                s.commit()
        with Session(engine) as s:
            seed_aliases(s)
            s.commit()
    except BaseException as e:
        with Session(engine) as s:
            r = s.get(MigrationRun, run_id)
            r.outcome, r.finished_at, r.report = "failed", _now(), {"error": str(e)[:500], "rows_done": done, "checkpoint": last}
            s.commit()
        raise
    after = backup.table_counts(engine)
    protected_ok = all(after.get(t) == before.get(t) for t in before if t != "jobs")
    with Session(engine) as s:
        r = s.get(MigrationRun, run_id)
        r.outcome, r.finished_at, r.row_counts_after = "ok", _now(), after
        r.report = {"rows_backfilled": done, "protected_tables_unchanged": protected_ok}
        s.commit()
    if not protected_ok:
        raise AcceptanceFailed("backfill changed a protected table; investigate and restore from the backup")
    return {"step": "backfill", "rows_backfilled": done, "run_id": run_id, "protected_tables_unchanged": protected_ok}


# ================================================================== dedup apply
def golden_precision() -> dict:
    """Run the built-in labeled pairs through decide(). A 'must not merge' pair that merges is a precision failure."""
    def J(src, title, loc="Pune, India", company="acme", desc="Role description text. " * 20, **kw):
        n = normalize
        j = Job(source=src, external_id="x", company=company, title=title, location=loc, url="u", description=desc, country="India",
                remote=kw.get("remote", False), company_norm=n.company_key(company), title_norm=n.title_norm(title),
                city_norm=n.city_norm(loc), first_seen=_now())
        j.posted_at = kw.get("posted_at")
        return j
    old, new = datetime(2026, 1, 1), datetime(2026, 6, 1)
    pairs = [  # (a, b, should_merge)
        (J("greenhouse", "Software Engineer"), J("lever", "Software Engineer"), True),
        (J("greenhouse", "Software Engineer II"), J("lever", "Software Engineer II (Remote) - Pune"), True),
        (J("greenhouse", "Data Analyst", loc=""), J("lever", "Data Analyst", loc=""), True),
        (J("greenhouse", "Software Engineer II"), J("lever", "Software Engineer III"), False),
        (J("greenhouse", "Senior Data Analyst"), J("lever", "Data Analyst"), False),
        (J("greenhouse", "Software Engineer", loc="Pune, India"), J("lever", "Software Engineer", loc="Mumbai, India"), False),
        (J("greenhouse", "Software Engineer"), J("greenhouse", "Software Engineer"), False),                       # same source
        (J("greenhouse", "Software Engineer", company="acme"), J("lever", "Software Engineer", company="globex"), False),
        (J("greenhouse", "Data Analyst", loc="", desc="A. " * 150), J("lever", "Data Analyst", loc="", desc="Totally different. " * 80), False),
        (J("greenhouse", "Software Engineer", posted_at=old), J("lever", "Software Engineer", posted_at=new), False),  # re-posted months later
        (J("greenhouse", "Software Engineer Intern"), J("lever", "Software Engineer"), False),
        (J("greenhouse", "Backend Developer"), J("lever", "Frontend Developer"), False),
    ]
    tp = fp = fn = 0
    bad = []
    for i, (a, b, want) in enumerate(pairs):
        got = dedup.decide(a, b).action == "merge"
        tp += got and want; fp += got and not want; fn += (not got) and want
        if got != want:
            bad.append(i)
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    return {"pairs": len(pairs), "true_merges": tp, "false_merges": fp, "missed_merges": fn, "precision": round(precision, 4),
            "failing_pairs": bad, "ok": fp == 0 and precision >= 0.99}


def dedup_apply(engine, apply: bool = False, outdir=None, backup_dir=None) -> dict:
    if not apply:
        with Session(engine) as s:
            report = dedup.dry_run_report(s)
        gp = golden_precision()
        report.update({"step": "dedup_apply", "golden": gp, "acceptance": {"golden_precision_ok": gp["ok"]}})
        report["acceptance_ok"] = gp["ok"]
        report["run_id"] = _record_dry_run(engine, "dedup_apply", report, outdir)
        return report
    manifest_hash = backup.require_verified_backup(engine, "dedup_apply", backup_dir)
    dry = _require_dry_run(engine, "dedup_apply")
    if not (dry.report or {}).get("acceptance_ok") or not golden_precision()["ok"]:
        raise AcceptanceFailed("dedup_apply: golden-pair precision gate failed; nothing was merged")
    before = backup.table_counts(engine)
    with Session(engine) as s:
        run = _take_lease(s, "dedup_apply")
        run.backup_manifest_hash, run.row_counts_before, run.inputs_hash = manifest_hash, before, dry.inputs_hash
        run_id, last = run.id, run.last_checkpoint or 0
        s.commit()
    stats = {"processed": 0, "merged": 0, "suggested": 0, "blocked": 0}
    try:
        with Session(engine) as s:
            ids = list(s.scalars(select(Job.id).where(Job.id > last, Job.canonical_job_id.is_(None), Job.company_norm.is_not(None)).order_by(Job.id)))
        for jid in ids:
            with Session(engine) as s:
                for rec in dedup.process_job(s, jid, migration_run_id=run_id):
                    stats["merged"] += rec["action"] == "merge"
                    stats["suggested"] += rec["action"] == "suggest"
                    stats["blocked"] += rec["action"] == "blocked"
                stats["processed"] += 1
                _fault("mid_dedup")
                if stats["processed"] % 25 == 0:
                    _heartbeat(s.get(MigrationRun, run_id), jid); s.commit()
        with Session(engine) as s:
            bad = dedup.validate_invariants(s)
            if bad:
                raise dedup.InvariantViolation("; ".join(bad[:5]))
    except BaseException as e:
        with Session(engine) as s:
            r = s.get(MigrationRun, run_id)
            r.outcome, r.finished_at, r.report = "failed", _now(), {"error": str(e)[:500], **stats}
            s.commit()
        raise
    after = backup.table_counts(engine)
    protected_ok = all(after.get(t) == before.get(t) for t in before if t != "jobs")
    with Session(engine) as s:
        r = s.get(MigrationRun, run_id)
        r.outcome, r.finished_at, r.row_counts_after, r.report = "ok", _now(), after, {**stats, "protected_tables_unchanged": protected_ok}
        s.commit()
    return {"step": "dedup_apply", "run_id": run_id, **stats, "protected_tables_unchanged": protected_ok}


# ================================================================== CLI
def status(engine) -> dict:
    from app import migrations, search
    with Session(engine) as s:
        unindexed = search.unindexed_count(s)
        last_runs = [{"id": r.id, "step": r.step, "mode": r.mode, "outcome": r.outcome} for r in
                     s.scalars(select(MigrationRun).order_by(MigrationRun.id.desc()).limit(5))]
    m = backup.latest_manifest()
    return {"schema_version": migrations.current_version(engine), "target": migrations.TARGET_VERSION,
            "counts": backup.table_counts(engine), "jobs_without_v2_columns": unindexed, "recent_runs": last_runs,
            "latest_backup": None if not m else {"created_at": m["created_at"], "expires_at": m["expires_at"],
                                                 "restore_ok": m.get("restore_test", {}).get("ok")}}


def main(argv: list[str] | None = None) -> int:
    import argparse
    from app.db import engine
    from app import migrations
    p = argparse.ArgumentParser(prog="python -m app.migrations", description="JobHunter v2 maintenance")
    p.add_argument("command", choices=["status", "upgrade", "backup", "backfill", "dedup", "validate"])
    p.add_argument("--apply", action="store_true", help="apply (default is a dry-run report)")
    a = p.parse_args(argv)
    try:
        if a.command == "upgrade":
            print("applied" if migrations.ensure_schema(engine) else "already current")
        elif a.command == "status":
            print(json.dumps(status(engine), indent=2, default=str))
        elif a.command == "backup":
            m = backup.create_verified_backup(engine)
            print(json.dumps({k: m[k] for k in ("backup_path", "manifest_path", "restore_test", "expires_at")}, indent=2, default=str))
            return 0 if m["restore_test"]["ok"] else 2
        elif a.command == "backfill":
            print(json.dumps(backfill(engine, apply=a.apply), indent=2, default=str))
        elif a.command == "dedup":
            print(json.dumps(dedup_apply(engine, apply=a.apply), indent=2, default=str))
        elif a.command == "validate":
            with Session(engine) as s:
                bad = dedup.validate_invariants(s)
            print("healthy" if not bad else "\n".join(bad))
            return 0 if not bad else 2
    except (backup.BackupGateError, backup.BackupUnavailable, DryRunRequired, StepBusy, AcceptanceFailed, migrations.MigrationBusy) as e:
        print(f"BLOCKED: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
