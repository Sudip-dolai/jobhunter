"""Verified, restorable backups. Bulk steps (backfill, dedup apply, expiry enforcement) refuse to run without one.

A backup only counts when it was RESTORE-TESTED: the copy is opened/restored into a scratch target and its per-table
row counts and a jobs checksum are compared with the manifest. There is no override flag.

SQLite: standard-library online backup API.   PostgreSQL: pg_dump -Fc + pg_restore into a scratch database
(needs the PostgreSQL client tools on PATH and a DIRECT, non-pooled connection; otherwise BackupUnavailable).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from app import config

PROTECTED_TABLES = ["users", "profiles", "matches", "applications", "contacts", "email_events", "jobs"]
GATED_STEPS = {"backfill", "dedup_apply", "expiry_enforce"}


class BackupUnavailable(RuntimeError):
    """The tools or permissions needed to take a verified backup are missing (stop condition S3)."""


class BackupGateError(RuntimeError):
    """A bulk step was attempted without a valid, restore-tested, current backup."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def fingerprint(engine) -> str:
    u = engine.url
    raw = f"sqlite:{Path(u.database).resolve()}" if u.get_backend_name() == "sqlite" else f"{u.get_backend_name()}:{u.host}:{u.port}:{u.database}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def table_counts(engine, tables=None) -> dict:
    tables = tables or PROTECTED_TABLES
    out = {}
    with engine.connect() as c:
        insp = inspect(c)
        for t in tables:
            if insp.has_table(t):
                out[t] = int(c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() or 0)
        c.rollback()
    return out


def jobs_checksum(engine) -> str:
    h = hashlib.sha256()
    with engine.connect() as c:
        if not inspect(c).has_table("jobs"):
            return h.hexdigest()
        res = c.execution_options(stream_results=True).execute(
            text("SELECT id, source, external_id, company, title, url FROM jobs ORDER BY id"))
        for row in res:
            h.update("\x1f".join("" if v is None else str(v) for v in row).encode() + b"\n")
        c.rollback()
    return h.hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def manifest_hash(manifest: dict) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, default=str).encode()).hexdigest()


# ------------------------------------------------------------------ SQLite
def _backup_sqlite(engine, dest: Path) -> None:
    src_path = engine.url.database
    if not src_path or src_path == ":memory:":
        raise BackupUnavailable("an in-memory SQLite database cannot be backed up")
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)               # consistent online snapshot
    finally:
        dst.close(); src.close()


def _restore_test_sqlite(backup_path: Path, manifest: dict) -> dict:
    scratch = Path(tempfile.mkdtemp(prefix="jh_restore_")) / "restored.db"
    shutil.copy2(backup_path, scratch)
    res = {"performed_at": _utcnow().isoformat(), "scratch_target": str(scratch), "integrity_ok": False, "counts_match": False}
    con = sqlite3.connect(scratch)
    try:
        res["integrity_ok"] = con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()
    eng = create_engine(f"sqlite:///{scratch}")
    try:
        res["counts_match"] = table_counts(eng) == manifest["table_counts"] and jobs_checksum(eng) == manifest["jobs_checksum"]
    finally:
        eng.dispose()
        shutil.rmtree(scratch.parent, ignore_errors=True)
    return res


# ------------------------------------------------------------------ PostgreSQL (needs client tools; untested in the sandbox)
def _pg_env_url(engine):
    return engine.url.render_as_string(hide_password=False)


def _backup_postgres(engine, dest: Path) -> None:
    if not shutil.which("pg_dump"):
        raise BackupUnavailable("pg_dump is not on PATH; install the PostgreSQL client tools (stop condition S3)")
    r = subprocess.run(["pg_dump", "-Fc", "--no-owner", "-f", str(dest), _pg_env_url(engine)], capture_output=True, text=True)
    if r.returncode != 0:
        raise BackupUnavailable(f"pg_dump failed: {r.stderr.strip()[:300]}")


def _restore_test_postgres(engine, backup_path: Path, manifest: dict) -> dict:
    for tool in ("pg_restore", "createdb", "dropdb"):
        if not shutil.which(tool):
            raise BackupUnavailable(f"{tool} is not on PATH (stop condition S3)")
    u = engine.url
    scratch_db = f"jh_restore_{os.getpid()}_{int(_utcnow().timestamp())}"
    base = ["-h", str(u.host or "localhost"), "-p", str(u.port or 5432), "-U", str(u.username or "postgres")]
    env = dict(os.environ, PGPASSWORD=u.password or "")
    res = {"performed_at": _utcnow().isoformat(), "scratch_target": scratch_db, "integrity_ok": False, "counts_match": False}
    try:
        if subprocess.run(["pg_restore", "--list", str(backup_path)], capture_output=True, env=env).returncode != 0:
            return res
        if subprocess.run(["createdb", *base, scratch_db], capture_output=True, env=env).returncode != 0:
            raise BackupUnavailable("cannot create a scratch database for the restore test")
        r = subprocess.run(["pg_restore", *base, "--no-owner", "-d", scratch_db, str(backup_path)], capture_output=True, env=env)
        res["integrity_ok"] = r.returncode == 0
        eng = create_engine(u.set(database=scratch_db))
        try:
            res["counts_match"] = table_counts(eng) == manifest["table_counts"] and jobs_checksum(eng) == manifest["jobs_checksum"]
        finally:
            eng.dispose()
    finally:
        subprocess.run(["dropdb", *base, "--if-exists", scratch_db], capture_output=True, env=env)
    return res


# ------------------------------------------------------------------ public API
def create_verified_backup(engine, outdir: Path | None = None, max_age_hours: float | None = None) -> dict:
    """Take a backup, restore-test it, write a manifest. Returns the manifest (restore_test.ok tells the result)."""
    outdir = Path(outdir or config.BACKUP_DIR)
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = _utcnow().strftime("%Y%m%dT%H%M%S")
    dialect = engine.url.get_backend_name()
    dest = outdir / (f"jobhunter-{stamp}.sqlite3" if dialect == "sqlite" else f"jobhunter-{stamp}.dump")
    manifest = {"created_at": _utcnow().isoformat(), "dialect": dialect, "fingerprint": fingerprint(engine),
                "backup_path": str(dest), "table_counts": table_counts(engine), "jobs_checksum": jobs_checksum(engine),
                "expires_at": (_utcnow() + timedelta(hours=max_age_hours or config.BACKUP_MAX_AGE_HOURS)).isoformat()}
    if dialect == "sqlite":
        _backup_sqlite(engine, dest)
    elif dialect == "postgresql":
        _backup_postgres(engine, dest)
    else:
        raise BackupUnavailable(f"unsupported database dialect: {dialect}")
    os.chmod(dest, 0o600)
    manifest["backup_sha256"] = _sha256_file(dest)
    # the live database may have grown during the backup, so compare the copy with what the copy itself contains
    if dialect == "sqlite":
        copy_eng = create_engine(f"sqlite:///{dest}")
        manifest["table_counts"] = table_counts(copy_eng)
        manifest["jobs_checksum"] = jobs_checksum(copy_eng)
        copy_eng.dispose()
        rt = _restore_test_sqlite(dest, manifest)
    else:
        rt = _restore_test_postgres(engine, dest, manifest)
    rt["ok"] = bool(rt["integrity_ok"] and rt["counts_match"])
    manifest["restore_test"] = rt
    mpath = dest.with_suffix(dest.suffix + ".manifest.json")
    mpath.write_text(json.dumps(manifest, indent=2, default=str))
    os.chmod(mpath, 0o600)
    manifest["manifest_path"] = str(mpath)
    return manifest


def latest_manifest(backup_dir: Path | None = None) -> dict | None:
    d = Path(backup_dir or config.BACKUP_DIR)
    files = sorted(d.glob("*.manifest.json"), key=lambda p: p.stat().st_mtime) if d.exists() else []
    if not files:
        return None
    m = json.loads(files[-1].read_text())
    m["manifest_path"] = str(files[-1])
    return m


def require_verified_backup(engine, step: str, backup_dir: Path | None = None, now: datetime | None = None) -> str:
    """Gate for bulk steps. Returns the manifest hash to record in migration_runs; raises BackupGateError otherwise."""
    m = latest_manifest(backup_dir)
    now = now or _utcnow()
    if m is None:
        raise BackupGateError(f"{step}: no backup manifest found - create a verified backup first (python -m app.migrations backup)")
    if not m.get("restore_test", {}).get("ok"):
        raise BackupGateError(f"{step}: the latest backup was not restore-tested successfully")
    if datetime.fromisoformat(m["expires_at"]) < now:
        raise BackupGateError(f"{step}: the latest backup has expired ({m['expires_at']}); take a fresh one")
    if m.get("fingerprint") != fingerprint(engine):
        raise BackupGateError(f"{step}: the backup belongs to a different database")
    path = Path(m["backup_path"])
    if not path.exists() or _sha256_file(path) != m.get("backup_sha256"):
        raise BackupGateError(f"{step}: the backup file is missing or was modified")
    live = table_counts(engine)
    for t, n in m["table_counts"].items():
        if live.get(t, 0) < n:
            raise BackupGateError(f"{step}: table {t} has fewer rows now ({live.get(t, 0)}) than in the backup ({n}); "
                                  "these steps never delete rows, so investigate before continuing")
    return manifest_hash({k: v for k, v in m.items() if k != "manifest_path"})
