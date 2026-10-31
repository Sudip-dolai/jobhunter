"""Find out which job-board system each company uses and build companies.json.

Input file (companies_input.txt), one company per line, '# Sector' lines set the sector:
    # SaaS & B2B Software
    Stripe | https://stripe.com/jobs/
    Freshworks            <- URL is optional; the name is then probed on Greenhouse/Lever/Ashby

Run:  python -m app.discover companies_input.txt
Custom career sites are supported when they publish schema.org JobPosting data (source "jsonld"); the rest go to unsupported.txt. Name-guessed matches carry "guess": true,
so check that they really are the right company.
"""
import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from urllib.parse import urljoin, urlparse

from app import config, scrapers

T = 15
PATTERNS = {
    "greenhouse": re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)"),
    "lever": re.compile(r"jobs(?:\.eu)?\.lever\.co/([A-Za-z0-9_.-]+)"),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)"),
    "workable": re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)"),
    "smartrecruiters": re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
    "recruitee": re.compile(r"https?://([A-Za-z0-9-]+)\.recruitee\.com"),
}
WORKDAY = re.compile(r"https?://([A-Za-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)")
STOP = re.compile(r"\b(technologies|technology|systems|limited|ltd|inc|corp|company|labs|software)\b", re.I)


def detect_from_url(url: str) -> tuple[str, str] | None:
    if m := WORKDAY.search(url or ""):
        return "workday", "|".join(m.groups())
    for source, pat in PATTERNS.items():
        if m := pat.search(url or ""):
            return source, m.group(1)
    return None


def slug_candidates(name: str) -> list[str]:
    n = STOP.sub("", name.lower().replace("&", "and")).strip()
    out = [re.sub(r"[^a-z0-9]", "", n), re.sub(r"[^a-z0-9]+", "-", n).strip("-")]
    return [s for i, s in enumerate(out) if s and s not in out[:i]]


def verify(source: str, slug: str) -> bool:
    try:
        if source == "greenhouse":
            r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=T)
            return r.status_code == 200 and "jobs" in r.json()
        if source == "lever":
            r = requests.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json", "limit": 1}, timeout=T)
            return r.status_code == 200 and isinstance(r.json(), list)
        if source == "ashby":
            r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=T)
            return r.status_code == 200 and "jobs" in r.json()
        if source == "workable":
            r = requests.get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", timeout=T)
            return r.status_code == 200 and "jobs" in r.json()
        if source == "smartrecruiters":
            r = requests.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", params={"limit": 1}, timeout=T)
            return r.status_code == 200 and r.json().get("totalFound", 0) > 0
        if source == "recruitee":
            r = requests.get(f"https://{slug}.recruitee.com/api/offers/", timeout=T)
            return r.status_code == 200 and "offers" in r.json()
        if source == "workday":
            tenant, wd, site = slug.split("|")
            r = requests.post(f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cs/{tenant}/{site}/jobs", timeout=T,
                              json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
            return r.status_code == 200 and "jobPostings" in r.json()
    except (requests.RequestException, ValueError):
        pass
    return False


def probe_jsonld(url: str) -> str | None:
    """Custom career site check: does it publish schema.org JobPosting data? Returns the listing/sitemap URL to use."""
    for cand in (url, urljoin(url, "/sitemap.xml")):
        try:
            links = scrapers._find_job_urls(cand, scrapers._get_text(cand))
        except Exception:
            continue
        for link in links[:5]:
            try:
                if scrapers.parse_jobposting(scrapers._get_text(link)):
                    return cand
            except Exception:
                continue
    return None


def discover(name: str, url: str = "", sector: str = "", check: bool = True) -> dict | None:
    if hit := detect_from_url(url):
        if not check or verify(*hit):
            return {"name": name, "source": hit[0], "slug": hit[1], "sector": sector}
    for slug in slug_candidates(name):
        for source in ("greenhouse", "lever", "ashby"):
            if verify(source, slug):
                return {"name": name, "source": source, "slug": slug, "sector": sector, "guess": True}
    if url and (found := probe_jsonld(url)):
        return {"name": name, "source": "jsonld", "slug": found, "sector": sector}
    if url:   # any other careers page: read it directly (links that look like software jobs)
        return {"name": name, "source": "page", "slug": url, "sector": sector}
    return None


def _name_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    for prefix in ("careers.", "jobs.", "career.", "apply."):
        host = host.removeprefix(prefix)
    return (host.split(".")[0] or "company").title()


def parse_input(text: str) -> list[tuple[str, str, str]]:
    rows, sector, seen = [], "", set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            sector = line.lstrip("# ").strip()
            continue
        name, _, url = (p.strip() for p in line.partition("|"))
        if name.lower().startswith("http"):      # a bare link: take the company name from the address
            name, url = _name_from_url(name), name
        if name.lower() in seen:
            continue  # duplicates are skipped
        seen.add(name.lower())
        rows.append((name, url, sector))
    return rows


def merge(existing: list[dict], found: list[dict]) -> list[dict]:
    by_name = {c["name"].lower(): c for c in existing}
    for c in found:
        by_name[c["name"].lower()] = c
    return sorted(by_name.values(), key=lambda c: c["name"].lower())


def add_links(text: str, check: bool = True) -> list[dict]:
    """Add career-page links (one per line: Name | link, or just a link) to companies.json."""
    found = [r for n, u, sec in parse_input(text) if (r := discover(n, u, sec, check))]
    path = config.ROOT / "companies.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    path.write_text(json.dumps(merge(existing, found), indent=2, ensure_ascii=False), encoding="utf-8")
    return found


def run(input_path: str, check: bool = True) -> dict:
    """Check every company in the input file and update companies.json / unsupported.txt."""
    rows = parse_input(Path(input_path).read_text(encoding="utf-8"))
    done = [0]

    def work(r):
        res = discover(r[0], r[1], r[2], check)
        done[0] += 1
        if done[0] % 25 == 0:
            print(f"  checked {done[0]}/{len(rows)}", flush=True)
        return res

    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(work, rows))
    found = [r for r in results if r]
    missing = [f"{n} | {u}" for (n, u, _), r in zip(rows, results) if not r]
    path = config.ROOT / "companies.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    path.write_text(json.dumps(merge(existing, found), indent=2, ensure_ascii=False), encoding="utf-8")
    (config.ROOT / "unsupported.txt").write_text("\n".join(missing), encoding="utf-8")
    (config.ROOT / "discovered.flag").write_text("done", encoding="utf-8")
    return {"total": len(rows), "found": len(found), "unsupported": len(missing), "pages": sum(1 for c in found if c["source"] == "page"),
            "guesses": [c["name"] for c in found if c.get("guess")]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", nargs="?", default=str(config.ROOT / "companies_input.txt"))
    ap.add_argument("--no-verify", action="store_true", help="trust board URLs without calling the APIs")
    a = ap.parse_args()
    r = run(a.input, not a.no_verify)
    print(f"{r['found']} of {r['total']} companies supported -> companies.json")
    print(f"  of which {r['pages']} are read as plain career pages (quality varies; check Jobs per company)")
    print(f"{r['unsupported']} without a link -> unsupported.txt")
    if r["guesses"]:
        print("Name-guessed (please verify these are the right company):", ", ".join(r["guesses"]))


if __name__ == "__main__":
    main()


# ---------------- v2: replacing aggregator feeds with direct employer sources ----------------
def coverage_report(session) -> dict:
    """How much of the unique (canonical) job set comes from direct employer sources versus aggregator feeds."""
    from sqlalchemy import func, select
    from app import sources as srcs
    from app.models import Job
    per = session.execute(select(Job.source, func.count()).where(Job.canonical_job_id.is_(None)).group_by(Job.source)).all()
    by_kind: dict[str, int] = {}
    for src, n in per:
        by_kind[srcs.kind_of(src)] = by_kind.get(srcs.kind_of(src), 0) + n
    total = sum(by_kind.values())
    direct = by_kind.get("ats", 0) + by_kind.get("employer_page", 0)
    feed_only = session.execute(select(Job.company_norm).where(Job.canonical_job_id.is_(None), Job.company_norm.is_not(None),
                                                               Job.source.in_(sorted(srcs.feed_sources()))).distinct()).scalars().all()
    has_direct = set(session.execute(select(Job.company_norm).where(Job.company_norm.is_not(None),
                                                                    Job.source.not_in(sorted(srcs.feed_sources()))).distinct()).scalars().all())
    return {"canonical_jobs": total, "by_kind": by_kind, "direct_share": round(direct / total, 4) if total else 0.0,
            "companies_only_in_feeds": sorted(set(feed_only) - has_direct)}


def propose_direct_sources(session, limit: int = 20, finder=None, sleep=None, delay: float = 1.0) -> list[dict]:
    """For companies seen ONLY through aggregator feeds, ask whether a public employer board exists.
    Returns PROPOSALS for a human to review: companies.json is never edited here, and a proposal for a source whose
    docs/auth/terms are not verified is marked not allowed (see app.sources.expansion_check)."""
    import time as _time
    from sqlalchemy import select
    from app import sources as srcs
    from app.models import Job
    finder = finder or discover
    sleep = sleep or _time.sleep
    names = {}
    for norm_, name in session.execute(select(Job.company_norm, Job.company).where(
            Job.canonical_job_id.is_(None), Job.company_norm.in_(coverage_report(session)["companies_only_in_feeds"]))):
        names.setdefault(norm_, name)
    out = []
    for norm_, name in list(names.items())[:limit]:
        found = finder(name, "", "", True)
        sleep(delay)
        if not found:
            continue
        allowed, reasons = srcs.expansion_check(found.get("source", ""))
        out.append({"company": name, "proposed": found, "allowed_to_add": allowed, "blocked_because": reasons, "status": "needs_review"})
    return out
