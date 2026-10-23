"""小红书抓取记忆：每轮全量扫描，checkpoint 只作审计与累计摘要，seen 记已决定候选。"""

from __future__ import annotations

import json
import sqlite3
import sys
from importlib import import_module
from pathlib import Path

import pytest

from trippostcollect.db.bootstrap import bootstrap_connection
from trippostcollect.scheduler.discovery import query_fingerprint
from trippostcollect.xhs import discovery as xhs_discovery
from trippostcollect.xhs.config import load_pool_config, load_target
from trippostcollect.xhs.discovery import (
    commit_child_discovery,
    load_checkpoint,
    resolve_discovery_plan,
    save_checkpoint,
    save_seen_candidates,
    update_campaign,
    xhs_query_fingerprint,
)
from trippostcollect.xhs.search_target import target_search_fingerprint_params

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def target(**overrides: object) -> dict[str, object]:
    return {
        "target_key": "qingdao_travel",
        "keyword": "青岛旅游",
        **overrides,
    }


def prepare_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    bootstrap_connection(conn, sync_content=False, sync_jobs=False)
    for account_id in ("xhs-a01", "xhs-a02"):
        conn.execute(
            """
            INSERT INTO xhs_accounts(account_id, status)
            VALUES (?, 'active')
            """,
            (account_id,),
        )
    conn.commit()
    return conn


def before_count_rule_fingerprint() -> str:
    """#94 计数判定规则并入指纹之前（#71 起）的取值。"""
    return query_fingerprint(
        "xhs", "青岛旅游", {"target_key": "qingdao_travel", **target_search_fingerprint_params()},
    )


def test_query_fingerprint_tracks_source_target_search_definition_and_count_rule(monkeypatch) -> None:
    base = target()
    changed_source = target(target_key="qingdao_food")
    legacy = query_fingerprint("xhs", "青岛旅游", {"target_key": "qingdao_travel"})

    assert xhs_query_fingerprint(base) != xhs_query_fingerprint(changed_source)
    # 目标排序与类型、计数判定规则并入指纹：与两代旧指纹都不同，改变任一项即形成新记忆。
    assert xhs_query_fingerprint(base) != legacy
    assert xhs_query_fingerprint(base) != before_count_rule_fingerprint()
    original = xhs_query_fingerprint(base)
    monkeypatch.setattr(xhs_discovery, "COUNT_RULE", "another_rule")
    assert xhs_query_fingerprint(base) != original
    monkeypatch.undo()
    monkeypatch.setattr(
        xhs_discovery,
        "target_search_fingerprint_params",
        lambda: {"search_sort": "general", "search_note_type": "video"},
    )
    assert xhs_query_fingerprint(base) != original


def test_count_rule_upgrade_dry_run_starts_new_memory_and_leaves_the_old_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """#94 规则改变后首轮：旧指纹下有未完成累计（批次摘要 status=incomplete）与 seen，新指纹 dry-run 显示
    checkpoint_found=false；不报错、不要求先跑完旧轮次，不把旧累计交给 child；旧 checkpoint 与 seen 行原样保留。"""
    xhs_runner = import_module("xhs_runner")
    db_path = tmp_path / "xhs.sqlite"
    conn = prepare_connection(db_path)
    old = before_count_rule_fingerprint()
    old_summary = tmp_path / "batch-000002.summary.json"
    old_summary.write_text(
        json.dumps({
            "import_completion_met": False,
            "records": [{"platform": "xhs", "status": "incomplete", "ok": False, "output": {}}],
        }),
        encoding="utf-8",
    )
    save_checkpoint(
        conn, target_key="qingdao_travel", account_id="xhs-a01", keyword="青岛旅游", query_fingerprint_value=old,
        source_has_more=True, source_exhausted=False, last_batch_complete=True, last_stop_reason="continue",
        last_run_id="round-8",
    )
    update_campaign(
        conn, target_key="qingdao_travel", account_id="xhs-a01", query_fingerprint_value=old,
        summary_path=str(old_summary), candidate_count=389,
    )
    save_seen_candidates(
        conn, target_key="qingdao_travel", account_id="xhs-a01", query_fingerprint_value=old,
        platform_post_ids=["blank-count-note", "valid-note"], run_id="round-8",
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(xhs_runner, "XHS_RUNTIME_ROOT", tmp_path / "runtime")
    monkeypatch.setattr(xhs_runner, "XHS_EXECUTION_STATE_ROOT", tmp_path / "states")
    monkeypatch.setattr(
        sys, "argv",
        ["xhs_runner.py", "--target-key", "qingdao_travel", "--account-id", "xhs-a01", "--db", str(db_path),
         "--dry-run"],
    )

    assert xhs_runner._run_main(xhs_runner.parse_args()) == 0

    output = json.loads(capsys.readouterr().out)
    discovery = output["plan"]["discovery"]
    assert discovery["query_fingerprint"] == xhs_query_fingerprint(target()) != old
    assert discovery["checkpoint_found"] is False and discovery["checkpoint_before"] is None
    assert (discovery["last_status"], discovery["campaign_summary_path"]) == ("", "")
    assert discovery["campaign_visit_ledgers"] == [] and discovery["seen_candidate_count"] == 0
    # 旧累计不进冻结输入，也不交给 child。
    assert str(old_summary) not in Path(output["state"]).read_text(encoding="utf-8")
    command = xhs_runner.build_child_command(
        target=load_target("qingdao_travel", xhs_runner.XHS_TARGET_CONFIG),
        pool=load_pool_config(xhs_runner.XHS_POOL_CONFIG), account={"account_id": "xhs-a01"},
        profile_dir=tmp_path / "profile", db_path=db_path, output_root=tmp_path / "output", no_import=False,
        post_interaction="none", discovery=discovery,
    )
    assert "--resume-summary" not in command
    assert command[command.index("--xhs-discovery-query-fingerprint") + 1] == discovery["query_fingerprint"]
    # 旧指纹的 checkpoint、累计指针与 seen 行原样保留作审计。
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        kept = load_checkpoint(conn, target_key="qingdao_travel", account_id="xhs-a01", query_fingerprint_value=old)
        assert kept is not None and kept["last_summary_path"] == str(old_summary)
        assert {row[0] for row in conn.execute("SELECT query_fingerprint FROM xhs_discovery_checkpoints")} == {old}
        assert conn.execute(
            "SELECT COUNT(*) FROM xhs_discovery_seen_candidates WHERE query_fingerprint=?", (old,),
        ).fetchone()[0] == 2


def test_checkpoint_isolated_per_account_and_resolves_campaign(tmp_path: Path) -> None:
    conn = prepare_connection(tmp_path / "xhs.sqlite")
    summary_path = tmp_path / "summary.json"
    snapshot = tmp_path / "behavior_evidence.content_visits.jsonl.batch-000003.snapshot"
    summary_path.write_text(
        json.dumps({"records": [{"output": {"content_visit_paths": [str(snapshot)]}}, {"output": {}}]}),
        encoding="utf-8",
    )
    fingerprint = xhs_query_fingerprint(target())
    save_checkpoint(
        conn,
        target_key="qingdao_travel",
        account_id="xhs-a01",
        keyword="青岛旅游",
        query_fingerprint_value=fingerprint,
        source_has_more=True,
        source_exhausted=False,
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
    assert first["last_status"] == "active"
    assert first["last_stop_reason"] == "runtime_failed"
    assert first["campaign_summary_path"] == str(summary_path.resolve())
    assert first["campaign_visit_ledgers"] == [str(snapshot)]
    assert first["campaign_visit_ledger_count"] == 1
    assert first["checkpoint_before"]["last_run_id"] == "run-1"
    for removed in ("resume_page", "resume_search_id", "source_exhausted", "top_refresh_max_pages"):
        assert removed not in first
    assert second["checkpoint_found"] is False
    assert second["campaign_summary_path"] == ""
    assert second["campaign_visit_ledgers"] == []
    assert second["seen_candidate_count"] == 0


def test_missing_campaign_summary_blocks_resume(tmp_path: Path) -> None:
    conn = prepare_connection(tmp_path / "missing.sqlite")
    fingerprint = xhs_query_fingerprint(target())
    save_checkpoint(
        conn,
        target_key="qingdao_travel",
        account_id="xhs-a01",
        keyword="青岛旅游",
        query_fingerprint_value=fingerprint,
        source_has_more=True,
        source_exhausted=False,
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


def test_complete_source_exhaustion_is_recorded_as_audit_status_and_saves_seen(tmp_path: Path) -> None:
    conn = prepare_connection(tmp_path / "exhausted.sqlite")
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
                    "source_page": 5,
                    "source_cursor": "target-search-id",
                    "resume_page": None,
                    "resume_cursor": None,
                    "source_has_more": False,
                    "batch_complete": True,
                    "discovery_phase": "full_scan",
                    "stop_reason": "source_exhausted",
                    "candidate_identities": ["top-note", "video-note"],
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
    seen_count = conn.execute("SELECT COUNT(*) FROM xhs_discovery_seen_candidates").fetchone()[0]
    next_plan = resolve_discovery_plan(conn, target=target(), account_id="xhs-a01")
    conn.close()

    assert result["source_exhausted"] is True
    assert result["imported_completion"] is True
    assert result["seen_candidate_count"] == 2
    assert "resume_page" not in result and "refresh_only" not in result
    assert checkpoint is not None
    assert checkpoint["status"] == "exhausted"
    assert checkpoint["source_has_more"] == 0
    assert checkpoint["last_summary_path"] is None
    assert checkpoint["campaign_candidate_count"] == 0
    assert seen_count == 2
    # 下一轮照样从第 1 页全量扫描，只是滑过已决定的候选。
    assert next_plan["last_status"] == "exhausted" and next_plan["seen_candidate_count"] == 2


def test_runtime_failure_after_complete_batches_stays_active(tmp_path: Path) -> None:
    conn = prepare_connection(tmp_path / "frontier.sqlite")
    plan = resolve_discovery_plan(conn, target=target(), account_id="xhs-a01")
    child_path = tmp_path / "failed-summary.json"
    child_path.write_text("{}", encoding="utf-8")

    result = commit_child_discovery(
        conn,
        target=target(),
        account_id="xhs-a01",
        run_id="run-failed",
        discovery_plan=plan,
        child_summary_path=child_path,
        child_summary={
            "pagination_evidence": {
                "stop_event": {
                    "source_page": 4,
                    "resume_page": None,
                    "source_has_more": True,
                    "batch_complete": False,
                    "discovery_phase": "full_scan",
                    "stop_reason": "runtime_failed",
                    "candidate_identities": ["decided-note"],
                }
            },
            "formal_validation": {"candidate_count": 3},
            "import_result": {"skipped": True, "reason": "source_not_exhausted"},
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

    assert result["source_exhausted"] is False
    assert checkpoint is not None
    assert checkpoint["status"] == "active"
    assert checkpoint["last_stop_reason"] == "runtime_failed"
    assert checkpoint["last_summary_path"] == str(child_path.resolve())
    assert checkpoint["campaign_candidate_count"] == 3


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
                    "resume_page": None,
                    "source_has_more": False,
                    "batch_complete": True,
                    "discovery_phase": "full_scan",
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
                    "resume_page": None,
                    "source_has_more": True,
                    "batch_complete": True,
                    "discovery_phase": "full_scan",
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


def test_incomplete_last_page_does_not_mark_source_exhausted(tmp_path: Path) -> None:
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
                    "resume_page": None,
                    "source_has_more": False,
                    "batch_complete": False,
                    "discovery_phase": "full_scan",
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
