"""Referral helper. Builds a search link and a message; YOU open the link and send the message."""
from urllib.parse import quote_plus


def linkedin_search_url(company: str, college: str = "") -> str:
    return "https://www.linkedin.com/search/results/people/?keywords=" + quote_plus(f"{company} {college}".strip())


def draft_message(user_name: str, company: str, title: str, job_url: str, skills: list[str], college: str = "") -> str:
    skill_txt = ", ".join(skills[:4]) if skills else "software development"
    where = f" from {college}" if college else ""
    return (
        f"Hi, I'm {user_name}{where}. I came across the {title} opening at {company} and it matches my "
        f"background in {skill_txt}. Would you be open to referring me or sharing any advice? "
        f"Job link: {job_url}\nHappy to send my resume. Thank you for your time!"
    )
