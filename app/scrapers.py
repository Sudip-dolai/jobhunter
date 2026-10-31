"""Scrape public job-board APIs: Greenhouse, Lever, Ashby, Workday. Each job is stored once for all users."""
import hashlib
import html
import importlib.util
import json
import re
import threading
import time
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests

from app import config
from app.filters import ISO, RATES, detect_countries, detect_job_type, detect_remote, extract_salary_lpa, is_software_role, norm_type

TIMEOUT = 30
JOOBLE_PAGES = 2
JOOBLE_QUERIES = ["software engineer", "software developer", "graduate engineer trainee"]
BIG_EMPLOYERS = [
    ("Tata Consultancy Services", ["tata consultancy", "tcs"]), ("Infosys", ["infosys"]), ("HCLTech", ["hcl"]),
    ("Accenture", ["accenture"]), ("Wipro", ["wipro"]), ("Cognizant", ["cognizant"]), ("Capgemini", ["capgemini"]),
    ("Tech Mahindra", ["tech mahindra"]), ("LTIMindtree", ["ltimindtree", "lti mindtree", "l&t infotech"]),
    ("IBM", ["ibm"]), ("Deloitte", ["deloitte"])]
FEED_SOURCES = {"jooble", "adzuna"}
_STATE = config.ROOT / ".feed_state.json"
ADZUNA_PAGES = 5               # 50 jobs per page
ADZUNA_QUERIES = ["software engineer", "software developer", "graduate engineer trainee"]
_ADZUNA_CUR = {"in": 1, "us": RATES["usd"], "gb": RATES["gbp"], "ca": RATES["cad"], "au": RATES["aud"], "sg": RATES["sgd"],
               "de": RATES["eur"], "fr": RATES["eur"], "nl": RATES["eur"], "ie": RATES["eur"], "es": RATES["eur"]}
WORKDAY_TERMS = ["software engineer", "developer"]
WORKDAY_MAX_LIST = 100       # max postings listed per company per run
WORKDAY_MAX_DETAIL = 40      # max new descriptions fetched per company per run
MAX_DETAIL = 40              # same cap for SmartRecruiters
MAX_JSONLD_LIST = 200
MAX_JSONLD_DETAIL = 60       # max new job pages fetched per custom site per run
UA = {"User-Agent": "Mozilla/5.0 (compatible; JobHunterBot/1.0)"}
PAGE_DELAY = 0.4              # seconds between page requests on the same site (be polite)
MAX_PAGE_JOBS = 60
MAX_PAGE_DETAIL = 30          # max new job pages opened per career page per run
_ROBOTS: dict = {}
_RENDER_LOCK = threading.Lock()


def clean_html(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _greenhouse_response(slug):
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"}, timeout=TIMEOUT)
    r.raise_for_status()
    return r


def _greenhouse_map(name, items):
    return [
        dict(source="greenhouse", company=name, external_id=str(j["id"]), title=j.get("title", ""),
             location=(j.get("location") or {}).get("name", ""), url=j.get("absolute_url", ""),
             description=clean_html(j.get("content", "")))
        for j in items
    ]


def _greenhouse(name, slug, known, opts=None):
    return _greenhouse_map(name, _greenhouse_response(slug).json().get("jobs", []))


def _lever(name, slug, known, opts=None):
    r = requests.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}, timeout=TIMEOUT)
    r.raise_for_status()
    return [
        dict(source="lever", company=name, external_id=str(j["id"]), title=j.get("text", ""),
             location=(j.get("categories") or {}).get("location", "") or "", url=j.get("hostedUrl", ""),
             description=clean_html(j.get("descriptionPlain") or j.get("description", "")),
             remote=(j.get("workplaceType") == "remote"), job_type=norm_type((j.get("categories") or {}).get("commitment", "")))
        for j in r.json()
    ]


def _ashby_response(slug):
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", params={"includeCompensation": "true"}, timeout=TIMEOUT)
    r.raise_for_status()
    return r


def _ashby_map(name, items, extras=False):
    """extras=True (v2 collector only) also captures publishedAt and applyUrl, both documented by Ashby."""
    out = []
    for j in items:
        if j.get("isListed") is False:
            continue
        comp = (j.get("compensation") or {}).get("compensationTierSummary", "") or ""
        desc = clean_html(j.get("descriptionPlain") or j.get("descriptionHtml", "")) + " " + comp
        row = dict(source="ashby", company=name, external_id=str(j["id"]), title=j.get("title", ""),
                   location=j.get("location", "") or "", url=j.get("jobUrl", ""), description=desc.strip(),
                   remote=bool(j.get("isRemote")), job_type=norm_type(j.get("employmentType", "")))
        if extras:
            row["apply_url"] = j.get("applyUrl") or None
            row["posted_at"] = j.get("publishedAt") or None
        out.append(row)
    return out


def _ashby(name, slug, known, opts=None):
    return _ashby_map(name, _ashby_response(slug).json().get("jobs", []))


def _workday(name, slug, known, opts=None):
    """slug format: tenant|wdN|site  e.g. nvidia|wd5|NVIDIAExternalCareerSite"""
    tenant, wd, site = slug.split("|")
    base = f"https://{tenant}.{wd}.myworkdayjobs.com"
    api = f"{base}/wday/cs/{tenant}/{site}"
    postings: dict[str, dict] = {}
    for term in WORKDAY_TERMS:
        offset = 0
        while offset < WORKDAY_MAX_LIST:
            r = requests.post(f"{api}/jobs", timeout=TIMEOUT,
                              json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": term})
            r.raise_for_status()
            data = r.json()
            for p in data.get("jobPostings", []):
                if p.get("externalPath") and is_software_role(p.get("title", "")):
                    postings.setdefault(p["externalPath"], p)
            offset += 20
            if offset >= data.get("total", 0) or not data.get("jobPostings"):
                break
    out, fetched = [], 0
    for path, p in postings.items():
        ext_id = path.rsplit("/", 1)[-1][:100]
        if ext_id in known:
            continue
        desc, loc = "", p.get("locationsText", "")
        if fetched < WORKDAY_MAX_DETAIL:
            fetched += 1
            try:
                d = requests.get(f"{api}{path}", timeout=TIMEOUT)
                d.raise_for_status()
                info = d.json().get("jobPostingInfo", {})
                desc, loc = clean_html(info.get("jobDescription", "")), info.get("location", loc)
            except requests.RequestException:
                pass
        out.append(dict(source="workday", company=name, external_id=ext_id, title=p.get("title", ""),
                        location=loc, url=f"{base}/en-US/{site}{path}", description=desc))
    return out


def _workable(name, slug, known, opts=None):
    r = requests.get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", params={"details": "true"}, timeout=TIMEOUT)
    r.raise_for_status()
    return [
        dict(source="workable", company=name, external_id=str(j.get("shortcode") or j.get("id")), title=j.get("title", ""),
             location=", ".join(x for x in [j.get("city"), j.get("state"), j.get("country")] if x),
             url=j.get("url") or j.get("shortlink", ""), description=clean_html(j.get("description", "")),
             remote=bool(j.get("telecommuting")), job_type=norm_type(j.get("employment_type", "")))
        for j in r.json().get("jobs", [])
    ]


def _smartrecruiters(name, slug, known, opts=None):
    api, out, offset, detail = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", [], 0, 0
    while offset < 500:
        r = requests.get(api, params={"limit": 100, "offset": offset}, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        items = data.get("content", [])
        if not items:
            break
        for j in items:
            ext = str(j["id"])
            if ext in known or not is_software_role(j.get("name", "")):
                continue
            loc = j.get("location") or {}
            country = ISO.get(str(loc.get("country", "")).lower(), loc.get("country", ""))
            desc = ""
            if detail < MAX_DETAIL:
                detail += 1
                try:
                    d = requests.get(f"{api}/{ext}", timeout=TIMEOUT).json()
                    secs = (d.get("jobAd") or {}).get("sections") or {}
                    desc = clean_html(" ".join((s or {}).get("text", "") for s in secs.values()))
                except (requests.RequestException, ValueError):
                    pass
            out.append(dict(source="smartrecruiters", company=name, external_id=ext, title=j.get("name", ""),
                            location=", ".join(x for x in [loc.get("city"), country] if x),
                            url=f"https://jobs.smartrecruiters.com/{slug}/{ext}", description=desc,
                            remote=bool(loc.get("remote")), job_type=norm_type((j.get("typeOfEmployment") or {}).get("label", ""))))
        offset += 100
        if offset >= data.get("totalFound", 0):
            break
    return out


def _recruitee(name, slug, known, opts=None):
    r = requests.get(f"https://{slug}.recruitee.com/api/offers/", timeout=TIMEOUT)
    r.raise_for_status()
    return [
        dict(source="recruitee", company=name, external_id=str(j["id"]), title=j.get("title", ""),
             location=j.get("location") or ", ".join(x for x in [j.get("city"), j.get("country")] if x),
             url=j.get("careers_url", ""), description=clean_html((j.get("description") or "") + " " + (j.get("requirements") or "")),
             remote=bool(j.get("remote")), job_type=norm_type(j.get("employment_type_code", "")))
        for j in r.json().get("offers", [])
    ]


def _jooble(name, slug, known, opts=None):
    """Job-search feed (free key). slug = "location|search words". With opts["match"], only that employer's jobs are kept."""
    location, _, query = slug.partition("|")
    match = [m.lower() for m in (opts or {}).get("match", [])]
    out = []
    for page in range(1, (1 if match else JOOBLE_PAGES) + 1):
        r = requests.post(f"https://jooble.org/api/{config.JOOBLE_API_KEY}", timeout=TIMEOUT,
                          json={"keywords": query, "location": location, "page": str(page)})
        r.raise_for_status()
        jobs = r.json().get("jobs", [])
        if not jobs:
            break
        for j in jobs:
            company = (j.get("company") or "").strip() or "Unknown"
            if match and not any(m in company.lower() for m in match):
                continue
            loc = j.get("location", "") or ""
            if location.lower() not in loc.lower():
                loc = ", ".join(x for x in [loc, location] if x)
            link = j.get("link", "")
            out.append(dict(source="jooble", company=company[:120],
                            external_id=str(j.get("id") or hashlib.sha1(link.encode()).hexdigest()[:20]),
                            title=clean_html(j.get("title", "")), location=loc, url=link,
                            description=(clean_html(j.get("snippet", "")) + " " + (j.get("salary") or "")).strip(),
                            job_type=norm_type(j.get("type", ""))))
        if len(jobs) < 20:
            break
    return out


def _adzuna(name, slug, known, opts=None):
    """Job-search feed (needs a free key). slug = "country|search words". Covers employers whose own sites we can't read."""
    country, _, query = slug.partition("|")
    out = []
    for page in range(1, ADZUNA_PAGES + 1):
        r = requests.get(f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}", timeout=TIMEOUT,
                         params={"app_id": config.ADZUNA_APP_ID, "app_key": config.ADZUNA_APP_KEY, "results_per_page": 50,
                                 "what": query, "sort_by": "date", "content-type": "application/json"})
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            break
        for j in results:
            rate = _ADZUNA_CUR.get(country)
            lo = hi = None
            if rate and str(j.get("salary_is_predicted", "0")) != "1" and j.get("salary_min"):
                lo = float(j["salary_min"]) * rate / 1e5
                hi = float(j.get("salary_max") or j["salary_min"]) * rate / 1e5
            area = (j.get("location") or {}).get("display_name", "")
            out.append(dict(source="adzuna", company=((j.get("company") or {}).get("display_name") or "Unknown")[:120],
                            external_id=str(j["id"]), title=j.get("title", ""),
                            location=", ".join(x for x in [area, ISO.get(country, "")] if x), url=j.get("redirect_url", ""),
                            description=clean_html(j.get("description", "")),
                            job_type=norm_type(j.get("contract_type") or j.get("contract_time") or ""),
                            salary_min=lo, salary_max=hi))
        if len(results) < 50:
            break
    return out


# ---------- custom career sites: schema.org JobPosting (JSON-LD) found on job pages ----------
JOB_LINK = re.compile(r"/(?:jobs?|careers?|positions?|openings?|vacanc\w*|requisitions?)/[^\s\"'<>]+", re.I)
_NOISE_WORDS = {"jobs", "job", "careers", "career", "position", "positions", "opening", "openings", "view", "apply", "details", "search"}


def _robots_ok(url: str) -> bool:
    """Respect robots.txt. If it can't be read, reading is allowed."""
    parts = urlparse(url)
    host = f"{parts.scheme}://{parts.netloc}"
    if host not in _ROBOTS:
        rp = None
        try:
            r = requests.get(host + "/robots.txt", timeout=10, headers=UA)
            if r.status_code == 200:
                rp = robotparser.RobotFileParser()
                rp.parse(r.text.splitlines())
        except requests.RequestException:
            pass
        _ROBOTS[host] = rp
    rp = _ROBOTS[host]
    return True if rp is None else rp.can_fetch("JobHunterBot", url)


def _get_text(url: str, render: bool = False) -> str:
    if not _robots_ok(url):
        raise PermissionError(f"robots.txt of this site does not allow reading {url}")
    if render:  # JavaScript sites; needs: pip install playwright && playwright install chromium
        from playwright.sync_api import sync_playwright

        with _RENDER_LOCK, sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=UA["User-Agent"])
            page.goto(url, timeout=60000)
            page.wait_for_load_state("networkidle", timeout=60000)
            for _ in range(6):   # scroll and press "Load more" so long lists are fully loaded
                page.mouse.wheel(0, 20000)
                page.wait_for_timeout(700)
                try:
                    page.locator("button:has-text('Load more'), button:has-text('Show more'), a:has-text('Load more')").first.click(timeout=800)
                except Exception:
                    pass
            text = page.content()
            browser.close()
            return text
    r = requests.get(url, timeout=TIMEOUT, headers=UA)
    r.raise_for_status()
    return r.text


def _locs(xml: str) -> list[str]:
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)


def _find_job_urls(url: str, text: str, pattern: str | None = None) -> list[str]:
    pat = re.compile(pattern, re.I) if pattern else JOB_LINK
    if "<sitemapindex" in text:
        found = []
        for child in _locs(text)[:10]:
            try:
                found += _locs(_get_text(child))
            except Exception:
                continue
    elif "<urlset" in text:
        found = _locs(text)
    else:
        found = [urljoin(url, h) for h in re.findall(r"href=[\"']([^\"'#]+)[\"']", text)]
    out, seen = [], set()
    for u in found:
        if u.startswith("http") and pat.search(u) and u not in seen:
            seen.add(u)
            out.append(u)
    return out[:MAX_JSONLD_LIST]


def _title_slug_ok(u: str) -> bool:
    """Skip pages whose URL clearly names a non-software role. URLs without a readable title are fetched."""
    words = [w for w in re.findall(r"[a-zA-Z]{3,}", urlparse(u).path) if w.lower() not in _NOISE_WORDS]
    return len(words) < 2 or is_software_role(" ".join(words))


def _nodes(obj):
    if isinstance(obj, list):
        for x in obj:
            yield from _nodes(x)
    elif isinstance(obj, dict):
        yield obj
        if isinstance(obj.get("@graph"), list):
            yield from _nodes(obj["@graph"])


def parse_jobposting(page_html: str) -> dict | None:
    for block in re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", page_html, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        for node in _nodes(data):
            t = node.get("@type")
            if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                return _normalize_posting(node)
    return None


def _normalize_posting(node: dict) -> dict:
    locs = node.get("jobLocation") or []
    parts = []
    for loc in locs if isinstance(locs, list) else [locs]:
        a = (loc or {}).get("address") or {}
        if isinstance(a, str):
            parts.append(a)
            continue
        country = a.get("addressCountry")
        country = country.get("name") if isinstance(country, dict) else country
        country = ISO.get(str(country).lower(), country) if country else ""
        parts.append(", ".join(x for x in [a.get("addressLocality"), a.get("addressRegion"), country] if x))
    sal = ""
    bs = node.get("baseSalary") or {}
    v = bs.get("value") if isinstance(bs, dict) else None
    if isinstance(v, dict) and str(v.get("unitText", "")).upper() == "YEAR":
        cur = bs.get("currency") or v.get("currency") or ""
        lo = v.get("minValue") or v.get("value")
        hi = v.get("maxValue") or lo
        try:
            sal = f"{cur} {float(lo):,.0f} - {float(hi):,.0f} per year"
        except (TypeError, ValueError):
            sal = ""
    emp = node.get("employmentType")
    emp = emp[0] if isinstance(emp, list) and emp else emp
    return dict(title=node.get("title", ""), location=" | ".join(p for p in parts if p),
                description=(clean_html(node.get("description", "")) + " " + sal).strip(),
                remote=str(node.get("jobLocationType", "")).upper() == "TELECOMMUTE", job_type=norm_type(str(emp or "")),
                # schema.org datePosted / validThrough (used only by the v2 ingest; the v1 insert path drops these keys)
                posted_at=node.get("datePosted") or None, valid_through=node.get("validThrough") or None)


_ANCHOR = re.compile(r"<a\b[^>]*?href=[\"']([^\"'#][^\"']*)[\"'][^>]*>(.*?)</a>", re.I | re.S)


def _link_jobs(base_url: str, page_html: str) -> list[tuple[str, str]]:
    """(title, url) for every link on the page whose text looks like a software job."""
    out, seen = [], set()
    for href, inner in _ANCHOR.findall(page_html):
        if href.lower().startswith(("mailto:", "javascript:", "tel:")):
            continue
        title = clean_html(inner)
        if not (6 <= len(title) <= 160) or not is_software_role(title):
            continue
        url = urljoin(base_url, href)
        if url.rstrip("/") == base_url.rstrip("/") or url in seen:
            continue
        seen.add(url)
        out.append((title[:200], url))
    return out


def _page(name, slug, known, opts=None):
    """Any careers page. slug = the page that LISTS jobs. Finds links that look like software jobs and opens each one.
    Uses a real browser automatically if the plain page shows no jobs and Playwright is installed ("render": true forces it)."""
    opts = opts or {}
    render = bool(opts.get("render"))
    links = _link_jobs(slug, _get_text(slug, render))
    if not links and not render and importlib.util.find_spec("playwright"):
        try:
            links = _link_jobs(slug, _get_text(slug, True))
            render = bool(links)
        except Exception:
            pass   # browser not installed or page blocked: keep the plain result
    out, detail = [], 0
    for title, url in links[:MAX_PAGE_JOBS]:
        ext = hashlib.sha1(url.encode()).hexdigest()[:20]
        if ext in known:
            continue
        job = dict(source="page", company=name, external_id=ext, title=title, location="", url=url, description="")
        if detail < MAX_PAGE_DETAIL:
            detail += 1
            time.sleep(PAGE_DELAY)
            try:
                page_html = _get_text(url, render)
                p = parse_jobposting(page_html)
                if p:
                    job.update(title=p["title"] or title, location=p["location"], description=p["description"],
                               remote=p["remote"], job_type=p["job_type"])
                else:
                    job["description"] = clean_html(re.sub(r"<(script|style)\b.*?</\1>", " ", page_html, flags=re.S | re.I))[:6000]
            except Exception:
                pass
        out.append(job)
    return out


def _jsonld(name, slug, known, opts=None):
    """slug = a careers listing page or a sitemap URL. Optional: "pattern" (regex for job URLs), "render": true."""
    opts = opts or {}
    render = bool(opts.get("render"))
    urls = _find_job_urls(slug, _get_text(slug, render), opts.get("pattern"))
    out, fetched = [], 0
    for u in urls:
        ext = hashlib.sha1(u.encode()).hexdigest()[:20]
        if ext in known or not _title_slug_ok(u):
            continue
        if fetched >= MAX_JSONLD_DETAIL:
            break
        fetched += 1
        try:
            p = parse_jobposting(_get_text(u, render))
        except Exception:
            continue
        if p:
            out.append(dict(source="jsonld", company=name, external_id=ext, title=p["title"], location=p["location"],
                            url=u, description=p["description"], remote=p["remote"], job_type=p["job_type"]))
    return out


FETCHERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby, "workday": _workday,
            "jooble": _jooble, "adzuna": _adzuna, "page": _page, "workable": _workable, "smartrecruiters": _smartrecruiters, "recruitee": _recruitee, "jsonld": _jsonld}


def enrich_jobs(company: dict, raw_jobs) -> list[dict]:
    """Software-role filter plus sector, remote, job type, country and salary. Shared by fetch_jobs and the v2 collector."""
    jobs = []
    for j in raw_jobs:
        if config.INGEST_SCOPE == "software" and not is_software_role(j["title"]):
            continue
        j["sector"] = company.get("sector", "")
        j["remote"] = bool(j.get("remote")) or detect_remote(j.get("location", ""), j["title"], j["description"])
        j["job_type"] = j.get("job_type") or detect_job_type(j["title"], j["description"])
        j["country"] = ", ".join(detect_countries(j.get("location", "")))
        if j.get("salary_min") is None:   # feeds may already carry a salary
            sal = extract_salary_lpa(j["description"])
            j["salary_min"], j["salary_max"] = sal if sal else (None, None)
        jobs.append(j)
    return jobs


def fetch_jobs(company: dict, known=None) -> list[dict]:
    """Fetch one company's jobs; keep only software roles and attach sector and salary."""
    fn = FETCHERS.get(company["source"])
    if not fn:
        raise ValueError(f"Unknown source: {company['source']}")
    return enrich_jobs(company, fn(company["name"], company["slug"], known or set(), company))


def load_feeds() -> list[dict]:
    """Search feeds that are added automatically when an Adzuna key is set in .env."""
    feeds: list[dict] = []
    if config.JOOBLE_API_KEY:
        feeds += [{"name": f"Jooble {loc}: {q}", "source": "jooble", "slug": f"{loc}|{q}", "sector": ""}
                  for loc in config.JOOBLE_LOCATIONS for q in JOOBLE_QUERIES]
        feeds += [{"name": f"Jooble: {c}", "source": "jooble", "slug": f"{config.JOOBLE_LOCATIONS[0]}|{c} software engineer",
                   "sector": "IT Services", "match": m} for c, m in BIG_EMPLOYERS]
    if config.ADZUNA_APP_ID and config.ADZUNA_APP_KEY:
        feeds += [{"name": f"Adzuna {c.upper()}: {q}", "source": "adzuna", "slug": f"{c}|{q}", "sector": ""}
                  for c in config.ADZUNA_COUNTRIES for q in ADZUNA_QUERIES]
    return feeds


def _feed_state() -> dict:
    try:
        return json.loads(_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def feeds_due(feeds: list[dict]) -> list[dict]:
    """Search feeds only run every FEED_INTERVAL_HOURS, so API limits are not used up."""
    state, now = _feed_state(), time.time()
    return [f for f in feeds if now - state.get(f["name"], 0) >= config.FEED_INTERVAL_HOURS * 3600]


def mark_feeds(names: list[str]) -> None:
    state, now = _feed_state(), time.time()
    state.update({n: now for n in names})
    _STATE.write_text(json.dumps(state), encoding="utf-8")


def load_companies(path: Path | None = None) -> list[dict]:
    path = path or config.ROOT / "companies.json"
    return json.loads(Path(path).read_text(encoding="utf-8"))
