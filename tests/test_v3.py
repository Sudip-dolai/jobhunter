from email.message import EmailMessage
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import autofill, crypto, demo, discover, gmail, pipeline, scrapers, tracker
from app.filters import detect_countries, detect_job_type, detect_remote, norm_type
from app.matcher import rule_check
from app.models import Base, EmailEvent, Job


@pytest.fixture()
def s():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        yield session


class R:
    def __init__(self, data): self.data = data
    def raise_for_status(self): pass
    def json(self): return self.data


# ---------- detectors and filters ----------
def test_countries_remote_type():
    assert detect_countries("Bengaluru, India") == ["India"]
    assert detect_countries("London, UK") == ["United Kingdom"]
    assert detect_countries("San Francisco, CA") == ["United States"]
    assert detect_countries("Bengaluru | London") == ["India", "United Kingdom"]
    assert detect_countries("Remote") == [] and detect_countries("") == []
    assert detect_remote("Remote - India") and detect_remote("Pune", "SDE", "This is a fully remote position")
    assert not detect_remote("Pune", "SDE", "Join our office")
    assert detect_job_type("Software Engineer Intern") == "Internship"
    assert detect_job_type("SDE - Contract") == "Contract"
    assert detect_job_type("SDE", "a 6 month fixed-term role") == "Contract"
    assert detect_job_type("SDE", "This is a full-time role") == "Permanent"
    assert detect_job_type("SDE", "") == "Not stated"
    assert [norm_type(x) for x in ["FullTime", "PartTime", "Temporary", "Intern", "x"]] == ["Permanent", "Part-time", "Contract", "Internship", ""]


def test_country_remote_type_rules():
    p = NS(grad_year=None, cgpa=None, target_roles=[], min_lpa=7, require_salary=False,
           countries=["India"], remote_pref="Any", job_types=["Permanent"])
    job = lambda **k: NS(**{**dict(title="SDE", description="", country="", remote=False, job_type=""), **k})
    assert rule_check(p, job(country="India"))[0]
    assert rule_check(p, job(country=""))[0]                                  # unknown country kept
    assert not rule_check(p, job(country="United States"))[0]
    assert rule_check(p, job(country="United States", remote=True))[0]        # remote kept
    assert rule_check(p, job(country="India, United Kingdom"))[0]
    assert not rule_check(p, job(job_type="Contract"))[0]
    assert rule_check(p, job(job_type="Not stated"))[0]
    p.remote_pref = "Remote only"
    assert not rule_check(p, job())[0] and rule_check(p, job(remote=True))[0]
    p.remote_pref = "On-site/hybrid only"
    assert not rule_check(p, job(remote=True))[0]


# ---------- new job-board scrapers ----------
def test_workable_smartrecruiters_recruitee(monkeypatch):
    wk = {"jobs": [{"shortcode": "AB1", "title": "Software Engineer", "city": "Pune", "country": "India", "telecommuting": True,
                    "employment_type": "Full-time", "url": "u", "description": "<p>Python</p>"}]}
    monkeypatch.setattr(scrapers.requests, "get", lambda *a, **k: R(wk))
    j = scrapers.fetch_jobs({"name": "W", "source": "workable", "slug": "w"})[0]
    assert j["remote"] and j["job_type"] == "Permanent" and j["country"] == "India"

    lst = {"totalFound": 1, "content": [{"id": "9", "name": "Backend Developer", "location": {"city": "Berlin", "country": "de"},
                                         "typeOfEmployment": {"label": "Contract"}}]}
    det = {"jobAd": {"sections": {"jobDescription": {"text": "<p>Go 20-30 LPA</p>"}}}}
    monkeypatch.setattr(scrapers.requests, "get", lambda url, **k: R(lst if url.endswith("/postings") else det))
    j = scrapers.fetch_jobs({"name": "S", "source": "smartrecruiters", "slug": "s"})[0]
    assert j["country"] == "Germany" and j["job_type"] == "Contract" and j["salary_max"] == 30

    rc = {"offers": [{"id": 5, "title": "Full Stack Engineer", "careers_url": "u", "location": "Remote, Poland", "remote": True,
                      "description": "<p>React</p>", "employment_type_code": "fulltime"}]}
    monkeypatch.setattr(scrapers.requests, "get", lambda *a, **k: R(rc))
    j = scrapers.fetch_jobs({"name": "R", "source": "recruitee", "slug": "r"})[0]
    assert j["remote"] and j["country"] == "Poland"


POSTING = """<html><script type="application/ld+json">{"@context":"https://schema.org","@type":"JobPosting",
"title":"Software Engineer II","description":"<p>Build things in Python.</p>","employmentType":"FULL_TIME",
"jobLocationType":"TELECOMMUTE","jobLocation":{"@type":"Place","address":{"addressLocality":"Pune","addressCountry":"IN"}},
"baseSalary":{"@type":"MonetaryAmount","currency":"INR","value":{"@type":"QuantitativeValue","minValue":1200000,"maxValue":1800000,"unitText":"YEAR"}}}
</script></html>"""


def test_jsonld_custom_site(monkeypatch):
    pages = {
        "https://x.com/careers": '<a href="/jobs/101-software-engineer">a</a><a href="/jobs/102-sales-manager">b</a><a href="/about">c</a>',
        "https://x.com/jobs/101-software-engineer": POSTING,
    }
    monkeypatch.setattr(scrapers, "_get_text", lambda url, render=False: pages[url])
    jobs = scrapers.fetch_jobs({"name": "X", "source": "jsonld", "slug": "https://x.com/careers", "sector": "IT Services"})
    assert len(jobs) == 1                                  # sales-manager page is skipped by its URL
    j = jobs[0]
    assert j["title"] == "Software Engineer II" and j["remote"] and j["job_type"] == "Permanent"
    assert j["country"] == "India" and (j["salary_min"], j["salary_max"]) == (12, 18)
    assert scrapers.fetch_jobs({"name": "X", "source": "jsonld", "slug": "https://x.com/careers"}, known={j["external_id"]}) == []


def test_jsonld_sitemap_and_discover(monkeypatch):
    sitemap = "<urlset><url><loc>https://y.com/jobs/7-developer</loc></url><url><loc>https://y.com/blog/post</loc></url></urlset>"
    assert scrapers._find_job_urls("https://y.com/sitemap.xml", sitemap) == ["https://y.com/jobs/7-developer"]
    pages = {"https://y.com/careers": "<html>no links</html>", "https://y.com/sitemap.xml": sitemap,
             "https://y.com/jobs/7-developer": POSTING}
    monkeypatch.setattr(scrapers, "_get_text", lambda url, render=False: pages[url])
    monkeypatch.setattr(discover, "verify", lambda *a: False)
    r = discover.discover("Y", "https://y.com/careers", "Telecom")
    assert r == {"name": "Y", "source": "jsonld", "slug": "https://y.com/sitemap.xml", "sector": "Telecom"}
    assert discover.detect_from_url("https://apply.workable.com/acme/") == ("workable", "acme")
    assert discover.detect_from_url("https://careers.smartrecruiters.com/Bosch") == ("smartrecruiters", "Bosch")
    assert discover.detect_from_url("https://acme.recruitee.com/") == ("recruitee", "acme")


# ---------- autofill ----------
def test_autofill_plan():
    user = NS(name="Aarav Sharma", email="a@x.com")
    prof = NS(phone="999", linkedin_url="https://linkedin.com/in/a", github_url="", portfolio_url="", city="Pune",
              college="Demo Institute", degree="B.Tech", branch="CSE", cgpa=8.1, grad_year=2027, tenth_pct=90.0,
              twelfth_pct=None, work_auth="Authorized to work in India", resume_text="Python projects")
    job = NS(title="SDE", company="Acme", description="")
    labels = ["First name *", "Last name", "Email", "Phone", "LinkedIn Profile", "GitHub", "College/University",
              "Graduation year", "CGPA", "10th percentage", "12th percentage", "Gender", "Expected salary",
              "Are you willing to relocate?", "Are you authorized to work in India?", "Current location",
              "How did you hear about us?", "Why do you want to work at Acme?"]
    fields = [{"i": i, "tag": "textarea" if "Why" in l else "input", "type": "text", "label": l} for i, l in enumerate(labels)]
    plan = autofill.plan_fill(fields, user, prof, job, llm=lambda sys, usr: "My draft answer.")
    got = {lab: val for _, val, _, lab in plan["fills"]}
    assert got["First name *"] == "Aarav" and got["Last name"] == "Sharma" and got["Phone"] == "999"
    assert got["College/University"] == "Demo Institute" and got["Graduation year"] == "2027" and got["CGPA"] == "8.1"
    assert got["10th percentage"] == "90.0" and got["Current location"] == "Pune"
    assert got["Are you authorized to work in India?"] == "Authorized to work in India"
    assert got["Why do you want to work at Acme?"] == "My draft answer." and plan["drafted"] == ["Why do you want to work at Acme?"]
    for sensitive in ["Gender", "Expected salary", "Are you willing to relocate?", "How did you hear about us?"]:
        assert sensitive not in got and sensitive in plan["left_blank"]
    assert "GitHub" in plan["left_blank"] and "12th percentage" in plan["left_blank"]     # no value saved: left empty
    no_llm = autofill.plan_fill(fields[-1:], user, prof, job, llm=None)                   # no API key: nothing invented
    assert no_llm["fills"] == [] and no_llm["drafted"] == []
    assert list(autofill.draft_common_answers(prof, job, llm=lambda a, b: "x").values()) == ["x"] * 3


# ---------- Gmail ----------
def test_crypto_roundtrip():
    assert crypto.decrypt(crypto.encrypt("abcd efgh")) == "abcd efgh"
    with pytest.raises(ValueError):
        crypto.decrypt("not-a-token")


class FakeIMAP:
    def __init__(self, raws): self.raws, self.readonly = raws, False
    def login(self, u, p): self.cred = (u, p)
    def select(self, box, readonly=False): self.readonly = readonly; return "OK", [b"1"]
    def search(self, _c, *a): return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.raws)))]
    def fetch(self, num, spec): return "OK", [(b"1 (BODY[] {9}", self.raws[int(num) - 1]), b")"]
    def logout(self): pass


def test_imap_fetch_is_readonly():
    m = EmailMessage()
    m["Subject"], m["From"], m["Message-ID"] = "Interview invite", "hr@acme.com", "<1@acme>"
    m.set_content("Please schedule a call.")
    fake = FakeIMAP([m.as_bytes()])
    out = gmail.fetch_recent("me@gmail.com", "pw", client_factory=lambda: fake)
    assert fake.readonly and fake.cred == ("me@gmail.com", "pw")
    assert out[0]["subject"] == "Interview invite" and "schedule a call" in out[0]["body"]


def test_gmail_sync_updates_tracker(s):
    user = demo.seed(s)
    user.profile.gmail_address, user.profile.gmail_app_password_enc = "me@gmail.com", crypto.encrypt("pw")
    jobs = {j.company: j for j in s.scalars(select(Job))}
    acme = tracker.add_application(s, user.id, jobs["Acme"].id, "Applied")
    globex = tracker.add_application(s, user.id, jobs["Globex"].id, "Applied")
    mail = lambda mid, sender, subj, body="": dict(message_id=mid, sender=sender, subject=subj, body=body)
    inbox = [mail("<1>", "hr@acme.com", "Interview invitation for Software Engineer Intern"),
             mail("<2>", "jobs@globex.com", "Update on your application", "Unfortunately we are not moving forward."),
             mail("<3>", "news@random.com", "Interview tips weekly"),
             mail("<4>", "hr@acme.com", "Your online assessment link")]       # lower than Interview: ignored
    alerts = []
    assert gmail.sync_user(s, user, fetch=lambda a, p: inbox, notify=lambda c, t: alerts.append(t)) == 2
    assert acme.status == "Interview" and globex.status == "Rejected" and len(alerts) == 2
    assert "[auto]" in acme.notes
    assert gmail.sync_user(s, user, fetch=lambda a, p: inbox, notify=lambda c, t: None) == 0       # idempotent
    assert s.scalar(select(EmailEvent.id).where(EmailEvent.message_id == "<3>")) is None
    user.profile.gmail_address = ""
    assert gmail.sync_user(s, user, fetch=lambda a, p: 1 / 0) == 0                                  # not configured: skipped


def test_run_all_reports_email_updates(s):
    demo.seed(s)
    res = pipeline.run_all(s, companies=[])
    assert res["emails_updated"] == 0 and not res["errors"]


# ---------- experience filter ----------
def test_experience_rule():
    from app.matcher import required_years
    assert required_years("3+ years of experience in Python") == 3
    assert required_years("1-3 years of professional experience") == 1
    assert required_years("Minimum 2 years in backend") == 2
    assert required_years("5 years experience in Java and 2 years experience in SQL") == 2
    assert required_years("We were founded 20 years ago") is None
    p = NS(grad_year=None, cgpa=None, target_roles=[], min_lpa=7, experience_years=0)
    job = lambda d: NS(title="Software Engineer", description=d)
    assert not rule_check(p, job("3+ years of experience required"))[0]
    assert rule_check(p, job("0-2 years of experience"))[0]
    assert rule_check(p, job("1 year of experience preferred"))[0]
    assert rule_check(p, job("freshers welcome"))[0]
    p.experience_years = 3
    assert rule_check(p, job("3+ years of experience required"))[0]


# ---------- Adzuna search feed ----------
def test_adzuna_feed(monkeypatch):
    from app import config
    page1 = {"results": [
        {"id": "11", "title": "Software Engineer", "company": {"display_name": "Tata Consultancy Services"},
         "location": {"display_name": "Kolkata, West Bengal"}, "redirect_url": "https://adzuna/x", "description": "Python and SQL",
         "salary_min": 800000, "salary_max": 1200000, "salary_is_predicted": "0", "contract_type": "permanent"},
        {"id": "12", "title": "Java Developer", "company": {"display_name": "Infosys"}, "location": {"display_name": "Pune"},
         "redirect_url": "u", "description": "Java", "salary_min": 300000, "salary_is_predicted": "1", "contract_time": "full_time"},
        {"id": "13", "title": "Sales Executive", "company": {"display_name": "HCL"}, "location": {"display_name": "Noida"}}]}
    monkeypatch.setattr(scrapers.requests, "get", lambda *a, **k: R(page1))
    jobs = scrapers.fetch_jobs({"name": "Adzuna IN", "source": "adzuna", "slug": "in|software engineer"})
    assert [j["company"] for j in jobs] == ["Tata Consultancy Services", "Infosys"]      # non-software job dropped
    tcs, infy = jobs
    assert (tcs["salary_min"], tcs["salary_max"]) == (8, 12) and tcs["country"] == "India" and tcs["job_type"] == "Permanent"
    assert infy["salary_min"] is None                                                       # predicted salaries are ignored
    monkeypatch.setattr(config, "ADZUNA_APP_ID", "")
    assert scrapers.load_feeds() == []
    monkeypatch.setattr(config, "ADZUNA_APP_ID", "id")
    monkeypatch.setattr(config, "ADZUNA_APP_KEY", "key")
    assert len(scrapers.load_feeds()) == 3 and scrapers.load_feeds()[0]["source"] == "adzuna"


# ---------- Jooble feed, targeted employers, throttling ----------
def test_jooble_feed_and_throttle(monkeypatch, tmp_path):
    from app import config
    data = {"jobs": [
        {"id": 1, "title": "Software <b>Engineer</b>", "company": "Infosys Limited", "location": "Pune", "link": "https://jooble.org/jdp/1",
         "snippet": "Java and <b>SQL</b>", "salary": "₹ 6 - 9 LPA", "type": "Full-time"},
        {"id": 2, "title": "Software Engineer", "company": "Random Startup", "location": "Bengaluru", "link": "https://jooble.org/jdp/2", "snippet": "Python"}]}
    monkeypatch.setattr(scrapers.requests, "post", lambda *a, **k: R(data))
    generic = scrapers.fetch_jobs({"name": "Jooble India: software engineer", "source": "jooble", "slug": "India|software engineer"})
    assert [j["company"] for j in generic] == ["Infosys Limited", "Random Startup"]
    assert generic[0]["title"] == "Software Engineer" and generic[0]["country"] == "India" and generic[0]["job_type"] == "Permanent"
    assert generic[0]["salary_max"] == 9                                                    # salary text is read from the feed
    targeted = scrapers.fetch_jobs({"name": "Jooble: Infosys", "source": "jooble", "slug": "India|Infosys software engineer", "match": ["infosys"]})
    assert [j["company"] for j in targeted] == ["Infosys Limited"]                          # only the requested employer is kept
    monkeypatch.setattr(config, "JOOBLE_API_KEY", "")
    monkeypatch.setattr(config, "ADZUNA_APP_ID", "")
    assert scrapers.load_feeds() == []
    monkeypatch.setattr(config, "JOOBLE_API_KEY", "key")
    feeds = scrapers.load_feeds()
    assert len(feeds) == 3 + len(scrapers.BIG_EMPLOYERS) and any(f.get("match") for f in feeds)
    monkeypatch.setattr(scrapers, "_STATE", tmp_path / "state.json")
    assert len(scrapers.feeds_due(feeds)) == len(feeds)
    scrapers.mark_feeds([feeds[0]["name"]])
    assert len(scrapers.feeds_due(feeds)) == len(feeds) - 1                                 # just ran: skipped until the interval passes


def test_scrape_all_runs_due_feeds_once(s, monkeypatch, tmp_path):
    from app import config
    monkeypatch.setattr(config, "JOOBLE_API_KEY", "key")
    monkeypatch.setattr(scrapers, "_STATE", tmp_path / "state.json")
    monkeypatch.setattr(pipeline, "load_companies", lambda: [])
    seen = []

    def fetcher(c, known=None):
        seen.append(c["name"])
        return []

    pipeline.scrape_all(s, None, fetcher)
    first = len(seen)
    assert first == 3 + len(scrapers.BIG_EMPLOYERS)
    pipeline.scrape_all(s, None, fetcher)
    assert len(seen) == first                                                               # second run within the interval: feeds skipped


# ---------- any careers page ----------
LISTING = """<html><body>
<a href="/jobs/101">Software Engineer - Pune</a> <a href="/jobs/101">Software Engineer - Pune</a>
<a href="https://careers.x.com/jobs/102"><div>Backend Developer</div></a>
<a href="/jobs/103">Sales Manager</a> <a href="mailto:hr@x.com">Software Engineer mail</a> <a href="/jobs/104">Apply</a>
</body></html>"""


def test_link_jobs_finds_software_links_only():
    links = scrapers._link_jobs("https://careers.x.com/search", LISTING)
    assert links == [("Software Engineer - Pune", "https://careers.x.com/jobs/101"), ("Backend Developer", "https://careers.x.com/jobs/102")]


def test_page_reader(monkeypatch):
    monkeypatch.setattr(scrapers, "PAGE_DELAY", 0)
    pages = {"https://careers.x.com/search": LISTING, "https://careers.x.com/jobs/101": POSTING,
             "https://careers.x.com/jobs/102": "<html><script>var a=1</script><body>Backend role. 14-20 LPA. Python.</body></html>"}
    monkeypatch.setattr(scrapers, "_get_text", lambda url, render=False: pages[url])
    jobs = scrapers.fetch_jobs({"name": "X", "source": "page", "slug": "https://careers.x.com/search", "sector": "IT Services"})
    a, b = jobs
    assert a["title"] == "Software Engineer II" and a["remote"] and a["country"] == "India"       # job page had structured data
    assert b["title"] == "Backend Developer" and (b["salary_min"], b["salary_max"]) == (14, 20)    # plain text page: salary read from text
    assert "var a=1" not in b["description"]
    again = scrapers.fetch_jobs({"name": "X", "source": "page", "slug": "https://careers.x.com/search"}, known={a["external_id"]})
    assert [j["title"] for j in again] == ["Backend Developer"]


def test_robots_txt_is_respected(monkeypatch):
    class Resp:
        status_code, text = 200, "User-agent: *\nDisallow: /private/"
    monkeypatch.setattr(scrapers, "_ROBOTS", {})
    monkeypatch.setattr(scrapers.requests, "get", lambda *a, **k: Resp())
    assert not scrapers._robots_ok("https://x.com/private/jobs/1") and scrapers._robots_ok("https://x.com/jobs/1")
    monkeypatch.setattr(scrapers, "_ROBOTS", {})
    monkeypatch.setattr(scrapers.requests, "get", lambda *a, **k: (_ for _ in ()).throw(scrapers.requests.RequestException()))
    assert scrapers._robots_ok("https://y.com/anything")                                          # robots.txt unreadable: allowed
    with pytest.raises(PermissionError):
        monkeypatch.setattr(scrapers, "_robots_ok", lambda url: False)
        scrapers._get_text("https://z.com/jobs")


def test_any_link_becomes_a_page_source(monkeypatch, tmp_path):
    from app import config
    monkeypatch.setattr(discover, "verify", lambda *a: False)
    monkeypatch.setattr(discover, "probe_jsonld", lambda url: None)
    assert discover.discover("TCS", "https://careers.tcs.com/search", "IT Services") == {
        "name": "TCS", "source": "page", "slug": "https://careers.tcs.com/search", "sector": "IT Services"}
    assert discover.discover("NoLink") is None
    assert discover.parse_input("https://careers.tcs.com/x\nInfosys | https://www.infosys.com/careers") == [
        ("Tcs", "https://careers.tcs.com/x", ""), ("Infosys", "https://www.infosys.com/careers", "")]
    monkeypatch.setattr(config, "ROOT", tmp_path)
    added = discover.add_links("HCLTech | https://www.hcltech.com/careers\nnot a link")
    assert [c["name"] for c in added] == ["HCLTech"]
    import json
    assert json.loads((tmp_path / "companies.json").read_text())[0]["source"] == "page"
