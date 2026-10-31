"""Sector list, software-role filter and salary (LPA) extraction."""
import re

SECTORS = [
    "AI & Data", "Cybersecurity", "Cloud & DevOps", "Product & Tech — Global", "Product & Tech — India",
    "BFSI & Fintech", "Consulting & Big 4", "IT Services", "E-commerce & Retail", "Healthcare Tech", "Edtech",
    "Semiconductor & Hardware", "Telecom", "SaaS & B2B Software", "Logistics & Supply Chain", "EV & AutoTech",
    "Media & Entertainment", "Manufacturing", "AgriTech", "PropTech & Real Estate", "Crypto & Web3", "HR Tech",
]

_INCLUDE = re.compile(
    r"\b(software|sde|swe|engineer|developer|programmer|back-?end|front-?end|full[\s-]?stack|devops|sre|sdet|qa|"
    r"android|ios|data scientist|machine learning)\b", re.I)
_EXCLUDE = re.compile(r"\b(mechanical|civil|chemical|electrical|sales|field service|manufacturing|customer success)\b", re.I)


def is_software_role(title: str) -> bool:
    return bool(_INCLUDE.search(title or "")) and not _EXCLUDE.search(title or "")


_N = r"(\d{1,3}(?:\.\d+)?)"
_UNIT = r"(?:lpa|lakhs?|lacs?)\b"
_RANGE = re.compile(rf"{_N}\s*(?:{_UNIT})?\s*(?:-|–|—|to)\s*{_N}\s*{_UNIT}", re.I)
_SINGLE = re.compile(rf"{_N}\s*{_UNIT}", re.I)
_INR = re.compile(r"(?:inr|₹|rs\.?)\s*(\d[\d,]{5,})(?:\s*(?:-|–|—|to)\s*(?:inr|₹|rs\.?)?\s*(\d[\d,]{5,}))?", re.I)


# Approximate INR per 1 unit of currency. Edit if you want different conversion rates.
RATES = {"usd": 85, "eur": 95, "gbp": 110, "cad": 62, "aud": 55, "sgd": 65}
_CUR = {"$": "usd", "us$": "usd", "usd": "usd", "£": "gbp", "gbp": "gbp", "€": "eur", "eur": "eur",
        "c$": "cad", "cad": "cad", "a$": "aud", "aud": "aud", "sgd": "sgd"}
_SYM = r"(?:us\$|c\$|a\$|\$|£|€|usd|gbp|eur|cad|aud|sgd)"
_FX = re.compile(rf"({_SYM})\s*(\d[\d,]*(?:\.\d+)?)\s*(k)?(?:\s*(?:-|–|—|to)\s*{_SYM}?\s*(\d[\d,]*(?:\.\d+)?)\s*(k)?)?", re.I)
_NOT_ANNUAL = re.compile(r"\b(hour|hr|hourly|month|monthly|week|weekly|day|daily)\b|/\s*(h|hr|mo|m)\b", re.I)


def _foreign_lpa(text: str) -> tuple[float, float] | None:
    """Annual salary in USD/EUR/GBP/CAD/AUD/SGD converted to LPA. Hourly or monthly pay is ignored."""
    for m in _FX.finditer(text):
        if _NOT_ANNUAL.search(text[m.end(): m.end() + 14]):
            continue
        cur = RATES[_CUR[m.group(1).lower()]]
        lo, hi = float(m.group(2).replace(",", "")), float((m.group(4) or m.group(2)).replace(",", ""))
        if m.group(3) or (m.group(5) and lo < 1000):
            lo *= 1000
        if m.group(5) or m.group(3):
            hi *= 1000
        lo, hi = min(lo, hi), max(lo, hi)
        if 5_000 <= lo and hi <= 5_000_000:
            return lo * cur / 1e5, hi * cur / 1e5
    return None


def extract_salary_lpa(text: str) -> tuple[float, float] | None:
    """Find a salary in text and return (min, max) in lakhs per annum (INR), or None."""
    text = text or ""
    if m := _RANGE.search(text):
        lo, hi = float(m.group(1)), float(m.group(2))
    elif m := _INR.search(text):
        lo = int(m.group(1).replace(",", "")) / 1e5
        hi = int(m.group(2).replace(",", "")) / 1e5 if m.group(2) else lo
    elif m := _SINGLE.search(text):
        lo = hi = float(m.group(1))
    else:
        return _foreign_lpa(text)
    lo, hi = min(lo, hi), max(lo, hi)
    return (lo, hi) if 1 <= lo and hi <= 500 else None


# ---------- country, remote and job type detection ----------
COUNTRIES = ["India", "United States", "United Kingdom", "Canada", "Germany", "Singapore", "Australia", "Ireland",
             "Netherlands", "France", "Israel", "Sweden", "Switzerland", "United Arab Emirates", "Japan", "Spain",
             "Poland", "Brazil"]
JOB_TYPES = ["Permanent", "Contract", "Part-time", "Internship"]
REMOTE_PREFS = ["Any", "Remote only", "On-site/hybrid only"]
ISO = {"in": "India", "us": "United States", "gb": "United Kingdom", "uk": "United Kingdom", "ca": "Canada",
       "de": "Germany", "sg": "Singapore", "au": "Australia", "ie": "Ireland", "nl": "Netherlands", "fr": "France",
       "il": "Israel", "se": "Sweden", "ch": "Switzerland", "ae": "United Arab Emirates", "jp": "Japan",
       "es": "Spain", "pl": "Poland", "br": "Brazil"}
_PLACES = {
    "India": ["india", "bangalore", "bengaluru", "mumbai", "delhi", "gurgaon", "gurugram", "noida", "hyderabad", "pune",
              "chennai", "kolkata", "ahmedabad", "jaipur", "kochi", "coimbatore", "indore", "chandigarh", "trivandrum"],
    "United States": ["united states", "usa", "new york", "san francisco", "seattle", "austin", "boston", "chicago",
                      "los angeles", "san jose", "mountain view", "menlo park", "sunnyvale", "palo alto", "redmond",
                      "atlanta", "denver", "san diego", "washington, dc", "cupertino", "santa clara"],
    "United Kingdom": ["united kingdom", "london", "manchester", "edinburgh", "england", "scotland", "belfast"],
    "Canada": ["canada", "toronto", "vancouver", "ottawa", "montreal", "waterloo", "calgary"],
    "Germany": ["germany", "berlin", "munich", "hamburg", "frankfurt"],
    "Singapore": ["singapore"],
    "Australia": ["australia", "sydney", "melbourne", "brisbane"],
    "Ireland": ["ireland", "dublin"],
    "Netherlands": ["netherlands", "amsterdam", "rotterdam"],
    "France": ["france", "paris"],
    "Israel": ["israel", "tel aviv"],
    "Sweden": ["sweden", "stockholm"],
    "Switzerland": ["switzerland", "zurich", "geneva"],
    "United Arab Emirates": ["united arab emirates", "dubai", "abu dhabi"],
    "Japan": ["japan", "tokyo"],
    "Spain": ["spain", "madrid", "barcelona"],
    "Poland": ["poland", "warsaw", "krakow"],
    "Brazil": ["brazil", "sao paulo", "são paulo"],
}
_PLACE_RE = [(c, re.compile(r"(?<![a-z])(?:" + "|".join(re.escape(p) for p in ps) + r")(?![a-z])", re.I)) for c, ps in _PLACES.items()]
_US_CASE = re.compile(r"\b(?:US|USA|U\.S\.)\b|,\s*(?:CA|NY|WA|TX|MA|IL|CO|GA|NJ|VA|FL|PA|NC|OR|AZ|MN)\b")
_UK_CASE = re.compile(r"\bUK\b")


def detect_countries(location: str) -> list[str]:
    """Countries named in a location string, in order of appearance. Empty list if unknown."""
    loc = location or ""
    hits: list[tuple[int, str]] = []
    for country, rx in _PLACE_RE:
        if m := rx.search(loc):
            hits.append((m.start(), country))
    if m := _US_CASE.search(loc):
        hits.append((m.start(), "United States"))
    if m := _UK_CASE.search(loc):
        hits.append((m.start(), "United Kingdom"))
    out: list[str] = []
    for _, c in sorted(hits):
        if c not in out:
            out.append(c)
    return out


_REMOTE_LOC = re.compile(r"\bremote\b|work from home|\bwfh\b|telecommut|anywhere", re.I)
_REMOTE_TEXT = re.compile(r"fully remote|100% remote|remote[- ]first|remote (?:position|role|job|opportunity)|work remotely|work from anywhere", re.I)


def detect_remote(location: str = "", title: str = "", text: str = "") -> bool:
    return bool(_REMOTE_LOC.search(f"{location} {title}") or _REMOTE_TEXT.search(text or ""))


def norm_type(s: str) -> str:
    s = (s or "").lower()
    if "intern" in s:
        return "Internship"
    if "part" in s:
        return "Part-time"
    if re.search(r"contract|temp|freelance", s):
        return "Contract"
    if re.search(r"full|perm", s):
        return "Permanent"
    return ""


_CONTRACT_TITLE = re.compile(r"\b(contract|contractor|freelance|temporary)\b", re.I)
_CONTRACT_TEXT = re.compile(r"contract (?:role|position|basis|to hire)|contract-to-hire|fixed[- ]term|freelance|temporary (?:role|position)", re.I)


def detect_job_type(title: str = "", text: str = "") -> str:
    """Permanent / Contract / Part-time / Internship, or 'Not stated'."""
    if re.search(r"\bintern(ship)?\b", title or "", re.I):
        return "Internship"
    if _CONTRACT_TITLE.search(title or "") or _CONTRACT_TEXT.search(text or ""):
        return "Contract"
    if re.search(r"part[- ]time", f"{title} {text}", re.I):
        return "Part-time"
    if re.search(r"full[- ]time|permanent", f"{title} {text}", re.I):
        return "Permanent"
    return "Not stated"
