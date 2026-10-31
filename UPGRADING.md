# Upgrading to JobHunter v2

v2 is **additive**: new nullable columns, new tables, new modules. Nothing is dropped, renamed or rewritten, and with every
`FEATURE_V2_*` flag at its default the app behaves exactly like v1. All 39 original tests still pass unchanged.

## What is new
* **Canonical job data**: normalized company/title/location, original and normalized salary, posted date, apply URL, lifecycle status.
* **Collection with evidence** (`app/collect.py`, `app/sources.py`): every run is logged; a run is only *complete* when the source's
  documented contract proves it (Greenhouse: `meta.total` matches; Ashby: structural check). Unknown completeness is always *false*.
* **Duplicate grouping** (`app/dedup.py`): the same job on several sources is grouped, never merged away. Reversible and audited.
* **Lifecycle** (`app/lifecycle.py`): first missing *complete* run = **stale**; **expired** needs 3 misses **and** 48 hours, and
  every safeguard. A failed, blocked, partial or shrunken run never changes a job.
* **Global Search page** (`app/search.py`): keywords, filters, sorting, relevance ranking and stable pagination, separate from your personal matches.
* **Safe maintenance** (`python -m app.migrations ...`): verified backups, dry-run reports, audit log.

## Safe rollout order
1. **Back up your database yourself once** (copy the `.db` file, or `pg_dump`). Then deploy the new code. Nothing changes yet.
2. Start the app/worker once. The schema upgrade is automatic, locked and transactional (safe if web and worker start together).
   `python -m app.migrations status` shows the version.
3. `python -m app.migrations backup` creates a **restore-tested** backup (needs `pg_dump`/`pg_restore`/`createdb` on PostgreSQL).
4. `python -m app.migrations backfill` (dry-run report in `reports/`), then `... backfill --apply`.
   The Search page needs this: jobs without v2 columns cannot be keyword-searched (the page tells you how many).
5. Set `FEATURE_V2_INGEST=1`. New scrapes now fill the v2 columns and log every run. Leave `EXPIRY_MODE=dry_run`.
6. After a fresh backup: `python -m app.migrations dedup` (report), then `dedup --apply`. Set `FEATURE_V2_DEDUP=1` for ongoing grouping.
7. Set `FEATURE_V2_MATCHING=1` (alerts skip duplicates/expired/stale; links prefer the direct apply URL).
8. Let `dry_run` collect evidence for at least a week. Then `EXPIRY_MODE=auto` enforces **per source** once its gate passes
   (7 days, 20 complete runs, <=0.5% reappearance rate) **and** a verified backup exists. Otherwise it silently stays in dry-run.

Every bulk step refuses to run without a restore-tested backup less than 24 hours old, and apply requires a dry-run of the *current* state.

## Rollback (cheapest first)
1. **Flags off.** Restores v1 behaviour immediately; legacy columns were never touched.
2. **Undo dedup**: each merge has an event; `dedup.unmerge(session, group_event_id)` restores the exact pointers (newest first).
3. **Undo expiry**: set `EXPIRY_MODE=off`; restore rows from `job_events` (`status` events) if needed.
4. **Old code against the new database needs no DDL**: all new columns are nullable (covered by a test).
5. **Restore** from the verified backup in `backups/` (needs your confirmation: it overwrites current data).

## Collectors and terms
Only sources whose official documentation was read are treated as verifiable. Recruitee's official docs say its API needs an
employer-issued token, Workable's official API is authenticated, SmartRecruiters' auth is unclear, Workday has no official docs found:
see `app/sources.py`. The registry's `expansion_check()` blocks proposing new companies for such sources until their terms are read.
No proxies, no user-agent or fingerprint changes, no CAPTCHA handling; blocked hosts are left alone (`app/http.py`, enforced by a test).

## Tests
`pytest` runs everything (SQLite). PostgreSQL concurrency, search and backup tests run when `TEST_POSTGRES_URL` is set.
`JH_STRESS=25 pytest tests/test_v4_migrations_concurrency.py` repeats the start-together race 25 times.
