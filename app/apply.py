"""Local autofill helper. NEVER submits: you review the form and click Submit yourself.

Usage:  pip install playwright && playwright install chromium
        python -m app.apply --user 1 --job 5
Fills name, email, phone, links, education and city from your profile, uploads your resume, and drafts answers
to open questions with OpenAI (if OPENAI_API_KEY is set). Sensitive questions are left blank for you.
Dropdowns, checkboxes and radio buttons are not touched.
"""
import argparse

from app import autofill, tracker
from app.db import SessionLocal, init_db
from app.models import Job, User

FIELD_SELECTOR = ("input:not([type=hidden]):not([type=file]):not([type=checkbox]):not([type=radio])"
                  ":not([type=submit]):not([type=button]), textarea")
SCAN_JS = """els => els.map((el, i) => {
  let t = '';
  if (el.id) { const l = document.querySelector('label[for="' + el.id + '"]'); if (l) t = l.innerText; }
  if (!t) { const p = el.closest('label'); if (p) t = p.innerText; }
  if (!t) t = el.getAttribute('aria-label') || el.placeholder || el.name || '';
  el.setAttribute('data-jh', i);
  return {i: i, tag: el.tagName.toLowerCase(), type: el.type || '', label: t.trim()};
})"""


def prepare_application(user_id: int, job_id: int, ui: bool = False) -> None:
    from playwright.sync_api import sync_playwright

    init_db()
    with SessionLocal() as s:
        user, job = s.get(User, user_id), s.get(Job, job_id)
        if not user or not job:
            raise SystemExit("Unknown user or job id.")
        profile = user.profile
        tracker.add_application(s, user_id, job_id, "Saved")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            page = browser.new_page()
            page.goto(job.url)
            page.wait_for_timeout(2500)
            plan = autofill.plan_fill(page.eval_on_selector_all(FIELD_SELECTOR, SCAN_JS), user, profile, job)
            for i, value, _kind, label in plan["fills"]:
                try:
                    page.fill(f'[data-jh="{i}"]', value, timeout=3000)
                except Exception:
                    print(f"Could not fill: {label}")
            if profile.resume_path:
                try:
                    page.set_input_files("input[type=file]", profile.resume_path, timeout=3000)
                except Exception:
                    print("Could not attach resume; upload it manually.")
            print(f"\nFilled {len(plan['fills'])} fields.")
            if plan["drafted"]:
                print("DRAFTED by AI, please read and edit:", *plan["drafted"], sep="\n  - ")
            if plan["left_blank"]:
                print("Left blank for you:", *plan["left_blank"], sep="\n  - ")
            if ui:  # started from the web app: keep the window open until you close it
                while not page.is_closed():
                    page.wait_for_timeout(1000)
            else:
                input("\nReview the form, then submit it yourself. Press Enter here when done...")
            try:
                browser.close()
            except Exception:
                pass
        if not ui and input("Did you submit? (y/N): ").strip().lower() == "y":
            tracker.set_status(s, tracker.add_application(s, user_id, job_id), "Applied")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", type=int, required=True)
    ap.add_argument("--job", type=int, required=True)
    ap.add_argument("--ui", action="store_true", help="started from the web app")
    a = ap.parse_args()
    prepare_application(a.user, a.job, a.ui)
