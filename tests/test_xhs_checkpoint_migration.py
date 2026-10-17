"""迁移 23：小红书 checkpoint 删除深层前沿两列（resume_page、resume_search_id），其余数据原样保留。"""

from __future__ import annotations

import sqlite3

from trippostcollect.db.bootstrap import XHS_RETIRED_CHECKPOINT_COLUMNS, ensure_xhs_control_schema
from trippostcollect.scheduler.discovery import query_fingerprint
from trippostcollect.xhs.discovery import xhs_query_fingerprint

TARGET = {"target_key": "qingdao_trip", "keyword": "青岛旅行"}
OLD_FINGERPRINT = query_fingerprint("xhs", "青岛旅行", {"target_key": "qingdao_trip"})


def _open_control_db(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _columns(conn: sqlite3.Connection) -> list[str]:
    return [row[1] for row in conn.execute("PRAGMA table_info(xhs_discovery_checkpoints)")]


def _versions(conn: sqlite3.Connection) -> set[int]:
    return {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}


def _pre_migration_database(path) -> None:
    """现行定义建库后加回两列、撤销 23 的登记，并写入旧指纹的 checkpoint 与 seen。"""
    conn = _open_control_db(path)
    ensure_xhs_control_schema(conn)
    conn.execute("ALTER TABLE xhs_discovery_checkpoints ADD COLUMN resume_page INTEGER NOT NULL DEFAULT 1")
    conn.execute("ALTER TABLE xhs_discovery_checkpoints ADD COLUMN resume_search_id TEXT")
    conn.execute("DELETE FROM schema_migrations WHERE version = 23")
    conn.execute("INSERT INTO xhs_accounts(account_id) VALUES ('account-a')")
    conn.execute(
        "INSERT INTO xhs_discovery_checkpoints(target_key, account_id, keyword, query_fingerprint, "
        "resume_page, resume_search_id, source_has_more, status, last_batch_complete, last_stop_reason, "
        "last_run_id, last_summary_path, campaign_candidate_count) "
        "VALUES ('qingdao_trip', 'account-a', '青岛旅行', ?, 7, 'old-search', 1, 'active', 1, "
        "'runtime_failed', 'run-1', '/runs/run-1/summary.json', 42)",
        (OLD_FINGERPRINT,),
    )
    conn.execute(
        "INSERT INTO xhs_discovery_seen_candidates(target_key, account_id, query_fingerprint, platform_post_id, "
        "first_run_id, last_run_id) VALUES ('qingdao_trip', 'account-a', ?, 'note-1', 'run-1', 'run-1')",
        (OLD_FINGERPRINT,),
    )
    conn.commit()
    conn.close()


def test_new_database_has_no_retired_columns_and_records_23(tmp_path):
    conn = _open_control_db(tmp_path / "fresh.sqlite")
    ensure_xhs_control_schema(conn)

    assert not set(XHS_RETIRED_CHECKPOINT_COLUMNS) & set(_columns(conn))
    assert 23 in _versions(conn)


def test_migration_drops_two_columns_and_keeps_rows_indexes_and_foreign_keys(tmp_path):
    path = tmp_path / "control.sqlite"
    _pre_migration_database(path)
    conn = _open_control_db(path)
    before = conn.execute(
        "SELECT id, target_key, account_id, keyword, query_fingerprint, source_has_more, status, "
        "last_batch_complete, last_stop_reason, last_run_id, last_summary_path, campaign_candidate_count, "
        "created_at, updated_at FROM xhs_discovery_checkpoints"
    ).fetchall()
    seen_before = conn.execute("SELECT * FROM xhs_discovery_seen_candidates").fetchall()

    ensure_xhs_control_schema(conn)

    columns = _columns(conn)
    assert not set(XHS_RETIRED_CHECKPOINT_COLUMNS) & set(columns)
    after = conn.execute(f"SELECT {', '.join(columns)} FROM xhs_discovery_checkpoints").fetchall()
    assert after == before
    assert conn.execute("SELECT * FROM xhs_discovery_seen_candidates").fetchall() == seen_before
    assert 23 in _versions(conn)
    indexes = {row[1] for row in conn.execute("PRAGMA index_list(xhs_discovery_checkpoints)")}
    assert "idx_xhs_discovery_checkpoints_target" in indexes
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    foreign = conn.execute("PRAGMA foreign_key_list(xhs_discovery_checkpoints)").fetchall()
    assert [(row[2], row[3], row[6]) for row in foreign] == [("xhs_accounts", "account_id", "CASCADE")]
    conn.execute("DELETE FROM xhs_accounts WHERE account_id = 'account-a'")
    assert conn.execute("SELECT COUNT(*) FROM xhs_discovery_checkpoints").fetchone() == (0,)


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "control.sqlite"
    _pre_migration_database(path)
    conn = _open_control_db(path)
    ensure_xhs_control_schema(conn)
    schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name = 'xhs_discovery_checkpoints' ORDER BY name"
    ).fetchall()
    rows = conn.execute("SELECT * FROM xhs_discovery_checkpoints").fetchall()

    ensure_xhs_control_schema(conn)

    assert conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name = 'xhs_discovery_checkpoints' ORDER BY name"
    ).fetchall() == schema
    assert conn.execute("SELECT * FROM xhs_discovery_checkpoints").fetchall() == rows
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 23").fetchone() == (1,)


def test_old_fingerprint_memory_is_kept_but_not_reused_by_the_full_scan_target(tmp_path):
    """目标排序与类型并入指纹：升级后首轮落在新记忆上，旧指纹的行原样保留供审计。"""
    path = tmp_path / "control.sqlite"
    _pre_migration_database(path)
    conn = _open_control_db(path)
    ensure_xhs_control_schema(conn)

    assert xhs_query_fingerprint(TARGET) != OLD_FINGERPRINT
    fingerprints = {row[0] for row in conn.execute("SELECT query_fingerprint FROM xhs_discovery_checkpoints")}
    assert fingerprints == {OLD_FINGERPRINT}
