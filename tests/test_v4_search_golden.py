"""Golden-query tests (plan section 9). Identical expectations on SQLite (always) and PostgreSQL (TEST_POSTGRES_URL)."""
import json
import os
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app import config, migrations, search
from app.models import Job
from app.search import BadCursor, SearchQuery
from tests.helpers_v4 import FIXTURES, mkjob, v2_engine

DATA = json.loads((FIXTURES / "search_golden.json").read_text())
NOW = datetime.fromisoformat(DATA["now"])
PG = os.getenv("TEST_POSTGRES_URL", "")


def load(session):
    for j in DATA["jobs"]:
        age = timedelta(days=j["age_days"])
        row = mkjob(session, j["source"], f"g{j['id']}", company=j["company"], title=j["title"], location=j["location"],
                    desc=j["description"], remote=j["remote"], country=j["country"], salary_min=j["salary_min"],
                    salary_max=j["salary_max"])
        assert row.id == j["id"]
        row.sort_date = row.first_seen = NOW - age
        row.last_seen_at = NOW - timedelta(days=j.get("last_seen_days", 0))
        row.status = j.get("status", "active")
        row.canonical_job_id = j.get("canonical_job_id")
        if "valid_through_days" in j:
            row.valid_through = NOW + timedelta(days=j["valid_through_days"])
    session.commit()


@pytest.fixture(params=["sqlite"] + (["postgres"] if PG else []))
def s(request, tmp_path):
    if request.param == "sqlite":
        eng, _ = v2_engine(tmp_path)
        with Session(eng) as session:
            load(session)
            yield session
        return
    schema = "gs_" + uuid.uuid4().hex[:8]
    admin = create_engine(PG)
    with admin.begin() as c:
        c.execute(text(f"CREATE SCHEMA {schema}"))
    eng = create_engine(PG + ("&" if "?" in PG else "?") + f"options=-csearch_path%3D{schema}")
    migrations.ensure_schema(eng)
    try:
        with Session(eng) as session:
            load(session)
            yield session
    finally:
        eng.dispose()
        with admin.begin() as c:
            c.execute(text(f"DROP SCHEMA {schema} CASCADE"))


def ids(page):
    return [i["id"] for i in page.items]


def run(s, **kw):
    kw.setdefault("page_size", 50)
    return search.search(s, SearchQuery(**kw), now=NOW)


LIVE = {1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27}
NEWEST = [8, 1, 19, 18, 10, 7, 3, 17, 2, 21, 6, 26, 4, 27, 22, 5, 20, 24, 11, 25, 12, 23]


def test_python_developer_exact_ranking_title_hits_trust_freshness_completeness(s):
    r = run(s, q="python developer")
    assert ids(r) == [1, 2, 11, 26, 8, 12]
    assert r.candidates_capped is False and r.total_estimate == 6
    assert [i["score"] for i in r.items] == sorted([i["score"] for i in r.items], reverse=True)


@pytest.mark.parametrize("q,expected", [("c++ engineer", [4]), (".net developer", [5]), ("zurich", [6]), ("ZÜRICH", [6]),
                                         ("zürich", [6]), ("PyThOn DeVeLoPeR", [1, 2, 11, 26, 8, 12])])
def test_special_tokens_accents_and_case(s, q, expected):
    assert ids(run(s, q=q)) == expected


def test_phrase_query_requires_the_phrase(s):
    r = run(s, q='"machine learning"')
    assert set(ids(r)) == {6, 20, 7}
    assert ids(r)[-1] == 7, "a description-only hit ranks below title hits"


def test_wildcard_characters_are_literal_never_patterns(s):
    assert search._esc("100%_x\\") == "100\\%\\_x\\\\"
    assert ids(run(s, q="100%")) == [17]
    assert set(ids(run(s, q="%"))) == LIVE, "no usable keyword: treated as browse, not a match-everything wildcard"
    assert set(ids(run(s, q="   "))) == LIVE and set(ids(run(s, q=""))) == LIVE


def test_country_filter_accepts_both_separator_forms(s):
    assert set(ids(run(s, country=["Canada"]))) == {17, 21, 27}
    assert set(ids(run(s, country=["india"], q="senior"))) == {2}


def test_city_and_company_and_posted_filters(s):
    assert set(ids(run(s, city="Pune"))) == {3, 11, 12, 23, 24}
    assert set(ids(run(s, company="Acme"))) == {1, 2}, "the duplicate member row is never returned"
    assert set(ids(run(s, posted_within_days=3))) == {1, 8, 2, 3, 7, 10, 17, 18, 19}


def test_remote_filter_treats_null_as_not_remote(s):
    assert set(ids(run(s, remote=True))) == {7, 17, 26}
    assert set(ids(run(s, remote=False))) == LIVE - {7, 17, 26}


def test_min_salary_include_keeps_unknown_and_flags_it(s):
    r = run(s, min_salary_lpa=20)
    known = {2, 4, 7, 10, 11, 21, 22}
    unknown = {3, 6, 17, 18, 26}
    assert set(ids(r)) == known | unknown
    assert {i["id"] for i in r.items if not i["salary_known"]} == unknown
    assert r.unknown_salary_count == 5


def test_min_salary_exclude_drops_unknown(s):
    r = run(s, min_salary_lpa=20, unknown_salary="exclude")
    assert set(ids(r)) == {2, 4, 7, 10, 11, 21, 22}
    assert r.unknown_salary_count == 5, "reports how many unknown-salary jobs were hidden by the filter"


def test_salary_sort_known_descending_unknown_last_ties_by_id(s):
    r = run(s, sort="salary")
    assert ids(r) == [21, 10, 4, 2, 7, 22, 11, 1, 24, 23, 27, 5, 19, 8, 25, 12, 20, 26, 18, 17, 6, 3]


def test_salary_sort_paginates_without_gaps_or_duplicates(s):
    seen, cursor = [], None
    for _ in range(20):
        p = run(s, sort="salary", page_size=5, cursor=cursor)
        seen += ids(p)
        cursor = p.next_cursor
        if not cursor:
            break
    assert seen == [21, 10, 4, 2, 7, 22, 11, 1, 24, 23, 27, 5, 19, 8, 25, 12, 20, 26, 18, 17, 6, 3]


def test_newest_browse_order_and_pagination(s):
    assert ids(run(s)) == NEWEST
    pages, cursor = [], None
    while True:
        p = run(s, page_size=5, cursor=cursor)
        assert p.total == 22
        pages.append(ids(p))
        cursor = p.next_cursor
        if not cursor:
            break
    assert [len(x) for x in pages] == [5, 5, 5, 5, 2]
    assert sum(pages, []) == NEWEST


def test_a_job_inserted_between_pages_does_not_shift_later_pages(s):
    p1 = run(s, page_size=5)
    newest = mkjob(s, "greenhouse", "late", company="Newco", title="Brand New Role", location="Pune, India")
    newest.sort_date = newest.first_seen = NOW
    s.commit()
    p2 = run(s, page_size=5, cursor=p1.next_cursor)
    assert ids(p2) == NEWEST[5:10], "snapshot: rows added after page 1 must not move later pages"
    fresh = run(s, page_size=5)
    assert ids(fresh)[0] == newest.id and fresh.total == 23, "a brand-new query does see the new job"


def test_relevance_pagination_is_stable_too(s):
    full = ids(run(s, q="engineer", page_size=50))
    seen, cursor = [], None
    while True:
        p = run(s, q="engineer", page_size=2, cursor=cursor)
        seen += ids(p)
        cursor = p.next_cursor
        if not cursor:
            break
    assert seen == full and len(set(seen)) == len(seen)


def test_status_filtering_stale_expired_and_valid_through(s):
    assert not ({13, 14, 15, 16, 9} & set(ids(run(s))))
    assert 14 in ids(run(s, include_stale=True)) and 15 not in ids(run(s, include_stale=True))
    assert 15 in ids(run(s, include_expired=True))


def test_feed_freshness_window(s, monkeypatch):
    assert 9 not in ids(run(s)) and 8 in ids(run(s))
    monkeypatch.setattr(config, "FEED_MAX_AGE_DAYS", 60)
    assert 9 in ids(run(s))


def test_duplicate_group_is_one_result_with_also_on(s):
    item = next(i for i in run(s, q="python developer").items if i["id"] == 1)
    assert [a["source"] for a in item["also_on"]] == ["lever"]
    assert 13 not in ids(run(s, q="python developer"))


def test_cursors_are_signed_and_bound_to_their_query(s):
    p = run(s, page_size=5)
    with pytest.raises(BadCursor):
        run(s, page_size=5, cursor=p.next_cursor + "x")
    with pytest.raises(BadCursor):
        run(s, q="python", page_size=5, cursor=p.next_cursor)
    with pytest.raises(BadCursor):
        run(s, page_size=5, cursor="garbage")


def test_unindexed_jobs_are_reported(s):
    assert search.unindexed_count(s) == 0
    s.execute(text("UPDATE jobs SET search_text = NULL WHERE id = 1")); s.commit()
    assert search.unindexed_count(s) == 1


def test_filter_options_lists_live_values(s):
    o = search.filter_options(s)
    assert "India" in o["country"] and "Canada" in o["country"] and "Permanent" in o["job_type"]
