"""Cross-source duplicate grouping (plan section 6).

Groups are one level deep: a CANONICAL row has canonical_job_id NULL, a member row points at a canonical row.
Every operation touches ONLY jobs.canonical_job_id and appends job_events. It never writes matches, applications,
contacts or email_events, so every user relationship is preserved by construction.

Invariants (checked after every operation and by validate_invariants):
  I1 a canonical row has NULL pointer; a member points to a canonical row (depth <= 1)
  I2 no self pointer and no missing target
  I3 all members of a group share one company
  I4 a canonical row never points anywhere (so cycles cannot exist)
  I5 no two members of one group come from the same source (same-source listings are distinct requisitions)
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app import config, sources
from app.models import Job, JobEvent
from app.normalize import jaccard, locations_compatible, shingles, title_similarity

log = logging.getLogger("jobhunter.dedup")


class DedupError(RuntimeError):
    pass


class InvariantViolation(DedupError):
    pass


class UnmergeRefused(DedupError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ------------------------------------------------------------------ helpers
def election_key(j: Job):
    """Lowest wins: highest source trust, then an apply URL, then the longer description, earliest first_seen, lowest id."""
    return (-sources.trust_of(j.source), 0 if (j.apply_url or j.url) else 1, -len(j.description or ""),
            j.first_seen or datetime.max, j.id)


def root_of(session: Session, job: Job) -> Job:
    return job if job.canonical_job_id is None else session.get(Job, job.canonical_job_id)


def group_members(session: Session, root_id: int) -> list[Job]:
    return list(session.scalars(select(Job).where(Job.canonical_job_id == root_id).order_by(Job.id)))


def group_sources(session: Session, root: Job) -> set:
    return {root.source} | {m.source for m in group_members(session, root.id)}


def _lock(session: Session, ids: list[int], company_key: str | None = None) -> None:
    """Serialize work on these rows. PostgreSQL: transaction-scoped advisory lock per company, then row locks in id order.
    SQLite: a no-op write takes the database write lock (single writer). Everything is re-read afterwards."""
    ids = sorted(set(i for i in ids if i))
    name = session.get_bind().dialect.name
    if name == "postgresql":
        if company_key:
            session.execute(text("SELECT pg_advisory_xact_lock(7406, hashtext(:k))"), {"k": company_key})
        session.execute(select(Job.id).where(Job.id.in_(ids)).order_by(Job.id).with_for_update())
    else:
        session.execute(update(Job).where(Job.id.in_(ids)).values(canonical_job_id=Job.canonical_job_id)
                        .execution_options(synchronize_session=False))


def _reload(session: Session, ids: list[int]) -> dict[int, Job]:
    rows = session.scalars(select(Job).where(Job.id.in_(ids)).execution_options(populate_existing=True)).all()
    return {r.id: r for r in rows}


def validate_invariants(session: Session, ids: list[int] | None = None) -> list[str]:
    """Return a list of violations (empty = healthy). With ids, only the groups touching those rows are examined."""
    q = select(Job.id, Job.canonical_job_id, Job.company_norm, Job.source)
    if ids is None:
        rows = {r[0]: r for r in session.execute(q)}
    else:
        scope = set(ids)
        rows = {r[0]: r for r in session.execute(q.where(Job.id.in_(scope) | Job.canonical_job_id.in_(scope)))}
        roots = {(r[1] if r[1] is not None else r[0]) for r in rows.values()}
        rows.update({r[0]: r for r in session.execute(q.where(Job.id.in_(roots) | Job.canonical_job_id.in_(roots)))})
        missing = {r[1] for r in rows.values() if r[1] is not None and r[1] not in rows}
        if missing:
            rows.update({r[0]: r for r in session.execute(q.where(Job.id.in_(missing)))})
    out = []
    members_by_root: dict[int, list] = {}
    for jid, ptr, comp, src in rows.values():
        if ptr is None:
            continue
        if ptr == jid:
            out.append(f"I2 job {jid} points to itself"); continue
        tgt = rows.get(ptr)
        if tgt is None:
            out.append(f"I2 job {jid} points to missing job {ptr}"); continue
        if tgt[1] is not None:
            out.append(f"I1/I4 job {jid} points to {ptr} which itself points to {tgt[1]}")
        if (comp or "") != (tgt[2] or ""):
            out.append(f"I3 job {jid} and its canonical {ptr} have different companies")
        members_by_root.setdefault(ptr, []).append((jid, src))
    for root, members in members_by_root.items():
        srcs = ([rows[root][3]] if root in rows else []) + [s_ for _, s_ in members]
        if len(srcs) != len(set(srcs)):
            out.append(f"I5 group {root} has two listings from the same source")
    return out


def _check(session: Session, ids: list[int]) -> None:
    bad = validate_invariants(session, ids)
    if bad:
        raise InvariantViolation("; ".join(bad[:5]))


def _log(session: Session, job_id: int, etype: str, before, after, reason: str, gid: str, score=None, related=None,
         run_id=None, migration_run_id=None, evidence=None) -> None:
    session.add(JobEvent(job_id=job_id, event_type=etype, from_value="NULL" if before is None else str(before),
                         to_value="NULL" if after is None else str(after), reason=reason[:60], score=score,
                         related_job_id=related, run_id=run_id, migration_run_id=migration_run_id,
                         group_event_id=gid, evidence=evidence))


def _set_pointers(session, changes: dict[int, int | None], jobs: dict[int, Job], etype, reason, gid, **kw) -> None:
    for jid, new in changes.items():
        j = jobs[jid]
        if j.canonical_job_id != new:
            _log(session, jid, etype, j.canonical_job_id, new, reason, gid, **kw)
            j.canonical_job_id = new


# ------------------------------------------------------------------ operations
def merge(session: Session, a_id: int, b_id: int, *, method: str, score: float | None = None, evidence: dict | None = None,
          run_id=None, migration_run_id=None, commit: bool = True) -> dict:
    """Merge the groups of two listings in ONE transaction. Idempotent. Raises DedupError if a rule forbids it."""
    if a_id == b_id:
        return {"action": "noop", "reason": "same row"}
    a0, b0 = session.get(Job, a_id), session.get(Job, b_id)
    if not a0 or not b0:
        raise DedupError("unknown job id")
    ids0 = [a_id, b_id] + [x for x in (a0.canonical_job_id, b0.canonical_job_id) if x]
    _lock(session, ids0, a0.company_norm)
    jobs = _reload(session, ids0)
    a, b = jobs[a_id], jobs[b_id]
    need = [x for x in (a.canonical_job_id, b.canonical_job_id) if x and x not in jobs]
    if need:                                   # a pointer moved between the first read and the lock: re-read, once
        _lock(session, need, a.company_norm)
        jobs.update(_reload(session, need))
        if any(x not in jobs or jobs[x].canonical_job_id is not None for x in (a.canonical_job_id, b.canonical_job_id) if x):
            session.rollback()
            raise DedupError("group changed concurrently; retry")
    ra = jobs[a.canonical_job_id] if a.canonical_job_id else a
    rb = jobs[b.canonical_job_id] if b.canonical_job_id else b
    if ra.id == rb.id:
        if commit:
            session.rollback()
        return {"action": "noop", "reason": "already grouped", "root": ra.id}
    # rules that can never be overridden
    if (ra.company_norm or "") != (rb.company_norm or "") or not ra.company_norm:
        session.rollback(); raise DedupError("different companies never merge")
    if group_sources(session, ra) & group_sources(session, rb):
        session.rollback(); raise DedupError("groups share a source: same-source listings are distinct requisitions")
    if (ra.status == "expired") != (rb.status == "expired"):
        session.rollback(); raise DedupError("an expired and a live listing never merge")
    winner, loser = sorted([ra, rb], key=election_key)
    members = group_members(session, loser.id)
    jobs.update(_reload(session, [m.id for m in members] + [winner.id, loser.id]))
    gid = uuid.uuid4().hex
    changes = {loser.id: winner.id, **{m.id: winner.id for m in members}}
    _set_pointers(session, changes, jobs, "merge", f"{method}", gid, score=score, related=winner.id,
                  run_id=run_id, migration_run_id=migration_run_id, evidence=evidence)
    session.flush()
    try:
        _check(session, list(changes))
    except InvariantViolation:
        session.rollback()
        raise
    if commit:
        session.commit()
    return {"action": "merged", "root": winner.id, "absorbed": [loser.id] + [m.id for m in members], "group_event_id": gid}


def promote(session: Session, group_member_id: int, *, reason: str = "higher_trust", run_id=None, commit: bool = True) -> dict:
    """Make the best-ranked listing of a group the canonical one (no-op if it already is). Pointers only."""
    seed = session.get(Job, group_member_id)
    root_id = seed.canonical_job_id or seed.id
    first_ids = [root_id] + [m.id for m in group_members(session, root_id)]
    _lock(session, first_ids, seed.company_norm)
    jobs = _reload(session, first_ids)
    members = [jobs[i] for i in first_ids]
    best = min(members, key=election_key)
    if best.id == root_id:
        if commit:
            session.rollback()
        return {"action": "noop", "root": root_id}
    gid = uuid.uuid4().hex
    changes = {best.id: None, **{m.id: best.id for m in members if m.id != best.id}}
    _set_pointers(session, changes, jobs, "promote", reason, gid, related=best.id, run_id=run_id)
    session.flush()
    try:
        _check(session, list(changes))
    except InvariantViolation:
        session.rollback()
        raise
    if commit:
        session.commit()
    return {"action": "promoted", "root": best.id, "old_root": root_id, "group_event_id": gid}


def unmerge(session: Session, group_event_id: str, *, commit: bool = True) -> dict:
    """Undo ONE merge/promote operation exactly. Refuses if any affected row changed since (undo newer events first)."""
    evs = list(session.scalars(select(JobEvent).where(JobEvent.group_event_id == group_event_id,
                                                      JobEvent.event_type.in_(("merge", "promote"))).order_by(JobEvent.id)))
    if not evs:
        raise UnmergeRefused("no such merge/promote operation")
    if any(e.undone_at is not None for e in evs):
        raise UnmergeRefused("this operation was already undone")
    ids = [e.job_id for e in evs]
    first = session.get(Job, ids[0])
    _lock(session, ids, first.company_norm if first else None)
    jobs = _reload(session, ids)
    for e in evs:
        cur = jobs[e.job_id].canonical_job_id
        if ("NULL" if cur is None else str(cur)) != e.to_value:
            session.rollback()
            raise UnmergeRefused(f"job {e.job_id} changed since this operation (now {cur}); undo newer operations first")
    gid = uuid.uuid4().hex
    now = _now()
    for e in evs:
        j = jobs[e.job_id]
        before = None if e.from_value == "NULL" else int(e.from_value)
        _log(session, j.id, "unmerge", j.canonical_job_id, before, f"undo:{e.event_type}", gid, related=e.related_job_id)
        j.canonical_job_id = before
        e.undone_at = now
    session.flush()
    try:
        _check(session, ids)
    except InvariantViolation:
        session.rollback()
        raise
    if commit:
        session.commit()
    return {"action": "unmerged", "restored": ids, "group_event_id": gid}


# ------------------------------------------------------------------ decisions
@dataclass
class Decision:
    action: str = "none"            # merge | suggest | none
    method: str = ""
    score: float = 0.0
    evidence: dict = field(default_factory=dict)


def decide(a: Job, b: Job) -> Decision:
    """Conservative pairwise decision between two canonical rows (plan section 5). Weak evidence never merges."""
    if a.source == b.source or not a.company_norm or a.company_norm != b.company_norm:
        return Decision()
    from app.normalize import level_tokens
    if level_tokens(a.title or "") != level_tokens(b.title or ""):
        return Decision()
    if a.posted_at and b.posted_at and abs((a.posted_at - b.posted_at).days) > 60:
        return Decision(evidence={"why": "posted dates differ by more than 60 days"})
    loc = locations_compatible(a.city_norm or "", b.city_norm or "", bool(a.remote), bool(b.remote), a.country or "", b.country or "")
    both_blank = not (a.city_norm and b.city_norm)
    da, db = a.description or "", b.description or ""
    long_enough = len(da) >= config.DEDUP_MIN_DESC_CHARS and len(db) >= config.DEDUP_MIN_DESC_CHARS
    desc = jaccard(shingles(da), shingles(db)) if long_enough else None
    tsim = 1.0 if (a.title_norm == b.title_norm and a.title_norm) else title_similarity(a.title_norm or "", b.title_norm or "")
    ev = {"title_similarity": round(tsim, 4), "desc_jaccard": None if desc is None else round(desc, 4),
          "location_compatible": loc, "a_city": a.city_norm, "b_city": b.city_norm}
    if tsim == 1.0 and loc:
        return Decision("merge", "key", 1.0, ev)                                  # Level 1: same normalized key
    if tsim == 1.0 and both_blank and not (a.remote and b.remote):
        if desc is not None and desc >= config.DEDUP_DESC_SIM:                     # blank location needs the description
            return Decision("merge", "key+desc", desc, ev)
        return Decision("suggest", "blank_location", tsim, ev)
    if tsim >= config.DEDUP_TITLE_SIM and loc and desc is not None and desc >= config.DEDUP_DESC_SIM:
        return Decision("merge", "fuzzy", min(tsim, desc), ev)                     # Level 2: cautious fuzzy
    if tsim >= 0.85 and loc:
        return Decision("suggest", "similar", tsim, ev)
    return Decision()


def candidates_for(session: Session, job: Job) -> list[Job]:
    """Canonical rows of the same company (different source), not expired, resolved from members to roots."""
    if not job.company_norm:
        return []
    rows = session.scalars(select(Job).where(Job.company_norm == job.company_norm, Job.id != job.id, Job.source != job.source,
                                             (Job.status.is_(None)) | (Job.status != "expired"))).all()
    roots: dict[int, Job] = {}
    for r in rows:
        root = r if r.canonical_job_id is None else session.get(Job, r.canonical_job_id)
        if root and root.id != job.id:
            roots[root.id] = root
    return list(roots.values())


def process_job(session: Session, job_id: int, *, run_id=None, migration_run_id=None, apply: bool = True) -> list[dict]:
    """Compare one (new) listing with its company block. Merges, or records 'suggest' events. Each merge is its own
    transaction. With apply=False nothing is written (used by dry-run reports)."""
    job = session.get(Job, job_id)
    if job is None or job.canonical_job_id is not None:
        return []
    out = []
    for cand in candidates_for(session, job):
        d = decide(job, cand)
        if d.action == "none":
            continue
        rec = {"job": job_id, "candidate": cand.id, "action": d.action, "method": d.method, "score": d.score, "evidence": d.evidence}
        if d.action == "merge" and apply:
            try:
                rec["result"] = merge(session, job_id, cand.id, method=d.method, score=d.score, evidence=d.evidence,
                                      run_id=run_id, migration_run_id=migration_run_id)
                job = session.get(Job, job_id)
                if job.canonical_job_id is not None:      # absorbed: stop comparing
                    out.append(rec); break
            except DedupError as e:
                rec["action"], rec["blocked"] = "blocked", str(e)
        elif d.action == "suggest" and apply:
            if not session.scalar(select(func.count()).select_from(JobEvent).where(
                    JobEvent.job_id == job_id, JobEvent.related_job_id == cand.id, JobEvent.event_type == "suggest",
                    JobEvent.reason == d.method)):
                _log(session, job_id, "suggest", None, None, d.method, uuid.uuid4().hex, score=d.score, related=cand.id,
                     run_id=run_id, migration_run_id=migration_run_id, evidence=d.evidence)
                session.commit()
        out.append(rec)
    return out


def process_new_jobs(session: Session, job_ids: list[int], *, run_id=None) -> dict:
    stats = {"processed": 0, "merged": 0, "suggested": 0, "blocked": 0}
    for jid in job_ids:
        for rec in process_job(session, jid, run_id=run_id):
            stats["merged"] += rec["action"] == "merge"
            stats["suggested"] += rec["action"] == "suggest"
            stats["blocked"] += rec["action"] == "blocked"
        stats["processed"] += 1
    return stats


def refresh_roots(session: Session, job_ids: list[int]) -> int:
    """After ingest, promote any group whose best-ranked listing is no longer the canonical one."""
    n = 0
    seen = set()
    for jid in job_ids:
        j = session.get(Job, jid)
        root = j.canonical_job_id or j.id if j else None
        if root and root not in seen:
            seen.add(root)
            if promote(session, root)["action"] == "promoted":
                n += 1
    return n


def dry_run_report(session: Session, limit_samples: int = 20) -> dict:
    """Simulate decisions over every company block without writing anything."""
    from collections import Counter, defaultdict
    blocks = defaultdict(list)
    for j in session.scalars(select(Job).where(Job.company_norm.is_not(None), Job.canonical_job_id.is_(None))):
        blocks[j.company_norm].append(j)
    counts, samples = Counter(), []
    for comp, js in blocks.items():
        for i in range(len(js)):
            for k in range(i + 1, len(js)):
                d = decide(js[i], js[k])
                if d.action != "none":
                    counts[f"{d.action}:{d.method}"] += 1
                    if len(samples) < limit_samples:
                        samples.append({"a": js[i].id, "b": js[k].id, "title_a": js[i].title, "title_b": js[k].title,
                                        "company": comp, **{"action": d.action, "method": d.method, "score": round(d.score, 4)}})
    return {"companies": len(blocks), "decisions": dict(counts), "samples": samples,
            "canonical_rows": sum(len(v) for v in blocks.values())}
