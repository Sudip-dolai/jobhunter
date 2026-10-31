"""Canonical companies and aliases. Resolution is by exact normalized key (never substring), so "hcl" cannot match
an unrelated company that merely contains those letters."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Company, CompanyAlias
from app.normalize import clamp, company_key
from app.scrapers import BIG_EMPLOYERS


class CompanyResolver:
    """Cached resolver for one batch of work (one run, one backfill). Creates companies on first sight."""

    def __init__(self, session: Session):
        self.s = session
        self.by_alias: dict[str, int] = {}
        for cid, alias in session.execute(select(CompanyAlias.company_id, CompanyAlias.alias_norm)):
            self.by_alias[alias] = cid
        for cid, norm_ in session.execute(select(Company.id, Company.name_norm)):
            self.by_alias.setdefault(norm_, cid)

    def resolve(self, name: str, sector: str | None = None, create: bool = True) -> int | None:
        key = company_key(name)
        if not key:
            return None
        if key in self.by_alias:
            return self.by_alias[key]
        if not create:
            return None
        c = Company(name=clamp(name, 160), name_norm=key, sector=clamp(sector, 60) or None)
        self.s.add(c)
        self.s.flush()
        self.by_alias[key] = c.id
        return c.id

    def add_alias(self, company_id: int, alias: str, origin: str = "seed") -> None:
        key = company_key(alias)
        if not key or key in self.by_alias:
            return
        self.s.add(CompanyAlias(company_id=company_id, alias_norm=key, alias_raw=clamp(alias, 200), origin=origin))
        self.by_alias[key] = company_id


def seed_aliases(session: Session, company_names: list[str] | None = None) -> int:
    """Seed canonical companies from the BIG_EMPLOYERS alias lists (V: scrapers.py) and any configured names."""
    r = CompanyResolver(session)
    n = 0
    for canonical, aliases in BIG_EMPLOYERS:
        cid = r.resolve(canonical)
        for a in aliases:
            before = len(r.by_alias)
            r.add_alias(cid, a, "big_employers")
            n += len(r.by_alias) - before
    for name in company_names or []:
        r.resolve(name)
    session.flush()
    return n
