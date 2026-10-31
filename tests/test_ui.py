from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "streamlit_app.py")

from app import demo
from app.db import SessionLocal, init_db


def test_ui_pages_render_without_errors():
    init_db()
    with SessionLocal() as s:
        user = demo.seed(s)
        from app import pipeline
        pipeline.run_all(s, companies=[])
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception  # login screen
    at.session_state["uid"] = user.id
    at.run()
    assert not at.exception
    for page in ["Matches", "Tracker", "Profile", "Contacts", "Run now", "Account"]:
        at.session_state["nav"] = page
        at.run()
        assert not at.exception, f"{page}: {at.exception}"
        if page == "Run now":
            assert len(at.dataframe) >= 2        # newest jobs + jobs per company


def test_new_user_lands_on_profile_and_next_button_works():
    from app import auth
    with SessionLocal() as s:
        user = auth.register(s, "fresh@example.com", "Fresh User", "secret1")
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    at.session_state["uid"] = user.id
    at.run()
    assert not at.exception and at.session_state["nav"] == "Profile"   # profile first, not Matches
    at.session_state["nav"] = "Matches"
    at.run()
    assert not at.exception                                                # incomplete profile: friendly prompt
    assert any("Complete your profile" in i.value for i in at.info)



def test_zero_counts_are_shown_not_blank():
    from app import auth
    with SessionLocal() as s:
        user = auth.register(s, "zero@example.com", "Zero Matches", "secret1")
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.session_state["uid"] = user.id
    at.session_state["nav"] = "Run now"
    at.run()
    assert not at.exception
    assert any("<b>0</b>" in m.value for m in at.markdown)      # "Your eligible matches: 0" must not be blank
