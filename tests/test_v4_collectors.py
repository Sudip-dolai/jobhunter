"""Completeness contracts (plan section 7): a 200 response is never enough; unknown stays False."""
import pytest
import requests

from app import collect, scrapers, sources


class FakeResp:
    def __init__(self, body, status=200, ctype="application/json; charset=utf-8"):
        self._body, self.status_code, self.headers = body, status, {"Content-Type": ctype}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            e = requests.HTTPError(f"{self.status_code}")
            e.response = self
            raise e


def gh(items, total="auto", **kw):
    meta = {"total": len(items) if total == "auto" else total}
    body = {"jobs": items, "meta": meta} if total is not None else {"jobs": items}
    return FakeResp(body, **kw)


def item(i, title="Software Engineer"):
    return {"id": i, "title": title, "location": {"name": "Bengaluru, India"}, "absolute_url": f"https://x/{i}", "content": "desc"}


GH = {"name": "Acme", "source": "greenhouse", "slug": "acme", "sector": "SaaS"}
AB = {"name": "Beta", "source": "ashby", "slug": "beta", "sector": "SaaS"}
LV = {"name": "Gamma", "source": "lever", "slug": "gamma", "sector": "SaaS"}


def patch_get(monkeypatch, *responses):
    seq = list(responses)
    monkeypatch.setattr(scrapers.requests, "get", lambda *a, **k: seq.pop(0) if len(seq) > 1 else seq[0])
    monkeypatch.setattr(collect.requests, "get", scrapers.requests.get)


def test_greenhouse_complete_only_when_meta_total_matches(monkeypatch):
    patch_get(monkeypatch, gh([item(1), item(2), item(3, "Sales Manager")]))
    r = collect.collect(GH)
    assert r.complete and r.listed_ids == {"1", "2", "3"} and r.expected_total == 3
    assert [j["external_id"] for j in r.jobs] == ["1", "2"], "software filter applies to jobs, never to listed_ids"


@pytest.mark.parametrize("resp", [
    gh([item(1), item(2)], total=5),                       # truncated: fewer jobs than meta.total
    gh([item(1), item(2)], total=None),                    # no meta.total at all
    gh([item(1), item(1)], total=2),                       # duplicate ids
    FakeResp({"jobs": "oops", "meta": {"total": 0}}),      # wrong type
    FakeResp({"error": "unavailable"}),                    # 200 with an error body
    FakeResp(ValueError("not json"), ctype="text/html"),   # HTML instead of JSON
    gh([item(1)], ctype="text/html"),                      # wrong content type
])
def test_greenhouse_200_is_not_automatically_complete(monkeypatch, resp):
    patch_get(monkeypatch, resp)
    assert collect.collect(GH).complete is False


def test_http_errors_become_outcomes_never_exceptions(monkeypatch):
    for code, outcome in [(404, "error"), (500, "error"), (403, "blocked"), (429, "blocked"), (401, "blocked")]:
        patch_get(monkeypatch, FakeResp({}, status=code))
        r = collect.collect(GH)
        assert (r.outcome, r.http_status, r.complete) == (outcome, code, False)


def test_network_error_and_robots_denial_are_failures(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(scrapers.requests, "get", boom)
    assert collect.collect(GH).outcome == "error"
    monkeypatch.setattr(scrapers, "_greenhouse_response", lambda slug: (_ for _ in ()).throw(PermissionError("robots")))
    assert collect.collect(GH).outcome == "robots_denied"


def test_ashby_structural_contract(monkeypatch):
    good = {"apiVersion": "1", "jobs": [{"id": "a1", "title": "Backend Engineer", "location": "Pune", "jobUrl": "u",
                                         "applyUrl": "https://apply/a1", "publishedAt": "2026-01-02T03:04:05.000+00:00",
                                         "descriptionPlain": "d", "isListed": True},
                                        {"id": "a2", "title": "Hidden Engineer", "isListed": False}]}
    patch_get(monkeypatch, FakeResp(good))
    r = collect.collect(AB)
    assert r.complete and r.listed_ids == {"a1"}, "unlisted jobs are not 'listed'"
    assert r.jobs[0]["apply_url"] == "https://apply/a1" and r.jobs[0]["posted_at"].startswith("2026-01-02")
    for bad in (FakeResp({"jobs": []}), FakeResp({"apiVersion": "1", "jobs": None}), FakeResp("nope")):
        patch_get(monkeypatch, bad)
        assert collect.collect(AB).complete is False


def test_lever_is_provisional_complete_stays_false_but_shadow_is_recorded(monkeypatch):
    raw = [{"id": "l1", "text": "Software Engineer", "hostedUrl": "u", "descriptionPlain": "d", "categories": {}}]
    patch_get(monkeypatch, FakeResp(raw), FakeResp([]))          # list, then the explicit empty page
    r = collect.collect(LV)
    assert r.complete is False and r.shadow_complete is True and r.listed_ids == {"l1"}
    patch_get(monkeypatch, FakeResp(raw), FakeResp([{"id": "more"}]))   # the list was truncated
    r = collect.collect(LV)
    assert r.complete is False and r.shadow_complete is False


@pytest.mark.parametrize("source", ["workday", "smartrecruiters", "workable", "recruitee", "page", "jsonld", "jooble", "adzuna"])
def test_unverified_or_partial_sources_are_never_complete(monkeypatch, source):
    monkeypatch.setattr(scrapers, "fetch_jobs", lambda c, known=None: [])
    r = collect.collect({"name": "X", "source": source, "slug": "s"})
    assert r.complete is False and r.listed_ids is None


def test_registry_has_the_last_word(monkeypatch):
    """Even if an adapter said complete, an unverified source can never end up complete."""
    monkeypatch.setitem(sources.REGISTRY, "greenhouse", sources.SourceSpec("greenhouse", "ats", 3, "unverified"))
    patch_get(monkeypatch, gh([item(1)]))
    assert collect.collect(GH).complete is False


def test_kill_switch_skips_collection(monkeypatch):
    monkeypatch.setattr(collect.config, "COLLECTION_ENABLED", False)
    r = collect.collect(GH)
    assert r.outcome == "skipped" and r.complete is False


def test_expansion_gate_blocks_unverified_sources():
    ok, why = sources.expansion_check("recruitee")
    assert not ok and any("authentication" in w for w in why)
    assert not sources.expansion_check("workday")[0]
    assert not sources.expansion_check("greenhouse")[0], "terms of use have not been read yet"
