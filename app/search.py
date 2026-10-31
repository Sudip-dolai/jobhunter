"""Global job search (plan section 9). Separate from personal matching: this module never imports Profile or Match.

Portable SQL (SQLite and PostgreSQL identical): every text comparison is an escaped LIKE against PRE-NORMALIZED
lowercase columns (title_norm, company_norm, search_text), so there is no dependence on database collation or on
lower()/LIKE behavior for non-ASCII text.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, case, func, literal, or_, select
from sqlalchemy.orm import Session

from app import config, sources
from app.models import Company, CompanyAlias, Job
from app.normalize import company_key, norm, tokenize

CURSOR_VERSION = 1
_QUOTED = re.compile(r'"([^"]+)"')
SORTS = ("relevance", "newest", "salary")


class BadCursor(ValueError):
    pass


@dataclass
class SearchQuery:
    q: str = ""
    country: list = field(default_factory=list)
    city: str = ""
    remote: bool | None = None
    job_type: list = field(default_factory=list)
    category: list = field(default_factory=list)
    sector: list = field(default_factory=list)
    company: str = ""
    min_salary_lpa: float | None = None
    unknown_salary: str = "include"          # include | exclude
    posted_within_days: int | None = None
    include_stale: bool = False
    include_expired: bool = False
    sort: str = "relevance"
    page_size: int = 0
    cursor: str | None = None


@dataclass
class Page:
    items: list
    next_cursor: str | None
    total: int | None = None                  # exact when there are no keywords
    total_estimate: int | None = None         # with keywords: size of the (capped) candidate set
    candidates_capped: bool = False
    unknown_salary_count: int = 0
    sort: str = "newest"


# ------------------------------------------------------------------ helpers
def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like(col, needle: str):
    return col.like(f"%{_esc(needle)}%", escape="\\")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _sort_date():
    return func.coalesce(Job.sort_date, Job.first_seen)


def _eff_max():
    return func.coalesce(Job.salary_max, Job.salary_min)


def _key() -> bytes:
    return hashlib.sha256(("jh-search|" + config.DATABASE_URL).encode()).digest()


def _sign(payload: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()).decode().rstrip("=")
    mac = hmac.new(_key(), raw.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{raw}.{mac}"


def _unsign(cursor: str) -> dict:
    try:
        raw, mac = cursor.rsplit(".", 1)
        if not hmac.compare_digest(mac, hmac.new(_key(), raw.encode(), hashlib.sha256).hexdigest()[:16]):
            raise BadCursor("cursor signature mismatch")
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except BadCursor:
        raise
    except Exception as e:
        raise BadCursor("malformed cursor") from e
    if data.get("v") != CURSOR_VERSION:
        raise BadCursor("cursor version mismatch")
    return data


def _query_hash(q: SearchQuery) -> str:
    d = {k: getattr(q, k) for k in ("q", "country", "city", "remote", "job_type", "category", "sector", "company",
                                    "min_salary_lpa", "unknown_salary", "posted_within_days", "include_stale",
                                    "include_expired", "sort")}
    return hashlib.sha1(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()[:12]


def parse_keywords(q: str) -> tuple[list[str], list[str]]:
    """-> (tokens, phrases). A double-quoted phrase is one required phrase."""
    phrases = [norm(p) for p in _QUOTED.findall(q or "") if norm(p)]
    rest = _QUOTED.sub(" ", q or "")
    return tokenize(rest), phrases


# ------------------------------------------------------------------ filters
def _filters(session: Session, q: SearchQuery, now: datetime, snapshot_max_id: int | None, with_salary: bool = True) -> list:
    c = [Job.canonical_job_id.is_(None)]
    allowed = ["active"] + (["stale"] if q.include_stale else []) + (["expired"] if q.include_expired else [])
    c.append(or_(Job.status.is_(None), Job.status.in_(allowed)))
    feeds = sorted(sources.feed_sources())
    cutoff = now - timedelta(days=config.FEED_MAX_AGE_DAYS)
    c.append(or_(Job.source.not_in(feeds), Job.last_seen_at.is_(None), Job.last_seen_at >= cutoff))
    c.append(or_(Job.valid_through.is_(None), Job.valid_through >= now))
    if snapshot_max_id is not None:
        c.append(Job.id <= snapshot_max_id)
    if q.country:
        padded = literal(",") + func.replace(func.lower(func.coalesce(Job.country, "")), ", ", ",") + literal(",")
        c.append(or_(*[padded.like(f"%,{_esc(x.strip().lower())},%", escape="\\") for x in q.country if x.strip()]))
    if q.city:
        padded = literal("|") + func.coalesce(Job.city_norm, "") + literal("|")
        c.append(padded.like(f"%|{_esc(norm(q.city))}|%", escape="\\"))
    if q.remote is True:
        c.append(Job.remote.is_(True))
    elif q.remote is False:
        c.append(or_(Job.remote.is_(None), Job.remote.is_(False)))
    if q.job_type:
        c.append(Job.job_type.in_(q.job_type))
    if q.category:
        c.append(Job.category.in_(q.category))
    if q.sector:
        c.append(func.lower(Job.sector).in_([s.lower() for s in q.sector]))
    if q.company:
        key = company_key(q.company)
        ids = [r[0] for r in session.execute(select(CompanyAlias.company_id).where(CompanyAlias.alias_norm == key))]
        ids += [r[0] for r in session.execute(select(Company.id).where(Company.name_norm == key))]
        c.append(or_(Job.company_norm == key, Job.company_id.in_(set(ids))) if ids else Job.company_norm == key)
    if q.posted_within_days:
        c.append(_sort_date() >= now - timedelta(days=q.posted_within_days))
    if with_salary and q.min_salary_lpa is not None:
        if q.unknown_salary == "exclude":
            c.append(_eff_max() >= q.min_salary_lpa)
        else:
            c.append(or_(_eff_max() >= q.min_salary_lpa, _eff_max().is_(None)))
    return c


# ------------------------------------------------------------------ scoring
def _strength(token: str, words: list[str], text: str) -> float:
    if token in words:
        return 1.0
    if any(w.startswith(token) for w in words):
        return 0.6
    return 0.3 if token in text else 0.0


def _mean(tokens: list[str], text: str) -> float:
    if not tokens:
        return 0.0
    words = text.split()
    return sum(_strength(t, words, text) for t in tokens) / len(tokens)


def score_job(j: Job, tokens: list[str], phrases: list[str], now: datetime) -> float:
    t = _mean(tokens, j.title_norm or "")
    c = _mean(tokens, j.company_norm or "")
    d = _mean(tokens, j.search_text or "")
    full = " ".join(tokens) if tokens else (phrases[0] if phrases else "")
    p = 1.0 if full and full in (j.title_norm or "") else 0.0
    text = max(0.0, min(1.0, 0.60 * t + 0.15 * c + 0.25 * d + 0.20 * p))
    sd = j.sort_date or j.first_seen or now
    age_days = max(0.0, (now - sd).total_seconds() / 86400)
    fresh = 0.5 ** (age_days / 14)
    trust = {3: 1.0, 2: 0.8, 1: 0.5}.get(sources.trust_of(j.source), 0.5)
    known = (j.salary_max if j.salary_max is not None else j.salary_min) is not None
    comp = 0.4 * known + 0.4 * (len(j.description or "") >= 300) + 0.2 * bool(j.apply_url)
    return round(100 * (0.55 * text + 0.20 * fresh + 0.15 * trust + 0.10 * comp), 4)


# ------------------------------------------------------------------ main entry
def _item(j: Job, score: float | None, also_on: list) -> dict:
    eff = j.salary_max if j.salary_max is not None else j.salary_min
    return {"id": j.id, "title": j.title, "company": j.company, "location": j.location, "url": j.url,
            "apply_url": j.apply_url or j.url, "source": j.source, "remote": bool(j.remote), "job_type": j.job_type,
            "country": j.country, "salary_min": j.salary_min, "salary_max": j.salary_max, "salary_known": eff is not None,
            "status": j.status or "active", "posted_at": j.posted_at, "sort_date": j.sort_date or j.first_seen,
            "category": j.category, "score": score, "also_on": also_on, "description": j.description}


def _also_on(session: Session, rows: list[Job]) -> dict[int, list]:
    ids = [r.id for r in rows]
    out: dict[int, list] = {i: [] for i in ids}
    if not ids:
        return out
    for m in session.scalars(select(Job).where(Job.canonical_job_id.in_(ids)).order_by(Job.id)):
        out[m.canonical_job_id].append({"source": m.source, "url": m.apply_url or m.url, "job_id": m.id})
    return out


def unindexed_count(session: Session) -> int:
    """Jobs that have no normalized search columns yet (they cannot be keyword-searched until backfilled)."""
    return int(session.scalar(select(func.count()).select_from(Job).where(Job.search_text.is_(None))) or 0)


def search(session: Session, q: SearchQuery, now: datetime | None = None) -> Page:
    size = max(1, min(50, q.page_size or config.SEARCH_PAGE_SIZE))
    sort = q.sort if q.sort in SORTS else "relevance"
    tokens, phrases = parse_keywords(q.q)
    has_kw = bool(tokens or phrases)
    if not has_kw and sort == "relevance":
        sort = "newest"                                        # nothing to rank by: browse newest first
    state = {}
    if q.cursor:
        state = _unsign(q.cursor)
        if state.get("h") != _query_hash(q):
            raise BadCursor("cursor does not belong to this query")
        now = datetime.fromisoformat(state["now"])
    now = now or _now()
    snap = state.get("snap")
    if snap is None:
        snap = int(session.scalar(select(func.max(Job.id))) or 0)
    conds = _filters(session, q, now, snap)
    base_cursor = {"v": CURSOR_VERSION, "h": _query_hash(q), "now": now.isoformat(), "snap": snap, "sort": sort}

    uconds = _filters(session, q, now, snap, with_salary=False)
    ukw = [_like(Job.search_text, t) for t in tokens] + [_like(Job.search_text, p) for p in phrases]
    unknown = int(session.scalar(select(func.count()).select_from(Job).where(*uconds, *ukw, _eff_max().is_(None))) or 0)
    if has_kw:
        return _search_keywords(session, q, conds, tokens, phrases, sort, size, state, base_cursor, now, unknown)
    return _search_browse(session, q, conds, sort, size, state, base_cursor, unknown)


def _search_browse(session, q, conds, sort, size, state, base_cursor, unknown) -> Page:
    total = int(session.scalar(select(func.count()).select_from(Job).where(*conds)) or 0)
    sd, eff = _sort_date(), _eff_max()
    stmt = select(Job, sd.label("sd"), eff.label("eff")).where(*conds)
    k = state.get("k")
    if sort == "newest":
        if k:
            kd = datetime.fromisoformat(k[0])
            stmt = stmt.where(or_(sd < kd, and_(sd == kd, Job.id < k[1])))
        stmt = stmt.order_by(sd.desc(), Job.id.desc())
    else:                                                       # salary: known first by value, unknown last, ties by id
        unk = case((eff.is_(None), 1), else_=0)
        if k:
            u, m, i = k
            stmt = stmt.where(and_(unk == 1, Job.id < i) if u == 1 else
                              or_(unk > 0, and_(unk == 0, or_(eff < m, and_(eff == m, Job.id < i)))))
        stmt = stmt.order_by(unk.asc(), eff.desc(), Job.id.desc())
    rows = session.execute(stmt.limit(size + 1)).all()
    more = len(rows) > size
    rows = rows[:size]
    jobs = [r[0] for r in rows]
    also = _also_on(session, jobs)
    nxt = None
    if more and rows:
        last = rows[-1]
        key = [last[1].isoformat(), last[0].id] if sort == "newest" else [0 if last[2] is not None else 1, last[2], last[0].id]
        nxt = _sign({**base_cursor, "k": key})
    return Page([_item(j, None, also[j.id]) for j in jobs], nxt, total=total, unknown_salary_count=unknown, sort=sort)


def _stage(session, conds, tokens, phrases, cols, limit, exclude_ids):
    tok = [or_(*[_like(c, t) for c in cols]) for t in tokens]
    ph = [or_(*[_like(c, p) for c in cols]) for p in phrases]
    stmt = select(Job).where(*conds, *tok, *ph)
    if exclude_ids:
        stmt = stmt.where(Job.id.not_in(exclude_ids))
    return list(session.scalars(stmt.order_by(_sort_date().desc(), Job.id.desc()).limit(limit)))


def _search_keywords(session, q, conds, tokens, phrases, sort, size, state, base_cursor, now, unknown) -> Page:
    cap = config.SEARCH_CAND_MAX
    a = _stage(session, conds, tokens, phrases, [Job.title_norm, Job.company_norm], cap, [])
    b = _stage(session, conds, tokens, phrases, [Job.search_text], max(0, cap - len(a)), [r.id for r in a]) if len(a) < cap else []
    cands = a + b
    capped = len(a) >= cap or len(cands) >= cap
    scored = [(score_job(j, tokens, phrases, now), j) for j in cands]
    sdv = lambda j: (j.sort_date or j.first_seen or now)  # noqa: E731
    if sort == "relevance":
        scored.sort(key=lambda t: (-t[0], -sdv(t[1]).timestamp(), -t[1].id))
    elif sort == "newest":
        scored.sort(key=lambda t: (-sdv(t[1]).timestamp(), -t[1].id))
    else:
        scored.sort(key=lambda t: (((t[1].salary_max if t[1].salary_max is not None else t[1].salary_min) is None),
                                   -(t[1].salary_max if t[1].salary_max is not None else (t[1].salary_min or 0)), -t[1].id))
    off = int(state.get("off", 0))
    window = scored[off:off + size]
    jobs = [j for _, j in window]
    also = _also_on(session, jobs)
    nxt = _sign({**base_cursor, "off": off + size}) if off + size < len(scored) else None
    return Page([_item(j, s, also[j.id]) for s, j in window], nxt, total_estimate=len(scored), candidates_capped=capped,
                unknown_salary_count=unknown, sort=sort)


def filter_options(session: Session) -> dict:
    """Distinct values for the UI filters (canonical, live jobs only)."""
    live = [Job.canonical_job_id.is_(None), or_(Job.status.is_(None), Job.status == "active")]
    types = [r[0] for r in session.execute(select(Job.job_type).where(*live).distinct()) if r[0]]
    cats = [r[0] for r in session.execute(select(Job.category).where(*live).distinct()) if r[0]]
    sectors = [r[0] for r in session.execute(select(Job.sector).where(*live).distinct()) if r[0]]
    countries = set()
    for (c,) in session.execute(select(Job.country).where(*live).distinct()):
        countries.update(x.strip() for x in (c or "").split(",") if x.strip())
    return {"job_type": sorted(types), "category": sorted(cats), "sector": sorted(sectors), "country": sorted(countries)}


def save_to_tracker(session: Session, user_id: int, job_id: int):
    """Group-aware 'Save to tracker': if the user already tracks ANY listing of the same job (the canonical row or one of
    its duplicates), return that Application instead of creating a second one. Calls tracker.add_application unchanged."""
    from app import tracker
    from app.models import Application
    job = session.get(Job, job_id)
    root = job.canonical_job_id or job.id
    ids = [root] + [m.id for m in session.scalars(select(Job).where(Job.canonical_job_id == root))]
    existing = session.scalar(select(Application).where(Application.user_id == user_id, Application.job_id.in_(ids)))
    if existing:
        return existing, False
    return tracker.add_application(session, user_id, job_id), True
