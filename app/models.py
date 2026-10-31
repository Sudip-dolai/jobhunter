from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy import event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    profile: Mapped["Profile"] = relationship(back_populates="user", uselist=False, cascade="all, delete-orphan")


class Profile(Base):
    __tablename__ = "profiles"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    phone: Mapped[str] = mapped_column(String(32), default="")
    college: Mapped[str] = mapped_column(String(200), default="")
    grad_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    branch: Mapped[str] = mapped_column(String(80), default="CSE")
    cgpa: Mapped[float | None] = mapped_column(Float, nullable=True)
    skills: Mapped[list] = mapped_column(JSON, default=list)
    target_roles: Mapped[list] = mapped_column(JSON, default=list)
    watch_companies: Mapped[list] = mapped_column(JSON, default=list)
    min_score: Mapped[int] = mapped_column(Integer, default=60)
    min_lpa: Mapped[float | None] = mapped_column(Float, nullable=True, default=7.0)
    max_lpa: Mapped[float | None] = mapped_column(Float, nullable=True, default=60.0)
    require_salary: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    sectors: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    experience_years: Mapped[float | None] = mapped_column(Float, nullable=True, default=0.0)
    countries: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    remote_pref: Mapped[str | None] = mapped_column(String(30), nullable=True, default="Any")
    job_types: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    degree: Mapped[str | None] = mapped_column(String(80), nullable=True, default="B.Tech")
    linkedin_url: Mapped[str | None] = mapped_column(String(300), nullable=True, default="")
    github_url: Mapped[str | None] = mapped_column(String(300), nullable=True, default="")
    portfolio_url: Mapped[str | None] = mapped_column(String(300), nullable=True, default="")
    city: Mapped[str | None] = mapped_column(String(120), nullable=True, default="")
    tenth_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    twelfth_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    work_auth: Mapped[str | None] = mapped_column(String(200), nullable=True, default="")
    gmail_address: Mapped[str | None] = mapped_column(String(255), nullable=True, default="")
    gmail_app_password_enc: Mapped[str | None] = mapped_column(Text, nullable=True, default="")
    resume_text: Mapped[str] = mapped_column(Text, default="")
    resume_path: Mapped[str] = mapped_column(String(500), default="")
    user: Mapped["User"] = relationship(back_populates="profile")


class Job(Base):  # shared by all users: scraped once
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("source", "external_id"),
        Index("ix_jobs_dedup_key", "dedup_key"),
        Index("ix_jobs_company_id", "company_id"),
        Index("ix_jobs_company_norm", "company_norm"),
        Index("ix_jobs_canonical_job_id", "canonical_job_id"),
        Index("ix_jobs_search_default", "status", "canonical_job_id", "sort_date", "id"),
        Index("ix_jobs_source_last_seen", "source", "last_seen_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(30))
    external_id: Mapped[str] = mapped_column(String(100))
    company: Mapped[str] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(String(300))
    location: Mapped[str] = mapped_column(String(200), default="")
    url: Mapped[str] = mapped_column(String(600), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    sector: Mapped[str | None] = mapped_column(String(60), nullable=True, default="")
    remote: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    job_type: Mapped[str | None] = mapped_column(String(20), nullable=True, default="")
    country: Mapped[str | None] = mapped_column(String(160), nullable=True, default="")
    salary_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=now)
    # ---- v2 (all nullable: legacy code ignores them, legacy rows keep working) ----
    canonical_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)   # NULL = this row is canonical
    company_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    apply_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    company_norm: Mapped[str | None] = mapped_column(String(160), nullable=True)
    title_norm: Mapped[str | None] = mapped_column(String(300), nullable=True)
    city_norm: Mapped[str | None] = mapped_column(String(80), nullable=True)
    dedup_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(40), nullable=True)
    search_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(String(12), nullable=True)           # active | stale | expired (NULL = active)
    status_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    miss_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_missed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    posted_at_source: Mapped[str | None] = mapped_column(String(16), nullable=True)  # source | first_seen
    valid_through: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sort_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)      # COALESCE(posted_at, first_seen)
    salary_raw: Mapped[str | None] = mapped_column(String(300), nullable=True)
    salary_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    salary_period: Mapped[str | None] = mapped_column(String(8), nullable=True)
    salary_orig_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_orig_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_source: Mapped[str | None] = mapped_column(String(16), nullable=True)     # structured | parsed | legacy_lpa
    category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    seniority: Mapped[str | None] = mapped_column(String(16), nullable=True)


class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (UniqueConstraint("user_id", "job_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    score: Mapped[int] = mapped_column(Integer, default=0)
    eligible: Mapped[bool] = mapped_column(Boolean, default=True)
    matched_skills: Mapped[list] = mapped_column(JSON, default=list)
    missing_skills: Mapped[list] = mapped_column(JSON, default=list)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    job: Mapped["Job"] = relationship()


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (UniqueConstraint("user_id", "job_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"))
    status: Mapped[str] = mapped_column(String(30), default="Saved")
    notes: Mapped[str] = mapped_column(Text, default="")
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    follow_up_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    job: Mapped["Job"] = relationship()


class Contact(Base):  # your own referral contacts
    __tablename__ = "contacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    company: Mapped[str] = mapped_column(String(120), index=True)
    link: Mapped[str] = mapped_column(String(400), default="")
    notes: Mapped[str] = mapped_column(Text, default="")


class EmailEvent(Base):  # emails already used to update the tracker
    __tablename__ = "email_events"
    __table_args__ = (UniqueConstraint("user_id", "message_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    message_id: Mapped[str] = mapped_column(String(300))
    application_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="")
    subject: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# ---------------- JobHunter v2: companies, run log, audit ----------------
class Company(Base):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    name_norm: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    domain: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sector: Mapped[str | None] = mapped_column(String(60), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class CompanyAlias(Base):
    __tablename__ = "company_aliases"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(Integer, index=True)
    alias_norm: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    alias_raw: Mapped[str] = mapped_column(String(200), default="")
    origin: Mapped[str] = mapped_column(String(30), default="")


class SourceRun(Base):
    """One collection attempt for one company/feed. The evidence base for lifecycle decisions."""
    __tablename__ = "source_runs"
    __table_args__ = (Index("ix_source_runs_lookup", "source", "company_key", "started_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(30))
    company_key: Mapped[str] = mapped_column(String(160))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    outcome: Mapped[str] = mapped_column(String(16), default="ok")   # ok|error|blocked|robots_denied|skipped
    complete: Mapped[bool] = mapped_column(Boolean, default=False)
    suspect: Mapped[bool] = mapped_column(Boolean, default=False)
    shadow_complete: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    listed_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pages_fetched: Mapped[int | None] = mapped_column(Integer, nullable=True)
    new_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, default=0)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")


class JobEvent(Base):
    """Append-only audit trail for merges, promotions, unmerges and lifecycle changes."""
    __tablename__ = "job_events"
    __table_args__ = (Index("ix_job_events_job", "job_id", "created_at"), Index("ix_job_events_type", "event_type", "created_at"))
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(16))   # merge|promote|unmerge|status|reopen|suggest|would_expire|probe
    from_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str] = mapped_column(String(60), default="")
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    related_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    migration_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    group_event_id: Mapped[str | None] = mapped_column(String(40), nullable=True)  # ties the rows of one operation together
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"
    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    applied_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    note: Mapped[str] = mapped_column(String(200), default="")


class MigrationRun(Base):
    __tablename__ = "migration_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    step: Mapped[str] = mapped_column(String(40), index=True)
    mode: Mapped[str] = mapped_column(String(10), default="dry_run")   # dry_run | apply
    outcome: Mapped[str] = mapped_column(String(12), default="running")  # running|ok|failed|abandoned
    report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    report_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    inputs_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    backup_manifest_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    row_counts_before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    row_counts_after: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_checkpoint: Mapped[int | None] = mapped_column(Integer, nullable=True)


# ---- PostgreSQL strictness guard -------------------------------------------------------------------------
# SQLite ignores VARCHAR(n) limits and NUL characters; PostgreSQL (Neon) rejects both and aborts the whole batch.
# Scraped text (long titles, many-city locations, long URLs) must therefore be clipped before it is written.
def _clean_strings(mapper, connection, target) -> None:
    for attr in mapper.column_attrs:
        col = attr.columns[0]
        if not isinstance(col.type, String):
            continue
        value = getattr(target, attr.key, None)
        if not isinstance(value, str):
            continue
        fixed = value.replace("\x00", "")
        limit = getattr(col.type, "length", None)
        if limit and len(fixed) > limit:
            fixed = fixed[:limit]
        if fixed != value:
            setattr(target, attr.key, fixed)


event.listen(Base, "before_insert", _clean_strings, propagate=True)
event.listen(Base, "before_update", _clean_strings, propagate=True)
