from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Application, now

STATUSES = ["Saved", "Applied", "Referred", "OA", "Interview", "Offer", "Rejected"]
FOLLOW_UP_DAYS = 7


def add_application(session: Session, user_id: int, job_id: int, status: str = "Saved") -> Application:
    app = session.scalar(select(Application).where(Application.user_id == user_id, Application.job_id == job_id))
    if app:
        return app
    app = Application(user_id=user_id, job_id=job_id)
    session.add(app)
    session.flush()
    set_status(session, app, status)
    return app


def set_status(session: Session, app: Application, status: str, notes: str | None = None) -> None:
    if status not in STATUSES:
        raise ValueError(f"Unknown status: {status}")
    app.status = status
    app.updated_at = now()
    if notes is not None:
        app.notes = notes
    if status == "Applied":
        app.applied_at = app.applied_at or now()
        app.follow_up_at = now() + timedelta(days=FOLLOW_UP_DAYS)
    elif status != "Saved":
        app.follow_up_at = None
    session.commit()


def due_followups(session: Session, user_id: int | None = None) -> list[Application]:
    q = select(Application).where(Application.status == "Applied", Application.follow_up_at <= now())
    if user_id:
        q = q.where(Application.user_id == user_id)
    return list(session.scalars(q))


def bump_followup(session: Session, app: Application) -> None:
    app.follow_up_at = now() + timedelta(days=FOLLOW_UP_DAYS)
    session.commit()


def stats(session: Session, user_id: int) -> dict[str, int]:
    rows = session.execute(
        select(Application.status, func.count()).where(Application.user_id == user_id).group_by(Application.status)
    ).all()
    return {s: dict(rows).get(s, 0) for s in STATUSES}


def classify_email(subject: str, body: str = "") -> str | None:
    """Guess a status from an email (used when you plug in Gmail later)."""
    t = f"{subject} {body}".lower()
    rules = [
        ("Offer", ["offer letter", "pleased to offer", "we are happy to offer"]),
        ("Rejected", ["unfortunately", "not moving forward", "regret to inform", "other candidates"]),
        ("Interview", ["interview", "schedule a call", "technical round"]),
        ("OA", ["online assessment", "coding test", "hackerrank", "codility", "assessment link"]),
    ]
    for status, words in rules:
        if any(w in t for w in words):
            return status
    return None
