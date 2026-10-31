"""Legacy interfaces that v2 promises to keep (plan section 12)."""
import inspect
import json

from app import config, db, notifier, pipeline, scrapers, tracker
from app.matcher import score_job


def params(fn):
    return list(inspect.signature(fn).parameters)


def test_scrape_all_signature_and_return_type_are_unchanged():
    assert params(pipeline.scrape_all) == ["session", "companies", "fetcher", "workers"]
    d = inspect.signature(pipeline.scrape_all).parameters
    assert d["companies"].default is None and d["fetcher"].default is scrapers.fetch_jobs and d["workers"].default == 8


def test_other_public_signatures_are_unchanged():
    assert params(pipeline.match_user) == ["session", "user", "scorer", "notify", "max_jobs"]
    assert params(pipeline.run_all)[0] == "session"
    assert params(notifier.send_telegram) == ["chat_id", "text"]
    assert params(tracker.add_application) == ["session", "user_id", "job_id", "status"]
    assert params(scrapers.fetch_jobs) == ["company", "known"]
    assert params(db.init_db) == [] and callable(db._add_missing_columns)
    assert params(score_job)[:2] == ["profile", "job"]


def test_every_legacy_collector_is_still_registered():
    assert {"greenhouse", "lever", "ashby", "workday", "workable", "smartrecruiters", "recruitee", "jsonld", "page",
            "jooble", "adzuna"} <= set(scrapers.FETCHERS)
    for fn in scrapers.FETCHERS.values():
        assert params(fn)[:3] == ["name", "slug", "known"]


def test_legacy_helpers_still_exist():
    for name in ("load_companies", "load_feeds", "feeds_due", "mark_feeds", "fetch_jobs", "FETCHERS", "BIG_EMPLOYERS", "FEED_SOURCES"):
        assert hasattr(scrapers, name), name


def test_config_keeps_its_environment_variable_names():
    for name in ("DATABASE_URL", "MAX_JOBS_PER_USER_PER_RUN"):
        assert hasattr(config, name), name


def test_v2_flags_default_to_the_v1_behaviour():
    for name, default in (("FEATURE_V2_INGEST", False), ("FEATURE_V2_DEDUP", False), ("FEATURE_V2_MATCHING", False)):
        assert getattr(config, name) is default, f"{name} must default to off so a fresh upgrade behaves exactly like v1"
    assert config.EXPIRY_MODE in ("dry_run", "off"), "expiry never enforces by default"
    assert config.INGEST_SCOPE == "software"


def test_companies_json_format_is_unchanged():
    rows = json.loads((config.ROOT / "companies.json").read_text())
    assert rows and all({"name", "source", "slug"} <= set(r) for r in rows)
    assert {r["source"] for r in rows} <= set(scrapers.FETCHERS)


def test_enrichment_refactor_keeps_fetch_jobs_behaviour(monkeypatch):
    raw = [dict(source="greenhouse", company="A", external_id="1", title="Software Engineer", location="Pune, India", url="u",
                description="Salary 12-18 LPA. Remote friendly"),
           dict(source="greenhouse", company="A", external_id="2", title="Sales Manager", location="Pune, India", url="u", description="d")]
    monkeypatch.setitem(scrapers.FETCHERS, "greenhouse", lambda name, slug, known, opts=None: [dict(r) for r in raw])
    out = scrapers.fetch_jobs({"name": "A", "source": "greenhouse", "slug": "a", "sector": "SaaS"})
    assert [j["external_id"] for j in out] == ["1"], "non-software titles are still dropped (INGEST_SCOPE=software)"
    assert (out[0]["salary_min"], out[0]["salary_max"], out[0]["sector"], out[0]["country"]) == (12.0, 18.0, "SaaS", "India")
    monkeypatch.setattr(config, "INGEST_SCOPE", "all")
    assert [j["external_id"] for j in scrapers.fetch_jobs({"name": "A", "source": "greenhouse", "slug": "a"})] == ["1", "2"]
