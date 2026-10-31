"""Source registry: what each collector is, how far it can be trusted, and what was verified about it.

`completeness_status` decides whether a collector may ever report complete=True:
  verified    official docs read AND a proven terminal condition (only these can drive lifecycle changes)
  provisional contract looks usable but is not proven; the run records `shadow_complete` only
  unverified  docs/auth/terms unclear; never complete
  never       search windows or capped/partial collectors; never complete
Verification facts below come from the official pages read during the v2 review (see the plan, section 11).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceSpec:
    name: str
    kind: str                       # ats | employer_page | feed | demo
    trust: int                      # 3 ats, 2 employer page, 1 feed
    completeness_status: str        # verified | provisional | unverified | never
    official_docs_url: str = ""
    verified_on: str = ""
    auth_required: bool = False
    terms_verified: bool = False
    note: str = ""

    @property
    def may_be_complete(self) -> bool:
        return self.completeness_status == "verified"

    @property
    def expansion_allowed(self) -> bool:
        """Adding new companies/hosts needs: docs read, no third-party credential needed, terms read."""
        return bool(self.official_docs_url and self.verified_on and not self.auth_required and self.terms_verified)


REGISTRY: dict[str, SourceSpec] = {s.name: s for s in [
    SourceSpec("greenhouse", "ats", 3, "verified", "https://developers.greenhouse.io/job-board.html", "2026-10-21",
               note="List returns all job posts with meta.total; no pagination. Terms of use not read yet."),
    SourceSpec("ashby", "ats", 3, "verified", "https://developers.ashbyhq.com/docs/public-job-posting-api", "2026-10-21",
               note="'All currently published' postings in one response; no total (structural check + shrink guard). Terms not read."),
    SourceSpec("lever", "ats", 3, "provisional", "https://github.com/lever/postings-api", "2026-10-21",
               note="skip/limit documented; no default size, total or hasNext documented. Shadow check: explicit empty page. EU host unsupported."),
    SourceSpec("smartrecruiters", "ats", 3, "unverified", "https://developers.smartrecruiters.com/docs/posting-api", "2026-10-21",
               note="Docs describe API key/OAuth; examples are unauthenticated. Max page size unverified."),
    SourceSpec("workable", "ats", 3, "unverified", "https://help.workable.com/hc/en-us/articles/115012771647", "2026-10-21",
               note="Official API is authenticated (spi/v3); the public endpoint documented is on another host than the repo uses."),
    SourceSpec("recruitee", "ats", 3, "unverified", "https://support.recruitee.com/en/articles/1066282-api-documentation", "2026-10-21",
               auth_required=True, note="Careers Site API requires an employer-issued token; the repo sends none."),
    SourceSpec("workday", "ats", 3, "unverified", note="No official documentation found; /wday/cs/ vs /wday/cxs/ to verify on a real tenant."),
    SourceSpec("page", "employer_page", 2, "never", note="Career pages: capped, new-only; robots.txt checked per host."),
    SourceSpec("jsonld", "employer_page", 2, "never", note="schema.org JobPosting pages: capped, new-only."),
    SourceSpec("jooble", "feed", 1, "never", "https://jooble.org/api/about", "2026-10-21",
               note="Key by request for portals/search engines; site Terms prohibit crawlers; search window."),
    SourceSpec("adzuna", "feed", 1, "never", "https://developer.adzuna.com/docs/search", "2026-10-21",
               note="Search window; terms/attribution not read."),
    SourceSpec("demo", "demo", 1, "never"),
]}


def spec(source: str) -> SourceSpec:
    return REGISTRY.get(source) or SourceSpec(source, "ats", 1, "unverified", note="unknown source")


def trust_of(source: str) -> int:
    return spec(source).trust


def kind_of(source: str) -> str:
    return spec(source).kind


def feed_sources() -> set:
    return {n for n, s in REGISTRY.items() if s.kind == "feed"}


def expansion_check(source: str) -> tuple[bool, list[str]]:
    s = spec(source)
    reasons = []
    if not s.official_docs_url or not s.verified_on:
        reasons.append("official docs not recorded")
    if s.auth_required:
        reasons.append("authentication (employer-issued token) is required")
    if not s.terms_verified:
        reasons.append("terms of use not read")
    return (not reasons, reasons)
