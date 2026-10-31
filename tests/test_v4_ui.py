"""Search page, lifecycle badges and the source-health panel, driven through Streamlit's AppTest."""
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from streamlit.testing.v1 import AppTest

from app import auth, tracker
from app.db import SessionLocal, init_db
from app.models import Application, Job, SourceRun, now
from tests.helpers_v4 import mkjob

APP = str(Path(__file__).resolve().parent.parent / "streamlit_app.py")


def _user(email):
    with SessionLocal() as s:
        return auth.register(s, email, "Search Tester", "secret1").id


def _page(uid, **state):
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.session_state["uid"] = uid
    at.session_state["nav"] = "Search"
    for k, v in state.items():
        at.session_state[k] = v
    return at.run()


def _text(at):
    return " ".join(m.value for m in at.markdown)


def _seed_quantum(n=12):
    """Idempotent: the test database is shared by the whole session."""
    init_db()
    with SessionLocal() as s:
        have = {j.external_id: j for j in s.scalars(select(Job).where(Job.external_id.like("zyx%")))}
        if len(have) >= n + 1:
            return [have[f"zyx{i}"].id for i in range(n)]
        ids = []
        for i in range(n):
            j = mkjob(s, "greenhouse", f"zyx{i}", company="Zyxco", title=f"Zyxquantum Engineer {i:02d}", location="Pune, India",
                      salary_min=10 + i, salary_max=12 + i)
            j.sort_date = now() - timedelta(days=i)
            ids.append(j.id)
        dup = mkjob(s, "lever", "zyx0-lever", company="Zyxco", title="Zyxquantum Engineer 00", location="Pune, India")
        dup.canonical_job_id = ids[0]
        s.commit()
        return ids


def test_search_page_is_registered_and_the_original_pages_still_exist():
    import streamlit_app  # noqa: F401  (module runs main() under AppTest; here we only check the registry via AppTest)
    uid = _user("pages@x.com")
    at = _page(uid)
    assert not at.exception
    for page in ["Matches", "Search", "Tracker", "Profile", "Contacts", "Run now", "Account"]:
        at.session_state["nav"] = page
        at.run()
        assert not at.exception, f"{page}: {at.exception}"


def test_keyword_search_filters_paginates_and_collapses_duplicates():
    ids = _seed_quantum()
    uid = _user("kw@x.com")
    at = _page(uid)
    at.text_input(key="se_q").set_value("zyxquantum").run()
    assert not at.exception
    assert any("12 job(s) found" in c.value for c in at.caption), [c.value for c in at.caption]
    html = _text(at)
    assert html.count("Zyxquantum Engineer") == 10 and "also on lever" in html, "page size 10; the duplicate is folded into its canonical job"
    # next page
    at.button(key="se_next").click().run()
    assert not at.exception
    html2 = _text(at)
    assert html2.count("Zyxquantum Engineer") == 2 and "Page 2" in " ".join(c.value for c in at.caption)
    at.button(key="se_prev").click().run()
    assert "Page 1" in " ".join(c.value for c in at.caption)


def test_changing_the_query_returns_to_page_one_and_filters_apply():
    _seed_quantum()
    uid = _user("filt@x.com")
    at = _page(uid)
    at.text_input(key="se_q").set_value("zyxquantum").run()
    at.button(key="se_next").click().run()
    at.number_input(key="se_sal").set_value(20.0).run()            # eff_max >= 20 -> engineers 08..11 (salary 12+i >= 20)
    assert not at.exception
    assert any("Page 1" in c.value for c in at.caption), "a new query starts at page 1"
    assert any("4 job(s) found" in c.value for c in at.caption), [c.value for c in at.caption]
    at.text_input(key="se_q").set_value("zyxquantum nonexistentword").run()
    assert any("No jobs match" in i.value for i in at.info)


def test_save_to_tracker_is_group_aware_and_idempotent():
    ids = _seed_quantum()
    uid = _user("save@x.com")
    at = _page(uid)
    at.text_input(key="se_q").set_value("zyxquantum engineer 00").run()
    at.button(key=f"se_save{ids[0]}").click().run()
    assert not at.exception
    with SessionLocal() as s:
        assert s.query(Application).filter_by(user_id=uid).count() == 1
        dup = s.scalar(select(Job).where(Job.external_id == "zyx0-lever"))
        from app import search as jobsearch
        app, created = jobsearch.save_to_tracker(s, uid, dup.id)        # saving the duplicate listing must NOT add a second one
        assert created is False and app.job_id == ids[0]
        assert s.query(Application).filter_by(user_id=uid).count() == 1


def test_possibly_closed_badge_on_tracker_and_search_toggle():
    init_db()
    with SessionLocal() as s:
        j = mkjob(s, "greenhouse", "closing1", company="Stalecorp", title="Zyxstale Analyst", location="Pune, India")
        j.status = "stale"
        s.commit()
        job_id = j.id
    uid = _user("stale@x.com")
    with SessionLocal() as s:
        tracker.add_application(s, uid, job_id); s.commit()
    at = _page(uid, nav="Tracker")
    at.session_state["nav"] = "Tracker"
    at.run()
    assert not at.exception and "possibly closed" in _text(at)
    at.session_state["nav"] = "Search"
    at.run()
    at.text_input(key="se_q").set_value("zyxstale").run()
    assert any("No jobs match" in i.value for i in at.info), "stale jobs are hidden by default"
    at.checkbox(key="se_stale").check().run()
    assert "Zyxstale Analyst" in _text(at) and "possibly closed" in _text(at)


def test_source_health_panel_shows_runs_without_error():
    init_db()
    with SessionLocal() as s:
        s.add(SourceRun(source="greenhouse", company_key="HealthCo", outcome="ok", complete=True, listed_count=7, new_count=2, finished_at=now()))
        s.add(SourceRun(source="lever", company_key="BlockedCo", outcome="blocked", complete=False, finished_at=now()))
        s.commit()
    uid = _user("health@x.com")
    at = _page(uid, nav="Run now")
    at.session_state["nav"] = "Run now"
    at.run()
    assert not at.exception
    assert any("Source health" in e.label for e in at.expander)
    frames = [d.value for d in at.dataframe]
    assert any("HealthCo" in set(f.get("Company / feed", [])) for f in frames if hasattr(f, "get") or hasattr(f, "columns"))


def _nav_options(at):
    return [list(g.options) for g in at.get("button_group")][0]


def test_search_page_can_be_switched_off_by_flag(monkeypatch):
    from app import config
    uid = _user("off@x.com")
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.session_state["uid"] = uid
    at.run()
    assert "Search" in _nav_options(at)
    monkeypatch.setattr(config, "FEATURE_V2_SEARCH", False)
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.session_state["uid"] = uid
    at.run()
    assert not at.exception and "Search" not in _nav_options(at)
    assert _nav_options(at) == ["Matches", "Tracker", "Profile", "Contacts", "Run now", "Account"], "the v1 navigation is unchanged"
