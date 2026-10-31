import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent

_url = os.getenv("DATABASE_URL") or f"sqlite:///{ROOT / 'jobhunter.db'}"
if _url.startswith("postgres://"):
    _url = _url.replace("postgres://", "postgresql://", 1)
DATABASE_URL = _url

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "resumes")
RESUME_DIR = Path(os.getenv("RESUME_DIR", ROOT / "resumes"))
JOOBLE_API_KEY = os.getenv("JOOBLE_API_KEY", "")
JOOBLE_LOCATIONS = [x.strip() for x in os.getenv("JOOBLE_LOCATIONS", "India").split(",") if x.strip()] or ["India"]
FEED_INTERVAL_HOURS = float(os.getenv("FEED_INTERVAL_HOURS", "6"))   # search feeds run at most this often, to save API calls
ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY", "")
ADZUNA_COUNTRIES = [c.strip().lower() for c in os.getenv("ADZUNA_COUNTRIES", "in").split(",") if c.strip()]
MAX_JOBS_PER_USER_PER_RUN = int(os.getenv("MAX_JOBS_PER_USER_PER_RUN", "100"))
SCRAPE_WORKERS = max(1, int(os.getenv("SCRAPE_WORKERS", "16")))   # companies fetched in parallel (network bound, so more = faster)
SECRET_KEY = os.getenv("SECRET_KEY", "")
ALLOW_SIGNUP = os.getenv("ALLOW_SIGNUP", "1").strip().lower() in {"1", "true", "yes", "on"}   # set 0 on a public server once your own account exists


# ---------------- JobHunter v2 ----------------
def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


# Feature flags: with all of them off the app behaves exactly like v1 (new columns stay NULL).
FEATURE_V2_INGEST = _flag("FEATURE_V2_INGEST")        # normalized upsert, run log, completeness tracking
FEATURE_V2_DEDUP = _flag("FEATURE_V2_DEDUP")          # cross-source duplicate grouping after each scrape
FEATURE_V2_MATCHING = _flag("FEATURE_V2_MATCHING")    # match_user skips duplicates / expired jobs
FEATURE_V2_SEARCH = _flag("FEATURE_V2_SEARCH", "1")   # the global Search page (read-only)
EXPIRY_MODE = os.getenv("EXPIRY_MODE", "dry_run").strip().lower()   # off | dry_run | auto | enforce
ENABLE_FEEDS = _flag("ENABLE_FEEDS", "1")
COLLECTION_ENABLED = _flag("COLLECTION_ENABLED", "1")  # global kill switch for all collection
FEED_MAX_AGE_DAYS = int(os.getenv("FEED_MAX_AGE_DAYS", "30"))
SEARCH_PAGE_SIZE = int(os.getenv("SEARCH_PAGE_SIZE", "20"))
SEARCH_CAND_MAX = int(os.getenv("SEARCH_CAND_MAX", "1000"))

# Lifecycle (stale after the 1st miss in a complete run; expired needs N misses AND this many hours)
EXPIRY_MISS_THRESHOLD = int(os.getenv("EXPIRY_MISS_THRESHOLD", "3"))
EXPIRY_MIN_HOURS = float(os.getenv("EXPIRY_MIN_HOURS", "48"))
EXPIRY_MASS_GUARD_PCT = float(os.getenv("EXPIRY_MASS_GUARD_PCT", "0.40"))
EXPIRY_SHRINK_RATIO = float(os.getenv("EXPIRY_SHRINK_RATIO", "0.50"))
EXPIRY_MIN_HISTORY_RUNS = int(os.getenv("EXPIRY_MIN_HISTORY_RUNS", "3"))
EXPIRY_MAX_PER_RUN = int(os.getenv("EXPIRY_MAX_PER_RUN", "200"))

# Duplicate detection thresholds
DEDUP_TITLE_SIM = float(os.getenv("DEDUP_TITLE_SIM", "0.93"))
DEDUP_DESC_SIM = float(os.getenv("DEDUP_DESC_SIM", "0.85"))
DEDUP_MIN_DESC_CHARS = int(os.getenv("DEDUP_MIN_DESC_CHARS", "300"))

# Backups required before bulk steps
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", ROOT / "backups"))
REPORT_DIR = Path(os.getenv("REPORT_DIR", ROOT / "reports"))
BACKUP_MAX_AGE_HOURS = float(os.getenv("BACKUP_MAX_AGE_HOURS", "24"))
MIGRATION_LOCK_TIMEOUT_S = int(os.getenv("MIGRATION_LOCK_TIMEOUT_S", "30"))

INGEST_SCOPE = os.getenv("INGEST_SCOPE", "software").strip().lower()   # software (v1 behavior) | all (multi-industry)
BOT_CONTACT_URL = os.getenv("BOT_CONTACT_URL", "")            # shown in the bot's user agent so site owners can reach you
HTTP_MIN_DELAY_S = float(os.getenv("HTTP_MIN_DELAY_S", "1.0"))
VERIFY_MAX_PROBES = int(os.getenv("VERIFY_MAX_PROBES", "25"))   # URL checks per worker cycle for partial sources (each waits HTTP_MIN_DELAY_S per host)
