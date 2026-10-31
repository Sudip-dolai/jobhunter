import json
import os
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app import backup, migrations
from tests.helpers_v4 import make_v1_db


@pytest.fixture
def eng(tmp_path):
    url = make_v1_db(tmp_path / "live.db")
    e = create_engine(url)
    migrations.ensure_schema(e)
    return e


def test_backup_is_restore_tested_and_gate_passes(eng, tmp_path):
    m = backup.create_verified_backup(eng, tmp_path / "bk")
    assert m["restore_test"]["ok"] and m["restore_test"]["integrity_ok"] and m["restore_test"]["counts_match"]
    assert m["table_counts"]["jobs"] == 3 and m["table_counts"]["matches"] == 1
    assert oct(os.stat(m["backup_path"]).st_mode)[-3:] == "600"
    assert backup.require_verified_backup(eng, "backfill", tmp_path / "bk")


def test_gate_refuses_without_a_manifest(eng, tmp_path):
    with pytest.raises(backup.BackupGateError, match="no backup manifest"):
        backup.require_verified_backup(eng, "dedup_apply", tmp_path / "none")


def test_gate_refuses_expired_manifest(eng, tmp_path):
    backup.create_verified_backup(eng, tmp_path / "bk")
    later = backup._utcnow() + timedelta(hours=backup.config.BACKUP_MAX_AGE_HOURS + 1)
    with pytest.raises(backup.BackupGateError, match="expired"):
        backup.require_verified_backup(eng, "backfill", tmp_path / "bk", now=later)


def test_gate_refuses_a_failed_restore_test(eng, tmp_path):
    m = backup.create_verified_backup(eng, tmp_path / "bk")
    p = Path(m["manifest_path"]); data = json.loads(p.read_text()); data["restore_test"]["ok"] = False
    p.write_text(json.dumps(data))
    with pytest.raises(backup.BackupGateError, match="not restore-tested"):
        backup.require_verified_backup(eng, "backfill", tmp_path / "bk")


def test_gate_refuses_a_tampered_or_missing_backup_file(eng, tmp_path):
    m = backup.create_verified_backup(eng, tmp_path / "bk")
    with open(m["backup_path"], "ab") as f:
        f.write(b"x")
    with pytest.raises(backup.BackupGateError, match="missing or was modified"):
        backup.require_verified_backup(eng, "backfill", tmp_path / "bk")


def test_gate_refuses_a_backup_of_a_different_database(eng, tmp_path):
    backup.create_verified_backup(eng, tmp_path / "bk")
    other = create_engine(make_v1_db(tmp_path / "other.db"))
    with pytest.raises(backup.BackupGateError, match="different database"):
        backup.require_verified_backup(other, "backfill", tmp_path / "bk")


def test_gate_refuses_when_protected_rows_disappeared(eng, tmp_path):
    backup.create_verified_backup(eng, tmp_path / "bk")
    with eng.begin() as c:
        c.execute(text("DELETE FROM applications"))
    with pytest.raises(backup.BackupGateError, match="fewer rows"):
        backup.require_verified_backup(eng, "backfill", tmp_path / "bk")


def test_gate_allows_growth_after_the_backup(eng, tmp_path):
    backup.create_verified_backup(eng, tmp_path / "bk")
    with eng.begin() as c:
        c.execute(text("INSERT INTO jobs (source,external_id,company,title,location,url,description,first_seen) VALUES ('x','new','C','T','','u','d',datetime('now'))"))
    assert backup.require_verified_backup(eng, "backfill", tmp_path / "bk")


def test_postgres_tools_missing_is_a_clean_blocker(monkeypatch, tmp_path):
    """Stop condition S3: no pg_dump means BackupUnavailable, never a silent skip."""
    class FakeURL:
        def get_backend_name(self): return "postgresql"
        host, port, database, username, password = "h", 5432, "d", "u", "p"
    class FakeEng:
        url = FakeURL()
        dialect = type("D", (), {"name": "postgresql"})()
    monkeypatch.setattr(backup, "table_counts", lambda e, t=None: {})
    monkeypatch.setattr(backup, "jobs_checksum", lambda e: "x")
    monkeypatch.setattr(backup.shutil, "which", lambda n: None)
    with pytest.raises(backup.BackupUnavailable, match="pg_dump"):
        backup.create_verified_backup(FakeEng(), tmp_path)
