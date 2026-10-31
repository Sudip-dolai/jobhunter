"""Bulk-discover thousands of companies that use public job boards, and add them to companies.json.

How: Common Crawl's public URL index lists every page it has seen on boards.greenhouse.io, jobs.lever.co,
jobs.ashbyhq.com, apply.workable.com, smartrecruiters and *.recruitee.com. The first path segment of those URLs is the
company's board slug. Each slug is then verified against the board's real API (app.discover.verify) and only working
boards with the right shape are added. Nothing is guessed: unverified slugs are dropped.

Run (needs internet; takes 10-40 minutes depending on --max-pages):
    python -m app.harvest                      # all sources
    python -m app.harvest --sources greenhouse lever --max-pages 5
    python -m app.harvest --slugs my_slugs.txt  # extra lines: "source slug" (e.g. "greenhouse figma")

Then run:  python run_pipeline.py
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

import requests

from app import config, discover

CC = "https://index.commoncrawl.org"
HEADERS = {"User-Agent": "jobhunter-harvest/1.0 (personal job search tool)"}

# source -> (cdx url pattern, matchType)
SOURCES = {
    "greenhouse": [("boards.greenhouse.io/*", "prefix"), ("job-boards.greenhouse.io/*", "prefix"), ("boards.eu.greenhouse.io/*", "prefix")],
    "lever": [("jobs.lever.co/*", "prefix"), ("jobs.eu.lever.co/*", "prefix")],
    "ashby": [("jobs.ashbyhq.com/*", "prefix")],
    "workable": [("apply.workable.com/*", "prefix")],
    "smartrecruiters": [("careers.smartrecruiters.com/*", "prefix"), ("jobs.smartrecruiters.com/*", "prefix")],
    "recruitee": [("recruitee.com", "domain")],
}
SKIP = {
    "embed", "api", "www", "static", "assets", "img", "images", "css", "js", "favicon.ico", "robots.txt", "sitemap.xml",
    "jobs", "job", "careers", "career", "boards", "login", "signin", "signup", "about", "help", "support", "apply",
    "company", "companies", "cdn", "app", "auth", "oauth", "v1", "v0", "wp-content", "blog", "docs", "status",
}
SLUG_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.%-]{1,60}$")


def extract_slug(source: str, url: str) -> str | None:
    """Board slug from a crawled URL, or None when the URL is not a company board page."""
    try:
        p = urlparse(url if "//" in url else "https://" + url)
    except ValueError:
        return None
    host = (p.netloc or "").lower().split(":")[0]
    parts = [x for x in p.path.split("/") if x]
    slug = None
    if source == "recruitee":
        if host.endswith(".recruitee.com") and host.count(".") == 2:
            slug = host.split(".")[0]
    elif source == "greenhouse" and parts and parts[0] == "embed":
        slug = (parse_qs(p.query).get("for") or [None])[0]
    elif parts:
        slug = parts[0]
    if not slug:
        return None
    slug = slug.strip()
    if slug.lower() in SKIP or not SLUG_OK.match(slug):
        return None
    return slug


def latest_index() -> str:
    r = requests.get(f"{CC}/collinfo.json", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()[0]["cdx-api"]          # newest collection first


def crawl_slugs(source: str, max_pages: int, api: str) -> set[str]:
    found: set[str] = set()
    for pattern, match in SOURCES[source]:
        params = {"url": pattern, "matchType": match, "output": "json", "fl": "url", "filter": "=status:200"}
        try:
            r = requests.get(api, params={**params, "showNumPages": "true"}, headers=HEADERS, timeout=60)
            pages = int(r.json().get("pages", 1)) if r.ok else 0
        except (requests.RequestException, ValueError):
            pages = 0
        for page in range(min(pages, max_pages)):
            for attempt in range(3):
                try:
                    r = requests.get(api, params={**params, "page": page}, headers=HEADERS, timeout=120)
                    if r.status_code == 200:
                        for line in r.text.splitlines():
                            try:
                                s = extract_slug(source, json.loads(line)["url"])
                            except (ValueError, KeyError):
                                continue
                            if s:
                                found.add(s)
                        break
                except requests.RequestException:
                    pass
                time.sleep(3 * (attempt + 1))
        print(f"  {source}: {len(found)} candidate slugs after {pattern}", flush=True)
    return found


def pretty(slug: str) -> str:
    return re.sub(r"[-_.]+", " ", slug).strip().title()


def run(sources: list[str], max_pages: int, extra: list[tuple[str, str]], workers: int = 16) -> dict:
    api = latest_index()
    print(f"Using Common Crawl index: {api}")
    path = config.ROOT / "companies.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    have = {(c["source"], str(c["slug"]).lower()) for c in existing}

    cands: list[tuple[str, str]] = list(extra)
    for s in sources:
        cands += [(s, slug) for slug in sorted(crawl_slugs(s, max_pages, api))]
    cands = [(s, slug) for s, slug in dict.fromkeys(cands) if (s, slug.lower()) not in have]
    print(f"{len(cands)} new candidates to verify")

    done, found = [0], []

    def work(c):
        s, slug = c
        ok = discover.verify(s, slug)
        done[0] += 1
        if done[0] % 100 == 0:
            print(f"  verified {done[0]}/{len(cands)}  ({len([x for x in found if x])} valid)", flush=True)
        return {"name": pretty(slug), "source": s, "slug": slug, "sector": "Discovered (startups & tech)"} if ok else None

    with ThreadPoolExecutor(workers) as ex:
        for res in ex.map(work, cands):
            found.append(res)
            if len(found) % 500 == 0:                       # checkpoint so a crash loses little
                path.write_text(json.dumps(discover.merge(existing, [f for f in found if f]), indent=2, ensure_ascii=False), encoding="utf-8")
    good = [f for f in found if f]
    path.write_text(json.dumps(discover.merge(existing, good), indent=2, ensure_ascii=False), encoding="utf-8")
    return {"candidates": len(cands), "added": len(good), "total": len(existing) + len(good)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sources", nargs="+", default=list(SOURCES), choices=list(SOURCES))
    ap.add_argument("--max-pages", type=int, default=20, help="Common Crawl result pages per URL pattern (more = more companies, slower)")
    ap.add_argument("--slugs", help="text file with extra 'source slug' lines to verify and add")
    ap.add_argument("--workers", type=int, default=16)
    a = ap.parse_args()
    extra = []
    if a.slugs:
        for line in open(a.slugs, encoding="utf-8"):
            bits = line.split()
            if len(bits) == 2 and bits[0] in SOURCES:
                extra.append((bits[0], bits[1]))
    r = run(a.sources, a.max_pages, extra, a.workers)
    print(f"Done. Verified and added {r['added']} companies ({r['candidates']} checked). companies.json now has {r['total']}.")
    print("Next: python run_pipeline.py")


if __name__ == "__main__":
    sys.exit(main())
