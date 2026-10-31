from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
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
    __table_args__ = (UniqueConstraint("source", "external_id"),)
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
