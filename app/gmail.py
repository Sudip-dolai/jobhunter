"""Gmail tracking over IMAP (read-only). Each user saves their Gmail address and an app password in Profile.
Emails that mention a company you track are classified (OA / Interview / Offer / Rejected) and update the tracker."""
import email
import imaplib
import re
from datetime import timedelta
from email.header import decode_header, make_header

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import crypto, tracker
from app.models import Application, EmailEvent, User, now
from app.notifier import send_telegram

RANK = {"Saved": 0, "Applied": 1, "Referred": 1, "OA": 2, "Interview": 3, "Offer": 4}
OPEN_STATUSES = ["Saved", "Applied", "Referred", "OA", "Interview"]


def _decode(value) -> str:
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return str(value or "")


def _text_body(msg) -> str:
    plain, html_part = "", ""
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if ctype == "text/plain":
            plain += text
        else:
            html_part += re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", plain or html_part).strip()


def fetch_recent(address: str, password: str, days: int = 30, limit: int = 200,
                 client_factory=lambda: imaplib.IMAP4_SSL("imap.gmail.com")) -> list[dict]:
    imap = client_factory()
    try:
        imap.login(address, password)
        imap.select("INBOX", readonly=True)  # read-only: nothing is marked read, moved or deleted
        since = (now() - timedelta(days=days)).strftime("%d-%b-%Y")
        _, data = imap.search(None, "SINCE", since)
        out = []
        for num in data[0].split()[-limit:]:
            _, parts = imap.fetch(num, "(BODY.PEEK[])")
            msg = email.message_from_bytes(parts[0][1])
            out.append(dict(message_id=(msg["Message-ID"] or f"uid-{num.decode()}").strip(),
                            sender=_decode(msg["From"]), subject=_decode(msg["Subject"]),
                            body=_text_body(msg)[:3000]))
        return out
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _company_in(company: str, text: str) -> bool:
    return bool(re.search(r"(?<![a-z0-9])" + re.escape(company.lower()) + r"(?![a-z0-9])", text))


def sync_user(session: Session, user: User, fetch=fetch_recent, notify=send_telegram) -> int:
    """Returns how many applications changed status."""
    p = user.profile
    if not (p and p.gmail_address and p.gmail_app_password_enc):
        return 0
    emails = fetch(p.gmail_address, crypto.decrypt(p.gmail_app_password_enc))
    apps = list(session.scalars(select(Application).where(Application.user_id == user.id, Application.status.in_(OPEN_STATUSES))))
    done = set(session.scalars(select(EmailEvent.message_id).where(EmailEvent.user_id == user.id)))
    changed = 0
    for em in emails:
        if em["message_id"] in done:
            continue
        status = tracker.classify_email(em["subject"], em["body"])
        if not status:
            continue
        hay = f"{em['sender']} {em['subject']} {em['body'][:1500]}".lower()
        matches = [a for a in apps if _company_in(a.job.company, hay)]
        if not matches:
            continue
        app = max(matches, key=lambda a: len(a.job.company))
        better = status == "Rejected" and app.status not in ("Offer", "Rejected") or RANK.get(status, -1) > RANK.get(app.status, 0)
        session.add(EmailEvent(user_id=user.id, message_id=em["message_id"][:300], application_id=app.id, status=status, subject=em["subject"][:300]))
        done.add(em["message_id"])
        if better:
            tracker.set_status(session, app, status, (app.notes + "\n" if app.notes else "") + f"[auto] {em['subject'][:120]}")
            notify(user.telegram_chat_id, f"Tracker updated: {app.job.company} - {app.job.title} is now {status}.")
            changed += 1
    session.commit()
    return changed
