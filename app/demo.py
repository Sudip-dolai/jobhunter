"""Demo data so you can test everything offline with no API keys."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth
from app.filters import extract_salary_lpa
from app.models import Job, User

DEMO_EMAIL, DEMO_PASSWORD = "demo@example.com", "demo1234"

RESUME = (
    "Aarav Sharma, B.Tech CSE, 2027 batch, CGPA 8.1. Skills: Python, Java, SQL, Django, React, "
    "Git, Docker, data structures, algorithms, DBMS. Projects: Django REST API, React dashboard."
)

JOBS = [
    ("Acme", "Software Engineer Intern", "Hiring 2027 batch students. Need Python, Django, SQL, Git and data structures. Minimum 7.0 CGPA."),
    ("Acme", "Senior Staff Engineer", "10+ years experience with Java, Kubernetes and system design."),
    ("Globex", "Data Analyst Intern", "Open to 2025 batch graduates. SQL, Excel, Tableau and Python required."),
    ("Globex", "Frontend Developer Intern", "2027 graduates welcome. React, JavaScript, HTML, CSS, TypeScript and Git."),
    ("Hooli", "Software Engineer Trainee", "2027 batch. Python and SQL. CTC 5 LPA."),
    ("Initech", "Backend Engineer Trainee", "Batch 2026/2027. Java, Spring, MySQL, Docker, AWS. CGPA 9.0 or above required."),
]


def seed(session: Session) -> User:
    user = session.scalar(select(User).where(User.email == DEMO_EMAIL))
    if not user:
        user = auth.register(session, DEMO_EMAIL, "Aarav Sharma", DEMO_PASSWORD)
    p = user.profile
    p.grad_year, p.cgpa, p.branch, p.college = 2027, 8.1, "CSE", "Demo Institute of Technology"
    p.skills, p.resume_text, p.min_score = ["python", "sql", "react"], RESUME, 50
    for i, (company, title, desc) in enumerate(JOBS, 1):
        if not session.scalar(select(Job.id).where(Job.source == "demo", Job.external_id == str(i))):
            lo, hi = extract_salary_lpa(desc) or (None, None)
            session.add(Job(source="demo", external_id=str(i), company=company, title=title, sector="SaaS & B2B Software",
                            salary_min=lo, salary_max=hi, url=f"https://example.com/jobs/{i}", description=desc))
    session.commit()
    return user
