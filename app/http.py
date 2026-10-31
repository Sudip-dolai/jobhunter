"""Polite HTTP for anything that touches third-party hosts outside their documented JSON APIs (URL verification probes,
the Lever shadow probe). Rules, enforced here and by tests/test_v4_compliance.py:

  * one fixed, identifying user agent (JobHunterBot, optional contact URL) - no rotation, no spoofing
  * no proxy support, no TLS/browser fingerprint impersonation, no CAPTCHA handling, no request-signing copied from a site
  * per-host concurrency of 1 and a minimum delay between requests (default 1 second, or the host's Crawl-delay)
  * 401 / 403 / 429 mean 'blocked': remember it, honor Retry-After, and do NOT touch that host again until it is over
"""
from __future__ import annotations

import threading
import time
from urllib.parse import urlsplit

import requests

from app import config

DEFAULT_DELAY = float(getattr(config, "HTTP_MIN_DELAY_S", 1.0))
MAX_BACKOFF_S = 3600
DEFAULT_BLOCK_S = 600


class BlockedError(RuntimeError):
    """The host refused us (401/403/429) or asked us to slow down. Treated as a failed run, never as 'job closed'."""


_state_lock = threading.Lock()
_host_locks: dict[str, threading.Lock] = {}
_last_request: dict[str, float] = {}
_blocked_until: dict[str, float] = {}


def user_agent() -> str:
    contact = getattr(config, "BOT_CONTACT_URL", "")
    return "Mozilla/5.0 (compatible; JobHunterBot/1.0" + (f"; +{contact}" if contact else "") + ")"


def _host(url: str) -> str:
    return urlsplit(url).netloc.lower()


def is_blocked(url: str, now: float | None = None) -> bool:
    return _blocked_until.get(_host(url), 0) > (now if now is not None else time.monotonic())


def reset_state() -> None:                      # for tests
    with _state_lock:
        _host_locks.clear(); _last_request.clear(); _blocked_until.clear()


def _retry_after_seconds(resp) -> float:
    raw = (resp.headers or {}).get("Retry-After", "")
    try:
        return min(MAX_BACKOFF_S, max(1.0, float(raw)))
    except (TypeError, ValueError):
        return DEFAULT_BLOCK_S


def polite_get(url: str, *, timeout: float = 15, delay: float | None = None, sleep=time.sleep, **kwargs):
    """GET with throttling and block memory. Returns the response. Raises BlockedError for blocked/limited hosts."""
    for forbidden in ("proxies", "cert", "verify", "auth", "cookies"):
        if forbidden in kwargs:
            raise TypeError(f"polite_get does not accept '{forbidden}' (no proxies, no TLS tampering, no credentials)")
    host = _host(url)
    if is_blocked(url):
        raise BlockedError(f"{host} is in back-off; not contacting it again yet")
    with _state_lock:
        lock = _host_locks.setdefault(host, threading.Lock())
    with lock:                                                  # one request at a time per host
        wait = (delay if delay is not None else DEFAULT_DELAY) - (time.monotonic() - _last_request.get(host, -1e9))
        if wait > 0:
            sleep(wait)
        headers = {"User-Agent": user_agent(), **kwargs.pop("headers", {})}
        headers["User-Agent"] = user_agent()                    # callers cannot override the identity
        try:
            resp = requests.get(url, headers=headers, timeout=timeout, **kwargs)
        finally:
            _last_request[host] = time.monotonic()
    if resp.status_code in (401, 403, 429):
        _blocked_until[host] = time.monotonic() + (_retry_after_seconds(resp) if resp.status_code == 429 else DEFAULT_BLOCK_S)
        raise BlockedError(f"{host} answered {resp.status_code}")
    return resp
