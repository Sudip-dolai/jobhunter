from types import SimpleNamespace as NS

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app import auth, discover, pipeline, scrapers
from app.filters import extract_salary_lpa, is_software_role
from app.matcher import rule_check
from app.models import Base, Job, Match


@pytest.fixture()
def s():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        yield session


class R:
    def __init__(self, data, status=200): self.data, self.status_code = data, status
    def raise_for_status(self): pass
    def json(self): return self.data


def test_salary_extraction():
    assert extract_salary_lpa("CTC ₹12-18 LPA") == (12, 18)
    assert extract_salary_lpa("10 to 20 lakhs per annum") == (10, 20)
    assert extract_salary_lpa("INR 1,200,000 - 2,000,000") == (12, 20)
    assert extract_salary_lpa("Rs. 12,00,000") == (12, 12)
    assert extract_salary_lpa("15 LPA") == (15, 15)
    assert extract_salary_lpa("We offer great perks") is None
    lo, hi = extract_salary_lpa("Base pay $120,000 - $150,000 per year")
    assert round(lo) == 102 and round(hi) == 128
    assert round(extract_salary_lpa("£60k")[0]) == 66
    assert round(extract_salary_lpa("USD 120-150k")[1]) == 128
    assert extract_salary_lpa("$50 per hour") is None and extract_salary_lpa("€4,000 per month") is None
    assert extract_salary_lpa("Get $20 off") is None
    assert extract_salary_lpa("Team of 5-10 people") is None


def test_software_role_filter():
    for t in ["Software Engineer II", "SDE-1", "Backend Developer", "Full Stack Engineer", "DevOps Engineer", "Data Scientist"]:
        assert is_software_role(t), t
    for t in ["Mechanical Engineer", "Sales Engineer", "Marketing Manager", "HR Executive"]:
        assert not is_software_role(t), t


def test_salary_rules():
    p = NS(grad_year=None, cgpa=None, target_roles=[], min_lpa=7, require_salary=False)
    job = lambda lo, hi: NS(title="SDE", description="", salary_min=lo, salary_max=hi)
    assert not rule_check(p, job(3, 5))[0]
    assert rule_check(p, job(6, 12))[0]
    assert rule_check(p, job(None, None))[0]          # unknown salary is kept
    assert rule_check(p, job(70, 90))[0]              # no maximum
    assert rule_check(p, job(7, 7))[0]
    p.require_salary = True
    assert not rule_check(p, job(None, None))[0]


def test_workday_scraper(monkeypatch):
    post = {"total": 2, "jobPostings": [
        {"title": "Software Engineer", "externalPath": "/job/Pune/Software-Engineer_JR1", "locationsText": "Pune"},
        {"title": "Mechanical Engineer", "externalPath": "/job/Pune/Mech_JR2", "locationsText": "Pune"}]}
    detail = {"jobPostingInfo": {"jobDescription": "<p>Python. 12-18 LPA</p>", "location": "Pune, India"}}
    monkeypatch.setattr(scrapers.requests, "post", lambda *a, **k: R(post))
    monkeypatch.setattr(scrapers.requests, "get", lambda *a, **k: R(detail))
    c = {"name": "NV", "source": "workday", "slug": "nv|wd5|Site", "sector": "Semiconductor & Hardware"}
    jobs = scrapers.fetch_jobs(c)
    assert len(jobs) == 1 and jobs[0]["external_id"] == "Software-Engineer_JR1"
    assert jobs[0]["salary_min"] == 12 and jobs[0]["sector"] == "Semiconductor & Hardware"
    assert jobs[0]["url"].startswith("https://nv.wd5.myworkdayjobs.com/en-US/Site/job/")
    assert scrapers.fetch_jobs(c, known={"Software-Engineer_JR1"}) == []  # already known: no detail fetch


def test_ashby_scraper(monkeypatch):
    data = {"jobs": [{"id": "a1", "title": "Backend Engineer", "location": "Remote", "jobUrl": "u",
                      "descriptionPlain": "Go", "compensation": {"compensationTierSummary": "₹20-30 LPA"}},
                     {"id": "a2", "title": "Designer", "descriptionPlain": ""}]}
    monkeypatch.setattr(scrapers.requests, "get", lambda *a, **k: R(data))
    jobs = scrapers.fetch_jobs({"name": "A", "source": "ashby", "slug": "a"})
    assert len(jobs) == 1 and jobs[0]["salary_max"] == 30


def test_discover(monkeypatch):
    assert discover.detect_from_url("https://boards.greenhouse.io/stripe") == ("greenhouse", "stripe")
    assert discover.detect_from_url("https://jobs.lever.co/spotify/abc") == ("lever", "spotify")
    assert discover.detect_from_url("https://jobs.ashbyhq.com/ramp") == ("ashby", "ramp")
    assert discover.detect_from_url("https://micron.wd1.myworkdayjobs.com/External") == ("workday", "micron|wd1|External")
    assert discover.detect_from_url("https://careers.google.com/") is None
    assert discover.slug_candidates("Persistent Systems") == ["persistent"]
    monkeypatch.setattr(discover, "verify", lambda src, slug: (src, slug) == ("lever", "razorpay"))
    r = discover.discover("Razorpay", "https://razorpay.com/jobs/", "Fintech")
    assert r == {"name": "Razorpay", "source": "lever", "slug": "razorpay", "sector": "Fintech", "guess": True}
    assert discover.discover("Nobody", "https://nobody.com/careers")["source"] == "page"   # unknown site: read as a plain careers page
    assert discover.discover("Nobody") is None                                              # no link at all: nothing to read
    rows = discover.parse_input("# Edtech\nUnacademy | https://u.com\n\nCoursera\n")
    assert discover.parse_input("# A\nX\nx | u\n") == [("X", "", "A")]
    assert rows == [("Unacademy", "https://u.com", "Edtech"), ("Coursera", "", "Edtech")]
    assert [c["name"] for c in discover.merge([{"name": "B"}], [{"name": "a"}, {"name": "B", "x": 1}])] == ["a", "B"]


def test_sector_filter_and_cap_counts_only_eligible(s):
    u = auth.register(s, "a@b.com", "A", "secret1")
    u.profile.resume_text, u.profile.sectors = "python sql", ["Edtech"]
    s.add_all([Job(source="t", external_id=str(i), company="C", title="SDE", sector=sec, description="Python SQL")
               for i, sec in enumerate(["Edtech", "Telecom", "Edtech", "Edtech"])])
    s.add(Job(source="t", external_id="x", company="C", title="Senior SDE", sector="Edtech"))
    s.commit()
    n = pipeline.match_user(s, u, max_jobs=2)
    rows = s.scalars(select(Match)).all()
    assert n == 3 and {m.job.sector for m in rows} == {"Edtech"}  # 2 scored + 1 free rejection; Telecom skipped
    assert sum(1 for m in rows if not m.eligible) == 1


def test_auto_migration(tmp_path, monkeypatch):
    from app import db
    eng = create_engine(f"sqlite:///{tmp_path}/old.db")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE jobs (id INTEGER PRIMARY KEY, source VARCHAR(30), external_id VARCHAR(100), company VARCHAR(120), "
                       "title VARCHAR(300), location VARCHAR(200), url VARCHAR(600), description TEXT, first_seen DATETIME)"))
    monkeypatch.setattr(db, "engine", eng)
    db.init_db()
    cols = {c["name"] for c in __import__("sqlalchemy").inspect(eng).get_columns("jobs")}
    assert {"sector", "salary_min", "salary_max"} <= cols
