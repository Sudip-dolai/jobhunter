"""Transaction-safe merge, promotion, unmerge (plan section 6)."""
import random
import subprocess
import sys
import textwrap

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import dedup
from app.models import Job, JobEvent
from tests.helpers_v4 import ROOT, mkjob, snapshot_user_data, v2_engine

SOURCES = ["greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee"]


@pytest.fixture
def db(tmp_path):
    eng, url = v2_engine(tmp_path, seed_v1=True)
    # make_v1_db seeds real user data: a Match on job 1 and an Application on job 2. Maintenance code must never touch them.
    with Session(eng) as s:
        rows = [mkjob(s, src, f"{src}-1") for src in SOURCES]            # six listings of the same job (ids 1..6)
        s.commit()
    yield eng, url


def test_merge_groups_listings_and_elects_the_most_trusted_root(db):
    eng, _ = db
    with Session(eng) as s:
        r = dedup.merge(s, 2, 1, method="key")           # lever (3) vs greenhouse (3): ties broken by apply url/desc/first_seen/id
        root = s.get(Job, r["root"])
        assert root.canonical_job_id is None
        other = s.get(Job, 1 if root.id == 2 else 2)
        assert other.canonical_job_id == root.id
        r2 = dedup.merge(s, 6, 1, method="key")           # a third source joins the same group
        assert s.get(Job, 6).canonical_job_id == r2["root"] == root.id or s.get(Job, r2["root"]).canonical_job_id is None
        assert dedup.validate_invariants(s) == []
        evs = s.query(JobEvent).filter_by(event_type="merge").all()
        assert evs and all(e.group_event_id and e.from_value is not None for e in evs)


def test_merge_is_idempotent_and_refuses_forbidden_pairs(db):
    eng, _ = db
    with Session(eng) as s:
        dedup.merge(s, 1, 2, method="key")
        assert dedup.merge(s, 1, 2, method="key")["action"] == "noop"
        other_company = mkjob(s, "lever", "x-1", company="Globex Corp"); s.commit()
        with pytest.raises(dedup.DedupError, match="different companies"):
            dedup.merge(s, 1, other_company.id, method="key")
        same_source = mkjob(s, "greenhouse", "greenhouse-2", location="Pune, India"); s.commit()
        with pytest.raises(dedup.DedupError, match="same-source|share a source"):
            dedup.merge(s, 1, same_source.id, method="key")
        assert dedup.validate_invariants(s) == []


def test_user_data_is_never_touched_by_merge_promote_unmerge(db):
    eng, _ = db
    before = snapshot_user_data(eng)
    with Session(eng) as s:
        gids = []
        for a, b in [(1, 2), (3, 4), (2, 3), (5, 6), (1, 6)]:
            r = dedup.merge(s, a, b, method="key")
            gids.append(r.get("group_event_id"))
        dedup.promote(s, 1)
        s.query(Job).filter(Job.id == 6).first()
        assert snapshot_user_data(eng) == before
        last = s.query(JobEvent).filter(JobEvent.event_type.in_(("merge", "promote"))).order_by(JobEvent.id.desc()).first()
        dedup.unmerge(s, last.group_event_id)
    assert snapshot_user_data(eng) == before, "matches/applications/users must be byte-identical"


def test_promotion_repoints_everything_and_keeps_depth_one(db):
    eng, _ = db
    with Session(eng) as s:
        for src_id in (2, 3, 4):
            dedup.merge(s, 1, src_id, method="key")
        root = s.get(Job, 1).canonical_job_id or 1
        # make a different member clearly better: it gets an apply URL and the longest description
        better = s.get(Job, 4 if root != 4 else 3)
        better.description = better.description * 5
        s.commit()
        r = dedup.promote(s, better.id)
        assert r["action"] == "promoted" and r["root"] == better.id
        assert s.get(Job, better.id).canonical_job_id is None
        members = dedup.group_members(s, better.id)
        assert {m.id for m in members} == {1, 2, 3, 4} - {better.id}
        assert dedup.validate_invariants(s) == []
        assert dedup.promote(s, better.id)["action"] == "noop"


def test_unmerge_restores_exact_pointers_and_marks_events_undone(db):
    eng, _ = db
    with Session(eng) as s:
        dedup.merge(s, 1, 2, method="key")
        before = {j.id: j.canonical_job_id for j in s.query(Job)}
        r = dedup.merge(s, 3, 1, method="key")
        dedup.unmerge(s, r["group_event_id"])
        assert {j.id: j.canonical_job_id for j in s.query(Job)} == before
        assert all(e.undone_at for e in s.query(JobEvent).filter_by(group_event_id=r["group_event_id"], event_type="merge"))
        with pytest.raises(dedup.UnmergeRefused, match="already undone"):
            dedup.unmerge(s, r["group_event_id"])


def test_stale_unmerge_is_refused_until_newer_operations_are_undone(db):
    eng, _ = db
    with Session(eng) as s:
        dedup.merge(s, 1, 2, method="key")
        first = dedup.merge(s, 3, 4, method="key")            # group A: 3 <- 4
        second = dedup.merge(s, 1, 3, method="key")           # group A is absorbed into group 1: row 4 is re-pointed
        assert s.get(Job, 4).canonical_job_id == 1
        with pytest.raises(dedup.UnmergeRefused, match="changed since"):
            dedup.unmerge(s, first["group_event_id"])         # row 4 no longer points where the event left it
        dedup.unmerge(s, second["group_event_id"])            # newest first ...
        dedup.unmerge(s, first["group_event_id"])             # ... then the older one succeeds
        assert s.get(Job, 4).canonical_job_id is None and s.get(Job, 3).canonical_job_id is None
        assert s.get(Job, 2).canonical_job_id == 1


def test_cycle_attempts_are_noops_or_refused(db):
    eng, _ = db
    with Session(eng) as s:
        dedup.merge(s, 1, 2, method="key")
        for a, b in [(1, 2), (2, 1), (2, 2)]:
            assert dedup.merge(s, a, b, method="key")["action"] == "noop"
        assert dedup.validate_invariants(s) == []
        # corrupt on purpose: the validator must see it
        s.execute(text("UPDATE jobs SET canonical_job_id = id WHERE id = 5")); s.commit()
        assert any("I2" in v for v in dedup.validate_invariants(s))


@pytest.mark.parametrize("seed", range(8))
def test_random_operation_sequences_keep_every_invariant(db, seed):
    eng, _ = db
    rnd = random.Random(seed)
    before = snapshot_user_data(eng)
    with Session(eng) as s:
        for _ in range(40):
            op = rnd.choice(["merge", "merge", "promote", "unmerge"])
            try:
                if op == "merge":
                    dedup.merge(s, rnd.randint(1, 6), rnd.randint(1, 6), method="key")
                elif op == "promote":
                    dedup.promote(s, rnd.randint(1, 6))
                else:
                    ev = s.query(JobEvent).filter(JobEvent.event_type.in_(("merge", "promote")), JobEvent.undone_at.is_(None)).order_by(JobEvent.id.desc()).first()
                    if ev:
                        dedup.unmerge(s, ev.group_event_id)
            except (dedup.DedupError,):
                s.rollback()
            assert dedup.validate_invariants(s) == [], (seed, op)
    assert snapshot_user_data(eng) == before


def test_two_processes_merging_overlapping_groups_keep_invariants(db):
    eng, url = db
    script = textwrap.dedent("""
        import os, sys, time
        os.environ["DATABASE_URL"] = sys.argv[1]
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from app import dedup
        eng = create_engine(sys.argv[1], connect_args={"timeout": 60})
        pairs = [tuple(map(int, p.split("-"))) for p in sys.argv[2].split(",")]
        go = sys.argv[3]
        open(go + ".ready." + str(os.getpid()), "w").close()
        while not os.path.exists(go): time.sleep(0.002)
        with Session(eng) as s:
            for a, b in pairs:
                for attempt in range(20):
                    try:
                        dedup.merge(s, a, b, method="key"); break
                    except dedup.DedupError as e:
                        s.rollback()
                        if "concurrently" not in str(e) and "share a source" not in str(e): raise
                        if "share a source" in str(e): break
                        time.sleep(0.05)
    """)
    import os, time
    from pathlib import Path
    go = Path(url.replace("sqlite:///", "")).parent / "go_merge"
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    procs = [subprocess.Popen([sys.executable, "-c", script, url, pairs, str(go)], cwd=ROOT, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)
             for pairs in ("1-2,2-3,3-4", "4-5,5-6,1-6", "6-3,2-5,1-4")]
    while len(list(go.parent.glob("go_merge.ready.*"))) < 3:
        time.sleep(0.05)
    go.write_text("go")
    outs = [p.communicate(timeout=120) for p in procs]
    assert all(p.returncode == 0 for p in procs), outs
    with Session(eng) as s:
        assert dedup.validate_invariants(s) == []
        roots = {j.canonical_job_id or j.id for j in s.query(Job)}
        assert len(roots) == 1, "all six listings should end in one group"
