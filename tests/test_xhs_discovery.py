from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from trippostcollect.db.bootstrap import bootstrap_connection
from trippostcollect.xhs.discovery import (
    commit_child_discovery,
    load_checkpoint,
    resolve_discovery_plan,
    save_checkpoint,
    update_campaign,
    xhs_query_fingerprint,
)


def target(**overrides: object) -> dict[str, object]:
    return {
        "target_key": "qingdao_travel",
        "keyword": "青岛旅游",
        "top_refresh_max_pages": 3,
        **overrides,
    }


def prepare_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    bootstrap_connection(conn, sync_content=False, sync_jobs=False)
    for account_id in ("xhs-a01", "xhs-a02"):
        conn.execute(
            """
            INSERT INTO xhs_accounts(
                account_id, status, profile_dir, encrypted_state_path
            ) VALUES (?, 'active', ?, ?)
            """,
            (account_id, f"/profiles/{account_id}", f"/states/{account_id}.enc"),
        )
    conn.commit()
    return conn


def test_query_fingerprint_tracks_source_but_not_refresh_budget() -> None:
    base = target()
    changed_budget = target(top_refresh_max_pages=5)
    changed_source = target(target_key="qingdao_food")

    assert xhs_query_fingerprint(base) == xhs_query_fingerprint(changed_budget)
    assert xhs_query_fingerprint(base) != xhs_query_fingerprint(changed_source)


def test_checkpoint_isolated_per_account_and_resolves_campaign(tmp_path: Path) -> None:
    conn = prepare_connection(tmp_path / "xhs.sqlite")
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps({"records": []}), encoding="utf-8")
    fingerprint = xhs_query_fingerprint(target())
    save_checkpoint(
        conn,
        target_key="qingdao_travel",
        account_id="xhs-a01",
        keyword="青岛旅游",
        query_fingerprint_value=fingerprint,
        resume_page=6,
        resume_search_id="search-a01",
        source_has_more=True,
        last_batch_complete=True,
        last_stop_reason="runtime_failed",
        last_run_id="run-1",
    )
    update_campaign(
        conn,
        target_key="qingdao_travel",
        account_id="xhs-a01",
        query_fingerprint_value=fingerprint,
        summary_path=str(summary_path),
        candidate_count=31,
    )
    conn.commit()

    first = resolve_discovery_plan(conn, target=target(), account_id="xhs-a01")
    second = resolve_discovery_plan(conn, target=target(), account_id="xhs-a02")
    conn.close()

    assert first["checkpoint_found"] is True
    assert first["resume_page"] == 6
    assert first["resume_search_id"] == "search-a01"
    assert first["top_refresh_max_pages"] == 3
    assert first["campaign_summary_path"] == str(summary_path.resolve())
    assert second["checkpoint_found"] is False
    assert second["resume_page"] == 1
    assert second["top_refresh_max_pages"] == 0


def test_missing_campaign_summary_blocks_resume(tmp_path: Path) -> None:
    conn = prepare_connection(tmp_path / "missing.sqlite")
    fingerprint = xhs_query_fingerprint(target())
    save_checkpoint(
        conn,
        target_key="qingdao_travel",
        account_id="xhs-a01",
        keyword="青岛旅游",
        query_fingerprint_value=fingerprint,
        resume_page=2,
        resume_search_id="search-id",
        source_has_more=True,
        last_batch_complete=True,
        last_stop_reason="continue",
        last_run_id="run-1",
    )
    update_campaign(
        conn,
        target_key="qingdao_travel",
        account_id="xhs-a01",
        query_fingerprint_value=fingerprint,
        summary_path=str(tmp_path / "gone.json"),
        candidate_count=10,
    )
    conn.commit()

    with pytest.raises(RuntimeError, match="campaign summary is missing"):
        resolve_discovery_plan(conn, target=target(), account_id="xhs-a01")
    conn.close()


def test_refresh_commit_preserves_exhausted_frontier(tmp_path: Path) -> None:
    conn = prepare_connection(tmp_path / "refresh.sqlite")
    fingerprint = xhs_query_fingerprint(target())
    save_checkpoint(
        conn,
        target_key="qingdao_travel",
        account_id="xhs-a01",
        keyword="青岛旅游",
        query_fingerprint_value=fingerprint,
        resume_page=9,
        resume_search_id="deep-search-id",
        source_has_more=False,
        last_batch_complete=True,
        last_stop_reason="source_exhausted",
        last_run_id="run-1",
    )
    conn.commit()
    plan = resolve_discovery_plan(conn, target=target(), account_id="xhs-a01")
    child_path = tmp_path / "child-summary.json"
    child_path.write_text("{}", encoding="utf-8")

    result = commit_child_discovery(
        conn,
        target=target(),
        account_id="xhs-a01",
        run_id="run-2",
        discovery_plan=plan,
        child_summary_path=child_path,
        child_summary={
            "pagination_evidence": {
                "stop_event": {
                    "source_page": 2,
                    "resume_page": 3,
                    "resume_cursor": "refresh-search-id",
                    "source_has_more": True,
                    "batch_complete": True,
                    "discovery_phase": "refresh",
                    "stop_reason": "continue",
                    "candidate_identities": ["top-note", "video-note"],
                }
            },
            "formal_validation": {"candidate_count": 4},
            "import_result": {"skipped": True, "reason": "source_not_exhausted"},
            "import_completion_met": False,
        },
    )
    checkpoint = load_checkpoint(
        conn,
        target_key="qingdao_travel",
        account_id="xhs-a01",
        query_fingerprint_value=fingerprint,
    )
    conn.close()

    assert result["refresh_only"] is True
    assert result["resume_page"] == 9
    assert result["resume_search_id"] == "deep-search-id"
    assert checkpoint is not None
    assert checkpoint["status"] == "exhausted"
    assert checkpoint["last_summary_path"] == str(child_path.resolve())
    with sqlite3.connect(tmp_path / "refresh.sqlite") as seen:
        seen_count = seen.execute(
            "SELECT COUNT(*) FROM xhs_discovery_seen_candidates"
        ).fetchone()[0]
    assert seen_count == 2


def test_frontier_commit_advances_and_clears_completed_campaign(tmp_path: Path) -> None:
    conn = prepare_connection(tmp_path / "frontier.sqlite")
    plan = resolve_discovery_plan(conn, target=target(), account_id="xhs-a01")
    child_path = tmp_path / "completed-summary.json"
    child_path.write_text("{}", encoding="utf-8")

    result = commit_child_discovery(
        conn,
        target=target(),
        account_id="xhs-a01",
        run_id="run-complete",
        discovery_plan=plan,
        child_summary_path=child_path,
        child_summary={
            "pagination_evidence": {
                "stop_event": {
                    "source_page": 4,
                    "resume_page": 5,
                    "resume_cursor": "stable-search-id",
                    "source_has_more": False,
                    "batch_complete": True,
                    "discovery_phase": "frontier",
                    "stop_reason": "source_exhausted",
                }
            },
            "formal_validation": {"candidate_count": 20},
            "import_result": {"inserted_rows": 20},
            "import_completion_met": True,
        },
    )
    fingerprint = xhs_query_fingerprint(target())
    checkpoint = load_checkpoint(
        conn,
        target_key="qingdao_travel",
        account_id="xhs-a01",
        query_fingerprint_value=fingerprint,
    )
    conn.close()

    assert result["resume_page"] == 5
    assert result["imported_completion"] is True
    assert checkpoint is not None
    assert checkpoint["resume_search_id"] == "stable-search-id"
    assert checkpoint["last_summary_path"] is None
    assert checkpoint["campaign_candidate_count"] == 0


def test_image_persistence_failure_preserves_campaign_summary(tmp_path: Path) -> None:
    conn = prepare_connection(tmp_path / "image-failure.sqlite")
    plan = resolve_discovery_plan(conn, target=target(), account_id="xhs-a01")
    child_path = tmp_path / "image-failure-summary.json"
    child_path.write_text("{}", encoding="utf-8")

    result = commit_child_discovery(
        conn,
        target=target(),
        account_id="xhs-a01",
        run_id="run-image-failure",
        discovery_plan=plan,
        child_summary_path=child_path,
        child_summary={
            "pagination_evidence": {
                "stop_event": {
                    "source_page": 4,
                    "resume_page": 5,
                    "resume_cursor": "stable-search-id",
                    "source_has_more": False,
                    "batch_complete": True,
                    "discovery_phase": "frontier",
                    "stop_reason": "source_exhausted",
                }
            },
            "formal_validation": {"candidate_count": 20},
            "import_result": {"inserted_rows": 20},
            "import_completion_met": True,
        },
        imported_completion_verified=False,
    )
    fingerprint = xhs_query_fingerprint(target())
    checkpoint = load_checkpoint(
        conn,
        target_key="qingdao_travel",
        account_id="xhs-a01",
        query_fingerprint_value=fingerprint,
    )
    conn.close()

    assert result["imported_completion"] is False
    assert checkpoint is not None
    assert checkpoint["last_summary_path"] == str(child_path.resolve())
    assert checkpoint["campaign_candidate_count"] == 20


def test_sqlite_import_failure_skips_checkpoint_seen_and_campaign(tmp_path: Path) -> None:
    db_path = tmp_path / "import-failure.sqlite"
    conn = prepare_connection(db_path)
    plan = resolve_discovery_plan(conn, target=target(), account_id="xhs-a01")
    child_path = tmp_path / "import-failure-summary.json"
    child_path.write_text("{}", encoding="utf-8")

    result = commit_child_discovery(
        conn,
        target=target(),
        account_id="xhs-a01",
        run_id="run-import-failure",
        discovery_plan=plan,
        child_summary_path=child_path,
        child_summary={
            "pagination_evidence": {
                "stop_event": {
                    "source_page": 1,
                    "resume_page": 2,
                    "resume_cursor": "next-search-id",
                    "source_has_more": True,
                    "batch_complete": True,
                    "discovery_phase": "frontier",
                    "stop_reason": "source_exhausted",
                    "candidate_identities": ["not-imported-note"],
                }
            },
            "formal_validation": {"candidate_count": 1},
            "import_result": {
                "reason": "sqlite_import_failed",
                "inserted_rows": 0,
                "rolled_back_images": 1,
            },
            "import_completion_met": False,
        },
    )
    fingerprint = xhs_query_fingerprint(target())
    checkpoint = load_checkpoint(
        conn,
        target_key="qingdao_travel",
        account_id="xhs-a01",
        query_fingerprint_value=fingerprint,
    )
    seen_count = conn.execute(
        "SELECT COUNT(*) FROM xhs_discovery_seen_candidates"
    ).fetchone()[0]
    conn.close()

    assert result == {"skipped": True, "reason": "sqlite_import_failed"}
    assert checkpoint is None
    assert seen_count == 0


def test_incomplete_last_page_does_not_mark_frontier_exhausted(tmp_path: Path) -> None:
    conn = prepare_connection(tmp_path / "incomplete.sqlite")
    plan = resolve_discovery_plan(conn, target=target(), account_id="xhs-a01")
    child_path = tmp_path / "incomplete-summary.json"
    child_path.write_text("{}", encoding="utf-8")

    commit_child_discovery(
        conn,
        target=target(),
        account_id="xhs-a01",
        run_id="run-incomplete",
        discovery_plan=plan,
        child_summary_path=child_path,
        child_summary={
            "pagination_evidence": {
                "stop_event": {
                    "source_page": 1,
                    "resume_page": 1,
                    "resume_cursor": "search-id",
                    "source_has_more": False,
                    "batch_complete": False,
                    "discovery_phase": "frontier",
                    "stop_reason": "runtime_failed",
                    "candidate_identities": ["processed-on-boundary"],
                }
            },
            "formal_validation": {"candidate_count": 1},
            "import_result": {"skipped": True},
            "import_completion_met": False,
        },
    )
    fingerprint = xhs_query_fingerprint(target())
    checkpoint = load_checkpoint(
        conn,
        target_key="qingdao_travel",
        account_id="xhs-a01",
        query_fingerprint_value=fingerprint,
    )
    conn.close()

    assert checkpoint is not None
    assert checkpoint["status"] == "active"
    assert checkpoint["source_has_more"] is None
    assert checkpoint["resume_page"] == 1
