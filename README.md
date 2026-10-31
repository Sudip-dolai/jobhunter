# Job Hunter

Multi-user job assistant: **Watch → Match → Find referral → Apply (prepare) → Track → Remind**.
Scrapes each company once (shared `jobs` table), scores every new job per user, alerts on Telegram, tracks applications.

## Quick start (about 2 minutes, no API keys needed)
```bash
python -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements.txt
python -m pytest -q                 # all tests should pass
python run_pipeline.py --demo       # seeds demo data and scores it offline
python -m app.discover              # (needs internet) finds which companies it can monitor
streamlit run streamlit_app.py      # login: demo@example.com / demo1234
```
Needs Python 3.10+.

## Covering TCS, Infosys, HCL, Accenture and other big employers
These companies use their own career sites, which cannot be read directly. Instead the app can search a job aggregator:
1. Get a free key at **jooble.org/api/about** (name, email, website; no card needed).
2. Put it in `.env` as `JOOBLE_API_KEY=...` (optional: `JOOBLE_LOCATIONS=India,United States`). Restart.
3. Each run then searches software jobs, plus targeted searches for TCS, Infosys, HCLTech, Accenture, Wipro, Cognizant, Capgemini, Tech Mahindra, LTIMindtree, IBM and Deloitte.
Feeds run at most every 6 hours (`FEED_INTERVAL_HOURS`) to keep API usage low. Links go through Jooble to the employer's page, and the descriptions are short snippets, so match scores for these jobs are rougher.
Coverage of any specific company is **not guaranteed**: Jooble only returns what it has. Those big firms also hire freshers mostly through campus and NQT drives, not job posts. Adzuna is supported too (`ADZUNA_APP_ID` / `ADZUNA_APP_KEY`), but its free allowance is small.

## Any careers page (TCS, Infosys, HCL, Accenture, ...)
You can give the app the link of any company's careers page and it will read it:
- **In the app:** Run now > Add career page links. One per line: `Name | link`, or just the link. Use the page that **lists the jobs** (a search-results page, ideally already filtered to software jobs in your country), not the company home page.
- **In bulk:** put them in `companies_input.txt` (`Name | link`) and run **Find and add companies**.
How it works: the app tries known job boards first, then structured job data, and otherwise reads the page directly: it finds links that look like software jobs, opens each one and reads the details. If the plain page shows no jobs and **Playwright** is installed, it opens the page in a real browser (needed for JavaScript sites; run `pip install playwright` then `playwright install chromium`).
It obeys each site's `robots.txt` and waits between requests. It cannot get past logins, CAPTCHAs, bot blocking, or pages that need you to type a search first. Check **Run now > Jobs per company**: a company with 0 jobs needs a better link. Quality varies a lot from site to site.

## One-click flow (what to do)
1. **Profile:** fill it in and add your resume (new accounts open here).
2. **Run now > Find and add companies:** one click, 5 to 15 minutes, adds every company the app can read (only needed once).
3. **Run now > Find new jobs now:** scrapes everything and scores it for you. Or run `python run_pipeline.py --loop --every 30` and it does this by itself (it also does step 2 automatically the first time).
4. **Matches:** open a job, then **Autofill application** opens the real application page in a browser on your PC with your details and resume filled in. You review it and click Submit.
5. **Tracker:** keeps the status. Gmail sync and Telegram alerts update it for you.
Jobs asking for more experience than you have (set in Profile) are skipped automatically.

## Monitor many companies (all sectors)
1. Put companies in `companies_input.txt` (`# Sector` headers, then `Name | careers URL`). ~110 are pre-filled from the Let's Code directory.
2. Run `python -m app.discover` (needs internet). It detects Greenhouse, Lever, Ashby and Workday boards, checks each one, and writes `companies.json`.
3. Companies on custom portals (Google, Amazon, TCS, ...) land in `unsupported.txt`. Those need their own scraper.
4. Entries marked `"guess": true` were found by name only. Check they are the right company.
5. `python run_pipeline.py` scrapes all companies in parallel. Only software-engineer roles are stored.

**Filters (per user, in Profile):** sectors, minimum salary in LPA (default 7, no maximum), optional "hide jobs with no salary", batch, CGPA.
Salaries in USD, EUR, GBP, CAD, AUD and SGD are converted to LPA at approximate fixed rates (`RATES` in `app/filters.py`); hourly and monthly pay is ignored. Most postings don't list a salary. Jobs with an unknown salary are kept by default; only jobs that state a salary below your minimum are dropped.
Older databases are upgraded automatically.

## New in this version
- **Gmail tracking:** Profile > Gmail tracking. Turn on 2-step verification, create a Google *App Password*, paste it in. It is stored encrypted (set `SECRET_KEY` in `.env`) and read over IMAP in read-only mode. Emails that mention a company you track move the application to OA / Interview / Offer / Rejected (never backwards). Runs in `run_pipeline.py`, or press "Sync Gmail now" in Tracker.
- **Autofill:** `python -m app.apply --user 1 --job 5`. Fills name, email, phone, links, education (degree, branch, college, CGPA, 10th/12th, graduation year) and city from Profile, uploads your resume, and drafts answers to open questions with OpenAI. Gender, salary, relocation, notice period and similar questions are never answered automatically. Dropdowns, checkboxes and radio buttons are left to you. It never submits.
- **Draft answers in the app:** Matches > "Draft answers to common questions". Uses only facts from your resume.
- **Custom career sites:** new sources `workable`, `smartrecruiters`, `recruitee` and `jsonld`. `jsonld` reads the schema.org JobPosting data that many custom career sites publish for Google Jobs (from the careers page or its sitemap). `python -m app.discover` detects this automatically. For JavaScript-only sites add `"render": true` to the company in `companies.json` (needs Playwright). Sites that publish no such data (for example some big-company portals) still need a custom scraper.
- **Country, remote and job type:** set in Profile. Remote jobs are always kept when you pick countries. Jobs with unknown country or type are kept. Matches shows Location, Remote and Type columns.

## New in v2: search engine, de-duplication, job lifecycle
Read **UPGRADING.md** first. v2 adds a global **Search** page, cross-source duplicate grouping, evidence-based "possibly closed"
detection, verified backups and a maintenance CLI (`python -m app.migrations status|upgrade|backup|backfill|dedup|validate`).
All of it is off or read-only until you switch it on, and the original behaviour and tests are unchanged.

## Using it on your phone
- **Same Wi-Fi (easiest):** double-click `run_on_phone.bat`. It prints addresses like `http://192.168.1.5:8501`. Open one in your phone browser. Allow the Windows firewall prompt for Private networks. Your PC must stay on.
- **From anywhere:** deploy the app (see the Google Cloud section) and open its web address on your phone.
- The layout adapts to small screens: cards stack, buttons are full width, and the menu opens from the arrow at the top left.

## First-time flow
Create account, fill in **Profile** and add your resume, then **Run now**, then **Matches**. New accounts open on Profile automatically.

## How it works
| Step | Where | Notes |
|---|---|---|
| Watch | `app/scrapers.py`, `companies.json` | Greenhouse, Lever, Ashby and Workday public JSON APIs (see above). |
| Match | `app/matcher.py` | Free rule filter first (seniority, role keywords, batch year, CGPA), then OpenAI `gpt-4o-mini`. With no `OPENAI_API_KEY` (or if the call fails) it falls back to a keyword matcher. |
| Referral | `app/referral.py` | LinkedIn *search link* + message draft + your own contacts list. Nothing is scraped from LinkedIn. |
| Apply | `app/apply.py` | Local Playwright autofill. It never submits: you review and click Submit. Optional install. |
| Track / Remind | `app/tracker.py`, `app/pipeline.py` | Statuses, 7-day follow-up reminders via Telegram, `classify_email()` ready for Gmail later. |
| Multi-user | `app/auth.py`, `app/models.py` | Every table has `user_id`. Passwords are PBKDF2-hashed. "Delete my data" is in the Account page. |

## Configuration
Copy `.env.example` to `.env`. Everything is optional.
- `OPENAI_API_KEY`: enables LLM scoring.
- `TELEGRAM_BOT_TOKEN`: create a bot with @BotFather; each user adds their chat id (from @userinfobot) in Profile.
- `DATABASE_URL`: leave empty for SQLite; paste your Supabase Postgres string for production.
- `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`: also store resumes in a private Supabase bucket named `resumes`.

## Make it fully automatic
- **On your own PC or a VM:** `python run_pipeline.py --loop --every 30` keeps scraping, matching, alerting and syncing Gmail by itself. Keep it running (for example in `tmux`, or as a service).
- **With Docker:** run the same image twice: once normally (web app) and once with `MODE=worker` (the automatic loop). On Google Cloud use a Cloud Run **Job** or a small VM for the worker, since Cloud Run services sleep when nobody visits.
- **Or use a scheduler:** run `python run_pipeline.py` from cron or Cloud Scheduler every 30 to 60 minutes.
Telegram alerts need your bot token in `.env` and your chat id in Profile. Without them everything still works; you just won't get phone alerts.

Final application submit stays manual on purpose: `python -m app.apply` fills the form and you click Submit.

## Deploy on Google Cloud
```bash
gcloud run deploy jobhunter --source . --region asia-south1 --allow-unauthenticated \
  --set-env-vars DATABASE_URL=...,OPENAI_API_KEY=...
```
Better: store secrets in Secret Manager. Create a second Cloud Run **Job** from the same image with command
`python run_pipeline.py` and trigger it with Cloud Scheduler. Set a billing budget alert.

## Honest limitations
- Gmail auto-tracking is not built yet (only the email classifier). Update statuses in the Tracker page.
- Auto-submit is intentionally not implemented: career sites use CAPTCHAs and bot detection, and wrong answers hurt you.
- Live scraping (Greenhouse/Lever) could not be tested from the build sandbox, so it is unit-tested with mocked responses. If a company slug returns 404 you'll see a warning, and the other companies still run.
- Login is a simple email and password. For public use, switch to Supabase Auth or Google OAuth.
