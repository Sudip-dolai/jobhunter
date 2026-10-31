"""Scrape once (in parallel) -> score per user -> alert -> follow-up reminders.
One company's or one user's failure never stops the rest."""
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import collect as collect_mod
from app import config, lifecycle, normalize, tracker
from app.companies import CompanyResolver
from app.gmail import sync_user
from app.matcher import MatchResult, rule_check, score_job
from app.models import Job, Match, SourceRun, User
from app.notifier import send_telegram
from app.scrapers import FEED_SOURCES, fetch_jobs, feeds_due, load_companies, load_feeds, mark_feeds

log = logging.getLogger("jobhunter")


_V2_ONLY_KEYS = {"posted_at", "valid_through", "apply_url"}
_INSERT_CHUNK = 100   # jobs saved per commit: small writes survive a dropped database connection


def scrape_all(session: Session, companies=None, fetcher=fetch_jobs, workers: int = 8) -> tuple[int, list[str]]:
    """Public entry point (signature and (new, errors) return unchanged). FEATURE_V2_INGEST selects the v2 path."""
    if config.FEATURE_V2_INGEST:
        return _scrape_all_v2(session, companies, fetcher, workers)
    return _scrape_all_v1(session, companies, fetcher, workers)


def _scrape_all_v1(session: Session, companies=None, fetcher=fetch_jobs, workers: int = 8) -> tuple[int, list[str]]:
    auto = companies is None
    companies = (load_companies() + feeds_due(load_feeds())) if auto else companies
    existing, known = set(), {}
    for source, ext, company in session.execute(select(Job.source, Job.external_id, Job.company)):
        existing.add((source, ext))
        known.setdefault(company, set()).add(ext)
    session.commit()   # end the read transaction now: scraping takes minutes and hosted Postgres (Neon) closes idle connections

    def run(c):
        try:
            return c, fetcher(c, known.get(c["name"], set())), None
        except Exception as e:  # network error, wrong slug, ...
            return c, [], e

    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(run, companies))
    if auto:
        mark_feeds([c["name"] for c, _, err in results if err is None and c.get("source") in FEED_SOURCES])
    new, errors, rows = 0, [], []
    for c, jobs, err in results:
        if err:
            errors.append(f"{c.get('name')}: {err}")
            continue
        for j in jobs:
            key = (j["source"], j["external_id"])
            if key not in existing:
                existing.add(key)
                rows.append({k: v for k, v in j.items() if k not in _V2_ONLY_KEYS})   # v1 path never fills v2 columns
    for i in range(0, len(rows), _INSERT_CHUNK):
        chunk = rows[i:i + _INSERT_CHUNK]
        for attempt in (1, 2):
            try:
                session.add_all([Job(**r) for r in chunk])
                session.commit()
                new += len(chunk)
                break
            except OperationalError as e:   # dropped connection: start a fresh transaction and retry this batch once
                session.rollback()
                if attempt == 2:
                    errors.append(f"saving {len(chunk)} jobs: {str(e.orig).strip()[:120]}")
                    log.warning("could not save a batch of %d jobs: %s", len(chunk), e.orig)
                else:
                    time.sleep(2)
    return new, errors


# ------------------------------------------------------------------ v2 ingest
_LEGACY_LIMITS = {"source": 30, "external_id": 100, "company": 120, "title": 300, "location": 200, "url": 600,
                  "sector": 60, "job_type": 20, "country": 160}
_LEGACY_FIELDS = ("source", "external_id", "company", "title", "location", "url", "description", "sector", "remote",
                  "job_type", "country", "salary_min", "salary_max")
_UPDATABLE = ("title", "location", "url", "description", "sector", "remote", "job_type", "country", "salary_min", "salary_max")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _clean_legacy(j: dict) -> dict:
    out = {k: j.get(k) for k in _LEGACY_FIELDS if k in j}
    for k, n in _LEGACY_LIMITS.items():
        if k in out:
            out[k] = normalize.clamp(out[k], n)
    out["description"] = normalize.clamp(out.get("description", ""), 10 ** 7)
    return out


def _as_result(c: dict, out) -> "collect_mod.CollectResult":
    """Accept a CollectResult, or a plain job list from legacy/test fetchers (those are never complete)."""
    if isinstance(out, collect_mod.CollectResult):
        return out
    return collect_mod.CollectResult(jobs=list(out), notes=["legacy fetcher: completeness unknown"])


def _collect_one(c: dict, known: set, fetcher) -> "collect_mod.CollectResult":
    try:
        if fetcher is fetch_jobs:
            return collect_mod.collect(c, known)
        return _as_result(c, fetcher(c, known))
    except Exception as e:                                   # a fetcher that raises is a failed run, never a closed board
        outcome, code = collect_mod.classify_error(e)
        return collect_mod.CollectResult(outcome=outcome, http_status=code, error=str(e)[:500])


def _upsert_company(session: Session, c: dict, res, run: SourceRun, resolver: CompanyResolver, now: datetime) -> tuple[list[int], int]:
    """Insert new jobs, update changed ones. One company = one transaction (the caller commits)."""
    src = c["source"]
    cleaned = [_clean_legacy(j) | {"_raw": j} for j in res.jobs]
    ids = [j["external_id"] for j in cleaned]
    have: dict[str, Job] = {}
    for i in range(0, len(ids), 500):
        for row in session.scalars(select(Job).where(Job.source == src, Job.external_id.in_(ids[i:i + 500]))):
            have[row.external_id] = row
    new_ids, updated = [], 0
    for j in cleaned:
        raw = j.pop("_raw")
        legacy = {k: v for k, v in j.items()}
        company_id = resolver.resolve(legacy["company"], legacy.get("sector"))
        row = have.get(legacy["external_id"])
        extras = dict(raw)                                   # posted_at / apply_url / valid_through travel in the raw dict
        derived_in = {**legacy, **{k: extras.get(k) for k in ("posted_at", "valid_through", "apply_url") if extras.get(k)}}
        if row is None:
            derived = normalize.derived_fields(derived_in, company_id=company_id, now=now)
            derived["last_run_id"] = run.id
            row = Job(**legacy, **derived)
            session.add(row)
            session.flush()
            new_ids.append(row.id)
            have[legacy["external_id"]] = row
        else:
            h = normalize.content_hash(legacy)
            if row.content_hash != h:
                for k in _UPDATABLE:
                    if k in legacy:
                        setattr(row, k, legacy[k])
                derived = normalize.derived_fields({**derived_in, "first_seen": row.first_seen}, company_id=company_id, now=now)
                for k, v in derived.items():
                    if k not in ("status", "miss_count", "last_seen_at"):    # lifecycle owns these
                        setattr(row, k, v)
                updated += 1
    run.new_count, run.updated_count = len(new_ids), updated
    return new_ids, updated


def _scrape_all_v2(session: Session, companies=None, fetcher=fetch_jobs, workers: int = 8) -> tuple[int, list[str]]:
    auto = companies is None
    if auto:
        feeds = feeds_due(load_feeds()) if config.ENABLE_FEEDS else []
        companies = load_companies() + feeds
    known: dict = {}
    for ext, company in session.execute(select(Job.external_id, Job.company)):
        known.setdefault(company, set()).add(ext)

    def run(c):
        return c, _collect_one(c, known.get(c["name"], set()), fetcher), _now()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(run, companies))
    if auto:
        mark_feeds([c["name"] for c, r, _ in results if r.outcome == "ok" and c.get("source") in FEED_SOURCES])
    resolver = CompanyResolver(session)
    new, errors, new_ids = 0, [], []
    for c, res, started in results:
        try:
            now = _now()
            run_row = lifecycle.record_run(session, c, res, started, now)
            if res.outcome == "skipped":
                session.commit(); continue
            if res.outcome != "ok":
                errors.append(f"{c.get('name')}: {res.error or res.outcome}")
                session.commit(); continue
            ids, _ = _upsert_company(session, c, res, run_row, resolver, now)
            lifecycle.apply_lifecycle(session, c, run_row, res, now)
            session.commit()
            new += len(ids)
            new_ids += ids
        except Exception as e:                                # one company's database failure never stops the rest
            session.rollback()
            errors.append(f"{c.get('name')}: {e}")
            log.warning("ingest failed for %s: %s", c.get("name"), e)
            try:
                session.add(SourceRun(source=c.get("source", "?"), company_key=c.get("name", "?"), outcome="error", error=str(e)[:500]))
                session.commit()
            except Exception:
                session.rollback()
    if config.FEATURE_V2_DEDUP and new_ids:
        try:
            from app import dedup
            dedup.process_new_jobs(session, new_ids)
            dedup.refresh_roots(session, new_ids)
        except Exception as e:
            session.rollback()
            errors.append(f"dedup: {e}")
    return new, errors


def match_user(session: Session, user: User, scorer=score_job, notify=send_telegram, max_jobs=None) -> int:
    """Rule-rejected jobs are free. Only eligible jobs count toward the per-run scoring cap (LLM cost)."""
    profile = user.profile
    if not profile or not (profile.resume_text or profile.skills):
        return 0
    cap = max_jobs or config.MAX_JOBS_PER_USER_PER_RUN
    done = select(Match.job_id).where(Match.user_id == user.id)
    q = select(Job).where(Job.id.not_in(done))
    if config.FEATURE_V2_MATCHING:
        # canonical rows only, never expired/stale jobs, and never a group the user was already matched on
        matched_groups = select(func.coalesce(Job.canonical_job_id, Job.id)).where(Job.id.in_(done))
        q = q.where(Job.canonical_job_id.is_(None), or_(Job.status.is_(None), Job.status == "active"),
                    Job.id.not_in(matched_groups))
    watch = [w for w in (profile.watch_companies or []) if w.strip()]
    if watch:
        q = q.where(Job.company.in_(watch))
    sectors = [x for x in (profile.sectors or []) if x]
    if sectors:
        q = q.where(or_(Job.sector.in_(sectors), Job.sector == "", Job.sector.is_(None), Job.sector.like("Discovered%")))   # startups found by harvest always count
    q = q.order_by(Job.first_seen.desc(), Job.id.desc()).limit(5000)
    count = paid = 0
    pending: list[str] = []      # alerts are sent only after their batch is saved, so a crash never double-alerts

    def flush() -> None:
        session.commit()
        for msg in pending:
            notify(user.telegram_chat_id, msg)
        pending.clear()

    for job in session.scalars(q).all():
        ok, why = rule_check(profile, job)
        if not ok:
            res = MatchResult(score=0, eligible=False, reason=why)
        elif paid >= cap:
            continue  # scored on a later run
        else:
            res = scorer(profile, job)
            paid += 1
        session.add(Match(user_id=user.id, job_id=job.id, **res.model_dump()))
        count += 1
        if res.eligible and res.score >= (profile.min_score or 60):
            sal = f"\nSalary: {job.salary_min:g}-{job.salary_max:g} LPA" if job.salary_min is not None else ""
            sal += f"\n{job.job_type or 'Not stated'}{' | Remote' if job.remote else ''}{' | ' + job.location if job.location else ''}"
            pending.append(f"New match {res.score}%: {job.title} at {job.company}{sal}\n{(job.apply_url or job.url) if config.FEATURE_V2_MATCHING else job.url}\n"
                           f"Missing: {', '.join(res.missing_skills) or 'none'}")
        if count % 100 == 0:     # one database round trip per 100 jobs instead of one per job
            flush()
    flush()
    return count


def reevaluate_matches(session: Session, user: User) -> int:
    """After a profile change: re-apply the free rules to existing matches. Newly ineligible ones are marked, and previously
    rejected ones are dropped so the next run scores them again under the new settings. Saved applications are untouched."""
    profile = user.profile
    if not profile:
        return 0
    changed = 0
    for m in list(session.scalars(select(Match).where(Match.user_id == user.id))):
        if not m.eligible:
            session.delete(m)
            changed += 1
            continue
        ok, why = rule_check(profile, m.job)
        if not ok:
            m.eligible, m.score, m.reason = False, 0, why
            changed += 1
    session.commit()
    return changed


def send_reminders(session: Session, notify=send_telegram) -> int:
    sent = 0
    for app in tracker.due_followups(session):
        user = session.get(User, app.user_id)
        notify(user.telegram_chat_id, f"Follow-up reminder: no update on {app.job.title} at {app.job.company}. Consider a nudge.")
        tracker.bump_followup(session, app)
        sent += 1
    return sent


def run_all(session: Session, **kw) -> dict:
    kw.setdefault("workers", config.SCRAPE_WORKERS)   # public default stays 8; real runs use SCRAPE_WORKERS (default 16)
    new, errors = scrape_all(session, **kw)
    if config.FEATURE_V2_INGEST:
        try:
            lifecycle.verification_pass(session)
            session.commit()
        except Exception as e:
            session.rollback()
            errors.append(f"verification: {e}")
    matched = 0
    for user in session.scalars(select(User)).all():
        try:
            matched += match_user(session, user)
        except Exception as e:
            session.rollback()
            errors.append(f"user {user.id}: {e}")
    emails = 0
    for user in session.scalars(select(User)).all():
        try:
            emails += sync_user(session, user)
        except Exception as e:  # bad Gmail password etc. never stops the others
            session.rollback()
            errors.append(f"gmail user {user.id}: {e}")
    reminders = send_reminders(session)
    return {"new_jobs": new, "scored": matched, "emails_updated": emails, "reminders": reminders, "errors": errors}