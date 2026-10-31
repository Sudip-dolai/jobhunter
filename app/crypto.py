"""Encrypt saved Gmail app passwords. Set SECRET_KEY in production."""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app import config


def _fernet() -> Fernet:
    if config.SECRET_KEY:
        key = base64.urlsafe_b64encode(hashlib.sha256(config.SECRET_KEY.encode()).digest())
    else:
        path = config.ROOT / ".secret_key"
        if not path.exists():
            path.write_bytes(Fernet.generate_key())
        key = path.read_bytes()
    return Fernet(key)


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise ValueError("Could not decrypt the saved Gmail password (SECRET_KEY changed?). Save it again in Profile.") from e
