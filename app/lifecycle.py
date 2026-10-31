"""Job lifecycle: run log, stale / expired, reopen. Evidence-based; failed or partial runs never change status.

Terms (plan section 8):
  complete run : source_runs row with outcome 'ok', complete=True, not suspect
  miss         : a job of that source+company, currently active/stale, absent from the listed ids of a complete run
  stale        : exactly the FIRST miss
  expired      : miss_count >= EXPIRY_MISS_THRESHOLD (3, in separate complete runs) AND >= EXPIRY_MIN_HOURS (48h)
                 since first_missed_at AND every safeguard passes
Modes: off | dry_run (bookkeeping + would_expire events, status never changes) | auto | enforce.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import backup, config, sources
from app.models import Job, JobEvent, SourceRun

log = logging.getLogger("jobhunter.lifecycle")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ------------------------------------------------------------------ run log
def record_run(session: Session, company: dict, result, started: datetime | None = None, now: datetime | None = None) -> SourceRun:
    """Write the source_runs row. Applies the shrink guard: a suddenly much smaller board is 'suspect', not complete."""
    now = now or _now()
    key = company.get("name", "")
    listed = len(result.listed_ids) if result.listed_ids is not None else None
    complete, suspect, notes = bool(result.complete and result.outcome == "ok"), False, list(result.notes)
    if complete and not sources.spec(company["source"]).may_be_complete:     # the registry has the last word
        complete = False
        notes.append("registry: source is not verified for completeness")
    if complete and listed is not None:
        prev = session.scalars(select(SourceRun).where(
            SourceRun.source == company["source"], SourceRun.company_key == key, SourceRun.complete.is_(True),
            SourceRun.suspect.is_(False)).order_by(SourceRun.id.desc()).limit(1)).first()
        if prev and (prev.listed_count or 0) >= 10 and listed < config.EXPIRY_SHRINK_RATIO * prev.listed_count:
            complete, suspect = False, True
            notes.append(f"SG3 shrink guard: {listed} listed vs {prev.listed_count} previously")
    run = SourceRun(source=company["source"], company_key=key, started_at=started or now, finished_at=now,
                    outcome=result.outcome, complete=complete, suspect=suspect, shadow_complete=result.shadow_complete,
                    listed_count=listed, expected_total=result.expected_total, pages_fetched=result.pages_fetched,
                    http_status=result.http_status, error=("; ".join([result.error] + notes))[:2000] if (result.error or notes) else "")
    session.add(run)
    session.flush()
    return run


# ------------------------------------------------------------------ gates
def expiry_gate(session: Session, source: str, now: datetime | None = None) -> tuple[bool, dict]:
    """Automatic (no human approval) criteria for enforcing expiry on one source (plan section 8)."""
    now = now or _now()
    complete = session.scalar(select(func.count()).select_from(SourceRun).where(SourceRun.source == source, SourceRun.complete.is_(True)))
    first = session.scalar(select(func.min(SourceRun.started_at)).where(SourceRun.source == source, SourceRun.complete.is_(True)))
    suspects = session.scalar(select(func.count()).select_from(SourceRun).where(SourceRun.source == source, SourceRun.suspect.is_(True)))
    would = session.scalar(select(func.count()).select_from(JobEvent).where(JobEvent.event_type == "would_expire", JobEvent.reason.like(f"{source}:%")))
    reappeared = session.scalar(select(func.count()).select_from(JobEvent).where(
        JobEvent.event_type == "would_expire", JobEvent.reason.like(f"{source}:%"), JobEvent.undone_at.is_not(None)))
    rate = (reappeared / would) if would else 0.0
    rep = {"source": source, "complete_runs": complete, "first_complete_run": first.isoformat() if first else None,
           "suspect_runs": suspects, "would_expire_events": would, "reappeared": reappeared, "reappearance_rate": round(rate, 4)}
    checks = {"completeness verified in registry": sources.spec(source).may_be_complete,
              ">= 7 days of observation": bool(first and (now - first) >= timedelta(days=7)),
              ">= 20 complete runs": complete >= 20,
              "reappearance rate <= 0.5%": rate <= 0.005,
              "no unexplained suspect runs": suspects == 0 or suspects <= max(1, complete // 20)}
    rep["checks"] = checks
    return all(checks.values()), rep


_GATE_CACHE: dict = {}      # fingerprint -> monotonic deadline of a passed backup gate (hashing the backup is not free)


def _backup_gate_cached(engine) -> None:
    fp = backup.fingerprint(engine)
    if _GATE_CACHE.get(fp, 0) > time.monotonic():
        return
    backup.require_verified_backup(engine, "expiry_enforce")
    _GATE_CACHE[fp] = time.monotonic() + 300


def effective_mode(session: Session, source: str) -> tuple[str, str]:
    """Resolve EXPIRY_MODE into what actually happens now. Returns (mode, reason)."""
    mode = config.EXPIRY_MODE
    if mode in ("off", "dry_run"):
        return mode, "configured"
    if mode == "auto":
        ok, rep = expiry_gate(session, source)
        if not ok:
            return "dry_run", "auto: gate not passed " + str([k for k, v in rep["checks"].items() if not v])
    try:
        _backup_gate_cached(session.get_bind())
    except backup.BackupGateError as e:
        return "dry_run", f"enforcement blocked: {e}"
    return "enforce", "gate passed" if mode == "auto" else "forced"


# ------------------------------------------------------------------ applying a run
def _event(session, job, etype, frm, to, reason, run_id=None, evidence=None):
    session.add(JobEvent(job_id=job.id, event_type=etype, from_value=frm, to_value=to, reason=reason[:60], run_id=run_id, evidence=evidence))


def _reopen(session, job, run_id, now):
    was = job.status
    job.status, job.status_reason, job.closed_at, job.miss_count, job.first_missed_at = "active", None, None, 0, None
    if was in ("stale", "expired"):
        _event(session, job, "reopen", was, "active", "seen_again", run_id)


def apply_lifecycle(session: Session, company: dict, run: SourceRun, result, now: datetime | None = None) -> dict:
    """Update last_seen / reopen for everything seen, and (complete runs only) count misses. Returns a summary."""
    now = now or _now()
    summary = {"touched": 0, "reopened": 0, "stale": 0, "expired": 0, "would_expire": 0, "held": "", "mode": ""}
    if run.outcome != "ok":                                   # SG2: failed / blocked / robots: no change at all
        return summary
    src, name = company["source"], company["name"]
    seen = set(result.listed_ids or set()) | {j["external_id"] for j in result.jobs}
    if seen:
        ids = list(seen)
        for i in range(0, len(ids), 500):
            rows = session.scalars(select(Job).where(Job.source == src, Job.external_id.in_(ids[i:i + 500]))).all()
            for job in rows:
                if job.status in ("stale", "expired") or (job.miss_count or 0) > 0:
                    if job.status in ("stale", "expired"):
                        summary["reopened"] += 1
                    mark_reappeared_would_expire(session, job)
                    _reopen(session, job, run.id, now)
                job.last_seen_at, job.last_run_id = now, run.id
                summary["touched"] += 1
    if not (run.complete and not run.suspect and result.listed_ids is not None) or config.EXPIRY_MODE == "off":
        return summary                                       # SG1: only complete runs produce misses
    mode, why = effective_mode(session, src)
    summary["mode"] = mode
    cands = session.scalars(select(Job).where(Job.source == src, Job.company == name,
                                              (Job.status.is_(None)) | (Job.status.in_(("active", "stale"))))).all()
    absent = [j for j in cands if j.external_id not in result.listed_ids]
    history = session.scalar(select(func.count()).select_from(SourceRun).where(
        SourceRun.source == src, SourceRun.company_key == name, SourceRun.complete.is_(True), SourceRun.suspect.is_(False))) or 0
    to_expire = []
    for job in absent:
        job.miss_count = (job.miss_count or 0) + 1
        if job.first_missed_at is None:
            job.first_missed_at = now
        if mode == "enforce" and job.status in (None, "active"):          # first miss => stale
            job.status, job.status_reason = "stale", "missing_from_board"
            _event(session, job, "status", "active", "stale", f"{src}:missing_from_board", run.id)
            summary["stale"] += 1
        hours = (now - job.first_missed_at) / timedelta(hours=1)
        if job.miss_count >= config.EXPIRY_MISS_THRESHOLD and hours >= config.EXPIRY_MIN_HOURS and history >= config.EXPIRY_MIN_HISTORY_RUNS:
            to_expire.append(job)
    # SG4 mass-expiry guard
    active_total = len(cands)
    if len(to_expire) > 5 and active_total and len(to_expire) / active_total > config.EXPIRY_MASS_GUARD_PCT:
        last = session.scalars(select(SourceRun.listed_count).where(
            SourceRun.source == src, SourceRun.company_key == name, SourceRun.complete.is_(True),
            SourceRun.suspect.is_(False)).order_by(SourceRun.id.desc()).limit(3)).all()
        stable = len(last) == 3 and None not in last and max(last) and (max(last) - min(last)) / max(last) <= 0.05
        if not stable:
            summary["held"] = f"SG4 mass-expiry guard: {len(to_expire)} of {active_total} held"
            to_expire = []
    for job in to_expire[:config.EXPIRY_MAX_PER_RUN]:                     # SG6 per-run cap
        if mode == "enforce":
            _event(session, job, "status", job.status or "active", "expired", f"{src}:missing_from_board", run.id,
                   {"miss_count": job.miss_count, "first_missed_at": job.first_missed_at.isoformat()})
            job.status, job.status_reason, job.closed_at = "expired", "missing_from_board", now
            summary["expired"] += 1
        else:                                                              # dry_run: record what WOULD happen, once per job
            already = session.scalar(select(func.count()).select_from(JobEvent).where(
                JobEvent.job_id == job.id, JobEvent.event_type == "would_expire", JobEvent.undone_at.is_(None)))
            if not already:
                _event(session, job, "would_expire", job.status or "active", "expired", f"{src}:missing_from_board", run.id,
                       {"miss_count": job.miss_count, "first_missed_at": job.first_missed_at.isoformat(), "mode_reason": why})
                summary["would_expire"] += 1
    return summary


def mark_reappeared_would_expire(session: Session, job: Job) -> None:
    """A job that had a would_expire event but is listed again: record it so the gate sees a false positive."""
    for ev in session.scalars(select(JobEvent).where(JobEvent.job_id == job.id, JobEvent.event_type == "would_expire",
                                                     JobEvent.undone_at.is_(None))):
        ev.undone_at = _now()


# ------------------------------------------------------------------ direct URL verification (partial sources)
VERIFY_SOURCES = ("workday", "smartrecruiters", "page", "jsonld")


def default_probe(url: str) -> int | None:
    """Polite probe of ONE job URL: robots.txt checked, identifying user agent, per-host throttle and block memory.
    Returns the HTTP status, or None when nothing reliable was learned (robots denied, blocked, network error).
    No proxies, no retries under another identity."""
    from app import http, scrapers
    try:
        if not scrapers._robots_ok(url):
            return None
        r = http.polite_get(url, timeout=15, stream=True, allow_redirects=False)
        code = r.status_code
        r.close()
        return code
    except http.BlockedError:
        return None
    except Exception:
        return None


def verification_pass(session: Session, probe=default_probe, max_probes: int | None = None, per_job_hours: float = 12,
                      sleep=time.sleep, delay: float = 1.0, now: datetime | None = None) -> dict:
    """Probe job URLs of partial sources. 404/410 counts as ONE miss; anything else changes nothing.
    The same three-misses-and-48-hours rule applies, and only in enforce mode do statuses change."""
    now = now or _now()
    max_probes = max_probes if max_probes is not None else config.VERIFY_MAX_PROBES
    out = {"probed": 0, "misses": 0, "expired": 0}
    if config.EXPIRY_MODE == "off":
        return out
    due = session.scalars(select(Job).where(
        Job.source.in_(VERIFY_SOURCES), (Job.status.is_(None)) | (Job.status.in_(("active", "stale"))), Job.url != "")
        .order_by(Job.last_seen_at.asc().nulls_first(), Job.id).limit(max_probes * 4)).all()
    for job in due:
        if out["probed"] >= max_probes:
            break
        meta_key = f"verify:{job.id}"
        last = session.scalar(select(func.max(JobEvent.created_at)).where(JobEvent.job_id == job.id, JobEvent.event_type == "probe",
                                                                           JobEvent.reason == meta_key))
        if last and (now - last) < timedelta(hours=per_job_hours):
            continue
        code = probe(job.url)
        out["probed"] += 1
        session.add(JobEvent(job_id=job.id, event_type="probe", reason=meta_key, to_value=str(code), created_at=now))
        if code in (404, 410):
            out["misses"] += 1
            job.miss_count = (job.miss_count or 0) + 1
            job.first_missed_at = job.first_missed_at or now
            mode, _ = effective_mode(session, job.source)
            hours = (now - job.first_missed_at) / timedelta(hours=1)
            if mode == "enforce" and job.status in (None, "active"):
                job.status, job.status_reason = "stale", f"http_{code}"
                _event(session, job, "status", "active", "stale", f"{job.source}:http_{code}")
            if job.miss_count >= config.EXPIRY_MISS_THRESHOLD and hours >= config.EXPIRY_MIN_HOURS:
                if mode == "enforce":
                    _event(session, job, "status", job.status or "active", "expired", f"{job.source}:http_{code}")
                    job.status, job.status_reason, job.closed_at = "expired", f"http_{code}", now
                    out["expired"] += 1
                else:
                    _event(session, job, "would_expire", job.status or "active", "expired", f"{job.source}:http_{code}")
        elif code is not None and code < 400:                  # still there: reset the miss counter
            if (job.miss_count or 0) > 0 or job.status == "stale":
                _reopen(session, job, None, now)
            job.last_seen_at = now
        sleep(delay)
    return out
