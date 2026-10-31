from types import SimpleNamespace as NS

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import auth, demo, pipeline, referral, scrapers, tracker
from app.matcher import extract_skills, keyword_score, rule_check, score_job
from app.models import Base, Job, Match


@pytest.fixture()
def s():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        yield session


def prof(**kw):
    d = dict(grad_year=2027, cgpa=8.0, skills=[], target_roles=[], resume_text="Python SQL Django Git", branch="CSE")
    d.update(kw)
    return NS(**d)


def job(title="SDE Intern", desc=""):
    return NS(title=title, description=desc, company="X")


def test_auth(s):
    u = auth.register(s, "A@B.com", "A", "secret1")
    assert auth.login(s, "a@b.com", "secret1").id == u.id
    assert auth.login(s, "a@b.com", "wrong") is None
    with pytest.raises(ValueError):
        auth.register(s, "a@b.com", "A", "secret1")
    with pytest.raises(ValueError):
        auth.register(s, "bad", "A", "secret1")
    auth.delete_user(s, u.id)
    assert auth.login(s, "a@b.com", "secret1") is None


def test_rules():
    assert rule_check(prof(), job(desc="2027 batch"))[0]
    assert not rule_check(prof(), job(desc="Open to 2025 batch only"))[0]
    assert rule_check(prof(), job(desc="Batch 2026/2027"))[0]
    assert not rule_check(prof(), job(desc="Minimum 9.0 CGPA"))[0]
    assert rule_check(prof(), job(desc="7 CGPA or above"))[0]
    assert not rule_check(prof(), job(title="Senior Engineer"))[0]
    assert not rule_check(prof(target_roles=["data"]), job(title="Web Intern"))[0]


def test_skills_boundaries():
    assert "java" not in extract_skills("We use JavaScript")
    assert {"c++", "python"} <= extract_skills("C++ and Python.")


def test_keyword_score():
    r = keyword_score(prof(), job(desc="Python, SQL and Kubernetes"))
    assert r.score == 67 and r.missing_skills == ["kubernetes"]
    assert score_job(prof(), job(desc="2025 batch")).eligible is False


def test_scrape_dedupe_and_error_isolation(s):
    jobs = [dict(source="t", company="C", external_id="1", title="T", location="", url="u", description="d")]

    def fetcher(c, known=None):
        if c["name"] == "bad":
            raise RuntimeError("boom")
        return jobs

    cs = [{"name": "bad"}, {"name": "good"}]
    assert pipeline.scrape_all(s, cs, fetcher)[0] == 1
    new, errors = pipeline.scrape_all(s, cs, fetcher)
    assert new == 0 and len(errors) == 1


def test_parsers(monkeypatch):
    class R:
        def __init__(self, data): self.data = data
        def raise_for_status(self): pass
        def json(self): return self.data

    gh = {"jobs": [{"id": 1, "title": "Software Engineer", "location": {"name": "Pune"}, "absolute_url": "u", "content": "&lt;p&gt;Hi &amp;amp; bye&lt;/p&gt;"}]}
    monkeypatch.setattr(scrapers.requests, "get", lambda *a, **k: R(gh))
    out = scrapers.fetch_jobs({"name": "G", "source": "greenhouse", "slug": "g"})
    assert out[0]["description"] == "Hi & bye" and out[0]["location"] == "Pune"
    lv = [{"id": "x", "text": "Backend Developer", "categories": {"location": "Remote"}, "hostedUrl": "u", "descriptionPlain": "Plain"}]
    monkeypatch.setattr(scrapers.requests, "get", lambda *a, **k: R(lv))
    assert scrapers.fetch_jobs({"name": "L", "source": "lever", "slug": "l"})[0]["title"] == "Backend Developer"


def test_full_pipeline_multi_user(s):
    u1 = demo.seed(s)
    u2 = auth.register(s, "two@x.com", "Two", "secret1")
    u2.profile.resume_text, u2.profile.grad_year = "Java Spring MySQL", 2026
    s.commit()
    alerts = []
    res = pipeline.run_all(s, companies=[])
    assert not res["errors"] and res["scored"] >= 5 + 5
    m = {x.job.title: x for x in s.scalars(select(Match).where(Match.user_id == u1.id))}
    assert m["Software Engineer Intern"].eligible and m["Software Engineer Intern"].score > 50
    assert not m["Senior Staff Engineer"].eligible
    assert not m["Data Analyst Intern"].eligible
    assert not m["Backend Engineer Trainee"].eligible  # CGPA 9.0 needed
    # idempotent: second run scores nothing new
    assert pipeline.run_all(s, companies=[])["scored"] == 0
    # alert sent to telegram only for strong eligible matches
    s.execute(Match.__table__.delete())
    s.expunge_all()
    u1 = s.get(type(u1), u1.id)
    u1.telegram_chat_id = "123"
    s.commit()
    pipeline.match_user(s, u1, notify=lambda chat, text: alerts.append((chat, text)))
    assert alerts and all(a[0] == "123" for a in alerts)


def test_one_user_failure_does_not_stop_others(s):
    demo.seed(s)
    auth.register(s, "t@x.com", "T", "secret1").profile.resume_text = "python"
    s.commit()
    orig = pipeline.match_user
    calls = []

    def flaky(session, user, **kw):
        calls.append(user.id)
        if len(calls) == 1:
            raise RuntimeError("expired token")
        return orig(session, user, **kw)

    pipeline.match_user = flaky
    try:
        res = pipeline.run_all(s, companies=[])
    finally:
        pipeline.match_user = orig
    assert len(calls) == 2 and len(res["errors"]) == 1


def test_tracker_and_followup(s):
    u = demo.seed(s)
    j = s.scalar(select(Job))
    a = tracker.add_application(s, u.id, j.id)
    assert a.status == "Saved"
    tracker.set_status(s, a, "Applied")
    assert a.follow_up_at and not tracker.due_followups(s)
    from datetime import timedelta
    from app.models import now
    a.follow_up_at = now() - timedelta(days=1)
    s.commit()
    sent = []
    assert pipeline.send_reminders(s, notify=lambda c, t: sent.append(t)) == 1 and sent
    assert tracker.stats(s, u.id)["Applied"] == 1
    with pytest.raises(ValueError):
        tracker.set_status(s, a, "Bogus")


def test_email_classifier_and_referral():
    assert tracker.classify_email("Your online assessment link") == "OA"
    assert tracker.classify_email("Unfortunately we are not moving forward") == "Rejected"
    assert tracker.classify_email("Hello") is None
    assert "Stripe" in referral.linkedin_search_url("Stripe", "IIT Delhi")
    assert "Job link: u" in referral.draft_message("A", "C", "T", "u", ["python"])
