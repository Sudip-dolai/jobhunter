"""Access rules (plan section 11): robots, rate limits, blocks, and no evasion of any kind."""
import re
import threading
import time
from pathlib import Path

import pytest

from app import collect, config, http, lifecycle, scrapers

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = [r"proxies\s*=", r"impersonate", r"curl_cffi", r"cloudscraper", r"undetected", r"stealth", r"captcha",
             r"fake_useragent", r"X-Forwarded-For", r"rotat\w*[ _-]*(proxy|proxies|user.?agent)", r"random\.choice\([^)]*agent",
             r"nkparam", r"verify\s*=\s*False"]


def source_files():
    files = list((ROOT / "app").glob("*.py")) + [ROOT / "streamlit_app.py", ROOT / "run_pipeline.py"]
    return [f for f in files if f.name != "http.py"]            # http.py only MENTIONS the rules in its docstring


def test_no_evasion_constructs_anywhere_in_the_application_code():
    hits = []
    for f in source_files():
        for n, line in enumerate(f.read_text().splitlines(), 1):
            for pat in FORBIDDEN:
                if re.search(pat, line, re.I):
                    hits.append(f"{f.name}:{n}: {line.strip()[:90]}")
    assert not hits, "forbidden construct(s) found:\n" + "\n".join(hits)


def test_http_module_itself_has_no_proxy_or_fingerprint_code():
    code = (ROOT / "app" / "http.py").read_text()
    body = code.split('"""', 2)[2]                              # everything after the docstring
    for pat in FORBIDDEN:
        assert not re.search(pat, body, re.I), pat


@pytest.fixture(autouse=True)
def clean_http():
    http.reset_state()
    yield
    http.reset_state()


class R:
    def __init__(self, status=200, headers=None):
        self.status_code, self.headers = status, headers or {}

    def close(self):
        pass


def test_polite_get_refuses_proxies_tls_tampering_and_credentials():
    for kw in ({"proxies": {"https": "http://x"}}, {"verify": False}, {"cert": "c.pem"}, {"auth": ("u", "p")}, {"cookies": {"a": "b"}}):
        with pytest.raises(TypeError, match="does not accept"):
            http.polite_get("https://example.com/a", **kw)


def test_the_user_agent_is_fixed_and_identifying_and_cannot_be_overridden(monkeypatch):
    seen = {}
    monkeypatch.setattr(http.requests, "get", lambda url, headers=None, **k: seen.update(h=headers) or R())
    monkeypatch.setattr(config, "BOT_CONTACT_URL", "https://me.example/bot")
    http.polite_get("https://example.com/a", delay=0, headers={"User-Agent": "Googlebot", "Accept": "text/html"})
    assert seen["h"]["User-Agent"] == "Mozilla/5.0 (compatible; JobHunterBot/1.0; +https://me.example/bot)"
    assert seen["h"]["Accept"] == "text/html"


def test_requests_to_one_host_are_spaced_and_other_hosts_are_independent(monkeypatch):
    sleeps = []
    monkeypatch.setattr(http.requests, "get", lambda url, **k: R())
    http.polite_get("https://a.example/1", delay=1.0, sleep=sleeps.append)
    http.polite_get("https://a.example/2", delay=1.0, sleep=sleeps.append)
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 1.0, "the second request to the same host waited"
    sleeps.clear()
    http.polite_get("https://b.example/1", delay=1.0, sleep=sleeps.append)
    assert sleeps == [], "a different host is not delayed"


def test_one_request_at_a_time_per_host(monkeypatch):
    active, peak = [0], [0]
    lock = threading.Lock()

    def slow_get(url, **k):
        with lock:
            active[0] += 1; peak[0] = max(peak[0], active[0])
        time.sleep(0.03)
        with lock:
            active[0] -= 1
        return R()
    monkeypatch.setattr(http.requests, "get", slow_get)
    ts = [threading.Thread(target=http.polite_get, args=("https://same.example/x",), kwargs={"delay": 0}) for _ in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert peak[0] == 1


@pytest.mark.parametrize("status", [401, 403, 429])
def test_blocked_responses_stop_all_further_contact_with_that_host(monkeypatch, status):
    calls = []
    monkeypatch.setattr(http.requests, "get", lambda url, **k: calls.append(url) or R(status, {"Retry-After": "120"}))
    with pytest.raises(http.BlockedError):
        http.polite_get("https://blocked.example/job/1", delay=0)
    assert http.is_blocked("https://blocked.example/other")
    with pytest.raises(http.BlockedError, match="back-off"):
        http.polite_get("https://blocked.example/job/2", delay=0)
    assert len(calls) == 1, "no second request is sent to a host that blocked us"
    assert not http.is_blocked("https://fine.example/")


def test_retry_after_is_honored_and_capped(monkeypatch):
    monkeypatch.setattr(http.requests, "get", lambda url, **k: R(429, {"Retry-After": "999999"}))
    with pytest.raises(http.BlockedError):
        http.polite_get("https://slow.example/a", delay=0)
    until = http._blocked_until["slow.example"] - time.monotonic()
    assert 3000 < until <= http.MAX_BACKOFF_S
    assert http.is_blocked("https://slow.example/b", now=time.monotonic() + 100)
    assert not http.is_blocked("https://slow.example/b", now=time.monotonic() + http.MAX_BACKOFF_S + 10)


def test_probe_never_requests_a_robots_disallowed_url(monkeypatch):
    monkeypatch.setattr(scrapers, "_robots_ok", lambda url: False)
    monkeypatch.setattr(http.requests, "get", lambda *a, **k: pytest.fail("a disallowed URL was requested"))
    assert lifecycle.default_probe("https://example.com/jobs/1") is None


def test_probe_treats_a_block_as_no_information_not_as_a_closed_job(monkeypatch):
    monkeypatch.setattr(scrapers, "_robots_ok", lambda url: True)
    monkeypatch.setattr(http.requests, "get", lambda *a, **k: R(403))
    assert lifecycle.default_probe("https://example.com/jobs/1") is None
    assert lifecycle.default_probe("https://example.com/jobs/2") is None      # and the host is now left alone


def test_blocked_collection_runs_are_failures_and_never_complete():
    import requests as rq
    resp = type("X", (), {"status_code": 429})()
    err = rq.HTTPError("429"); err.response = resp
    assert collect.classify_error(err) == ("blocked", 429)
    assert collect.classify_error(PermissionError("robots")) == ("robots_denied", None)


def test_global_kill_switch_stops_collection(monkeypatch):
    monkeypatch.setattr(config, "COLLECTION_ENABLED", False)
    monkeypatch.setattr(scrapers.requests, "get", lambda *a, **k: pytest.fail("collection ran although it is switched off"))
    r = collect.collect({"name": "Acme", "source": "greenhouse", "slug": "acme"})
    assert r.outcome == "skipped" and r.complete is False
