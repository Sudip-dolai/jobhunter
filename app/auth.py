import hashlib
import hmac
import os
import re

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import Application, Contact, EmailEvent, Match, Profile, User


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"{salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, _ = stored.split("$")
        return hmac.compare_digest(hash_password(password, bytes.fromhex(salt_hex)), stored)
    except ValueError:
        return False


def register(session: Session, email: str, name: str, password: str) -> User:
    email = email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("Enter a valid email address.")
    if len(password) < 6:
        raise ValueError("Password must be at least 6 characters.")
    if session.scalar(select(User).where(User.email == email)):
        raise ValueError("An account with this email already exists.")
    user = User(email=email, name=name.strip(), password_hash=hash_password(password))
    user.profile = Profile()
    session.add(user)
    session.commit()
    return user


def login(session: Session, email: str, password: str) -> User | None:
    user = session.scalar(select(User).where(User.email == email.strip().lower()))
    return user if user and verify_password(password, user.password_hash) else None


def delete_user(session: Session, user_id: int) -> None:
    """Delete-my-data: removes the account and everything tied to it."""
    for model in (Match, Application, Contact, EmailEvent):
        session.execute(delete(model).where(model.user_id == user_id))
    session.execute(delete(Profile).where(Profile.user_id == user_id))
    session.execute(delete(User).where(User.id == user_id))
    session.commit()
