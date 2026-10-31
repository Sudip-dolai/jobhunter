"""Shared helpers for the v2 tests: build a v1-schema database, spawn real processes, etc."""
import os
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def make_v1_db(path, jobs=3, users=2) -> str:
    """Create a SQLite file with the exact v1 schema and some rows (jobs, users, matches, applications)."""
    con = sqlite3.connect(path)
    con.executescript((FIXTURES / "v1_schema.sql").read_text())
    for u in range(1, users + 1):
        con.execute("INSERT INTO users (id,email,name,password_hash,created_at) VALUES (?,?,?,?,datetime('now'))",
                    (u, f"u{u}@x.com", f"U{u}", "h"))
    for j in range(1, jobs + 1):
        con.execute("INSERT INTO jobs (id,source,external_id,company,title,location,url,description,first_seen) "
                    "VALUES (?,?,?,?,?,?,?,?,datetime('now'))",
                    (j, "greenhouse", f"e{j}", "Acme Inc", f"Software Engineer {j}", "Bengaluru, India", f"https://x/{j}", "desc " * 20))
    con.execute("INSERT INTO matches (user_id,job_id,score,eligible,matched_skills,missing_skills,reason,created_at) "
                "VALUES (1,1,80,1,'[]','[]','ok',datetime('now'))")
    con.execute("INSERT INTO applications (user_id,job_id,status,notes,updated_at) VALUES (1,2,'Saved','n',datetime('now'))")
    con.commit()
    con.close()
    return f"sqlite:///{path}"


CHILD = textwrap.dedent("""
    import os, sys, time
    os.environ["DATABASE_URL"] = sys.argv[1]
    go = sys.argv[2]
    from app import db, migrations      # import first (slow), then wait for the start signal
    open(go + ".ready." + str(os.getpid()), "w").close()
    while not os.path.exists(go):
        time.sleep(0.002)
    try:
        db.init_db()
    except migrations.MigrationBusy as e:
        print("BUSY", e); sys.exit(3)
    except Exception as e:
        print("ERR", type(e).__name__, e); sys.exit(1)
""")


def spawn_init(url, go_file, env_extra=None):
    env = dict(os.environ, **(env_extra or {}))
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.Popen([sys.executable, "-c", CHILD, url, str(go_file)], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def run_concurrent_init(url, go_file, n=8, env_extra=None, timeout=90):
    procs = [spawn_init(url, go_file, env_extra) for _ in range(n)]
    deadline = time.time() + 60
    while len(list(Path(go_file).parent.glob(Path(go_file).name + ".ready.*"))) < n and time.time() < deadline:
        time.sleep(0.05)                  # every child has finished importing and is waiting at the barrier
    Path(go_file).write_text("go")
    outs = []
    for p in procs:
        out, _ = p.communicate(timeout=timeout)
        outs.append((p.returncode, out))
    return outs


# ---------------------------------------------------------------- v2 database harness
def v2_engine(tmp_path, name="v2.db", seed_v1=False):
    from sqlalchemy import create_engine
    from app import migrations
    url = make_v1_db(tmp_path / name, jobs=0, users=2) if seed_v1 else f"sqlite:///{tmp_path / name}"
    eng = create_engine(url, connect_args={"timeout": 30})
    migrations.ensure_schema(eng)
    return eng, url


_RES = {}


def mkjob(s, source, ext, company="Acme Inc", title="Software Engineer", location="Bengaluru, India", desc=None,
          remote=False, country="India", **kw):
    """Insert a fully normalized v2 job (what the v2 ingest would write)."""
    from app import normalize
    from app.companies import CompanyResolver
    from app.models import Job
    desc = desc if desc is not None else ("Build scalable services with Python and Go. " * 12)
    j = dict(source=source, external_id=ext, company=company, title=title, location=location, url=f"https://{source}.example/{ext}",
             description=desc, sector="", remote=remote, job_type="Permanent", country=country, salary_min=None, salary_max=None)
    extra = {k: kw.pop(k) for k in ("posted_at", "valid_through", "apply_url") if k in kw}
    j.update(kw)
    legacy = {k: j[k] for k in ("source", "external_id", "company", "title", "location", "url", "description", "sector",
                                "remote", "job_type", "country", "salary_min", "salary_max")}
    d = normalize.derived_fields({**j, **extra}, company_id=CompanyResolver(s).resolve(company))
    row = Job(**legacy, **d)
    s.add(row)
    s.flush()
    return row


def snapshot_user_data(eng):
    from sqlalchemy import text
    out = {}
    with eng.connect() as c:
        for t in ("users", "matches", "applications", "contacts", "email_events"):
            out[t] = [tuple(r) for r in c.execute(text(f"SELECT * FROM {t} ORDER BY id")).fetchall()]
        c.rollback()
    return out
