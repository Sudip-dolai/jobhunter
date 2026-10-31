"""Decide what to type into application-form fields. Pure logic (no browser) so it can be tested.

- Known fields (name, email, phone, education, links) come from your profile.
- Sensitive questions (gender, salary, visa, relocation, ...) are NEVER answered automatically.
- Open questions ("Why do you want to work here?") are drafted by OpenAI from your resume, for you to review.
"""
import re

from app import config

SENSITIVE = re.compile(
    r"gender|\bsex\b|\brace\b|ethnic|disabilit|veteran|pronoun|religio|sexual orientation|caste|criminal|convict|"
    r"salary|compensation|\bctc\b|expected pay|notice period|relocat|referred by|how did you hear", re.I)

RULES = [
    (r"first\s*name|given\s*name", "first"),
    (r"last\s*name|surname|family\s*name", "last"),
    (r"e-?mail", "email"),
    (r"phone|mobile|contact\s*number", "phone"),
    (r"linkedin", "linkedin"),
    (r"github", "github"),
    (r"portfolio|website|personal\s*site|blog", "portfolio"),
    (r"\b10th\b|tenth|\bssc\b|matric", "tenth"),
    (r"\b12th\b|twelfth|\bhsc\b|intermediate", "twelfth"),
    (r"cgpa|\bgpa\b|grade point", "cgpa"),
    (r"graduation|passing\s*year|year of passing|batch", "grad_year"),
    (r"college|university|school|institution", "college"),
    (r"degree|qualification", "degree"),
    (r"branch|major|discipline|field of study|speciali[sz]ation", "branch"),
    (r"authori[sz]ed to work|work authori[sz]ation|right to work|visa|sponsor", "work_auth"),
    (r"current\s*(city|location)|\bcity\b|\blocation\b|based in", "city"),
    (r"^(full\s*)?name$|your name|full name", "full"),
]
_RULES = [(re.compile(p, re.I), k) for p, k in RULES]

COMMON_QUESTIONS = [
    "Tell us about yourself.",
    "Why do you want to work at this company?",
    "Why should we hire you for this role?",
]


def _s(v) -> str:
    return "" if v is None else str(v).strip()


def profile_values(user, profile) -> dict[str, str]:
    first, _, last = _s(user.name).partition(" ")
    g = lambda name: _s(getattr(profile, name, ""))
    return dict(first=first, last=last.strip() or first, full=_s(user.name), email=_s(user.email), phone=g("phone"),
                linkedin=g("linkedin_url"), github=g("github_url"), portfolio=g("portfolio_url"), city=g("city"),
                college=g("college"), degree=g("degree"), branch=g("branch"), cgpa=g("cgpa"), grad_year=g("grad_year"),
                tenth=g("tenth_pct"), twelfth=g("twelfth_pct"), work_auth=g("work_auth"))


def normalize(label: str) -> str:
    label = re.sub(r"\(.*?required.*?\)|\*", " ", label or "", flags=re.I)
    return re.sub(r"\s+", " ", label).strip().lower()


def is_open_question(label: str, tag: str) -> bool:
    return tag == "textarea" or len(label) >= 40


def answer_for(label: str, tag: str, vals: dict[str, str]) -> tuple[str, str]:
    """Returns (kind, value). kind: profile | draft | sensitive | blank"""
    norm = normalize(label)
    if not norm:
        return "blank", ""
    if SENSITIVE.search(norm):
        return "sensitive", ""
    for rx, key in _RULES:
        if rx.search(norm):
            return ("profile", vals[key]) if vals.get(key) else ("blank", "")
    if is_open_question(norm, tag):
        return "draft", ""
    return "blank", ""


def _openai_llm(system: str, user: str) -> str:
    from openai import OpenAI

    resp = OpenAI(api_key=config.OPENAI_API_KEY).chat.completions.create(
        model=config.OPENAI_MODEL, temperature=0.3,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    return (resp.choices[0].message.content or "").strip()


def draft_answer(profile, job, question: str, llm=None) -> str:
    """Short first-person draft from the resume. Returns '' when no LLM is available."""
    llm = llm or (_openai_llm if config.OPENAI_API_KEY else None)
    if llm is None:
        return ""
    system = ("You write short, honest job-application answers (max 120 words, first person) for a student. "
              "Use ONLY facts from the resume. Never invent employers, numbers, awards or skills. "
              "If the resume lacks what is needed, answer in general terms.")
    user = (f"RESUME:\n{(profile.resume_text or '')[:5000]}\n\nJOB: {job.title} at {job.company}\n"
            f"{(job.description or '')[:3000]}\n\nQUESTION: {question}")
    try:
        return llm(system, user).strip()
    except Exception:
        return ""


def draft_common_answers(profile, job, llm=None) -> dict[str, str]:
    return {q: draft_answer(profile, job, q, llm) for q in COMMON_QUESTIONS}


def plan_fill(fields: list[dict], user, profile, job, llm=None) -> dict:
    """fields: [{i, tag, type, label}] scanned from the page.
    Returns {'fills': [(i, value, kind, label)], 'drafted': [labels], 'left_blank': [labels]}"""
    vals = profile_values(user, profile)
    fills, drafted, blank = [], [], []
    for f in fields:
        kind, value = answer_for(f["label"], f["tag"], vals)
        if kind == "draft":
            value = draft_answer(profile, job, f["label"], llm)
            if value:
                drafted.append(f["label"])
            else:
                kind = "blank"
        if value:
            fills.append((f["i"], value, kind, f["label"]))
        elif f["label"]:
            blank.append(f["label"])
    return {"fills": fills, "drafted": drafted, "left_blank": blank}
