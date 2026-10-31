"""Completeness-aware collection. A successful HTTP response does NOT mean a complete source run.

complete=True requires ALL of (plan section 7): G1 every request 2xx, G2 body parsed as the expected JSON type,
G3 expected keys/types present, G4 expected host, G5 pagination ended on a PROVEN terminal condition for that source,
G6 nothing truncated and ids unique. Unknown completeness stays False.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import requests

from app import config, scrapers, sources

log = logging.getLogger("jobhunter.collect")


@dataclass
class CollectResult:
    jobs: list = field(default_factory=list)
    listed_ids: set | None = None          # every id the source listed, BEFORE software/scope filtering and known-skipping
    complete: bool = False                 # default False: unknown means false
    outcome: str = "ok"                    # ok | error | blocked | robots_denied | skipped
    http_status: int | None = None
    pages_fetched: int = 0
    expected_total: int | None = None
    shadow_complete: bool | None = None
    suspect: bool = False
    notes: list = field(default_factory=list)
    error: str = ""


def classify_error(exc: BaseException) -> tuple[str, int | None]:
    """Map an exception to a run outcome. 401/403/429 are 'blocked': back off, never retry under another identity."""
    if isinstance(exc, PermissionError):
        return "robots_denied", None
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        code = exc.response.status_code
        return ("blocked" if code in (401, 403, 429) else "error"), code
    return "error", None


def _ids_unique(ids: list) -> bool:
    return len(ids) == len(set(ids)) and all(i not in (None, "") for i in ids)


def _json_response_ok(r) -> bool:
    ctype = (r.headers.get("Content-Type") or "").lower()
    return 200 <= r.status_code < 300 and "json" in ctype


def collect_greenhouse(company: dict, known=None) -> CollectResult:
    res = CollectResult()
    r = scrapers._greenhouse_response(company["slug"])
    res.http_status, res.pages_fetched = r.status_code, 1
    try:
        data = r.json()
    except ValueError:
        res.notes.append("G2: body is not JSON"); return res
    items, meta = data.get("jobs") if isinstance(data, dict) else None, (data.get("meta") if isinstance(data, dict) else None)
    if not isinstance(items, list):
        res.notes.append("G3: 'jobs' missing or not a list"); return res
    ids = [str(j.get("id")) if isinstance(j, dict) and j.get("id") is not None else None for j in items]
    res.listed_ids = {i for i in ids if i}
    total = meta.get("total") if isinstance(meta, dict) else None
    res.expected_total = total if isinstance(total, int) else None
    res.jobs = scrapers.enrich_jobs(company, scrapers._greenhouse_map(company["name"], [j for j in items if isinstance(j, dict)]))
    checks = {"G1/G2 json 2xx": _json_response_ok(r), "G3 meta.total is an int": isinstance(total, int),
              "G5 len(jobs) == meta.total": isinstance(total, int) and len(items) == total, "G6 ids unique": _ids_unique(ids)}
    res.notes += [f"failed: {k}" for k, ok in checks.items() if not ok]
    res.complete = all(checks.values())
    return res


def collect_ashby(company: dict, known=None) -> CollectResult:
    res = CollectResult()
    r = scrapers._ashby_response(company["slug"])
    res.http_status, res.pages_fetched = r.status_code, 1
    try:
        data = r.json()
    except ValueError:
        res.notes.append("G2: body is not JSON"); return res
    items = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(items, list):
        res.notes.append("G3: 'jobs' missing or not a list"); return res
    rows = [j for j in items if isinstance(j, dict)]
    mapped = scrapers._ashby_map(company["name"], rows, extras=True)
    ids = [str(j.get("id")) if j.get("id") is not None else None for j in rows if j.get("isListed") is not False]
    res.listed_ids = {i for i in ids if i}
    res.jobs = scrapers.enrich_jobs(company, mapped)
    checks = {"G1/G2 json 2xx": _json_response_ok(r), "G3 apiVersion present": "apiVersion" in data,
              "G6 ids unique": _ids_unique(ids), "G3 jobs is a list": True}
    res.notes += [f"failed: {k}" for k, ok in checks.items() if not ok]
    res.notes.append("structural contract only (no total to cross-check); the shrink guard applies")
    res.complete = all(checks.values())
    return res


def collect_lever(company: dict, known=None) -> CollectResult:
    """Provisional: complete stays False. shadow_complete records whether an explicit empty page followed the list."""
    res = CollectResult()
    raw = scrapers._lever(company["name"], company["slug"], set(), company)      # one request, same as the legacy path
    res.pages_fetched = 1
    res.listed_ids = {j["external_id"] for j in raw}
    ids_ok = len(res.listed_ids) == len(raw)
    res.jobs = scrapers.enrich_jobs(company, raw)
    try:
        r = requests.get(f"https://api.lever.co/v0/postings/{company['slug']}",
                         params={"mode": "json", "skip": len(raw), "limit": 1}, timeout=scrapers.TIMEOUT)
        r.raise_for_status()
        res.pages_fetched += 1
        body = r.json()
        res.shadow_complete = bool(raw) and isinstance(body, list) and body == [] and ids_ok
    except Exception as e:                  # the shadow probe must never fail the run
        res.shadow_complete = None
        res.notes.append(f"shadow probe failed: {e}")
    res.notes.append("provisional source: complete=False until promoted from shadow data")
    return res


def collect(company: dict, known=None) -> CollectResult:
    """Collect one company/feed. Never raises: failures become a CollectResult with an outcome and complete=False."""
    if not config.COLLECTION_ENABLED:
        return CollectResult(outcome="skipped", notes=["COLLECTION_ENABLED is off"])
    source = company.get("source", "")
    spec = sources.spec(source)
    try:
        if source == "greenhouse":
            res = collect_greenhouse(company, known)
        elif source == "ashby":
            res = collect_ashby(company, known)
        elif source == "lever":
            res = collect_lever(company, known)
        else:
            res = CollectResult(jobs=scrapers.fetch_jobs(company, known), notes=[f"{spec.completeness_status}: never complete"])
    except Exception as e:
        outcome, code = classify_error(e)
        return CollectResult(outcome=outcome, http_status=code, error=str(e)[:500], notes=["run failed; no lifecycle change"])
    if res.complete and not spec.may_be_complete:      # belt and braces: the registry has the last word
        res.complete = False
        res.notes.append("registry: source is not verified for completeness")
    return res
