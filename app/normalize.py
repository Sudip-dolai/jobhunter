"""Normalization used by dedup, search and lifecycle. Pure functions, no database access.

Design rules (see the v2 plan): never drop information silently, never merge on weak evidence,
keep legacy fields untouched. Everything here only *derives* new values.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from app.filters import _CUR, _FX, _INR, _NOT_ANNUAL, _PLACES, _RANGE, _SINGLE, extract_salary_lpa

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_NON_TOKEN = re.compile(r"[^a-z0-9+#.]+")
_BRACKETS = re.compile(r"[\(\[\{][^)\]}]*[\)\]\}]")
_REQ_CODE = re.compile(r"^\d{5,}$")
_REQ_PREFIXED = re.compile(r"\b(?:req|jr|job|id|r)[-_#:]?\s?\d{3,}\b", re.I)

LEGAL_SUFFIXES = {"inc", "llc", "ltd", "limited", "pvt", "private", "corp", "corporation", "co", "gmbh", "plc",
                  "ag", "pte", "llp", "sa", "bv", "nv", "lp", "incorporated"}
# Deliberately NOT removed: technologies, solutions, services, systems (they distinguish real companies).

_ROMAN = {"i": "i", "ii": "ii", "iii": "iii", "iv": "iv"}
_ARABIC = {"1": "i", "2": "ii", "3": "iii", "4": "iv"}
_LEVEL_WORDS = {"senior": "senior", "sr": "senior", "junior": "junior", "jr": "junior", "staff": "staff",
                "principal": "principal", "lead": "lead", "intern": "intern", "internship": "intern",
                "trainee": "trainee", "associate": "associate", "manager": "manager", "director": "director",
                "head": "head", "vp": "vp", "graduate": "graduate", "fresher": "graduate", "entry": "entry"}

_CITY_ALIASES = {"bengaluru": "bangalore", "gurgaon": "gurugram", "bombay": "mumbai", "madras": "chennai",
                 "calcutta": "kolkata", "new delhi": "delhi", "trivandrum": "thiruvananthapuram",
                 "sao paulo": "são paulo"}
_NOT_CITY = {"usa", "uk", "england", "scotland", "washington, dc"}
_COUNTRY_WORDS = {c.lower() for c in _PLACES} | _NOT_CITY
_CITY_RE = []
for _country, _places in _PLACES.items():
    for _p in _places:
        if _p.lower() in _COUNTRY_WORDS:
            continue
        _CITY_RE.append((re.compile(r"(?<![a-z])" + re.escape(_p) + r"(?![a-z])", re.I), _CITY_ALIASES.get(_p.lower(), _p.lower())))


def clamp(value, n: int) -> str:
    """Strip control characters and cut to a column width (keeps Postgres from rejecting inserts)."""
    if value is None:
        return ""
    return _CTRL.sub(" ", str(value)).strip()[:n]


def norm(s) -> str:
    """NFKD, strip accents, casefold; keep a-z 0-9 + # . so c++, c#, .net and node.js survive."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).casefold()
    toks = [t.rstrip(".") for t in _NON_TOKEN.sub(" ", s).split()]
    return " ".join(t for t in toks if t)


def tokenize(q: str, limit: int = 8) -> list[str]:
    toks = [t for t in norm(q).split() if len(t) > 1 or t in {"c", "r"}]
    return toks[:limit]


def company_key(name: str) -> str:
    toks = norm(str(name or "").replace("&", " and ")).split()
    while len(toks) > 1 and toks[-1] in LEGAL_SUFFIXES:
        toks.pop()
    return " ".join(toks)[:160]


def _looks_like_location(seg: str) -> bool:
    n = norm(seg)
    if not n:
        return False
    if n in {"remote", "hybrid", "onsite", "on site"}:
        return True
    return any(rx.search(seg) for rx, _ in _CITY_RE) or any(w in n for w in _COUNTRY_WORDS)


def title_norm(title: str) -> str:
    t = _REQ_PREFIXED.sub(" ", _BRACKETS.sub(" ", str(title or "")))
    for sep in (" - ", " – ", " | ", " — "):          # "Engineer - Bengaluru" -> "Engineer"
        if sep in t:
            head, _, tail = t.rpartition(sep)
            if head.strip() and len(tail.split()) <= 5 and _looks_like_location(tail):
                t = head
    toks = [x for x in norm(t).split() if not _REQ_CODE.match(x)]
    return " ".join(toks)[:300]


def level_tokens(title: str) -> frozenset:
    """Seniority/level markers that must be equal for two titles to be the same job."""
    toks = title_norm(title).split()
    out = set()
    for i, t in enumerate(toks):
        if t in _LEVEL_WORDS:
            out.add(_LEVEL_WORDS[t])
    if toks:
        last = toks[-1]
        if len(toks) > 1 and last in _ROMAN:
            out.add(_ROMAN[last])
        elif len(toks) > 1 and last in _ARABIC:
            out.add(_ARABIC[last])
    return frozenset(out)


def seniority(title: str) -> str | None:
    lv = level_tokens(title)
    for key in ("director", "vp", "head", "principal", "staff", "manager", "lead", "senior"):
        if key in lv:
            return key
    for key in ("intern", "trainee", "graduate", "entry", "junior", "associate"):
        if key in lv:
            return key
    return "mid" if lv else None


def city_set(location: str) -> frozenset:
    cities = set()
    for rx, name in _CITY_RE:
        if rx.search(location or ""):
            cities.add(name)
    if not cities:
        first = norm((location or "").split(",")[0])
        words = set(first.split())
        if (first and len(first) <= 40 and not (words & {"remote", "hybrid", "onsite", "anywhere", "wfh", "worldwide"})
                and first not in _COUNTRY_WORDS and not any(w in first for w in _COUNTRY_WORDS if len(w) > 3)):
            cities.add(first)
    return frozenset(cities)


def city_norm(location: str) -> str:
    return "|".join(sorted(city_set(location)))[:80]


def location_key(location: str, remote: bool, country: str) -> str:
    cs = city_norm(location)
    if cs:
        return cs
    if remote:
        return "remote:" + norm(country).replace(" ", "")
    return ""


def locations_compatible(a_city: str, b_city: str, a_remote: bool, b_remote: bool, a_country: str, b_country: str) -> bool:
    """True when two listings plausibly describe the same location. Unknown on either side is NOT compatible."""
    A, B = set(filter(None, (a_city or "").split("|"))), set(filter(None, (b_city or "").split("|")))
    if A and B:
        return bool(A & B)
    if not A and not B and a_remote and b_remote:
        ca = {x.strip().lower() for x in (a_country or "").split(",") if x.strip()}
        cb = {x.strip().lower() for x in (b_country or "").split(",") if x.strip()}
        return bool(ca & cb) if ca and cb else True
    return False


def dedup_key(company_k: str, title_n: str, loc_key: str) -> str:
    return hashlib.sha1(f"{company_k}|{title_n}|{loc_key}".encode()).hexdigest()


def content_hash(j: dict) -> str:
    blob = "\x1f".join(str(j.get(k) or "") for k in ("title", "location", "url", "description", "salary_min", "salary_max"))
    return hashlib.sha1(re.sub(r"\s+", " ", blob).encode()).hexdigest()


def search_text(title: str, company: str, description: str, location: str = "") -> str:
    """Pre-normalized text the portable search matches against: title | company | location | first 2000 chars of description."""
    return f"{title_norm(title)} | {company_key(company)} | {norm(location)} | {norm(description)[:2000]}"


def shingles(text: str, k: int = 5) -> set:
    w = norm(text).split()
    if len(w) < k:
        return {" ".join(w)} if w else set()
    return {" ".join(w[i:i + k]) for i in range(len(w) - k + 1)}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def title_similarity(a: str, b: str) -> float:
    """Token-set similarity of two normalized titles (order-insensitive)."""
    from difflib import SequenceMatcher
    ta, tb = " ".join(sorted(a.split())), " ".join(sorted(b.split()))
    return SequenceMatcher(None, ta, tb).ratio() if ta and tb else 0.0


def parse_dt(value) -> datetime | None:
    """ISO strings, epoch seconds or milliseconds -> naive UTC datetime. Anything unusable -> None."""
    if value in (None, "", 0):
        return None
    try:
        if isinstance(value, (int, float)):
            v = float(value)
            v = v / 1000 if v > 1e11 else v
            return datetime.fromtimestamp(v, tz=timezone.utc).replace(tzinfo=None)
        s = str(value).strip().replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        return d.astimezone(timezone.utc).replace(tzinfo=None) if d.tzinfo else d
    except (ValueError, OverflowError, OSError):
        return None


def salary_detail(text: str) -> dict:
    """Original salary information found in free text, in addition to the legacy LPA value."""
    text = text or ""
    if extract_salary_lpa(text) is None:
        return {}
    if m := _RANGE.search(text):
        lo, hi, cur, per = float(m.group(1)) * 1e5, float(m.group(2)) * 1e5, "INR", "year"
    elif m := _INR.search(text):
        lo = int(m.group(1).replace(",", "")); hi = int(m.group(2).replace(",", "")) if m.group(2) else lo
        cur, per = "INR", "year"
    elif m := _SINGLE.search(text):
        lo = hi = float(m.group(1)) * 1e5; cur, per = "INR", "year"
    elif m := _FX.search(text):
        sym = m.group(1).lower().replace("us$", "$")
        cur = _CUR.get(sym, _CUR.get(m.group(1).lower(), "usd")).upper()
        mult = lambda v, k: float(v.replace(",", "")) * (1000 if k else 1)  # noqa: E731
        lo = mult(m.group(2), m.group(3)); hi = mult(m.group(4), m.group(5)) if m.group(4) else lo
        w = _NOT_ANNUAL.search(text[m.start(): m.end() + 20])
        per = "year"
        if w:
            word = (w.group(1) or "").lower()
            per = {"hour": "hour", "hr": "hour", "hourly": "hour", "month": "month", "monthly": "month",
                   "week": "week", "weekly": "week", "day": "day", "daily": "day"}.get(word, "year")
    else:
        return {}
    return {"salary_raw": clamp(m.group(0), 300), "salary_currency": cur[:3], "salary_period": per,
            "salary_orig_min": min(lo, hi), "salary_orig_max": max(lo, hi), "salary_source": "parsed"}


_CATEGORY_RULES = [
    ("software", r"software|developer|engineer|devops|sre|backend|frontend|full.?stack|programmer|qa|sdet|mobile|android|ios"),
    ("data", r"data|analyst|machine learning|\bml\b|\bai\b|scientist|bi\b"),
    ("design", r"design|ux|ui\b|creative"),
    ("product", r"product manager|product owner|program manager"),
    ("sales", r"sales|account executive|business development"),
    ("marketing", r"marketing|seo|content|brand"),
    ("finance", r"finance|accountant|audit|tax|controller"),
    ("hr", r"recruit|talent|human resources|\bhr\b"),
    ("support", r"support|customer success|customer service|help desk"),
    ("operations", r"operations|logistics|supply chain|warehouse"),
]


def classify_category(title: str) -> str | None:
    t = norm(title)
    for name, pat in _CATEGORY_RULES:
        if re.search(pat, t):
            return name
    return None


def derived_fields(j: dict, *, company_id: int | None = None, now: datetime | None = None) -> dict:
    """All v2 columns for one legacy-shaped job dict (does not touch the legacy values)."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    ck = company_key(j.get("company", ""))
    tn = title_norm(j.get("title", ""))
    loc, remote, country = j.get("location", "") or "", bool(j.get("remote")), j.get("country", "") or ""
    out = {
        "company_id": company_id, "company_norm": ck, "title_norm": tn, "city_norm": city_norm(loc),
        "dedup_key": dedup_key(ck, tn, location_key(loc, remote, country)),
        "content_hash": content_hash(j),
        "search_text": search_text(j.get("title", ""), j.get("company", ""), j.get("description", ""), j.get("location", "")),
        "category": classify_category(j.get("title", "")), "seniority": seniority(j.get("title", "")),
        "last_seen_at": now, "status": "active", "miss_count": 0,
    }
    posted = parse_dt(j.get("posted_at"))
    out["posted_at"] = posted
    out["posted_at_source"] = "source" if posted else "first_seen"
    out["valid_through"] = parse_dt(j.get("valid_through"))
    first_seen = j.get("first_seen") or now
    out["sort_date"] = posted or first_seen
    if j.get("apply_url"):
        out["apply_url"] = clamp(j["apply_url"], 1000)
    det = salary_detail(j.get("description", ""))
    if det:
        out.update(det)
    elif j.get("salary_min") is not None:
        out["salary_source"] = "structured"
    return out


def hours_between(a: datetime, b: datetime) -> float:
    return (b - a) / timedelta(hours=1)
