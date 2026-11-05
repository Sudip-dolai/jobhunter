# JobHunter — Personal Job Search Engine

Finds software jobs straight from company career pages, filters them by **your** eligibility (country, remote rules, experience, batch, CGPA, salary), scores them against your profile, alerts you on Telegram and tracks every application.
No paid job API is needed.

**Live demo:** https://jobhunter-wx8z.onrender.com

## What it does
**Watch → Match → Apply (prepare) → Track → Remind**

- **Company discovery:** a bulk harvester reads Common Crawl's public URL index for Greenhouse, Lever, Ashby, Workable, SmartRecruiters and Recruitee boards, then verifies every slug against the real API. Only working boards are added, nothing is guessed.
- **Scrapers for 7 ATS types:** Greenhouse, Lever, Ashby, Workday, Workable, SmartRecruiters, Recruitee, plus a JSON-LD (schema.org JobPosting) reader for custom career pages. Every job is normalized to one common structure.
- **Polite scraping:** obeys `robots.txt`, per-host rate limiting, concurrent workers, one failing company never stops the run.
- **Eligibility rules:** a job that restricts to another country is dropped; jobs that allow your country or are worldwide-remote are kept. Startup filter, experience, graduation batch, CGPA and minimum salary are applied per user.
- **Matching:** free rule filter first, then a 0-100 keyword/skill score. Optional OpenAI (`gpt-4o-mini`) refines the score. Without an API key it falls back to the keyword matcher.
- **Search page and de-duplication:** global search, cross-source duplicate grouping, and evidence-based "possibly closed" detection.
- **Tracker:** application status board. Optional **Gmail tracking** (read-only IMAP, encrypted app password) moves applications to OA / Interview / Offer / Rejected, never backwards.
- **Telegram alerts:** new high-score matches and 7-day follow-up reminders.
- **Autofill (Playwright):** fills the real application form from your profile and resume. **It never submits**: you review and click Submit.
- **Multi-user:** every table has `user_id`, passwords are hashed, and "Delete my data" is on the Account page.

## Tech stack
| Layer | Technology |
|---|---|
| UI | Streamlit |
| Backend | Python 3.10+, SQLAlchemy 2 ORM |
| Database | PostgreSQL on **Neon** (SQLite when `DATABASE_URL` is empty) |
| Scraping | requests, ThreadPoolExecutor, Playwright (optional) |
| Alerts | Telegram Bot API |
| AI (optional) | OpenAI `gpt-4o-mini` |
| Deploy | Docker on Render (free web service); GitHub Actions for scheduled runs |

## Quick start (local, about 2 minutes, no API keys)
```bash
python -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements.txt
python -m pytest -q                 # tests should pass
python run_pipeline.py --demo       # seeds demo data and scores it offline
streamlit run streamlit_app.py      # login: demo@example.com / demo1234
```

## First-time flow
1. Create an account and fill in **Profile** (skills, country, batch, CGPA, resume).
2. **Run now → Find and add companies** (one-time, 5 to 15 minutes), or run `python -m app.harvest` for thousands of companies.
3. **Run now → Find new jobs now**, or `python run_pipeline.py --loop --every 30`.
4. Open **Matches**, review jobs, use **Autofill application**, then track in **Tracker**.

## Bulk company discovery
```bash
python -m app.harvest                                  # all sources
python -m app.harvest --sources greenhouse lever --max-pages 5
python -m app.harvest --slugs my_slugs.txt             # lines: "greenhouse figma"
```
You can also paste any careers-page link in **Run now → Add career page links** or in `companies_input.txt` (`Name | link`).

## Configuration (`.env`)
Never commit `.env`. Everything except `DATABASE_URL` and `SECRET_KEY` is optional.

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | `postgresql+psycopg2://USER:PASSWORD@HOST/neondb?sslmode=require` (Neon direct host, not `-pooler`). Empty = local SQLite |
| `SECRET_KEY` | Encrypts stored Gmail app passwords. Use a long random string |
| `TELEGRAM_BOT_TOKEN` | Bot from @BotFather. Each user adds their chat id in Profile |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | Optional LLM scoring and draft answers |
| `ALLOW_SIGNUP` | `0` closes registration (single-user public deployment) |
| `FEATURE_V2_SEARCH` | `1` enables the Search page |
| `SCRAPE_WORKERS`, `HTTP_MIN_DELAY_S` | Scraper speed and politeness |
| `JOOBLE_API_KEY`, `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | Optional aggregator feeds for big employers with custom portals |

## Deploy on Render (free)
1. Push the repo to GitHub.
2. Render → **New → Blueprint** → pick the repo (uses `render.yaml`).
3. Enter `DATABASE_URL`, `SECRET_KEY` and `TELEGRAM_BOT_TOKEN` in the dashboard.
4. After creating your own account, set `ALLOW_SIGNUP=0`.

Free Render services sleep after 15 minutes without visitors. For automatic scraping use the scheduled GitHub Actions workflow (`.github/workflows/pipeline.yml`), or a paid Render worker (`MODE=worker`). Use one, not both. See `DEPLOY.md`.

## Maintenance CLI
`python -m app.migrations status|upgrade|backup|backfill|dedup|validate` — see `UPGRADING.md`.

## Limitations
- Auto-submit is intentionally not implemented: career sites use CAPTCHAs and bot detection, and wrong answers hurt you.
- Sites with no public API or structured data, or that need login/CAPTCHA, cannot be scraped. Check **Run now → Jobs per company** for companies with 0 jobs.
- Most postings do not list a salary; unknown-salary jobs are kept by default.
- Free Render instances sleep, so the first page load can take about a minute.
