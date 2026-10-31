"""Match a job to a user: cheap rule checks first, then OpenAI (or a free keyword fallback)."""
import json
import re

from pydantic import BaseModel, Field

from app import config
from app.filters import JOB_TYPES


class MatchResult(BaseModel):
    score: int = Field(0, ge=0, le=100)
    eligible: bool = True
    matched_skills: list[str] = []
    missing_skills: list[str] = []
    reason: str = ""


SKILL_VOCAB = [
    "python", "java", "c++", "c#", "javascript", "typescript", "golang", "rust", "kotlin", "swift", "sql",
    "react", "angular", "vue", "node.js", "express", "django", "flask", "fastapi", "spring", "html", "css",
    "tailwind", "next.js", "mongodb", "postgresql", "mysql", "redis", "firebase", "aws", "gcp", "azure",
    "docker", "kubernetes", "git", "linux", "rest", "graphql", "microservices", "ci/cd", "terraform",
    "machine learning", "deep learning", "nlp", "pytorch", "tensorflow", "scikit-learn", "pandas", "numpy",
    "data structures", "algorithms", "system design", "oop", "dbms", "operating systems", "computer networks",
    "android", "flutter", "spark", "kafka", "tableau", "power bi", "excel", "selenium", "playwright",
]
_PATTERNS = {s: re.compile(r"(?<![\w+#])" + re.escape(s) + r"(?![\w+#])", re.I) for s in SKILL_VOCAB}
SENIOR_WORDS = re.compile(r"\b(senior|sr\.?|staff|principal|lead|manager|director|head of|architect|vp)\b", re.I)
_BATCH = re.compile(
    r"(?:batch|graduat\w*|class of|pass(?:ing)?[\s-]*out)[^.\n]{0,80}|[^.\n]{0,40}(?:batch|graduates?|pass(?:ing)?[\s-]*out)",
    re.I,
)
_YEAR = re.compile(r"\b(20(?:2\d|3[0-5]))\b")
_CGPA = [
    re.compile(r"(\d(?:\.\d{1,2})?)\s*\+?\s*(?:cgpa|gpa)", re.I),
    re.compile(r"(?:cgpa|gpa)[^.\n\d]{0,30}(\d(?:\.\d{1,2})?)", re.I),
]


_EXP = re.compile(r"(\d{1,2})\s*\+?\s*(?:(?:-|–|to)\s*\d{1,2}\s*)?(?:years?|yrs?)\b[^.\n]{0,40}?\b(?:experience|exp)\b", re.I)
_EXP_MIN = re.compile(r"(?:minimum|min\.?|at least)(?: of)?\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.I)


def required_years(text: str) -> int | None:
    """Smallest years-of-experience requirement stated in a job text, or None."""
    vals = [int(m.group(1)) for m in _EXP.finditer(text or "")] + [int(m.group(1)) for m in _EXP_MIN.finditer(text or "")]
    return min(vals) if vals else None


def extract_skills(text: str) -> set[str]:
    return {s for s, p in _PATTERNS.items() if p.search(text or "")}


_WORLDWIDE = re.compile(r"anywhere|worldwide|world-wide|\bglobal\b|any country|all countries|\bapac\b|\basia\b|asia[- ]pacific", re.I)
_DESC_OPEN = re.compile(r"work from anywhere|anywhere in the world|remote[^.\n]{0,60}(?:worldwide|anywhere|globally|any country)", re.I)


def _open_to(wanted: set, location: str, description: str) -> bool:
    """A remote job tied to a foreign country is still for you if its location says worldwide/APAC/Asia, or its text says it
    hires in one of your countries ("remote, open to candidates in India")."""
    if _WORLDWIDE.search(location or ""):
        return True
    head = (description or "")[:4000]
    if _DESC_OPEN.search(head):
        return True
    return any(re.search(r"(?:remote|hiring|open to|candidates?|applicants?|eligible|based|located|work(?:ing)?)\W+(?:\w+\W+){0,6}?" + re.escape(c),
                         head, re.I) for c in wanted)


def rule_check(profile, job) -> tuple[bool, str]:
    """Free eligibility filter: seniority, target role, graduation batch, CGPA."""
    title, text = job.title or "", f"{job.title} {job.description}"
    if SENIOR_WORDS.search(title):
        return False, "Senior-level role."
    need, have = required_years(text), float(getattr(profile, "experience_years", 0) or 0)
    if need is not None and need > have + 1:
        return False, f"Needs {need}+ years of experience; you have {have:g}."
    roles = [r.lower() for r in (profile.target_roles or []) if r.strip()]
    if roles and not any(r in title.lower() for r in roles):
        return False, "Title doesn't match your target roles."
    lo, hi = getattr(job, "salary_min", None), getattr(job, "salary_max", None)
    min_lpa = 7.0 if getattr(profile, "min_lpa", None) is None else profile.min_lpa
    if hi is not None and hi < min_lpa:
        return False, f"Pays up to {hi:g} LPA, below your minimum of {min_lpa:g}."
    if lo is None and getattr(profile, "require_salary", False):
        return False, "Salary not listed."
    pref = getattr(profile, "remote_pref", None) or "Any"
    remote = bool(getattr(job, "remote", False))
    if pref == "Remote only" and not remote:
        return False, "Not a remote job."
    if pref == "On-site/hybrid only" and remote:
        return False, "Remote job."
    wanted = {c for c in (getattr(profile, "countries", None) or []) if c}
    where = {c.strip() for c in (getattr(job, "country", "") or "").split(",") if c.strip()}
    if wanted and where and not (wanted & where):
        # a remote job tied to one foreign country ("Remote - US") is usually closed to others; only worldwide ones pass
        if not (remote and _open_to(wanted, getattr(job, "location", ""), job.description)):
            return False, f"Located in {', '.join(sorted(where))}, outside your countries."
    types = {t for t in (getattr(profile, "job_types", None) or []) if t}
    jt = getattr(job, "job_type", "") or ""
    if types and jt in JOB_TYPES and jt not in types:
        return False, f"{jt} role; you want {', '.join(sorted(types))}."
    years = {int(y) for seg in _BATCH.findall(text) for y in _YEAR.findall(seg)}
    if years and profile.grad_year and profile.grad_year not in years:
        return False, f"Targets batch {', '.join(map(str, sorted(years)))}; yours is {profile.grad_year}."
    if profile.cgpa:
        for pat in _CGPA:
            for m in pat.finditer(text):
                need = float(m.group(1))
                if 5 <= need <= 10 and need > profile.cgpa:
                    return False, f"Requires CGPA {need}; yours is {profile.cgpa}."
    return True, ""


def keyword_score(profile, job) -> MatchResult:
    need = extract_skills(f"{job.title} {job.description}")
    have = extract_skills(profile.resume_text or "") | {s.lower() for s in (profile.skills or [])}
    matched, missing = sorted(need & have), sorted(need - have)
    score = round(100 * len(matched) / len(need)) if need else 0
    reason = f"Keyword match: {len(matched)}/{len(need)} required skills found." if need else "No known skills listed in the JD."
    return MatchResult(score=score, eligible=True, matched_skills=matched, missing_skills=missing, reason=reason)


def llm_score(profile, job) -> MatchResult:
    from openai import OpenAI

    client = OpenAI(api_key=config.OPENAI_API_KEY)
    system = (
        "You score how well a student's resume fits a job. Reply with JSON only: "
        '{"score":0-100,"eligible":bool,"matched_skills":[str],"missing_skills":[str],"reason":"one sentence"}. '
        "Set eligible=false if the job clearly needs more experience, a different graduation batch, or a higher CGPA."
    )
    user = (
        f"Student: batch {profile.grad_year}, branch {profile.branch}, CGPA {profile.cgpa}.\n"
        f"RESUME:\n{(profile.resume_text or '')[:6000]}\n\n"
        f"JOB: {job.title} at {job.company}\n{(job.description or '')[:6000]}"
    )
    resp = client.chat.completions.create(
        model=config.OPENAI_MODEL,
        response_format={"type": "json_object"},
        temperature=0,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    return MatchResult.model_validate(json.loads(resp.choices[0].message.content))


def score_job(profile, job) -> MatchResult:
    ok, why = rule_check(profile, job)
    if not ok:
        return MatchResult(score=0, eligible=False, reason=why)
    if config.OPENAI_API_KEY:
        try:
            return llm_score(profile, job)
        except Exception as e:  # network, quota, bad JSON -> free fallback
            print(f"LLM failed ({e}); using keyword matcher.")
    return keyword_score(profile, job)