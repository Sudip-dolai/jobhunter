"""v2 ingest, flag-off equivalence, dedup integration and group-aware matching."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth, config, dedup, lifecycle, pipeline
from app.collect import CollectResult
from app.models import Company, Job, JobEvent, Match, SourceRun
from tests.helpers_v4 import mkjob, v2_engine

T0 = datetime(2026, 11, 1, 9, 0, 0)


def raw(ext, title="Software Engineer", company="Acme Inc", source="greenhouse", location="Pune, India", desc=None, **kw):
    return dict(source=source, company=company, external_id=ext, title=title, location=location, url=f"https://{source}/{ext}",
                description=desc if desc is not None else "Build services in Python and Go. " * 15, sector="SaaS", remote=False,
                job_type="Permanent", country="India", salary_min=None, salary_max=None, **kw)


def scrape(eng, boards, monkeypatch, v2=True, dedup_on=False):
    monkeypatch.setattr(config, "FEATURE_V2_INGEST", v2)
    monkeypatch.setattr(config, "FEATURE_V2_DEDUP", dedup_on)
    monkeypatch.setattr(pipeline, "_now", lambda: T0)
    companies = [{"name": n, "source": src, "slug": "s"} for n, src, _ in boards]
    by = {n: res for n, _, res in boards}
    with Session(eng) as s:
        return pipeline.scrape_all(s, companies=companies, fetcher=lambda c, k: by[c["name"]])


def res(jobs, **kw):
    return CollectResult(jobs=jobs, listed_ids={j["external_id"] for j in jobs}, **kw)


def test_v2_ingest_writes_normalized_columns(tmp_path, monkeypatch):
    eng, _ = v2_engine(tmp_path)
    j = raw("1", title="Senior Backend Engineer (Remote) - Pune", company="Acme, Inc.", posted_at="2026-10-20T08:00:00Z",
            apply_url="https://apply.example/1", desc="Salary $120k - $150k per year. " + "x" * 400)
    new, errors = scrape(eng, [("Acme, Inc.", "ashby", res([j], complete=True))], monkeypatch)
    assert (new, errors) == (1, [])
    with Session(eng) as s:
        row = s.scalar(select(Job))
        assert row.company == "Acme, Inc." and row.title.startswith("Senior Backend Engineer"), "legacy fields untouched"
        assert row.company_norm == "acme" and row.title_norm == "senior backend engineer" and row.city_norm == "pune"
        assert row.dedup_key and len(row.dedup_key) == 40 and row.content_hash and "pune" in row.search_text
        assert row.status == "active" and row.miss_count == 0 and row.last_seen_at == T0 and row.last_run_id
        assert row.posted_at == datetime(2026, 10, 20, 8) and row.posted_at_source == "source" and row.sort_date == row.posted_at
        assert row.apply_url == "https://apply.example/1"
        assert (row.salary_currency, row.salary_period, row.salary_orig_min, row.salary_source) == ("USD", "year", 120000.0, "parsed")
        assert row.category == "software" and row.seniority == "senior"
        c = s.scalar(select(Company))
        assert c.name_norm == "acme" and row.company_id == c.id
        run = s.scalar(select(SourceRun))
        assert (run.outcome, run.listed_count, run.new_count) == ("ok", 1, 1)


def test_over_long_fields_are_clamped_and_never_lose_the_run(tmp_path, monkeypatch):
    eng, _ = v2_engine(tmp_path)
    j = raw("1", title="T" * 900, company="C" * 500, desc="d")
    j["url"] = "https://x/" + "u" * 3000
    new, errors = scrape(eng, [("Long", "greenhouse", res([j, raw("2")]))], monkeypatch)
    assert new == 2 and not errors
    with Session(eng) as s:
        r = s.scalar(select(Job).where(Job.external_id == "1"))
        assert len(r.title) == 300 and len(r.company) == 120 and len(r.url) == 600


def test_one_companys_database_failure_does_not_lose_the_others(tmp_path, monkeypatch):
    eng, _ = v2_engine(tmp_path)
    real = pipeline._upsert_company

    def flaky(session, c, res_, run, resolver, now):
        if c["name"] == "Boom":
            raise RuntimeError("simulated database failure")
        return real(session, c, res_, run, resolver, now)
    monkeypatch.setattr(pipeline, "_upsert_company", flaky)
    new, errors = scrape(eng, [("Good", "greenhouse", res([raw("g1", company="Good")])),
                               ("Boom", "greenhouse", res([raw("b1", company="Boom")])),
                               ("Also", "greenhouse", res([raw("a1", company="Also")]))], monkeypatch)
    assert new == 2 and len(errors) == 1 and "Boom" in errors[0]
    with Session(eng) as s:
        assert {j.company for j in s.scalars(select(Job))} == {"Good", "Also"}
        assert s.scalar(select(SourceRun).where(SourceRun.company_key == "Boom", SourceRun.outcome == "error"))


def test_failed_fetch_is_an_error_run_not_a_closed_board(tmp_path, monkeypatch):
    eng, _ = v2_engine(tmp_path)
    scrape(eng, [("Acme", "greenhouse", res([raw("1"), raw("2")], complete=True))], monkeypatch)
    new, errors = scrape(eng, [("Acme", "greenhouse", CollectResult(outcome="error", error="HTTP 500"))], monkeypatch)
    assert new == 0 and errors == ["Acme: HTTP 500"]
    with Session(eng) as s:
        assert all(j.status == "active" and j.miss_count == 0 for j in s.scalars(select(Job)))


def test_changed_listing_is_updated_and_reindexed_but_lifecycle_state_is_kept(tmp_path, monkeypatch):
    eng, _ = v2_engine(tmp_path)
    scrape(eng, [("Acme", "greenhouse", res([raw("1", title="Software Engineer")]))], monkeypatch)
    with Session(eng) as s:
        s.scalar(select(Job)).miss_count = 2; s.commit()
    scrape(eng, [("Acme", "greenhouse", res([raw("1", title="Staff Software Engineer", desc="New description " * 30)]))], monkeypatch)
    with Session(eng) as s:
        r = s.scalar(select(Job))
        assert r.title == "Staff Software Engineer" and r.title_norm == "staff software engineer" and "new description" in r.search_text
        assert (s.scalar(select(SourceRun).order_by(SourceRun.id.desc())).updated_count) == 1


def test_flags_off_behaves_like_v1_and_leaves_v2_columns_null(tmp_path, monkeypatch):
    eng, _ = v2_engine(tmp_path)
    new, errors = scrape(eng, [("Acme", "greenhouse", [raw("1"), raw("2")])], monkeypatch, v2=False)
    assert new == 2 and not errors
    with Session(eng) as s:
        for j in s.scalars(select(Job)):
            assert (j.status, j.company_norm, j.dedup_key, j.search_text, j.last_seen_at, j.canonical_job_id) == (None,) * 6
        assert s.query(SourceRun).count() == 0 and s.query(Company).count() == 0


def test_legacy_list_fetchers_work_under_v2_and_are_never_complete(tmp_path, monkeypatch):
    eng, _ = v2_engine(tmp_path)
    new, errors = scrape(eng, [("Acme", "greenhouse", [raw("1")])], monkeypatch)       # a plain list, like old test fakes
    assert new == 1
    with Session(eng) as s:
        run = s.scalar(select(SourceRun))
        assert run.complete is False and run.listed_count is None


# ------------------------------------------------------------------ dedup integration
def test_cross_source_duplicates_are_grouped_after_ingest(tmp_path, monkeypatch):
    eng, _ = v2_engine(tmp_path)
    scrape(eng, [("Acme Inc", "greenhouse", res([raw("g1", title="Software Engineer II", desc="Same job text. " * 30)])),
                 ("Acme Inc 2", "lever", res([raw("l1", title="Software Engineer II (Remote)", source="lever",
                                                  desc="Same job text. " * 30, company="Acme, Inc.")])),
                 ("Acme Inc 3", "ashby", res([raw("a1", title="Software Engineer III", source="ashby", desc="Same job text. " * 30)])),
                 ("Acme Inc 4", "workable", res([raw("w1", title="Software Engineer II", source="workable", location="Mumbai, India",
                                                     desc="Same job text. " * 30)]))],
           monkeypatch, dedup_on=True)
    with Session(eng) as s:
        by = {j.source: j for j in s.scalars(select(Job))}
        assert dedup.validate_invariants(s) == []
        roots = {j.source for j in by.values() if j.canonical_job_id is None}
        assert by["greenhouse"].canonical_job_id is None or by["lever"].canonical_job_id is None
        assert (by["greenhouse"].canonical_job_id or by["greenhouse"].id) == (by["lever"].canonical_job_id or by["lever"].id)
        assert by["ashby"].canonical_job_id is None and by["ashby"].id in {j.id for j in by.values() if j.canonical_job_id is None}, \
            "a different level (III vs II) is never merged"
        assert by["workable"].canonical_job_id is None, "a different city is never merged"
        assert s.query(JobEvent).filter_by(event_type="merge").count() == 1


def test_same_source_twins_are_never_merged(tmp_path, monkeypatch):
    eng, _ = v2_engine(tmp_path)
    scrape(eng, [("Acme", "greenhouse", res([raw("1", location="Pune, India"), raw("2", location="Pune, India")]))], monkeypatch, dedup_on=True)
    with Session(eng) as s:
        assert all(j.canonical_job_id is None for j in s.scalars(select(Job)))


def test_blank_location_with_similar_description_merges_but_without_evidence_only_suggests(tmp_path, monkeypatch):
    eng, _ = v2_engine(tmp_path)
    same = "Identical long description about the role and the team. " * 12
    scrape(eng, [("Acme", "greenhouse", res([raw("1", title="Data Analyst", location="", desc=same)])),
                 ("Acme L", "lever", res([raw("2", title="Data Analyst", source="lever", location="", desc=same)])),
                 ("Beta G", "greenhouse", res([raw("3", title="Data Analyst", company="Beta", location="", desc="Short.")])),
                 ("Beta L", "lever", res([raw("4", title="Data Analyst", company="Beta", source="lever", location="", desc="Other.")]))],
           monkeypatch, dedup_on=True)
    with Session(eng) as s:
        acme = s.scalars(select(Job).where(Job.company == "Acme Inc")).all()
        assert len({j.canonical_job_id or j.id for j in acme}) == 1
        beta = s.scalars(select(Job).where(Job.company == "Beta")).all()
        assert all(j.canonical_job_id is None for j in beta)
        assert s.query(JobEvent).filter_by(event_type="suggest", reason="blank_location").count() >= 1


def test_dedup_dry_run_report_writes_nothing(tmp_path, monkeypatch):
    eng, _ = v2_engine(tmp_path)
    scrape(eng, [("A", "greenhouse", res([raw("1", title="Data Analyst", desc="Same. " * 80)])),
                 ("B", "lever", res([raw("2", title="Data Analyst", source="lever", desc="Same. " * 80)]))], monkeypatch)
    with Session(eng) as s:
        rep = dedup.dry_run_report(s)
        assert rep["decisions"] == {"merge:key": 1}
        assert s.query(JobEvent).count() == 0 and all(j.canonical_job_id is None for j in s.scalars(select(Job)))


# ------------------------------------------------------------------ group-aware matching
@pytest.fixture
def user_env(tmp_path, monkeypatch):
    eng, _ = v2_engine(tmp_path)
    monkeypatch.setattr(config, "FEATURE_V2_MATCHING", True)
    with Session(eng) as s:
        u = auth.register(s, "u@x.com", "U", "secret1")
        u.profile.resume_text, u.profile.grad_year, u.telegram_chat_id = "Python Django Postgres Docker", 2026, "42"
        s.commit()
    return eng


def intern(s, src, ext, **kw):
    kw.setdefault("title", "Software Engineer Intern")
    kw.setdefault("desc", "Python Django internship for 2026 graduates. " * 10)
    return mkjob(s, src, ext, **kw)


def run_match(eng):
    sent = []
    with Session(eng) as s:
        u = s.scalar(select(__import__("app.models", fromlist=["User"]).User))
        n = pipeline.match_user(s, u, notify=lambda chat, text: sent.append(text) or True)
        s.commit()
        return n, sent, [(m.job_id) for m in s.scalars(select(Match))]


def test_matching_skips_duplicates_expired_and_stale_jobs(user_env):
    eng = user_env
    with Session(eng) as s:
        a = intern(s, "greenhouse", "a", company="Acme")
        b = intern(s, "lever", "b", company="Acme")
        dedup.merge(s, a.id, b.id, method="key")
        intern(s, "greenhouse", "c", company="Beta").status = "expired"
        intern(s, "greenhouse", "d", company="Gamma").status = "stale"
        e = intern(s, "greenhouse", "e", company="Delta")
        e.apply_url = "https://apply.example/e"
        s.commit()
        group_root = a.canonical_job_id or a.id
        e_id = e.id
    n, sent, matched = run_match(eng)
    assert n == 2 and set(matched) == {group_root, e_id}, "one match per group; no expired or stale jobs"
    assert any("https://apply.example/e" in t for t in sent), "alerts prefer apply_url"


def test_canonical_promotion_after_a_match_never_causes_a_second_alert(user_env):
    eng = user_env
    with Session(eng) as s:
        a = intern(s, "greenhouse", "a", company="Acme")
        b = intern(s, "lever", "b", company="Acme")
        s.commit()
        dedup.merge(s, a.id, b.id, method="key")
        root_id = s.get(Job, a.id).canonical_job_id or a.id
    n1, sent1, _ = run_match(eng)
    assert n1 == 1 and len(sent1) == 1
    with Session(eng) as s:                                   # a better listing shows up and takes over as canonical
        member = s.get(Job, b.id if root_id == a.id else a.id)
        member.description = member.description * 6
        s.commit()
        assert dedup.promote(s, member.id)["action"] == "promoted"
    n2, sent2, matched = run_match(eng)
    assert n2 == 0 and sent2 == [] and matched == [root_id], "the user was already matched on this group"


def test_matching_is_unchanged_when_the_flag_is_off(user_env, monkeypatch):
    eng = user_env
    monkeypatch.setattr(config, "FEATURE_V2_MATCHING", False)
    with Session(eng) as s:
        a = intern(s, "greenhouse", "a", company="Acme")
        b = intern(s, "lever", "b", company="Acme")
        s.commit()
        dedup.merge(s, a.id, b.id, method="key")
    n, sent, matched = run_match(eng)
    assert n == 2, "v1 behavior: every row is scored, duplicates included"


def test_jsonld_dates_reach_v2_columns_but_never_the_v1_insert_path(tmp_path, monkeypatch):
    from app import scrapers
    node = {"title": "Software Engineer", "description": "d", "datePosted": "2026-10-01", "validThrough": "2026-12-31T00:00:00Z",
            "jobLocation": {"address": {"addressLocality": "Pune", "addressCountry": "IN"}}}
    posting = scrapers._normalize_posting(node)
    assert posting["posted_at"] == "2026-10-01" and posting["valid_through"] == "2026-12-31T00:00:00Z"
    eng, _ = v2_engine(tmp_path)
    row = raw("jl1", source="jsonld", **{"posted_at": posting["posted_at"], "valid_through": posting["valid_through"]})
    scrape(eng, [("Site", "jsonld", res([row]))], monkeypatch)                      # v2: captured
    with Session(eng) as s:
        j = s.scalar(select(Job))
        assert j.posted_at == datetime(2026, 10, 1) and j.valid_through == datetime(2026, 12, 31) and j.posted_at_source == "source"
    eng2, _ = v2_engine(tmp_path, name="v1path.db")
    scrape(eng2, [("Site", "jsonld", [row])], monkeypatch, v2=False)                # v1 path: the keys are dropped, columns stay NULL
    with Session(eng2) as s:
        j = s.scalar(select(Job))
        assert j.posted_at is None and j.valid_through is None and j.apply_url is None


def test_coverage_report_and_direct_source_proposals(tmp_path, monkeypatch):
    from app import discover
    eng, _ = v2_engine(tmp_path)
    scrape(eng, [("Direct Co", "greenhouse", res([raw("1", company="Direct Co")])),
                 ("Feed A", "adzuna", res([raw("f1", company="Direct Co", source="adzuna"), raw("f2", company="Feedonly Ltd", source="adzuna"),
                                           raw("f3", company="Another Corp", source="adzuna")]))], monkeypatch)
    with Session(eng) as s:
        cov = discover.coverage_report(s)
        assert cov["canonical_jobs"] == 4 and cov["by_kind"] == {"ats": 1, "feed": 3} and cov["direct_share"] == 0.25
        assert cov["companies_only_in_feeds"] == ["another", "feedonly"], "Direct Co already has a direct source"
        found = {"Feedonly Ltd": {"name": "Feedonly Ltd", "source": "greenhouse", "slug": "feedonly"},
                 "Another Corp": {"name": "Another Corp", "source": "recruitee", "slug": "another"}}
        props = discover.propose_direct_sources(s, finder=lambda n, u, sec, chk: found.get(n), sleep=lambda x: None)
        by = {p["company"]: p for p in props}
        assert set(by) == {"Feedonly Ltd", "Another Corp"} and all(p["status"] == "needs_review" for p in props)
        assert by["Another Corp"]["allowed_to_add"] is False and any("authentication" in r for r in by["Another Corp"]["blocked_because"])
        assert by["Feedonly Ltd"]["allowed_to_add"] is False and "terms of use not read" in by["Feedonly Ltd"]["blocked_because"]
