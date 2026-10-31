"""Lifecycle (plan section 8): stale = first miss in a complete run; expired = 3 misses AND >= 48h AND all safeguards."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import backup, config, lifecycle, pipeline
from app.collect import CollectResult
from app.models import Job, JobEvent, SourceRun
from tests.helpers_v4 import v2_engine

T0 = datetime(2026, 11, 1, 9, 0, 0)


class Env:
    def __init__(self, eng, monkeypatch):
        self.eng, self.mp, self.clock = eng, monkeypatch, [T0]
        monkeypatch.setattr(pipeline, "_now", lambda: self.clock[0])
        monkeypatch.setattr(lifecycle, "_now", lambda: self.clock[0])
        monkeypatch.setattr(config, "FEATURE_V2_INGEST", True)
        monkeypatch.setattr(lifecycle, "_backup_gate_cached", lambda engine: None)     # tests opt in to the real gate

    def mode(self, m):
        self.mp.setattr(config, "EXPIRY_MODE", m)

    def advance(self, **kw):
        self.clock[0] += timedelta(**kw)

    def run(self, ids, *, source="greenhouse", company="Acme", complete=True, outcome="ok", listed=None, error=""):
        jobs = [dict(source=source, company=company, external_id=i, title="Software Engineer", location="Pune, India",
                     url=f"https://x/{i}", description="d", sector="", remote=False, job_type="Permanent", country="India",
                     salary_min=None, salary_max=None) for i in ids]
        res = CollectResult(jobs=jobs, listed_ids=set(ids if listed is None else listed), complete=complete, outcome=outcome, error=error)
        with Session(self.eng) as s:
            out = pipeline.scrape_all(s, companies=[{"name": company, "source": source, "slug": "s"}], fetcher=lambda c, k: res)
        return out

    def job(self, ext, source="greenhouse"):
        with Session(self.eng) as s:
            j = s.scalar(select(Job).where(Job.source == source, Job.external_id == ext))
            s.expunge(j)
            return j

    def events(self, etype=None):
        with Session(self.eng) as s:
            q = select(JobEvent)
            if etype:
                q = q.where(JobEvent.event_type == etype)
            return list(s.scalars(q.order_by(JobEvent.id)))


@pytest.fixture
def env(tmp_path, monkeypatch):
    eng, _ = v2_engine(tmp_path)
    return Env(eng, monkeypatch)


def warm_up(env, ids=("a", "b"), runs=3):
    """A few complete runs so the company has enough history (SG5)."""
    for _ in range(runs):
        env.run(list(ids)); env.advance(minutes=30)


def test_enforce_timeline_first_miss_is_stale_expiry_needs_three_misses_and_48_hours(env):
    env.mode("enforce")
    warm_up(env)
    env.run(["b"])                                      # a is missing for the FIRST time
    a = env.job("a")
    assert (a.status, a.miss_count, a.first_missed_at) == ("stale", 1, env.clock[0])
    t_first = env.clock[0]
    env.advance(minutes=30); env.run(["b"])             # miss 2
    env.advance(minutes=30); env.run(["b"], outcome="error", error="boom")   # a failed run changes nothing
    assert env.job("a").miss_count == 2
    env.advance(minutes=30); env.run(["b"])             # miss 3, but only ~1.5h since the first miss
    a = env.job("a")
    assert a.miss_count == 3 and a.status == "stale", "three misses inside 48 hours must stay stale"
    env.clock[0] = t_first + timedelta(hours=48, minutes=1); env.run(["b"])
    a = env.job("a")
    assert a.status == "expired" and a.closed_at == env.clock[0] and a.status_reason == "missing_from_board"
    assert env.job("b").status == "active"
    assert [e.to_value for e in env.events("status")] == ["stale", "expired"]
    with Session(env.eng) as s:
        assert s.scalar(select(Job).where(Job.external_id == "a")) is not None, "rows are never deleted"


def test_dry_run_never_changes_status_but_records_what_would_happen(env):
    env.mode("dry_run")
    warm_up(env)
    t0 = env.clock[0]
    for i in range(4):
        env.run(["b"]); env.advance(hours=17)
    a = env.job("a")
    assert a.status == "active" and a.miss_count >= 3 and a.first_missed_at is not None
    would = env.events("would_expire")
    assert len(would) == 1 and would[0].reason == "greenhouse:missing_from_board"
    assert not env.events("status")


def test_expiry_off_changes_nothing(env):
    env.mode("off")
    warm_up(env)
    env.run(["b"])
    a = env.job("a")
    assert a.status == "active" and (a.miss_count or 0) == 0


@pytest.mark.parametrize("outcome,complete", [("error", False), ("blocked", False), ("robots_denied", False), ("skipped", False)])
def test_failed_blocked_or_robots_runs_never_change_anything(env, outcome, complete):
    env.mode("enforce")
    warm_up(env)
    env.run(["b"], outcome=outcome, complete=complete)
    a = env.job("a")
    assert a.status == "active" and (a.miss_count or 0) == 0


def test_partial_runs_neither_count_misses_nor_reset_them(env):
    env.mode("enforce")
    warm_up(env)
    env.run(["b"])                                          # miss 1 (complete)
    env.advance(minutes=30); env.run(["b"], complete=False)  # partial: no miss
    assert env.job("a").miss_count == 1 and env.job("a").status == "stale"
    env.advance(minutes=30); env.run(["a", "b"], complete=False)  # partial but a WAS listed: it is back
    a = env.job("a")
    assert a.status == "active" and a.miss_count == 0 and a.first_missed_at is None


def test_reappearance_reopens_stale_and_expired_jobs_with_an_event(env):
    env.mode("enforce")
    warm_up(env)
    env.run(["b"])
    env.advance(hours=49); env.run(["b"]); env.advance(minutes=1)
    env.run(["b"]); env.advance(minutes=1); env.run(["b"])
    assert env.job("a").status == "expired"
    env.advance(minutes=30); env.run(["a", "b"])
    a = env.job("a")
    assert a.status == "active" and a.miss_count == 0 and a.closed_at is None
    assert [e.from_value for e in env.events("reopen")] == ["expired"]


def test_shrink_guard_marks_the_run_suspect_and_counts_no_misses(env):
    env.mode("enforce")
    ids = [f"j{i}" for i in range(12)]
    warm_up(env, ids)
    env.run(["j0", "j1", "j2", "j3"])                         # 4 listed vs 12 before: below 50%
    with Session(env.eng) as s:
        last = s.scalars(select(SourceRun).order_by(SourceRun.id.desc())).first()
        assert last.suspect is True and last.complete is False
    assert env.job("j11").status == "active" and (env.job("j11").miss_count or 0) == 0


def test_mass_expiry_guard_holds_while_the_board_is_unstable_and_releases_when_it_settles(env):
    env.mode("enforce")
    ids = [f"j{i}" for i in range(20)]
    warm_up(env, ids)
    env.mp.setattr(config, "EXPIRY_SHRINK_RATIO", 0.0)          # disable SG3 so SG4 is what is under test
    wobble = [["k1"], ["k1", "k2", "k3", "k4", "k5"]]           # listed counts 1,5,1,5,...: never within 5%
    for i in range(6):
        env.run(wobble[i % 2]); env.advance(hours=12)
    assert all(env.job(f"j{i}").status != "expired" for i in range(20)), "SG4: an unstable board must not mass-expire"
    for _ in range(4):                                          # now the board settles at the same size
        env.run(["k1"]); env.advance(hours=12)
    assert all(env.job(f"j{i}").status == "expired" for i in range(20)), "once stable for 3 runs the guard releases"


def test_not_enough_history_means_no_expiry(env):
    env.mode("enforce")
    env.mp.setattr(config, "EXPIRY_MIN_HISTORY_RUNS", 8)         # inert at the default 3 (three misses already need 3 runs)
    env.run(["a", "b"])
    for _ in range(4):
        env.advance(hours=20); env.run(["b"])
    assert env.job("a").status == "stale", "SG5: not enough complete runs yet"
    for _ in range(4):
        env.advance(hours=20); env.run(["b"])
    assert env.job("a").status == "expired"


def test_per_run_cap_limits_expirations(env):
    env.mode("enforce")
    env.mp.setattr(config, "EXPIRY_MAX_PER_RUN", 2)
    env.mp.setattr(config, "EXPIRY_MASS_GUARD_PCT", 1.0)
    env.mp.setattr(config, "EXPIRY_SHRINK_RATIO", 0.0)
    ids = [f"j{i}" for i in range(10)]
    warm_up(env, ids + ["keep"])
    env.run(["keep"]); env.advance(hours=24); env.run(["keep"]); env.advance(hours=25); env.run(["keep"])
    with Session(env.eng) as s:
        assert s.query(Job).filter(Job.status == "expired").count() == 2


def test_feeds_are_touched_but_never_expired(env):
    env.mode("enforce")
    for _ in range(4):
        env.run(["f1", "f2"], source="adzuna", company="Adzuna IN: software engineer", complete=True)
        env.advance(hours=30)
    env.run(["f1"], source="adzuna", company="Adzuna IN: software engineer", complete=True)
    for _ in range(4):
        env.advance(hours=30); env.run(["f1"], source="adzuna", company="Adzuna IN: software engineer", complete=True)
    f2, f1 = env.job("f2", "adzuna"), env.job("f1", "adzuna")
    assert f2.status != "expired", "search windows can never prove a job is closed"
    assert f1.last_seen_at == env.clock[0]


def test_enforcement_is_blocked_without_a_verified_backup(env, tmp_path):
    env.mp.setattr(lifecycle, "_backup_gate_cached", lifecycle._backup_gate_cached.__wrapped__ if hasattr(lifecycle._backup_gate_cached, "__wrapped__") else
                   (lambda engine: backup.require_verified_backup(engine, "expiry_enforce", tmp_path / "nobackups")))
    env.mode("enforce")
    warm_up(env)
    env.run(["b"])
    a = env.job("a")
    assert a.status == "active", "no verified backup: behave like dry_run"
    with Session(env.eng) as s:
        mode, why = lifecycle.effective_mode(s, "greenhouse")
    assert mode == "dry_run" and "blocked" in why


def test_auto_mode_enforces_only_after_the_gate_passes(env):
    env.mode("auto")
    with Session(env.eng) as s:
        ok, rep = lifecycle.expiry_gate(s, "greenhouse", now=T0)
        assert not ok and rep["checks"][">= 20 complete runs"] is False
    for _ in range(21):
        env.run(["a", "b"]); env.advance(hours=9)             # 21 runs over ~8 days
    with Session(env.eng) as s:
        ok, rep = lifecycle.expiry_gate(s, "greenhouse", now=env.clock[0])
        assert ok, rep
        assert lifecycle.effective_mode(s, "greenhouse")[0] == "enforce"
        assert lifecycle.expiry_gate(s, "lever", now=env.clock[0])[0] is False   # other sources are judged separately


def test_would_expire_false_positives_feed_the_gate(env):
    env.mode("dry_run")
    warm_up(env)
    for _ in range(4):
        env.run(["b"]); env.advance(hours=17)
    assert len(env.events("would_expire")) == 1
    env.run(["a", "b"])                                       # it came back: the dry-run prediction was wrong
    assert env.events("would_expire")[0].undone_at is not None
    with Session(env.eng) as s:
        _, rep = lifecycle.expiry_gate(s, "greenhouse", now=env.clock[0])
        assert rep["would_expire_events"] == 1 and rep["reappeared"] == 1 and rep["reappearance_rate"] == 1.0


# ------------------------------------------------------------------ verification pass for partial sources
def _seed_partial(env, source="page"):
    env.run(["p1"], source=source, company="Site", complete=False)


def test_probe_404_counts_one_miss_each_and_expires_only_after_three_and_48h(env):
    env.mode("enforce")
    _seed_partial(env)
    codes = iter([404, 404, 404, 404])
    for hours in (0, 13, 26, 49):
        env.clock[0] = T0 + timedelta(hours=hours)
        with Session(env.eng) as s:
            out = lifecycle.verification_pass(s, probe=lambda url: next(codes), sleep=lambda x: None, now=env.clock[0])
            s.commit()
    j = env.job("p1", "page")
    assert j.miss_count == 4 and j.status == "expired" and j.status_reason == "http_404"


def test_probe_other_statuses_change_nothing_and_200_resets(env):
    env.mode("enforce")
    _seed_partial(env)
    for i, code in enumerate([403, 429, 500, None, 302]):
        with Session(env.eng) as s:
            lifecycle.verification_pass(s, probe=lambda url, c=code: c, sleep=lambda x: None, now=T0 + timedelta(hours=13 * (i + 1)))
            s.commit()
    j = env.job("p1", "page")
    assert j.status in (None, "active") and (j.miss_count or 0) == 0
    with Session(env.eng) as s:
        lifecycle.verification_pass(s, probe=lambda url: 404, sleep=lambda x: None, now=T0 + timedelta(hours=100)); s.commit()
    assert env.job("p1", "page").status == "stale"
    with Session(env.eng) as s:
        lifecycle.verification_pass(s, probe=lambda url: 200, sleep=lambda x: None, now=T0 + timedelta(hours=120)); s.commit()
    j = env.job("p1", "page")
    assert j.status == "active" and j.miss_count == 0


def test_probes_are_rate_limited_per_job_and_capped_per_run(env):
    env.mode("dry_run")
    for i in range(5):
        env.run([f"p{i}"], source="page", company="Site", complete=False)
    calls = []
    with Session(env.eng) as s:
        out = lifecycle.verification_pass(s, probe=lambda u: calls.append(u) or 200, max_probes=3, sleep=lambda x: None, now=T0)
        s.commit()
        assert out["probed"] == 3
        out2 = lifecycle.verification_pass(s, probe=lambda u: calls.append(u) or 200, max_probes=10, sleep=lambda x: None, now=T0 + timedelta(hours=1))
        s.commit()
    assert out2["probed"] == 2, "jobs probed less than 12 hours ago are skipped"


def test_default_probe_respects_robots_and_sends_no_evasion(monkeypatch):
    from app import scrapers
    monkeypatch.setattr(scrapers, "_robots_ok", lambda url: False)
    called = []
    monkeypatch.setattr("requests.get", lambda *a, **k: called.append(1))
    assert lifecycle.default_probe("https://example.com/jobs/1") is None and not called
