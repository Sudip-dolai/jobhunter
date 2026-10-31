"""Run the full cycle: scrape -> match -> alert -> Gmail sync -> reminders.

  python run_pipeline.py                    one run (use with cron / Cloud Scheduler)
  python run_pipeline.py --loop --every 30  keeps running by itself, one cycle every 30 minutes
  python run_pipeline.py --demo             offline test with sample data
"""
import argparse
import logging
import time

from app import config, demo, discover, pipeline
from app.scrapers import load_companies
from app.db import SessionLocal, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("jobhunter")


def auto_discover() -> None:
    """First start only: if few companies are configured, find more automatically (a few minutes, needs internet)."""
    if (config.ROOT / "discovered.flag").exists() or len(load_companies()) >= 50:
        return
    log.info("Only %d companies configured. Finding more (one-time, 5-15 minutes)...", len(load_companies()))
    try:
        r = discover.run(str(config.ROOT / "companies_input.txt"))
        log.info("Discovery done: %d of %d companies can be monitored.", r["found"], r["total"])
    except Exception:
        log.exception("Discovery failed; continuing with the current company list")


def v2_summary() -> dict:
    """One-line health view of the v2 pipeline (only when it is swiatched on)."""
    from sqlalchemy import func, select
    from app.models import Job, SourceRun
    with SessionLocal() as s:
        by_status = dict(s.execute(select(func.coalesce(Job.status, "active"), func.count()).group_by(func.coalesce(Job.status, "active"))).all())
        runs = dict(s.execute(select(SourceRun.outcome, func.count()).group_by(SourceRun.outcome)).all())
        return {"jobs_by_status": by_status, "runs_by_outcome": runs, "expiry_mode": config.EXPIRY_MODE}


def run_once(demo_mode: bool = False) -> dict:
    with SessionLocal() as s:
        if demo_mode:
            demo.seed(s)
            return pipeline.run_all(s, companies=[])
        result = pipeline.run_all(s)
    if config.FEATURE_V2_INGEST:
        result["v2"] = v2_summary()
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="seed demo data and skip internet scraping")
    ap.add_argument("--loop", action="store_true", help="repeat forever")
    ap.add_argument("--every", type=float, default=30, help="minutes between runs in --loop mode")
    ap.add_argument("--max-runs", type=int, default=0, help="stop after N runs (0 = never; for testing)")
    args = ap.parse_args()
    init_db()
    if not args.demo:
        auto_discover()
    runs = 0
    while True:
        try:
            result = run_once(args.demo)
            log.info("Run finished: %s", result)
        except Exception:  # a crashed cycle must not kill the worker
            log.exception("Run failed; will retry next cycle")
        runs += 1
        if args.demo:
            print(f"Demo login -> {demo.DEMO_EMAIL} / {demo.DEMO_PASSWORD}")
        if not args.loop or (args.max_runs and runs >= args.max_runs):
            break
        time.sleep(args.every * 60)
